import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.web.admin import create_admin_app
from primary_signal.web.public import create_public_app

TEST_SETTINGS = Settings(environment=RuntimeEnvironment.TEST)


def get(app: FastAPI, path: str) -> Response:
    async def request() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.get(path)

    return asyncio.run(request())


def test_public_liveness() -> None:
    app = create_public_app(TEST_SETTINGS)
    response = get(app, "/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "surface": "public"}
    assert get(app, "/admin/health/live").status_code == 404


def test_admin_liveness() -> None:
    app = create_admin_app(TEST_SETTINGS)

    assert get(app, "/health/live").json() == {
        "status": "ok",
        "surface": "admin",
    }
    assert get(app, "/admin/health/live").json() == {"status": "ok"}


def test_api_documentation_is_disabled_by_default() -> None:
    app = create_public_app(TEST_SETTINGS)

    assert get(app, "/docs").status_code == 404
    assert get(app, "/openapi.json").status_code == 404
