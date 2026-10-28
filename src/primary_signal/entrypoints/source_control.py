"""Pause or resume one configured source with a required audit reason."""

import argparse
import json
from collections.abc import Callable, Sequence
from typing import cast

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.db.engine import UnexpectedDatabaseRoleError
from primary_signal.sources.control import set_source_enabled, validate_reason
from primary_signal.sources.repository import validate_source_key


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pause", "resume"))
    parser.add_argument("source_key", help="Exact configured source key")
    parser.add_argument("--reason", required=True, help="Audit reason, at most 500 characters")
    args = parser.parse_args(argv)
    try:
        source_key = validate_source_key(args.source_key)
        reason = validate_reason(args.reason)
    except ValueError as error:
        parser.error(str(error))
    try:
        load_settings = cast(Callable[[], DatabaseSettings], DatabaseSettings)
        engine = create_database_engine(load_settings())
        try:
            with engine.begin() as connection:
                result = set_source_enabled(
                    connection, source_key, enabled=args.action == "resume", reason=reason
                )
        finally:
            engine.dispose()
    except ValueError, ValidationError, SQLAlchemyError, UnexpectedDatabaseRoleError:
        parser.exit(1, "source control unavailable\n")
    print(json.dumps(result.as_dict(), separators=(",", ":")))


if __name__ == "__main__":  # pragma: no cover
    main()
