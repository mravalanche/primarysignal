"""Serve the development retrieval API without database access."""

import argparse
import ipaddress
import os
from collections.abc import Sequence

import uvicorn

from primary_signal.config import Settings
from primary_signal.observability import configure_logging
from primary_signal.retrieval.api import create_retriever_app
from primary_signal.retrieval.fixture_rpc import fixture_rpc_listener


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
    parser.add_argument("--host", help="Loopback host, or the exact fixture RPC address")
    parser.add_argument("--port", type=int, help="Local port, or the exact fixture RPC port")
    opt_in = parser.add_mutually_exclusive_group()
    opt_in.add_argument("--allow-local-fetch", action="store_true")
    opt_in.add_argument("--allow-fixture-rpc", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run only an explicitly selected development or test binding."""

    parser = _parser()
    args = parser.parse_args(argv)
    if not args.allow_local_fetch and not args.allow_fixture_rpc:
        parser.error("retrieval requires explicit development or test opt-in")
    if args.allow_fixture_rpc:
        try:
            host, port = fixture_rpc_listener(args.host, args.port)
        except ValueError as error:
            parser.error(str(error))
    else:
        try:
            host = _loopback_address(args.host if args.host is not None else "127.0.0.1")
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))
        port = args.port if args.port is not None else 8765
        if not 1 <= port <= 65535:
            parser.error("port must be between 1 and 65535")
    if os.environ.get("PRIMARY_SIGNAL_ENVIRONMENT") not in ("development", "test"):
        parser.error("retrieval requires explicit development or test environment")
    settings = Settings()
    if settings.environment.value not in ("development", "test"):
        parser.error("retrieval requires development or test settings")
    configure_logging(level=settings.log_level.value)
    uvicorn.run(
        create_retriever_app(),
        host=host,
        port=port,
        proxy_headers=False,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
