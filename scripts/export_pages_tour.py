"""Export an allowlisted fictional reading tour for GitHub Pages.

The export uses the ordinary public Jinja pages with a static-tour context. It
never copies a site tree or a runtime database into the publication artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from httpx import ASGITransport, AsyncClient

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.web.common.templates import TemplateSurface, surface_static_root
from primary_signal.web.public import create_public_app
from primary_signal.web.public.demo import DEMO_STORIES, DemoStoryReader

DEFAULT_BASE_PATH = "/primarysignal/"
ASSETS = (
    "public.css",
    "static-tour.js",
    "brand/favicon.svg",
    "brand/favicon-light.svg",
    "brand/favicon-dark.svg",
    "brand/favicon-16.png",
    "brand/favicon-32.png",
    "brand/favicon-48.png",
    "brand/apple-touch-icon.png",
)
URL_ATTRIBUTES = frozenset({"href", "src", "action", "data-light-icon", "data-dark-icon"})
URL_PATTERN = re.compile(
    r"(?P<name>\b(?:href|src|action|data-light-icon|data-dark-icon))="
    r'(?P<quote>["\'])(?P<value>.*?)(?P=quote)',
    re.DOTALL,
)
FORBIDDEN_TEXT = ("/__dev", "/admin", "/api/", "public.example", "REPLACE_ME", ".env.")


def tour_pages() -> dict[str, str]:
    """Return only explicitly approved public routes and their output files."""

    pages = {"/": "index.html", "/progress": "progress/index.html"}
    for story in DEMO_STORIES:
        pages[f"/stories/{story.slug}"] = f"stories/{story.slug}/index.html"
        for tag in story.tags:
            pages[f"/tags/{tag.id}"] = f"tags/{tag.id}/index.html"
    return pages


def _rewrite_url(value: str, *, base_path: str, pages: dict[str, str]) -> str:
    if value.startswith("#"):
        return value
    parsed = urlsplit(value)
    if (parsed.scheme or parsed.netloc) and (parsed.scheme, parsed.netloc) != (
        "http",
        "testserver",
    ):
        raise ValueError(f"external URL in static tour: {value}")
    path = unquote(parsed.path)
    if path in pages:
        static_path = "" if path == "/" else path.lstrip("/") + "/"
    elif path.startswith("/assets/public/") and path.removeprefix("/assets/public/") in ASSETS:
        static_path = path.lstrip("/")
    else:
        raise ValueError(f"unapproved URL in static tour: {value}")
    if parsed.query and path not in (
        "/",
        *(f"/tags/{tag.id}" for story in DEMO_STORIES for tag in story.tags),
    ):
        raise ValueError(f"unexpected query in static tour: {value}")
    return urlunsplit(("", "", base_path + static_path, parsed.query, parsed.fragment))


def _rewrite_html(html: str, *, base_path: str, pages: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        value = _rewrite_url(match.group("value"), base_path=base_path, pages=pages)
        quote = match.group("quote")
        return f"{match.group('name')}={quote}{value}{quote}"

    return URL_PATTERN.sub(replace, html)


class _Urls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.urls.extend(value for name, value in attrs if name in URL_ATTRIBUTES and value)


def validate_tour(output: Path, *, base_path: str = DEFAULT_BASE_PATH) -> None:
    """Fail closed on extra files, broken links, or route/data leaks."""

    expected = (
        set(tour_pages().values()) | {f"assets/public/{name}" for name in ASSETS} | {".nojekyll"}
    )
    actual = {str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()}
    if actual != expected:
        raise ValueError(
            f"static tour file allowlist mismatch: extra={actual - expected}, missing={expected - actual}"
        )
    for output_name in sorted(expected):
        path = output / output_name
        if path.suffix not in {".html", ".js", ".css", ".svg"}:
            continue
        content = path.read_text(encoding="utf-8")
        if any(token in content for token in FORBIDDEN_TEXT):
            raise ValueError(f"forbidden route or sample URL in {output_name}")
        if path.suffix != ".html":
            continue
        if "Synthetic demonstration" not in content:
            raise ValueError(f"fictional-data notice missing from {output_name}")
        parser = _Urls()
        parser.feed(content)
        for url in parser.urls:
            if url.startswith("#"):
                continue
            parsed = urlsplit(url)
            if parsed.scheme or parsed.netloc or not parsed.path.startswith(base_path):
                raise ValueError(f"unapproved link in {output_name}: {url}")
            target = unquote(parsed.path[len(base_path) :])
            if ".." in Path(target).parts:
                raise ValueError(f"path traversal in {output_name}: {url}")
            target_file = (
                output / target / "index.html" if not Path(target).suffix else output / target
            )
            if not target_file.is_file():
                raise ValueError(f"broken link in {output_name}: {url}")


async def _render_pages(output: Path, *, base_path: str, pages: dict[str, str]) -> None:
    app = create_public_app(
        Settings(environment=RuntimeEnvironment.TEST),
        story_reader=DemoStoryReader(),
        demo_mode=True,
        static_tour=True,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for route, output_name in pages.items():
            response = await client.get(route)
            response.raise_for_status()
            destination = output / output_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                _rewrite_html(response.text, base_path=base_path, pages=pages), encoding="utf-8"
            )


def export_tour(output: Path, *, base_path: str = DEFAULT_BASE_PATH) -> None:
    if (
        not base_path.startswith("/")
        or not base_path.endswith("/")
        or ".." in Path(base_path).parts
    ):
        raise ValueError("base path must be an absolute directory path")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    pages = tour_pages()
    asyncio.run(_render_pages(output, base_path=base_path, pages=pages))
    for asset in ASSETS:
        destination = output / "assets/public" / asset
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(surface_static_root(TemplateSurface.PUBLIC) / asset, destination)
    (output / ".nojekyll").touch()
    validate_tour(output, base_path=base_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-path", default=DEFAULT_BASE_PATH)
    arguments = parser.parse_args()
    export_tour(arguments.output, base_path=arguments.base_path)
    print(f"Exported and validated static tour: {arguments.output}")
