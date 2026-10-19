"""Saving a workflow's steps keeps the step results of its past executions.

Step results (``workflow_step_executions``) reference the step's row id, with
``ON DELETE CASCADE``. Every save (the designer, ``PUT /api/workflows/{id}``
with ``steps``, Load Defaults with ``update_existing``) deleted all the steps
and created them again, so the results of every past execution were deleted
and Recent Executions showed their steps as Pending.
"""
import os

os.environ['TESTING'] = 'true'
os.environ['SKIP_STARTUP_TASKS'] = 'true'

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.common.database import Base
from src.db_models.process_workflows import WorkflowStepDb, WorkflowStepExecutionDb
from src.models.process_workflows import (
    EntityType,
    ProcessWorkflowCreate,
    ProcessWorkflowUpdate,
    StepType,
    TriggerType,
    WorkflowStepCreate,
    WorkflowTrigger,
)
from src.repositories.process_workflows_repository import (
    process_workflow_repo,
    workflow_execution_repo,
)
from src.models.process_workflows import WorkflowExecutionCreate


def _steps(done_name='Done'):
    return [
        WorkflowStepCreate(
            step_id='check', name='Check', step_type=StepType.CONDITIONAL,
            config={'condition': "obj.name != ''"}, on_pass='done', on_fail='stop', order=0,
        ),
        WorkflowStepCreate(step_id='done', name=done_name, step_type=StepType.PASS, order=1),
        WorkflowStepCreate(step_id='stop', name='Stop', step_type=StepType.FAIL, order=2),
    ]


def _workflow(db, name='Kept results'):
    return process_workflow_repo.create(db, ProcessWorkflowCreate(
        name=name,
        trigger=WorkflowTrigger(type=TriggerType.MANUAL, entity_types=[EntityType.DATA_PRODUCT]),
        steps=_steps(),
    ))


def _run(db, workflow):
    """A past execution with a result for 'check' and 'done'."""
    execution = workflow_execution_repo.create(db, WorkflowExecutionCreate(
        workflow_id=workflow.id,
    ))
    rows = {s.step_id: s.id for s in workflow.steps}
    for step_id in ('check', 'done'):
        workflow_execution_repo.add_step_execution(
            db, execution.id, rows[step_id], status='succeeded', passed=True,
        )
    return execution


def _row_ids(workflow):
    return {s.step_id: s.id for s in workflow.steps}


def test_saving_the_same_steps_keeps_their_rows(db_session):
    workflow = _workflow(db_session)
    before = _row_ids(workflow)

    updated = process_workflow_repo.update(
        db_session, workflow.id, ProcessWorkflowUpdate(steps=_steps('Done (renamed)')),
    )

    assert _row_ids(updated) == before
    assert {s.step_id: s.name for s in updated.steps}['done'] == 'Done (renamed)'


def test_a_past_execution_still_points_at_the_saved_steps(db_session):
    workflow = _workflow(db_session)
    execution = _run(db_session, workflow)

    updated = process_workflow_repo.update(
        db_session, workflow.id, ProcessWorkflowUpdate(steps=_steps('Done (renamed)')),
    )

    current = set(_row_ids(updated).values())
    results = (
        db_session.query(WorkflowStepExecutionDb)
        .filter(WorkflowStepExecutionDb.execution_id == execution.id)
        .all()
    )
    assert len(results) == 2
    assert {r.step_id for r in results} <= current


def test_added_and_removed_steps(db_session):
    workflow = _workflow(db_session)
    before = _row_ids(workflow)
    steps = _steps()[:2] + [
        WorkflowStepCreate(step_id='notify', name='Notify', step_type=StepType.NOTIFICATION,
                           config={'recipients': 'requester', 'template': 'custom'}, order=2),
    ]

    updated = process_workflow_repo.update(
        db_session, workflow.id, ProcessWorkflowUpdate(steps=steps),
    )

    after = _row_ids(updated)
    assert set(after) == {'check', 'done', 'notify'}
    assert after['check'] == before['check'] and after['done'] == before['done']
    assert after['notify'] not in before.values()
    remaining = db_session.query(WorkflowStepDb).filter(
        WorkflowStepDb.workflow_id == workflow.id,
    ).count()
    assert remaining == 3


@pytest.fixture
def fk_session():
    """A session on its own SQLite database with foreign keys enforced, as on Postgres."""
    engine = create_engine('sqlite://')

    @event.listens_for(engine, 'connect')
    def _fk_on(dbapi_connection, _):
        dbapi_connection.execute('PRAGMA foreign_keys=ON')

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_results_survive_a_save_when_the_database_cascades(fk_session):
    workflow = _workflow(fk_session)
    execution = _run(fk_session, workflow)

    process_workflow_repo.update(
        fk_session, workflow.id, ProcessWorkflowUpdate(steps=_steps('Done (renamed)')),
    )
    fk_session.expire_all()

    count = fk_session.query(WorkflowStepExecutionDb).filter(
        WorkflowStepExecutionDb.execution_id == execution.id,
    ).count()
    assert count == 2


def test_only_the_results_of_a_removed_step_go_with_it(fk_session):
    workflow = _workflow(fk_session)
    execution = _run(fk_session, workflow)
    kept_check = _row_ids(workflow)['check']

    process_workflow_repo.update(
        fk_session, workflow.id, ProcessWorkflowUpdate(steps=[_steps()[0], _steps()[2]]),
    )
    fk_session.expire_all()

    results = fk_session.query(WorkflowStepExecutionDb).filter(
        WorkflowStepExecutionDb.execution_id == execution.id,
    ).all()
    assert [r.step_id for r in results] == [kept_check]
