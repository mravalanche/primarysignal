"""Versioned response models for the public story API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from primary_signal.publication import PublicSignalKind, StoryType, TagKind, Topic


class PublicApiModel(BaseModel):
    """Common strict response-model behaviour."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class TagResponse(PublicApiModel):
    """A canonical tag or entity attached to a story."""

    id: str = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", max_length=160)
    label: str = Field(min_length=1, max_length=120)
    kind: TagKind


class SignalResponse(PublicApiModel):
    """A signal which has passed its public evidence rule."""

    kind: PublicSignalKind
    evidence_source_ids: tuple[str, ...]


class SourceResponse(PublicApiModel):
    """One intentional entry in a story's public source trail."""

    id: str = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", max_length=160)
    title: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=160)
    url: HttpUrl
    first_published_at: datetime | None
    is_primary: bool


class StorySummaryResponse(PublicApiModel):
    """A published story summary suitable for listings."""

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=160)
    headline: str = Field(min_length=1, max_length=300)
    synthesis: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    primary_topic: Topic
    story_type: StoryType
    first_reported_at: datetime
    latest_material_update_at: datetime
    source_count: int = Field(ge=1)
    tags: tuple[TagResponse, ...]
    signals: tuple[SignalResponse, ...]
    uk_relevant: bool


class StoryResponse(StorySummaryResponse):
    """A published story with its public source trail."""

    sources: tuple[SourceResponse, ...]


class StoryPageResponse(PublicApiModel):
    """A cursor-addressed page of published story summaries."""

    items: tuple[StorySummaryResponse, ...]
    next_cursor: str | None
