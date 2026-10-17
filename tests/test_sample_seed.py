"""The sample seed stays inside the disposable database boundary."""

import os

import pytest
from sqlalchemy import create_engine, text

from primary_signal.entrypoints import sample_seed
from primary_signal.entrypoints.sample_seed import assert_test_database, seed


class _Connection:
    def __init__(self, user: str, database: str) -> None:
        self.user = user
        self.database = database

    def execute(self, query: object) -> object:
        sql = str(query)
        value = self.user if "current_user" in sql else self.database
        return _Result(value)


class _Result:
    def __init__(self, value: str) -> None:
        self.value = value

    def scalar_one(self) -> str:
        return self.value


def test_seed_rejects_non_disposable_database() -> None:
    with pytest.raises(RuntimeError, match="disposable _test database"):
        assert_test_database(_Connection("lineage_test", "production"), "lineage_test")  # type: ignore[arg-type]


def test_seed_rejects_wrong_role() -> None:
    with pytest.raises(RuntimeError, match="role does not match"):
        assert_test_database(_Connection("other", "disposable_test"), "lineage_test")  # type: ignore[arg-type]


def test_seed_requires_distinct_roles() -> None:
    with pytest.raises(ValueError, match="separate logins"):
        seed(None, None, lineage_role="same", writer_role="same")  # type: ignore[arg-type]


def test_main_uses_both_test_roles_and_disposes_connections(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "PRIMARY_SIGNAL_TEST_DATABASE_URL", "postgresql+psycopg://lineage@/sample_test"
    )
    monkeypatch.setenv("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE", "lineage_test")
    monkeypatch.setenv(
        "PRIMARY_SIGNAL_TEST_PUBLICATION_DATABASE_URL", "postgresql+psycopg://writer@/sample_test"
    )
    monkeypatch.setenv("PRIMARY_SIGNAL_TEST_PUBLICATION_DATABASE_EXPECTED_ROLE", "publication_test")

    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engines = [FakeEngine(), FakeEngine()]
    pending = engines.copy()

    def fake_create_database_engine(_settings: object) -> FakeEngine:
        return pending.pop(0)

    def fake_seed(
        _lineage: object, _writer: object, *, lineage_role: str, writer_role: str
    ) -> bool:
        return lineage_role == "lineage_test" and writer_role == "publication_test"

    monkeypatch.setattr(sample_seed, "create_database_engine", fake_create_database_engine)
    monkeypatch.setattr(sample_seed, "seed", fake_seed)
    sample_seed.main()
    assert "synthetic draft created" in capsys.readouterr().out
    assert all(engine.disposed for engine in engines)


def test_main_rejects_missing_disposable_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRIMARY_SIGNAL_TEST_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="synthetic seed failed"):
        sample_seed.main()


@pytest.mark.postgres
def test_seed_creates_one_traceable_draft_and_rerun_is_idempotent() -> None:
    lineage_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    lineage_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    writer_url = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLICATION_DATABASE_URL")
    writer_role = os.environ.get("PRIMARY_SIGNAL_TEST_PUBLICATION_DATABASE_EXPECTED_ROLE")
    if not all((lineage_url, lineage_role, writer_url, writer_role)):
        pytest.skip("configure isolated disposable test database roles")
    assert lineage_url and lineage_role and writer_url and writer_role
    lineage = create_engine(lineage_url, hide_parameters=True)
    writer = create_engine(writer_url, hide_parameters=True)
    try:
        assert isinstance(
            seed(lineage, writer, lineage_role=lineage_role, writer_role=writer_role), bool
        )
        assert not seed(lineage, writer, lineage_role=lineage_role, writer_role=writer_role)
        with lineage.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT r.status, s.current_revision_id, rs.article_id, "
                        "rs.content_version_id, a.source_id, v.origin_fetch_attempt_id "
                        "FROM primary_signal.stories s JOIN primary_signal.story_revisions r "
                        "ON r.story_id=s.id JOIN primary_signal.revision_sources rs "
                        "ON rs.revision_id=r.id JOIN primary_signal.articles a "
                        "ON a.id=rs.article_id JOIN primary_signal.content_versions v "
                        "ON v.id=rs.content_version_id WHERE s.slug='synthetic-security-notice'"
                    )
                )
                .mappings()
                .one()
            )
            assert row["status"] == "draft"
            assert row["current_revision_id"] is None
            assert all(
                row[key] is not None
                for key in (
                    "article_id",
                    "content_version_id",
                    "source_id",
                    "origin_fetch_attempt_id",
                )
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM primary_signal.story_revisions r "
                        "JOIN primary_signal.stories s ON s.id=r.story_id "
                        "WHERE s.slug='synthetic-security-notice'"
                    )
                ).scalar_one()
                == 1
            )
    finally:
        writer.dispose()
        lineage.dispose()
