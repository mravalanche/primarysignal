"""Server-rendered publication pages use only the public story projection."""

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.publication import (
    InvalidCursor,
    PublicSignal,
    PublicSignalKind,
    PublicSource,
    PublicStory,
    PublicStoryPage,
    PublicStorySummary,
    PublicTag,
    StoryListQuery,
    StoryType,
    TagKind,
    Topic,
)
from primary_signal.web.public import create_public_app

FIRST = datetime(2026, 10, 8, 9, 20, tzinfo=UTC)
UPDATED = datetime(2026, 10, 9, 10, 5, tzinfo=UTC)


def _summary() -> PublicStorySummary:
    return PublicStorySummary(
        slug="identity-advisory",
        headline="Identity service releases a session validation fix",
        synthesis="The vendor has published a fix for affected deployments.",
        why_it_matters="Operators can check and update affected installations.",
        primary_topic=Topic.SECURITY_ENGINEERING,
        story_type=StoryType.ADVISORY,
        first_reported_at=FIRST,
        latest_material_update_at=UPDATED,
        source_count=1,
        tags=(PublicTag("identity", "Identity", TagKind.CURATED),),
        signals=(PublicSignal(PublicSignalKind.OFFICIAL_ADVISORY, ("vendor",)),),
        uk_relevant=True,
    )


def _story() -> PublicStory:
    summary = _summary()
    return PublicStory(
        slug=summary.slug,
        headline=summary.headline,
        synthesis=summary.synthesis,
        why_it_matters=summary.why_it_matters,
        primary_topic=summary.primary_topic,
        story_type=summary.story_type,
        first_reported_at=summary.first_reported_at,
        latest_material_update_at=summary.latest_material_update_at,
        source_count=summary.source_count,
        tags=summary.tags,
        signals=summary.signals,
        uk_relevant=summary.uk_relevant,
        sources=(
            PublicSource(
                "vendor",
                "Session validation update",
                "Example Vendor",
                "https://vendor.public.example/advisory",
                FIRST,
                True,
            ),
        ),
    )


@dataclass
class Reader:
    page: PublicStoryPage
    detail: PublicStory | None = None
    queries: list[StoryListQuery] = field(default_factory=lambda: list[StoryListQuery]())
    requested_slugs: list[str] = field(default_factory=lambda: list[str]())
    tags: dict[str, PublicTag] = field(
        default_factory=lambda: {"identity": PublicTag("identity", "Identity", TagKind.CURATED)}
    )

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        self.queries.append(query)
        if query.cursor == "bad":
            raise InvalidCursor("invalid")
        return self.page

    def get_story(self, slug: str) -> PublicStory | None:
        self.requested_slugs.append(slug)
        return self.detail

    def get_tag(self, tag_id: str) -> PublicTag | None:
        return self.tags.get(tag_id)


def get(app: FastAPI, path: str) -> Response:
    async def send() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://public.example"
        ) as client:
            return await client.get(path)

    return asyncio.run(send())


def app_for(reader: Reader) -> FastAPI:
    return create_public_app(Settings(environment=RuntimeEnvironment.TEST), story_reader=reader)


def test_latest_renders_provenance_and_neutral_exact_timestamps() -> None:
    reader = Reader(PublicStoryPage((_summary(),), next_cursor="opaque.cursor"))
    response = get(app_for(reader), "/")

    assert response.status_code == 200
    assert "Friday 9 October" in response.text  # Grouped by latest update, not first report.
    assert "First reported" in response.text and "Last updated" in response.text
    assert '<time datetime="2026-10-08T09:20:00+00:00"' in response.text
    assert 'aria-label="First reported 8 Oct 2026, 09:20 UTC"' in response.text
    assert 'data-exact="8 Oct 2026, 09:20 UTC"' in response.text
    assert 'title="8 Oct 2026, 09:20 UTC"' not in response.text
    assert "/stories/identity-advisory#sources" in response.text
    assert "Official advisory" in response.text
    assert '<details class="ps-signal-control ps-signal-official-advisory">' in response.text
    assert '<summary class="ps-signal-chip">Official advisory</summary>' in response.text
    assert "/stories/identity-advisory#signal-official-advisory" in response.text
    assert 'class="badge badge-sm ps-tag ps-tag-curated" href="/tags/identity"' in response.text
    assert "cursor=opaque.cursor" in response.text
    assert reader.requested_slugs == []  # Listing never fetches full stories per row.


def test_topic_tabs_preserve_other_filters_without_carrying_cursor() -> None:
    reader = Reader(PublicStoryPage((_summary(),)))
    response = get(
        app_for(reader), "/?story_type=advisory&uk_relevant=true&topic=security-engineering"
    )

    assert response.status_code == 200
    assert 'href="/?story_type=advisory&amp;uk_relevant=true"' in response.text
    assert (
        'href="/?topic=research-and-tools&amp;story_type=advisory&amp;uk_relevant=true"'
        in response.text
    )
    assert 'aria-current="page">Security engineering</a>' in response.text
    assert '<input type="hidden" name="topic" value="security-engineering">' in response.text
    assert 'id="latest-topic"' not in response.text


def test_latest_passes_filters_and_cursor_to_reader_and_next_link() -> None:
    reader = Reader(PublicStoryPage((_summary(),), next_cursor="next+cursor"))
    response = get(
        app_for(reader),
        "/?topic=security-engineering&story_type=advisory&uk_relevant=true&cursor=previous",
    )

    assert response.status_code == 200
    assert reader.queries == [
        StoryListQuery(
            limit=20,
            cursor="previous",
            topic=Topic.SECURITY_ENGINEERING,
            story_type=StoryType.ADVISORY,
            uk_relevant=True,
        )
    ]
    assert (
        "topic=security-engineering&amp;story_type=advisory&amp;uk_relevant=true&amp;cursor=next%2Bcursor"
        in response.text
    )


def test_blank_select_values_represent_all_and_preserve_uk_filter() -> None:
    reader = Reader(PublicStoryPage((_summary(),)))
    app = app_for(reader)

    all_response = get(app, "/?topic=&story_type=")
    uk_response = get(app, "/?topic=&story_type=&uk_relevant=true")

    assert all_response.status_code == 200
    assert uk_response.status_code == 200
    assert reader.queries == [
        StoryListQuery(limit=20),
        StoryListQuery(limit=20, uk_relevant=True),
    ]


def test_latest_escapes_story_text_and_uses_canonical_tag_links() -> None:
    hostile = replace(
        _summary(),
        headline="<script>alert(1)</script>",
        synthesis="Read <unsafe> source",
        tags=(PublicTag("tag", "<tag>", TagKind.CURATED),),
    )
    response = get(app_for(Reader(PublicStoryPage((hostile,)))), "/")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;tag&gt;" in response.text
    assert 'href="/tags/tag"' in response.text
    assert "?tag=" not in response.text


def test_tag_page_uses_canonical_label_and_preserves_tag_in_pagination() -> None:
    reader = Reader(PublicStoryPage((_summary(),), next_cursor="next+cursor"))
    response = get(
        app_for(reader),
        "/tags/identity?topic=security-engineering&story_type=advisory&uk_relevant=true",
    )

    assert response.status_code == 200
    assert "Identity — Tags — Primary Signal" in response.text
    assert 'href="/tags/identity" aria-current="page"' in response.text
    assert 'action="/tags/identity"' in response.text
    assert 'href="/tags/identity?story_type=advisory&amp;uk_relevant=true"' in response.text
    assert (
        "/tags/identity?topic=security-engineering&amp;story_type=advisory"
        "&amp;uk_relevant=true&amp;cursor=next%2Bcursor"
    ) in response.text
    assert reader.queries == [
        StoryListQuery(
            limit=20,
            tag_id="identity",
            topic=Topic.SECURITY_ENGINEERING,
            story_type=StoryType.ADVISORY,
            uk_relevant=True,
        )
    ]


def test_tag_page_unknown_invalid_empty_and_cursor_states() -> None:
    reader = Reader(PublicStoryPage(()))
    app = app_for(reader)

    assert get(app, "/tags/unknown").status_code == 404
    assert get(app, "/tags/INVALID").status_code == 422
    assert "No stories found" in get(app, "/tags/identity").text
    invalid = get(app, "/tags/identity?cursor=bad")
    assert invalid.status_code == 400
    assert 'href="/tags/identity"' in invalid.text


def test_detail_has_full_source_trail_and_signal_evidence() -> None:
    reader = Reader(PublicStoryPage(()), detail=_story())
    response = get(app_for(reader), "/stories/identity-advisory#sources")

    assert response.status_code == 200
    assert 'id="sources"' in response.text
    assert 'id="source-vendor"' in response.text
    assert 'href="https://vendor.public.example/advisory"' in response.text
    assert 'href="#source-vendor"' in response.text
    assert "Why it matters" in response.text
    assert reader.requested_slugs == ["identity-advisory"]


def test_source_preview_is_lazy_bounded_escaped_and_links_to_full_trail() -> None:
    detail = _story()
    extra_sources = tuple(
        PublicSource(
            f"follow-up-{index}",
            "<script>source</script>" if index == 1 else f"Follow-up {index}",
            f"Publisher {index}",
            f"https://publisher.public.example/{index}",
            FIRST,
            False,
        )
        for index in range(1, 5)
    )
    detail = replace(detail, source_count=5, sources=(*detail.sources, *extra_sources))
    reader = Reader(PublicStoryPage((_summary(),)), detail=detail)
    app = app_for(reader)

    listing = get(app, "/")
    assert listing.status_code == 200
    assert reader.requested_slugs == []
    assert 'href="/stories/identity-advisory#sources"' in listing.text

    preview = get(app, "/stories/identity-advisory/sources-preview")
    assert preview.status_code == 200
    assert preview.text.count('class="ps-source-card"') == 3
    assert "View all 5 sources" in preview.text
    assert "Publisher 3" not in preview.text
    assert "&lt;script&gt;source&lt;/script&gt;" in preview.text
    assert "<script>source</script>" not in preview.text
    assert reader.requested_slugs == ["identity-advisory"]


def test_missing_source_preview_returns_404() -> None:
    reader = Reader(PublicStoryPage(()))
    response = get(app_for(reader), "/stories/missing-story/sources-preview")

    assert response.status_code == 404
    assert "Story not found" in response.text


def test_empty_invalid_cursor_and_missing_story_have_clear_states() -> None:
    reader = Reader(PublicStoryPage(()))
    app = app_for(reader)

    assert "No stories found" in get(app, "/").text
    bad = get(app, "/?cursor=bad")
    assert bad.status_code == 400 and "Page unavailable" in bad.text
    missing = get(app, "/stories/missing-story")
    assert missing.status_code == 404 and "Story not found" in missing.text
