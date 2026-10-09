"""Opaque cursor contract for stable public story pagination."""

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from primary_signal.publication.models import (
    StoryListQuery,
    validate_aware_datetime,
    validate_public_identifier,
)


class InvalidCursor(ValueError):
    """The cursor is malformed or belongs to a different filtered listing."""


@dataclass(frozen=True, slots=True)
class StoryCursor:
    """Last row in the total order: update time descending, slug ascending."""

    latest_material_update_at: datetime
    slug: str


def _filter_values(query: StoryListQuery) -> dict[str, str | bool | None]:
    return {
        "topic": query.topic.value if query.topic is not None else None,
        "story_type": query.story_type.value if query.story_type is not None else None,
        "uk_relevant": query.uk_relevant,
        "tag_id": query.tag_id,
    }


def encode_cursor(position: StoryCursor, query: StoryListQuery) -> str:
    """Encode a position bound to the listing's filters, excluding page size."""

    validate_aware_datetime(position.latest_material_update_at, name="cursor update time")
    validate_public_identifier(position.slug, name="cursor slug", slug=True)
    payload: dict[str, object] = {
        "v": 1,
        "position": {
            "latest_material_update_at": position.latest_material_update_at.isoformat(),
            "slug": position.slug,
        },
        "filters": _filter_values(query),
    }
    encoded = urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return encoded.rstrip(b"=").decode("ascii")


def decode_cursor(value: str, query: StoryListQuery) -> StoryCursor:
    """Decode a cursor and reject malformed or cross-filter reuse."""

    try:
        padding = "=" * (-len(value) % 4)
        raw = urlsafe_b64decode(value + padding)
        payload = cast(dict[str, Any], json.loads(raw))
        if set(payload) != {"v", "position", "filters"} or payload["v"] != 1:
            raise InvalidCursor("unsupported cursor")
        if payload["filters"] != _filter_values(query):
            raise InvalidCursor("cursor does not match the requested filters")
        position = cast(dict[str, Any], payload["position"])
        if set(position) != {"latest_material_update_at", "slug"}:
            raise InvalidCursor("invalid cursor position")
        update_time = datetime.fromisoformat(cast(str, position["latest_material_update_at"]))
        slug = cast(str, position["slug"])
        candidate = StoryCursor(latest_material_update_at=update_time, slug=slug)
        validate_aware_datetime(candidate.latest_material_update_at, name="cursor update time")
        validate_public_identifier(candidate.slug, name="cursor slug", slug=True)
        return candidate
    except InvalidCursor:
        raise
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidCursor("invalid cursor") from error
