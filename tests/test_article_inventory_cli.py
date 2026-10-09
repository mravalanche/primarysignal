"""The local inventory command emits only bounded metadata."""

import json
import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock

import pytest
from sqlalchemy import Engine

from primary_signal.db import DatabaseSettings
from primary_signal.entrypoints import article_inventory
from primary_signal.ingestion.inventory import ArticleItem, ArticleListQuery, ArticlePage


def test_inventory_command_omits_full_text_and_raw_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    moment = datetime(2026, 10, 9, tzinfo=UTC)
    page = ArticlePage(
        items=(
            ArticleItem(
                article_id=uuid.uuid7(),
                source_key="public-example",
                source_name="Public Example",
                canonical_url="https://public.example/story?private=never-print",
                title="Synthetic notice",
                first_seen_at=moment,
                fetched_at=moment,
                word_count=12,
            ),
        ),
        next_cursor="opaque-cursor",
    )
    engine = Mock(spec=Engine)
    monkeypatch.setattr(
        article_inventory, "DatabaseSettings", lambda: cast(DatabaseSettings, object())
    )

    def fake_engine(_settings: DatabaseSettings) -> Engine:
        return cast(Engine, engine)

    monkeypatch.setattr(article_inventory, "create_database_engine", fake_engine)
    observed: list[ArticleListQuery] = []

    def fake_list_page(_engine: Engine, query: ArticleListQuery) -> ArticlePage:
        observed.append(query)
        return page

    monkeypatch.setattr(article_inventory, "list_page", fake_list_page)
    article_inventory.main(["--search", "synthetic", "--source", "public-example", "--limit", "1"])
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["next_cursor"] == "opaque-cursor"
    assert parsed["items"][0]["title"] == "Synthetic notice"
    assert "private=never-print" not in output
    assert "canonical_url" not in parsed["items"][0]
    assert "extracted_text" not in parsed["items"][0]
    assert observed[0].limit == 1
    engine.dispose.assert_called_once()


def test_invalid_inventory_arguments_never_open_database(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = Mock()
    monkeypatch.setattr(article_inventory, "create_database_engine", factory)
    with pytest.raises(SystemExit) as raised:
        article_inventory.main(["--limit", "1000"])
    assert raised.value.code == 2
    factory.assert_not_called()
