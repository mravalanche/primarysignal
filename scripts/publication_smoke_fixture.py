"""Private, synthetic-only configuration for the disposable publication smoke.

The fixed credentials below are public test fixtures. This module never reads
operator environment files, local login SQL, registry credentials or TLS keys.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

DATABASE = "primary_signal_publication_test"
BOOTSTRAP_ROLE = "bootstrap_owner_test"
MIGRATION_ROLE = "migration_test"
ADMIN_PASSWORD = "synthetic-publication-smoke-password"  # noqa: S105  # pragma: allowlist secret
ADMIN_PHC = "$argon2id$v=19$m=65536,t=3,p=4$cHVibGljLWZpeHR1cmUtc2FsdA$aCY9JUKXcY/QZQO+8pj/TSeVR04dARxFHvRWMLJ/jAg"  # pragma: allowlist secret
SESSION_KEY = "12" * 32  # pragma: allowlist secret
BOOTSTRAP_PASSWORD = "public_bootstrap_fixture_only"  # noqa: S105  # pragma: allowlist secret
RUNTIME_ROLES = {
    "public_reader_test": "primary_signal_cap_public_read",
    "publication_writer_test": "primary_signal_cap_publication_write",
    "admin_session_test": "primary_signal_cap_admin_session",
    "editorial_reader_test": "primary_signal_cap_editorial_read",
    "publication_decision_test": "primary_signal_cap_publication_decision",
}
RUNTIME_SERVICES = ("migrate", "seed", "public", "admin")
NETWORKS = ("public_db", "admin_db", "public_ingress", "admin_ingress")
SERVICES = {"database", *RUNTIME_SERVICES, "admin_tls"}
ENV_SERVICES = ("database", *RUNTIME_SERVICES)
INIT_FILES = (
    "010_queue_capability_roles.sql",
    "020_feed_scheduler_capability_role.sql",
    "030_feed_poll_capability_role.sql",
    "040_article_persist_capability_role.sql",
    "050_article_inventory_capability_role.sql",
    "060_public_projection_roles.sql",
    "070_publication_writer_capability_role.sql",
    "080_source_health_capability_role.sql",
    "090_editorial_read_capability_role.sql",
    "100_admin_session_capability_role.sql",
    "110_publication_decision_capability_role.sql",
    "120_retention_cleanup_capability_role.sql",
    "130_job_health_capability_role.sql",
    "140_source_control_capability_role.sql",
)


class FixtureBoundaryError(RuntimeError):
    """A fixed-message failure that does not print configuration values."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise FixtureBoundaryError(message)


def local_docker_context(directory: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    """Ignore inherited Docker contexts, credentials, endpoints and Compose input."""
    config = directory / "docker-config"
    config.mkdir(mode=0o700, exist_ok=True)
    docker = shutil.which("docker", path=os.defpath)
    if docker is None:
        raise FixtureBoundaryError("docker compose is required for the publication smoke")
    environment = {
        "PATH": os.defpath,
        "DOCKER_CONFIG": str(config),
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "COMPOSE_DISABLE_ENV_FILE": "1",
    }
    return (docker, "--host", "unix:///var/run/docker.sock", "--config", str(config)), environment


def _write(path: Path, content: str, *, mode: int = 0o600) -> None:
    require(not path.exists() and not path.is_symlink(), "synthetic fixture path already exists")
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _literal_env(values: dict[str, str]) -> str:
    # Single quotes suppress Compose interpolation of the literal Argon2 PHC.
    require(
        all("'" not in value and "\n" not in value for value in values.values()),
        "invalid synthetic environment value",
    )
    return "".join(f"{key}='{value}'\n" for key, value in values.items())


def _url(role: str) -> str:
    return f"postgresql+psycopg://{role}:public_{role}_fixture_only@database:5432/{DATABASE}"  # pragma: allowlist secret


def _database_settings(role: str, prefix: str = "PRIMARY_SIGNAL") -> dict[str, str]:
    return {f"{prefix}_DATABASE_URL": _url(role), f"{prefix}_DATABASE_EXPECTED_ROLE": role}


def _login_sql() -> str:
    # All names and passwords originate from fixed public test constants.
    statements = ["-- PUBLIC SYNTHETIC FIXTURES; NEVER USE OUTSIDE THIS DISPOSABLE SMOKE.\n"]
    for role in (MIGRATION_ROLE, *RUNTIME_ROLES):
        statements.append(
            f"CREATE ROLE {role} LOGIN PASSWORD 'public_{role}_fixture_only' "  # pragma: allowlist secret
            "NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS;\n"
        )
    statements.extend(
        (
            f"GRANT CREATE ON DATABASE {DATABASE} TO {MIGRATION_ROLE}, primary_signal_public_owner;\n",
            f"GRANT primary_signal_public_owner TO {MIGRATION_ROLE};\n",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n",
        )
    )
    statements.extend(
        f"GRANT {capability} TO {role};\n" for role, capability in RUNTIME_ROLES.items()
    )
    return "".join(statements)


def _generate_tls(directory: Path, environment: dict[str, str]) -> None:
    openssl = shutil.which("openssl", path=os.defpath)
    if openssl is None:
        raise FixtureBoundaryError("openssl is required for the local synthetic TLS fixture")
    result = subprocess.run(  # noqa: S603 - fixed local OpenSSL operation and private output paths.
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(directory / "admin_tls_key.pem"),
            "-out",
            str(directory / "admin_tls_certificate.pem"),
        ],
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )
    require(result.returncode == 0, "synthetic TLS generation failed")
    for filename in ("admin_tls_key.pem", "admin_tls_certificate.pem"):
        # The enclosing directory stays owner-only; nginx reads only its two
        # individual Compose secret mounts as UID 101.
        (directory / filename).chmod(0o444)


@dataclass(frozen=True)
class PublicationSmokeFixture:
    root: Path
    directory: Path
    project: str
    image: str
    public_port: int
    admin_port: int
    docker_args: tuple[str, ...]
    compose_args: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    expected_environments: dict[str, dict[str, str]] = field(repr=False)
    public_origin: str
    admin_origin: str
    admin_password: str = field(default=ADMIN_PASSWORD, repr=False)
    expected_slug: str = "synthetic-security-notice"
    expected_headline: str = "Synthetic security notice"
    database_name: str = DATABASE
    bootstrap_role: str = BOOTSTRAP_ROLE
    migration_role: str = MIGRATION_ROLE


def create_fixture(
    root: Path, directory: Path, project: str, *, image: str, public_port: int, admin_port: int
) -> PublicationSmokeFixture:
    root = root.resolve()
    directory = directory.resolve()
    require(
        directory.is_relative_to(
            Path("/tmp")  # noqa: S108 - only a parent boundary, never a predictable created path.
        )
        and not directory.is_relative_to(root),
        "synthetic fixture must be outside the checkout in /tmp",
    )
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project), "invalid synthetic Compose project name"
    )
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9_.-]*:[a-z0-9][a-z0-9_.-]*", image),
        "synthetic smoke requires a tagged local image",
    )
    require(
        type(public_port) is int
        and type(admin_port) is int
        and 1024 <= public_port <= 65535
        and 1024 <= admin_port <= 65535
        and public_port != admin_port,
        "invalid synthetic loopback ports",
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not any(directory.iterdir()), "synthetic fixture directory must be empty")
    directory.chmod(0o700)
    docker_args, environment = local_docker_context(directory)
    environment["PRIMARY_SIGNAL_PUBLICATION_PUBLIC_PORT"] = str(public_port)
    environment["PRIMARY_SIGNAL_PUBLICATION_ADMIN_PORT"] = str(admin_port)
    public_origin = f"http://127.0.0.1:{public_port}"
    admin_origin = f"https://localhost:{admin_port}"
    production = {
        "PRIMARY_SIGNAL_ENVIRONMENT": "production",
        "PRIMARY_SIGNAL_DEBUG": "false",
        "PRIMARY_SIGNAL_API_DOCS_ENABLED": "false",
    }
    values = {
        "database": {"POSTGRES_DB": DATABASE, "POSTGRES_USER": BOOTSTRAP_ROLE},
        "migrate": _database_settings(MIGRATION_ROLE),
        "seed": {
            **_database_settings(MIGRATION_ROLE, "PRIMARY_SIGNAL_TEST"),
            **_database_settings("publication_writer_test", "PRIMARY_SIGNAL_TEST_PUBLICATION"),
        },
        "public": {**production, **_database_settings("public_reader_test")},
        "admin": {
            **production,
            **_database_settings("admin_session_test"),
            **_database_settings("editorial_reader_test", "PRIMARY_SIGNAL_ADMIN_EDITORIAL"),
            **_database_settings("publication_decision_test", "PRIMARY_SIGNAL_ADMIN_PUBLICATION"),
            "PRIMARY_SIGNAL_ADMIN_ORIGIN": admin_origin,
            "PRIMARY_SIGNAL_ADMIN_PASSWORD_PHC": ADMIN_PHC,
            "PRIMARY_SIGNAL_ADMIN_SESSION_KEY_HEX": SESSION_KEY,
            "PRIMARY_SIGNAL_ADMIN_DECISION_ACTOR": "synthetic smoke operator",
        },
    }
    for name, settings in values.items():
        _write(directory / f"{name}.env", _literal_env(settings))
    _write(directory / "database_password", BOOTSTRAP_PASSWORD + "\n", mode=0o444)
    _write(directory / "bootstrap-logins.sql", _login_sql(), mode=0o444)
    _generate_tls(directory, environment)
    overlay = ["services:\n"]
    for name in ENV_SERVICES:
        overlay.extend(
            (
                f"  {name}:\n",
                f"    env_file: !override [{json.dumps(str(directory / f'{name}.env'))}]\n",
            )
        )
        if name in RUNTIME_SERVICES:
            overlay.extend(
                ("    build: !reset null\n", f"    image: {image}\n", "    pull_policy: never\n")
            )
        if name in {"public", "admin"}:
            port = public_port if name == "public" else admin_port
            target = 8000 if name == "public" else 8443
            overlay.append(f'    ports: !override ["127.0.0.1:{port}:{target}"]\n')
        if name == "database":
            overlay.extend(
                ("    volumes: !override\n", "      - publication-data:/var/lib/postgresql\n")
            )
            for filename in INIT_FILES:
                binding = (
                    str(root / "deploy/postgres/initdb" / filename)
                    + f":/docker-entrypoint-initdb.d/{filename}:ro"
                )
                overlay.append(f"      - {json.dumps(binding)}\n")
            overlay.append(
                f"      - {json.dumps(str(directory / 'bootstrap-logins.sql') + ':/docker-entrypoint-initdb.d/900_local_logins.sql:ro')}\n"
            )
    overlay.append("secrets:\n")
    for key, filename in (
        ("database_password", "database_password"),
        ("admin_tls_certificate", "admin_tls_certificate.pem"),
        ("admin_tls_key", "admin_tls_key.pem"),
    ):
        overlay.append(f"  {key}:\n    file: {json.dumps(str(directory / filename))}\n")
    path = directory / "compose.synthetic.yaml"
    _write(path, "".join(overlay))
    compose_args = (
        "compose",
        "--project-directory",
        str(root),
        "--project-name",
        project,
        "--env-file",
        "/dev/null",
        "-f",
        str(root / "compose.publication.yaml"),
        "-f",
        str(path),
    )
    return PublicationSmokeFixture(
        root,
        directory,
        project,
        image,
        public_port,
        admin_port,
        docker_args,
        compose_args,
        environment,
        values,
        public_origin,
        admin_origin,
    )


def approved_assets(root: Path) -> set[Path]:
    root = root.resolve()
    init = root / "deploy/postgres/initdb"
    require(init.resolve().is_relative_to(root), "tracked bootstrap mount escapes the checkout")
    require(init.is_dir() and not init.is_symlink(), "invalid tracked PostgreSQL bootstrap mount")
    scripts = {(init / name).resolve() for name in INIT_FILES}
    require(
        all(path.is_relative_to(root) for path in scripts),
        "tracked bootstrap file escapes the checkout",
    )
    require(
        all((init / name).is_file() and not (init / name).is_symlink() for name in INIT_FILES),
        "invalid PostgreSQL bootstrap file",
    )
    nginx = root / "deploy/publication/admin-nginx.conf"
    require(nginx.resolve().is_relative_to(root), "tracked nginx mount escapes the checkout")
    require(nginx.is_file() and not nginx.is_symlink(), "invalid tracked nginx mount")
    return scripts | {nginx.resolve()}


def _fixture_path(
    fixture: PublicationSmokeFixture, value: object, *, approved: set[Path] | None = None
) -> Path:
    require(isinstance(value, str), "invalid synthetic configuration path")
    path = Path(cast(str, value))
    require(path.is_absolute(), "synthetic configuration paths must be absolute")
    resolved = path.resolve()
    require(
        resolved.is_relative_to(fixture.directory) or resolved in (approved or set()),
        "configuration path escapes the synthetic fixture",
    )
    return resolved


def validate_unresolved_model(
    fixture: PublicationSmokeFixture, model: dict[str, Any], *, resolved: bool = False
) -> None:
    """Guard every file/bind reference before Compose reads any environment file."""
    approved = approved_assets(fixture.root)
    services = model.get("services", {})
    require(set(services) == SERVICES, "synthetic Compose service set changed")
    for name, service in services.items():
        env_files = service.get("env_file", [])
        require(
            len(env_files) == (1 if name in ENV_SERVICES and not resolved else 0),
            "synthetic environment file count changed",
        )
        for env_file in env_files:
            source = (
                cast(dict[str, Any], env_file).get("path")
                if isinstance(env_file, dict)
                else env_file
            )
            path = _fixture_path(fixture, source)
            require(path == fixture.directory / f"{name}.env", "synthetic environment file changed")
        for volume in service.get("volumes", []):
            require(volume.get("type") in {"bind", "volume"}, "unsupported synthetic mount")
            if volume.get("type") == "bind":
                _fixture_path(fixture, volume.get("source"), approved=approved)
    secrets = model.get("secrets", {})
    require(
        set(secrets) == {"database_password", "admin_tls_certificate", "admin_tls_key"},
        "synthetic secret set changed",
    )
    for name, value in secrets.items():
        require(set(value) <= {"file", "name"}, "external synthetic secrets are forbidden")
        filename = name if name == "database_password" else f"{name}.pem"
        require(
            _fixture_path(fixture, value.get("file")) == fixture.directory / filename,
            "synthetic secret path changed",
        )
    # Extension/config mounts must not introduce additional file reads.
    require(not model.get("configs"), "synthetic Compose configs are forbidden")
    require(not model.get("include"), "synthetic Compose includes are forbidden")


def _network_names(service: dict[str, Any]) -> set[str]:
    return set(service.get("networks", {}))


def validate_resolved_model(fixture: PublicationSmokeFixture, model: dict[str, Any]) -> None:
    """Check runtime identities and isolation after the file-boundary guard passes."""
    validate_unresolved_model(fixture, model, resolved=True)
    services = model["services"]
    networks = model.get("networks", {})
    require(set(networks) == set(NETWORKS), "synthetic network set changed")
    require(
        all(
            bool(value.get("internal", False)) == (name in {"public_db", "admin_db"})
            and not value.get("external")
            and value.get("driver") in {None, "bridge"}
            and not value.get("driver_opts")
            and not value.get("ipam")
            and value.get("name") == f"{fixture.project}_{name}"
            for name, value in networks.items()
        ),
        "synthetic network isolation changed",
    )
    volumes = model.get("volumes", {})
    require(set(volumes) == {"publication-data"}, "synthetic volume set changed")
    volume = volumes["publication-data"]
    require(
        not volume.get("external")
        and volume.get("driver") in {None, "local"}
        and volume.get("name") == f"{fixture.project}_publication-data"
        and not volume.get("driver_opts"),
        "synthetic database volume must belong to this project",
    )
    require(model.get("name") == fixture.project, "synthetic project changed")
    for name, service in services.items():
        require(
            not service.get("privileged")
            and not service.get("devices")
            and not service.get("cap_add")
            and not service.get("pid")
            and not service.get("ipc")
            and not service.get("entrypoint")
            and not service.get("volumes_from")
            and not service.get("uts")
            and not service.get("cgroup")
            and not service.get("userns_mode")
            and not service.get("device_cgroup_rules"),
            "unsafe synthetic service option",
        )
        require(
            not service.get("env_file"), "resolved synthetic environment files remain unresolved"
        )
        if name in ENV_SERVICES:
            expected = dict(fixture.expected_environments[name])
            if name == "database":
                expected["POSTGRES_PASSWORD_FILE"] = "/run/secrets/database_password"  # noqa: S105 - a container file path, not a password.
            actual = dict(service.get("environment", {}))
            if name == "admin":
                # Compose escapes literal dollars in `config` output so the
                # emitted model can be parsed again. Decode that one output
                # layer; the env file itself retains the single-quoted PHC.
                reported_phc = actual.get("PRIMARY_SIGNAL_ADMIN_PASSWORD_PHC")
                if isinstance(reported_phc, str):
                    actual["PRIMARY_SIGNAL_ADMIN_PASSWORD_PHC"] = reported_phc.replace("$$", "$")
            require(actual == expected, "synthetic runtime environment changed")
        else:
            require(not service.get("environment"), "unexpected TLS service environment")
        expected_image = (
            fixture.image
            if name in RUNTIME_SERVICES
            else {"database": "postgres:18.6", "admin_tls": "nginx:1.29-alpine"}[name]
        )
        require(service.get("image") == expected_image, "synthetic image changed")
        if name in RUNTIME_SERVICES:
            require(
                not service.get("build") and service.get("pull_policy") == "never",
                "synthetic runtime must use its current local image",
            )
        if name in {"public", "admin"}:
            ports = service.get("ports", [])
            expected_port = fixture.public_port if name == "public" else fixture.admin_port
            expected_target = 8000 if name == "public" else 8443
            require(
                len(ports) == 1
                and ports[0].get("host_ip") == "127.0.0.1"
                and str(ports[0].get("published")) == str(expected_port)
                and ports[0].get("target") == expected_target
                and ports[0].get("protocol") == "tcp",
                "synthetic web port must bind loopback only",
            )
        else:
            require(not service.get("ports"), "unexpected synthetic published port")
        expected_networks = (
            {"public_db", "admin_db"}
            if name == "database"
            else {"public_db", "public_ingress"}
            if name == "public"
            else {"admin_db", "admin_ingress"}
            if name == "admin"
            else {"admin_db"}
        )
        if name == "admin_tls":
            require(
                service.get("network_mode") == "service:admin" and not service.get("networks"),
                "TLS must share only the private admin namespace",
            )
        else:
            require(
                not service.get("network_mode") and _network_names(service) == expected_networks,
                "synthetic service network changed",
            )
        if name in {*RUNTIME_SERVICES, "admin_tls"}:
            require(
                service.get("read_only") is True
                and service.get("cap_drop") == ["ALL"]
                and service.get("security_opt") == ["no-new-privileges:true"]
                and service.get("pids_limit") == 64
                and service.get("init") is True
                and service.get("tmpfs") == ["/tmp:rw,noexec,nosuid,size=16m"],  # noqa: S108 - container tmpfs options, not a host file.
                "synthetic runtime hardening changed",
            )
        if name in RUNTIME_SERVICES:
            require(
                not service.get("user"),
                "synthetic runtime must retain the image's unprivileged user",
            )
        if name == "admin_tls":
            require(service.get("user") == "101:101", "TLS must retain its unprivileged user")
        expected_secrets: set[str] = (
            {"database_password"}
            if name == "database"
            else {"admin_tls_certificate", "admin_tls_key"}
            if name == "admin_tls"
            else set()
        )
        service_secrets = service.get("secrets", [])
        require(
            {secret.get("source") for secret in service_secrets} == expected_secrets
            and all(
                secret.get("target", secret.get("source"))
                in {secret.get("source"), f"/run/secrets/{secret.get('source')}"}
                for secret in service_secrets
            ),
            "synthetic service secret mounts changed",
        )
        mounted = service.get("volumes", [])
        expected_mounts: set[tuple[str, str, str, bool]]
        if name == "database":
            expected_mounts = {
                ("volume", "publication-data", "/var/lib/postgresql", False),
                (
                    "bind",
                    str(fixture.directory / "bootstrap-logins.sql"),
                    "/docker-entrypoint-initdb.d/900_local_logins.sql",
                    True,
                ),
                *(
                    (
                        "bind",
                        str((fixture.root / "deploy/postgres/initdb" / filename).resolve()),
                        f"/docker-entrypoint-initdb.d/{filename}",
                        True,
                    )
                    for filename in INIT_FILES
                ),
            }
        elif name == "admin_tls":
            expected_mounts = {
                (
                    "bind",
                    str((fixture.root / "deploy/publication/admin-nginx.conf").resolve()),
                    "/etc/nginx/nginx.conf",
                    True,
                )
            }
        else:
            expected_mounts = set()
        actual_mounts = {
            (
                mount.get("type"),
                mount.get("source"),
                mount.get("target"),
                bool(mount.get("read_only")),
            )
            for mount in mounted
        }
        require(actual_mounts == expected_mounts, "synthetic mount set changed")
    require(
        services["admin"]["command"]
        == ["primary-signal-web", "--surface", "admin", "--host", "127.0.0.1", "--port", "8001"],
        "admin must retain its loopback-only production command",
    )
    require(
        services["public"]["command"]
        == ["primary-signal-web", "--surface", "public", "--host", "0.0.0.0", "--port", "8000"],  # noqa: S104 - container interface; host binding is checked above.
        "public command changed",
    )
    require(
        services["seed"]["command"] == ["primary-signal-sample-seed"],
        "synthetic seed command changed",
    )
    require(
        "assert_test_database" in str(services["migrate"].get("command")),
        "migration must verify the disposable database",
    )


def _render(fixture: PublicationSmokeFixture, *, unresolved: bool) -> dict[str, Any]:
    args = (*fixture.docker_args, *fixture.compose_args, "config", "--format", "json")
    if unresolved:
        args += ("--no-interpolate", "--no-env-resolution")
    result = subprocess.run(  # noqa: S603 - fixed local Docker context and validated fixture paths.
        args,
        env=fixture.env,
        cwd=fixture.root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    require(result.returncode == 0, "synthetic Compose rendering failed")
    try:
        model = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise FixtureBoundaryError("synthetic Compose returned an invalid model") from error
    require(isinstance(model, dict), "synthetic Compose returned an invalid model")
    return cast(dict[str, Any], model)


def validate_fixture_models(fixture: PublicationSmokeFixture) -> None:
    unresolved = _render(fixture, unresolved=True)
    validate_unresolved_model(fixture, unresolved)
    resolved = _render(fixture, unresolved=False)
    validate_resolved_model(fixture, resolved)
