"""Shared SQLAlchemy metadata and mapped-column conventions."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_NAME = "primary_signal"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base for all application-owned PostgreSQL tables."""

    metadata = MetaData(schema=SCHEMA_NAME, naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """Generate sortable UUIDv7 identifiers in the application process."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)


class CreatedAtMixin:
    """Record insertion time in the database clock."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UpdatedAtMixin(CreatedAtMixin):
    """Record insertion and last application update times."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
