"""Explicit engine and session construction."""

from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import Pool

from primary_signal.db.settings import DatabaseSettings


class UnexpectedDatabaseRoleError(RuntimeError):
    """The connection authenticated as a role other than the expected role."""


def assert_database_role(connection: Connection, expected_role: str) -> None:
    actual_role = connection.execute(text("SELECT current_user")).scalar_one()
    if actual_role != expected_role:
        raise UnexpectedDatabaseRoleError("database role does not match the configured role")


def create_database_engine(
    settings: DatabaseSettings,
    *,
    pool_class: type[Pool] | None = None,
    search_path: str = "pg_catalog,primary_signal",
) -> Engine:
    """Create an engine that verifies the authenticated PostgreSQL role."""

    options: dict[str, Any] = {
        "connect_args": {
            "application_name": settings.application_name,
            "connect_timeout": settings.connect_timeout_seconds,
            "options": (
                f"-c timezone=UTC -c statement_timeout={settings.statement_timeout_ms} "
                f"-c lock_timeout={settings.lock_timeout_ms} "
                "-c idle_in_transaction_session_timeout="
                f"{settings.idle_transaction_timeout_ms} "
                f"-c search_path={search_path}"
            ),
        },
        "echo": False,
        "hide_parameters": True,
        "pool_pre_ping": True,
    }
    if pool_class is None:
        options.update(
            max_overflow=settings.max_overflow,
            pool_size=settings.pool_size,
            pool_timeout=settings.pool_timeout_seconds,
        )
    else:
        options["poolclass"] = pool_class
    engine = create_engine(settings.url.get_secret_value(), **options)

    @event.listens_for(engine, "engine_connect")
    def assert_expected_role(connection: Connection) -> None:
        assert_database_role(connection, settings.expected_role)

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the project's synchronous unit-of-work factory."""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
