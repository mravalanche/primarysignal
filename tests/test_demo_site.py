"""The explicit sample site fills ordinary reading routes without a database."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.web.public import create_public_app
from primary_signal.web.public.demo import DemoStoryReader
from primary_signal.web.public.progress import load_progress


def get(path: str, *, demo: bool = True) -> tuple[int, str]:
    app = create_public_app(
        Settings(environment=RuntimeEnvironment.DEVELOPMENT),
        story_reader=DemoStoryReader() if demo else None,
        demo_mode=demo,
    )

    async def request() -> tuple[int, str]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://public.example"
        ) as client:
            response = await client.get(path)
            return response.status_code, response.text

    return asyncio.run(request())


def test_sample_fills_normal_public_pages_and_marks_content_fictional() -> None:
    status, latest = get("/")
    assert status == 200
    assert "Synthetic demonstration" in latest
    assert "Identity service fix follows targeted exploitation reports" in latest

    status, detail = get("/stories/identity-service-fix")
    assert status == 200
    assert "This story and its source are fictional" in detail
    assert "Sample identity service advisory" in detail

    status, tag = get("/tags/identity")
    assert status == 200
    assert "Identity service fix" in tag
    assert "Package maintainers" not in tag

    status, filtered = get("/?topic=research-and-tools")
    assert status == 200
    assert "Study finds inconsistent parser" in filtered
    assert "Identity service fix" not in filtered


def test_progress_is_demo_only_and_percentages_match_checks() -> None:
    status, page = get("/progress")
    assert status == 200
    progress = load_progress()
    assert f"{progress['completed']}/{progress['total']}" in page
    assert f"{progress['percent']}%" in page
    assert get("/progress", demo=False)[0] == 404


def test_demo_is_rejected_in_production() -> None:
    with pytest.raises(RuntimeError, match="unavailable in production"):
        create_public_app(
            Settings(environment=RuntimeEnvironment.PRODUCTION),
            story_reader=DemoStoryReader(),
            demo_mode=True,
        )
