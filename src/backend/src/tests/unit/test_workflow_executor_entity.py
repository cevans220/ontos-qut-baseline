"""The execution model returned by the executor carries the trigger entity."""
import os
os.environ['TESTING'] = 'true'
os.environ['SKIP_STARTUP_TASKS'] = 'true'

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.common.workflow_executor import WorkflowExecutor
from src.models.process_workflows import TriggerContext, TriggerType


def _db_execution(trigger_context):
    return SimpleNamespace(
        id='exec-1',
        workflow_id='wf-1',
        trigger_context=trigger_context,
        status='succeeded',
        current_step_id=None,
        success_count=1,
        failure_count=0,
        error_message=None,
        started_at=None,
        finished_at=None,
        triggered_by='user@example.com',
        step_executions=[],
    )


def _executor():
    return WorkflowExecutor(MagicMock())


def test_db_to_model_carries_entity_from_trigger_context():
    tc = TriggerContext(
        entity_type='data_product',
        entity_id='dp-1',
        entity_name='Orders',
        trigger_type=TriggerType.MANUAL,
    )
    model = _executor()._db_to_model(
        _db_execution(tc.model_dump_json()), 'My workflow'
    )
    assert model.entity_type == 'data_product'
    assert model.entity_id == 'dp-1'
    assert model.entity_name == 'Orders'


def test_db_to_model_without_trigger_context_leaves_entity_empty():
    model = _executor()._db_to_model(_db_execution(None), 'My workflow')
    assert model.entity_type is None
    assert model.entity_id is None
    assert model.entity_name is None


def test_db_to_model_ignores_invalid_trigger_context():
    model = _executor()._db_to_model(_db_execution('not json'), 'My workflow')
    assert model.entity_id is None
