"""Shared HTML and static-asset integration."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from primary_signal.web.common.templates import (
    COMMON_STATIC_ROOT,
    TemplateSurface,
    surface_static_root,
)


def mount_ui_assets(app: FastAPI, surface: TemplateSurface) -> None:
    """Mount common and surface-specific immutable build output."""

    app.mount(
        "/assets/common",
        StaticFiles(directory=COMMON_STATIC_ROOT),
        name="common-assets",
    )
    app.mount(
        f"/assets/{surface.value}",
        StaticFiles(directory=surface_static_root(surface)),
        name=f"{surface.value}-assets",
    )
