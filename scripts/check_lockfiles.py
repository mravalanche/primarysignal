"""Reject credentials and unexpected package sources in committed lockfiles."""

from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from urllib.parse import SplitResult, urlsplit

NPM_REGISTRY_HOST = "registry.npmjs.org"
PYPI_REGISTRY_URL = "https://pypi.org/simple"
PYPI_FILES_HOST = "files.pythonhosted.org"


def _strings(value: object, path: str = "root") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in cast(dict[str, object], value).items():
            yield from _strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(cast(list[object], value)):
            yield from _strings(child, f"{path}[{index}]")


def _parsed_url(value: str) -> SplitResult | None:
    parsed = urlsplit(value)
    return parsed if parsed.scheme and parsed.netloc else None


def _credential_errors(data: object, lockfile: str) -> list[str]:
    errors: list[str] = []
    for path, value in _strings(data):
        parsed = _parsed_url(value)
        if parsed is not None and (parsed.username is not None or parsed.password is not None):
            errors.append(f"{lockfile}: {path} contains URL credentials")
    return errors


def check_package_lock(path: Path) -> list[str]:
    """Validate npm sources without treating integrity hashes as secrets."""

    with path.open(encoding="utf-8") as stream:
        data = cast(object, json.load(stream))
    errors = _credential_errors(data, path.name)
    if not isinstance(data, dict):
        return [*errors, f"{path.name}: root must be an object"]
    root = cast(dict[str, object], data)
    packages = root.get("packages")
    if not isinstance(packages, dict):
        return [*errors, f"{path.name}: packages must be an object"]

    for package_path, package in cast(dict[str, object], packages).items():
        if not isinstance(package, dict):
            continue
        package_data = cast(dict[str, object], package)
        if "resolved" not in package_data:
            continue
        resolved = package_data["resolved"]
        location = f"packages.{package_path}.resolved"
        if not isinstance(resolved, str):
            errors.append(f"{path.name}: {location} must be a URL string")
            continue
        parsed = _parsed_url(resolved)
        if (
            parsed is None
            or parsed.scheme != "https"
            or parsed.hostname != NPM_REGISTRY_HOST
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
            or not parsed.path.endswith(".tgz")
        ):
            errors.append(f"{path.name}: {location} is not an approved npm registry tarball")
    return errors


def check_uv_lock(path: Path) -> list[str]:
    """Validate the registry and artifact URLs emitted by uv for PyPI."""

    with path.open("rb") as stream:
        data = cast(object, tomllib.load(stream))
    errors = _credential_errors(data, path.name)
    if not isinstance(data, dict):
        return [*errors, f"{path.name}: root must be a table"]
    root = cast(dict[str, object], data)
    packages = root.get("package")
    if not isinstance(packages, list):
        return [*errors, f"{path.name}: package must be an array"]

    approved_url_paths: set[str] = set()
    for index, package in enumerate(cast(list[object], packages)):
        if not isinstance(package, dict):
            errors.append(f"{path.name}: package[{index}] must be a table")
            continue
        package_data = cast(dict[str, object], package)
        source = package_data.get("source")
        if isinstance(source, dict) and "registry" in source:
            source_data = cast(dict[str, object], source)
            registry = source_data["registry"]
            approved_url_paths.add(f"root.package[{index}].source.registry")
            if registry != PYPI_REGISTRY_URL:
                errors.append(f"{path.name}: package[{index}].source.registry is not PyPI")
        sdist = package_data.get("sdist")
        if isinstance(sdist, dict) and "url" in sdist:
            sdist_data = cast(dict[str, object], sdist)
            location = f"root.package[{index}].sdist.url"
            approved_url_paths.add(location)
            errors.extend(_check_python_artifact_url(sdist_data["url"], path.name, location))
        wheels = package_data.get("wheels", [])
        if isinstance(wheels, list):
            for wheel_index, wheel in enumerate(cast(list[object], wheels)):
                if isinstance(wheel, dict) and "url" in wheel:
                    wheel_data = cast(dict[str, object], wheel)
                    location = f"root.package[{index}].wheels[{wheel_index}].url"
                    approved_url_paths.add(location)
                    errors.extend(
                        _check_python_artifact_url(wheel_data["url"], path.name, location)
                    )

    for location, value in _strings(root):
        if _parsed_url(value) is not None and location not in approved_url_paths:
            errors.append(f"{path.name}: {location} is an unexpected URL field")
    return errors


def _check_python_artifact_url(value: object, lockfile: str, location: str) -> list[str]:
    if not isinstance(value, str):
        return [f"{lockfile}: {location} must be a URL string"]
    parsed = _parsed_url(value)
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.hostname != PYPI_FILES_HOST
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/packages/")
        or not parsed.path.endswith((".whl", ".tar.gz", ".zip"))
    ):
        return [f"{lockfile}: {location} is not an approved PyPI artifact URL"]
    return []


def main() -> int:
    """Check the repository's two committed dependency lockfiles."""

    project_root = Path(__file__).resolve().parents[1]
    errors = [
        *check_package_lock(project_root / "package-lock.json"),
        *check_uv_lock(project_root / "uv.lock"),
    ]
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
