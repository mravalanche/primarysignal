"""Metadata-only PostgreSQL capability for the article inventory reader."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

ROLE = "primary_signal_cap_article_inventory"
LOGIN = "inventory_test"
FUNCTION = "primary_signal.search_article_inventory(text,text,timestamptz,uuid,integer)"


@pytest.fixture
def inventory_engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Engine, Engine]]:
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    inventory_url = os.environ.get("PRIMARY_SIGNAL_TEST_INVENTORY_DATABASE_URL")
    if not admin_url or not expected_role or not inventory_url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires admin and restricted inventory DSNs")
        pytest.skip("set test admin and inventory DSNs for PostgreSQL privilege tests")

    admin = create_engine(admin_url, hide_parameters=True)
    inventory = create_engine(inventory_url, hide_parameters=True)
    with admin.connect() as connection:
        database_name, current_user = connection.execute(
            text("SELECT current_database(), current_user")
        ).one()
        assert str(database_name).endswith("_test")
        assert current_user == expected_role
        assert connection.execute(
            text("SELECT to_regrole(:role) IS NOT NULL"), {"role": ROLE}
        ).scalar_one(), "run scripts/bootstrap_test_database_roles.py before migrations"
    with inventory.connect() as connection:
        assert connection.execute(text("SELECT current_user")).scalar_one() == LOGIN

    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", admin_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
    try:
        yield admin, inventory
    finally:
        admin.dispose()
        inventory.dispose()


@pytest.mark.postgres
def test_inventory_capability_is_read_only_and_isolated(
    inventory_engines: tuple[Engine, Engine],
) -> None:
    admin, inventory = inventory_engines
    with admin.connect() as connection:
        attributes = connection.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolinherit, rolcreatedb, rolcreaterole, "
                "rolreplication, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=:role"
            ),
            {"role": ROLE},
        ).one()
        assert not any(attributes)
        assert connection.execute(
            text("SELECT pg_has_role(:login, :role, 'member')"),
            {"login": LOGIN, "role": ROLE},
        ).scalar_one()
        assert not connection.execute(
            text("SELECT pg_has_role('processor_test', :role, 'member')"), {"role": ROLE}
        ).scalar_one()

        assert connection.execute(
            text("SELECT has_function_privilege(:role, :function, 'EXECUTE')"),
            {"role": ROLE, "function": FUNCTION},
        ).scalar_one()
        assert not connection.execute(
            text("SELECT has_function_privilege('public', :function, 'EXECUTE')"),
            {"function": FUNCTION},
        ).scalar_one()
        for table in ("articles", "article_urls", "sources", "content_versions"):
            actual = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.column_privileges "
                        "WHERE grantee=:role AND table_schema='primary_signal' "
                        "AND table_name=:table AND privilege_type='SELECT'"
                    ),
                    {"role": ROLE, "table": table},
                ).scalars()
            )
            assert not actual
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                assert not connection.execute(
                    text(
                        "SELECT has_table_privilege(:role, 'primary_signal.' || :table, :privilege)"
                    ),
                    {"role": ROLE, "table": table, "privilege": privilege},
                ).scalar_one()
        assert not connection.execute(
            text("SELECT has_column_privilege(:role, 'primary_signal.jobs', 'id', 'SELECT')"),
            {"role": ROLE},
        ).scalar_one()
        assert not connection.execute(
            text(
                "SELECT has_column_privilege('public', 'primary_signal.content_versions', 'extracted_text', 'SELECT')"
            )
        ).scalar_one()
        assert connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_indexes "
                "WHERE schemaname='primary_signal' AND tablename='content_versions' "
                "AND indexname='ix_content_versions_english_search' "
                "AND indexdef LIKE '%USING gin%')"
            )
        ).scalar_one()

    with inventory.connect() as connection:
        connection.execute(
            text(
                "SELECT article_id FROM primary_signal.search_article_inventory("
                "'synthetic', NULL, NULL, NULL, 2)"
            )
        ).fetchall()

    denied = (
        "SELECT extracted_text FROM primary_signal.content_versions WHERE false",
        "SELECT id FROM primary_signal.articles WHERE false",
        "SELECT original_url FROM primary_signal.article_urls WHERE false",
        "SELECT raw_response_hash FROM primary_signal.content_versions WHERE false",
        "SELECT id FROM primary_signal.fetch_attempts WHERE false",
        "SELECT id FROM primary_signal.jobs WHERE false",
        "UPDATE primary_signal.articles SET last_seen_at=now() WHERE false",
        "DELETE FROM primary_signal.content_versions WHERE false",
        "INSERT INTO primary_signal.sources (id, source_key, name, homepage_url) "
        "VALUES (gen_random_uuid(), 'forbidden', 'Forbidden', 'https://public.example')",
    )
    for statement in denied:
        with pytest.raises(DBAPIError), inventory.begin() as connection:
            connection.execute(text(statement))


@pytest.mark.postgres
def test_inventory_grants_and_index_round_trip(
    inventory_engines: tuple[Engine, Engine],
) -> None:
    admin, _ = inventory_engines
    config = Config(Path(__file__).resolve().parents[1] / "alembic.ini")
    try:
        command.downgrade(config, "20261009_06")
        with admin.connect() as connection:
            assert not connection.execute(
                text("SELECT to_regprocedure(:function) IS NOT NULL"),
                {"function": FUNCTION},
            ).scalar_one()
            assert not connection.execute(
                text(
                    "SELECT to_regclass('primary_signal.ix_content_versions_english_search') IS NOT NULL"
                )
            ).scalar_one()
        command.upgrade(config, "head")
        with admin.connect() as connection:
            assert connection.execute(
                text("SELECT has_function_privilege(:role, :function, 'EXECUTE')"),
                {"role": ROLE, "function": FUNCTION},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT to_regclass('primary_signal.ix_content_versions_english_search') IS NOT NULL"
                )
            ).scalar_one()
    finally:
        command.upgrade(config, "head")
