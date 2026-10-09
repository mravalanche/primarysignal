"""Print a bounded source and feed health report from a local terminal."""

import argparse
import json
from collections.abc import Callable, Sequence
from typing import cast

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.db.engine import UnexpectedDatabaseRoleError
from primary_signal.sources.health import read_health, validate_limit


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=100, help="Maximum sources and feeds (1 to 500 each)"
    )
    args = parser.parse_args(argv)
    try:
        limit = validate_limit(args.limit)
    except ValueError as error:
        parser.error(str(error))
    try:
        load_settings = cast(Callable[[], DatabaseSettings], DatabaseSettings)
        engine = create_database_engine(load_settings())
        try:
            with engine.connect() as connection:
                report = read_health(connection, limit=limit)
        finally:
            engine.dispose()
    except ValidationError, SQLAlchemyError, UnexpectedDatabaseRoleError:
        parser.exit(1, "source health report unavailable\n")
    print(json.dumps(report.as_dict(), separators=(",", ":")))


if __name__ == "__main__":  # pragma: no cover
    main()
