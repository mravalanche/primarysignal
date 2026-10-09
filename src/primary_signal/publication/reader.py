"""Read boundary for the curated public publication projection."""

from typing import Protocol

from primary_signal.publication.models import (
    PublicStory,
    PublicStoryPage,
    PublicTag,
    StoryListQuery,
)


class StoryReader(Protocol):
    """Read only currently published stories from a curated projection.

    Listings use ``latest_material_update_at DESC, slug ASC``. Keyword search
    uses relevance descending before that same deterministic tie-break.
    Implementations must bind an opaque cursor to the active filters and raise
    :class:`InvalidCursor` when it is malformed or reused across filters.
    """

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        """Return a cursor-addressed page in publication order."""
        ...

    def get_story(self, slug: str) -> PublicStory | None:
        """Return one currently published story, or ``None``."""
        ...

    def get_tag(self, tag_id: str) -> PublicTag | None:
        """Return a canonical tag visible on published stories, or ``None``."""
        ...


class EmptyStoryReader:
    """Non-production reader used before the persistence adapter exists."""

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        del query
        return PublicStoryPage(items=())

    def get_story(self, slug: str) -> None:
        del slug
        return None

    def get_tag(self, tag_id: str) -> None:
        del tag_id
        return None
