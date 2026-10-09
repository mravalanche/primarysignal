"""Boundary checks for the read-only article inventory contract."""

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from primary_signal.ingestion.inventory import (
    ArticleItem,
    ArticleListQuery,
    _decode_cursor,  # pyright: ignore[reportPrivateUsage]
    _encode_cursor,  # pyright: ignore[reportPrivateUsage]
    _filter_hash,  # pyright: ignore[reportPrivateUsage]
)


@pytest.mark.parametrize(
    "search",
    ["", "  ", "x" * 201, "thing\x00", "thing\nother"],
)
def test_search_rejects_empty_oversized_or_control_text(search: str) -> None:
    with pytest.raises(ValueError, match="search"):
        ArticleListQuery(search=search)


@pytest.mark.parametrize("limit", [0, 51, True, 1.5])
def test_limit_is_bounded_integer(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        ArticleListQuery(limit=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("source_key", ["", "Bad", "source_key", "a" * 129])
def test_source_key_is_a_bounded_slug(source_key: str) -> None:
    with pytest.raises(ValueError, match="source key"):
        ArticleListQuery(source_key=source_key)


def test_cursor_binds_filters_without_exposing_search() -> None:
    item = ArticleItem(
        article_id=uuid.uuid7(),
        source_key="source-one",
        source_name="Source One",
        canonical_url="https://public.example/notice",
        title="Notice",
        first_seen_at=datetime.now(UTC),
        fetched_at=datetime.now(UTC),
        word_count=2,
    )
    query = ArticleListQuery(search="private search words", source_key="source-one")
    cursor = _encode_cursor(query, item)
    assert len(cursor) <= 500
    decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    assert b"private search words" not in decoded
    assert _decode_cursor(
        ArticleListQuery(search=query.search, source_key=query.source_key, cursor=cursor)
    ) == (
        item.first_seen_at,
        item.article_id,
    )
    with pytest.raises(ValueError, match="cursor"):
        _decode_cursor(
            ArticleListQuery(search="different", source_key=query.source_key, cursor=cursor)
        )


@pytest.mark.parametrize("cursor", ["!", "x" * 501, "bnVsbA", "W10"])
def test_malformed_cursor_rejected(cursor: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        query = ArticleListQuery(cursor=cursor)
        _decode_cursor(query)


def test_cursor_rejects_naive_timestamp() -> None:
    payload = [1, _filter_hash(ArticleListQuery()), "2026-10-09T10:00:00", str(uuid.uuid7())]
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(ValueError, match="cursor"):
        _decode_cursor(ArticleListQuery(cursor=cursor))
