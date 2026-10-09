"""Run one bounded extracted-text cleanup batch with the maintenance login."""

import argparse
import json
from collections.abc import Callable, Sequence
from typing import cast

from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.db.engine import UnexpectedDatabaseRoleError
from primary_signal.ingestion.retention_cleanup import clear_expired_extracted_text


def run_batch(engine: Engine, *, limit: int = 100) -> tuple[str, ...]:
    """Commit one batch and expose identifiers only after it succeeds."""

    with engine.begin() as connection:
        cleared = clear_expired_extracted_text(connection, limit=limit)
    return tuple(str(identifier) for identifier in cleared)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="Rows per batch (1 to 500)")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 500:
        parser.error("limit must be between 1 and 500")
    try:
        settings = cast(Callable[[], DatabaseSettings], DatabaseSettings)()
        engine = create_database_engine(settings)
        try:
            cleared = run_batch(engine, limit=args.limit)
        finally:
            engine.dispose()
    except (ValidationError, SQLAlchemyError, UnexpectedDatabaseRoleError, RuntimeError) as error:
        raise SystemExit("retention cleanup unavailable") from error
    print(json.dumps({"count": len(cleared), "content_version_ids": cleared}))


if __name__ == "__main__":  # pragma: no cover
    main()
