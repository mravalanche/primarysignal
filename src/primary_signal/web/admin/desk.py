"""Authenticated, read-only views of the editorial metadata reader."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, cast
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from primary_signal.publication.editorial_reader import (
    EditorialReader,
    EditorialSourceLineage,
    EditorialStoryRow,
)
from primary_signal.publication.models import Topic, validate_public_identifier
from primary_signal.web.common.templates import TemplateSurface, create_templates

router = APIRouter(prefix="/admin/desk", tags=["admin-reading-desk"])
templates = create_templates(TemplateSurface.ADMIN)

STATES = ("draft", "validated", "published", "superseded", "suppressed")
TOPIC_OPTIONS = tuple((topic.value, topic.value.replace("-", " ").title()) for topic in Topic)


@dataclass(frozen=True, slots=True)
class DeskFilters:
    q: str
    state: str
    topic: str
    source: str


def _safe_external_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return value


def _reader(request: Request) -> EditorialReader:
    return cast(EditorialReader, request.app.state.editorial_reader)


def _query_url(base: str, filters: DeskFilters, **extra: str | None) -> str:
    parameters = {
        "q": filters.q or None,
        "state": filters.state or None,
        "topic": filters.topic or None,
        "source": filters.source or None,
        **extra,
    }
    query = urlencode({key: value for key, value in parameters.items() if value is not None})
    return f"{base}?{query}" if query else base


def _state(row: EditorialStoryRow) -> str:
    return "suppressed" if row.suppressed else row.candidate_status or "no-revision"


def _cursor(at: str | None, identifier: str | None) -> tuple[datetime, uuid.UUID] | None:
    if at is None and identifier is None:
        return None
    if at is None or identifier is None:
        raise HTTPException(status_code=400, detail="Invalid story cursor")
    try:
        parsed_at = datetime.fromisoformat(at)
        parsed_id = uuid.UUID(identifier)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid story cursor") from error
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise HTTPException(status_code=400, detail="Invalid story cursor")
    return parsed_at, parsed_id


def _source_view(source: EditorialSourceLineage) -> dict[str, object]:
    return {
        "source": source,
        "safe_url": _safe_external_url(source.public_url),
        "canonical_url": _safe_external_url(source.current_canonical_url),
    }


async def _page(
    request: Request,
    *,
    slug: str | None,
    q: str | None,
    state: str | None,
    topic: str | None,
    source: str | None,
    before_at: str | None,
    before_id: str | None,
    all_sources: bool,
) -> HTMLResponse:
    filters = DeskFilters((q or "").strip(), state or "", topic or "", source or "")
    if filters.state and filters.state not in STATES:
        raise HTTPException(status_code=422, detail="Invalid publication state")
    if filters.topic and filters.topic not in {item.value for item in Topic}:
        raise HTTPException(status_code=422, detail="Invalid topic")
    if filters.source:
        try:
            validate_public_identifier(filters.source, name="source key", slug=True)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid source key") from error
    before = _cursor(before_at, before_id)
    reader = _reader(request)

    try:
        page = await run_in_threadpool(
            reader.list_stories,
            limit=20,
            before=before,
            q=filters.q or None,
            state=filters.state or None,
            topic=filters.topic or None,
            source=filters.source or None,
        )
        detail = await run_in_threadpool(reader.get_story, slug) if slug is not None else None
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid story filter") from error
    if slug is not None and detail is None:
        raise HTTPException(status_code=404, detail="Story not found")
    inventory_url = "/admin/desk"
    stories = tuple(
        {
            "row": row,
            "state": _state(row),
            "url": _query_url(f"/admin/desk/stories/{row.slug}", filters),
        }
        for row in page.items
    )
    older_url = (
        _query_url(
            inventory_url,
            filters,
            before_at=page.next_position[0].isoformat(),
            before_id=str(page.next_position[1]),
        )
        if page.next_position
        else None
    )
    candidate = (
        next(
            (item for item in detail.revisions if item.id == detail.story.candidate_revision_id),
            None,
        )
        if detail
        else None
    )
    visible_sources = (
        detail.candidate_sources
        if detail and all_sources
        else (detail.candidate_sources[:5] if detail else ())
    )
    public_origin = cast(str | None, request.app.state.public_origin)
    public_url = (
        f"{public_origin}/stories/{detail.story.slug}"
        if detail and detail.current_revision and public_origin
        else None
    )
    return templates.TemplateResponse(
        request=request,
        name="reading_desk_live.html",
        context={
            "inventory_url": inventory_url,
            "filters": filters,
            "state_options": tuple((value, value.title()) for value in STATES),
            "topic_options": TOPIC_OPTIONS,
            "stories": stories,
            "older_url": older_url,
            "detail": detail,
            "candidate": candidate,
            "source_views": tuple(_source_view(item) for item in visible_sources),
            "all_sources_url": (
                _query_url(f"/admin/desk/stories/{detail.story.slug}", filters, all_sources="1")
                if detail and len(detail.candidate_sources) > 5 and not all_sources
                else None
            ),
            "public_url": public_url,
            "csrf_token": request.state.admin_session.csrf_secret.hex(),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("", response_class=HTMLResponse)
async def inventory(
    request: Request,
    q: Annotated[str | None, Query(max_length=160)] = None,
    state: Annotated[str | None, Query(max_length=24)] = None,
    topic: Annotated[str | None, Query(max_length=80)] = None,
    source: Annotated[str | None, Query(max_length=128)] = None,
    before_at: Annotated[str | None, Query(max_length=64)] = None,
    before_id: Annotated[str | None, Query(max_length=36)] = None,
) -> HTMLResponse:
    return await _page(
        request,
        slug=None,
        q=q,
        state=state,
        topic=topic,
        source=source,
        before_at=before_at,
        before_id=before_id,
        all_sources=False,
    )


@router.get("/stories/{slug}", response_class=HTMLResponse)
async def story_detail(
    request: Request,
    slug: Annotated[str, Path(max_length=160)],
    q: Annotated[str | None, Query(max_length=160)] = None,
    state: Annotated[str | None, Query(max_length=24)] = None,
    topic: Annotated[str | None, Query(max_length=80)] = None,
    source: Annotated[str | None, Query(max_length=128)] = None,
    all_sources: bool = False,
) -> HTMLResponse:
    return await _page(
        request,
        slug=slug,
        q=q,
        state=state,
        topic=topic,
        source=source,
        before_at=None,
        before_id=None,
        all_sources=all_sources,
    )
