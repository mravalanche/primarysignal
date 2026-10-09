"""Synthetic render checks for the unmounted editorial screen."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from primary_signal.web.common.templates import TemplateSurface, create_templates


def _url_for(name: str, path: str) -> str:
    return f"/assets/{name}/{path.lstrip('/')}"


def _context() -> dict[str, Any]:
    timestamp = datetime(2026, 10, 9, 10, 12, tzinfo=UTC)
    story = {
        "slug": "identity-gateway",
        "story_type": "Advisory",
        "topic": "Security engineering",
        "headline": "Identity gateway receives session fix",
        "synthesis": "A vendor has updated its gateway after a session validation fault was reported.",
        "why_it_matters": "Operators can compare deployed versions with the advisory.",
        "changes": "Affected versions and update advice have changed.",
        "state": "ready",
        "state_label": "Ready",
        "candidate_number": 2,
        "current_number": 1,
    }
    return {
        "url_for": _url_for,
        "inventory_url": "/admin/stories",
        "filters": {"q": "", "state": "", "topic": "", "source": ""},
        "state_options": (("ready", "Ready"), ("published", "Published")),
        "topic_options": (("security-engineering", "Security engineering"),),
        "source_options": (("vendor", "Vendor"),),
        "stories": (
            {
                "slug": story["slug"],
                "url": "/admin/stories/identity-gateway",
                "headline": story["headline"],
                "topic": story["topic"],
                "state": story["state"],
                "state_label": story["state_label"],
                "updated_at": timestamp,
                "updated_label": "10:12 UTC",
                "revision_number": 2,
            },
        ),
        "older_url": None,
        "story": story,
        "public_url": "https://public.example/stories/identity-gateway",
        "sources": (
            {
                "publisher": "Vendor",
                "role": "Primary advisory",
                "title": "Gateway session update",
                "public_url": "https://public.example/advisory",
                "source_url": "https://public.example/advisory-original",
                "supports": "Affected versions and fix.",
                "article_id": "article-example-1",
                "content_version_id": "version-example-1",
                "published_at": timestamp,
                "published_label": "09:20 UTC",
            },
        ),
        "all_sources_url": None,
        "publication_state": {"kind": "ready", "message": "Ready to publish.", "public_url": None},
        "checks": ({"passed": True, "label": "Required copy is present"},),
        "validation": {
            "fingerprint": "synthetic-fingerprint",
            "validated_at": timestamp,
            "validated_label": "10:12 UTC",
            "conflicts": "None",
        },
    }


def _render(context: dict[str, Any]) -> str:
    return (
        create_templates(TemplateSurface.ADMIN)
        .env.get_template("reading_desk.html")
        .render(**context)
    )


def test_reading_desk_renders_inventory_evidence_and_gated_actions() -> None:
    html = _render(_context())

    assert html.count("<h1") == 1
    assert 'aria-current="page"' in html
    assert 'method="get" action="/admin/stories"' in html
    assert "Candidate revision 2" in html
    assert "Current public revision" in html
    assert "Gateway session update" in html
    assert "Content version" in html
    assert "https://public.example/advisory-original" in html
    assert "synthetic-fingerprint" in html
    assert "Publish revision 2</button>" in html
    assert 'type="button" disabled' in html
    assert 'name="reason"' not in html
    assert "raw body" not in html.lower()


def test_reading_desk_empty_and_failure_states_are_explicit_and_escaped() -> None:
    context = _context()
    context["stories"] = ()
    context["story"] = None
    html = _render(context)
    assert "No matching stories" in html
    assert "Select a story" in html

    context = _context()
    context["publication_state"] = {
        "kind": "partial_failure",
        "message": "Publication failed; public revision 1 remains live. <script>alert(1)</script>",
        "public_url": None,
    }
    html = _render(context)
    assert "public revision 1 remains live" in html
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_reading_desk_styles_have_responsive_and_focus_foundations() -> None:
    stylesheet = Path("frontend/styles/admin.css").read_text()

    assert ".ps-desk-grid" in stylesheet
    assert "@media (max-width: 48rem)" in stylesheet
    assert ".ps-desk-story:focus-visible" in stylesheet
