"""Search contract at the public API and cursor boundary."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from primary_signal.publication import (
    InvalidCursor,
    PublicStoryPage,
    StoryCursor,
    StoryListQuery,
    Topic,
    decode_cursor,
    encode_cursor,
)
from primary_signal.web.public.stories import list_stories


class RecordingReader:
    def __init__(self) -> None:
        self.queries: list[StoryListQuery] = []

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        self.queries.append(query)
        if query.cursor is not None:
            raise InvalidCursor("malformed")
        return PublicStoryPage(items=())

    def get_story(self, slug: str) -> None:
        return None

    def get_tag(self, tag_id: str) -> None:
        return None


def test_search_normalizes_terms_and_returns_empty_page() -> None:
    reader = RecordingReader()
    page = list_stories(reader, q="CVE-2026-12345", topic=Topic.SECURITY_ENGINEERING)
    assert page.model_dump() == {"items": (), "next_cursor": None}
    assert reader.queries[0].q == "cve 2026 12345"
    assert reader.queries[0].topic is Topic.SECURITY_ENGINEERING


@pytest.mark.parametrize("query", ["!!!", "one\x00two", "a " * 9, "x" * 101])
def test_search_rejects_malformed_or_excessive_input(query: str) -> None:
    reader = RecordingReader()
    with pytest.raises(HTTPException) as error:
        list_stories(reader, q=query)
    assert error.value.status_code == 422
    assert reader.queries == []


def test_search_cursor_binds_normalized_query_and_filters() -> None:
    query = StoryListQuery(limit=1, q="  Vendor   Advisory ", uk_relevant=True)
    position = StoryCursor(datetime(2026, 1, 1, tzinfo=UTC), "synthetic-story", 0.075)
    cursor = encode_cursor(position, query)
    assert (
        decode_cursor(cursor, StoryListQuery(limit=10, q="vendor advisory", uk_relevant=True))
        == position
    )
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, StoryListQuery(limit=1, q="vendor incident", uk_relevant=True))
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, StoryListQuery(limit=1, q="vendor advisory"))
    with pytest.raises(InvalidCursor):
        decode_cursor(
            cursor, StoryListQuery(limit=1, q="vendor advisory", uk_relevant=True, tag_id="other")
        )


def test_search_cursor_rejects_invalid_rank() -> None:
    query = StoryListQuery(limit=1, q="vendor")
    with pytest.raises(InvalidCursor):
        encode_cursor(StoryCursor(datetime(2026, 1, 1, tzinfo=UTC), "synthetic-story"), query)


def test_search_cursor_fits_all_maximum_filters() -> None:
    query = StoryListQuery(
        limit=1,
        q="a" * 40 + " " + "b" * 40 + " " + "c" * 18,
        tag_id="t" * 160,
        topic=Topic.VULNERABILITIES_AND_EXPLOITATION,
        uk_relevant=True,
    )
    position = StoryCursor(datetime(2026, 1, 1, tzinfo=UTC), "s" * 160, 0.5)
    cursor = encode_cursor(position, query)
    assert 500 < len(cursor) <= 1024
    assert decode_cursor(cursor, query) == position
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor + "!", query)


def test_public_route_reports_malformed_search_cursor() -> None:
    with pytest.raises(HTTPException) as error:
        list_stories(RecordingReader(), q="vendor", cursor="malformed")
    assert error.value.status_code == 400
