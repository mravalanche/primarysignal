"""Create queue capability and synthetic login roles for disposable CI tests."""

import os
from pathlib import Path
from typing import Any, LiteralString, cast

from psycopg import Connection as PsycopgConnection
from psycopg import sql
from sqlalchemy import Connection as SqlAlchemyConnection
from sqlalchemy import create_engine, text
from sqlalchemy.engine import RowMapping

TEST_DATABASE_URL_ENV = "PRIMARY_SIGNAL_TEST_DATABASE_URL"
SUBMIT_ROLE = "primary_signal_cap_queue_submit"
CONSUME_ROLE = "primary_signal_cap_queue_consume"
FEED_SCHEDULE_ROLE = "primary_signal_cap_feed_schedule"
FEED_POLL_ROLE = "primary_signal_cap_feed_poll"
ARTICLE_PERSIST_ROLE = "primary_signal_cap_article_persist"
ARTICLE_INVENTORY_ROLE = "primary_signal_cap_article_inventory"
PUBLIC_READ_ROLE = "primary_signal_cap_public_read"
PUBLICATION_WRITE_ROLE = "primary_signal_cap_publication_write"
SOURCE_HEALTH_ROLE = "primary_signal_cap_source_health"
SCHEDULER_ROLE = "scheduler_test"
PROCESSOR_ROLE = "processor_test"
INVENTORY_ROLE = "inventory_test"
PUBLIC_ROLE = "public_test"
PUBLICATION_ROLE = "publication_test"
HEALTH_ROLE = "health_test"
SCHEDULER_PASSWORD = "primary_signal_scheduler_test_only"  # noqa: S105  # pragma: allowlist secret
PROCESSOR_PASSWORD = "primary_signal_processor_test_only"  # noqa: S105  # pragma: allowlist secret
INVENTORY_PASSWORD = "primary_signal_inventory_test_only"  # noqa: S105  # pragma: allowlist secret
PUBLIC_PASSWORD = "primary_signal_public_test_only"  # noqa: S105  # pragma: allowlist secret
PUBLICATION_PASSWORD = "primary_signal_publication_test_only"  # noqa: S105  # pragma: allowlist secret
HEALTH_PASSWORD = "primary_signal_health_test_only"  # noqa: S105  # pragma: allowlist secret


def _role_rows(
    connection: SqlAlchemyConnection, role_names: tuple[str, ...]
) -> dict[str, RowMapping]:
    rows = connection.execute(
        text(
            "SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreatedb, "
            "rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_catalog.pg_roles WHERE rolname = ANY(:role_names)"
        ),
        {"role_names": list(role_names)},
    ).mappings()
    return {str(row["rolname"]): row for row in rows}


def _create_or_verify_login(
    connection: SqlAlchemyConnection,
    *,
    role_name: str,
    password: str,
    capabilities: tuple[str, ...],
) -> None:
    rows = _role_rows(connection, (role_name,))
    if not rows:
        # Use the driver directly so an exception cannot include a SQLAlchemy
        # statement containing the fixed, disposable test credential.
        driver = cast(PsycopgConnection[Any], connection.connection.driver_connection)
        with driver.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role_name), sql.Literal(password))
            )
            for capability in capabilities:
                cursor.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(capability), sql.Identifier(role_name)
                    )
                )
        return

    row = rows[role_name]
    safe_attributes = (
        row["rolcanlogin"]
        and row["rolinherit"]
        and not row["rolsuper"]
        and not row["rolcreatedb"]
        and not row["rolcreaterole"]
        and not row["rolreplication"]
        and not row["rolbypassrls"]
    )
    if not safe_attributes:
        raise SystemExit(f"existing synthetic login role {role_name} has unsafe attributes")

    membership_rows = connection.execute(
        text(
            "SELECT parent.rolname, membership.admin_option, "
            "membership.inherit_option, membership.set_option "
            "FROM pg_catalog.pg_auth_members AS membership "
            "JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid "
            "JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member "
            "WHERE member.rolname = :role_name"
        ),
        {"role_name": role_name},
    ).mappings()
    memberships = {
        str(row["rolname"]): (
            bool(row["admin_option"]),
            bool(row["inherit_option"]),
            bool(row["set_option"]),
        )
        for row in membership_rows
    }
    expected_memberships = {capability: (False, True, True) for capability in capabilities}
    members = set(
        connection.execute(
            text(
                "SELECT member.rolname FROM pg_catalog.pg_auth_members AS membership "
                "JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid "
                "JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member "
                "WHERE parent.rolname = :role_name"
            ),
            {"role_name": role_name},
        ).scalars()
    )
    owned = connection.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend AS dependency "
            "JOIN pg_catalog.pg_roles AS role ON role.oid = dependency.refobjid "
            "WHERE dependency.refclassid = 'pg_catalog.pg_authid'::regclass "
            "AND role.rolname = :role_name AND dependency.deptype IN ('a', 'o'))"
        ),
        {"role_name": role_name},
    ).scalar_one()
    if memberships != expected_memberships or members or owned:
        raise SystemExit(f"existing synthetic login role {role_name} is not isolated")


def main() -> None:
    """Apply the test role bootstrap without printing URLs or credentials."""

    database_url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not database_url:
        raise SystemExit(f"{TEST_DATABASE_URL_ENV} is required")

    repository_root = Path(__file__).resolve().parents[1]
    bootstrap_paths = (
        repository_root / "deploy/postgres/initdb/010_queue_capability_roles.sql",
        repository_root / "deploy/postgres/initdb/020_feed_scheduler_capability_role.sql",
        repository_root / "deploy/postgres/initdb/030_feed_poll_capability_role.sql",
        repository_root / "deploy/postgres/initdb/040_article_persist_capability_role.sql",
        repository_root / "deploy/postgres/initdb/050_article_inventory_capability_role.sql",
        repository_root / "deploy/postgres/initdb/060_public_projection_roles.sql",
        repository_root / "deploy/postgres/initdb/070_publication_writer_capability_role.sql",
                repository_root / "deploy/postgres/initdb/080_source_health_capability_role.sql",
    )
    engine = create_engine(database_url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            database_name = str(connection.execute(text("SELECT current_database()")).scalar_one())
            if not database_name.endswith("_test"):
                raise SystemExit("role bootstrap requires a disposable database ending in _test")
            # This is intentionally a one-shot bootstrap. The SQL refuses
            # pre-existing memberships, ACLs or ownership before test logins
            # receive either capability.
            driver = cast(PsycopgConnection[Any], connection.connection.driver_connection)
            with driver.cursor() as cursor:
                for bootstrap_path in bootstrap_paths:
                    # No parameter collection: psycopg must leave PostgreSQL's
                    # format('%I', ...) placeholder for the server to interpret.
                    bootstrap_sql = bootstrap_path.read_text(encoding="utf-8")
                    cursor.execute(sql.SQL(cast(LiteralString, bootstrap_sql)))
            _create_or_verify_login(
                connection,
                role_name=SCHEDULER_ROLE,
                password=SCHEDULER_PASSWORD,
                capabilities=(SUBMIT_ROLE, FEED_SCHEDULE_ROLE),
            )
            _create_or_verify_login(
                connection,
                role_name=PROCESSOR_ROLE,
                password=PROCESSOR_PASSWORD,
                capabilities=(SUBMIT_ROLE, CONSUME_ROLE, FEED_POLL_ROLE, ARTICLE_PERSIST_ROLE),
            )
            _create_or_verify_login(
                connection,
                role_name=INVENTORY_ROLE,
                password=INVENTORY_PASSWORD,
                capabilities=(ARTICLE_INVENTORY_ROLE,),
            )
            _create_or_verify_login(
                connection,
                role_name=HEALTH_ROLE,
                password=HEALTH_PASSWORD,
                capabilities=(SOURCE_HEALTH_ROLE,),
            )
            _create_or_verify_login(
                connection,
                role_name=PUBLIC_ROLE,
                password=PUBLIC_PASSWORD,
                capabilities=(PUBLIC_READ_ROLE,),
            )
            _create_or_verify_login(
                connection,
                role_name=PUBLICATION_ROLE,
                password=PUBLICATION_PASSWORD,
                capabilities=(PUBLICATION_WRITE_ROLE,),
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
