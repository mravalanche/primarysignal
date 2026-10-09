"""Article jobs prepare only bounded, fenced database callbacks."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy import Connection, Engine

from primary_signal.ingestion.article_handler import ArticleRetrievalHandler
from primary_signal.ingestion.article_results import ArticleRetrievalDisabled, ArticleTarget
from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.jobs.contracts import RetrieveArticleV1
from primary_signal.jobs.handlers import JobProcessingError
from primary_signal.jobs.repository import FailureDisposition, JobLease, JobRepository
from primary_signal.retrieval.article import ArticleFetchResult
from primary_signal.retrieval.extract import ArticleExtraction


def _lease() -> JobLease:
    return JobLease(
        job_id=uuid.uuid7(),
        attempt_id=uuid.uuid7(),
        attempt_number=1,
        lease_token=uuid.uuid7(),
        worker_id=f"processor:{uuid.uuid4()}",
        lease_expires_at=datetime.now(UTC),
        job_type="articles.retrieve",
        payload_version=1,
        payload=RetrieveArticleV1(article_id=uuid.uuid7(), article_url_id=uuid.uuid7()),
    )


def _target(lease: JobLease) -> ArticleTarget:
    payload = cast(RetrieveArticleV1, lease.payload)
    return ArticleTarget(payload.article_id, payload.article_url_id, "https://public.example/a")


def _result(target: ArticleTarget, *, status: int = 200) -> ArticleFetchResult:
    digest = hashlib.sha256(
        b"primary-signal/article-normalization/v1\0"
        + json.dumps(["Title", "Text."], separators=(",", ":")).encode()
    ).hexdigest()
    return ArticleFetchResult(
        status=status,
        final_url=target.url,
        redirect_chain=(),
        content_type="text/html" if status == 200 else None,
        decoded_byte_count=32 if status == 200 else 0,
        raw_response_hash="a" * 64 if status == 200 else None,
        extraction=ArticleExtraction("Title", "Text.", 1, digest) if status == 200 else None,
        etag=None,
        last_modified=None,
    )


def test_article_success_prepares_result_for_fenced_completion() -> None:
    lease = _lease()
    target = _target(lease)
    fetcher = Mock()
    result = _result(target)
    fetcher.fetch_article.return_value = result
    with patch("primary_signal.ingestion.article_handler.ArticleRetrievalRepository") as repository:
        repository.return_value.load_target.return_value = target
        callback = ArticleRetrievalHandler(cast(Engine, MagicMock()), fetcher)(lease)
        assert callback is not None
        fetcher.fetch_article.assert_called_once_with(target.url)
        repository.return_value.record_result.assert_not_called()
        callback(cast(Connection, Mock()), cast(JobRepository, Mock()))
        arguments = repository.return_value.record_result.call_args.kwargs
        assert arguments["target"] == target
        assert arguments["job_id"] == lease.job_id
        assert arguments["result"] is result
        assert arguments["started_at"] <= arguments["completed_at"]


@pytest.mark.parametrize("status", [200, 304])
def test_article_failure_prepares_safe_attempt(status: int) -> None:
    lease = _lease()
    target = _target(lease)
    fetcher = Mock()
    if status == 200:
        fetcher.fetch_article.side_effect = FeedFetchError("dependency_timeout")
        code = "dependency_timeout"
    else:
        fetcher.fetch_article.return_value = _result(target, status=304)
        code = "invalid_response"
    with patch("primary_signal.ingestion.article_handler.ArticleRetrievalRepository") as repository:
        repository.return_value.load_target.return_value = target
        with pytest.raises(JobProcessingError) as raised:
            ArticleRetrievalHandler(cast(Engine, MagicMock()), fetcher)(lease)
        assert raised.value.failure.code == code
        assert raised.value.on_failure is not None
        raised.value.on_failure(
            cast(Connection, Mock()),
            cast(JobRepository, Mock()),
            FailureDisposition(status="dead", retry_at=None),
        )
        arguments = repository.return_value.record_failure.call_args.kwargs
        assert arguments["target"] == target
        assert arguments["error_code"] == code
        assert arguments["started_at"] <= arguments["completed_at"]


def test_disabled_article_skips_fetch() -> None:
    fetcher = Mock()
    with patch("primary_signal.ingestion.article_handler.ArticleRetrievalRepository") as repository:
        repository.return_value.load_target.side_effect = ArticleRetrievalDisabled()
        assert ArticleRetrievalHandler(cast(Engine, MagicMock()), fetcher)(_lease()) is None
        fetcher.fetch_article.assert_not_called()


def test_invalid_extraction_becomes_a_safe_failed_attempt() -> None:
    lease = _lease()
    target = _target(lease)
    fetcher = Mock()
    valid = _result(target)
    fetcher.fetch_article.return_value = ArticleFetchResult(
        status=200,
        final_url=valid.final_url,
        redirect_chain=(),
        content_type=valid.content_type,
        decoded_byte_count=valid.decoded_byte_count,
        raw_response_hash=valid.raw_response_hash,
        extraction=ArticleExtraction("Title", "Text.", 1, "b" * 64),
        etag=None,
        last_modified=None,
    )
    with patch("primary_signal.ingestion.article_handler.ArticleRetrievalRepository") as repository:
        repository.return_value.load_target.return_value = target
        with pytest.raises(JobProcessingError) as raised:
            ArticleRetrievalHandler(cast(Engine, MagicMock()), fetcher)(lease)
        assert raised.value.failure.code == "retriever_protocol"
        assert raised.value.on_failure is not None
        repository.return_value.record_result.assert_not_called()
