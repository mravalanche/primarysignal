"""Validation at the explicit operator publication boundary."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.entrypoints import web
from primary_signal.publication.models import PublicSource, PublicStory, StoryType, Topic
from primary_signal.publication.writer import (
    DraftReference,
    OperatorDecision,
    PublicationWriter,
    assert_publication_writer_role,
)


def test_writer_rejects_a_login_without_its_restricted_capability() -> None:
    connection = MagicMock(spec=Connection)
    connection.execute.return_value.scalar_one.return_value = False
    with pytest.raises(RuntimeError, match="restricted privileges"):
        assert_publication_writer_role(connection)


def test_admin_web_runtime_does_not_open_writer_database(monkeypatch: pytest.MonkeyPatch) -> None:
    database_engine = MagicMock()
    admin_app = MagicMock()
    run_server = MagicMock()

    def fake_admin_app(settings: Settings) -> MagicMock:
        del settings
        return admin_app

    monkeypatch.setattr(web, "Settings", lambda: Settings(environment=RuntimeEnvironment.TEST))
    monkeypatch.setattr(web, "create_database_engine", database_engine)
    monkeypatch.setattr(web, "create_admin_app", fake_admin_app)
    monkeypatch.setattr(web.uvicorn, "run", run_server)

    web.main(["--surface", "admin"])

    database_engine.assert_not_called()
    run_server.assert_called_once()


def story() -> PublicStory:
    now = datetime.now(UTC)
    return PublicStory(
        slug="synthetic-notice",
        headline="Synthetic notice",
        synthesis="A synthetic update.",
        why_it_matters="Synthetic relevance.",
        primary_topic=Topic.SECURITY_ENGINEERING,
        story_type=StoryType.ADVISORY,
        first_reported_at=now,
        latest_material_update_at=now,
        source_count=1,
        sources=(
            PublicSource(
                id="source-one",
                title="Synthetic source",
                publisher="Public Example",
                url="https://public.example/notice",
                first_published_at=now,
                is_primary=True,
            ),
        ),
    )


def test_operator_decision_requires_named_actor_and_reason() -> None:
    with pytest.raises(ValueError, match="named operator"):
        OperatorDecision(actor="system", reason="reviewed")
    with pytest.raises(ValueError, match="decision reason"):
        OperatorDecision(actor="editor", reason=" ")


def test_draft_references_must_match_visible_trail() -> None:
    publication = story()
    writer = PublicationWriter(engine=None, expected_role="publication_test")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="references must match"):
        writer.create_draft(publication, ())
    other = PublicSource(
        id="different",
        title="Other",
        publisher="Public Example",
        url="https://public.example/other",
        first_published_at=None,
        is_primary=False,
    )
    with pytest.raises(ValueError, match="references must match"):
        writer.create_draft(publication, (DraftReference(other, uuid.uuid7(), uuid.uuid7()),))
