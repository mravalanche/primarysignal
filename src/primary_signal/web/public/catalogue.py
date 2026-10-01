"""Development-only component catalogue."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from primary_signal.publication import PublicSignalKind
from primary_signal.web.common.presentation import (
    ComponentCatalogueView,
    SignalView,
    SourceTrailView,
    StoryRowView,
    TagView,
)
from primary_signal.web.common.templates import TemplateSurface, create_templates

router = APIRouter(include_in_schema=False)
templates = create_templates(TemplateSurface.PUBLIC)


def catalogue_view() -> ComponentCatalogueView:
    """Build synthetic examples which exercise normal and awkward content."""

    return ComponentCatalogueView(
        standard_story=StoryRowView(
            headline="Vendor fixes identity flaw used in targeted attacks",
            summary=(
                "The update closes an authentication path observed in a small number of "
                "incidents. Administrators should check exposed systems first."
            ),
            topic_label="Vulnerabilities",
            age_label="38 minutes ago",
            tags=(TagView("CVE-2026-0123"), TagView("Identity"), TagView("Cloud")),
            signals=(
                SignalView(PublicSignalKind.ACTIVE_EXPLOITATION, "Active exploitation"),
                SignalView(PublicSignalKind.ACTIONABLE, "Actionable"),
            ),
            source_trail=SourceTrailView(4, ("Vendor advisory", "Public.example")),
            uk_relevant=True,
        ),
        long_title_story=StoryRowView(
            headline=(
                "Maintainers publish a coordinated response to a supply-chain issue "
                "affecting several widely used build tools"
            ),
            summary=(
                "The investigation is continuing. The available evidence identifies affected "
                "versions, but not every downstream package has reported its status."
            ),
            topic_label="Security engineering",
            age_label="3 hours ago",
            tags=(TagView("Supply chain"), TagView("Build systems")),
            signals=(
                SignalView(PublicSignalKind.DEVELOPING, "Developing"),
                SignalView(PublicSignalKind.WIDELY_REPORTED, "Widely reported"),
            ),
            source_trail=SourceTrailView(7, ("Project advisory",)),
        ),
    )


@router.get("/__dev/components", response_class=HTMLResponse)
async def component_catalogue(request: Request) -> HTMLResponse:
    """Render shared components using safe, synthetic content."""

    return templates.TemplateResponse(
        request=request,
        name="catalogue.html",
        context={"catalogue": catalogue_view()},
    )
