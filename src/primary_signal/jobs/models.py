"""Durable, lease-based PostgreSQL job models."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from primary_signal.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin


class Job(UUIDPrimaryKeyMixin, UpdatedAtMixin, Base):
    """A durable unit of work claimed in a short transaction."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'dead', 'cancelled')", name="valid_status"
        ),
        CheckConstraint("payload_version > 0", name="positive_payload_version"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="object_payload"),
        CheckConstraint("octet_length(payload::text) <= 65536", name="bounded_payload"),
        CheckConstraint("priority BETWEEN -100 AND 100", name="priority_range"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempt_count BETWEEN 0 AND max_attempts", name="attempt_count_range"),
        CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'running' AND worker_id IS NULL AND lease_expires_at IS NULL)",
            name="lease_lifecycle",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'dead', 'cancelled') AND completed_at IS NOT NULL) OR (status IN ('queued', 'running') AND completed_at IS NULL)",
            name="completion_lifecycle",
        ),
        CheckConstraint(
            "last_error_detail IS NULL OR char_length(last_error_detail) <= 2048",
            name="bounded_error_detail",
        ),
        Index(
            "ix_jobs_claim",
            "queue",
            text("priority DESC"),
            "run_after",
            "id",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_jobs_recovery_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_jobs_status_created", "status", "created_at"),
        Index(
            "uq_jobs_active_deduplication",
            "queue",
            "job_type",
            "deduplication_key",
            unique=True,
            postgresql_where=text(
                "deduplication_key IS NOT NULL AND status IN ('queued', 'running')"
            ),
        ),
    )

    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    queue: Mapped[str] = mapped_column(
        Text, nullable=False, default="default", server_default="default"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    deduplication_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="queued", server_default="queued"
    )
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    worker_id: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_detail: Mapped[str | None] = mapped_column(Text)


class JobAttempt(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable history for one execution attempt."""

    __tablename__ = "job_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number > 0", name="positive_attempt_number"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'retry', 'dead', 'lease_expired', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint(
            "(finished_at IS NULL AND status = 'running') OR (finished_at IS NOT NULL AND status <> 'running')",
            name="finished_lifecycle",
        ),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="time_order"),
        CheckConstraint(
            "error_detail IS NULL OR char_length(error_detail) <= 2048", name="bounded_error_detail"
        ),
        UniqueConstraint("job_id", "attempt_number"),
        Index("ix_job_attempts_job_started", "job_id", "started_at"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("primary_signal.jobs.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="running", server_default="running"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
