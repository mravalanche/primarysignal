"""Read boundary for the curated public publication projection."""

from typing import Protocol

from primary_signal.publication.models import PublicStory, PublicStoryPage, StoryListQuery


class StoryReader(Protocol):
    """Read only currently published stories from a curated projection.

    Listings use the total order ``latest_material_update_at DESC, slug ASC``.
    Implementations must bind an opaque cursor to the active filters and raise
    :class:`InvalidCursor` when it is malformed or reused across filters.
    """

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        """Return a cursor-addressed page in publication order."""
        ...

    def get_story(self, slug: str) -> PublicStory | None:
        """Return one currently published story, or ``None``."""
        ...


class EmptyStoryReader:
    """Non-production reader used before the persistence adapter exists."""

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        del query
        return PublicStoryPage(items=())

    def get_story(self, slug: str) -> None:
        del slug
        return None
