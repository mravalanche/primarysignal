"""Read-only public story API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from primary_signal.publication import (
    InvalidCursor,
    StoryListQuery,
    StoryReader,
    StoryType,
    Topic,
)
from primary_signal.web.public.dependencies import get_story_reader
from primary_signal.web.public.schemas import StoryPageResponse, StoryResponse

router = APIRouter(prefix="/api/v1/stories", tags=["stories"])
ReaderDependency = Annotated[StoryReader, Depends(get_story_reader)]


@router.get(
    "",
    response_model=StoryPageResponse,
    responses={status.HTTP_400_BAD_REQUEST: {"description": "Invalid pagination cursor"}},
)
def list_stories(
    reader: ReaderDependency,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
    topic: Topic | None = None,
    story_type: StoryType | None = None,
    uk_relevant: bool | None = None,
    tag_id: Annotated[
        str | None, Query(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", max_length=160)
    ] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> StoryPageResponse:
    """List currently published stories from the curated public projection."""

    try:
        listing = StoryListQuery(
            limit=limit,
            cursor=cursor,
            topic=topic,
            story_type=story_type,
            uk_relevant=uk_relevant,
            tag_id=tag_id,
            q=q,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid search query",
        ) from error
    try:
        page = reader.list_stories(listing)
    except InvalidCursor as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid pagination cursor",
        ) from error
    return StoryPageResponse.model_validate(page)


@router.get("/{slug}", response_model=StoryResponse)
def get_story(
    slug: Annotated[
        str,
        Path(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=160),
    ],
    reader: ReaderDependency,
) -> StoryResponse:
    """Fetch one currently published story by its stable public slug."""

    story = reader.get_story(slug)
    if story is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Story not found",
        )
    return StoryResponse.model_validate(story)
