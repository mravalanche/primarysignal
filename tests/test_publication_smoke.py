"""Boundary and persistence guards for the real disposable publication smoke."""

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

    def containers(_fixture: PublicationSmokeFixture) -> dict[str, tuple[str, str]]:
        return next(snapshots)

    def volume(_fixture: PublicationSmokeFixture) -> str:
        return "same-volume"

    def compose(_fixture: PublicationSmokeFixture, *_args: str, **_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(smoke, "_containers", containers)
    monkeypatch.setattr(smoke, "_volume", volume)
    monkeypatch.setattr(smoke, "_compose", compose)
    if unchanged:
        with pytest.raises(smoke.SmokeFailure, match="runtime services did not restart"):
            smoke._restart(fixture)  # pyright: ignore[reportPrivateUsage]
    else:
        smoke._restart(fixture)  # pyright: ignore[reportPrivateUsage]


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
