"""Boundary and persistence guards for the real disposable publication smoke."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from scripts import smoke_publication as smoke
from scripts.publication_smoke_fixture import PublicationSmokeFixture


def _fixture(root: Path, directory: Path) -> PublicationSmokeFixture:
    return cast(
        PublicationSmokeFixture,
        SimpleNamespace(
            root=root,
            directory=directory,
            env={"PATH": os.defpath},
            project="synthetic-project",
            image="synthetic-project:local",
            docker_args=("docker",),
            public_port=18002,
            admin_port=18443,
        ),
    )


def _tracked_root(tmp_path: Path) -> tuple[Path, Path]:
    root, directory = tmp_path / "checkout", tmp_path / "fixture"
    root.mkdir()
    directory.mkdir()
    git = shutil.which("git", path=os.defpath)
    assert git is not None
    environment = {
        "PATH": os.defpath,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    subprocess.run(  # noqa: S603 - fixed local Git operation in a synthetic temporary checkout.
        [git, "init", "--quiet", str(root)], check=True, capture_output=True, env=environment
    )
    for name in smoke.BUILD_PATHS[:8]:
        (root / name).write_text("public synthetic fixture\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src/public.py").write_text("# public fixture\n", encoding="utf-8")
    subprocess.run([git, "add", "."], cwd=root, check=True, capture_output=True, env=environment)  # noqa: S603
    return root, directory


def test_build_context_excludes_untracked_operator_and_fixture_files(tmp_path: Path) -> None:
    root, directory = _tracked_root(tmp_path)
    (root / "src/operator-private.txt").write_text("synthetic private marker", encoding="utf-8")
    (root / ".env.operator").write_text("synthetic private marker", encoding="utf-8")
    (directory / "admin.env").write_text("synthetic private marker", encoding="utf-8")
    context = smoke._build_context(_fixture(root, directory))  # pyright: ignore[reportPrivateUsage]
    assert (context / "src/public.py").is_file()
    assert not (context / "src/operator-private.txt").exists()
    assert not (context / ".env.operator").exists()
    assert not (context / "admin.env").exists()


def test_build_context_rejects_a_tracked_symlink(tmp_path: Path) -> None:
    root, directory = _tracked_root(tmp_path)
    outside = tmp_path / "synthetic-operator-file"
    outside.write_text("synthetic private marker", encoding="utf-8")
    (root / "src/public.py").unlink()
    (root / "src/public.py").symlink_to(outside)
    with pytest.raises(smoke.SmokeFailure, match="tracked build context"):
        smoke._build_context(_fixture(root, directory))  # pyright: ignore[reportPrivateUsage]


def test_command_error_does_not_reflect_child_output(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, tmp_path)
    with pytest.raises(smoke.SmokeFailure, match=r"^synthetic stage$") as raised:
        smoke._command(  # pyright: ignore[reportPrivateUsage]
            fixture,
            [sys.executable, "-c", "import sys;print('private-fixture-marker');sys.exit(1)"],
            stage="synthetic stage",
        )
    assert "private-fixture-marker" not in str(raised.value)


@pytest.mark.parametrize("unchanged", [True, False])
def test_restart_requires_new_runtime_start_times_and_retains_one_shots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unchanged: bool
) -> None:
    fixture = _fixture(tmp_path, tmp_path)
    services = (*smoke.RUNTIME, "migrate", "seed")
    before = {
        name: (name, "first|running|0" if name in smoke.RUNTIME else "first|exited|0")
        for name in services
    }
    after = {
        name: (identifier, state if unchanged or name not in smoke.RUNTIME else "second|running|0")
        for name, (identifier, state) in before.items()
    }
    snapshots = iter((before, after))
    commands: list[tuple[str, ...]] = []
    waits: list[tuple[str, bool]] = []

    def containers(_fixture: PublicationSmokeFixture) -> dict[str, tuple[str, str]]:
        return next(snapshots)

    def volume(_fixture: PublicationSmokeFixture) -> str:
        return "same-volume"

    def compose(_fixture: PublicationSmokeFixture, *args: str, **_kwargs: object) -> str:
        assert args[0] == "stop", "Compose must not start completed dependencies"
        return ""

    def docker(
        _fixture: PublicationSmokeFixture, *args: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess([], 0, "", "")

    def wait_runtime(
        _fixture: PublicationSmokeFixture, identifier: str, *, require_health: bool
    ) -> None:
        waits.append((identifier, require_health))

    def runtime_bindings(
        _fixture: PublicationSmokeFixture, _containers: dict[str, tuple[str, str]]
    ) -> None:
        return None

    monkeypatch.setattr(smoke, "_containers", containers)
    monkeypatch.setattr(smoke, "_volume", volume)
    monkeypatch.setattr(smoke, "_compose", compose)
    monkeypatch.setattr(smoke, "_docker", docker)
    monkeypatch.setattr(smoke, "_wait_runtime", wait_runtime)
    monkeypatch.setattr(smoke, "_runtime_bindings", runtime_bindings)
    if unchanged:
        with pytest.raises(smoke.SmokeFailure, match="runtime services did not restart"):
            smoke._restart(fixture)  # pyright: ignore[reportPrivateUsage]
    else:
        smoke._restart(fixture)  # pyright: ignore[reportPrivateUsage]
    assert commands == [("start", "database"), ("start", "public", "admin"), ("start", "admin_tls")]
    assert waits == [("database", True), ("public", True), ("admin", False), ("admin_tls", False)]


def test_runtime_health_wait_is_bounded_without_starting_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = _fixture(tmp_path, tmp_path)
    now = [0.0]
    probes: list[str] = []
    monkeypatch.setattr(smoke, "monotonic", lambda: now[0])

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def docker(
        _fixture: PublicationSmokeFixture, *args: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert args[:3] == (
            "inspect",
            "--format",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
        )
        assert kwargs["timeout"] == 1
        probes.append(args[3])
        return subprocess.CompletedProcess([], 0, "running|starting", "")

    monkeypatch.setattr(smoke, "sleep", sleep)
    monkeypatch.setattr(smoke, "_docker", docker)
    with pytest.raises(smoke.SmokeFailure, match="runtime readiness"):
        smoke._wait_runtime(fixture, "owned-container", require_health=True, timeout=1)  # pyright: ignore[reportPrivateUsage]
    assert now[0] == 1
    assert probes == ["owned-container"] * 4


@pytest.mark.parametrize(
    ("service", "ports"),
    [
        ("public", None),
        ("public", {"8000/tcp": None}),
        ("public", {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18002"}]}),  # noqa: S104 - rejected fixture, no actual listener.
        ("public", {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19002"}]}),
        (
            "public",
            {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18002"}],
                "9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19000"}],
            },
        ),
        ("admin", {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18443"}]}),
        ("database", {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "15432"}]}),
    ],
)
def test_runtime_checks_effective_missing_misbound_and_unexpected_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service: str, ports: object
) -> None:
    fixture = _fixture(tmp_path, tmp_path)
    models: dict[str, object] = {
        "public": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18002"}]},
        "admin": {"8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18443"}]},
        "database": {"5432/tcp": None},
    }
    models[service] = ports

    def docker(
        _fixture: PublicationSmokeFixture, *args: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert args[:3] == ("inspect", "--format", "{{json .NetworkSettings.Ports}}")
        return subprocess.CompletedProcess([], 0, json.dumps(models[args[3]]), "")

    monkeypatch.setattr(smoke, "_docker", docker)
    containers = {name: (name, "started|running|0") for name in models}
    with pytest.raises(smoke.SmokeFailure, match="published runtime ports"):
        smoke._runtime_bindings(fixture, containers)  # pyright: ignore[reportPrivateUsage]


def test_runtime_allows_null_image_expose_entries_without_publishing_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = _fixture(tmp_path, tmp_path)
    models = {
        "public": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18002"}], "8001/tcp": None},
        "admin": {"8000/tcp": None, "8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18443"}]},
        "database": {"5432/tcp": None},
    }

    def docker(
        _fixture: PublicationSmokeFixture, *args: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert args[:3] == ("inspect", "--format", "{{json .NetworkSettings.Ports}}")
        return subprocess.CompletedProcess([], 0, json.dumps(models[args[3]]), "")

    monkeypatch.setattr(smoke, "_docker", docker)
    containers = {name: (name, "started|running|0") for name in models}
    smoke._runtime_bindings(fixture, containers)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("phase", ["fresh", "cleanup"])
def test_resource_guards_cover_all_four_project_networks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    fixture = _fixture(tmp_path, tmp_path)
    networks: set[str] = set()

    def exists(_fixture: PublicationSmokeFixture, kind: str, name: str) -> bool:
        if kind == "network":
            networks.add(name)
        return False

    def docker(
        _fixture: PublicationSmokeFixture, *args: str, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "1.53" if args[0] == "version" else "", "")

    def compose(*_args: object, **_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(smoke, "_exists", exists)
    monkeypatch.setattr(smoke, "_docker", docker)
    monkeypatch.setattr(smoke, "_compose", compose)
    if phase == "fresh":
        smoke._fresh(fixture)  # pyright: ignore[reportPrivateUsage]
    else:
        smoke._cleanup(fixture)  # pyright: ignore[reportPrivateUsage]
    assert networks == {
        f"synthetic-project_{name}"
        for name in ("public_db", "admin_db", "public_ingress", "admin_ingress")
    }


def test_failed_cleanup_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _fixture(tmp_path, tmp_path)
    monkeypatch.setattr(sys, "argv", ["smoke_publication"])

    def create(*_args: object, **_kwargs: object) -> PublicationSmokeFixture:
        return fixture

    def successful(*_args: object, **_kwargs: object) -> None:
        return None

    def node(_fixture: PublicationSmokeFixture) -> Path:
        return Path("/synthetic-node")

    def environment(_fixture: PublicationSmokeFixture, _node: Path) -> dict[str, str]:
        return {}

    def context(_fixture: PublicationSmokeFixture) -> Path:
        return tmp_path

    def command(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "", "")

    def compose(*_args: object, **_kwargs: object) -> str:
        return ""

    def database(_fixture: PublicationSmokeFixture, _query: str) -> str:
        return "draft,validated,published,suppressed"

    def cleanup(_fixture: PublicationSmokeFixture) -> None:
        raise smoke.SmokeFailure("cleanup")

    monkeypatch.setattr(smoke, "create_fixture", create)
    for name in ("validate_fixture_models", "_fresh", "_runtime", "_browser", "_restart"):
        monkeypatch.setattr(smoke, name, successful)
    monkeypatch.setattr(smoke, "_node", node)
    monkeypatch.setattr(smoke, "_browser_environment", environment)
    monkeypatch.setattr(smoke, "_build_context", context)
    monkeypatch.setattr(smoke, "_docker", command)
    monkeypatch.setattr(smoke, "_compose", compose)
    monkeypatch.setattr(smoke, "_database", database)
    monkeypatch.setattr(smoke, "_cleanup", cleanup)
    assert smoke.main() == 1
    output = capsys.readouterr().out
    assert "cleanup failed" in output
    assert "passed publish/read/suppress" not in output
