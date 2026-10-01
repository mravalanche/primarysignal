"""Serve the public or administration web surface."""

import argparse
from collections.abc import Sequence

import uvicorn

from primary_signal.config import Settings
from primary_signal.web.admin import create_admin_app
from primary_signal.web.public import create_public_app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("public", "admin"), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run exactly one web surface."""

    args = _parser().parse_args(argv)
    settings = Settings()
    factory = create_public_app if args.surface == "public" else create_admin_app
    uvicorn.run(
        factory(settings),
        host=args.host,
        port=args.port,
        proxy_headers=False,
    )


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
