#!/usr/bin/env python3
"""Keep the project's version numbers honest.

**The major version is the schema version.** ``v1.x.y`` speaks schema v1 — the
server image and the client library alike — so a consumer who pins
``tns-mirror-client>=1,<2`` is pinning the contract, which is the thing that
actually matters to them. A breaking schema change bumps the major everywhere;
everything else is a minor or a patch.

Both artifacts therefore carry one version, restated in five places that drift
silently. The first symptom of drift is usually a release that publishes a
number the code does not claim:

    server/pyproject.toml
    server/src/tns_mirror_server/__init__.py    (__version__ and SCHEMA_VERSION)
    client/pyproject.toml
    client/src/tns_mirror_client/__init__.py
    client/src/tns_mirror_client/client.py     (SCHEMA_VERSION)
    docs/src/_data/site.json                   (version and schemaVersion)

Pass a tag to check a release publishes what it claims:

    python3 scripts/check_versions.py --tag v1.0.0

Standard library only, so this runs before anything is installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def pyproject_version(path: str) -> str:
    """The first `version = "..."` under [project]. Avoids a TOML dependency."""
    project = read(path).split("[project]", 1)[-1]
    match = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
    if match is None:
        raise SystemExit(f"{path}: no version under [project]")
    return match.group(1)


def dunder(path: str, name: str) -> str:
    match = re.search(rf'^{name}\s*=\s*"([^"]+)"', read(path), re.MULTILINE)
    if match is None:
        raise SystemExit(f"{path}: no {name}")
    return match.group(1)


def dunder_int(path: str, name: str) -> int:
    match = re.search(rf"^{name}\s*=\s*(\d+)", read(path), re.MULTILINE)
    if match is None:
        raise SystemExit(f"{path}: no {name}")
    return int(match.group(1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="release tag to check against, e.g. v1.0.0")
    args = parser.parse_args()

    site = json.loads(read("docs/src/_data/site.json"))

    versions = {
        "server/pyproject.toml": pyproject_version("server/pyproject.toml"),
        "tns_mirror_server/__init__.py": dunder(
            "server/src/tns_mirror_server/__init__.py", "__version__"
        ),
        "client/pyproject.toml": pyproject_version("client/pyproject.toml"),
        "tns_mirror_client/__init__.py": dunder(
            "client/src/tns_mirror_client/__init__.py", "__version__"
        ),
        "docs/src/_data/site.json": site["version"],
    }

    schema_versions = {
        "tns_mirror_server/__init__.py": dunder_int(
            "server/src/tns_mirror_server/__init__.py", "SCHEMA_VERSION"
        ),
        "tns_mirror_client/client.py": dunder_int(
            "client/src/tns_mirror_client/client.py", "SCHEMA_VERSION"
        ),
        "docs/src/_data/site.json": site["schemaVersion"],
    }

    problems: list[str] = []

    def describe(values: dict[str, object]) -> str:
        width = max(len(name) for name in values)
        return "\n".join(f"      {name:<{width}}  {value}" for name, value in values.items())

    if len(set(versions.values())) != 1:
        problems.append(
            "the project version disagrees across files — both artifacts ship "
            "one version:\n" + describe(versions)
        )

    if len(set(schema_versions.values())) != 1:
        problems.append("the schema version disagrees across files:\n" + describe(schema_versions))

    version = versions["server/pyproject.toml"]
    schema = schema_versions["tns_mirror_server/__init__.py"]
    major = version.split(".")[0]

    if major != str(schema):
        problems.append(
            f"version {version} claims major {major}, but the schema is v{schema}. "
            f"The major version *is* the schema version: a consumer pinning "
            f"'tns-mirror-client>={major},<{int(major) + 1}' is pinning the contract. "
            f"See schema/README.md."
        )

    if args.tag and args.tag.removeprefix("v") != version:
        problems.append(
            f"tag {args.tag} does not match the project version {version}. "
            f"Either bump the five files above, or tag v{version}."
        )

    if problems:
        print("Version check failed:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}\n", file=sys.stderr)
        return 1

    print(f"version {version} — speaks schema v{schema}")
    print("  server  → Docker Hub (never PyPI)")
    print("  client  → PyPI")
    if args.tag:
        print(f"  tag {args.tag} matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
