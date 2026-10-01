"""Validated, insert-only persistence for configured sources and feeds."""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Connection, Engine, Table, func, insert
from sqlalchemy.exc import IntegrityError

from primary_signal.identity.urls import UrlIdentity, identify_url
from primary_signal.sources.models import Feed, Source

MAX_SOURCE_KEY_LENGTH = 100
MAX_NAME_LENGTH = 200
MAX_POLL_INTERVAL_SECONDS = 30 * 24 * 60 * 60

_SOURCE_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_sources = cast(Table, Source.__table__)
_feeds = cast(Table, Feed.__table__)


class SourceConflict(RuntimeError):
    """The source key is already configured."""


class FeedConflict(RuntimeError):
    """The feed URL is already configured."""


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: uuid.UUID
    source_key: str
    name: str
    homepage_url: str = field(repr=False)
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FeedRecord:
    id: uuid.UUID
    source_id: uuid.UUID
    name: str
    configured_url: str = field(repr=False)
    normalized_url: str = field(repr=False)
    url_hash: str = field(repr=False)
    url_normalization_version: int
    enabled: bool
    poll_interval_seconds: int
    next_poll_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _validated_source_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_SOURCE_KEY_LENGTH
        or _SOURCE_KEY.fullmatch(value) is None
    ):
        raise ValueError("source key must be lowercase words separated by hyphens")
    return value


def _validated_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be text")
    value = value.strip()
    if not 1 <= len(value) <= MAX_NAME_LENGTH:
        raise ValueError(f"name must be between 1 and {MAX_NAME_LENGTH} characters")
    return value


def _validated_interval(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("poll interval must be an integer number of seconds")
    if not 60 <= value <= MAX_POLL_INTERVAL_SECONDS:
        raise ValueError(
            f"poll interval must be between 60 and {MAX_POLL_INTERVAL_SECONDS} seconds"
        )
    return value


def _validated_url(value: object) -> tuple[str, UrlIdentity]:
    if not isinstance(value, str):
        raise ValueError("URL must be text")
    return value, identify_url(value)


def _validated_enabled(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("enabled must be a boolean")
    return value


def _validated_source_id(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ValueError("source id must be a UUID")
    return value


def _constraint_name(error: IntegrityError) -> str | None:
    diagnostic = getattr(error.orig, "diag", None)
    return cast(str | None, getattr(diagnostic, "constraint_name", None))


def _source_record(row: Any) -> SourceRecord:
    return SourceRecord(
        id=cast(uuid.UUID, row.id),
        source_key=cast(str, row.source_key),
        name=cast(str, row.name),
        homepage_url=cast(str, row.homepage_url),
        enabled=cast(bool, row.enabled),
        created_at=cast(datetime, row.created_at),
        updated_at=cast(datetime, row.updated_at),
    )


def _feed_record(row: Any) -> FeedRecord:
    return FeedRecord(
        id=cast(uuid.UUID, row.id),
        source_id=cast(uuid.UUID, row.source_id),
        name=cast(str, row.name),
        configured_url=cast(str, row.configured_url),
        normalized_url=cast(str, row.normalized_url),
        url_hash=cast(str, row.url_hash),
        url_normalization_version=cast(int, row.url_normalization_version),
        enabled=cast(bool, row.enabled),
        poll_interval_seconds=cast(int, row.poll_interval_seconds),
        next_poll_at=cast(datetime | None, row.next_poll_at),
        created_at=cast(datetime, row.created_at),
        updated_at=cast(datetime, row.updated_at),
    )


class SourceFeedRepository:
    """Source/feed writes within a caller-owned transaction."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create_source(
        self,
        *,
        source_key: str,
        name: str,
        homepage_url: str,
        enabled: bool = True,
    ) -> SourceRecord:
        key = _validated_source_key(source_key)
        validated_name = _validated_name(name)
        _, homepage = _validated_url(homepage_url)
        validated_enabled = _validated_enabled(enabled)

        statement = (
            insert(_sources)
            .values(
                id=uuid.uuid7(),
                source_key=key,
                name=validated_name,
                homepage_url=homepage.normalized_url,
                enabled=validated_enabled,
            )
            .returning(*_sources.c)
        )
        try:
            row = self._connection.execute(statement).one()
        except IntegrityError as error:
            if _constraint_name(error) == "uq_sources_source_key":
                raise SourceConflict("source key is already configured") from None
            raise
        return _source_record(row)

    def create_feed(
        self,
        *,
        source_id: uuid.UUID,
        name: str,
        configured_url: str,
        enabled: bool = True,
        poll_interval_seconds: int = 900,
    ) -> FeedRecord:
        validated_source_id = _validated_source_id(source_id)
        validated_name = _validated_name(name)
        preserved_url, identity = _validated_url(configured_url)
        interval = _validated_interval(poll_interval_seconds)
        validated_enabled = _validated_enabled(enabled)

        statement = (
            insert(_feeds)
            .values(
                id=uuid.uuid7(),
                source_id=validated_source_id,
                name=validated_name,
                configured_url=preserved_url,
                normalized_url=identity.normalized_url,
                url_hash=identity.url_hash,
                url_normalization_version=identity.normalization_version,
                enabled=validated_enabled,
                poll_interval_seconds=interval,
                next_poll_at=func.clock_timestamp(),
            )
            .returning(*_feeds.c)
        )
        try:
            row = self._connection.execute(statement).one()
        except IntegrityError as error:
            if _constraint_name(error) == "uq_feeds_url_normalization_version_url_hash":
                raise FeedConflict("feed URL is already configured") from None
            raise
        return _feed_record(row)


type RepositoryFactory = Callable[[Connection], SourceFeedRepository]


class TransactionalSourceStore:
    """Run each source/feed write in a fresh, short transaction."""

    def __init__(
        self,
        engine: Engine,
        *,
        repository_factory: RepositoryFactory = SourceFeedRepository,
    ) -> None:
        self._engine = engine
        self._repository_factory = repository_factory

    def create_source(
        self,
        *,
        source_key: str,
        name: str,
        homepage_url: str,
        enabled: bool = True,
    ) -> SourceRecord:
        with self._engine.begin() as connection:
            return self._repository_factory(connection).create_source(
                source_key=source_key,
                name=name,
                homepage_url=homepage_url,
                enabled=enabled,
            )

    def create_feed(
        self,
        *,
        source_id: uuid.UUID,
        name: str,
        configured_url: str,
        enabled: bool = True,
        poll_interval_seconds: int = 900,
    ) -> FeedRecord:
        with self._engine.begin() as connection:
            return self._repository_factory(connection).create_feed(
                source_id=source_id,
                name=name,
                configured_url=configured_url,
                enabled=enabled,
                poll_interval_seconds=poll_interval_seconds,
            )
