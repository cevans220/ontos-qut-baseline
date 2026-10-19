"""
Workflow Trigger Registry.

Provides a centralized system for registering trigger points throughout the app
and executing matching workflows when events occur.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

from src.models.process_workflows import (
    TriggerType,
    EntityType,
    TriggerContext,
    ProcessWorkflow,
    WorkflowExecution,
    ExecutionStatus,
    GateErrorMode,
)
from src.common.api_errors import CodedValueError
from src.common.logging import get_logger

logger = get_logger(__name__)


@dataclass
class TriggerEvent:
    """Represents a trigger event."""
    trigger_type: TriggerType
    entity_type: EntityType
    entity_id: str
    entity_name: Optional[str] = None
    entity_data: Optional[Dict[str, Any]] = None
    user_email: Optional[str] = None
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    scope_type: Optional[str] = None  # 'project', 'catalog', 'domain'
    scope_id: Optional[str] = None


class TriggerRegistry:
    """Registry for workflow triggers.
    
    Provides methods to fire trigger events and execute matching workflows.
    """

    def __init__(self, db: Session):
        self._db = db
        self._workflows_manager = None
        self._workflow_executor = None

    def _get_workflows_manager(self):
        """Lazy load workflows manager to avoid circular imports."""
        if self._workflows_manager is None:
            from src.controller.workflows_manager import WorkflowsManager
            self._workflows_manager = WorkflowsManager(self._db)
        return self._workflows_manager

    def _get_workflow_executor(self):
        """Lazy load workflow executor to avoid circular imports."""
        if self._workflow_executor is None:
            from src.common.workflow_executor import WorkflowExecutor
            self._workflow_executor = WorkflowExecutor(self._db)
        return self._workflow_executor

    def fire_trigger(
        self,
        event: TriggerEvent,
        *,
        blocking: bool = True,
        errors: Optional[List[Tuple[ProcessWorkflow, Exception]]] = None,
    ) -> List[WorkflowExecution]:
        """Fire a trigger event and execute matching workflows.
        
        Args:
            event: The trigger event
            blocking: If True, execute workflows synchronously
            errors: When given, each workflow whose execution raised is
                appended with its exception (it has no execution in the result)
            
        Returns:
            List of workflow executions
        """
        logger.info(
            f"Trigger fired: {event.trigger_type.value} for {event.entity_type.value} "
            f"'{event.entity_id}'"
        )
        
        # Get matching workflows
        workflows_manager = self._get_workflows_manager()
        matching_workflows = workflows_manager.get_workflows_for_trigger(
            trigger_type=event.trigger_type,
            entity_type=event.entity_type,
            scope_type=event.scope_type,
            scope_id=event.scope_id,
            from_status=event.from_status,
            to_status=event.to_status,
        )
        
        if not matching_workflows:
            logger.debug(f"No workflows match trigger: {event.trigger_type.value}")
            return []
        
        logger.info(f"Found {len(matching_workflows)} matching workflow(s)")
        
        # Build trigger context
        trigger_context = TriggerContext(
            entity_type=event.entity_type.value,
            entity_id=event.entity_id,
            entity_name=event.entity_name,
            trigger_type=event.trigger_type,
            user_email=event.user_email,
            entity_data=event.entity_data,
            from_status=event.from_status,
            to_status=event.to_status,
        )
        
        # Execute workflows
        executor = self._get_workflow_executor()
        executions = []
        
        for workflow in matching_workflows:
            try:
                execution = executor.execute_workflow(
                    workflow=workflow,
                    entity=event.entity_data or {},
                    entity_type=event.entity_type.value,
                    entity_id=event.entity_id,
                    entity_name=event.entity_name,
                    user_email=event.user_email,
                    trigger_context=trigger_context,
                    blocking=blocking,
                )
                executions.append(execution)
                
                logger.info(
                    f"Workflow '{workflow.name}' executed with status: {execution.status.value}"
                )
            except Exception as e:
                logger.exception(f"Failed to execute workflow '{workflow.name}': {e}")
                if errors is not None:
                    errors.append((workflow, e))
        
        return executions

    def on_create(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire an on_create trigger.
        
        Args:
            entity_type: Type of entity being created
            entity_id: ID of the entity
            entity_name: Name of the entity
            entity_data: Entity data dictionary
            user_email: User performing the action
            scope_type: Scope type (project, catalog, domain)
            scope_id: Scope ID
            blocking: Execute synchronously
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_CREATE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        return self.fire_trigger(event, blocking=blocking)

    def on_update(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire an on_update trigger."""
        event = TriggerEvent(
            trigger_type=TriggerType.ON_UPDATE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        return self.fire_trigger(event, blocking=blocking)

    def on_delete(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire an on_delete trigger."""
        event = TriggerEvent(
            trigger_type=TriggerType.ON_DELETE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        return self.fire_trigger(event, blocking=blocking)

    def on_status_change(
        self,
        entity_type: EntityType,
        entity_id: str,
        from_status: str,
        to_status: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire an on_status_change trigger."""
        event = TriggerEvent(
            trigger_type=TriggerType.ON_STATUS_CHANGE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            from_status=from_status,
            to_status=to_status,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        return self.fire_trigger(event, blocking=blocking)

    def before_create(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
    ) -> Tuple[bool, List[WorkflowExecution]]:
        """Fire a before_create trigger for pre-creation validation.
        
        This is an inline/blocking trigger that runs BEFORE the entity is created.
        Use for compliance checks, naming convention validation, etc.
        
        Args:
            entity_type: Type of entity being created
            entity_id: Proposed ID of the entity (may not exist yet)
            entity_name: Name of the entity
            entity_data: Entity data dictionary to validate
            user_email: User performing the action
            scope_type: Scope type (project, catalog, domain)
            scope_id: Scope ID
            
        Returns:
            Tuple of (all_passed, executions) where:
            - all_passed: True if all workflows succeeded, False if any failed
            - executions: List of workflow executions with detailed results
        """
        event = TriggerEvent(
            trigger_type=TriggerType.BEFORE_CREATE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        logger.info(f"before_create called for {entity_type.value} '{entity_name}' with data: {entity_data}")
        executions = self.fire_trigger(event, blocking=True)
        logger.info(f"before_create returned {len(executions)} executions")
        
        # Check if all workflows passed
        all_passed = all(
            exe.status == ExecutionStatus.SUCCEEDED 
            for exe in executions
        ) if executions else True  # No workflows = pass
        
        return all_passed, executions

    def before_update(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
    ) -> Tuple[bool, List[WorkflowExecution]]:
        """Fire a before_update trigger for pre-update validation.
        
        This is an inline/blocking trigger that runs BEFORE the entity is updated.
        
        Args:
            entity_type: Type of entity being updated
            entity_id: ID of the entity
            entity_name: Name of the entity
            entity_data: Updated entity data dictionary to validate
            user_email: User performing the action
            scope_type: Scope type (project, catalog, domain)
            scope_id: Scope ID
            
        Returns:
            Tuple of (all_passed, executions)
        """
        event = TriggerEvent(
            trigger_type=TriggerType.BEFORE_UPDATE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        executions = self.fire_trigger(event, blocking=True)
        
        all_passed = all(
            exe.status == ExecutionStatus.SUCCEEDED 
            for exe in executions
        ) if executions else True
        
        return all_passed, executions

    def before_status_change(
        self,
        entity_type: EntityType,
        entity_id: str,
        from_status: str,
        to_status: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
    ) -> Tuple[bool, List[WorkflowExecution]]:
        """Fire a before_status_change trigger for pre-transition validation.

        Runs BEFORE the status is persisted. Returns (all_passed, executions)
        so the caller can block the transition when any workflow fails.

        A gate whose evaluation raises has no execution. The error is logged
        and audited; when the gate is ``on_error: fail_closed`` this raises
        ``GateEvaluationError`` (the transition is refused), otherwise the
        gate is left out, as before.
        """
        event = TriggerEvent(
            trigger_type=TriggerType.BEFORE_STATUS_CHANGE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            from_status=from_status,
            to_status=to_status,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        logger.info(
            f"before_status_change called for {entity_type.value} '{entity_name}': "
            f"'{from_status}' -> '{to_status}'"
        )
        gate_errors: List[Tuple[ProcessWorkflow, Exception]] = []
        executions = self.fire_trigger(event, blocking=True, errors=gate_errors)
        for workflow, error in gate_errors:
            report_gate_error(event, error, workflow=workflow)
        closed = [wf.name for wf, _ in gate_errors if wf.trigger.on_error == GateErrorMode.FAIL_CLOSED]
        if closed:
            raise GateEvaluationError(from_status, to_status, closed)

        all_passed = all(
            exe.status == ExecutionStatus.SUCCEEDED
            for exe in executions
        ) if executions else True

        return all_passed, executions

    # =========================================================================
    # Publication Triggers
    # =========================================================================

    def on_publish(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire when an entity is published to the marketplace."""
        event = TriggerEvent(
            trigger_type=TriggerType.ON_PUBLISH,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_publish fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    def on_unpublish(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire when an entity is unpublished from the marketplace."""
        event = TriggerEvent(
            trigger_type=TriggerType.ON_UNPUBLISH,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_unpublish fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    # =========================================================================
    # Certification Triggers
    # =========================================================================

    def on_request_certify(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire when certification is requested for an entity."""
        event = TriggerEvent(
            trigger_type=TriggerType.ON_REQUEST_CERTIFY,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        logger.info(f"on_request_certify fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    def on_certify(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire after certification is granted."""
        event = TriggerEvent(
            trigger_type=TriggerType.ON_CERTIFY,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_certify fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    def on_decertify(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire after certification is removed."""
        event = TriggerEvent(
            trigger_type=TriggerType.ON_DECERTIFY,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_decertify fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    # =========================================================================
    # Maturity Triggers
    # =========================================================================

    def on_maturity_change(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        from_level: Optional[int] = None,
        to_level: Optional[int] = None,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire when an entity's maturity level changes."""
        data = dict(entity_data or {})
        data["from_maturity_level"] = from_level
        data["to_maturity_level"] = to_level
        event = TriggerEvent(
            trigger_type=TriggerType.ON_MATURITY_CHANGE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=data,
            user_email=user_email,
        )
        logger.info(
            f"on_maturity_change fired for {entity_type.value} '{entity_id}': "
            f"level {from_level} -> {to_level}"
        )
        return self.fire_trigger(event, blocking=blocking)

    # =========================================================================
    # Request Triggers
    # =========================================================================

    def on_request_review(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire when a review is requested for an entity.
        
        Used for datasets, data contracts, and data products when a user
        requests a steward or domain owner review.
        
        Args:
            entity_type: Type of entity (dataset, data_contract, data_product)
            entity_id: ID of the entity
            entity_name: Name of the entity
            entity_data: Entity data including request details (message, requester, etc.)
            user_email: User requesting the review
            scope_type: Scope type (project, catalog, domain)
            scope_id: Scope ID
            blocking: Execute synchronously
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_REQUEST_REVIEW,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        logger.info(f"on_request_review fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    def on_request_access(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire when access is requested for an entity.
        
        Used for access grants, project access, and role access requests.
        
        Args:
            entity_type: Type of entity (access_grant, project, etc.)
            entity_id: ID of the entity or request
            entity_name: Name of the entity
            entity_data: Request details (duration, permission level, justification, etc.)
            user_email: User requesting access
            scope_type: Scope type
            scope_id: Scope ID
            blocking: Execute synchronously
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_REQUEST_ACCESS,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        logger.info(f"on_request_access fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    def on_request_publish(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire when publication to the marketplace is requested.
        
        Used for data contracts and data products. Deployment requests fire
        ``on_request_deploy`` instead.
        
        Args:
            entity_type: Type of entity (data_contract, data_product)
            entity_id: ID of the entity
            entity_name: Name of the entity
            entity_data: Request details (target catalog, justification, etc.)
            user_email: User requesting publish
            scope_type: Scope type
            scope_id: Scope ID
            blocking: Execute synchronously
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_REQUEST_PUBLISH,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        logger.info(f"on_request_publish fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    def on_request_deploy(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire when deployment of a data contract to Unity Catalog is requested.
        
        Args:
            entity_type: Type of entity (data_contract)
            entity_id: ID of the entity
            entity_name: Name of the entity
            entity_data: Request details (target catalog and schema, message)
            user_email: User requesting the deployment
            scope_type: Scope type
            scope_id: Scope ID
            blocking: Execute synchronously
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_REQUEST_DEPLOY,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        logger.info(
            "on_request_deploy fired for %s '%s' by %s", entity_type.value, entity_id, user_email
        )
        return self.fire_trigger(event, blocking=blocking)

    def on_request_status_change(
        self,
        entity_type: EntityType,
        entity_id: str,
        from_status: str,
        to_status: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        *,
        blocking: bool = True,
    ) -> List[WorkflowExecution]:
        """Fire when a status change is requested (but not yet applied).
        
        Used when users request a status transition that requires approval,
        e.g., draft -> published for datasets or contracts.
        
        Args:
            entity_type: Type of entity
            entity_id: ID of the entity
            from_status: Current status
            to_status: Requested target status
            entity_name: Name of the entity
            entity_data: Request details (justification, etc.)
            user_email: User requesting the change
            scope_type: Scope type
            scope_id: Scope ID
            blocking: Execute synchronously
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_REQUEST_STATUS_CHANGE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
            from_status=from_status,
            to_status=to_status,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        logger.info(
            f"on_request_status_change fired for {entity_type.value} '{entity_id}': "
            f"'{from_status}' -> '{to_status}' by {user_email}"
        )
        return self.fire_trigger(event, blocking=blocking)

    # =========================================================================
    # Job Lifecycle Triggers
    # =========================================================================

    def on_job_success(
        self,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire when a background job completes successfully.
        
        Args:
            entity_id: Job run ID
            entity_name: Job name
            entity_data: Job details (workflow_id, run_id, duration, etc.)
            user_email: User who triggered the job
            blocking: Execute synchronously (default False for async)
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_JOB_SUCCESS,
            entity_type=EntityType.JOB,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_job_success fired for job '{entity_name}' (run {entity_id})")
        return self.fire_trigger(event, blocking=blocking)

    def on_job_failure(
        self,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire when a background job fails.
        
        Args:
            entity_id: Job run ID
            entity_name: Job name
            entity_data: Job details (workflow_id, run_id, error_message, etc.)
            user_email: User who triggered the job
            blocking: Execute synchronously (default False for async)
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_JOB_FAILURE,
            entity_type=EntityType.JOB,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_job_failure fired for job '{entity_name}' (run {entity_id})")
        return self.fire_trigger(event, blocking=blocking)

    # =========================================================================
    # Subscription Triggers
    # =========================================================================

    def on_subscribe(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire when a user subscribes to an entity.
        
        Args:
            entity_type: Type of entity (dataset, data_product)
            entity_id: ID of the entity
            entity_name: Name of the entity
            entity_data: Subscription details (subscriber_email, reason, etc.)
            user_email: User subscribing
            blocking: Execute synchronously (default False for async)
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_SUBSCRIBE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_subscribe fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    def on_unsubscribe(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire when a user unsubscribes from an entity.
        
        Args:
            entity_type: Type of entity (dataset, data_product)
            entity_id: ID of the entity
            entity_name: Name of the entity
            entity_data: Subscription details
            user_email: User unsubscribing
            blocking: Execute synchronously (default False for async)
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_UNSUBSCRIBE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_unsubscribe fired for {entity_type.value} '{entity_id}' by {user_email}")
        return self.fire_trigger(event, blocking=blocking)

    # =========================================================================
    # Access Lifecycle Triggers
    # =========================================================================

    def on_expiring(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire when an entity is about to expire.
        
        Used for access grants, subscriptions, or any time-limited resources.
        Typically triggered by a scheduled job checking for expiring items.
        
        Args:
            entity_type: Type of entity (access_grant, etc.)
            entity_id: ID of the entity
            entity_name: Name or description of the entity
            entity_data: Details including expiration date, days remaining, etc.
            user_email: User affected by expiration
            blocking: Execute synchronously (default False for async)
            
        Returns:
            List of workflow executions
        """
        event = TriggerEvent(
            trigger_type=TriggerType.ON_EXPIRING,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_expiring fired for {entity_type.value} '{entity_id}'")
        return self.fire_trigger(event, blocking=blocking)

    def on_revoke(
        self,
        entity_type: EntityType,
        entity_id: str,
        entity_name: Optional[str] = None,
        entity_data: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        revoked_by: Optional[str] = None,
        *,
        blocking: bool = False,
    ) -> List[WorkflowExecution]:
        """Fire when access or a resource is revoked.
        
        Used for access grants when an admin revokes access before expiration.
        
        Args:
            entity_type: Type of entity (access_grant, etc.)
            entity_id: ID of the entity
            entity_name: Name or description of the entity
            entity_data: Details about the revocation (reason, etc.)
            user_email: User whose access was revoked
            revoked_by: User who performed the revocation
            blocking: Execute synchronously (default False for async)
            
        Returns:
            List of workflow executions
        """
        if entity_data is None:
            entity_data = {}
        entity_data['revoked_by'] = revoked_by
        
        event = TriggerEvent(
            trigger_type=TriggerType.ON_REVOKE,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
        logger.info(f"on_revoke fired for {entity_type.value} '{entity_id}' (revoked by {revoked_by})")
        return self.fire_trigger(event, blocking=blocking)


def failed_execution_reasons(executions: List[WorkflowExecution]) -> List[Dict[str, Any]]:
    """Why blocking (``before_*``) workflows did not let an operation through.

    One reason per execution that did not succeed: "waiting for approval"
    for paused executions; otherwise the execution's error (the message of
    the step it ended on, e.g. a Fail step), else the error or message of
    its last failed step, else its status.

    Each reason is ``{workflow, text}``; a reason the app writes also has a
    ``code`` and ``params`` (``waiting_for_approval``, the step's own error
    code such as ``rule_entity_load_failed``, ``execution_status``), so the UI
    can show it in the user's language. A text an admin wrote has no code.
    """
    reasons: List[Dict[str, Any]] = []
    for exe in executions:
        if exe.status == ExecutionStatus.SUCCEEDED:
            continue
        code: Optional[str] = None
        params: Dict[str, Any] = {}
        if exe.status == ExecutionStatus.PAUSED:
            step = exe.current_step_name or exe.current_step_id
            reason = f"waiting for approval ({step})"
            code, params = 'waiting_for_approval', {'step': step}
        else:
            reason = exe.error_message
            for step in reversed(exe.step_executions or []):
                if reason:
                    break
                if step.passed is False or step.error_message:
                    data = step.result_data if isinstance(step.result_data, dict) else {}
                    reason = step.error_message or data.get('message')
            if reason:
                code, params = _step_error_code(exe, reason)
            else:
                reason = exe.status.value
                code, params = 'execution_status', {'status': reason}
        entry: Dict[str, Any] = {'workflow': exe.workflow_name, 'text': str(reason)}
        if code:
            entry['code'] = code
            entry['params'] = params
        reasons.append(entry)
    return reasons


def _step_error_code(exe: WorkflowExecution, reason: Any) -> Tuple[Optional[str], Dict[str, Any]]:
    """The code and parameters a step stored with the error shown as ``reason``."""
    for step in reversed(exe.step_executions or []):
        data = step.result_data if isinstance(step.result_data, dict) else {}
        if step.error_message == reason and isinstance(data.get('error_code'), str):
            params = data.get('error_params')
            return data['error_code'], params if isinstance(params, dict) else {}
    return None, {}


def describe_failed_executions(executions: List[WorkflowExecution]) -> str:
    """The reasons of ``failed_execution_reasons`` as one English text."""
    reasons = [
        f"{r['workflow']}: {r['text']}" if r['workflow'] else r['text']
        for r in failed_execution_reasons(executions)
    ]
    return "; ".join(reasons) or "Workflow validation failed"


class StatusChangeBlockedError(CodedValueError):
    """A ``before_status_change`` workflow blocked a status change.

    ``str()`` is the English message; ``code`` and ``params`` (the statuses
    and the reasons of ``failed_execution_reasons``) let the UI show it in the
    user's language.
    """

    code = 'status_change_blocked'

    def __init__(self, from_status: str, to_status: str, executions: List[WorkflowExecution]):
        super().__init__(
            f"Status transition from '{from_status}' to '{to_status}' "
            f"blocked by workflow: {describe_failed_executions(executions)}",
            fromStatus=from_status,
            toStatus=to_status,
            reasons=failed_execution_reasons(executions),
        )

    @classmethod
    def without_reasons(cls, from_status: str, to_status: str) -> 'StatusChangeBlockedError':
        """The error with a generic reason, built without reading the executions."""
        blocked = cls.__new__(cls)
        CodedValueError.__init__(
            blocked,
            f"Status transition from '{from_status}' to '{to_status}' "
            f"blocked by workflow: Workflow validation failed",
            fromStatus=from_status,
            toStatus=to_status,
            reasons=[],
        )
        return blocked


class GateEvaluationError(CodedValueError):
    """A ``before_status_change`` gate marked ``fail_closed`` could not be evaluated.

    ``str()`` is the English message; ``params`` has the statuses and the
    names of the gates that raised (empty when the gates couldn't be listed).
    """

    code = 'status_change_gate_error'

    def __init__(self, from_status: str, to_status: str, workflows: List[str]):
        names = f" ({', '.join(workflows)})" if workflows else ""
        super().__init__(
            f"Status transition from '{from_status}' to '{to_status}' refused: "
            f"a workflow gate{names} could not be evaluated. Ask an administrator "
            f"to check the workflow.",
            fromStatus=from_status,
            toStatus=to_status,
            workflows=workflows,
        )


def report_gate_error(
    event: TriggerEvent,
    error: Exception,
    *,
    workflow: Optional[ProcessWorkflow] = None,
) -> None:
    """Log a gate that raised at error level and write it to the audit log.

    ``workflow`` is None when the error came before the gates were known
    (listing them failed).
    """
    mode = workflow.trigger.on_error.value if workflow else None
    name = workflow.name if workflow else None
    logger.error(
        f"before_status_change gate error for {event.entity_type.value} '{event.entity_id}' "
        f"({event.from_status} -> {event.to_status}), workflow {name or '(not listed)'}, "
        f"on_error={mode or 'unknown'}: {error}",
        exc_info=error,
    )
    try:
        from src.controller.audit_manager import log_system_action
        log_system_action(
            username=event.user_email or 'system',
            feature='process-workflows',
            action='GATE_ERROR',
            success=False,
            details={
                'entity_type': event.entity_type.value,
                'entity_id': event.entity_id,
                'from_status': event.from_status,
                'to_status': event.to_status,
                'workflow_id': workflow.id if workflow else None,
                'workflow_name': name,
                'on_error': mode,
                'error': f"{type(error).__name__}: {error}",
            },
        )
    except Exception as audit_err:  # The audit must never hide the gate decision
        logger.error(f"Could not audit the gate error: {audit_err}", exc_info=True)


def _fail_closed_gate_names(db: Session, event: TriggerEvent) -> List[str]:
    """Active ``fail_closed`` gates that would match ``event``, read directly from the table.

    Used when listing the gates through the workflows manager raised, so the
    mode of each gate is still honoured. Kept independent of that code path.
    """
    import json
    from src.db_models.process_workflows import ProcessWorkflowDb

    names: List[str] = []
    rows = db.query(ProcessWorkflowDb).filter(ProcessWorkflowDb.is_active == True).all()  # noqa: E712
    for row in rows:
        try:
            trigger = json.loads(row.trigger_config) if row.trigger_config else {}
        except (TypeError, ValueError):
            continue
        if trigger.get('type') != TriggerType.BEFORE_STATUS_CHANGE.value:
            continue
        if trigger.get('on_error') != GateErrorMode.FAIL_CLOSED.value:
            continue
        entity_types = trigger.get('entity_types') or []
        if entity_types and event.entity_type.value not in entity_types:
            continue
        if trigger.get('from_status') and trigger['from_status'] != event.from_status:
            continue
        if trigger.get('to_status') and trigger['to_status'] != event.to_status:
            continue
        names.append(row.name)
    return names


def run_status_gates(
    db: Session,
    *,
    entity_type: EntityType,
    entity_id: str,
    from_status: str,
    to_status: str,
    entity_name: Optional[str] = None,
    entity_data: Optional[Dict[str, Any]] = None,
    user_email: Optional[str] = None,
) -> None:
    """Run the ``before_status_change`` gates of a transition; raise when it must not proceed.

    - A gate that fails (or waits for approval) blocks: ``StatusChangeBlockedError``.
    - A gate that raises while it is evaluated is logged and audited; a
      ``fail_closed`` gate refuses the transition (``GateEvaluationError``),
      a ``fail_open`` one (the default) lets it proceed.
    - When the gates can't even be listed, the ``fail_closed`` ones are read
      straight from the workflows table; if that fails too, the mode is
      unknown and the transition proceeds (the error is still reported).
    """
    event = TriggerEvent(
        trigger_type=TriggerType.BEFORE_STATUS_CHANGE,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        entity_data=entity_data,
        user_email=user_email,
        from_status=from_status,
        to_status=to_status,
    )
    try:
        gate_passed, gate_executions = get_trigger_registry(db).before_status_change(
            entity_type=entity_type,
            entity_id=entity_id,
            from_status=from_status,
            to_status=to_status,
            entity_name=entity_name,
            entity_data=entity_data,
            user_email=user_email,
        )
    except ValueError:
        raise
    except Exception as e:
        report_gate_error(event, e)
        try:
            closed = _fail_closed_gate_names(db, event)
        except Exception as lookup_err:
            logger.error(
                f"Could not read the gates' on_error mode after the gate error, "
                f"so the transition proceeds: {lookup_err}",
                exc_info=True,
            )
            return
        if closed:
            raise GateEvaluationError(from_status, to_status, closed) from e
        return
    if not gate_passed:
        try:
            blocked = StatusChangeBlockedError(from_status, to_status, gate_executions)
        except Exception as e:
            # A blocking gate stays blocking even if its message can't be built (#1012)
            logger.error(f"Could not describe the blocking gates: {e}", exc_info=True)
            blocked = StatusChangeBlockedError.without_reasons(from_status, to_status)
        raise blocked


def get_trigger_registry(db: Session) -> TriggerRegistry:
    """Get a trigger registry instance.

    Args:
        db: Database session

    Returns:
        TriggerRegistry instance
    """
    return TriggerRegistry(db)


def fire_trigger_safe(
    db: Session,
    method: str,
    *,
    entity_type: EntityType,
    entity_id: str,
    entity_name: Optional[str] = None,
    entity_data: Optional[Dict[str, Any]] = None,
    user_email: Optional[str] = None,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    blocking: bool = False,
) -> None:
    """Fire a trigger method on the registry, logging and swallowing errors.

    Convenience wrapper that eliminates repetitive try/except blocks in routes.

    Args:
        db: Database session
        method: Name of the TriggerRegistry method (e.g. "on_create", "on_delete")
        entity_type: Entity type enum value
        entity_id: ID of the entity
        entity_name: Display name of the entity
        entity_data: Additional entity data dict
        user_email: User performing the action
        from_status: Previous status (for status-change triggers)
        to_status: New status (for status-change triggers)
        blocking: Whether to execute synchronously
    """
    try:
        registry = get_trigger_registry(db)
        kwargs: Dict[str, Any] = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity_name,
            "entity_data": entity_data,
            "user_email": user_email,
        }
        if from_status is not None:
            kwargs["from_status"] = from_status
        if to_status is not None:
            kwargs["to_status"] = to_status
        if method not in ("before_create", "before_update", "before_status_change"):
            kwargs["blocking"] = blocking
        getattr(registry, method)(**kwargs)
    except Exception as e:
        logger.warning(f"{method} trigger failed for {entity_type.value} {entity_id}: {e}")


# ============================================================================
# Entity Data Enrichment Helpers
# ============================================================================
#
# Workflow authors reference entity attributes in webhook ``body_template``s
# via ``${entity.<field>}`` substitution. For data-product triggers we
# pre-resolve the underlying DP's output ports into structured records so
# templates can reach catalog/schema/table without each author re-parsing
# ODPS ``asset_identifier`` strings.


def enrich_entity_data_with_data_product(
    entity_data: Dict[str, Any],
    data_product,
) -> Dict[str, Any]:
    """Enrich ``entity_data`` with output-port-derived fields from a DP.

    Adds two keys (only when not already supplied — caller wins):

    - ``output_ports``: list of ``{name, catalog, schema, table, fqn}``
      dicts, one per OutputPortDb whose ``asset_identifier`` parses as
      a 3-part Unity Catalog reference (``catalog.schema.table``). Ports
      without a usable identifier are skipped so the workflow author
      never sees half-formed rows.
    - ``catalogs``: sorted, deduplicated list of catalog names across
      every usable output port. Empty list when the DP has no parseable
      ports.

    The function is intentionally tolerant: a missing ``output_ports``
    relationship, ``None`` identifiers, or non-3-part identifiers are
    all logged at debug-level and silently skipped. Failures here must
    never break the trigger-fire path that wraps this call.

    Args:
        entity_data: The entity_data dict to mutate. Returned for
            convenience; mutation is in place. Caller-supplied keys
            (``output_ports``/``catalogs``) are preserved.
        data_product: Resolved ``DataProductDb`` row. May be ``None``,
            in which case the function is a no-op.

    Returns:
        The (possibly mutated) ``entity_data`` dict.
    """
    if data_product is None:
        return entity_data

    ports = getattr(data_product, 'output_ports', None) or []
    parsed_ports: List[Dict[str, Any]] = []
    catalogs_seen: set = set()

    for port in ports:
        identifier = getattr(port, 'asset_identifier', None)
        if not identifier or not isinstance(identifier, str):
            continue
        parts = identifier.split('.')
        # Require exactly catalog.schema.table — anything else is a
        # non-UC asset (file path, job id, etc.) and isn't useful to a
        # webhook template that wants per-catalog grants.
        if len(parts) != 3:
            logger.debug(
                "Skipping output port '%s' on DP %s: asset_identifier "
                "'%s' did not parse as catalog.schema.table",
                getattr(port, 'name', '?'),
                getattr(data_product, 'id', '?'),
                identifier,
            )
            continue
        catalog, schema, table = parts
        parsed_ports.append({
            'name': getattr(port, 'name', None) or '',
            'catalog': catalog,
            'schema': schema,
            'table': table,
            'fqn': identifier,
        })
        catalogs_seen.add(catalog)

    if 'output_ports' not in entity_data:
        entity_data['output_ports'] = parsed_ports
    if 'catalogs' not in entity_data:
        entity_data['catalogs'] = sorted(catalogs_seen)

    return entity_data

