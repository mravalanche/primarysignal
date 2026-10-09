import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from jinja2 import UndefinedError

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.publication import PublicStory, PublicStoryPage, PublicTag, StoryListQuery
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

    def get_tag(self, tag_id: str) -> PublicTag | None:
        del tag_id
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


def test_public_preview_renders_editorial_structure_and_evidence() -> None:
    app = create_public_app(Settings(environment=RuntimeEnvironment.TEST))

    response = get(app, "/__dev/preview")

    assert response.status_code == 200
    assert response.text.count("<h1") == 1
    assert "Morning brief" in response.text
    assert "Latest reporting" in response.text
    assert "On the desk" in response.text
    assert "<time" in response.text
    assert "Active exploitation" in response.text
    assert "A public source describes observed use" in response.text
    assert 'href="#sources-identity-service-fix"' in response.text
    assert 'data-source-toggle aria-controls="sources-identity-service-fix"' in response.text
    assert 'id="source-incident-analysis"' in response.text
    assert 'href="/__dev/preview?tag=Identity"' in response.text
    assert "3 sources" in response.text
    assert "bookmark" not in response.text.casefold()
    assert "confidence" not in response.text.casefold()


def test_public_preview_filters_compose_and_clear() -> None:
    app = create_public_app(Settings(environment=RuntimeEnvironment.TEST))

    response = get(app, "/__dev/preview?topic=vulnerabilities&tag=Identity&uk=true")

    assert response.status_code == 200
    assert "Identity service fix follows" in response.text
    assert "Package maintainers rotate" not in response.text
    assert 'href="/__dev/preview">Clear</a>' in response.text
    assert 'name="uk" value="true" checked' in response.text
    assert 'name="topic" value="vulnerabilities"' in response.text
    assert 'aria-current="page">Vulnerabilities</a>' in response.text


def test_public_preview_has_an_empty_state_for_valid_unmatched_filters() -> None:
    app = create_public_app(Settings(environment=RuntimeEnvironment.TEST))

    response = get(app, "/__dev/preview?topic=research&tag=Identity&uk=true")

    assert response.status_code == 200
    assert "No stories match these filters" in response.text
    assert "0 stories" in response.text


def test_public_preview_is_absent_in_production() -> None:
    app = create_public_app(
        Settings(environment=RuntimeEnvironment.PRODUCTION),
        story_reader=SafeTestStoryReader(),
    )

    assert get(app, "/__dev/preview").status_code == 404


def test_assets_are_served_locally() -> None:
    app = create_public_app(Settings(environment=RuntimeEnvironment.TEST))

    assert get(app, "/assets/public/public.css").status_code == 200
    assert get(app, "/assets/common/htmx.min.js").status_code == 200


def test_preview_styles_include_responsive_news_desk_markers() -> None:
    stylesheet = Path("src/primary_signal/web/public/static/public.css").read_text()

    assert ".ps-news-desk" in stylesheet
    assert ".ps-topic-scroller" in stylesheet
    assert ".ps-tag-more-mobile" in stylesheet
    assert "prefers-reduced-motion" in stylesheet


def test_template_environment_uses_strict_undefined_and_autoescape() -> None:
    environment = create_templates(TemplateSurface.PUBLIC).env

    assert environment.from_string("{{ value }}").render(value="<script>") == "&lt;script&gt;"
    with pytest.raises(UndefinedError):
        environment.from_string("{{ missing }}").render()
