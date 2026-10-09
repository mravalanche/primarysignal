"""PostgreSQL proof of article inventory search and stable keyset paging."""

import hashlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, text, update

from primary_signal.identity.urls import identify_url
from primary_signal.ingestion.inventory import ArticleInventoryRepository, ArticleListQuery
from primary_signal.ingestion.models import Article, ArticleUrl, ContentVersion, FetchAttempt
from primary_signal.sources.models import Source


@pytest.mark.postgres
def test_current_version_search_source_filter_and_keyset_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_URL")
    expected_role = os.environ.get("PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE")
    if not database_url or not expected_role:
        pytest.skip("set disposable PostgreSQL test database settings")
    engine = create_engine(database_url, hide_parameters=True)
    with engine.connect() as connection:
        assert str(connection.execute(text("SELECT current_database()")).scalar_one()).endswith(
            "_test"
        )
        assert connection.execute(text("SELECT current_user")).scalar_one() == expected_role
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_URL", database_url)
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", expected_role)
    command.upgrade(Config(Path(__file__).resolve().parents[1] / "alembic.ini"), "head")

    now = datetime.now(UTC)
    source_keys = [f"inventory-test-{uuid.uuid7().hex}" for _ in range(2)]
    source_ids = [uuid.uuid7() for _ in source_keys]
    article_ids = [uuid.uuid7() for _ in range(4)]

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            for source_id, key in zip(source_ids, source_keys, strict=True):
                connection.execute(
                    insert(Source).values(
                        id=source_id,
                        source_key=key,
                        name=f"Synthetic {key}",
                        homepage_url="https://public.example/",
                    )
                )

            def add_version(article_id: uuid.UUID, *, title: str, body: str) -> None:
                attempt_id, version_id = uuid.uuid7(), uuid.uuid7()
                url = f"https://public.example/article/{article_id}"
                connection.execute(
                    insert(FetchAttempt).values(
                        id=attempt_id,
                        article_id=article_id,
                        retrieval_strategy="direct_http",
                        requested_url=url,
                        redirect_chain=[],
                        status="running",
                        started_at=now,
                    )
                )
                connection.execute(
                    insert(ContentVersion).values(
                        id=version_id,
                        article_id=article_id,
                        origin_fetch_attempt_id=attempt_id,
                        raw_response_hash=hashlib.sha256(body.encode()).hexdigest(),
                        normalized_content_hash=hashlib.sha256((title + body).encode()).hexdigest(),
                        normalization_version=1,
                        extracted_title=title,
                        extracted_text=body,
                        extractor_name="synthetic-test",
                        extractor_version="1",
                        word_count=len(body.split()),
                        fetched_at=now,
                    )
                )
                connection.execute(
                    update(Article)
                    .where(Article.id == article_id)
                    .values(current_content_version_id=version_id)
                )

            for index, article_id in enumerate(article_ids):
                source_id = source_ids[0] if index < 3 else source_ids[1]
                url = f"https://public.example/article/{article_id}"
                identity = identify_url(url)
                url_id = uuid.uuid7()
                connection.execute(
                    insert(Article).values(
                        id=article_id,
                        source_id=source_id,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                connection.execute(
                    insert(ArticleUrl).values(
                        id=url_id,
                        article_id=article_id,
                        original_url=url,
                        normalized_url=identity.normalized_url,
                        normalized_url_hash=identity.url_hash,
                        normalization_version=identity.normalization_version,
                        kind="submitted",
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                connection.execute(
                    update(Article)
                    .where(Article.id == article_id)
                    .values(current_canonical_url_id=url_id)
                )
                add_version(article_id, title=f"Notice {index}", body="amber bulletin")
            # An older revision must not appear in current-version search.
            add_version(article_ids[0], title="Revised notice", body="cobalt bulletin")

            repository = ArticleInventoryRepository(connection)
            first_page = repository.list_articles(ArticleListQuery(limit=2))
            assert len(first_page.items) == 2
            assert first_page.next_cursor is not None
            second_page = repository.list_articles(
                ArticleListQuery(limit=2, cursor=first_page.next_cursor)
            )
            assert len(second_page.items) == 2
            assert second_page.next_cursor is None
            assert [item.article_id for item in first_page.items + second_page.items] == sorted(
                article_ids, reverse=True
            )
            assert all(
                item.canonical_url.startswith("https://public.example/")
                for item in first_page.items
            )
            assert not hasattr(first_page.items[0], "extracted_text")

            cobalt = repository.list_articles(ArticleListQuery(search="cobalt"))
            assert [item.article_id for item in cobalt.items] == [article_ids[0]]
            assert cobalt.items[0].title == "Revised notice"
            assert cobalt.items[0].word_count == 2
            assert article_ids[0] not in {
                item.article_id
                for item in repository.list_articles(ArticleListQuery(search="amber")).items
            }
            assert repository.list_articles(ArticleListQuery(search="the and")).items == ()
            filtered = repository.list_articles(ArticleListQuery(source_key=source_keys[1]))
            assert [item.article_id for item in filtered.items] == [article_ids[3]]
        finally:
            transaction.rollback()
            engine.dispose()
