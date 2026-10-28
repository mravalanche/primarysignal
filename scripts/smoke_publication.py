"""Prove the disposable publication stack through real browser decisions."""

import argparse
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from contextlib import ExitStack, suppress
from pathlib import Path
from time import monotonic, sleep
from typing import cast

from alembic.config import Config
from alembic.script import ScriptDirectory

from scripts.publication_smoke_fixture import (
    NETWORKS,
    FixtureBoundaryError,
    PublicationSmokeFixture,
    create_fixture,
    validate_fixture_models,
)

RAW_MARKER = "Fictional evidence for a disposable publication test."
PUBLISH_REASON = "Synthetic browser publication smoke: reviewed source lineage."
SUPPRESS_REASON = "Synthetic browser publication smoke: suppression verification."
RUNTIME = ("database", "public", "admin", "admin_tls")
BUILD_PATHS = (
    "Dockerfile",
    ".dockerignore",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "alembic.ini",
    "frontend",
    "src",
    "migrations",
)


class SmokeFailure(RuntimeError):
    """A stage label, never command output or fixture configuration."""


def _progress(stage: str) -> None:
    print(f"Synthetic publication smoke: {stage}.", flush=True)


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)


def _command(
    fixture: PublicationSmokeFixture,
    arguments: Sequence[str],
    *,
    stage: str,
    timeout: int = 60,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(  # noqa: S603 - all callers use explicit local commands, never a shell.
        list(arguments),
        cwd=fixture.root,
        env=fixture.env if env is None else env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, errors = process.communicate(input=input_text, timeout=timeout)
    except BaseException:
        _terminate(process)
        raise
    result = subprocess.CompletedProcess(list(arguments), process.returncode, output, errors)
    if check and result.returncode:
        raise SmokeFailure(stage)
    return result


def _docker(
    fixture: PublicationSmokeFixture,
    *args: str,
    stage: str,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _command(
        fixture, (*fixture.docker_args, *args), stage=stage, timeout=timeout, check=check
    )


def _compose(fixture: PublicationSmokeFixture, *args: str, stage: str, timeout: int = 60) -> str:
    return _command(
        fixture,
        (*fixture.docker_args, *fixture.compose_args, *args),
        stage=stage,
        timeout=timeout,
    ).stdout.strip()


def _exists(fixture: PublicationSmokeFixture, kind: str, name: str) -> bool:
    result = _docker(fixture, kind, "inspect", name, stage="resource inspection", check=False)
    if result.returncode == 0:
        return True
    missing = {
        "image": "no such image",
        "volume": "no such volume",
        "network": "not found",
    }
    if result.returncode == 1 and missing[kind] in result.stderr.lower():
        return False
    raise SmokeFailure("resource inspection")


def _fresh(fixture: PublicationSmokeFixture) -> None:
    if not _docker(
        fixture, "version", "--format", "{{.Server.APIVersion}}", stage="local daemon"
    ).stdout.strip():
        raise SmokeFailure("local daemon")
    containers = _docker(
        fixture,
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label=com.docker.compose.project={fixture.project}",
        stage="fresh project",
    ).stdout.strip()
    if (
        containers
        or _exists(fixture, "image", fixture.image)
        or _exists(fixture, "volume", f"{fixture.project}_publication-data")
    ):
        raise SmokeFailure("fresh project")
    for name in NETWORKS:
        if _exists(fixture, "network", f"{fixture.project}_{name}"):
            raise SmokeFailure("fresh project")


def _build_context(fixture: PublicationSmokeFixture) -> Path:
    """Send only tracked build inputs, including their current working copies."""
    git = shutil.which("git", path=os.defpath)
    if git is None:
        raise SmokeFailure("tracked build context")
    files = _command(
        fixture, [git, "ls-files", "-z", "--", *BUILD_PATHS], stage="tracked build context"
    ).stdout.split("\0")
    context = fixture.directory / "build-context"
    context.mkdir(mode=0o700)
    copied: set[str] = set()
    for filename in filter(None, files):
        relative = Path(filename)
        source = fixture.root / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or source.is_symlink()
            or not source.is_file()
            or not source.resolve().is_relative_to(fixture.root)
        ):
            raise SmokeFailure("tracked build context")
        destination = context / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied.add(filename)
    if not set(BUILD_PATHS[:8]) <= copied:
        raise SmokeFailure("tracked build context")
    return context


def _node(fixture: PublicationSmokeFixture) -> Path:
    version = (fixture.root / ".node-version").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"24\.\d+\.\d+", version) is None:
        raise SmokeFailure("pinned Node toolchain")
    architecture = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
    if architecture is None:
        raise SmokeFailure("pinned Node toolchain")
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    candidates = [
        cache / "primarysignal-dev" / f"node-v{version}-linux-{architecture}" / "bin/node"
    ]
    system_node = shutil.which("node")
    if system_node:
        candidates.append(Path(system_node))
    for candidate in candidates:
        if (
            candidate.is_file()
            and _command(
                fixture,
                [str(candidate), "--version"],
                stage="pinned Node toolchain",
                check=False,
            ).stdout.strip()
            == f"v{version}"
        ):
            return candidate
    raise SmokeFailure("pinned Node toolchain")


def _browser_environment(fixture: PublicationSmokeFixture, node: Path) -> dict[str, str]:
    installed = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if installed == "0":
        browsers = fixture.root / "node_modules/playwright-core/.local-browsers"
    elif installed:
        browsers = Path(installed).resolve()
    else:
        browsers = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"
    scratch = fixture.directory / "browser-tmp"
    scratch.mkdir(mode=0o700)
    private_paths: dict[str, str] = {}
    for variable in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        path = fixture.directory / variable.lower()
        path.mkdir(mode=0o700)
        private_paths[variable] = str(path)
    environment = (
        fixture.env
        | private_paths
        | {
            "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
            "TMPDIR": str(scratch),
        }
    )
    _command(
        fixture,
        [
            str(node),
            "-e",
            "const{chromium}=require('playwright');if(!require('fs').existsSync(chromium.executablePath()))process.exit(1)",
        ],
        stage="cached Playwright browser",
        env=environment,
    )
    return environment


def _browser(
    fixture: PublicationSmokeFixture, node: Path, environment: dict[str, str], phase: str
) -> None:
    configuration = {
        "phase": phase,
        "publicOrigin": fixture.public_origin,
        "adminOrigin": fixture.admin_origin,
        "adminPassword": fixture.admin_password,
        "slug": fixture.expected_slug,
        "headline": fixture.expected_headline,
        "rawMarker": RAW_MARKER,
        "publishReason": PUBLISH_REASON,
        "suppressReason": SUPPRESS_REASON,
    }
    result = _command(
        fixture,
        [str(node), str(fixture.root / "scripts/smoke_publication_browser.mjs")],
        stage=f"browser {phase}",
        timeout=90,
        input_text=json.dumps(configuration),
        env=environment,
    )
    try:
        if json.loads(result.stdout) != {"phase": phase, "passed": True}:
            raise SmokeFailure(f"browser {phase}")
    except ValueError:
        raise SmokeFailure(f"browser {phase}") from None


def _containers(fixture: PublicationSmokeFixture) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for service in ("database", "migrate", "seed", "public", "admin", "admin_tls"):
        identifier = _compose(
            fixture, "ps", "--all", "--quiet", service, stage="container identity"
        )
        if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
            raise SmokeFailure("container identity")
        state = _docker(
            fixture,
            "inspect",
            "--format",
            "{{.State.StartedAt}}|{{.State.Status}}|{{.State.ExitCode}}",
            identifier,
            stage="container state",
        ).stdout.strip()
        if service in {"migrate", "seed"} and not state.endswith("|exited|0"):
            raise SmokeFailure("migration and seed completion")
        result[service] = (identifier, state)
    return result


def _volume(fixture: PublicationSmokeFixture) -> str:
    value = _docker(
        fixture,
        "volume",
        "inspect",
        "--format",
        '{{.Name}}|{{.CreatedAt}}|{{index .Labels "com.docker.compose.project"}}',
        f"{fixture.project}_publication-data",
        stage="volume identity",
    ).stdout.strip()
    if not value.startswith(f"{fixture.project}_publication-data|") or not value.endswith(
        f"|{fixture.project}"
    ):
        raise SmokeFailure("volume identity")
    return value


def _restart(fixture: PublicationSmokeFixture) -> None:
    before, volume = _containers(fixture), _volume(fixture)
    _compose(
        fixture,
        "stop",
        "--timeout",
        "10",
        "admin_tls",
        "public",
        "admin",
        "database",
        stage="runtime restart",
        timeout=60,
    )
    # Compose start can start completed dependencies. Address only the captured
    # runtime containers so migration and seed cannot run again.
    _docker(fixture, "start", before["database"][0], stage="runtime restart", timeout=30)
    _wait_runtime(fixture, before["database"][0], require_health=True)
    _docker(
        fixture,
        "start",
        before["public"][0],
        before["admin"][0],
        stage="runtime restart",
        timeout=30,
    )
    _wait_runtime(fixture, before["public"][0], require_health=True)
    _wait_runtime(fixture, before["admin"][0], require_health=False)
    _docker(fixture, "start", before["admin_tls"][0], stage="runtime restart", timeout=30)
    _wait_runtime(fixture, before["admin_tls"][0], require_health=False)
    after = _containers(fixture)
    if _volume(fixture) != volume or any(after[name][0] != before[name][0] for name in before):
        raise SmokeFailure("restart persistence")
    if any(after[name] != before[name] for name in ("migrate", "seed")):
        raise SmokeFailure("one-shot services reran")
    if any(
        after[name][1].split("|")[0] == before[name][1].split("|")[0]
        or after[name][1].split("|")[1] != "running"
        for name in RUNTIME
    ):
        raise SmokeFailure("runtime services did not restart")
    _runtime_bindings(fixture, after)


def _wait_runtime(
    fixture: PublicationSmokeFixture, identifier: str, *, require_health: bool, timeout: float = 90
) -> None:
    deadline = monotonic() + timeout
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SmokeFailure("runtime readiness")
        state = _docker(
            fixture,
            "inspect",
            "--format",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            identifier,
            stage="runtime readiness",
            timeout=max(1, min(10, int(remaining))),
        ).stdout.strip()
        status, separator, health = state.partition("|")
        if (
            not separator
            or status not in {"created", "running", "restarting"}
            or health == "unhealthy"
        ):
            raise SmokeFailure("runtime readiness")
        if status == "running" and (not require_health or health == "healthy"):
            return
        if status == "running" and require_health and not health:
            raise SmokeFailure("runtime readiness")
        sleep(min(0.25, max(0, deadline - monotonic())))


def _runtime_bindings(
    fixture: PublicationSmokeFixture, containers: dict[str, tuple[str, str]]
) -> None:
    """Check effective bindings; requested HostConfig ports alone are insufficient."""

    expected = {
        "public": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(fixture.public_port)}]},
        "admin": {"8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(fixture.admin_port)}]},
        "database": {},
    }
    for service, wanted in expected.items():
        raw = _docker(
            fixture,
            "inspect",
            "--format",
            "{{json .NetworkSettings.Ports}}",
            containers[service][0],
            stage="published runtime ports",
        ).stdout
        try:
            ports = json.loads(raw)
        except ValueError, TypeError:
            raise SmokeFailure("published runtime ports") from None
        if ports is None:
            ports = {}
        if not isinstance(ports, dict):
            raise SmokeFailure("published runtime ports")
        port_values = cast(dict[str, object], ports)
        # Image EXPOSE declarations can legitimately retain null entries. Only
        # actual nonempty host bindings belong in the comparison.
        effective = {
            port: bindings
            for port, bindings in port_values.items()
            if bindings is not None and bindings != []
        }
        if effective != wanted:
            raise SmokeFailure("published runtime ports")


def _database(fixture: PublicationSmokeFixture, query: str) -> str:
    return _compose(
        fixture,
        "exec",
        "-T",
        "database",
        "psql",
        "--no-psqlrc",
        "--no-password",
        "--username",
        fixture.bootstrap_role,
        "--dbname",
        fixture.database_name,
        "--no-align",
        "--tuples-only",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        query,
        stage="synthetic database verification",
    )


def _runtime(fixture: PublicationSmokeFixture) -> None:
    containers = _containers(fixture)
    _runtime_bindings(fixture, containers)
    head = ScriptDirectory.from_config(Config(str(fixture.root / "alembic.ini"))).get_current_head()
    if (
        not head
        or _database(fixture, "SELECT version_num FROM primary_signal.alembic_version") != head
    ):
        raise SmokeFailure("database migration head")
    if (
        _database(
            fixture,
            "SELECT rolname,rolsuper FROM pg_catalog.pg_roles WHERE rolname='migration_test'",
        )
        != "migration_test|f"
    ):
        raise SmokeFailure("non-superuser migration login")
    for service in ("public", "admin"):
        if (
            _compose(fixture, "exec", "-T", service, "id", "-u", stage="non-root runtime")
            != "10001"
        ):
            raise SmokeFailure("non-root runtime")
    _compose(
        fixture,
        "exec",
        "-T",
        "public",
        "python",
        "-c",
        "import sys;from alembic.config import Config;from alembic.script import ScriptDirectory;from primary_signal.entrypoints.migrate import migration_config_path;assert ScriptDirectory.from_config(Config(migration_config_path())).get_current_head()==sys.argv[1]",
        head,
        stage="packaged migration head",
    )
    if (
        _database(
            fixture,
            "SELECT (SELECT count(*) FROM primary_signal.stories),(SELECT count(*) FROM primary_signal.story_revisions),(SELECT count(*) FROM primary_signal.content_versions)",
        )
        != "1|1|1"
    ):
        raise SmokeFailure("synthetic lineage")


def _cleanup(fixture: PublicationSmokeFixture) -> None:
    failed = False
    try:
        _compose(
            fixture,
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "10",
            stage="cleanup",
            timeout=90,
        )
    except SmokeFailure, OSError, subprocess.SubprocessError:
        failed = True
    try:
        if _exists(fixture, "image", fixture.image):
            _docker(fixture, "image", "rm", fixture.image, stage="cleanup", timeout=60)
        if _exists(fixture, "volume", f"{fixture.project}_publication-data"):
            failed = True
        for name in NETWORKS:
            if _exists(fixture, "network", f"{fixture.project}_{name}"):
                failed = True
        remaining = _docker(
            fixture,
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={fixture.project}",
            stage="cleanup",
        ).stdout.strip()
        failed = failed or bool(remaining) or _exists(fixture, "image", fixture.image)
    except SmokeFailure, OSError, subprocess.SubprocessError:
        failed = True
    if failed:
        raise SmokeFailure("cleanup")


def _ports() -> tuple[int, int]:
    with ExitStack() as stack:
        sockets = [
            stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) for _ in range(2)
        ]
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        return sockets[0].getsockname()[1], sockets[1].getsockname()[1]


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    root = Path(__file__).resolve().parents[1]
    project = f"ps-publication-smoke-{uuid.uuid4().hex}"
    status = 1
    try:
        with tempfile.TemporaryDirectory(
            prefix="primary-signal-publication-smoke-", dir="/tmp"
        ) as directory:
            public_port, admin_port = _ports()
            fixture = create_fixture(
                root,
                Path(directory),
                project,
                image=f"{project}:local",
                public_port=public_port,
                admin_port=admin_port,
            )
            owned = False
            try:
                _progress("validating synthetic boundaries")
                validate_fixture_models(fixture)
                node = _node(fixture)
                environment = _browser_environment(fixture, node)
                _fresh(fixture)
                owned = True
                _progress("building the current local application image")
                context = _build_context(fixture)
                _docker(
                    fixture,
                    "build",
                    "--target",
                    "runtime",
                    "--tag",
                    fixture.image,
                    str(context),
                    stage="image build",
                    timeout=600,
                )
                _progress("starting the six-service disposable stack")
                _compose(
                    fixture,
                    "up",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "--no-build",
                    stage="stack startup",
                    timeout=240,
                )
                _runtime(fixture)
                _progress("publishing through the ordinary HTML form")
                _browser(fixture, node, environment, "publish")
                _progress("restarting runtime containers and checking publication persistence")
                _restart(fixture)
                _browser(fixture, node, environment, "published")
                _progress("suppressing through the existing browser dialog")
                _browser(fixture, node, environment, "suppress")
                _progress("restarting runtime containers and checking suppression persistence")
                _restart(fixture)
                _browser(fixture, node, environment, "suppressed")
                _runtime(fixture)
                if (
                    _database(
                        fixture,
                        "SELECT string_agg(to_status,',' ORDER BY occurred_at,id) FROM primary_signal.publication_events",
                    )
                    != "draft,validated,published,suppressed"
                ):
                    raise SmokeFailure("publication audit history")
                status = 0
            except SmokeFailure as error:
                _progress(
                    f"failed during {error}; command output and fixture configuration were withheld"
                )
            except FixtureBoundaryError, OSError, ValueError, subprocess.SubprocessError:
                _progress("failed; command output and fixture configuration were withheld")
            finally:
                if owned:
                    _progress(
                        "removing only this run's containers, volume, networks, and local image"
                    )
                    try:
                        _cleanup(fixture)
                    except SmokeFailure:
                        _progress("cleanup failed")
                        status = 1
    except FixtureBoundaryError, OSError, ValueError, subprocess.SubprocessError:
        status = 1
        _progress("fixture preparation failed")
    if status == 0:
        _progress(
            "passed publish/read/suppress and two persisted restarts; all run resources removed"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
