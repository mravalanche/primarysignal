"""Synthetic Compose boundary checks without Docker, OpenSSL or operator files."""

import json
import stat
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from argon2 import PasswordHasher
from scripts import check_publication_compose as checker
from scripts import publication_smoke_fixture as fixtures

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> fixtures.PublicationSmokeFixture:
    def tls(directory: Path, _environment: dict[str, str]) -> None:
        for name in ("admin_tls_certificate.pem", "admin_tls_key.pem"):
            (directory / name).write_text("synthetic TLS placeholder for model tests only\n")
            (directory / name).chmod(0o444)

    monkeypatch.setattr(fixtures, "_generate_tls", tls)
    monkeypatch.setattr(fixtures.shutil, "which", Mock(return_value="/usr/bin/docker"))
    return fixtures.create_fixture(
        ROOT,
        tmp_path / "fixture",
        "primary-signal-fixture-contract",
        image="primary-signal-publication-smoke:contract",
        public_port=18002,
        admin_port=18443,
    )


def model(fixture: fixtures.PublicationSmokeFixture, *, unresolved: bool = False) -> dict[str, Any]:
    # A sanitized normalized model captured from the tracked Compose bundle,
    # rather than a second implementation of its overlay generator.
    content = (ROOT / "tests/fixtures/publication-compose.synthetic.json").read_text()
    result = cast(
        dict[str, Any],
        json.loads(
            content.replace("${CHECKOUT}", str(fixture.root)).replace(
                "${FIXTURE}", str(fixture.directory)
            )
        ),
    )
    for name, values in fixture.expected_environments.items():
        result["services"][name]["environment"] = dict(values)
        if name == "database":
            result["services"][name]["environment"]["POSTGRES_PASSWORD_FILE"] = (
                "/run/secrets/database_password"  # noqa: S105 - container secret path.
            )
        if unresolved:
            result["services"][name]["env_file"] = [
                {"path": str(fixture.directory / f"{name}.env")}
            ]
    return result


def test_fixture_ignores_ambient_configuration_and_keeps_auth_literal(
    fixture: fixtures.PublicationSmokeFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert stat.S_IMODE(fixture.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((fixture.directory / "admin.env").stat().st_mode) == 0o600
    assert stat.S_IMODE((fixture.directory / "admin_tls_key.pem").stat().st_mode) == 0o444
    assert fixture.env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert "HOME" not in fixture.env
    assert "--env-file" in fixture.compose_args and "/dev/null" in fixture.compose_args
    assert "--config" in fixture.docker_args
    assert list((fixture.directory / "docker-config").iterdir()) == []
    admin = (fixture.directory / "admin.env").read_text()
    assert f"PRIMARY_SIGNAL_ADMIN_PASSWORD_PHC='{fixtures.ADMIN_PHC}'" in admin
    assert PasswordHasher().verify(fixtures.ADMIN_PHC, fixture.admin_password)
    assert fixtures.MIGRATION_ROLE not in admin and fixtures.BOOTSTRAP_ROLE not in admin
    assert fixtures.MIGRATION_ROLE not in (fixture.directory / "public.env").read_text()
    login = (fixture.directory / "bootstrap-logins.sql").read_text()
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE" in login
    assert "GRANT primary_signal_public_owner TO migration_test" in login
    for role, capability in fixtures.RUNTIME_ROLES.items():
        assert f"GRANT {capability} TO {role};" in login


def test_model_accepts_expected_isolation_and_literal_phc_output(
    fixture: fixtures.PublicationSmokeFixture,
) -> None:
    config = model(fixture)
    fixtures.validate_resolved_model(fixture, config)
    config["services"]["admin"]["environment"]["PRIMARY_SIGNAL_ADMIN_PASSWORD_PHC"] = (
        fixtures.ADMIN_PHC.replace("$", "$$")
    )
    fixtures.validate_resolved_model(fixture, config)


@pytest.mark.parametrize("location", ["environment", "secret", "bind"])
def test_unresolved_guard_rejects_operator_or_host_paths(
    fixture: fixtures.PublicationSmokeFixture, location: str
) -> None:
    config = model(fixture, unresolved=True)
    if location == "environment":
        config["services"]["public"]["env_file"][0]["path"] = str(ROOT / ".env.publication.public")
    elif location == "secret":
        config["secrets"]["admin_tls_key"]["file"] = str(
            ROOT / ".secrets/publication/admin_tls_key.pem"
        )
    else:
        config["services"]["database"]["volumes"][1]["source"] = "/var/run/docker.sock"
    with pytest.raises(fixtures.FixtureBoundaryError, match="escapes the synthetic fixture"):
        fixtures.validate_unresolved_model(fixture, config)


def test_unresolved_guard_rejects_symlink_escape(
    fixture: fixtures.PublicationSmokeFixture, tmp_path: Path
) -> None:
    target = tmp_path / "outside-fixture.env"
    target.write_text("synthetic outside placeholder\n")
    path = fixture.directory / "public.env"
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(fixtures.FixtureBoundaryError, match="escapes the synthetic fixture"):
        fixtures.validate_unresolved_model(fixture, model(fixture, unresolved=True))


@pytest.mark.parametrize(
    ("target", "key", "value", "message"),
    [
        ("network", "external", True, "private and internal"),
        ("network", "name", "another-project_public_db", "private and internal"),
        ("network", "driver", "host", "private and internal"),
        ("volume", "external", True, "belong to this project"),
        ("volume", "name", "another-project_database", "belong to this project"),
        (
            "public",
            "ports",
            [{"host_ip": "192.0.2.10", "published": "18002", "target": 8000, "protocol": "tcp"}],
            "loopback only",
        ),
        ("public", "read_only", False, "hardening changed"),
        ("public", "cap_drop", [], "hardening changed"),
        (
            "public",
            "security_opt",
            ["no-new-privileges:true", "seccomp:unconfined"],
            "hardening changed",
        ),
        ("public", "pids_limit", 0, "hardening changed"),
        ("public", "init", False, "hardening changed"),
        ("public", "tmpfs", [], "hardening changed"),
        ("public", "user", "0", "unprivileged user"),
        ("admin_tls", "user", "0:0", "unprivileged user"),
        (
            "public",
            "secrets",
            [{"source": "database_password", "target": "/run/secrets/database_password"}],
            "secret mounts changed",
        ),
        ("public", "volumes_from", ["database"], "unsafe synthetic service option"),
        ("public", "network_mode", "host", "service network changed"),
        ("public", "image", "public.example/external:tag", "image changed"),
        ("public", "build", {"context": "/synthetic/build"}, "current local image"),
    ],
)
def test_resolved_guard_rejects_boundary_changes(
    fixture: fixtures.PublicationSmokeFixture, target: str, key: str, value: object, message: str
) -> None:
    config = model(fixture)
    section = (
        config["networks"]["public_db"]
        if target == "network"
        else config["volumes"]["publication-data"]
        if target == "volume"
        else config["services"][target]
    )
    section[key] = value
    with pytest.raises(fixtures.FixtureBoundaryError, match=message):
        fixtures.validate_resolved_model(fixture, config)


@pytest.mark.parametrize(
    "key",
    [
        "PRIMARY_SIGNAL_DATABASE_URL",
        "PRIMARY_SIGNAL_ADMIN_PASSWORD_PHC",
        "PRIMARY_SIGNAL_ADMIN_SESSION_KEY_HEX",
        "PRIMARY_SIGNAL_ADMIN_ORIGIN",
    ],
)
def test_resolved_guard_rejects_altered_admin_identity(
    fixture: fixtures.PublicationSmokeFixture, key: str
) -> None:
    config = model(fixture)
    config["services"]["admin"]["environment"][key] = "synthetic-invalid-value"
    with pytest.raises(fixtures.FixtureBoundaryError, match="runtime environment changed"):
        fixtures.validate_resolved_model(fixture, config)


def test_invalid_raw_paths_stop_before_any_env_resolution(
    fixture: fixtures.PublicationSmokeFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = model(fixture, unresolved=True)
    config["services"]["admin"]["env_file"][0]["path"] = str(ROOT / ".env.publication.admin")
    render = Mock(return_value=config)
    monkeypatch.setattr(fixtures, "_render", render)
    with pytest.raises(fixtures.FixtureBoundaryError):
        fixtures.validate_fixture_models(fixture)
    assert render.call_count == 1
    assert render.call_args.kwargs == {"unresolved": True}


def test_constructor_never_reads_operator_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.read_text

    def guarded(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name.startswith(".env.publication.") and not path.name.endswith(".example"):
            pytest.fail("operator environment file was read")
        if ".secrets" in path.parts:
            pytest.fail("operator secret file was read")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    monkeypatch.setattr(fixtures, "_generate_tls", Mock())
    monkeypatch.setattr(fixtures.shutil, "which", Mock(return_value="/usr/bin/docker"))
    fixture = fixtures.create_fixture(
        ROOT,
        tmp_path / "private",
        "synthetic-no-operator",
        image="synthetic:local",
        public_port=18002,
        admin_port=18443,
    )
    assert fixture.directory.exists()


def test_checker_uses_private_placeholders_and_ignores_host_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "ssh://public.example")
    monkeypatch.setenv("COMPOSE_FILE", "/synthetic/operator-compose.yaml")
    monkeypatch.setenv("COMPOSE_ENV_FILES", "/synthetic/operator.env")
    monkeypatch.setenv("PRIMARY_SIGNAL_PUBLICATION_IMAGE", "public.example/operator:tag")
    captured: dict[str, Any] = {}

    def run(args: list[str], **kwargs: Any) -> Any:
        captured.update(args=args, kwargs=kwargs)
        copied = Path(args[args.index("-f") + 1]).read_text()
        assert ".secrets/publication/" not in copied
        assert "env_file: .env.publication.public.example" in copied
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps({"services": {}, "networks": {}, "secrets": {}}),
            },
        )()

    monkeypatch.setattr(checker.subprocess, "run", run)
    monkeypatch.setattr(fixtures.shutil, "which", Mock(return_value="/usr/bin/docker"))
    checker.render_model(ROOT, "compose.publication.yaml", tmp_path)
    args = captured["args"]
    assert args[args.index("--host") + 1] == "unix:///var/run/docker.sock"
    assert args[args.index("--env-file") + 1] == "/dev/null"
    environment = captured["kwargs"]["env"]
    assert "COMPOSE_FILE" not in environment and "COMPOSE_ENV_FILES" not in environment
    assert environment["PRIMARY_SIGNAL_PUBLICATION_IMAGE"] == checker.IMAGE_DIGEST
    assert environment["DOCKER_CONFIG"] == str(tmp_path / "docker-config")
