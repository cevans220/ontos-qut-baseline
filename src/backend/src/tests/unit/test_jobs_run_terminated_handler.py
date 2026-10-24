"""The job poller must hand finished runs to the registered per-workflow handler.

Features that keep their own run records (Metadata Generation) rely on this
backstop to finish runs nobody has open in the UI. Covers TERMINATED as well
as SKIPPED / INTERNAL_ERROR (which have no result state), and that running or
already-notified runs are not handed over.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from databricks.sdk.service.jobs import RunLifeCycleState, RunResultState

from src.controller.jobs_manager import JobsManager


def _poll_once(life_cycle_state, result_state, *, notified_at=None):
    mgr = JobsManager(db=MagicMock(), ws_client=MagicMock())
    handler = MagicMock()
    mgr.register_run_terminated_handler("metagen_generate", handler)

    installation = SimpleNamespace(
        id="inst-1", workflow_id="metagen_generate", name="Metadata Generation", job_id=42, last_polled_at=None,
    )
    run = SimpleNamespace(
        run_id=777,
        run_name="metagen",
        start_time=0,
        end_time=0,
        state=SimpleNamespace(life_cycle_state=life_cycle_state, result_state=result_state, state_message=""),
    )
    mgr._client.jobs.list_runs.return_value = [run]
    session = MagicMock()

    def fake_get_db():
        yield session

    def stop_after_first(timeout=None):
        mgr._stop_polling.set()
        return True

    with patch("src.common.database.get_db", fake_get_db), \
         patch.object(mgr._stop_polling, "wait", side_effect=stop_after_first), \
         patch("src.controller.jobs_manager.workflow_installation_repo") as inst_repo, \
         patch("src.controller.jobs_manager.workflow_job_run_repo") as run_repo, \
         patch.object(mgr, "_create_job_failure_notification"), \
         patch.object(mgr, "_create_job_success_notification"):
        inst_repo.get_all_installed.return_value = [installation]
        run_repo.upsert_run.return_value = SimpleNamespace(notified_at=notified_at)
        mgr._poll_job_states(interval_seconds=0)
    return handler, session


def test_terminated_success_run_is_handed_to_handler():
    handler, session = _poll_once(RunLifeCycleState.TERMINATED, RunResultState.SUCCESS)
    handler.assert_called_once_with(session, 777, "SUCCESS")


@pytest.mark.parametrize("state", [RunLifeCycleState.SKIPPED, RunLifeCycleState.INTERNAL_ERROR])
def test_runs_without_result_state_pass_their_life_cycle_state(state):
    handler, session = _poll_once(state, None)
    handler.assert_called_once_with(session, 777, state.value)


def test_running_run_is_not_handed_over():
    handler, _ = _poll_once(RunLifeCycleState.RUNNING, None)
    handler.assert_not_called()


def test_already_notified_run_is_not_handed_over_again():
    handler, _ = _poll_once(RunLifeCycleState.TERMINATED, RunResultState.SUCCESS, notified_at="2026-10-24")
    handler.assert_not_called()
