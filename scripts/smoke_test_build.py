"""Install the built wheel in isolation and import its application factories."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    """Smoke-test the newest Primary Signal wheel outside the source tree."""

    project_root = Path(__file__).resolve().parents[1]
    wheels = list((project_root / "dist").glob("primary_signal-*.whl"))
    if not wheels:
        print("No Primary Signal wheel found in dist/", file=sys.stderr)
        return 2

    wheel = max(wheels, key=lambda path: path.stat().st_mtime_ns)
    uv = shutil.which("uv")
    if uv is None:
        print("uv is required to test the built wheel", file=sys.stderr)
        return 2

    import_check = (
        "from primary_signal.web.admin import create_admin_app; "
        "from primary_signal.web.public import create_public_app; "
        "assert create_admin_app().title; "
        "assert create_public_app().title"
    )
    with tempfile.TemporaryDirectory(prefix="primary-signal-wheel-") as directory:
        # The executable, wheel path, and Python statement are fixed locally.
        completed = subprocess.run(  # noqa: S603
            [
                uv,
                "run",
                "--isolated",
                "--no-project",
                "--with",
                str(wheel),
                "python",
                "-I",
                "-c",
                import_check,
            ],
            check=False,
            cwd=directory,
        )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
