from dataclasses import dataclass

from fastapi import FastAPI

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.web.admin import create_admin_app
from primary_signal.web.public import create_public_app


@dataclass(frozen=True, order=True)
class RouteManifestEntry:
    path: str
    methods: tuple[str, ...]
    name: str


def route_manifest(app: FastAPI) -> frozenset[RouteManifestEntry]:
    """Read the framework's generated manifest instead of router internals."""

    http_methods = {"delete", "get", "head", "options", "patch", "post", "put"}
    return frozenset(
        RouteManifestEntry(
            path=path,
            methods=tuple(sorted(method for method in operations if method in http_methods)),
            name=next(
                (
                    operation["operationId"]
                    for method, operation in operations.items()
                    if method in http_methods
                ),
                "",
            ),
        )
        for path, operations in app.openapi()["paths"].items()
    )


def test_public_route_manifest_contains_no_admin_routes() -> None:
    settings = Settings(environment=RuntimeEnvironment.TEST)
    public_routes = route_manifest(create_public_app(settings))
    admin_routes = route_manifest(create_admin_app(settings))
    admin_only_routes = frozenset(
        route for route in admin_routes if route.path.startswith("/admin")
    )

    assert admin_only_routes, "the test must observe at least one admin route"
    assert public_routes.isdisjoint(admin_only_routes)
    assert all(not route.path.startswith("/admin") for route in public_routes)
