"""Import all mapped classes so migrations see complete metadata."""

from primary_signal.ingestion.models import (
    Article,
    ArticleUrl,
    ContentVersion,
    ContentVersionRetention,
    FeedEntry,
    FeedPollRun,
    FetchAttempt,
)
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.publication.storage import (
    PublicationEvent,
    RevisionSignal,
    RevisionSource,
    RevisionTag,
    SignalEvidence,
    Story,
    StoryRevision,
    Tag,
)
from primary_signal.sources.models import Feed, Source
from primary_signal.web.admin.models import AdminLoginAttempt, AdminSession

__all__ = [
    "AdminLoginAttempt",
    "AdminSession",
    "Article",
    "ArticleUrl",
    "ContentVersion",
    "ContentVersionRetention",
    "Feed",
    "FeedEntry",
    "FeedPollRun",
    "FetchAttempt",
    "Job",
    "JobAttempt",
    "PublicationEvent",
    "RevisionSignal",
    "RevisionSource",
    "RevisionTag",
    "SignalEvidence",
    "Source",
    "Story",
    "StoryRevision",
    "Tag",
]
