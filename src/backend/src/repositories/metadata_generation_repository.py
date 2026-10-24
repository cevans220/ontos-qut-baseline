"""
Repository for Generation Runs and Description Suggestions.
"""

from typing import Dict, List, Optional

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from src.common.logging import get_logger
from src.db_models.metadata_generation import DescriptionSuggestionDb, MetadataGenerationRunDb

logger = get_logger(__name__)

ACTIVE_RUN_STATUSES = ('pending', 'running', 'ingesting')


class MetadataGenerationRunsRepository:

    def get(self, db: Session, run_id: str) -> Optional[MetadataGenerationRunDb]:
        return db.query(MetadataGenerationRunDb).filter(MetadataGenerationRunDb.id == run_id).first()

    def get_by_job_run_id(self, db: Session, job_run_id: str) -> Optional[MetadataGenerationRunDb]:
        return db.query(MetadataGenerationRunDb).filter(
            MetadataGenerationRunDb.job_run_id == job_run_id,
        ).first()

    def list_for_user(
        self, db: Session, user_id: str, *, limit: int = 50, skip: int = 0
    ) -> List[MetadataGenerationRunDb]:
        return (
            db.query(MetadataGenerationRunDb)
            .filter(MetadataGenerationRunDb.user_id == user_id)
            .order_by(desc(MetadataGenerationRunDb.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def list_all(self, db: Session, *, limit: int = 50, skip: int = 0) -> List[MetadataGenerationRunDb]:
        return (
            db.query(MetadataGenerationRunDb)
            .order_by(desc(MetadataGenerationRunDb.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_active_for_user(self, db: Session, user_id: str) -> int:
        return (
            db.query(MetadataGenerationRunDb)
            .filter(
                MetadataGenerationRunDb.user_id == user_id,
                MetadataGenerationRunDb.status.in_(ACTIVE_RUN_STATUSES),
            )
            .count()
        )

    def list_active(self, db: Session) -> List[MetadataGenerationRunDb]:
        return (
            db.query(MetadataGenerationRunDb)
            .filter(MetadataGenerationRunDb.status.in_(ACTIVE_RUN_STATUSES))
            .all()
        )

    def create(self, db: Session, *, user_id: str, **kwargs) -> MetadataGenerationRunDb:
        run = MetadataGenerationRunDb(user_id=user_id, status='pending', **kwargs)
        db.add(run)
        db.flush()
        db.refresh(run)
        return run

    def delete(self, db: Session, run: MetadataGenerationRunDb) -> None:
        db.delete(run)
        db.flush()


class DescriptionSuggestionsRepository:

    def list_for_run(
        self,
        db: Session,
        run_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 1000,
        skip: int = 0,
    ) -> List[DescriptionSuggestionDb]:
        q = db.query(DescriptionSuggestionDb).filter(DescriptionSuggestionDb.run_id == run_id)
        if status:
            q = q.filter(DescriptionSuggestionDb.status == status)
        return (
            q.order_by(
                DescriptionSuggestionDb.table_fqn,
                # Table-level row (no column) first, then columns
                DescriptionSuggestionDb.column_name.isnot(None),
                DescriptionSuggestionDb.column_name,
            )
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_for_run(self, db: Session, run_id: str, *, status: Optional[str] = None) -> int:
        q = db.query(func.count(DescriptionSuggestionDb.id)).filter(DescriptionSuggestionDb.run_id == run_id)
        if status:
            q = q.filter(DescriptionSuggestionDb.status == status)
        return q.scalar() or 0

    def counts_by_status(self, db: Session, run_id: str) -> Dict[str, int]:
        rows = (
            db.query(DescriptionSuggestionDb.status, func.count(DescriptionSuggestionDb.id))
            .filter(DescriptionSuggestionDb.run_id == run_id)
            .group_by(DescriptionSuggestionDb.status)
            .all()
        )
        return {status: count for status, count in rows}

    def supersede_pending_for_targets(
        self, db: Session, *, exclude_run_id: str, targets: List[tuple]
    ) -> int:
        """Mark pending suggestions from other runs for the same targets as superseded.

        ``targets`` is a list of ``(table_fqn, column_name_or_None)``.
        """
        if not targets:
            return 0
        superseded = 0
        target_set = set(targets)
        table_fqns = {t[0] for t in targets}
        candidates = (
            db.query(DescriptionSuggestionDb)
            .filter(
                DescriptionSuggestionDb.run_id != exclude_run_id,
                DescriptionSuggestionDb.status == 'pending',
                DescriptionSuggestionDb.review_request_id.is_(None),
                DescriptionSuggestionDb.table_fqn.in_(table_fqns),
            )
            .all()
        )
        for sug in candidates:
            if (sug.table_fqn, sug.column_name) in target_set:
                sug.status = 'superseded'
                superseded += 1
        db.flush()
        return superseded


metagen_runs_repo = MetadataGenerationRunsRepository()
metagen_suggestions_repo = DescriptionSuggestionsRepository()
