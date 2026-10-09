"""Restricted admin-session storage against disposable PostgreSQL."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.web.admin.auth import StoredSession
from primary_signal.web.admin.session_store import (
    PostgresSessionStore,
    assert_admin_session_database_role,
)


@pytest.mark.postgres
def test_restricted_admin_session_store(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    restricted_url = os.environ.get("PRIMARY_SIGNAL_TEST_ADMIN_SESSION_DATABASE_URL")
    if not admin_url or not expected:
        pytest.skip("set disposable PostgreSQL test database settings")
    if not restricted_url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires the restricted admin session login DSN")
        pytest.skip("set restricted admin session login DSN")
    engine = create_engine(admin_url, hide_parameters=True)
    restricted = create_engine(restricted_url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
                "_test"
            )
            assert connection.execute(text("SELECT current_user")).scalar_one() == expected
        monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", admin_url)
        monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected)
        command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")
        with restricted.connect() as connection:
            assert (
                connection.execute(text("SELECT current_user")).scalar_one() == "admin_session_test"
            )
            assert_admin_session_database_role(connection)
        store = PostgresSessionStore(restricted)
        now = datetime.now(UTC)
        digest = b"d" * 32
        source = b"s" * 32
        session = StoredSession(digest, b"c" * 32, now, now)
        assert store.claim_login_attempt(
            source, since=now - timedelta(minutes=15), at=now, source_limit=1, global_limit=2
        )
        assert not store.claim_login_attempt(
            source, since=now - timedelta(minutes=15), at=now, source_limit=1, global_limit=2
        )
        store.create(session)
        assert (
            store.find_and_touch(
                digest,
                now=now + timedelta(seconds=1),
                idle_since=now - timedelta(minutes=30),
                absolute_since=now - timedelta(hours=12),
            )
            is not None
        )
        store.revoke(digest)
        assert (
            store.find_and_touch(
                digest,
                now=now + timedelta(seconds=2),
                idle_since=now - timedelta(minutes=30),
                absolute_since=now - timedelta(hours=12),
            )
            is None
        )
        for statement in (
            "SELECT id FROM primary_signal.articles LIMIT 1",
            "SELECT id FROM primary_signal.stories LIMIT 1",
            "UPDATE primary_signal.story_revisions SET status = status WHERE false",
        ):
            with pytest.raises(DBAPIError), restricted.begin() as connection:
                connection.execute(text(statement))
    finally:
        restricted.dispose()
        engine.dispose()
