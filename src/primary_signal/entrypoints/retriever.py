"""Serve the local feed retrieval API without database access."""

import argparse
import ipaddress
import os
from collections.abc import Sequence

import uvicorn

from primary_signal.config import Settings
from primary_signal.observability import configure_logging
from primary_signal.retrieval.api import create_retriever_app


def _loopback_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("host must be a loopback IP address") from error
    if not address.is_loopback:
        raise argparse.ArgumentTypeError("host must be a loopback IP address")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=_loopback_address, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-local-fetch", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run only on loopback while deployment network controls are pending."""

    parser = _parser()
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if not args.allow_local_fetch or os.environ.get("PRIMARY_SIGNAL_ENVIRONMENT") not in (
        "development",
        "test",
    ):
        parser.error("local retrieval requires explicit development or test opt-in")
    settings = Settings()
    configure_logging(level=settings.log_level.value)
    uvicorn.run(
        create_retriever_app(),
        host=args.host,
        port=args.port,
        proxy_headers=False,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
