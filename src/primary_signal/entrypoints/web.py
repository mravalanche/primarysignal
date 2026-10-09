"""Serve the public or administration web surface."""

import argparse
import logging
from collections.abc import Callable, Sequence
from typing import cast

import uvicorn
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.observability import configure_logging, log_exception
from primary_signal.publication import PostgresStoryReader
from primary_signal.web.admin import create_admin_app
from primary_signal.web.admin.auth import AdminAuthService
from primary_signal.web.admin.session_store import (
    PostgresSessionStore,
    assert_admin_session_database_role,
)
from primary_signal.web.admin.settings import AdminAuthSettings
from primary_signal.web.public import create_public_app

LOGGER = logging.getLogger(__name__)

_PUBLIC_ROLE_CHECK = text(
    """
WITH RECURSIVE memberships(role_oid) AS (
    SELECT member_of.roleid FROM pg_catalog.pg_auth_members AS member_of
    WHERE member_of.member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
    UNION
    SELECT member_of.roleid FROM pg_catalog.pg_auth_members AS member_of
    JOIN memberships ON member_of.member = memberships.role_oid
)
SELECT
    session_user = current_user
    AND COALESCE((SELECT NOT (role.rolsuper OR role.rolcreaterole OR role.rolcreatedb
                          OR role.rolbypassrls OR role.rolreplication)
              FROM pg_catalog.pg_roles AS role WHERE role.rolname = current_user), false)
    AND pg_catalog.pg_has_role(current_user, 'primary_signal_cap_public_read', 'USAGE')
    AND NOT EXISTS (
        SELECT 1 FROM memberships
        JOIN pg_catalog.pg_roles AS inherited ON inherited.oid = memberships.role_oid
        WHERE inherited.rolname <> 'primary_signal_cap_public_read'
    )
    AND NOT pg_catalog.has_schema_privilege(current_user, 'primary_signal', 'USAGE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'public', 'CREATE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'primary_signal_public', 'CREATE')
    AND NOT EXISTS (
        SELECT 1 FROM (VALUES ('stories'), ('sources'), ('story_tags'),
                            ('tags'), ('signals'), ('signal_evidence')) AS required(name)
        WHERE NOT pg_catalog.has_table_privilege(
            current_user, 'primary_signal_public.' || required.name, 'SELECT')
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'primary_signal'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              pg_catalog.has_table_privilege(current_user, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR pg_catalog.has_any_column_privilege(current_user, relation.oid,
                  'SELECT,INSERT,UPDATE,REFERENCES')
          )
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'primary_signal_public'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND pg_catalog.has_table_privilege(current_user, relation.oid,
              'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
    ) AS is_restricted_public_reader
"""
)


def assert_public_database_role(connection: Connection) -> None:
    """Reject a public web login with private or administrative privileges."""

    if connection.execute(_PUBLIC_ROLE_CHECK).scalar_one() is not True:
        raise RuntimeError("public database role lacks the required restricted privileges")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("public", "admin"), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    return parser


def public_database_settings() -> DatabaseSettings:
    """Load the public login with a process-specific pool identity."""

    load_settings = cast(Callable[[], DatabaseSettings], DatabaseSettings)
    return load_settings().model_copy(
        update={"application_name": "primary_signal_public_web", "max_overflow": 0}
    )


def admin_session_database_settings() -> DatabaseSettings:
    """Load a distinct session-only login for the private web process."""

    load_settings = cast(Callable[[], DatabaseSettings], DatabaseSettings)
    return load_settings().model_copy(
        update={"application_name": "primary_signal_admin_session", "max_overflow": 0}
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Run exactly one web surface."""

    args = _parser().parse_args(argv)
    configure_logging(level="INFO")
    engine: Engine | None = None
    exit_code = 0
    try:
        settings = Settings()
        configure_logging(level=settings.log_level.value)
        if args.surface == "public":
            if settings.environment is RuntimeEnvironment.PRODUCTION:
                engine = create_database_engine(
                    public_database_settings(), search_path="pg_catalog"
                )
                # Check the configured public login before accepting requests.
                with engine.connect() as connection:
                    assert_public_database_role(connection)
                app = create_public_app(settings, story_reader=PostgresStoryReader(engine))
            else:
                app = create_public_app(settings)
        else:
            if settings.environment is RuntimeEnvironment.PRODUCTION:
                if args.host not in {"127.0.0.1", "::1"}:
                    raise RuntimeError("production administration must bind to loopback")
                engine = create_database_engine(
                    admin_session_database_settings(), search_path="pg_catalog"
                )
                with engine.connect() as connection:
                    assert_admin_session_database_role(connection)
                load_auth_settings = cast(Callable[[], AdminAuthSettings], AdminAuthSettings)
                admin_auth = AdminAuthService(
                    load_auth_settings().auth_config(), PostgresSessionStore(engine)
                )
                app = create_admin_app(settings, auth_service=admin_auth)
            else:
                app = create_admin_app(settings)
        # Do not let proxy headers rewrite client or scheme before the admin
        # app can reject untrusted forwarding metadata.
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            proxy_headers=False,
            log_config=None,
        )
    except Exception as exception:
        log_exception(LOGGER, "web.failed", exception, result="fatal")
        exit_code = 1
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception as exception:
                log_exception(LOGGER, "web.dispose.failed", exception, result="fatal")
                exit_code = 1
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
