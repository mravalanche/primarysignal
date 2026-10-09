"""Restricted source-health grants against a disposable PostgreSQL database."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

ROLE = "primary_signal_cap_source_health"


@pytest.mark.postgres
def test_source_health_capability() -> None:
    url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not url or not expected:
        pytest.skip("set disposable PostgreSQL test database settings")
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
                "_test"
            )
            assert connection.execute(text("SELECT current_user")).scalar_one() == expected
        command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb "
                    "AND NOT rolcreaterole FROM pg_catalog.pg_roles WHERE rolname=:role"
                ),
                {"role": ROLE},
            ).scalar_one()
            for table, allowed, forbidden in (
                ("sources", "source_key", "homepage_url"),
                ("feeds", "last_success_at", "configured_url"),
            ):
                assert connection.execute(
                    text("SELECT has_column_privilege(:role, :table, :column, 'SELECT')"),
                    {"role": ROLE, "table": f"primary_signal.{table}", "column": allowed},
                ).scalar_one()
                assert not connection.execute(
                    text("SELECT has_column_privilege(:role, :table, :column, 'SELECT')"),
                    {"role": ROLE, "table": f"primary_signal.{table}", "column": forbidden},
                ).scalar_one()
                assert not connection.execute(
                    text("SELECT has_table_privilege(:role, :table, 'INSERT, UPDATE, DELETE')"),
                    {"role": ROLE, "table": f"primary_signal.{table}"},
                ).scalar_one()
    finally:
        engine.dispose()
