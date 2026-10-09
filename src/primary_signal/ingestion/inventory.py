"""Bounded, read-only inventory of articles with a current extracted version."""

import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Connection, text

_SOURCE_KEY = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


@dataclass(frozen=True, slots=True)
class ArticleListQuery:
    search: str | None = None
    source_key: str | None = None
    limit: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.search is not None and (
            not self.search.strip()
            or len(self.search) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in self.search)
        ):
            raise ValueError("search must be nonempty, printable, and at most 200 characters")
        if self.source_key is not None and (
            len(self.source_key) > 128 or not _SOURCE_KEY.fullmatch(self.source_key)
        ):
            raise ValueError("invalid source key")
        if type(self.limit) is not int or not 1 <= self.limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if self.cursor is not None and (not self.cursor or len(self.cursor) > 500):
            raise ValueError("invalid inventory cursor")


@dataclass(frozen=True, slots=True)
class ArticleItem:
    article_id: uuid.UUID
    source_key: str
    source_name: str
    canonical_url: str
    title: str | None
    first_seen_at: datetime
    fetched_at: datetime
    word_count: int | None


@dataclass(frozen=True, slots=True)
class ArticlePage:
    items: tuple[ArticleItem, ...]
    next_cursor: str | None


def _filter_hash(query: ArticleListQuery) -> str:
    canonical = json.dumps([query.search, query.source_key], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_cursor(query: ArticleListQuery, item: ArticleItem) -> str:
    payload = [1, _filter_hash(query), item.first_seen_at.isoformat(), str(item.article_id)]
    serialized = json.dumps(payload, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(serialized).rstrip(b"=").decode("ascii")


def _decode_cursor(query: ArticleListQuery) -> tuple[datetime, uuid.UUID] | None:
    if query.cursor is None:
        return None
    try:
        encoded = query.cursor.encode("ascii")
        raw = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        decoded = cast(object, json.loads(raw))
        if not isinstance(decoded, list):
            raise ValueError
        payload = cast(list[object], decoded)
        if len(payload) != 4:
            raise ValueError
        version, fingerprint, timestamp, identifier = payload
        if version != 1 or fingerprint != _filter_hash(query):
            raise ValueError
        if not isinstance(timestamp, str) or not isinstance(identifier, str):
            raise ValueError
        position_at = datetime.fromisoformat(timestamp)
        if position_at.utcoffset() is None:
            raise ValueError
        return position_at.astimezone(UTC), uuid.UUID(identifier)
    except (UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise ValueError("invalid inventory cursor") from exc


class ArticleInventoryRepository:
    """List metadata from current article versions; the body is never returned."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def list_articles(self, query: ArticleListQuery) -> ArticlePage:
        position = _decode_cursor(query)
        params: dict[str, object] = {
            "search": query.search,
            "source_key": query.source_key,
            "cursor_at": position[0] if position is not None else None,
            "cursor_id": position[1] if position is not None else None,
            "row_limit": query.limit + 1,
        }
        statement = text(
            "SELECT article_id, source_key, source_name, canonical_url, "
            "title, first_seen_at, fetched_at, word_count "
            "FROM primary_signal.search_article_inventory("
            "CAST(:search AS text), CAST(:source_key AS text), "
            "CAST(:cursor_at AS timestamptz), CAST(:cursor_id AS uuid), "
            "CAST(:row_limit AS integer))"
        )
        rows = self._connection.execute(statement, params).mappings().all()
        items = tuple(ArticleItem(**row) for row in rows[: query.limit])
        next_cursor = _encode_cursor(query, items[-1]) if len(rows) > query.limit else None
        return ArticlePage(items, next_cursor)
