"""The static tour contains only fictional, navigable public pages."""

from pathlib import Path

import pytest
from scripts.export_pages_tour import DEFAULT_BASE_PATH, export_tour, tour_pages, validate_tour

from primary_signal.config import RuntimeEnvironment, Settings
from primary_signal.web.public import create_public_app


def test_static_tour_export_is_closed_and_navigable(tmp_path: Path) -> None:
    output = tmp_path / "site"
    export_tour(output)

    assert len(tour_pages()) == 8
    index = (output / "index.html").read_text(encoding="utf-8")
    story = (output / "stories/identity-service-fix/index.html").read_text(encoding="utf-8")
    assert f'href="{DEFAULT_BASE_PATH}stories/identity-service-fix/' in index
    assert f'href="{DEFAULT_BASE_PATH}stories/identity-service-fix/#sources"' in index
    assert "data-source-preview" not in index
    assert (
        "data-relative-time" in index
    )  # Exact UTC stays visible; no conversion script is exported.
    assert "Intl.RelativeTimeFormat" not in index
    assert "public.example" not in story
    assert "Sample identity service advisory" in story
    assert (output / "assets/public/brand/favicon-16.png").is_file()
    assert (output / "assets/public/brand/favicon.svg").is_file()
    validate_tour(output)


def test_static_tour_validation_rejects_extra_file(tmp_path: Path) -> None:
    output = tmp_path / "site"
    export_tour(output)
    (output / "private.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ValueError, match="allowlist mismatch"):
        validate_tour(output)


def test_static_tour_validation_rejects_broken_link(tmp_path: Path) -> None:
    output = tmp_path / "site"
    export_tour(output)
    index = output / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            f'href="{DEFAULT_BASE_PATH}progress/"',
            f'href="{DEFAULT_BASE_PATH}missing/"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="broken link"):
        validate_tour(output)


def test_static_tour_requires_fictional_demo_reader() -> None:
    with pytest.raises(RuntimeError, match="requires the synthetic demo"):
        create_public_app(Settings(environment=RuntimeEnvironment.TEST), static_tour=True)
