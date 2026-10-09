"""Import all mapped classes so migrations see complete metadata."""

from primary_signal.ingestion.models import (
    Article,
    ArticleUrl,
    ContentVersion,
    FeedEntry,
    FeedPollRun,
    FetchAttempt,
)
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.publication.storage import (
    RevisionSignal,
    RevisionSource,
    RevisionTag,
    SignalEvidence,
    Story,
    StoryRevision,
    Tag,
)
from primary_signal.sources.models import Feed, Source

__all__ = [
    "Article",
    "ArticleUrl",
    "ContentVersion",
    "Feed",
    "FeedEntry",
    "FeedPollRun",
    "FetchAttempt",
    "Job",
    "JobAttempt",
    "RevisionSignal",
    "RevisionSource",
    "RevisionTag",
    "SignalEvidence",
    "Source",
    "Story",
    "StoryRevision",
    "Tag",
]
