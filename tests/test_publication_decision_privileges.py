"""The admin decision login may invoke transitions, but cannot edit their inputs."""

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from primary_signal.publication.decision_writer import assert_publication_decision_role


@pytest.mark.postgres
def test_publication_decision_login_is_transition_only(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    restricted_url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLICATION_DECISION_DATABASE_URL")
    if not admin_url or not expected:
        pytest.skip("set disposable PostgreSQL test database settings")
    if not restricted_url:
        if os.environ.get("PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS") == "true":
            pytest.fail("CI requires the restricted publication decision login DSN")
        pytest.skip("set restricted publication decision login DSN")

    admin = create_engine(admin_url, hide_parameters=True)
    restricted = create_engine(restricted_url, hide_parameters=True)
    try:
        with admin.connect() as connection:
            assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
                "_test"
            )
            assert connection.execute(text("SELECT current_user")).scalar_one() == expected
        monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", admin_url)
        monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected)
        command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

        with restricted.connect() as connection:
            assert_publication_decision_role(connection, "publication_decision_test")
            with pytest.raises(RuntimeError, match="configured role"):
                assert_publication_decision_role(connection, "editorial_test")
            assert connection.execute(text("SELECT current_user")).scalar_one() == (
                "publication_decision_test"
            )
            assert connection.execute(
                text("SELECT has_schema_privilege(current_user, 'primary_signal', 'USAGE')")
            ).scalar_one()
            assert not connection.execute(
                text("SELECT has_schema_privilege(current_user, 'primary_signal', 'CREATE')")
            ).scalar_one()
            memberships = set(
                connection.execute(
                    text(
                        "SELECT parent.rolname FROM pg_catalog.pg_auth_members AS membership "
                        "JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid "
                        "WHERE membership.member = (SELECT oid FROM pg_catalog.pg_roles "
                        "WHERE rolname = current_user)"
                    )
                ).scalars()
            )
            assert memberships == {"primary_signal_cap_publication_decision"}

        with admin.connect() as connection, pytest.raises(RuntimeError, match="restricted"):
            assert_publication_decision_role(connection, expected)

        with restricted.connect() as connection:
            allowed = {
                "publish_reviewed(uuid,uuid,text,uuid,text,text)",
                "suppress_reviewed(uuid,uuid,text,text)",
            }
            denied = {
                "draft_fingerprint_v2(uuid,uuid)",
                "lock_story_for_draft(text)",
                "finalize_draft(uuid,uuid)",
            }
            for signature in allowed | denied:
                can_execute = connection.execute(
                    text("SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"),
                    {"signature": f"primary_signal.{signature}"},
                ).scalar_one()
                assert can_execute is (signature in allowed), signature

            for table_name in connection.execute(
                text(
                    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'primary_signal'"
                )
            ).scalars():
                table = f"primary_signal.{table_name}"
                assert not connection.execute(
                    text(
                        "SELECT has_table_privilege(current_user, :table, "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ),
                    {"table": table},
                ).scalar_one(), table
                for column in connection.execute(
                    text(
                        "SELECT attname FROM pg_catalog.pg_attribute "
                        "WHERE attrelid = to_regclass(:table) AND attnum > 0 AND NOT attisdropped"
                    ),
                    {"table": table},
                ).scalars():
                    assert not connection.execute(
                        text(
                            "SELECT has_column_privilege(current_user, :table, :column, "
                            "'SELECT,INSERT,UPDATE,REFERENCES')"
                        ),
                        {"table": table, "column": column},
                    ).scalar_one(), (table, column)

        # The reviewed transitions are callable. Synthetic missing story IDs fail
        # at their own guard rather than a privilege check, without changing rows.
        missing = uuid.uuid7()
        with restricted.begin() as connection, pytest.raises(DBAPIError) as rejected:
            connection.execute(
                text("SELECT primary_signal.suppress_reviewed(:story,:current,:actor,:reason)"),
                {
                    "story": missing,
                    "current": missing,
                    "actor": "test-editor",
                    "reason": "Synthetic permission probe",
                },
            )
        assert getattr(rejected.value.orig, "sqlstate", None) == "P0001"
        for statement in (
            "SELECT id FROM primary_signal.stories LIMIT 1",
            "SELECT extracted_text FROM primary_signal.content_versions LIMIT 1",
            "SELECT digest FROM primary_signal.admin_sessions LIMIT 1",
            "UPDATE primary_signal.stories SET suppressed=true WHERE false",
            "INSERT INTO primary_signal.publication_events "
            "(id,story_id,revision_id,to_status,actor,reason) "
            "VALUES (uuidv7(),uuidv7(),uuidv7(),'draft','test-editor','bypass')",
            "SELECT primary_signal.lock_story_for_draft('synthetic')",
        ):
            with restricted.begin() as connection, pytest.raises(DBAPIError) as denied:
                connection.execute(text(statement))
            assert getattr(denied.value.orig, "sqlstate", None) == "42501", statement
    finally:
        restricted.dispose()
        admin.dispose()
