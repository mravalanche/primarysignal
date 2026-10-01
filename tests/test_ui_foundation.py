import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from jinja2 import UndefinedError

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.publication import PublicStory, PublicStoryPage, StoryListQuery
from primary_signal.web.common.templates import TemplateSurface, create_templates
from primary_signal.web.public import create_public_app


class SafeTestStoryReader:
    """Explicit non-production data source used to exercise production routing."""

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        del query
        return PublicStoryPage(items=())

    def get_story(self, slug: str) -> PublicStory | None:
        del slug
        return None


def get(app: FastAPI, path: str) -> Response:
    async def request() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.get(path)

    return asyncio.run(request())


def test_component_catalogue_renders_named_signals_and_provenance() -> None:
    app = create_public_app(Settings(environment=RuntimeEnvironment.TEST))

    response = get(app, "/__dev/components")

    assert response.status_code == 200
    assert "Active exploitation" in response.text
    assert "Widely reported" in response.text
    assert "4 sources" in response.text
    assert "UK impact" in response.text
    assert "bookmark" not in response.text.casefold()
    assert "strong signal" not in response.text.casefold()


def test_component_catalogue_is_absent_in_production() -> None:
    app = create_public_app(
        Settings(environment=RuntimeEnvironment.PRODUCTION),
        story_reader=SafeTestStoryReader(),
    )

    assert get(app, "/__dev/components").status_code == 404


def test_assets_are_served_locally() -> None:
    app = create_public_app(Settings(environment=RuntimeEnvironment.TEST))

    assert get(app, "/assets/public/public.css").status_code == 200
    assert get(app, "/assets/common/htmx.min.js").status_code == 200


def test_template_environment_uses_strict_undefined_and_autoescape() -> None:
    environment = create_templates(TemplateSurface.PUBLIC).env

    assert environment.from_string("{{ value }}").render(value="<script>") == "&lt;script&gt;"
    with pytest.raises(UndefinedError):
        environment.from_string("{{ missing }}").render()
