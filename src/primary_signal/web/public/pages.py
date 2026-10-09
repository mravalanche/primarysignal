"""Server-rendered public reading pages backed by the published projection."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from functools import partial
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse

from primary_signal.publication import (
    InvalidCursor,
    PublicSignalKind,
    PublicStorySummary,
    PublicTag,
    StoryListQuery,
    StoryReader,
    StoryType,
    Topic,
)
from primary_signal.web.common.templates import TemplateSurface, create_templates
from primary_signal.web.public.dependencies import get_story_reader

router = APIRouter(include_in_schema=False)
templates = create_templates(TemplateSurface.PUBLIC)
ReaderDependency = Annotated[StoryReader, Depends(get_story_reader)]


@dataclass(frozen=True, slots=True)
class DayGroup:
    day: date
    stories: tuple[PublicStorySummary, ...]


def group_by_update_day(stories: tuple[PublicStorySummary, ...]) -> tuple[DayGroup, ...]:
    """Keep reader ordering while adding headings for contiguous UTC days."""

    groups: list[DayGroup] = []
    for story in stories:
        day = story.latest_material_update_at.astimezone(UTC).date()
        if groups and groups[-1].day == day:
            previous = groups[-1]
            groups[-1] = DayGroup(day, (*previous.stories, story))
        else:
            groups.append(DayGroup(day, (story,)))
    return tuple(groups)


TOPIC_LABELS = {
    Topic.VULNERABILITIES_AND_EXPLOITATION: "Vulnerabilities",
    Topic.THREAT_ACTIVITY_AND_INCIDENTS: "Threat activity",
    Topic.SECURITY_ENGINEERING: "Security engineering",
    Topic.POLICY_AND_STRATEGY: "Policy & strategy",
    Topic.RESEARCH_AND_TOOLS: "Research",
}
TYPE_LABELS = {
    StoryType.NEWS: "News",
    StoryType.RESEARCH: "Research",
    StoryType.ADVISORY: "Advisory",
    StoryType.INCIDENT: "Incident",
    StoryType.ANALYSIS: "Analysis",
    StoryType.OPINION: "Opinion",
    StoryType.TOOL_RELEASE: "Tool release",
}
SIGNAL_DETAILS = {
    PublicSignalKind.PRIMARY_SOURCE: (
        "Primary source",
        "The source trail includes an original publication, research, or artefact directly relevant to this story.",
    ),
    PublicSignalKind.OFFICIAL_ADVISORY: (
        "Official advisory",
        "An official advisory or notice supports this report.",
    ),
    PublicSignalKind.ACTIVE_EXPLOITATION: (
        "Active exploitation",
        "A public source describes exploitation observed against real systems.",
    ),
    PublicSignalKind.EXPLOIT_AVAILABLE: (
        "Exploit available",
        "A public source documents available exploit material.",
    ),
    PublicSignalKind.ACTIONABLE: (
        "Actionable",
        "Published guidance gives defenders a concrete next step.",
    ),
    PublicSignalKind.CONFIRMED_INCIDENT: (
        "Confirmed incident",
        "A public source confirms that an incident occurred.",
    ),
    PublicSignalKind.DEVELOPING: (
        "Developing",
        "Material details remain subject to follow-up reporting.",
    ),
    PublicSignalKind.WIDELY_REPORTED: (
        "Widely reported",
        "Multiple independent public sources cover this story.",
    ),
    PublicSignalKind.DEEP_READ: (
        "Deep read",
        "The source includes substantial original methods or analysis.",
    ),
}


def utc_display(value: datetime) -> str:
    """Give a stable, human-readable timestamp when scripting is unavailable."""

    return value.astimezone(UTC).strftime("%-d %b %Y, %H:%M UTC")


def latest_url(
    *,
    cursor: str | None = None,
    topic: Topic | None = None,
    story_type: StoryType | None = None,
    uk_relevant: bool = False,
    tag_id: str | None = None,
) -> str:
    parameters: dict[str, str] = {}
    if topic is not None:
        parameters["topic"] = topic.value
    if story_type is not None:
        parameters["story_type"] = story_type.value
    if uk_relevant:
        parameters["uk_relevant"] = "true"
    if cursor is not None:
        parameters["cursor"] = cursor
    path = f"/tags/{tag_id}" if tag_id is not None else "/"
    return path + ("?" + urlencode(parameters) if parameters else "")


def _context(*, tag: PublicTag | None = None, **extra: object) -> dict[str, object]:
    return {
        "topics": TOPIC_LABELS,
        "story_types": TYPE_LABELS,
        "signal_details": SIGNAL_DETAILS,
        "utc_display": utc_display,
        "latest_url": partial(latest_url, tag_id=tag.id if tag is not None else None),
        "tag": tag,
        **extra,
    }


def _optional_filter[EnumFilter: StrEnum](
    value: str | None, enum_type: type[EnumFilter]
) -> EnumFilter | None:
    """Treat a blank select as All while rejecting unsupported nonblank values."""

    if value is None or value == "":
        return None
    try:
        return enum_type(value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid filter value") from error


@router.get("/", response_class=HTMLResponse)
def latest_page(
    request: Request,
    reader: ReaderDependency,
    cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
    topic: Annotated[str | None, Query(max_length=80)] = None,
    story_type: Annotated[str | None, Query(max_length=80)] = None,
    uk_relevant: bool = False,
) -> HTMLResponse:
    """Render published stories with linkable, cursor-bound filters."""

    return _listing_page(request, reader, cursor, topic, story_type, uk_relevant)


@router.get("/tags/{tag_id}", response_class=HTMLResponse)
def tag_page(
    request: Request,
    tag_id: Annotated[str, Path(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", max_length=160)],
    reader: ReaderDependency,
    cursor: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
    topic: Annotated[str | None, Query(max_length=80)] = None,
    story_type: Annotated[str | None, Query(max_length=80)] = None,
    uk_relevant: bool = False,
) -> HTMLResponse:
    """Show one canonical tag and its currently published stories."""

    tag = reader.get_tag(tag_id)
    if tag is None:
        return templates.TemplateResponse(
            request=request,
            name="tag_not_found.html",
            status_code=404,
            context={},
        )
    return _listing_page(request, reader, cursor, topic, story_type, uk_relevant, tag=tag)


def _listing_page(
    request: Request,
    reader: StoryReader,
    cursor: str | None,
    topic: str | None,
    story_type: str | None,
    uk_relevant: bool,
    *,
    tag: PublicTag | None = None,
) -> HTMLResponse:
    selected_topic = _optional_filter(topic, Topic)
    selected_type = _optional_filter(story_type, StoryType)
    filters = {
        "topic": selected_topic,
        "story_type": selected_type,
        "uk_relevant": uk_relevant,
    }
    try:
        page = reader.list_stories(
            StoryListQuery(
                limit=20,
                cursor=cursor,
                topic=selected_topic,
                story_type=selected_type,
                uk_relevant=True if uk_relevant else None,
                tag_id=tag.id if tag is not None else None,
            )
        )
    except InvalidCursor:
        return templates.TemplateResponse(
            request=request,
            name="latest.html",
            status_code=400,
            context=_context(
                tag=tag,
                groups=(),
                next_url=None,
                filters=filters,
                error="This page link has expired or does not match the current filters.",
            ),
        )
    return templates.TemplateResponse(
        request=request,
        name="latest.html",
        context=_context(
            tag=tag,
            groups=group_by_update_day(page.items),
            next_url=(
                latest_url(
                    cursor=page.next_cursor,
                    topic=selected_topic,
                    story_type=selected_type,
                    uk_relevant=uk_relevant,
                    tag_id=tag.id if tag is not None else None,
                )
                if page.next_cursor
                else None
            ),
            filters=filters,
            error=None,
        ),
    )


@router.get("/stories/{slug}", response_class=HTMLResponse)
def story_page(
    request: Request,
    slug: Annotated[str, Path(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=160)],
    reader: ReaderDependency,
) -> HTMLResponse:
    """Show one published story and its complete public source trail."""

    story = reader.get_story(slug)
    if story is None:
        return templates.TemplateResponse(
            request=request,
            name="story_not_found.html",
            status_code=404,
            context={},
        )
    return templates.TemplateResponse(
        request=request,
        name="story_detail.html",
        context=_context(
            story=story, source_lookup={source.id: source for source in story.sources}
        ),
    )


@router.get("/stories/{slug}/sources-preview", response_class=HTMLResponse)
def source_preview(
    request: Request,
    slug: Annotated[str, Path(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=160)],
    reader: ReaderDependency,
) -> HTMLResponse:
    """Render a bounded source fragment only when a reader asks for it."""

    story = reader.get_story(slug)
    if story is None:
        return HTMLResponse("Story not found", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="source_preview.html",
        context={"story": story, "preview_sources": story.sources[:3]},
    )
