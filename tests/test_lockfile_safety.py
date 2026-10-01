import json
from pathlib import Path

from scripts.check_lockfiles import check_package_lock, check_uv_lock


def write_package_lock(path: Path, resolved: str) -> None:
    path.write_text(
        json.dumps({"packages": {"node_modules/example": {"resolved": resolved}}}),
        encoding="utf-8",
    )


def test_package_lock_accepts_the_npm_registry(tmp_path: Path) -> None:
    lockfile = tmp_path / "package-lock.json"
    write_package_lock(lockfile, "https://registry.npmjs.org/example/-/example-1.0.0.tgz")

    assert check_package_lock(lockfile) == []


def test_package_lock_rejects_credentials_and_other_sources(tmp_path: Path) -> None:
    lockfile = tmp_path / "package-lock.json"
    credentials = ":".join(("operator", "placeholder"))
    write_package_lock(lockfile, f"https://{credentials}@packages.example/example.tgz")

    errors = check_package_lock(lockfile)

    assert any("credentials" in error for error in errors)
    assert any("approved npm registry" in error for error in errors)


def test_uv_lock_accepts_pypi_registry_artifacts(tmp_path: Path) -> None:
    lockfile = tmp_path / "uv.lock"
    lockfile.write_text(
        """
[[package]]
name = "example"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/packages/aa/example-1.0.0.tar.gz", hash = "sha256:abc", size = 1 }
wheels = [
  { url = "https://files.pythonhosted.org/packages/bb/example-1.0.0-py3-none-any.whl", hash = "sha256:def", size = 1 },
]
""",
        encoding="utf-8",
    )

    assert check_uv_lock(lockfile) == []


def test_uv_lock_rejects_credentials_and_unexpected_urls(tmp_path: Path) -> None:
    lockfile = tmp_path / "uv.lock"
    credentials = ":".join(("operator", "placeholder"))
    lockfile.write_text(
        f"""
[[package]]
name = "example"
version = "1.0.0"
source = {{ registry = "https://{credentials}@packages.example/simple" }}
wheels = [
  {{ url = "https://mirror.example/example.whl", hash = "sha256:def", size = 1 }},
]
homepage = "https://public.example/project"
""",
        encoding="utf-8",
    )

    errors = check_uv_lock(lockfile)

    assert any("credentials" in error for error in errors)
    assert any("not PyPI" in error for error in errors)
    assert any("approved PyPI artifact" in error for error in errors)
    assert any("unexpected URL field" in error for error in errors)
