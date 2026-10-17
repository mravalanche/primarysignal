"""Fixed, fictional publication data for the loopback Docker demonstration."""

from dataclasses import fields
from datetime import UTC, datetime

from primary_signal.publication import (
    InvalidCursor,
    PublicSource,
    PublicStory,
    PublicStoryPage,
    PublicStorySummary,
    PublicTag,
    StoryListQuery,
    StoryType,
    TagKind,
    Topic,
)


def _story(
    slug: str,
    headline: str,
    synthesis: str,
    why: str,
    topic: Topic,
    kind: StoryType,
    hour: int,
    publisher: str,
    source_title: str,
    tag: PublicTag,
) -> PublicStory:
    published = datetime(2026, 10, 1, hour, 0, tzinfo=UTC)
    return PublicStory(
        slug=slug,
        headline=headline,
        synthesis=synthesis,
        why_it_matters=why,
        primary_topic=topic,
        story_type=kind,
        first_reported_at=published,
        latest_material_update_at=published,
        source_count=1,
        tags=(tag,),
        sources=(
            PublicSource(
                id=f"{slug}-source",
                title=source_title,
                publisher=publisher,
                url=f"https://public.example/{slug}",
                first_published_at=published,
                is_primary=True,
            ),
        ),
    )


DEMO_STORIES = (
    _story(
        "identity-service-fix",
        "Identity service fix follows targeted exploitation reports",
        "A fictional vendor update closes an authentication path described in sample reporting.",
        "Teams can see how a concise summary leads to the original source trail.",
        Topic.VULNERABILITIES_AND_EXPLOITATION,
        StoryType.ADVISORY,
        9,
        "Example Vendor",
        "Sample identity service advisory",
        PublicTag("identity", "Identity", TagKind.CURATED),
    ),
    _story(
        "package-signing-incident",
        "Package maintainers rotate signing material after publishing incident",
        "A fictional publishing issue affected two sample releases; maintainers document a response.",
        "The sample shows how incident reporting can link directly to a maintainer notice.",
        Topic.SECURITY_ENGINEERING,
        StoryType.INCIDENT,
        8,
        "Example Project",
        "Sample package signing notice",
        PublicTag("supply-chain", "Supply chain", TagKind.CURATED),
    ),
    _story(
        "protocol-parser-study",
        "Study finds inconsistent parser behaviour across common protocol libraries",
        "Fictional researchers compare boundary handling and publish a sample test set.",
        "The sample demonstrates how original research appears in the reading flow.",
        Topic.RESEARCH_AND_TOOLS,
        StoryType.RESEARCH,
        7,
        "Example Research Lab",
        "Sample protocol study",
        PublicTag("protocols", "Protocols", TagKind.TECHNOLOGY),
    ),
)


class DemoStoryReader:
    """Read-only, in-memory reader for explicit local demo startup."""

    def list_stories(self, query: StoryListQuery) -> PublicStoryPage:
        if query.cursor is not None:
            raise InvalidCursor("the sample publication has one page")
        stories = tuple(
            story
            for story in DEMO_STORIES
            if (query.topic is None or story.primary_topic is query.topic)
            and (query.story_type is None or story.story_type is query.story_type)
            and (query.uk_relevant is None or story.uk_relevant is query.uk_relevant)
            and (query.tag_id is None or any(tag.id == query.tag_id for tag in story.tags))
            and (
                query.q is None
                or all(
                    term in f"{story.headline} {story.synthesis}".casefold()
                    for term in query.q.split()
                )
            )
        )
        return PublicStoryPage(tuple(self._summary(story) for story in stories[: query.limit]))

    def get_story(self, slug: str) -> PublicStory | None:
        return next((story for story in DEMO_STORIES if story.slug == slug), None)

    def get_tag(self, tag_id: str) -> PublicTag | None:
        return next((tag for story in DEMO_STORIES for tag in story.tags if tag.id == tag_id), None)

    @staticmethod
    def _summary(story: PublicStory) -> PublicStorySummary:
        return PublicStorySummary(
            **{field.name: getattr(story, field.name) for field in fields(PublicStorySummary)}
        )
