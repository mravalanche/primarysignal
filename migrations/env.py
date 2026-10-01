"""Alembic environment configured without storing or logging a database URL."""

from collections.abc import Mapping
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from primary_signal.db.base import SCHEMA_NAME, Base
from primary_signal.db.engine import create_database_engine
from primary_signal.db.models import *  # noqa: F403 - registers every mapped table
from primary_signal.db.settings import DatabaseSettings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_name(
    name: str | None,
    type_: str,
    parent_names: Mapping[str, str | None],
) -> bool:
    """Limit drift checks to explicitly schema-qualified application tables."""

    if type_ == "schema":
        return name == SCHEMA_NAME
    if type_ == "table":
        qualified_name = parent_names.get("schema_qualified_table_name")
        return qualified_name in target_metadata.tables
    return True


def run_migrations_offline() -> None:
    """Generate PostgreSQL SQL without connecting to a database."""

    settings = DatabaseSettings()
    context.configure(
        url=settings.url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        version_table_schema=SCHEMA_NAME,
    )
    context.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"')
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations after verifying the authenticated database role."""

    engine = create_database_engine(
        DatabaseSettings(),
        pool_class=NullPool,
        search_path="pg_catalog",
    )
    with engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            version_table_schema=SCHEMA_NAME,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            connection.execute(
                text(
                    "SELECT pg_catalog.pg_advisory_xact_lock("
                    "pg_catalog.hashtextextended('primary_signal_migrations', 0))"
                )
            )
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
