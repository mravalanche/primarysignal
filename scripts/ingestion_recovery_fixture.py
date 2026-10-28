"""Synthetic-only files and a loopback server for container recovery checks."""

import json
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from primary_signal.retrieval.extract import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    NORMALIZATION_VERSION,
    extract_article_html,
)
from primary_signal.retrieval.protocol import (
    ARTICLE_FETCH_CAPABILITY,
    FEED_FETCH_CAPABILITY,
    PROTOCOL_VERSION,
    READY_PATH,
)
from scripts.publication_smoke_fixture import (
    INIT_FILES,
    FixtureBoundaryError,
    local_docker_context,
    require,
)

DATABASE = "primary_signal_ingestion_recovery_test"
BOOTSTRAP_ROLE = "recovery_bootstrap_test"
MIGRATION_ROLE = "recovery_migration_test"
PROCESSOR_ROLE = "recovery_processor_test"
BOOTSTRAP_PASSWORD = "public_recovery_bootstrap_fixture_only"  # noqa: S105  # pragma: allowlist secret
MIGRATION_PASSWORD = "public_recovery_migration_fixture_only"  # noqa: S105  # pragma: allowlist secret
PROCESSOR_PASSWORD = "public_recovery_processor_fixture_only"  # noqa: S105  # pragma: allowlist secret
NAMESPACE = uuid.UUID("d4a5fcc4-1033-44c6-aab8-630ec44d7f46")
SOURCE_ID = uuid.uuid5(NAMESPACE, "source")
FEED_ID = uuid.uuid5(NAMESPACE, "feed")
ARTICLE_ID = uuid.uuid5(NAMESPACE, "article")
URL_ID = uuid.uuid5(NAMESPACE, "url")
JOB_ID = uuid.uuid5(NAMESPACE, "job")
SOURCE_KEY = "synthetic-container-recovery"
ARTICLE_URL = "https://public.example/synthetic-container-recovery"
FEED_URL = "https://public.example/synthetic-container-recovery.xml"
FEED_ENTRY_TITLE = "Synthetic feed recovery notice"
FEED_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0"><channel><title>Synthetic recovery feed</title>'
    "<link>https://public.example/</link><description>Disposable synthetic feed</description>"
    f"<item><title>{FEED_ENTRY_TITLE}</title><link>{ARTICLE_URL}</link>"
    '<guid isPermaLink="false">synthetic-container-recovery-entry</guid>'
    "<pubDate>Wed, 28 Oct 2026 12:00:00 GMT</pubDate>"
    "<description>Synthetic cobalt feed recovery notice.</description>"
    "</item></channel></rss>"
).encode()
RecoveryMode = Literal["article", "feed"]

ARTICLE_BODY = (
    b"<html><title>Synthetic recovery notice</title>"
    b"<article>Synthetic cobalt bulletin for isolated ingestion recovery.</article></html>"
)
_EXTRACTION = extract_article_html(ARTICLE_BODY, "text/html")
EXPECTED_TITLE = cast(str, _EXTRACTION.title)
EXPECTED_TEXT = _EXTRACTION.text
EXPECTED_WORD_COUNT = _EXTRACTION.word_count
EXPECTED_NORMALIZED_HASH = _EXTRACTION.normalized_content_hash
NETWORKS = ("recovery_db",)
VOLUME = "recovery-data"
SERVICES = {"database", "setup", "retriever", "worker"}
RUNTIME_USER = "10001:10001"
WORKER_COMMAND = [
    "primary-signal-processor",
    "--queue",
    "retrieval",
    "--once",
    "--retriever-origin",
    "http://127.0.0.1:8765",
    "--allow-local-fetch",
]
SETUP_COMMAND = [
    "python",
    "-c",
    "from primary_signal.db import DatabaseSettings,create_database_engine;"
    "from primary_signal.entrypoints.sample_seed import assert_test_database;"
    "from primary_signal.entrypoints.migrate import main;"
    "settings=DatabaseSettings();engine=create_database_engine(settings);"
    "connection=engine.connect();assert_test_database(connection,settings.expected_role);"
    "connection.close();engine.dispose();main([])",
]


def _worker_command(mode: RecoveryMode) -> list[str]:
    command = WORKER_COMMAND.copy()
    if mode == "feed":
        command[2] = "ingestion"
    return command


def server_source(mode: RecoveryMode = "article") -> str:
    """Return standalone server code with public payload data and no DB settings."""
    import base64
    import hashlib

    require(mode in {"article", "feed"}, "invalid recovery mode")

    payload: dict[str, object] = {
        "status": 200,
        "final_url": ARTICLE_URL,
        "redirect_chain": [],
        "content_type": "text/html",
        "decoded_byte_count": len(ARTICLE_BODY),
        "raw_response_hash": hashlib.sha256(ARTICLE_BODY).hexdigest(),
        "extracted_title": EXPECTED_TITLE,
        "extracted_text": EXPECTED_TEXT,
        "word_count": EXPECTED_WORD_COUNT,
        "normalized_content_hash": EXPECTED_NORMALIZED_HASH,
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "etag": None,
        "last_modified": None,
    }
    if mode == "feed":
        payload = {
            "status": 200,
            "final_url": FEED_URL,
            "body_base64": base64.b64encode(FEED_BODY).decode("ascii"),
            "etag": None,
            "last_modified": None,
        }
    ready = {
        "ready": True,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": [FEED_FETCH_CAPABILITY if mode == "feed" else ARTICLE_FETCH_CAPABILITY],
    }
    source = '''"""A closed synthetic retrieval fixture, never an internet retriever."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAYLOAD = json.loads(__PAYLOAD__)
READY = json.loads(__READY__)
TARGET_URL = __TARGET_URL__
FETCH_PATH = __FETCH_PATH__
READY_PATH = __READY_PATH__

class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self, port=8765):
        self.first_started = threading.Event()
        self.released = threading.Event()
        self.lock = threading.Lock()
        self.request_count = 0
        self.capacity = threading.BoundedSemaphore(4)
        super().__init__(("127.0.0.1", port), Handler)

    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.capacity.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.capacity.release()

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, *_args):
        pass

    def send_json(self, status, value):
        body = json.dumps(value, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        if self.path == READY_PATH:
            self.send_json(200, READY)
        elif self.path == "/control/state":
            with self.server.lock:
                state = {"first_started": self.server.first_started.is_set(),
                         "request_count": self.server.request_count,
                         "released": self.server.released.is_set()}
            self.send_json(200, state)
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.headers.get("Transfer-Encoding") is not None:
            self.send_json(400, {"error": "invalid_request"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= length <= 2048:
                raise ValueError()
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError()
            if self.path == "/control/release":
                if body:
                    raise ValueError()
                self.server.released.set()
                self.send_json(200, {"released": True})
                return
            if self.path != FETCH_PATH:
                self.send_json(404, {"error": "not_found"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            value = json.loads(body, object_pairs_hook=unique)
            if value != {"url": TARGET_URL, "etag": None, "last_modified": None}:
                raise ValueError()
        except (ValueError, UnicodeError, OSError, RecursionError):
            self.send_json(400, {"error": "invalid_request"})
            return
        with self.server.lock:
            self.server.request_count += 1
            first = self.server.request_count == 1
            if first:
                self.server.first_started.set()
        if first and not self.server.released.wait(timeout=90):
            self.send_json(503, {"error": "synthetic_hold_expired"})
            return
        self.send_json(200, PAYLOAD)

if __name__ == "__main__":
    with FixtureServer() as server:
        server.serve_forever()
'''
    return (
        source.replace("__PAYLOAD__", repr(json.dumps(payload)))
        .replace("__READY__", repr(json.dumps(ready)))
        .replace("__TARGET_URL__", repr(FEED_URL if mode == "feed" else ARTICLE_URL))
        .replace(
            "__FETCH_PATH__", repr("/v1/feeds/fetch" if mode == "feed" else "/v1/articles/fetch")
        )
        .replace("__READY_PATH__", repr(READY_PATH))
    )


def _write(path: Path, content: str, *, mode: int = 0o600) -> None:
    require(not path.exists() and not path.is_symlink(), "synthetic recovery file already exists")
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _url(role: str, password: str) -> str:
    return f"postgresql+psycopg://{role}:{password}@database:5432/{DATABASE}"


def _login_sql(mode: RecoveryMode = "article") -> str:
    capabilities = (
        "primary_signal_cap_queue_consume,primary_signal_cap_queue_submit,primary_signal_cap_feed_poll"
        if mode == "feed"
        else "primary_signal_cap_queue_consume,primary_signal_cap_article_persist"
    )
    return (
        "-- PUBLIC SYNTHETIC FIXTURES FOR A DISPOSABLE CONTAINER TEST ONLY.\n"
        f"CREATE ROLE {MIGRATION_ROLE} LOGIN PASSWORD '{MIGRATION_PASSWORD}' "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS;\n"
        f"CREATE ROLE {PROCESSOR_ROLE} LOGIN PASSWORD '{PROCESSOR_PASSWORD}' "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS;\n"
        f"GRANT CREATE ON DATABASE {DATABASE} TO {MIGRATION_ROLE},primary_signal_public_owner;\n"
        f"GRANT primary_signal_public_owner TO {MIGRATION_ROLE};\n"
        f"GRANT {capabilities} TO {PROCESSOR_ROLE};\n"
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n"
    )


@dataclass(frozen=True)
class IngestionRecoveryFixture:
    root: Path
    directory: Path
    project: str
    image: str
    docker_args: tuple[str, ...]
    compose_args: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    expected_environments: dict[str, dict[str, str]] = field(repr=False)
    expected_compose: dict[str, Any] = field(repr=False)
    mode: RecoveryMode = "article"
    database_name: str = DATABASE
    bootstrap_role: str = BOOTSTRAP_ROLE
    migration_role: str = MIGRATION_ROLE
    processor_role: str = PROCESSOR_ROLE


def create_fixture(
    root: Path, directory: Path, project: str, *, image: str, mode: RecoveryMode = "article"
) -> IngestionRecoveryFixture:
    require(mode in {"article", "feed"}, "invalid recovery mode")
    root, directory = root.resolve(), directory.resolve()
    require(
        directory.is_relative_to(Path("/tmp")) and not directory.is_relative_to(root),  # noqa: S108 - caller securely creates a random private directory under this boundary.
        "recovery fixture must be private and outside the checkout",
    )
    require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project), "invalid recovery project")
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9_.-]*:[a-z0-9][a-z0-9_.-]*", image),
        "recovery requires a tagged local image",
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not any(directory.iterdir()), "recovery fixture directory must be empty")
    directory.chmod(0o700)
    docker_args, environment = local_docker_context(directory)
    expected = {
        "database": {"POSTGRES_DB": DATABASE, "POSTGRES_USER": BOOTSTRAP_ROLE},
        "setup": {
            "PRIMARY_SIGNAL_DATABASE_URL": _url(MIGRATION_ROLE, MIGRATION_PASSWORD),
            "PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE": MIGRATION_ROLE,
        },
        "worker": {
            "PRIMARY_SIGNAL_ENVIRONMENT": "test",
            "PRIMARY_SIGNAL_DATABASE_URL": _url(PROCESSOR_ROLE, PROCESSOR_PASSWORD),
            "PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE": PROCESSOR_ROLE,
        },
    }
    for service, values in expected.items():
        _write(
            directory / f"{service}.env",
            "".join(f"{key}='{value}'\n" for key, value in values.items()),
        )
    _write(directory / "database_password", BOOTSTRAP_PASSWORD + "\n", mode=0o444)
    _write(directory / "bootstrap-logins.sql", _login_sql(mode), mode=0o444)
    _write(directory / "server.py", server_source(mode), mode=0o444)
    runtime = {
        "image": image,
        "pull_policy": "never",
        "user": RUNTIME_USER,
        "read_only": True,
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"],  # noqa: S108 - isolated container tmpfs, not a host file.
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 64,
        "mem_limit": "256m",
        "cpus": 1,
        "init": True,
        "restart": "no",
    }
    model: dict[str, Any] = {
        "name": project,
        "services": {
            "database": {
                "image": "postgres:18.6",
                "env_file": [str(directory / "database.env")],
                "environment": {"POSTGRES_PASSWORD_FILE": "/run/secrets/database_password"},
                "secrets": ["database_password"],
                "volumes": [
                    f"{VOLUME}:/var/lib/postgresql",
                    *(
                        f"{root / 'deploy/postgres/initdb' / filename}:/docker-entrypoint-initdb.d/{filename}:ro"
                        for filename in INIT_FILES
                    ),
                    f"{directory / 'bootstrap-logins.sql'}:/docker-entrypoint-initdb.d/900_local_logins.sql:ro",
                ],
                "networks": {NETWORKS[0]: {}},
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        "pg_isready --host=127.0.0.1 --username=$${POSTGRES_USER} --dbname=$${POSTGRES_DB}",
                    ],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 10,
                },
            },
            "setup": {
                **runtime,
                "env_file": [str(directory / "setup.env")],
                "command": SETUP_COMMAND,
                "networks": {NETWORKS[0]: {}},
            },
            "retriever": {
                **runtime,
                "command": ["python", "/fixture/server.py"],
                "volumes": [f"{directory / 'server.py'}:/fixture/server.py:ro"],
                "networks": {NETWORKS[0]: {}},
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "import json,urllib.request;assert json.load(urllib.request.urlopen('http://127.0.0.1:8765/v1/ready',timeout=2))['ready']",
                    ],
                    "interval": "1s",
                    "timeout": "3s",
                    "retries": 30,
                },
            },
            "worker": {
                **runtime,
                "env_file": [str(directory / "worker.env")],
                "command": _worker_command(mode),
                "network_mode": "service:retriever",
            },
        },
        "networks": {NETWORKS[0]: {"internal": True, "name": f"{project}_{NETWORKS[0]}"}},
        "volumes": {VOLUME: {"name": f"{project}_{VOLUME}"}},
        "secrets": {"database_password": {"file": str(directory / "database_password")}},
    }
    path = directory / "compose.synthetic.json"
    _write(path, json.dumps(model))
    return IngestionRecoveryFixture(
        root,
        directory,
        project,
        image,
        docker_args,
        (
            "compose",
            "--project-directory",
            str(root),
            "--project-name",
            project,
            "--env-file",
            "/dev/null",
            "-f",
            str(path),
        ),
        environment,
        expected,
        model,
        mode=mode,
    )


def _approved_bootstrap(root: Path) -> Path:
    root = root.resolve()
    path = root / "deploy/postgres/initdb"
    require(path.resolve().is_relative_to(root), "recovery bootstrap mount escapes checkout")
    require(path.is_dir() and not path.is_symlink(), "invalid recovery bootstrap path")
    require(
        {entry.name for entry in path.iterdir()} == set(INIT_FILES),
        "unexpected recovery bootstrap file",
    )
    require(
        all((path / name).resolve().is_relative_to(root) for name in INIT_FILES),
        "recovery bootstrap file escapes checkout",
    )
    require(
        all((path / name).is_file() and not (path / name).is_symlink() for name in INIT_FILES),
        "invalid recovery bootstrap file",
    )
    return path.resolve()


def validate_unresolved_model(
    fixture: IngestionRecoveryFixture, model: dict[str, Any], *, resolved: bool = False
) -> None:
    require(
        set(model) <= {"name", "services", "networks", "volumes", "secrets"},
        "extra recovery configuration is forbidden",
    )
    services = model.get("services", {})
    require(set(services) == SERVICES, "recovery service set changed")
    require(
        not model.get("configs") and not model.get("include"),
        "extra recovery configuration is forbidden",
    )
    bootstrap = _approved_bootstrap(fixture.root)
    expected_files = {
        "server.py": server_source(fixture.mode),
        "bootstrap-logins.sql": _login_sql(fixture.mode),
        "database_password": BOOTSTRAP_PASSWORD + "\n",
        **{
            f"{name}.env": "".join(f"{key}='{value}'\n" for key, value in values.items())
            for name, values in fixture.expected_environments.items()
        },
    }
    for filename, content in expected_files.items():
        path = fixture.directory / filename
        require(
            path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(fixture.directory),
            "recovery file escaped fixture",
        )
        require(path.read_text(encoding="utf-8") == content, "recovery fixture file changed")
    expected_binds: dict[str, set[tuple[str, str]]] = {
        "database": {
            *(
                (str(bootstrap / filename), f"/docker-entrypoint-initdb.d/{filename}")
                for filename in INIT_FILES
            ),
            (
                str(fixture.directory / "bootstrap-logins.sql"),
                "/docker-entrypoint-initdb.d/900_local_logins.sql",
            ),
        },
        "retriever": {(str(fixture.directory / "server.py"), "/fixture/server.py")},
        "worker": set(),
        "setup": set(),
    }
    for name, service in services.items():
        allowed = (
            {
                "image",
                "env_file",
                "environment",
                "healthcheck",
                "networks",
                "secrets",
                "volumes",
                "command",
                "entrypoint",
            }
            if name == "database"
            else {
                "image",
                "env_file",
                "environment",
                "healthcheck",
                "networks",
                "volumes",
                "command",
                "entrypoint",
                "pull_policy",
                "user",
                "read_only",
                "tmpfs",
                "cap_drop",
                "security_opt",
                "pids_limit",
                "mem_limit",
                "cpus",
                "init",
                "restart",
                "network_mode",
                "depends_on",
            }
        )
        require(set(service) <= allowed, "unknown recovery service setting")
        env_files = service.get("env_file", [])
        require(
            len(env_files) == (1 if name in fixture.expected_environments and not resolved else 0),
            "recovery environment file set changed",
        )
        for item in env_files:
            path = cast(dict[str, Any], item).get("path") if isinstance(item, dict) else item
            require(
                isinstance(path, str)
                and path == str(fixture.directory / f"{name}.env")
                and not Path(path).is_symlink(),
                "recovery environment path changed",
            )
        binds: set[tuple[str, str]] = set()
        for mount in service.get("volumes", []):
            require(mount.get("type") in {"bind", "volume"}, "unsupported recovery mount")
            if mount.get("type") == "bind":
                source = mount.get("source")
                require(isinstance(source, str), "invalid recovery mount source")
                path = Path(source)
                require(
                    not path.is_symlink()
                    and (
                        path.resolve().is_relative_to(fixture.directory)
                        or path.resolve()
                        in {(bootstrap / filename).resolve() for filename in INIT_FILES}
                    ),
                    "recovery mount escapes fixture",
                )
                require(mount.get("read_only") is True, "recovery bind must be read-only")
                require(
                    set(mount.get("bind", {})) <= {"create_host_path"},
                    "recovery bind options changed",
                )
                binds.add((str(path), cast(str, mount.get("target"))))
        require(binds == expected_binds[name], "recovery bind set changed")
    secrets = model.get("secrets", {})
    require(set(secrets) == {"database_password"}, "recovery secret set changed")
    value = secrets["database_password"]
    require(
        set(value) <= {"file", "name"}
        and value.get("file") == str(fixture.directory / "database_password"),
        "recovery password file changed",
    )


def validate_resolved_model(fixture: IngestionRecoveryFixture, model: dict[str, Any]) -> None:
    validate_unresolved_model(fixture, model, resolved=True)
    require(model.get("name") == fixture.project, "recovery project changed")
    networks = model.get("networks", {})
    require(set(networks) == set(NETWORKS), "recovery network set changed")
    require(
        all(
            value.get("internal") is True
            and value.get("name") == f"{fixture.project}_{name}"
            and not value.get("external")
            and not value.get("driver_opts")
            and not value.get("ipam")
            and set(value) <= {"name", "internal", "ipam"}
            for name, value in networks.items()
        ),
        "recovery network must be project-owned and internal",
    )
    volumes = model.get("volumes", {})
    require(
        set(volumes) == {VOLUME}
        and volumes[VOLUME].get("name") == f"{fixture.project}_{VOLUME}"
        and not volumes[VOLUME].get("external")
        and not volumes[VOLUME].get("driver_opts")
        and set(volumes[VOLUME]) <= {"name"},
        "recovery volume must belong to the project",
    )
    services = model["services"]
    for name, service in services.items():
        require(
            not service.get("ports")
            and not service.get("privileged")
            and not service.get("devices")
            and not service.get("cap_add")
            and not service.get("pid")
            and not service.get("ipc")
            and service.get("entrypoint") is None,
            "unsafe recovery service setting",
        )
        require(
            service.get("image") == ("postgres:18.6" if name == "database" else fixture.image)
            and not service.get("build"),
            "recovery image changed",
        )
        if name != "database":
            require(
                service.get("pull_policy") == "never"
                and service.get("user") == RUNTIME_USER
                and service.get("read_only") is True
                and service.get("cap_drop") == ["ALL"]
                and service.get("security_opt") == ["no-new-privileges:true"]
                and service.get("pids_limit") == 64
                and service.get("init") is True,
                "recovery runtime hardening changed",
            )
            require(service.get("restart") == "no", "recovery restart policy changed")
            require(
                service.get("tmpfs") == ["/tmp:rw,noexec,nosuid,size=16m"]  # noqa: S108 - isolated container tmpfs.
                and str(service.get("mem_limit")) == str(256 * 1024 * 1024)
                and service.get("cpus") == 1,
                "recovery runtime bounds changed",
            )
        expected = dict(fixture.expected_environments.get(name, {}))
        if name == "database":
            expected["POSTGRES_PASSWORD_FILE"] = "/run/secrets/database_password"  # noqa: S105 - fixed container file path.
        require(service.get("environment", {}) == expected, "recovery runtime environment changed")
        if name == "worker":
            require(
                service.get("network_mode") == "service:retriever"
                and not service.get("networks")
                and service.get("command") == _worker_command(fixture.mode),
                "worker namespace or command changed",
            )
            require(
                service.get("depends_on")
                == {
                    "retriever": {"condition": "service_started", "restart": True, "required": True}
                },
                "worker dependencies changed",
            )
        else:
            require(
                not service.get("network_mode")
                and set(service.get("networks", {})) == set(NETWORKS),
                "recovery network membership changed",
            )
            require(not service.get("depends_on"), "unexpected recovery service dependency")
        if name == "database":
            require(service.get("command") is None, "database initialization command changed")
        if name in {"setup", "retriever"}:
            require(
                service.get("command")
                == (SETUP_COMMAND if name == "setup" else ["python", "/fixture/server.py"]),
                "recovery command changed",
            )
        mounted = service.get("volumes", [])
        volume_mounts = [mount for mount in mounted if mount.get("type") == "volume"]
        require(
            volume_mounts
            == (
                [
                    {
                        "type": "volume",
                        "source": VOLUME,
                        "target": "/var/lib/postgresql",
                        "volume": {},
                    }
                ]
                if name == "database"
                else []
            ),
            "recovery data volume mount changed",
        )
        expected_secrets = (
            [{"source": "database_password", "target": "/run/secrets/database_password"}]
            if name == "database"
            else []
        )
        require(
            service.get("secrets", []) == expected_secrets, "recovery service secret access changed"
        )


def validate_fixture_models(fixture: IngestionRecoveryFixture) -> None:
    manifest = fixture.directory / "compose.synthetic.json"
    require(manifest.is_file() and not manifest.is_symlink(), "recovery Compose file changed")
    try:
        require(
            json.loads(manifest.read_text(encoding="utf-8")) == fixture.expected_compose,
            "recovery Compose file changed",
        )
    except ValueError, TypeError:
        raise FixtureBoundaryError("recovery Compose file changed") from None
    for unresolved in (True, False):
        args = (*fixture.docker_args, *fixture.compose_args, "config", "--format", "json")
        if unresolved:
            args += ("--no-interpolate", "--no-env-resolution")
        result = subprocess.run(  # noqa: S603 - fixed local Docker/Compose context, never shell input.
            args,
            cwd=fixture.root,
            env=fixture.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        require(result.returncode == 0, "recovery Compose rendering failed")
        try:
            model = json.loads(result.stdout)
        except ValueError, TypeError:
            raise FixtureBoundaryError("invalid recovery Compose model") from None
        require(isinstance(model, dict), "invalid recovery Compose model")
        typed = cast(dict[str, Any], model)
        if unresolved:
            validate_unresolved_model(fixture, typed)
        else:
            validate_resolved_model(fixture, typed)
