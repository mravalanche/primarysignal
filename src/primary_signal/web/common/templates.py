"""Strict Jinja environments and packaged static asset paths."""

from enum import StrEnum
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined, select_autoescape


class TemplateSurface(StrEnum):
    """HTML surfaces with separate template and asset roots."""

    PUBLIC = "public"
    ADMIN = "admin"


WEB_ROOT = Path(__file__).resolve().parent.parent
COMMON_TEMPLATE_ROOT = WEB_ROOT / "common" / "templates"
COMMON_STATIC_ROOT = WEB_ROOT / "common" / "static"


def surface_static_root(surface: TemplateSurface) -> Path:
    """Return the packaged asset root for one web surface."""

    return WEB_ROOT / surface.value / "static"


def create_templates(surface: TemplateSurface) -> Jinja2Templates:
    """Create a strict, autoescaping environment with surface-first lookup."""

    environment = Environment(
        loader=ChoiceLoader(
            (
                FileSystemLoader(WEB_ROOT / surface.value / "templates"),
                FileSystemLoader(COMMON_TEMPLATE_ROOT),
            )
        ),
        autoescape=select_autoescape(enabled_extensions=("html", "xml"), default_for_string=True),
        undefined=StrictUndefined,
        enable_async=False,
    )
    return Jinja2Templates(env=environment)
