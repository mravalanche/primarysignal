"""Shared behaviour for entrypoints that are not part of this milestone."""

import sys
from typing import NoReturn


def fail_unimplemented(process_name: str) -> NoReturn:
    """Stop an unavailable process with a stable, actionable error."""

    print(
        f"primary-signal-{process_name}: not implemented in the platform scaffold",
        file=sys.stderr,
    )
    raise SystemExit(2)
