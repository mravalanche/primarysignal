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
    "Source",
]
