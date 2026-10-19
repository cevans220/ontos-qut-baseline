"""Lifecycle status checks shared by the create and update paths of products and contracts.

A status change runs through ``transition_status`` (the ``change-status`` and
``request-status-change`` routes), which checks the transition and runs the
``before_status_change`` gates. An update (``PUT``) may only repeat the stored
status, and a new entity starts in one of the lifecycle statuses.
``certified`` is not a status: certification is ``certification_level``, set
by the certify action.
"""
from typing import Iterable, Optional

from src.common.api_errors import CodedValueError
from src.models.lifecycle import DATA_CONTRACT_TRANSITIONS, DATA_PRODUCT_TRANSITIONS

PRODUCT_STATUSES = tuple(DATA_PRODUCT_TRANSITIONS)
CONTRACT_STATUSES = tuple(DATA_CONTRACT_TRANSITIONS)


class StatusChangeRequiresTransitionError(CodedValueError):
    """An update tried to change the status, which only a transition may do."""

    code = 'status_change_requires_transition'

    def __init__(self, from_status: str, to_status: str):
        super().__init__(
            f"An update can't change the status from '{from_status}' to '{to_status}'. "
            "Use the change-status or request-status-change action, which checks the "
            "transition and runs the status change workflows.",
            fromStatus=from_status,
            toStatus=to_status,
        )


class InvalidStatusError(CodedValueError):
    """A status that isn't one of the entity's lifecycle statuses."""

    code = 'invalid_status'

    def __init__(self, status: str, allowed: Iterable[str]):
        allowed = list(allowed)
        super().__init__(
            f"'{status}' is not a lifecycle status. Use one of: {', '.join(allowed)}.",
            status=status,
            allowed=allowed,
        )


class CertifiedIsNotAStatusError(CodedValueError):
    """``certified`` sent as a status; certification is a separate field."""

    code = 'certified_is_not_a_status'

    def __init__(self):
        super().__init__(
            "'certified' is not a status: certification is set with the certify action, "
            "on an active entity."
        )


def _normalized(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    value = getattr(status, 'value', status)
    return str(value).strip().lower() or None


def check_status_unchanged(stored: Optional[str], requested: Optional[str]) -> None:
    """Refuse an update whose status differs from the stored one.

    A missing status, or the stored one (in any case), is accepted.
    """
    requested_norm = _normalized(requested)
    if requested_norm is None:
        return
    stored_norm = _normalized(stored) or 'draft'
    if requested_norm != stored_norm:
        raise StatusChangeRequiresTransitionError(stored_norm, requested_norm)


def normalize_new_status(status: Optional[str], allowed: Iterable[str], default: str = 'draft') -> str:
    """The lowercase status of a new entity, or an error for one that isn't a lifecycle status."""
    allowed = tuple(allowed)
    normalized = _normalized(status)
    if normalized is None:
        return default
    if normalized == 'certified':
        raise CertifiedIsNotAStatusError()
    if normalized not in allowed:
        raise InvalidStatusError(normalized, allowed)
    return normalized
