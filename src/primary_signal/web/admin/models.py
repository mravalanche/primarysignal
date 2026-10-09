"""Mapped admin storage tables for migration drift checks."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from primary_signal.db.base import Base


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    __table_args__ = (
        CheckConstraint("octet_length(digest) = 32", name="digest_length"),
        CheckConstraint("octet_length(csrf_secret) = 32", name="csrf_length"),
        CheckConstraint("last_seen_at >= created_at", name="time_order"),
    )

    digest: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    csrf_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminLoginAttempt(Base):
    __tablename__ = "admin_login_attempts"
    __table_args__ = (Index("ix_admin_login_attempts_recent", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
