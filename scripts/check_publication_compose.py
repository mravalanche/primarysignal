"""Check the disposable Compose model using placeholder-only example files."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, TypedDict, cast


class ComposeConfig(TypedDict):
    services: dict[str, dict[str, Any]]
    networks: dict[str, dict[str, Any]]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


IMAGE_DIGEST = "ghcr.io/mravalanche/primarysignal@sha256:" + "0" * 64
RUNTIME_SERVICES = ("migrate", "seed", "public", "admin")


def render_model(root: Path, filename: str) -> ComposeConfig:
    model = (root / filename).read_text(encoding="utf-8")
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
        environment = os.environ.copy()
        environment["PRIMARY_SIGNAL_PUBLICATION_IMAGE"] = IMAGE_DIGEST
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
            env=environment,
            text=True,
        )
    return cast(ComposeConfig, json.loads(result.stdout))


def check_boundaries(config: ComposeConfig, root: Path) -> None:
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
    public_networks = set(services["public"]["networks"])
    admin_networks = set(services["admin"]["networks"])
    database_networks = set(services["database"]["networks"])
    require(not public_networks & admin_networks, "web services must not share a network")
    require(public_networks | admin_networks == database_networks, "database network links changed")
    require(public_networks == {"public_db"}, "public database network changed")
    require(admin_networks == {"admin_db"}, "admin database network changed")
    for name in ("migrate", "seed"):
        require(set(services[name]["networks"]) == admin_networks, f"{name} network changed")
    migration_example = (root / ".env.publication.migrate.example").read_text(encoding="utf-8")
    seed_example = (root / ".env.publication.seed.example").read_text(encoding="utf-8")
    require(
        "bootstrap_owner_test" not in migration_example + seed_example,
        "bootstrap owner reached an app example",
    )
    for name in ("public", "admin"):
        example = (root / f".env.publication.{name}.example").read_text(encoding="utf-8")
        for forbidden in (
            "bootstrap_owner_test",
            "migration_test",
            "publication_writer_test",
            "PRIMARY_SIGNAL_TEST_",
        ):
            require(forbidden not in example, f"{name} example includes {forbidden}")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    local = render_model(root, "compose.publication.yaml")
    published = render_model(root, "compose.publication.image.yaml")
    for config in (local, published):
        check_boundaries(config, root)

    for name in RUNTIME_SERVICES:
        local_service = dict(local["services"][name])
        published_service = dict(published["services"][name])
        require(
            local_service.pop("build", None)
            == {"context": str(root), "dockerfile": "Dockerfile", "target": "runtime"},
            f"{name} must use the local runtime build",
        )
        require("image" not in local_service, f"{name} local service must not pull an image")
        require(
            published_service.pop("image", None) == IMAGE_DIGEST, f"{name} image digest changed"
        )
        require(
            published_service.pop("pull_policy", None) == "always", f"{name} pull policy changed"
        )
        require("build" not in published_service, f"{name} published service must not build")
        require(
            local_service == published_service, f"{name} runtime controls differ between models"
        )
    for name in ("database", "admin_tls"):
        require(
            local["services"][name] == published["services"][name],
            f"{name} controls differ between models",
        )
    print(
        "Local and published-image Compose models keep the same security boundaries (placeholder config only)."
    )


if __name__ == "__main__":
    main()
