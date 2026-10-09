"""Validation at the explicit operator publication boundary."""

import uuid
from datetime import UTC, datetime

import pytest

from primary_signal.publication.models import PublicSource, PublicStory, StoryType, Topic
from primary_signal.publication.writer import DraftReference, OperatorDecision, PublicationWriter


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


def test_input_fingerprint_is_stable_and_binds_content_version() -> None:
    from primary_signal.publication.writer import fingerprint_draft

    publication = story()
    article_id, first_version, second_version = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
    first = (DraftReference(publication.sources[0], article_id, first_version),)
    second = (DraftReference(publication.sources[0], article_id, second_version),)
    assert fingerprint_draft(publication, first) == fingerprint_draft(publication, first)
    assert fingerprint_draft(publication, first) != fingerprint_draft(publication, second)
