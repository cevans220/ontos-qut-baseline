"""Unit tests for MetadataGenerationManager (Generation Runs + ingest).

Runs against the in-memory ``db_session``; the Jobs API and the SQL warehouse
are mocked. Covers: launching the job, the active-run limit, status refresh
from the Jobs API, idempotent ingest with fill-gaps filtering, supersession of
older pending suggestions, cancel and delete rules.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks.sdk.service.sql import StatementState

from src.common.errors import ConflictError
from src.controller.metadata_generation_manager import (
    WORKFLOW_ID,
    ActiveRunLimitError,
    MetadataGenerationManager,
    selection_to_table_names,
)
from src.db_models.metadata_generation import DescriptionSuggestionDb
from src.db_models.workflow_installations import WorkflowInstallationDb
from src.models.metadata_generation import SelectionItem, StartRunRequest

RESULT_COLUMNS = ["object_type", "table_fqn", "column_name", "base_text", "proposed_text"]


def _settings():
    return SimpleNamespace(
        LLM_ENDPOINT="my-endpoint",
        DATABRICKS_CATALOG="app_ontos",
        DATABRICKS_SCHEMA="app_ontos",
        DATABRICKS_WAREHOUSE_ID="wh-1",
    )


def _ws_returning(rows):
    """Workspace client whose statement execution returns ``rows`` in one chunk."""
    ws = MagicMock()
    ws.statement_execution.execute_statement.return_value = SimpleNamespace(
        statement_id="st-1",
        status=SimpleNamespace(state=StatementState.SUCCEEDED, error=None),
        manifest=SimpleNamespace(
            schema=SimpleNamespace(columns=[SimpleNamespace(name=c) for c in RESULT_COLUMNS]),
            total_chunk_count=1,
        ),
        result=SimpleNamespace(data_array=rows),
    )
    return ws


def _jobs(job_run_id=111):
    jobs = MagicMock()
    jobs.run_job.return_value = job_run_id
    return jobs


def _manager(jobs=None, ws=None):
    return MetadataGenerationManager(
        settings=_settings(),
        jobs_manager=jobs or _jobs(),
        workspace_client_factory=(lambda: ws) if ws else None,
    )


def _request(**kwargs):
    return StartRunRequest(selection=[SelectionItem(type="schema", fqn="main.sales")], **kwargs)


@pytest.fixture
def installation(db_session):
    inst = WorkflowInstallationDb(workflow_id=WORKFLOW_ID, name="Metadata Generation", job_id=42)
    db_session.add(inst)
    db_session.flush()
    return inst


def test_selection_to_table_names_validates_depth():
    assert selection_to_table_names([
        {"type": "catalog", "fqn": "main"},
        {"type": "schema", "fqn": "main.sales"},
        {"type": "table", "fqn": "main.sales.orders"},
    ]) == ["main", "main.sales", "main.sales.orders"]
    with pytest.raises(ValueError):
        selection_to_table_names([{"type": "table", "fqn": "main.sales"}])


def test_registers_poller_handler():
    jobs = _jobs()
    manager = _manager(jobs=jobs)
    jobs.register_run_terminated_handler.assert_called_once_with(WORKFLOW_ID, manager.handle_job_terminated)


def test_start_run_launches_job_with_app_schema_and_default_model(db_session, installation):
    jobs = _jobs(job_run_id=777)
    run = _manager(jobs=jobs).start_run(db_session, user_id="a@x.com", request=_request(comment_style="concise"))

    assert run.status == "running"
    assert run.job_run_id == "777"
    assert run.options["model"] == "my-endpoint"
    assert run.options["allow_data"] is False
    params = jobs.run_job.call_args.kwargs["job_parameters"]
    assert params["ontos_run_id"] == run.id
    assert params["table_names"] == "main.sales"
    assert params["output_catalog"] == "app_ontos"
    assert params["comment_style"] == "concise"
    assert params["allow_data"] == "false"
    assert jobs.run_job.call_args.kwargs["job_id"] == 42


def test_start_run_requires_installed_job(db_session):
    with pytest.raises(ConflictError):
        _manager().start_run(db_session, user_id="a@x.com", request=_request())


def test_start_run_limits_active_runs_per_user(db_session, installation):
    manager = _manager()
    manager.start_run(db_session, user_id="a@x.com", request=_request())
    with pytest.raises(ActiveRunLimitError):
        manager.start_run(db_session, user_id="a@x.com", request=_request())
    # Another user is not blocked
    manager.start_run(db_session, user_id="b@x.com", request=_request())


def test_start_run_marks_run_failed_when_job_launch_fails(db_session, installation):
    jobs = _jobs()
    jobs.run_job.side_effect = RuntimeError("boom")
    manager = _manager(jobs=jobs)
    with pytest.raises(RuntimeError):
        manager.start_run(db_session, user_id="a@x.com", request=_request())
    run = manager.list_runs(db_session, "a@x.com")[0]
    assert run.status == "failed"
    assert run.error == "Failed to start the background job"


def test_refresh_ingests_on_success_with_fill_gaps_filter(db_session, installation):
    ws = _ws_returning([
        ["table", "main.sales.orders", None, None, "Customer orders."],
        ["column", "main.sales.orders", "id", None, "Order id."],
        # Already described: skipped unless regenerate_existing
        ["column", "main.sales.orders", "amount", "Total amount", "Order amount."],
        # Empty proposal: skipped
        ["column", "main.sales.orders", "note", None, "  "],
    ])
    jobs = _jobs()
    manager = _manager(jobs=jobs, ws=ws)
    run = manager.start_run(db_session, user_id="a@x.com", request=_request())

    jobs.get_job_status.return_value = {"life_cycle_state": "RUNNING", "result_state": None}
    assert manager.refresh_run(db_session, run).status == "running"

    jobs.get_job_status.return_value = {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"}
    run = manager.refresh_run(db_session, run)

    assert run.status == "completed"
    assert run.suggestion_count == 2
    suggestions = manager.list_suggestions(db_session, run.id)
    assert [(s.object_type, s.column_name) for s in suggestions] == [("table", None), ("column", "id")]
    assert all(s.status == "pending" for s in suggestions)
    sql_kwargs = ws.statement_execution.execute_statement.call_args.kwargs
    assert "`app_ontos`.`app_ontos`.`metagen_results`" in sql_kwargs["statement"]
    assert sql_kwargs["parameters"][0].value == run.id


def test_regenerate_existing_keeps_described_objects(db_session, installation):
    ws = _ws_returning([["column", "main.sales.orders", "amount", "Total amount", "Order amount."]])
    jobs = _jobs()
    manager = _manager(jobs=jobs, ws=ws)
    run = manager.start_run(db_session, user_id="a@x.com", request=_request(regenerate_existing=True))
    manager.ingest_results(db_session, run)

    [sug] = manager.list_suggestions(db_session, run.id)
    assert sug.base_text == "Total amount"
    assert sug.proposed_text == "Order amount."


def test_ingest_is_idempotent(db_session, installation):
    ws = _ws_returning([["table", "main.sales.orders", None, None, "Customer orders."]])
    manager = _manager(ws=ws)
    run = manager.start_run(db_session, user_id="a@x.com", request=_request())
    manager.ingest_results(db_session, run)
    manager.ingest_results(db_session, run)

    assert manager.count_suggestions(db_session, run.id) == 1
    assert ws.statement_execution.execute_statement.call_count == 1


def test_newer_run_supersedes_older_pending_suggestions(db_session, installation):
    rows = [["table", "main.sales.orders", None, None, "Customer orders."]]
    manager = _manager(ws=_ws_returning(rows))
    first = manager.start_run(db_session, user_id="a@x.com", request=_request())
    manager.ingest_results(db_session, first)
    second = manager.start_run(db_session, user_id="a@x.com", request=_request())
    manager.ingest_results(db_session, second)

    assert manager.counts(db_session, first.id) == {"superseded": 1}
    assert manager.counts(db_session, second.id) == {"pending": 1}


def test_poller_handler_marks_failed_and_cancelled(db_session, installation):
    jobs = _jobs(job_run_id=900)
    manager = _manager(jobs=jobs)
    run = manager.start_run(db_session, user_id="a@x.com", request=_request())
    manager.handle_job_terminated(db_session, 900, "FAILED")
    assert run.status == "failed"
    assert "FAILED" in run.error

    jobs.run_job.return_value = 901
    run2 = manager.start_run(db_session, user_id="a@x.com", request=_request())
    manager.handle_job_terminated(db_session, 901, "CANCELED")
    assert run2.status == "cancelled"
    # Unknown job runs are ignored
    manager.handle_job_terminated(db_session, 12345, "SUCCESS")


def test_ingest_failure_marks_run_failed(db_session, installation):
    ws = MagicMock()
    ws.statement_execution.execute_statement.side_effect = RuntimeError("warehouse down")
    manager = _manager(ws=ws)
    run = manager.start_run(db_session, user_id="a@x.com", request=_request())
    manager.ingest_results(db_session, run)
    assert run.status == "failed"
    assert run.error == "Failed to read the generated descriptions"


def test_cancel_and_delete_rules(db_session, installation):
    jobs = _jobs(job_run_id=555)
    manager = _manager(jobs=jobs, ws=_ws_returning([["table", "main.sales.orders", None, None, "Orders."]]))
    run = manager.start_run(db_session, user_id="a@x.com", request=_request())

    with pytest.raises(ValueError):
        manager.delete_run(db_session, run)

    manager.cancel_run(db_session, run)
    jobs.cancel_run.assert_called_once_with(555)
    assert run.status == "cancelled"
    with pytest.raises(ValueError):
        manager.cancel_run(db_session, run)

    manager.delete_run(db_session, run)
    assert manager.get_run(db_session, run.id) is None


def test_delete_blocked_while_suggestions_in_open_review(db_session, installation):
    manager = _manager(ws=_ws_returning([["table", "main.sales.orders", None, None, "Orders."]]))
    run = manager.start_run(db_session, user_id="a@x.com", request=_request())
    manager.ingest_results(db_session, run)
    sug = db_session.query(DescriptionSuggestionDb).filter_by(run_id=run.id).one()
    sug.review_request_id = "review-1"
    db_session.flush()

    with pytest.raises(ValueError, match="open review"):
        manager.delete_run(db_session, run)


def test_jobs_manager_run_terminated_handler_dispatch():
    from src.controller.jobs_manager import JobsManager

    jobs = JobsManager(db=MagicMock(), ws_client=MagicMock())
    handler = MagicMock()
    jobs.register_run_terminated_handler(WORKFLOW_ID, handler)
    db = MagicMock()

    jobs._notify_run_terminated(WORKFLOW_ID, 5, "SUCCESS", db)
    handler.assert_called_once_with(db, 5, "SUCCESS")

    # Other workflows are ignored; a failing handler is contained
    jobs._notify_run_terminated("other", 6, "SUCCESS", db)
    handler.side_effect = RuntimeError("boom")
    jobs._notify_run_terminated(WORKFLOW_ID, 7, "FAILED", db)
    db.rollback.assert_called_once()
