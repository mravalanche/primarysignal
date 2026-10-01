"""Immutable presentation records shared by HTML surfaces."""

from dataclasses import dataclass

from primary_signal.publication import PublicSignalKind


@dataclass(frozen=True, slots=True)
class TagView:
    """A compact, descriptive story tag."""

    label: str


@dataclass(frozen=True, slots=True)
class SignalView:
    """An evidence-backed signal label; never a composite score."""

    kind: PublicSignalKind
    label: str


@dataclass(frozen=True, slots=True)
class SourceTrailView:
    """Compact public provenance for a story summary."""

    source_count: int
    publisher_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoryRowView:
    """The data needed by the shared story-row component."""

    headline: str
    summary: str
    topic_label: str
    age_label: str
    tags: tuple[TagView, ...]
    signals: tuple[SignalView, ...]
    source_trail: SourceTrailView
    uk_relevant: bool = False


@dataclass(frozen=True, slots=True)
class ComponentCatalogueView:
    """Synthetic states used to exercise the component system."""

    standard_story: StoryRowView
    long_title_story: StoryRowView
