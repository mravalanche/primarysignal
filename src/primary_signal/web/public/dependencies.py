"""Dependencies owned by the public web surface."""

from typing import cast

from fastapi import Request

from primary_signal.publication import StoryReader


def get_story_reader(request: Request) -> StoryReader:
    """Return the public-projection reader configured for this application."""

    return cast(StoryReader, request.app.state.story_reader)
