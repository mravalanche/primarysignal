"""One fixed RPC binding for isolated development and test fixtures.

This contract permits separate fixture namespaces. It does not verify network
policy or authorize production retrieval, and is never a retrieval-target rule.
"""

from typing import Final

FIXTURE_RPC_HOST: Final = "192.0.2.10"
FIXTURE_RPC_PORT: Final = 8765
FIXTURE_RPC_ORIGIN: Final = "http://192.0.2.10:8765"


def fixture_rpc_listener(host: object, port: object) -> tuple[str, int]:
    """Require the exact explicit fixture listener without resolving a host."""
    if (
        type(host) is not str
        or host != FIXTURE_RPC_HOST
        or type(port) is not int
        or port != FIXTURE_RPC_PORT
    ):
        raise ValueError("fixture RPC requires its exact reserved listener and port")
    return FIXTURE_RPC_HOST, FIXTURE_RPC_PORT


def fixture_rpc_origin(value: object) -> str:
    """Require the canonical fixture origin, including its explicit fixed port."""
    if type(value) is not str or value != FIXTURE_RPC_ORIGIN:
        raise ValueError("fixture RPC requires its exact reserved HTTP origin")
    return FIXTURE_RPC_ORIGIN
