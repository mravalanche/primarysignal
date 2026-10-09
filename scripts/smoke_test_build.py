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

    required_resources = (
        "web/common/templates/layouts/base.html",
        "web/common/templates/components/ui.html",
        "web/common/templates/components/story.html",
        "web/common/static/htmx.min.js",
        "web/public/templates/layouts/public.html",
        "web/public/templates/catalogue.html",
        "web/public/static/public.css",
        "web/admin/templates/layouts/admin.html",
        "web/admin/static/admin.css",
        "db/alembic.ini",
        "db/migrations/env.py",
        "db/migrations/versions/20261001_01_initial_ingestion_schema.py",
        "db/migrations/versions/20261001_02_harden_job_leases.py",
        "db/migrations/versions/20261001_03_queue_capability_grants.py",
        "db/migrations/versions/20261001_04_feed_scheduler_grants.py",
        "db/migrations/versions/20261007_05_feed_poll_grants.py",
        "db/migrations/versions/20261009_06_article_persist_grants.py",
        "db/migrations/versions/20261009_07_article_inventory_search.py",
        "db/migrations/versions/20261009_08_publication_projection.py",
        "db/migrations/versions/20261009_09_publication_writer.py",
        "db/migrations/versions/20261009_10_source_health.py",
        "db/migrations/versions/20261009_11_source_read_lock.py",
        "db/migrations/versions/20261009_12_editorial_read.py",
    )
    import_check = (
        "from importlib.resources import files; "
        "from alembic.config import Config; "
        "from alembic.script import ScriptDirectory; "
        "from primary_signal.web.admin import create_admin_app; "
        "from primary_signal.web.public import create_public_app; "
        "from primary_signal.entrypoints.migrate import migration_config_path; "
        f"required={required_resources!r}; "
        "root=files('primary_signal'); "
        "missing=[path for path in required if not root.joinpath(path).is_file()]; "
        "assert not missing, f'missing packaged UI resources: {missing}'; "
        "assert migration_config_path().is_file(); "
        "assert ScriptDirectory.from_config(Config(migration_config_path())).get_current_head() == '20261009_12'; "
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
