"""Synthetic RSS/Atom parsing and hostile XML bounds."""

from datetime import UTC, datetime

import pytest

from primary_signal.ingestion.feed_parser import InvalidFeed, parse_feed
from primary_signal.ingestion.feed_polls import FeedFetchResult


def test_rss_entry_identity_and_dates_are_stable() -> None:
    feed = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Example</title>
      <item><guid>story-1</guid><link>HTTPS://PUBLIC.EXAMPLE:443/one</link>
        <title> First  story </title><description>Brief update</description>
        <pubDate>Wed, 07 Oct 2026 09:00:00 GMT</pubDate></item>
    </channel></rss>"""
    first = parse_feed(feed)
    second = parse_feed(feed)

    assert first == second
    assert len(first) == 1
    assert first[0].identity_method == "guid"
    assert first[0].reported_url == "HTTPS://PUBLIC.EXAMPLE:443/one"
    assert first[0].reported_title == "First story"
    assert first[0].reported_published_at == datetime(2026, 10, 7, 9, tzinfo=UTC)
    assert len(first[0].identity_key) == 64
    assert len(first[0].metadata_hash) == 64


def test_atom_uses_alternate_link_and_url_identity() -> None:
    feed = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Notice</title><link rel="self" href="https://public.example/feed/1"/>
      <link rel="alternate" href="https://public.example/article/1"/>
      <updated>2026-10-07T09:30:00Z</updated></entry></feed>"""
    entry = parse_feed(feed)[0]
    assert entry.identity_method == "url"
    assert entry.reported_url == "https://public.example/article/1"
    assert entry.reported_updated_at == datetime(2026, 10, 7, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<rss>",
        b"<!DOCTYPE rss [<!ENTITY x 'expanded'>]><rss>&x;</rss>",
        "<!DOCTYPE rss><rss/>".encode("utf-16"),
        b"<html><body>not a feed</body></html>",
        b"<rss>" + b"<x>" * 33 + b"</x>" * 33 + b"</rss>",
        b" " * (2 * 1024 * 1024 + 1),
    ],
)
def test_unsafe_or_unsupported_xml_is_rejected(body: bytes) -> None:
    with pytest.raises(InvalidFeed):
        parse_feed(body)


def test_invalid_entry_url_is_not_retrieved() -> None:
    invalid = (
        b"<rss><channel><item><guid>one</guid><link>javascript:bad</link></item></channel></rss>"
    )
    assert parse_feed(invalid)[0].reported_url is None


def test_fetch_result_requires_bounded_success_response() -> None:
    result = FeedFetchResult(status=304, final_url="https://public.example/feed", body=b"")
    assert result.status == 304
    with pytest.raises(ValueError):
        FeedFetchResult(status=304, final_url="https://public.example/feed", body=b"x")
    with pytest.raises(ValueError):
        FeedFetchResult(status=500, final_url="https://public.example/feed", body=b"")
    with pytest.raises(ValueError):
        FeedFetchResult(
            status=200, final_url="https://public.example/feed", body=b"x", etag="x\r\ny"
        )
