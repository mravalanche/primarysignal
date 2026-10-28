"""Provision and verify only the VM gate's newly created SCRAM TCP database."""

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy import URL

from scripts import bootstrap_test_database_roles as bootstrap

ADMIN_ROLE = "ps_dev_admin"
ADMIN_PASSWORD = "primary_signal_dev_admin_test_only"  # noqa: S105  # pragma: allowlist secret
WRONG_PASSWORD = "primary_signal_wrong_test_only"  # noqa: S105  # pragma: allowlist secret
DATABASE = "primary_signal_test"
ROLE_LOGINS = {
    "PRIMARY_SIGNAL_TEST_SCHEDULER_DATABASE_URL": (
        bootstrap.SCHEDULER_ROLE,
        bootstrap.SCHEDULER_PASSWORD,
    ),
    "PRIMARY_SIGNAL_TEST_PROCESSOR_DATABASE_URL": (
        bootstrap.PROCESSOR_ROLE,
        bootstrap.PROCESSOR_PASSWORD,
    ),
    "PRIMARY_SIGNAL_TEST_INVENTORY_DATABASE_URL": (
        bootstrap.INVENTORY_ROLE,
        bootstrap.INVENTORY_PASSWORD,
    ),
    "PRIMARY_SIGNAL_TEST_PUBLIC_DATABASE_URL": (bootstrap.PUBLIC_ROLE, bootstrap.PUBLIC_PASSWORD),
    "PRIMARY_SIGNAL_TEST_PUBLICATION_DATABASE_URL": (
        bootstrap.PUBLICATION_ROLE,
        bootstrap.PUBLICATION_PASSWORD,
    ),
    "PRIMARY_SIGNAL_TEST_HEALTH_DATABASE_URL": (bootstrap.HEALTH_ROLE, bootstrap.HEALTH_PASSWORD),
    "PRIMARY_SIGNAL_TEST_JOB_HEALTH_DATABASE_URL": (
        bootstrap.JOB_HEALTH_LOGIN_ROLE,
        bootstrap.JOB_HEALTH_PASSWORD,
    ),
    "PRIMARY_SIGNAL_TEST_EDITORIAL_DATABASE_URL": (
        bootstrap.EDITORIAL_ROLE,
        bootstrap.EDITORIAL_PASSWORD,
    ),
    "PRIMARY_SIGNAL_TEST_ADMIN_SESSION_DATABASE_URL": (
        bootstrap.ADMIN_SESSION_LOGIN_ROLE,
        bootstrap.ADMIN_SESSION_PASSWORD,
    ),
    "PRIMARY_SIGNAL_TEST_PUBLICATION_DECISION_DATABASE_URL": (
        bootstrap.PUBLICATION_DECISION_LOGIN_ROLE,
        bootstrap.PUBLICATION_DECISION_PASSWORD,
    ),
    "PRIMARY_SIGNAL_TEST_RETENTION_CLEANUP_DATABASE_URL": (
        bootstrap.RETENTION_CLEANUP_LOGIN_ROLE,
        bootstrap.RETENTION_CLEANUP_PASSWORD,
    ),
}


def _environment() -> dict[str, str]:
    # Libpq service/host overrides and passfiles must never redirect the gate
    # or consult an operator's credentials. Test DSNs are built below instead.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PG", "PRIMARY_SIGNAL_"))
    }
    environment["PGPASSFILE"] = os.devnull
    return environment


def _connect(
    *,
    host: str,
    port: int,
    database: str = "postgres",
    role: str = ADMIN_ROLE,
    password: str = ADMIN_PASSWORD,
) -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=host,
        hostaddr="127.0.0.1" if host == "127.0.0.1" else "",
        port=port,
        dbname=database,
        user=role,
        password=password,
        connect_timeout=3,
        options="-c statement_timeout=3000 -c lock_timeout=1000",
        sslmode="disable",
        autocommit=True,
    )


def _identity(connection: psycopg.Connection[Any]) -> tuple[str, str]:
    row = connection.execute(
        "SELECT current_setting('data_directory'), system_identifier::text FROM pg_control_system()"
    ).fetchone()
    if row is None:
        raise ValueError("missing disposable cluster identity")
    return str(Path(row[0]).resolve()), str(row[1])


def _verify_cluster(cluster_dir: Path, port: int) -> None:
    expected_directory = str((cluster_dir / "data").resolve())
    with _connect(host=str(cluster_dir / "socket"), port=port) as connection:
        socket_identity = _identity(connection)
    if socket_identity[0] != expected_directory:
        raise ValueError("private socket does not belong to the disposable cluster")
    with _connect(host="127.0.0.1", port=port) as connection:
        if _identity(connection) != socket_identity:
            raise ValueError("TCP endpoint does not belong to the disposable cluster")
        if connection.execute("SHOW password_encryption").fetchone() != ("scram-sha-256",):
            raise ValueError("disposable cluster does not use SCRAM passwords")
        host_rules = connection.execute(
            "SELECT auth_method, error FROM pg_hba_file_rules WHERE type LIKE 'host%'"
        ).fetchall()
        if not host_rules or any(
            method != "scram-sha-256" or error is not None for method, error in host_rules
        ):
            raise ValueError("disposable TCP authentication rules are not exclusively SCRAM")
    try:
        with _connect(host="127.0.0.1", port=port, password=WRONG_PASSWORD):
            pass
    except psycopg.OperationalError as error:
        # Libpq may omit SQLSTATE on failed startup authentication. The
        # verified parsed HBA rules and successful connections bracket this
        # negative check without relying on localized exception text.
        if error.sqlstate not in (None, "28P01"):
            raise ValueError("disposable TCP authentication rejection was not verified") from None
    else:
        raise ValueError("disposable TCP endpoint accepted an incorrect password")
    # A successful second identity read rules out an unavailable server being
    # mistaken for the expected wrong-password rejection.
    with _connect(host="127.0.0.1", port=port) as connection:
        if _identity(connection) != socket_identity:
            raise ValueError("disposable TCP endpoint changed during authentication checks")


def _url(role: str, password: str, port: int) -> str:
    return URL.create(
        "postgresql+psycopg",
        username=role,
        password=password,
        host="127.0.0.1",
        port=port,
        database=DATABASE,
        query={"connect_timeout": "3", "sslmode": "disable"},
    ).render_as_string(hide_password=False)


def _run(cluster_dir: Path, port: int, mode: str) -> int:
    environment = _environment()
    # The direct libpq probes need the same isolation as their child commands.
    os.environ.clear()
    os.environ.update(environment)
    _verify_cluster(cluster_dir, port)
    with _connect(host=str(cluster_dir / "socket"), port=port) as connection:
        connection.execute("CREATE DATABASE primary_signal_test")
    socket_environment = environment | {
        "PRIMARY_SIGNAL_TEST_DATABASE_URL": URL.create(
            "postgresql+psycopg",
            username=ADMIN_ROLE,
            password=ADMIN_PASSWORD,
            database=DATABASE,
            query={"host": str(cluster_dir / "socket"), "port": str(port)},
        ).render_as_string(hide_password=False),
    }
    # Bootstrap over the owner-only Unix socket; do not expose test TCP DSNs
    # until all restricted logins exist and their authentication has passed.
    subprocess.run(
        [sys.executable, "-m", "scripts.bootstrap_test_database_roles"],
        env=socket_environment,
        check=True,
        capture_output=True,
        timeout=60,
    )
    environment.update(
        {
            "PRIMARY_SIGNAL_TEST_DATABASE_URL": _url(ADMIN_ROLE, ADMIN_PASSWORD, port),
            "PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE": ADMIN_ROLE,
            "PRIMARY_SIGNAL_TEST_PUBLICATION_DATABASE_EXPECTED_ROLE": bootstrap.PUBLICATION_ROLE,
            "PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS": "true",
            "PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE": "true",
        }
    )
    for variable, (role, password) in ROLE_LOGINS.items():
        with _connect(
            host="127.0.0.1", port=port, database=DATABASE, role=role, password=password
        ) as connection:
            if connection.execute("SELECT current_user").fetchone() != (role,):
                raise ValueError("unexpected disposable test role")
        environment[variable] = _url(role, password, port)
    migration_environment = environment | {
        "PRIMARY_SIGNAL_DATABASE_URL": environment["PRIMARY_SIGNAL_TEST_DATABASE_URL"],
        "PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE": ADMIN_ROLE,
    }
    subprocess.run(
        [sys.executable, "-m", "primary_signal.entrypoints.migrate"],
        env=migration_environment,
        check=True,
        timeout=120,
    )
    completed = subprocess.run(  # noqa: S603 - fixed local Poe task, never a shell command.
        [sys.executable, "-m", "poethepoet", "check-offline" if mode == "offline" else "check"],
        env=environment,
        check=False,
    )
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("port")
    password_file = commands.add_parser("password-file")
    password_file.add_argument("--cluster-dir", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--cluster-dir", type=Path, required=True)
    run.add_argument("--port", type=int, required=True)
    run.add_argument("--mode", choices=("offline", "check"), required=True)
    args = parser.parse_args()
    try:
        if args.command == "port":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                print(probe.getsockname()[1])
            return 0
        if args.command == "password-file":
            path = args.cluster_dir / "initdb-password"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(ADMIN_PASSWORD + "\n")
            return 0
        if not 1 <= args.port <= 65535:
            raise ValueError("invalid disposable TCP port")
        return _run(args.cluster_dir, args.port, args.mode)
    except OSError, ValueError, psycopg.Error, subprocess.SubprocessError:
        print("disposable TCP database setup failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
