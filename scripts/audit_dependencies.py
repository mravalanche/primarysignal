"""Audit all locked third-party dependencies without including this project."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def executable(name: str) -> str:
    """Resolve a required command or stop with an actionable error."""

    path = shutil.which(name)
    if path is None:
        print(f"{name} is required to audit dependencies", file=sys.stderr)
        raise SystemExit(2)
    return path


def main() -> int:
    """Export the uv lock and audit the resulting hashed requirements."""

    uv = executable("uv")
    pip_audit = executable("pip-audit")
    project_root = Path(__file__).resolve().parents[1]

    with tempfile.TemporaryDirectory(prefix="primary-signal-audit-") as directory:
        requirements = Path(directory) / "requirements.txt"
        # The executable and arguments are fixed locally; no user input reaches the shell.
        exported = subprocess.run(  # noqa: S603
            [
                uv,
                "--quiet",
                "export",
                "--locked",
                "--all-groups",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            check=False,
            cwd=project_root,
        )
        if exported.returncode != 0:
            return exported.returncode

        # The executable and arguments are fixed locally; no user input reaches the shell.
        audited = subprocess.run(  # noqa: S603
            [
                pip_audit,
                "--requirement",
                str(requirements),
                "--require-hashes",
                "--disable-pip",
                "--strict",
                "--progress-spinner",
                "off",
            ],
            check=False,
            cwd=project_root,
        )
        return audited.returncode


if __name__ == "__main__":
    raise SystemExit(main())
