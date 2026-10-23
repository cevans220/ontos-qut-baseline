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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from src.common.config import Settings
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

# Jobs API life-cycle states after which a run will not change any more
_TERMINAL_LIFE_CYCLE_STATES = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}


class ActiveRunLimitError(ValueError):
    """The user already has the maximum number of runs in flight."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    # ------------------------------------------------------------------
    # Start / cancel / delete
    # ------------------------------------------------------------------
    def start_run(self, db: Session, *, user_id: str, request: StartRunRequest) -> MetadataGenerationRunDb:
        """Create a Generation Run and launch the background job."""
        if metagen_runs_repo.count_active_for_user(db, user_id) >= MAX_ACTIVE_RUNS_PER_USER:
            raise ActiveRunLimitError("You already have a Generation Run in progress. Wait for it to finish or cancel it.")
        if not self._jobs:
            raise ValueError("Jobs manager not available")

        from src.repositories.workflow_installations_repository import workflow_installation_repo
        installation = workflow_installation_repo.get_by_workflow_id(db=db, workflow_id=WORKFLOW_ID)
        if not installation:
            raise ConflictError(
                "The Metadata Generation background job is not enabled. "
                "Ask an administrator to enable it under Settings > Jobs."
            )

        selection = [item.model_dump() for item in request.selection]
        table_names = selection_to_table_names(selection)
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

        job_params = {
            "ontos_run_id": run.id,
            "table_names": ",".join(table_names),
            "output_catalog": self._settings.DATABRICKS_CATALOG,
            "output_schema": self._settings.DATABRICKS_SCHEMA,
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
    def refresh_run(self, db: Session, run: MetadataGenerationRunDb) -> MetadataGenerationRunDb:
        """Pull the job state from the Jobs API and finish the run if it terminated."""
        if run.status in TERMINAL_RUN_STATUSES or not run.job_run_id or not self._jobs:
            return run
        status = self._jobs.get_job_status(int(run.job_run_id))
        if status and status.get("life_cycle_state") in _TERMINAL_LIFE_CYCLE_STATES:
            self._finish(db, run, status.get("result_state"))
        return run

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
    def ingest_results(self, db: Session, run: MetadataGenerationRunDb) -> int:
        """Load the job's rows from Delta into Description Suggestions.

        Idempotent: a run that is already completed is left alone, and rows of
        an interrupted earlier attempt are replaced.
        """
        if run.status == GenerationRunStatus.COMPLETED.value:
            return run.suggestion_count
        try:
            rows = self._fetch_results(run.id)
        except Exception as e:
            logger.error("Failed to read metadata generation results for run %s: %s", run.id, e, exc_info=True)
            run.status = GenerationRunStatus.FAILED.value
            run.error = "Failed to read the generated descriptions"
            run.progress_message = None
            run.completed_at = _now()
            db.commit()
            return 0

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
        db.commit()
        logger.info("Ingested %d description suggestions for run %s", len(targets), run.id)
        return len(targets)

    def _fetch_results(self, run_id: str) -> List[Dict[str, Optional[str]]]:
        """Read this run's rows from the metagen_results Delta table via the SQL warehouse."""
        from databricks.sdk.service.sql import StatementParameterListItem, StatementState

        ws = self._ws_factory() if self._ws_factory else None
        if ws is None:
            from src.common.workspace_client import get_workspace_client
            ws = get_workspace_client()

        table = ".".join(
            f"`{part}`" for part in (self._settings.DATABRICKS_CATALOG, self._settings.DATABRICKS_SCHEMA, RESULTS_TABLE)
        )
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
