"""
Metadata Generation manager.

A Generation Run runs the ``metagen_generate`` background job (dbxmetagen in
comment mode) over a selection of UC objects. The job writes normalized rows
to the Delta table ``<DATABRICKS_CATALOG>.<DATABRICKS_SCHEMA>.metagen_results``;
when the job finishes, this manager ingests them as Description Suggestions.

Run status is derived from the Jobs API, refreshed on demand by the run page
and by the JobsManager background poller (see ``handle_job_terminated``).
See docs/decisions/metadata-generation-execution.md.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from src.common.config import Settings, resolve_volume
from src.common.errors import ConflictError
from src.common.logging import get_logger
from src.db_models.metadata_generation import DescriptionSuggestionDb, MetadataGenerationRunDb
from src.models.metadata_generation import (
    TERMINAL_RUN_STATUSES,
    GenerationRunStatus,
    StartRunRequest,
    SuggestionStatus,
)
from src.repositories.metadata_generation_repository import (
    metagen_runs_repo,
    metagen_suggestions_repo,
)

logger = get_logger(__name__)

WORKFLOW_ID = "metagen_generate"
RESULTS_TABLE = "metagen_results"
DEFAULT_MODEL = "databricks-claude-sonnet-4-6"
MAX_ACTIVE_RUNS_PER_USER = 1
# An ingest claim older than this is treated as abandoned (e.g. the app restarted)
STALE_INGEST_CLAIM = timedelta(minutes=15)

# Jobs API life-cycle states after which a run will not change any more
_TERMINAL_LIFE_CYCLE_STATES = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}


class ActiveRunLimitError(ValueError):
    """The user already has the maximum number of runs in flight."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def selections_overlap(a: str, b: str) -> bool:
    """Whether two ``cat[.sch[.tbl]]`` names cover a common table."""
    a, b = a.lower(), b.lower()
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def selection_to_table_names(selection: List[Dict[str, str]]) -> List[str]:
    """Flatten a selection into the ``cat[.sch[.tbl]]`` names the job expands."""
    names: List[str] = []
    for item in selection:
        fqn = (item.get("fqn") or "").strip()
        expected_parts = {"catalog": 1, "schema": 2, "table": 3}[item["type"]]
        if fqn.count(".") + 1 != expected_parts:
            raise ValueError(f"'{fqn}' is not a valid {item['type']} name")
        names.append(fqn)
    return names


class MetadataGenerationManager:

    def __init__(
        self,
        *,
        settings: Settings,
        jobs_manager: Any = None,
        workspace_client_factory: Optional[Callable[[], Any]] = None,
    ):
        self._settings = settings
        self._jobs = jobs_manager
        self._ws_factory = workspace_client_factory
        if self._jobs is not None and hasattr(self._jobs, "register_run_terminated_handler"):
            self._jobs.register_run_terminated_handler(WORKFLOW_ID, self.handle_job_terminated)

    def _output_schema(self) -> Tuple[str, str]:
        """The app's deploy-time catalog and schema.

        Databricks Apps deployments inject the volume path (its catalog and
        schema win); bundle and local deployments set DATABRICKS_CATALOG /
        DATABRICKS_SCHEMA. A volume that isn't a UC volume (a local path in
        development) doesn't name a schema.
        """
        try:
            resolved = resolve_volume(self._settings)
        except ValueError:
            resolved = None
        if resolved:
            return resolved[0], resolved[1]
        return self._settings.DATABRICKS_CATALOG, self._settings.DATABRICKS_SCHEMA

    # ------------------------------------------------------------------
    # Start / cancel / delete
    # ------------------------------------------------------------------
    def start_run(self, db: Session, *, user_id: str, request: StartRunRequest) -> MetadataGenerationRunDb:
        """Create a Generation Run and launch the background job."""
        self._refresh_active_runs(db)
        if metagen_runs_repo.count_active_for_user(db, user_id) >= MAX_ACTIVE_RUNS_PER_USER:
            raise ActiveRunLimitError("You already have a Generation Run in progress. Wait for it to finish or cancel it.")
        selection = [item.model_dump() for item in request.selection]
        table_names = selection_to_table_names(selection)
        if not self._jobs:
            raise ValueError("Jobs manager not available")
        self._check_no_overlapping_run(db, table_names)

        from src.repositories.workflow_installations_repository import workflow_installation_repo
        installation = workflow_installation_repo.get_by_workflow_id(db=db, workflow_id=WORKFLOW_ID)
        if not installation:
            raise ConflictError(
                "The Metadata Generation background job is not enabled. "
                "Ask an administrator to enable it under Settings > Jobs."
            )

        model = request.model or self._settings.LLM_ENDPOINT or DEFAULT_MODEL
        options = {
            "comment_style": request.comment_style.value,
            "model": model,
            # Data sampling stays off until the admin-gated toggle lands (#1188)
            "allow_data": False,
            "sample_size": 0,
            "regenerate_existing": request.regenerate_existing,
            "auto_submit": request.auto_submit,
        }

        run = metagen_runs_repo.create(db, user_id=user_id, selection=selection, options=options)
        db.commit()

        output_catalog, output_schema = self._output_schema()
        job_params = {
            "ontos_run_id": run.id,
            "table_names": ",".join(table_names),
            "output_catalog": output_catalog,
            "output_schema": output_schema,
            "model": model,
            "comment_style": options["comment_style"],
            "allow_data": "false",
            "sample_size": "0",
            "regenerate_existing": str(request.regenerate_existing).lower(),
        }
        try:
            job_run_id = self._jobs.run_job(
                job_id=int(installation.job_id),
                job_name=WORKFLOW_ID,
                workflow_id=WORKFLOW_ID,
                job_parameters=job_params,
            )
        except Exception as e:
            logger.error("Failed to launch metadata generation job for run %s: %s", run.id, e, exc_info=True)
            run.status = GenerationRunStatus.FAILED.value
            run.error = "Failed to start the background job"
            run.completed_at = _now()
            db.commit()
            raise

        run.job_run_id = str(job_run_id)
        run.status = GenerationRunStatus.RUNNING.value
        run.started_at = _now()
        run.progress_message = "Generating descriptions"
        db.commit()
        db.refresh(run)
        return run

    def _check_no_overlapping_run(self, db: Session, table_names: List[str]) -> None:
        """Reject a run whose objects an active run is already describing.

        dbxmetagen's log carries no run id, so the job attributes its rows by
        table and time; two concurrent runs over the same table could pick up
        each other's output.
        """
        for other in metagen_runs_repo.list_active(db):
            for item in other.selection or []:
                for name in table_names:
                    if selections_overlap(name, item.get("fqn", "")):
                        raise ConflictError(
                            f"Another Generation Run is already describing objects in {item['fqn']}. "
                            "Wait for it to finish or cancel it."
                        )

    def cancel_run(self, db: Session, run: MetadataGenerationRunDb) -> MetadataGenerationRunDb:
        if run.status in TERMINAL_RUN_STATUSES:
            raise ValueError(f"Run is already {run.status}")
        if run.job_run_id and self._jobs:
            self._jobs.cancel_run(int(run.job_run_id))
        run.status = GenerationRunStatus.CANCELLED.value
        run.progress_message = None
        run.completed_at = _now()
        db.commit()
        db.refresh(run)
        return run

    def delete_run(self, db: Session, run: MetadataGenerationRunDb) -> None:
        if run.status not in TERMINAL_RUN_STATUSES:
            raise ValueError("Cancel the run before deleting it")
        in_review = (
            db.query(DescriptionSuggestionDb.id)
            .filter(
                DescriptionSuggestionDb.run_id == run.id,
                DescriptionSuggestionDb.review_request_id.isnot(None),
                DescriptionSuggestionDb.status.in_((SuggestionStatus.PENDING.value, SuggestionStatus.ACCEPTED.value)),
            )
            .first()
        )
        if in_review:
            raise ValueError("The run has suggestions in an open review")
        metagen_runs_repo.delete(db, run)
        db.commit()

    # ------------------------------------------------------------------
    # Status refresh
    # ------------------------------------------------------------------
    def refresh_run(
        self,
        db: Session,
        run: MetadataGenerationRunDb,
        *,
        defer_ingest: Optional[Callable[[str], None]] = None,
    ) -> MetadataGenerationRunDb:
        """Pull the job state from the Jobs API and finish the run if it terminated.

        ``defer_ingest`` (run id -> None) hands a successful run's ingest to
        the caller instead of running it inline; request handlers use it so a
        slow warehouse read doesn't hold the request.
        """
        if run.status in TERMINAL_RUN_STATUSES or not run.job_run_id or not self._jobs:
            return run
        if run.status == GenerationRunStatus.INGESTING.value and not self._is_stale_claim(run):
            return run  # the job is done; someone is loading its results
        status = self._jobs.get_job_status(int(run.job_run_id))
        if status and status.get("life_cycle_state") in _TERMINAL_LIFE_CYCLE_STATES:
            # SKIPPED / INTERNAL_ERROR runs have no result state
            result_state = status.get("result_state") or status.get("life_cycle_state")
            if result_state == "SUCCESS" and defer_ingest is not None:
                defer_ingest(run.id)
            else:
                self._finish(db, run, result_state)
        return run

    @staticmethod
    def _is_stale_claim(run: MetadataGenerationRunDb) -> bool:
        updated_at = run.updated_at
        if updated_at is None:
            return True
        if updated_at.tzinfo is None:  # SQLite returns naive UTC
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return updated_at < _now() - STALE_INGEST_CLAIM

    def ingest_run_by_id(self, run_id: str) -> None:
        """Ingest a finished run in its own session (for background tasks)."""
        from src.common.database import get_session_factory

        db = get_session_factory()()
        try:
            run = metagen_runs_repo.get(db, run_id)
            if run is not None:
                self.ingest_results(db, run)
        except Exception:
            logger.error("Background ingest of metadata generation run %s failed", run_id, exc_info=True)
        finally:
            db.close()

    def _refresh_active_runs(self, db: Session) -> None:
        """Finish active runs whose job ended, before they count against limits.

        Covers runs nobody has open and whose poller notification was already
        used up, e.g. an ingest claim abandoned by an app restart (taken over
        once stale).
        """
        for run in metagen_runs_repo.list_active(db):
            try:
                self.refresh_run(db, run)
            except Exception:
                logger.warning("Failed to refresh metadata generation run %s", run.id, exc_info=True)

    def handle_job_terminated(self, db: Session, job_run_id: int, result_state: Optional[str]) -> None:
        """JobsManager poller callback for terminated ``metagen_generate`` runs."""
        run = metagen_runs_repo.get_by_job_run_id(db, str(job_run_id))
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return
        self._finish(db, run, result_state)

    def _finish(self, db: Session, run: MetadataGenerationRunDb, result_state: Optional[str]) -> None:
        if result_state == "SUCCESS":
            self.ingest_results(db, run)
            return
        run.status = (
            GenerationRunStatus.CANCELLED.value if result_state == "CANCELED" else GenerationRunStatus.FAILED.value
        )
        if run.status == GenerationRunStatus.FAILED.value:
            run.error = f"Background job ended with {result_state or 'an unknown state'}"
        run.progress_message = None
        run.completed_at = _now()
        db.commit()

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    def _claim_for_ingest(self, db: Session, run: MetadataGenerationRunDb) -> bool:
        """Atomically move the run to ``ingesting``; False if someone else holds it.

        The run page's polling and the JobsManager poller can both finish the
        same run from different sessions; only the claimant ingests.
        """
        cutoff = _now() - STALE_INGEST_CLAIM
        claimed = (
            db.query(MetadataGenerationRunDb)
            .filter(
                MetadataGenerationRunDb.id == run.id,
                or_(
                    MetadataGenerationRunDb.status.in_(
                        (GenerationRunStatus.PENDING.value, GenerationRunStatus.RUNNING.value)
                    ),
                    and_(
                        MetadataGenerationRunDb.status == GenerationRunStatus.INGESTING.value,
                        MetadataGenerationRunDb.updated_at < cutoff,
                    ),
                ),
            )
            .update(
                {
                    MetadataGenerationRunDb.status: GenerationRunStatus.INGESTING.value,
                    MetadataGenerationRunDb.progress_message: "Loading generated descriptions",
                    MetadataGenerationRunDb.updated_at: _now(),
                },
                synchronize_session=False,
            )
        )
        db.commit()
        db.refresh(run)
        return claimed == 1

    def ingest_results(self, db: Session, run: MetadataGenerationRunDb) -> int:
        """Load the job's rows from Delta into Description Suggestions.

        Idempotent: a completed run, or one another worker is ingesting, is
        left alone; rows of an interrupted earlier attempt are replaced.
        """
        if run.status == GenerationRunStatus.COMPLETED.value or not self._claim_for_ingest(db, run):
            return run.suggestion_count
        try:
            rows = self._fetch_results(run.id)
            # A failure while storing rolls back only the half-written suggestions
            with db.begin_nested():
                count = self._store_suggestions(db, run, rows)
            db.commit()
            logger.info("Ingested %d description suggestions for run %s", count, run.id)
            return count
        except Exception as e:
            logger.error("Failed to ingest metadata generation results for run %s: %s", run.id, e, exc_info=True)
            run.status = GenerationRunStatus.FAILED.value
            run.error = "Failed to read the generated descriptions"
            run.progress_message = None
            run.completed_at = _now()
            db.commit()
            return 0

    def _store_suggestions(self, db: Session, run: MetadataGenerationRunDb, rows: List[Dict[str, Optional[str]]]) -> int:

        regenerate_existing = bool((run.options or {}).get("regenerate_existing"))
        db.query(DescriptionSuggestionDb).filter(DescriptionSuggestionDb.run_id == run.id).delete()

        targets = []
        for row in rows:
            base_text = (row.get("base_text") or "").strip() or None
            proposed_text = (row.get("proposed_text") or "").strip()
            if not proposed_text:
                continue
            # Fill-gaps-only by default: an existing description is kept
            if base_text and not regenerate_existing:
                continue
            if base_text == proposed_text:
                continue
            column_name = row.get("column_name") or None
            db.add(DescriptionSuggestionDb(
                run_id=run.id,
                kind="description",
                object_type=row.get("object_type") or ("column" if column_name else "table"),
                table_fqn=row["table_fqn"],
                column_name=column_name,
                base_text=base_text,
                proposed_text=proposed_text,
                status=SuggestionStatus.PENDING.value,
            ))
            targets.append((row["table_fqn"], column_name))

        # A newer run replaces older, not yet reviewed suggestions for the same objects
        metagen_suggestions_repo.supersede_pending_for_targets(db, exclude_run_id=run.id, targets=targets)

        run.suggestion_count = len(targets)
        run.status = GenerationRunStatus.COMPLETED.value
        run.progress_message = None
        run.error = None
        run.completed_at = _now()
        db.flush()
        return len(targets)

    def _fetch_results(self, run_id: str) -> List[Dict[str, Optional[str]]]:
        """Read this run's rows from the metagen_results Delta table via the SQL warehouse."""
        from databricks.sdk.service.sql import StatementParameterListItem, StatementState

        ws = self._ws_factory() if self._ws_factory else None
        if ws is None:
            from src.common.workspace_client import get_workspace_client
            ws = get_workspace_client()

        table = ".".join(f"`{part}`" for part in (*self._output_schema(), RESULTS_TABLE))
        resp = ws.statement_execution.execute_statement(
            statement=(
                f"SELECT object_type, table_fqn, column_name, base_text, proposed_text "
                f"FROM {table} WHERE ontos_run_id = :run_id"
            ),
            warehouse_id=self._settings.DATABRICKS_WAREHOUSE_ID,
            parameters=[StatementParameterListItem(name="run_id", value=run_id)],
            wait_timeout="50s",
        )
        deadline = time.monotonic() + 600
        while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            if time.monotonic() > deadline:
                raise TimeoutError("Timed out reading metadata generation results")
            time.sleep(2)
            resp = ws.statement_execution.get_statement(resp.statement_id)
        if not resp.status or resp.status.state != StatementState.SUCCEEDED:
            message = resp.status.error.message if resp.status and resp.status.error else "unknown error"
            raise RuntimeError(f"Results query did not succeed: {message}")

        columns = [c.name for c in resp.manifest.schema.columns]
        data = list((resp.result.data_array if resp.result else None) or [])
        for chunk_index in range(1, resp.manifest.total_chunk_count or 1):
            chunk = ws.statement_execution.get_statement_result_chunk_n(resp.statement_id, chunk_index)
            data.extend(chunk.data_array or [])
        return [dict(zip(columns, values)) for values in data]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_run(self, db: Session, run_id: str) -> Optional[MetadataGenerationRunDb]:
        return metagen_runs_repo.get(db, run_id)

    def list_runs(self, db: Session, user_id: Optional[str], *, limit: int = 50) -> List[MetadataGenerationRunDb]:
        if user_id is None:
            return metagen_runs_repo.list_all(db, limit=limit)
        return metagen_runs_repo.list_for_user(db, user_id, limit=limit)

    def counts(self, db: Session, run_id: str) -> Dict[str, int]:
        return metagen_suggestions_repo.counts_by_status(db, run_id)

    def list_suggestions(
        self, db: Session, run_id: str, *, status: Optional[str] = None, limit: int = 1000, skip: int = 0
    ) -> List[DescriptionSuggestionDb]:
        return metagen_suggestions_repo.list_for_run(db, run_id, status=status, limit=limit, skip=skip)

    def count_suggestions(self, db: Session, run_id: str, *, status: Optional[str] = None) -> int:
        return metagen_suggestions_repo.count_for_run(db, run_id, status=status)
