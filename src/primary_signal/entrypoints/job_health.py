"""Print bounded queue counts and recent job metadata from a local terminal."""

import argparse
import json
from collections.abc import Callable, Sequence
from typing import cast

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.db.engine import UnexpectedDatabaseRoleError
from primary_signal.jobs.health import read_health, validate_limit, validate_queue, validate_status


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", help="Filter by queue machine name")
    parser.add_argument(
        "--status", help="Filter recent jobs by stored status; keep all queue counts"
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="Maximum queues and jobs (1 to 500 each)"
    )
    args = parser.parse_args(argv)
    try:
        queue = validate_queue(args.queue)
        status = validate_status(args.status)
        limit = validate_limit(args.limit)
    except ValueError as error:
        parser.error(str(error))
    try:
        load_settings = cast(Callable[[], DatabaseSettings], DatabaseSettings)
        engine = create_database_engine(load_settings())
        try:
            with engine.connect() as connection:
                report = read_health(connection, queue=queue, status=status, limit=limit)
        finally:
            engine.dispose()
    except ValueError, ValidationError, SQLAlchemyError, UnexpectedDatabaseRoleError:
        parser.exit(1, "job health report unavailable\n")
    print(json.dumps(report.as_dict(), separators=(",", ":")))


if __name__ == "__main__":  # pragma: no cover
    main()
