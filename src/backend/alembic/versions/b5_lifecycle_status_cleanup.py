"""move product and contract statuses that aren't lifecycle statuses to the lifecycle ones

a9 and b2_add_cert_pub_to_contracts turned 'certified' into status 'active'
plus the top certification level. Create, import and the contract dialogs kept
accepting any status after that ('certified', the dialogs' 'archived', or a
capitalized 'Proposed'), and an update could change it. Now a new entity starts
in a lifecycle status and an update can't change it, so a product left in one
of those would be stuck (a product's transitions start from a lifecycle
status). This converts them:

- 'certified' -> 'active' plus the top certification level, as a9/b2 did;
- 'archived' -> 'retired';
- a lifecycle status in another case -> lowercase.

Any other value is left as it is.

Revision ID: b5_lifecycle_status_cleanup
Revises: b4_compliance_run_error_code
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b5_lifecycle_status_cleanup'
down_revision: Union[str, None] = 'b4_compliance_run_error_code'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LIFECYCLE_STATUSES = (
    'draft', 'sandbox', 'proposed', 'under_review', 'approved', 'active', 'deprecated', 'retired',
)


def upgrade() -> None:
    statuses = ", ".join(f"'{s}'" for s in LIFECYCLE_STATUSES)
    for table in ('data_products', 'data_contracts'):
        op.execute(f"""
            UPDATE {table}
            SET status = 'active',
                certification_level = COALESCE(
                    certification_level, (SELECT MAX(level_order) FROM certification_levels)
                ),
                certified_at = COALESCE(certified_at, updated_at)
            WHERE lower(status) = 'certified'
        """)
        op.execute(f"UPDATE {table} SET status = 'retired' WHERE lower(status) = 'archived'")
        op.execute(f"""
            UPDATE {table} SET status = lower(status)
            WHERE status <> lower(status) AND lower(status) IN ({statuses})
        """)


def downgrade() -> None:
    # Data only: the converted rows keep their new status.
    pass
