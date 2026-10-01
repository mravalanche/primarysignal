"""Public web application composition."""

from fastapi import FastAPI

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.publication import EmptyStoryReader, StoryReader
from primary_signal.web.common import Surface, create_base_app
from primary_signal.web.common.health import router as health_router
from primary_signal.web.common.templates import TemplateSurface
from primary_signal.web.common.ui import mount_ui_assets
from primary_signal.web.public.catalogue import router as catalogue_router
from primary_signal.web.public.stories import router as stories_router


def create_public_app(
    settings: Settings | None = None,
    *,
    story_reader: StoryReader | None = None,
) -> FastAPI:
    """Create the internet-facing application without administration routes."""

    resolved_settings = settings or Settings()
    if resolved_settings.environment is RuntimeEnvironment.PRODUCTION and (
        story_reader is None or isinstance(story_reader, EmptyStoryReader)
    ):
        raise RuntimeError("the production public application requires a story reader")
    app = create_base_app(settings=resolved_settings, surface=Surface.PUBLIC)
    app.state.story_reader = story_reader if story_reader is not None else EmptyStoryReader()
    mount_ui_assets(app, TemplateSurface.PUBLIC)
    app.include_router(health_router)
    app.include_router(stories_router)
    if resolved_settings.environment is not RuntimeEnvironment.PRODUCTION:
        app.include_router(catalogue_router)
    return app
