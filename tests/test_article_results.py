"""Synthetic decisions for article content persistence."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection
from sqlalchemy.dialects import postgresql

from primary_signal.ingestion.article_results import (
    ArticleRetrievalDisabled,
    ArticleRetrievalRepository,
    ArticleTarget,
    validate_article_result,
)
from primary_signal.retrieval.article import ArticleFetchResult, RedirectHop
from primary_signal.retrieval.extract import ArticleExtraction

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
ARTICLE_ID = uuid.uuid7()
URL_ID = uuid.uuid7()
SOURCE_ID = uuid.uuid7()
URL = "https://public.example/story"
TARGET = ArticleTarget(ARTICLE_ID, URL_ID, URL)


class _Result:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> Any:
        return self.value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalar_one(self) -> Any:
        return self.value


def _connection(*results: object) -> MagicMock:
    connection = MagicMock(spec=Connection)
    connection.execute.side_effect = [_Result(result) for result in results]
    return connection


def _result(
    *,
    hash_override: str | None = None,
    count_override: int | None = None,
    text_override: str | None = None,
) -> ArticleFetchResult:
    title = "Notice"
    text = text_override if text_override is not None else "Synthetic article text."
    canonical = json.dumps([title, text], ensure_ascii=False, separators=(",", ":")).encode()
    digest = hashlib.sha256(b"primary-signal/article-normalization/v1\0" + canonical).hexdigest()
    return ArticleFetchResult(
        status=200,
        final_url="https://public.example/final",
        redirect_chain=(RedirectHop(status=302, url="https://public.example/final"),),
        content_type="text/html; charset=utf-8",
        decoded_byte_count=100,
        raw_response_hash="a" * 64,
        extraction=ArticleExtraction(title, text, count_override or 3, hash_override or digest),
        etag='"tag"',
        last_modified=None,
    )


def _current() -> tuple[object, ...]:
    return (
        SOURCE_ID,
        True,
        {"source_id": SOURCE_ID, "current_canonical_url_id": URL_ID},
        URL,
    )


def _statements(connection: MagicMock) -> list[str]:
    return [
        str(call.args[0].compile(dialect=postgresql.dialect()))
        for call in connection.execute.call_args_list
    ]


def test_load_target_requires_enabled_source_and_current_selected_url() -> None:
    connection = _connection(
        {"normalized_url": URL, "current_canonical_url_id": URL_ID, "enabled": True}
    )
    assert (
        ArticleRetrievalRepository(cast(Connection, connection)).load_target(ARTICLE_ID, URL_ID)
        == TARGET
    )
    for unavailable in (
        None,
        {"normalized_url": URL, "current_canonical_url_id": uuid.uuid7(), "enabled": True},
        {"normalized_url": URL, "current_canonical_url_id": URL_ID, "enabled": False},
    ):
        with pytest.raises(ArticleRetrievalDisabled):
            ArticleRetrievalRepository(cast(Connection, _connection(unavailable))).load_target(
                ARTICLE_ID, URL_ID
            )


def test_new_result_creates_terminal_attempt_and_content_pointer() -> None:
    version_id = uuid.uuid7()
    connection = _connection(*_current(), None, version_id, None, None)
    summary = ArticleRetrievalRepository(cast(Connection, connection)).record_result(
        target=TARGET,
        job_id=uuid.uuid7(),
        result=_result(),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )
    assert (summary.status, summary.stored, summary.content_version_id) == (
        "fetched",
        True,
        version_id,
    )
    statements = _statements(connection)
    assert len(statements) == 8
    assert "FOR SHARE" not in statements[1]
    assert "FOR UPDATE" in statements[2]
    assert "INSERT INTO primary_signal.fetch_attempts" in statements[4]
    assert "INSERT INTO primary_signal.content_versions" in statements[5]
    assert (
        "ON CONFLICT (article_id, normalization_version, normalized_content_hash) DO NOTHING"
        in statements[5]
    )
    assert "UPDATE primary_signal.fetch_attempts" in statements[6]
    assert "UPDATE primary_signal.articles" in statements[7]
    attempt_values = (
        connection.execute.call_args_list[4].args[0].compile(dialect=postgresql.dialect()).params
    )
    assert attempt_values["requested_url"] == URL
    assert attempt_values["final_url"] == "https://public.example/final"
    assert attempt_values["redirect_chain"] == [
        {"status": 302, "url": "https://public.example/final"}
    ]
    assert "raw_html" not in str(attempt_values)


def test_duplicate_content_reuses_version_and_records_new_attempt() -> None:
    existing_id = uuid.uuid7()
    connection = _connection(*_current(), None, None, existing_id, None, None)
    summary = ArticleRetrievalRepository(cast(Connection, connection)).record_result(
        target=TARGET,
        job_id=uuid.uuid7(),
        result=_result(),
        started_at=NOW,
        completed_at=NOW,
    )
    assert summary.status == "fetched"
    assert not summary.stored
    assert summary.content_version_id == existing_id
    assert "SELECT primary_signal.content_versions.id" in _statements(connection)[6]


def test_changed_or_disabled_target_is_skipped_without_writes() -> None:
    cases = (
        (SOURCE_ID, False),
        (SOURCE_ID, True, {"source_id": SOURCE_ID, "current_canonical_url_id": uuid.uuid7()}),
        (
            SOURCE_ID,
            True,
            {"source_id": SOURCE_ID, "current_canonical_url_id": URL_ID},
            "https://public.example/changed",
        ),
    )
    for case in cases:
        connection = _connection(*case)
        summary = ArticleRetrievalRepository(cast(Connection, connection)).record_result(
            target=TARGET,
            job_id=uuid.uuid7(),
            result=_result(),
            started_at=NOW,
            completed_at=NOW,
        )
        assert summary.status == "skipped"
        assert not summary.stored
        assert all("INSERT" not in statement for statement in _statements(connection))


def test_invalid_metadata_or_304_rejected_before_database_reads() -> None:
    for result in (
        _result(hash_override="b" * 64),
        _result(count_override=99),
        _result(text_override="Synthetic\x00article text."),
        ArticleFetchResult(
            status=304,
            final_url=URL,
            redirect_chain=(),
            content_type=None,
            decoded_byte_count=0,
            raw_response_hash=None,
            extraction=None,
            etag=None,
            last_modified=None,
        ),
    ):
        with pytest.raises(ValueError):
            validate_article_result(result)
        connection = _connection()
        with pytest.raises(ValueError):
            ArticleRetrievalRepository(cast(Connection, connection)).record_result(
                target=TARGET,
                job_id=uuid.uuid7(),
                result=result,
                started_at=NOW,
                completed_at=NOW,
            )
        connection.execute.assert_not_called()

    validate_article_result(_result())


def test_failure_records_safe_code_in_terminal_attempt() -> None:
    connection = _connection(*_current(), None, None)
    ArticleRetrievalRepository(cast(Connection, connection)).record_failure(
        target=TARGET,
        job_id=uuid.uuid7(),
        error_code="dependency_timeout",
        started_at=NOW,
        completed_at=NOW,
    )
    statements = _statements(connection)
    assert len(statements) == 6
    assert "INSERT INTO primary_signal.fetch_attempts" in statements[4]
    assert "UPDATE primary_signal.fetch_attempts" in statements[5]
    assert (
        connection.execute.call_args_list[5]
        .args[0]
        .compile(dialect=postgresql.dialect())
        .params["error_code"]
        == "dependency_timeout"
    )


def test_failure_for_stale_target_does_not_write_attempt() -> None:
    connection = _connection(
        SOURCE_ID, True, {"source_id": SOURCE_ID, "current_canonical_url_id": uuid.uuid7()}
    )
    ArticleRetrievalRepository(cast(Connection, connection)).record_failure(
        target=TARGET,
        job_id=uuid.uuid7(),
        error_code="dependency_timeout",
        started_at=NOW,
        completed_at=NOW,
    )
    assert all("INSERT" not in statement for statement in _statements(connection))


def test_bad_failure_code_and_time_order_rejected_before_database_reads() -> None:
    for code, start, end in (
        ("error with details", NOW, NOW),
        ("dependency_timeout", NOW, NOW - timedelta(seconds=1)),
        ("dependency_timeout", NOW.replace(tzinfo=None), NOW),
    ):
        connection = _connection()
        with pytest.raises(ValueError):
            ArticleRetrievalRepository(cast(Connection, connection)).record_failure(
                target=TARGET,
                job_id=uuid.uuid7(),
                error_code=code,
                started_at=start,
                completed_at=end,
            )
        connection.execute.assert_not_called()
