"""Validated records exposed by the curated public projection."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address
from urllib.parse import SplitResult, urlsplit, urlunsplit

PUBLIC_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_public_identifier(value: str, *, name: str, slug: bool = False) -> None:
    pattern = SLUG_PATTERN if slug else PUBLIC_ID_PATTERN
    if len(value) > 160 or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is not a valid public identifier")


def _require_text(value: str, *, name: str, maximum: int | None = None) -> None:
    if not value.strip() or (maximum is not None and len(value) > maximum):
        raise ValueError(f"{name} is not valid public text")


def validate_aware_datetime(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _is_ambiguous_numeric_host(host: str) -> bool:
    numeric_label = re.compile(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)")
    return all(numeric_label.fullmatch(label) is not None for label in host.split("."))


def _canonical_public_url(value: str) -> str:
    if _has_control_characters(value) or "\\" in value:
        raise ValueError("url contains unsafe characters")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("url is not a valid public URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("url is not a valid public URL")
    if "%" in host:
        raise ValueError("url host must not contain a zone identifier")

    canonical_host = host.casefold().rstrip(".")
    if not canonical_host:
        raise ValueError("url is not a valid public URL")
    try:
        address = ip_address(canonical_host)
    except ValueError:
        if _is_ambiguous_numeric_host(canonical_host):
            raise ValueError("url contains an ambiguous numeric address") from None
        try:
            canonical_host = canonical_host.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise ValueError("url host is not valid") from error
        if "." not in canonical_host or canonical_host.endswith((".localhost", ".local")):
            raise ValueError("url host is not a public hostname") from None
        canonical_netloc = canonical_host
    else:
        if not address.is_global:
            raise ValueError("url contains a non-public literal address")
        canonical_netloc = f"[{address.compressed}]" if address.version == 6 else str(address)

    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
        canonical_netloc = f"{canonical_netloc}:{port}"
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme,
            netloc=canonical_netloc,
            path=parsed.path,
            query=parsed.query,
            fragment=parsed.fragment,
        )
    )


def _require_enum(value: object, enum_type: type[StrEnum], *, name: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{name} is not supported")


class Topic(StrEnum):
    """Primary topics from the product contract."""

    VULNERABILITIES_AND_EXPLOITATION = "vulnerabilities-and-exploitation"
    THREAT_ACTIVITY_AND_INCIDENTS = "threat-activity-and-incidents"
    SECURITY_ENGINEERING = "security-engineering"
    POLICY_AND_STRATEGY = "policy-and-strategy"
    RESEARCH_AND_TOOLS = "research-and-tools"


class StoryType(StrEnum):
    """Editorial forms from the product contract."""

    NEWS = "news"
    RESEARCH = "research"
    ADVISORY = "advisory"
    INCIDENT = "incident"
    ANALYSIS = "analysis"
    OPINION = "opinion"
    TOOL_RELEASE = "tool-release"


class TagKind(StrEnum):
    """Public tag and entity kinds supported by the product contract."""

    CVE = "cve"
    ORGANISATION = "organisation"
    PRODUCT = "product"
    ACTOR = "actor"
    TECHNOLOGY = "technology"
    SECTOR = "sector"
    CURATED = "curated"


class PublicSignalKind(StrEnum):
    """Signals which may pass the contract's public evidence rules."""

    PRIMARY_SOURCE = "primary-source"
    OFFICIAL_ADVISORY = "official-advisory"
    ACTIVE_EXPLOITATION = "active-exploitation"
    EXPLOIT_AVAILABLE = "exploit-available"
    ACTIONABLE = "actionable"
    CONFIRMED_INCIDENT = "confirmed-incident"
    DEVELOPING = "developing"
    WIDELY_REPORTED = "widely-reported"
    DEEP_READ = "deep-read"


@dataclass(frozen=True, slots=True)
class PublicTag:
    """A public canonical tag or entity label."""

    id: str
    label: str
    kind: TagKind

    def __post_init__(self) -> None:
        validate_public_identifier(self.id, name="tag id")
        _require_text(self.label, name="tag label", maximum=120)
        _require_enum(self.kind, TagKind, name="tag kind")


@dataclass(frozen=True, slots=True)
class PublicSignal:
    """An evidence-gated marker linked only to public source identifiers."""

    kind: PublicSignalKind
    evidence_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_enum(self.kind, PublicSignalKind, name="signal kind")
        if not self.evidence_source_ids:
            raise ValueError("a public signal requires public source evidence")
        for source_id in self.evidence_source_ids:
            validate_public_identifier(source_id, name="evidence source id")
        if len(set(self.evidence_source_ids)) != len(self.evidence_source_ids):
            raise ValueError("signal evidence source ids must be unique")


@dataclass(frozen=True, slots=True)
class PublicSource:
    """A source in the intentionally public story trail."""

    id: str
    title: str
    publisher: str
    url: str
    first_published_at: datetime | None
    is_primary: bool

    def __post_init__(self) -> None:
        validate_public_identifier(self.id, name="source id")
        _require_text(self.title, name="source title", maximum=300)
        _require_text(self.publisher, name="source publisher", maximum=160)
        object.__setattr__(self, "url", _canonical_public_url(self.url))
        if self.first_published_at is not None:
            validate_aware_datetime(self.first_published_at, name="source first_published_at")


@dataclass(frozen=True, slots=True)
class PublicStorySummary:
    """The validated public projection used by story listings."""

    slug: str
    headline: str
    synthesis: str
    why_it_matters: str
    primary_topic: Topic
    story_type: StoryType
    first_reported_at: datetime
    latest_material_update_at: datetime
    source_count: int
    tags: tuple[PublicTag, ...] = ()
    signals: tuple[PublicSignal, ...] = ()
    uk_relevant: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier(self.slug, name="story slug", slug=True)
        _require_text(self.headline, name="headline", maximum=300)
        _require_text(self.synthesis, name="synthesis")
        _require_text(self.why_it_matters, name="why_it_matters")
        _require_enum(self.primary_topic, Topic, name="primary topic")
        _require_enum(self.story_type, StoryType, name="story type")
        validate_aware_datetime(self.first_reported_at, name="first_reported_at")
        validate_aware_datetime(self.latest_material_update_at, name="latest_material_update_at")
        if self.latest_material_update_at < self.first_reported_at:
            raise ValueError("latest material update cannot predate first report")
        if self.source_count < 1:
            raise ValueError("a published story must have at least one source")
        tag_ids = tuple(tag.id for tag in self.tags)
        if len(set(tag_ids)) != len(tag_ids):
            raise ValueError("story tag ids must be unique")
        signal_kinds = tuple(signal.kind for signal in self.signals)
        if len(set(signal_kinds)) != len(signal_kinds):
            raise ValueError("story signal kinds must be unique")


@dataclass(frozen=True, slots=True)
class PublicStory(PublicStorySummary):
    """A published story and its intentional public source trail."""

    sources: tuple[PublicSource, ...] = ()

    def __post_init__(self) -> None:
        PublicStorySummary.__post_init__(self)
        if len(self.sources) != self.source_count:
            raise ValueError("source count must match the public source trail")
        source_ids = {source.id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("public source ids must be unique")
        for source in self.sources:
            if (
                source.first_published_at is not None
                and source.first_published_at > self.latest_material_update_at
            ):
                raise ValueError("source publication cannot follow the latest material update")
        for signal in self.signals:
            if not set(signal.evidence_source_ids) <= source_ids:
                raise ValueError("signal evidence must reference the public source trail")


@dataclass(frozen=True, slots=True)
class PublicStoryPage:
    """One cursor-addressed page of published stories."""

    items: tuple[PublicStorySummary, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class StoryListQuery:
    """Filters accepted by a public story reader."""

    limit: int
    cursor: str | None = None
    topic: Topic | None = None
    story_type: StoryType | None = None
    uk_relevant: bool | None = None
    tag_id: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if self.cursor is not None and (not self.cursor or len(self.cursor) > 500):
            raise ValueError("cursor is not valid")
        if self.tag_id is not None:
            validate_public_identifier(self.tag_id, name="tag id")
