"""Development-only, synthetic preview of the public news desk."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from primary_signal.publication import PublicSignalKind
from primary_signal.web.common.presentation import SourceTrailView, TagView
from primary_signal.web.common.templates import TemplateSurface, create_templates

router = APIRouter(include_in_schema=False)
templates = create_templates(TemplateSurface.PUBLIC)


@dataclass(frozen=True, slots=True)
class PreviewSource:
    """A deliberately fictional source displayed in the provenance trail."""

    id: str
    publisher: str
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class PreviewSignal:
    """A named signal with a plain-language rule and visible evidence."""

    kind: PublicSignalKind
    label: str
    definition: str
    evidence_source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreviewStory:
    """One synthetic story used to evaluate the public interface."""

    slug: str
    headline: str
    summary: str
    topic: str
    topic_label: str
    published_at: datetime
    age_label: str
    tags: tuple[TagView, ...]
    signals: tuple[PreviewSignal, ...]
    sources: tuple[PreviewSource, ...]
    uk_relevant: bool = False

    @property
    def source_trail(self) -> SourceTrailView:
        """Return compact provenance for the shared story-row component."""

        publishers = tuple(dict.fromkeys(source.publisher for source in self.sources))
        return SourceTrailView(len(self.sources), publishers[:2])

    @property
    def source_lookup(self) -> dict[str, PreviewSource]:
        """Index public sources for evidence links in the strict template."""

        return {source.id: source for source in self.sources}


@dataclass(frozen=True, slots=True)
class PreviewFilters:
    """Validated filters reflected by the preview form."""

    topic: str | None
    tag: str | None
    uk: bool


TOPICS = (
    ("vulnerabilities", "Vulnerabilities"),
    ("threats", "Threat activity"),
    ("engineering", "Security engineering"),
    ("policy", "Policy & strategy"),
    ("research", "Research & tools"),
)


def _source(identifier: str, publisher: str, title: str) -> PreviewSource:
    return PreviewSource(
        id=identifier,
        publisher=publisher,
        title=title,
        url=f"https://{publisher.casefold().replace(' ', '-')}.public.example/{identifier}",
    )


def preview_stories() -> tuple[PreviewStory, ...]:
    """Build stable synthetic stories without borrowing live names or reporting."""

    vendor = _source("vendor-identity-advisory", "Northstar Systems", "Identity service advisory")
    centre = _source("coordination-note", "Example Security Centre", "Coordinated response note")
    lab = _source("incident-analysis", "Signal Ridge Labs", "Observed exploitation patterns")
    maintainer = _source("package-notice", "Harbour Project", "Package signing notice")
    registry = _source("registry-guidance", "Example Package Registry", "Publisher guidance")
    policy = _source("resilience-paper", "Public Policy Office", "Resilience consultation paper")
    research = _source("protocol-study", "Cedar Research", "Protocol implementation study")
    community = _source("maintainer-analysis", "Common Stack", "Maintainer impact analysis")

    return (
        PreviewStory(
            slug="identity-service-fix",
            headline="Identity service fix follows targeted exploitation reports",
            summary=(
                "A vendor update closes an authentication path seen in a small set of incidents. "
                "Teams should identify internet-facing installations before applying the fix."
            ),
            topic="vulnerabilities",
            topic_label="Vulnerabilities",
            published_at=datetime(2026, 10, 1, 7, 22, tzinfo=UTC),
            age_label="38 minutes ago",
            tags=(
                TagView("CVE-2026-0123"),
                TagView("Identity"),
                TagView("Cloud"),
                TagView("Access control"),
            ),
            signals=(
                PreviewSignal(
                    PublicSignalKind.ACTIVE_EXPLOITATION,
                    "Active exploitation",
                    "A public source describes observed use against real systems.",
                    ("incident-analysis",),
                ),
                PreviewSignal(
                    PublicSignalKind.ACTIONABLE,
                    "Actionable",
                    "The available guidance gives defenders a concrete next step.",
                    ("vendor-identity-advisory", "coordination-note"),
                ),
            ),
            sources=(vendor, centre, lab),
            uk_relevant=True,
        ),
        PreviewStory(
            slug="package-signing-incident",
            headline="Package maintainers rotate signing material after publishing incident",
            summary=(
                "A short-lived publishing issue affected two releases. Maintainers have replaced "
                "the relevant material and documented which versions need review."
            ),
            topic="engineering",
            topic_label="Security engineering",
            published_at=datetime(2026, 10, 1, 6, 5, tzinfo=UTC),
            age_label="2 hours ago",
            tags=(TagView("Supply chain"), TagView("Packages"), TagView("Signing")),
            signals=(
                PreviewSignal(
                    PublicSignalKind.PRIMARY_SOURCE,
                    "Primary source",
                    "The source trail includes the organisation responsible for the affected project.",
                    ("package-notice",),
                ),
            ),
            sources=(maintainer, registry),
        ),
        PreviewStory(
            slug="resilience-reporting-proposal",
            headline="Consultation proposes a common reporting window for service disruption",
            summary=(
                "The draft focuses on operators of widely used digital services. Responses are "
                "open for six weeks before the proposal moves to technical review."
            ),
            topic="policy",
            topic_label="Policy & strategy",
            published_at=datetime(2026, 9, 30, 21, 40, tzinfo=UTC),
            age_label="10 hours ago",
            tags=(TagView("Resilience"), TagView("Reporting"), TagView("Consultation")),
            signals=(
                PreviewSignal(
                    PublicSignalKind.OFFICIAL_ADVISORY,
                    "Official publication",
                    "The proposal is linked directly from the issuing public body.",
                    ("resilience-paper",),
                ),
            ),
            sources=(policy,),
            uk_relevant=True,
        ),
        PreviewStory(
            slug="protocol-parser-study",
            headline="Study finds inconsistent parser behaviour across common protocol libraries",
            summary=(
                "Researchers compared boundary handling in nine implementations. The work includes "
                "a reproducible test set and cautious mitigation advice for maintainers."
            ),
            topic="research",
            topic_label="Research & tools",
            published_at=datetime(2026, 9, 30, 16, 15, tzinfo=UTC),
            age_label="16 hours ago",
            tags=(TagView("Protocols"), TagView("Parsers"), TagView("Open source")),
            signals=(
                PreviewSignal(
                    PublicSignalKind.DEEP_READ,
                    "Deep read",
                    "The source provides original methods, results and reproducible material.",
                    ("protocol-study",),
                ),
                PreviewSignal(
                    PublicSignalKind.WIDELY_REPORTED,
                    "Multiple perspectives",
                    "Independent coverage adds material maintainer context to the original work.",
                    ("protocol-study", "maintainer-analysis"),
                ),
            ),
            sources=(research, community),
        ),
    )


def _normalise_filter(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().casefold()
    return cleaned or None


def filtered_stories(filters: PreviewFilters) -> tuple[PreviewStory, ...]:
    """Apply composable topic, tag and UK-impact filters."""

    return tuple(
        story
        for story in preview_stories()
        if (filters.topic is None or story.topic == filters.topic)
        and (filters.tag is None or any(tag.label.casefold() == filters.tag for tag in story.tags))
        and (not filters.uk or story.uk_relevant)
    )


@router.get("/__dev/preview", response_class=HTMLResponse)
async def public_preview(
    request: Request,
    topic: Annotated[str | None, Query(max_length=40)] = None,
    tag: Annotated[str | None, Query(max_length=80)] = None,
    uk: bool = False,
) -> HTMLResponse:
    """Render the approved news-desk direction using only synthetic content."""

    known_topics = {value for value, _ in TOPICS}
    normalised_topic = _normalise_filter(topic)
    if normalised_topic not in known_topics:
        normalised_topic = None
    filters = PreviewFilters(
        topic=normalised_topic,
        tag=_normalise_filter(tag),
        uk=uk,
    )
    all_stories = preview_stories()
    stories = filtered_stories(filters)
    available_tags = tuple(
        dict.fromkeys(tag_item.label for story in all_stories for tag_item in story.tags)
    )
    return templates.TemplateResponse(
        request=request,
        name="preview.html",
        context={
            "brief_date": date(2026, 10, 1),
            "filters": filters,
            "stories": stories,
            "topics": TOPICS,
            "available_tags": available_tags,
            "result_count": len(stories),
        },
    )
