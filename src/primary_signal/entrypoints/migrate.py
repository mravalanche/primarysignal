"""Apply or reverse database schema migrations."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def migration_config_path() -> Path:
    """Locate Alembic configuration in a source checkout or installed wheel."""

    package_config = Path(__file__).resolve().parents[1] / "db" / "alembic.ini"
    if package_config.is_file():
        return package_config
    return Path(__file__).resolve().parents[3] / "alembic.ini"


def main(argv: Sequence[str] | None = None) -> None:
    """Run migrations using environment-only database settings."""

    _parser().parse_args(argv)
    config = Config(migration_config_path())
    command.upgrade(config, "head")


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
