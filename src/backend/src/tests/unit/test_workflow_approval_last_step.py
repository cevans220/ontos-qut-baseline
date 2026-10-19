"""A workflow whose last step is an approval finishes when it is decided.

On resume, the approval's branch (``on_pass`` / ``on_fail``) picks the next
step. When the approval has no next step for the decision, the loop that runs
the remaining steps never starts, and the execution stayed ``running`` (with a
``finished_at``), so it counted as neither succeeded nor failed and its
completion handlers saw ``succeeded=False`` even for an approval.
"""
import os

os.environ['TESTING'] = 'true'
os.environ['SKIP_STARTUP_TASKS'] = 'true'

import uuid
from unittest.mock import patch

from src.common.workflow_executor import WorkflowExecutor
from src.controller.workflows_manager import WorkflowsManager
from src.models.process_workflows import (
    EntityType,
    ProcessWorkflowCreate,
    StepType,
    TriggerContext,
    TriggerType,
    WorkflowStepCreate,
    WorkflowTrigger,
)
from src.repositories.process_workflows_repository import process_workflow_repo


def _workflow(db, steps):
    db_workflow = process_workflow_repo.create(db, ProcessWorkflowCreate(
        name=f'wf-{uuid.uuid4().hex[:6]}',
        trigger=WorkflowTrigger(type=TriggerType.MANUAL, entity_types=[EntityType.DATA_PRODUCT]),
        steps=steps,
    ))
    return WorkflowsManager(db).get_workflow(db_workflow.id)


def _approval(on_pass=None, on_fail=None, order=0):
    return WorkflowStepCreate(
        step_id='approve', step_type=StepType.APPROVAL,
        config={'approvers': 'reviewer@example.com'},
        on_pass=on_pass, on_fail=on_fail, order=order,
    )


def _paused(db, workflow):
    execution = WorkflowExecutor(db).execute_workflow(
        workflow=workflow,
        entity={'id': 'p-1', 'name': 'sales_prod'},
        entity_type='data_product',
        entity_id='p-1',
        entity_name='sales_prod',
        user_email='pat@example.com',
        trigger_context=TriggerContext(
            entity_type='data_product', entity_id='p-1', entity_name='sales_prod',
            trigger_type=TriggerType.MANUAL, user_email='pat@example.com',
        ),
    )
    assert execution.status.value == 'paused'
    return execution


def _decide(db, execution, approved, reason=None):
    result_data = {'decision': 'approved' if approved else 'rejected'}
    if reason:
        result_data['reason'] = reason
    return WorkflowExecutor(db).resume_workflow(
        execution_id=execution.id,
        step_result=approved,
        result_data=result_data,
        user_email='reviewer@example.com',
    )


def test_an_approved_last_approval_succeeds(db_session):
    workflow = _workflow(db_session, [_approval()])
    execution = _paused(db_session, workflow)

    resumed = _decide(db_session, execution, approved=True)

    assert resumed.status.value == 'succeeded'
    assert resumed.finished_at is not None
    assert resumed.error_message is None


def test_a_rejected_last_approval_fails_with_the_reason(db_session):
    workflow = _workflow(db_session, [_approval()])
    execution = _paused(db_session, workflow)

    resumed = _decide(db_session, execution, approved=False, reason='Not ready')

    assert resumed.status.value == 'failed'
    assert resumed.error_message == 'Not ready'


def test_a_rejection_without_an_on_fail_step_fails(db_session):
    """The approval has a next step only when approved."""
    workflow = _workflow(db_session, [
        _approval(on_pass='ok'),
        WorkflowStepCreate(step_id='ok', step_type=StepType.PASS, order=1),
    ])
    execution = _paused(db_session, workflow)

    resumed = _decide(db_session, execution, approved=False)

    assert resumed.status.value == 'failed'
    assert resumed.error_message == 'Request denied'


def test_an_approval_followed_by_a_step_still_runs_it(db_session):
    workflow = _workflow(db_session, [
        _approval(on_pass='ok'),
        WorkflowStepCreate(step_id='ok', step_type=StepType.PASS, order=1),
    ])
    execution = _paused(db_session, workflow)

    resumed = _decide(db_session, execution, approved=True)

    assert resumed.status.value == 'succeeded'
    assert resumed.success_count == 2


def test_completion_handlers_see_the_outcome(db_session):
    workflow = _workflow(db_session, [_approval()])
    execution = _paused(db_session, workflow)

    with patch.object(WorkflowExecutor, '_handle_workflow_completion') as completion:
        _decide(db_session, execution, approved=True)

    assert completion.call_args.kwargs['succeeded'] is True
    assert completion.call_args.kwargs['approved'] is True
