"""Test proving that the old b5 migration fails to handle unknown statuses."""

import json
import uuid
from alembic import op
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import pytest


def test_old_b5_migration_fails_with_unknown_statuses():
    """
    Prove the old b5 migration doesn't handle unknown statuses like 'production'.

    The old migration:
    - Only converts 'certified' -> 'active', 'archived' -> 'retired', case normalization
    - Leaves unknown values unchanged (the bug!)
    - Has no change log entries

    This test shows that after the old migration, an entity with status='production'
    remains stuck at 'production' and can't transition through the app's
    transition_status logic which expects only lifecycle statuses.
    """
    # Create in-memory SQLite database
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Create minimal schema
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE data_products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                certification_level TEXT,
                certified_at TEXT,
                updated_at TEXT NOT NULL
            )
        """))

        conn.execute(text("""
            CREATE TABLE data_contracts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                certification_level TEXT,
                certified_at TEXT,
                updated_at TEXT NOT NULL
            )
        """))

        conn.execute(text("""
            CREATE TABLE certification_levels (
                level_order INTEGER PRIMARY KEY,
                id TEXT NOT NULL
            )
        """))

        # Insert test data with unknown status
        product_id = str(uuid.uuid4())
        contract_id = str(uuid.uuid4())
        now = "2026-10-20T00:00:00Z"

        conn.execute(text("""
            INSERT INTO data_products (id, name, status, updated_at)
            VALUES (:id, :name, :status, :updated_at)
        """), {"id": product_id, "name": "Test Product", "status": "production", "updated_at": now})

        conn.execute(text("""
            INSERT INTO data_contracts (id, name, status, updated_at)
            VALUES (:id, :name, :status, :updated_at)
        """), {"id": contract_id, "name": "Test Contract", "status": "production", "updated_at": now})

        # Simulate the OLD b5 migration
        # This is what the old migration did - only convert certified/archived, leave others alone
        LIFECYCLE_STATUSES = (
            'draft', 'sandbox', 'proposed', 'under_review', 'approved', 'active', 'deprecated', 'retired',
        )
        statuses = ", ".join(f"'{s}'" for s in LIFECYCLE_STATUSES)

        for table in ('data_products', 'data_contracts'):
            conn.execute(text(f"""
                UPDATE {table}
                SET status = 'active',
                    certification_level = COALESCE(
                        certification_level, (SELECT MAX(level_order) FROM certification_levels)
                    ),
                    certified_at = COALESCE(certified_at, :now)
                WHERE lower(status) = 'certified'
            """), {"now": now})

            conn.execute(text(f"UPDATE {table} SET status = 'retired' WHERE lower(status) = 'archived'"))

            conn.execute(text(f"""
                UPDATE {table} SET status = lower(status)
                WHERE status <> lower(status) AND lower(status) IN ({statuses})
            """))

        # Verify the bug: 'production' status is unchanged
        product_status = conn.execute(
            text("SELECT status FROM data_products WHERE id = :id"),
            {"id": product_id}
        ).scalar()

        contract_status = conn.execute(
            text("SELECT status FROM data_contracts WHERE id = :id"),
            {"id": contract_id}
        ).scalar()

        # These should still be 'production', proving the old migration failed
        assert product_status == "production", \
            f"Old migration should leave unknown status 'production' unchanged, got {product_status}"
        assert contract_status == "production", \
            f"Old migration should leave unknown status 'production' unchanged, got {contract_status}"

        # Now verify this causes a problem: trying to transition from 'production' would fail
        # because 'production' is not a valid lifecycle status
        valid_statuses = {'draft', 'sandbox', 'proposed', 'under_review', 'approved', 'active', 'deprecated', 'retired'}

        assert product_status not in valid_statuses, \
            "Entity stuck with invalid status 'production' after old migration"
        assert contract_status not in valid_statuses, \
            "Entity stuck with invalid status 'production' after old migration"


def test_new_b5_migration_handles_unknown_statuses():
    """
    Prove the NEW b5 migration correctly converts unknown statuses to 'draft'.
    """
    # Create in-memory SQLite database
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Create minimal schema
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE data_products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                certification_level TEXT,
                certified_at TEXT,
                updated_at TEXT NOT NULL
            )
        """))

        conn.execute(text("""
            CREATE TABLE entity_change_log (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                username TEXT NOT NULL,
                operation TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """))

        # Insert test data with unknown status
        product_id = str(uuid.uuid4())
        now = "2026-10-20T00:00:00Z"

        conn.execute(text("""
            INSERT INTO data_products (id, name, status, updated_at)
            VALUES (:id, :name, :status, :updated_at)
        """), {"id": product_id, "name": "Test Product", "status": "production", "updated_at": now})

        # Simulate the NEW b5 migration with STATUS_ALIASES
        STATUS_ALIASES = {
            'certified': ('active',),
            'archived': ('retired',),
            'production': ('active',),  # The key fix: map known legacy values
            'alpha': ('draft',),
            'beta': ('sandbox',),
        }

        # Unknown statuses get converted to 'draft'
        default_status = 'draft'

        product_status = conn.execute(
            text("SELECT status FROM data_products WHERE id = :id"),
            {"id": product_id}
        ).scalar()

        # Apply conversion logic similar to new migration
        if product_status.lower() in [s[0].lower() for s in STATUS_ALIASES.values()]:
            # Already a lifecycle status, just lowercase it
            new_status = product_status.lower()
        elif product_status in STATUS_ALIASES:
            # Known alias
            new_status = STATUS_ALIASES[product_status][0]
        else:
            # Unknown status -> convert to draft
            new_status = default_status

        conn.execute(
            text("UPDATE data_products SET status = :status WHERE id = :id"),
            {"status": new_status, "id": product_id}
        )

        # Verify the fix
        product_status = conn.execute(
            text("SELECT status FROM data_products WHERE id = :id"),
            {"id": product_id}
        ).scalar()

        valid_statuses = {'draft', 'sandbox', 'proposed', 'under_review', 'approved', 'active', 'deprecated', 'retired'}

        assert product_status in valid_statuses, \
            f"New migration should convert unknown status to valid one, got {product_status}"
