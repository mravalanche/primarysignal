"""Create fixed capability roles in an explicitly disposable test database."""

import os
from pathlib import Path

from sqlalchemy import create_engine, text

TEST_DATABASE_URL_ENV = "PRIMARY_SIGNAL_TEST_DATABASE_URL"


def main() -> None:
    """Apply the role bootstrap without printing the database URL."""

    database_url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not database_url:
        raise SystemExit(f"{TEST_DATABASE_URL_ENV} is required")

    repository_root = Path(__file__).resolve().parents[1]
    bootstrap_sql = (
        repository_root / "deploy/postgres/initdb/010_queue_capability_roles.sql"
    ).read_text(encoding="utf-8")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            database_name = str(connection.execute(text("SELECT current_database()")).scalar_one())
            if not database_name.endswith("_test"):
                raise SystemExit("role bootstrap requires a disposable database ending in _test")
            connection.exec_driver_sql(bootstrap_sql)
            connection.commit()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
