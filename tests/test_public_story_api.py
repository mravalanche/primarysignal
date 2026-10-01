import asyncio
import json
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.publication import (
    EmptyStoryReader,
    InvalidCursor,
    PublicSignal,
    PublicSignalKind,
    PublicSource,
    PublicStory,
    PublicStoryPage,
    PublicStorySummary,
    PublicTag,
    StoryCursor,
    StoryListQuery,
    StoryType,
    TagKind,
    Topic,
    decode_cursor,
    encode_cursor,
)
from primary_signal.web.public import create_public_app

TEST_SETTINGS = Settings(environment=RuntimeEnvironment.TEST)
PUBLISHED_AT = datetime(2026, 9, 30, 8, 15, tzinfo=UTC)
UPDATED_AT = datetime(2026, 9, 30, 9, 30, tzinfo=UTC)


def request(app: FastAPI, path: str) -> Response:
    async def send() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://public.example",
        ) as client:
            return await client.get(path)

    return asyncio.run(send())


def story_summary(
    *,
    slug: str = "vendor-publishes-security-advisory",
    updated_at: datetime = UPDATED_AT,
) -> PublicStorySummary:
    return PublicStorySummary(
        slug=slug,
        headline="Vendor publishes security advisory",
        synthesis="The vendor has published fixes for a product vulnerability.",
        why_it_matters="Affected operators can now identify and apply the fixed release.",
        primary_topic=Topic.VULNERABILITIES_AND_EXPLOITATION,
        story_type=StoryType.ADVISORY,
        first_reported_at=PUBLISHED_AT,
        latest_material_update_at=updated_at,
        source_count=2,
        tags=(
            PublicTag(id="cve-2026-12345", label="CVE-2026-12345", kind=TagKind.CVE),
            PublicTag(id="example-product", label="Example Product", kind=TagKind.PRODUCT),
        ),
        signals=(
            PublicSignal(
                kind=PublicSignalKind.OFFICIAL_ADVISORY,
                evidence_source_ids=("vendor-advisory",),
            ),
            PublicSignal(
                kind=PublicSignalKind.ACTIONABLE,
                evidence_source_ids=("vendor-advisory",),
            ),
        ),
        uk_relevant=True,
    )


def story() -> PublicStory:
    summary = story_summary()
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
                id="vendor-advisory",
                title="Security advisory for Example Product",
                publisher="Example Vendor",
                url="https://vendor.example/advisories/example-product",
                first_published_at=PUBLISHED_AT,
                is_primary=True,
            ),
            PublicSource(
                id="research-analysis",
                title="Analysis of the Example Product vulnerability",
                publisher="Example Research Group",
                url="https://research.example/reports/example-product",
                first_published_at=UPDATED_AT,
                is_primary=False,
            ),
        ),
    )


@dataclass
class RecordingStoryReader:
    page: PublicStoryPage
    stories: dict[str, PublicStory]
    queries: list[StoryListQuery] = field(default_factory=lambda: list[StoryListQuery]())

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        self.queries.append(query)
        if query.cursor == "malformed":
            raise InvalidCursor("malformed cursor")
        return self.page

    def get_story(self, slug: str) -> PublicStory | None:
        return self.stories.get(slug)


@dataclass(frozen=True)
class CursorStoryReader:
    """Contract fake implementing the required total order and cursor binding."""

    stories: tuple[PublicStorySummary, ...]

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        filtered = tuple(
            story
            for story in self.stories
            if (query.topic is None or story.primary_topic is query.topic)
            and (query.story_type is None or story.story_type is query.story_type)
            and (query.uk_relevant is None or story.uk_relevant is query.uk_relevant)
        )
        ordered = sorted(filtered, key=lambda item: item.slug)
        ordered.sort(key=lambda item: item.latest_material_update_at, reverse=True)
        start = 0
        if query.cursor is not None:
            position = decode_cursor(query.cursor, query)
            try:
                start = next(
                    index + 1
                    for index, item in enumerate(ordered)
                    if item.latest_material_update_at == position.latest_material_update_at
                    and item.slug == position.slug
                )
            except StopIteration as error:
                raise InvalidCursor("cursor position is no longer available") from error
        items = tuple(ordered[start : start + query.limit])
        next_cursor = None
        if items and start + len(items) < len(ordered):
            last = items[-1]
            next_cursor = encode_cursor(
                StoryCursor(last.latest_material_update_at, last.slug), query
            )
        return PublicStoryPage(items=items, next_cursor=next_cursor)

    def get_story(self, slug: str) -> None:
        del slug
        return None


def test_lists_published_story_summaries_with_filters() -> None:
    summary = story_summary()
    reader = RecordingStoryReader(
        page=PublicStoryPage(items=(summary,), next_cursor="next-page"),
        stories={},
    )
    app = create_public_app(TEST_SETTINGS, story_reader=reader)

    response = request(
        app,
        "/api/v1/stories?limit=10&cursor=current-page"
        "&topic=vulnerabilities-and-exploitation&story_type=advisory&uk_relevant=true",
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["first_reported_at"] == "2026-09-30T08:15:00Z"
    assert item["latest_material_update_at"] == "2026-09-30T09:30:00Z"
    assert item["tags"] == [
        {"id": "cve-2026-12345", "label": "CVE-2026-12345", "kind": "cve"},
        {"id": "example-product", "label": "Example Product", "kind": "product"},
    ]
    assert item["signals"] == [
        {"kind": "official-advisory", "evidence_source_ids": ["vendor-advisory"]},
        {"kind": "actionable", "evidence_source_ids": ["vendor-advisory"]},
    ]
    assert response.json()["next_cursor"] == "next-page"
    assert reader.queries == [
        StoryListQuery(
            limit=10,
            cursor="current-page",
            topic=Topic.VULNERABILITIES_AND_EXPLOITATION,
            story_type=StoryType.ADVISORY,
            uk_relevant=True,
        )
    ]


def test_fetches_one_published_story_with_source_trail() -> None:
    published_story = story()
    reader = RecordingStoryReader(
        page=PublicStoryPage(items=()),
        stories={published_story.slug: published_story},
    )
    response = request(
        create_public_app(TEST_SETTINGS, story_reader=reader),
        f"/api/v1/stories/{published_story.slug}",
    )

    assert response.status_code == 200
    assert response.json()["sources"] == [
        {
            "id": "vendor-advisory",
            "title": "Security advisory for Example Product",
            "publisher": "Example Vendor",
            "url": "https://vendor.example/advisories/example-product",
            "first_published_at": "2026-09-30T08:15:00Z",
            "is_primary": True,
        },
        {
            "id": "research-analysis",
            "title": "Analysis of the Example Product vulnerability",
            "publisher": "Example Research Group",
            "url": "https://research.example/reports/example-product",
            "first_published_at": "2026-09-30T09:30:00Z",
            "is_primary": False,
        },
    ]


def test_empty_reader_and_http_errors() -> None:
    app = create_public_app(TEST_SETTINGS)

    assert request(app, "/api/v1/stories").json() == {
        "items": [],
        "next_cursor": None,
    }
    assert request(app, "/api/v1/stories/not-published").status_code == 404
    assert request(app, "/api/v1/stories?limit=51").status_code == 422
    assert request(app, "/api/v1/stories/Not_Valid").status_code == 422

    invalid_reader = RecordingStoryReader(PublicStoryPage(items=()), {})
    invalid_response = request(
        create_public_app(TEST_SETTINGS, story_reader=invalid_reader),
        "/api/v1/stories?cursor=malformed",
    )
    assert invalid_response.status_code == 400
    assert invalid_response.json() == {"detail": "Invalid pagination cursor"}


def test_production_requires_a_real_reader() -> None:
    production = Settings(environment=RuntimeEnvironment.PRODUCTION)

    with pytest.raises(RuntimeError, match="requires a story reader"):
        create_public_app(production)
    with pytest.raises(RuntimeError, match="requires a story reader"):
        create_public_app(production, story_reader=EmptyStoryReader())

    create_public_app(
        production,
        story_reader=RecordingStoryReader(PublicStoryPage(items=()), {}),
    )


def test_cursor_is_stable_across_ties_and_bound_to_filters() -> None:
    earlier = UPDATED_AT.replace(hour=8)
    reader = CursorStoryReader(
        (
            story_summary(slug="story-c", updated_at=earlier),
            story_summary(slug="story-b"),
            story_summary(slug="story-a"),
        )
    )
    first_query = StoryListQuery(limit=2, uk_relevant=True)
    first_page = reader.list_stories(first_query)

    assert [item.slug for item in first_page.items] == ["story-a", "story-b"]
    assert first_page.next_cursor is not None
    second_page = reader.list_stories(replace(first_query, cursor=first_page.next_cursor))
    assert [item.slug for item in second_page.items] == ["story-c"]

    with pytest.raises(InvalidCursor, match="filters"):
        decode_cursor(
            first_page.next_cursor,
            StoryListQuery(limit=2, uk_relevant=False),
        )
    with pytest.raises(InvalidCursor, match="invalid cursor"):
        decode_cursor("not-json", first_query)


def test_publication_boundary_rejects_unsafe_source_values() -> None:
    def make_source(
        *,
        url: str,
        source_id: str = "source-one",
        first_published_at: datetime = PUBLISHED_AT,
    ) -> PublicSource:
        return PublicSource(
            id=source_id,
            title="A public report",
            publisher="Example Publisher",
            url=url,
            first_published_at=first_published_at,
            is_primary=True,
        )

    hostile_userinfo = ":".join(("reader", "placeholder"))
    for url in (
        "file:///etc/passwd",
        f"https://{hostile_userinfo}@public.example/report",
        "http://127.0.0.1/report",
        "http://192.0.2.10/report",
        "http://2130706433/report",
        "http://0x7f000001/report",
        "http://0177.0.0.1/report",
        "http://localhost/report",
        "http://intranet/report",
        "http://host.local/report",
        "http://[fe80::1%25eth0]/report",
        "https://public.example/\nreport",
    ):
        with pytest.raises(ValueError, match=r"URL|address|host|characters"):
            make_source(url=url)
    assert (
        make_source(url="HTTPS://PUBLIC.EXAMPLE.:443/report").url == "https://public.example/report"
    )
    with pytest.raises(ValueError, match="timezone"):
        make_source(
            url="https://publisher.example/report",
            first_published_at=datetime(2026, 9, 30),
        )
    with pytest.raises(ValueError, match="identifier"):
        make_source(url="https://publisher.example/report", source_id="Bad ID")


def test_publication_boundary_rejects_inconsistent_story_values() -> None:
    summary = story_summary()
    with pytest.raises(ValueError, match="at least one source"):
        replace(summary, source_count=-1)
    with pytest.raises(ValueError, match="timezone"):
        replace(summary, first_reported_at=datetime(2026, 9, 30))
    with pytest.raises(ValueError, match="predate"):
        replace(summary, latest_material_update_at=PUBLISHED_AT.replace(hour=7))
    with pytest.raises(ValueError, match="identifier"):
        replace(summary, slug="Invalid slug")
    with pytest.raises(ValueError, match="not supported"):
        replace(summary, story_type=cast(StoryType, "invalid"))
    with pytest.raises(ValueError, match="not supported"):
        replace(summary.tags[0], kind=cast(TagKind, "invalid"))
    with pytest.raises(ValueError, match="requires public source evidence"):
        replace(summary.signals[0], evidence_source_ids=())

    published_story = story()
    with pytest.raises(ValueError, match="source count"):
        replace(published_story, source_count=3)
    with pytest.raises(ValueError, match="public source trail"):
        replace(
            published_story,
            signals=(
                replace(
                    published_story.signals[0],
                    evidence_source_ids=("missing-source",),
                ),
            ),
        )


def test_publication_boundary_enforces_projection_invariants() -> None:
    summary = story_summary()
    with pytest.raises(ValueError, match="public text"):
        replace(summary.tags[0], label="   ")
    with pytest.raises(ValueError, match="unique"):
        replace(
            summary.signals[0],
            evidence_source_ids=("vendor-advisory", "vendor-advisory"),
        )
    with pytest.raises(ValueError, match="tag ids"):
        replace(summary, tags=(summary.tags[0], summary.tags[0]))
    with pytest.raises(ValueError, match="signal kinds"):
        replace(summary, signals=(summary.signals[0], summary.signals[0]))

    published_story = story()
    with pytest.raises(ValueError, match="source ids"):
        replace(
            published_story,
            sources=(published_story.sources[0], published_story.sources[0]),
        )
    future_source = replace(
        published_story.sources[1],
        first_published_at=UPDATED_AT + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="follow the latest"):
        replace(
            published_story,
            sources=(published_story.sources[0], future_source),
        )

    undated = replace(published_story.sources[0], first_published_at=None)
    assert undated.first_published_at is None
    canonical_ip = replace(
        published_story.sources[0],
        url="https://[2606:4700:4700::1111]:8443/report",
    )
    assert canonical_ip.url == "https://[2606:4700:4700::1111]:8443/report"

    with pytest.raises(ValueError, match="limit"):
        StoryListQuery(limit=0)
    with pytest.raises(ValueError, match="cursor"):
        StoryListQuery(limit=10, cursor="")


def test_cursor_rejects_unsupported_and_malformed_positions() -> None:
    query = StoryListQuery(limit=10)

    def encoded(payload: object) -> str:
        return urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    with pytest.raises(InvalidCursor, match="unsupported"):
        decode_cursor(encoded({"v": 2, "position": {}, "filters": {}}), query)
    with pytest.raises(InvalidCursor, match="position"):
        decode_cursor(
            encoded(
                {
                    "v": 1,
                    "position": {"slug": "story-one"},
                    "filters": {
                        "topic": None,
                        "story_type": None,
                        "uk_relevant": None,
                    },
                }
            ),
            query,
        )
