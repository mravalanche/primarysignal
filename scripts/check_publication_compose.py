"""Check the disposable Compose model using placeholder-only example files."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    model = (root / "compose.publication.yaml").read_text(encoding="utf-8")
    for name in ("database", "migrate", "seed", "public", "admin"):
        source = f"env_file: .env.publication.{name}\n"
        require(source in model, f"missing {name} service configuration")
        model = model.replace(source, f"env_file: .env.publication.{name}.example\n")

    with tempfile.TemporaryDirectory(prefix="primary-signal-compose-check-") as directory:
        path = Path(directory) / "compose.yaml"
        path.write_text(model, encoding="utf-8")
        docker = shutil.which("docker")
        if docker is None:
            raise SystemExit("docker compose is required")
        result = subprocess.run(  # noqa: S603 - fixed argv; model is this repository's file.
            [
                docker,
                "compose",
                "--project-directory",
                str(root),
                "-f",
                str(path),
                "config",
                "--no-env-resolution",
                "--format",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    config = json.loads(result.stdout)
    services = config["services"]
    for name in ("database", "migrate", "seed", "admin_tls"):
        require(not services[name].get("ports"), f"{name} must have no published port")
    for name in ("public", "admin"):
        ports = services[name]["ports"]
        require(
            len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1",
            f"{name} must bind host loopback",
        )
    require(
        services["admin_tls"]["network_mode"] == "service:admin",
        "admin TLS must share admin namespace",
    )
    require("127.0.0.1" in services["admin"]["command"], "admin must bind container loopback")
    require(
        services["migrate"]["depends_on"]["database"]["condition"] == "service_healthy",
        "migration ordering is invalid",
    )
    require(
        "assert_test_database" in str(services["migrate"]["command"]),
        "migration must check the disposable database before changing schema",
    )
    require(
        services["seed"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully",
        "seed ordering is invalid",
    )
    for name in ("public", "admin"):
        require(
            services[name]["depends_on"]["seed"]["condition"] == "service_completed_successfully",
            f"{name} must wait for seed",
        )
    require(
        all(network["internal"] for network in config["networks"].values()),
        "networks must be internal",
    )
    for name in ("public", "admin"):
        example = (root / f".env.publication.{name}.example").read_text(encoding="utf-8")
        for forbidden in ("migration_test", "publication_writer_test", "PRIMARY_SIGNAL_TEST_"):
            require(forbidden not in example, f"{name} example includes {forbidden}")
    print("Disposable Compose model and network boundaries are valid (placeholder config only).")


if __name__ == "__main__":
    main()
