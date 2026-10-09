"""Serve the public or administration web surface."""

import argparse
import logging
import os
from collections.abc import Callable, Sequence
from typing import cast

import uvicorn
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.observability import configure_logging, log_exception
from primary_signal.publication import PostgresStoryReader
from primary_signal.publication.decision_writer import (
    PublicationDecisionWriter,
    assert_publication_decision_role,
)
from primary_signal.publication.editorial_reader import (
    EditorialReader,
    assert_editorial_database_role,
)
from primary_signal.web.admin import create_admin_app
from primary_signal.web.admin.auth import AdminAuthService
from primary_signal.web.admin.session_store import (
    PostgresSessionStore,
    assert_admin_session_database_role,
)
from primary_signal.web.admin.settings import (
    AdminAuthSettings,
    AdminDecisionDatabaseSettings,
    AdminEditorialDatabaseSettings,
)
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


def admin_editorial_database_settings() -> AdminEditorialDatabaseSettings:
    """Load the separate editorial-read login for the private web process."""

    load_settings = cast(
        Callable[[], AdminEditorialDatabaseSettings], AdminEditorialDatabaseSettings
    )
    return load_settings().model_copy(
        update={"application_name": "primary_signal_admin_editorial", "max_overflow": 0}
    )


def admin_decision_database_settings() -> AdminDecisionDatabaseSettings | None:
    """Enable browser decisions only when a separate restricted login is configured."""

    url = os.environ.get("PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_URL")
    url_file = os.environ.get("PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_URL_FILE")
    role = os.environ.get("PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_EXPECTED_ROLE")
    if url is None and url_file is None and role is None:
        return None
    if (url is None and url_file is None) or not role:
        raise RuntimeError("admin publication database URL and role must be set together")
    load_settings = cast(Callable[[], AdminDecisionDatabaseSettings], AdminDecisionDatabaseSettings)
    return load_settings().model_copy(
        update={"application_name": "primary_signal_admin_decision", "max_overflow": 0}
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Run exactly one web surface."""

    args = _parser().parse_args(argv)
    configure_logging(level="INFO")
    engine: Engine | None = None
    editorial_engine: Engine | None = None
    decision_engine: Engine | None = None
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
                admin_settings = load_auth_settings()
                admin_auth = AdminAuthService(
                    admin_settings.auth_config(), PostgresSessionStore(engine)
                )
                editorial_settings = admin_editorial_database_settings()
                editorial_engine = create_database_engine(
                    editorial_settings, search_path="pg_catalog"
                )
                with editorial_engine.connect() as connection:
                    assert_editorial_database_role(connection, editorial_settings.expected_role)
                decision_settings = admin_decision_database_settings()
                decision_writer = None
                if decision_settings is not None:
                    decision_engine = create_database_engine(
                        decision_settings, search_path="pg_catalog"
                    )
                    with decision_engine.connect() as connection:
                        assert_publication_decision_role(
                            connection, decision_settings.expected_role
                        )
                    decision_writer = PublicationDecisionWriter(
                        decision_engine, expected_role=decision_settings.expected_role
                    )
                app = create_admin_app(
                    settings,
                    auth_service=admin_auth,
                    editorial_reader=EditorialReader(
                        editorial_engine, expected_role=editorial_settings.expected_role
                    ),
                    decision_writer=decision_writer,
                    decision_actor=admin_settings.decision_actor,
                    public_origin=admin_settings.public_origin,
                )
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
        if editorial_engine is not None:
            try:
                editorial_engine.dispose()
            except Exception as exception:
                log_exception(LOGGER, "web.editorial.dispose.failed", exception, result="fatal")
                exit_code = 1
        if decision_engine is not None:
            try:
                decision_engine.dispose()
            except Exception as exception:
                log_exception(LOGGER, "web.decision.dispose.failed", exception, result="fatal")
                exit_code = 1
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
