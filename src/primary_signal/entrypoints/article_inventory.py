"""Search the internal article inventory from a local operator terminal."""

import argparse
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Engine

from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.ingestion.inventory import (
    ArticleInventoryRepository,
    ArticleListQuery,
    ArticlePage,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search", help="Words to search in current article text")
    parser.add_argument("--source", dest="source_key", help="Exact source key")
    parser.add_argument("--limit", type=int, default=20, help="Rows per page (1 to 50)")
    parser.add_argument("--cursor", help="Cursor returned by a previous page")
    return parser


def _page_json(page: ArticlePage) -> str:
    items: list[dict[str, Any]] = []
    for item in page.items:
        items.append(
            {
                "article_id": str(item.article_id),
                "source_key": item.source_key,
                "source_name": item.source_name,
                "title": item.title,
                "first_seen_at": _timestamp(item.first_seen_at),
                "fetched_at": _timestamp(item.fetched_at),
                "word_count": item.word_count,
            }
        )
    return json.dumps({"items": items, "next_cursor": page.next_cursor}, separators=(",", ":"))


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def list_page(engine: Engine, query: ArticleListQuery) -> ArticlePage:
    """Open one read-only connection and return a bounded inventory page."""

    with engine.connect() as connection:
        return ArticleInventoryRepository(connection).list_articles(query)


def main(argv: Sequence[str] | None = None) -> None:
    """Print metadata and an opaque next cursor, never extracted body text."""

    parser = _parser()
    args = parser.parse_args(argv)
    try:
        query = ArticleListQuery(
            search=args.search,
            source_key=args.source_key,
            limit=args.limit,
            cursor=args.cursor,
        )
    except ValueError as error:
        parser.error(str(error))
    load_settings = cast(Callable[[], DatabaseSettings], DatabaseSettings)
    settings = load_settings()
    engine = create_database_engine(settings)
    try:
        page = list_page(engine, query)
    finally:
        engine.dispose()
    print(_page_json(page))


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
