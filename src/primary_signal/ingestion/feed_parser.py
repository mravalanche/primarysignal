"""Bounded extraction of RSS and Atom entry metadata from untrusted XML."""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from primary_signal.identity.urls import InvalidUrl, identify_url

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_ELEMENTS = 12000
MAX_FIELD_LENGTH = 4096
_FORBIDDEN_XML = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class InvalidFeed(ValueError):
    """A feed cannot be safely parsed within the supported limits."""


@dataclass(frozen=True, slots=True)
class ParsedFeedEntry:
    identity_method: str
    identity_key: str
    reported_guid: str | None = field(repr=False)
    reported_url: str | None = field(repr=False)
    reported_title: str | None = field(repr=False)
    reported_summary: str | None = field(repr=False)
    reported_author: str | None = field(repr=False)
    reported_published_at: datetime | None
    reported_updated_at: datetime | None
    metadata_hash: str


def _text(element: ET.Element, names: tuple[str, ...]) -> str | None:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] in names:
            parts: list[str] = []
            remaining = MAX_FIELD_LENGTH
            for part in child.itertext():
                if len(part) > remaining:
                    return None
                parts.append(part[:remaining])
                remaining -= len(part)
            value = " ".join("".join(parts).split())
            if value:
                return value
    return None


def _date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except TypeError, ValueError, IndexError:
            return None
    return result.astimezone(UTC) if result.tzinfo else None


def _url(element: ET.Element, *, atom: bool) -> str | None:
    if atom:
        for child in element:
            if (
                child.tag.rsplit("}", 1)[-1] == "link"
                and child.get("rel", "alternate") == "alternate"
            ):
                candidate = child.get("href")
                if candidate and len(candidate) <= MAX_FIELD_LENGTH:
                    return candidate
        return None
    return _text(element, ("link",))


def _entry(element: ET.Element, *, atom: bool) -> ParsedFeedEntry | None:
    guid = _text(element, ("id",) if atom else ("guid",))
    url = _url(element, atom=atom)
    title = _text(element, ("title",))
    summary = _text(element, ("summary", "content") if atom else ("description",))
    author = _text(element, ("name", "author", "creator"))
    published = _date(_text(element, ("published",) if atom else ("pubDate", "date")))
    updated = _date(_text(element, ("updated",)))
    normalized_url = None
    if url:
        try:
            normalized_url = identify_url(url).normalized_url
        except InvalidUrl:
            url = None
    if guid:
        method, identity = "guid", guid
    elif normalized_url:
        method, identity = "url", normalized_url
    elif title:
        method, identity = (
            "fingerprint",
            "\x1f".join((title, published.isoformat() if published else "")),
        )
    else:
        return None
    values = (
        guid,
        url,
        title,
        summary,
        author,
        published.isoformat() if published else None,
        updated.isoformat() if updated else None,
    )
    metadata = "\x1f".join(value or "" for value in values)
    return ParsedFeedEntry(
        identity_method=method,
        identity_key=hashlib.sha256(identity.encode()).hexdigest(),
        reported_guid=guid,
        reported_url=url,
        reported_title=title,
        reported_summary=summary,
        reported_author=author,
        reported_published_at=published,
        reported_updated_at=updated,
        metadata_hash=hashlib.sha256(metadata.encode()).hexdigest(),
    )


def parse_feed(body: bytes) -> tuple[ParsedFeedEntry, ...]:
    """Parse supported feed entries without resolving entities or fetching URLs."""

    if not body or len(body) > MAX_FEED_BYTES:
        raise InvalidFeed("feed body is empty or exceeds the size limit")
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise InvalidFeed("feed must be UTF-8 XML") from error
    if _FORBIDDEN_XML.search(decoded):
        raise InvalidFeed("feed contains a document type or entity declaration")
    try:
        root = safe_fromstring(body)
    except (ET.ParseError, DefusedXmlException) as error:
        raise InvalidFeed("feed is not well-formed XML") from error
    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > MAX_ELEMENTS or depth > 32:
            raise InvalidFeed("feed XML exceeds structure limits")
        stack.extend((child, depth + 1) for child in element)
    root_name = root.tag.rsplit("}", 1)[-1]
    if root_name not in {"rss", "RDF", "feed"}:
        raise InvalidFeed("unsupported feed format")
    atom = root_name == "feed"
    name = "entry" if atom else "item"
    elements = [child for child in root.iter() if child.tag.rsplit("}", 1)[-1] == name]
    if len(elements) > MAX_ENTRIES:
        raise InvalidFeed("feed has too many entries")
    return tuple(parsed for child in elements if (parsed := _entry(child, atom=atom)) is not None)
