#!/usr/bin/env python3
"""Keep the project's version numbers honest.

``VERSION`` at the repository root is the one file you edit. Everything else is
derived from it:

    python3 scripts/check_versions.py --write    # propagate VERSION everywhere
    python3 scripts/check_versions.py            # fail if anything drifted
    python3 scripts/check_versions.py --tag 1.0.4

Same shape as ``server/scripts/render_schema.py``: one source, a generator, and
CI failing on drift. The derived files hold literals rather than reading
``VERSION`` at runtime because the server and api images build with a *narrow*
context — ``server/`` and ``api/``, not the repository root — so nothing inside
them can reach a file at the top level. Each artifact stays self-contained;
this script is what keeps them agreeing.

**The major version is the schema version.** ``v1.x.y`` speaks schema v1 — the
server image and the client library alike — so a consumer who pins
``tns-mirror-client>=1,<2`` is pinning the contract, which is the thing that
actually matters to them. A breaking schema change bumps the major everywhere;
everything else is a minor or a patch.

The derived files:

    server/pyproject.toml
    server/src/tns_mirror_server/__init__.py    (__version__)
    client/pyproject.toml
    client/src/tns_mirror_client/__init__.py
    api/pyproject.toml
    api/src/tns_mirror_api/__init__.py
    docs/src/_data/site.json                    (version)

SCHEMA_VERSION is deliberately *not* derived from VERSION. It changes on its own
rare, deliberate occasions, and is still cross-checked below.

Standard library only, so this runs before anything is installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = "VERSION"

#: Every derived location, as (label, path, pattern). Group 1 of each pattern is
#: the literal to read or rewrite, so one table drives both --write and --check.
DERIVED: list[tuple[str, str, str]] = [
    (
        "server/pyproject.toml",
        "server/pyproject.toml",
        r'(?m)^version\s*=\s*"([^"]+)"',
    ),
    (
        "tns_mirror_server/__init__.py",
        "server/src/tns_mirror_server/__init__.py",
        r'(?m)^__version__\s*=\s*"([^"]+)"',
    ),
    (
        "client/pyproject.toml",
        "client/pyproject.toml",
        r'(?m)^version\s*=\s*"([^"]+)"',
    ),
    (
        "tns_mirror_client/__init__.py",
        "client/src/tns_mirror_client/__init__.py",
        r'(?m)^__version__\s*=\s*"([^"]+)"',
    ),
    (
        "api/pyproject.toml",
        "api/pyproject.toml",
        r'(?m)^version\s*=\s*"([^"]+)"',
    ),
    (
        "tns_mirror_api/__init__.py",
        "api/src/tns_mirror_api/__init__.py",
        r'(?m)^__version__\s*=\s*"([^"]+)"',
    ),
    (
        "docs/src/_data/site.json",
        "docs/src/_data/site.json",
        r'("version"\s*:\s*)"([^"]+)"',
    ),
]


#: Image tags and version pins in the compose files and the documentation.
#: Unlike the files above these may hold several pins each, and all of them must
#: equal VERSION — a quickstart telling people to pull a superseded image is a
#: silent way to ship the wrong thing. Anchored on distinctive text so a version
#: number that means something else is never rewritten.
PIN_PATTERNS = [
    # image: sarhatabaot/tns-mirror-api:1.0.4
    # image: tns-mirror-server:${TNS_MIRROR_VERSION:-1.0.4}
    r"(tns-mirror-(?:server|api):(?:\$\{TNS_MIRROR_VERSION:-)?)(\d+\.\d+\.\d+)",
    # TNS_MIRROR_VERSION=1.0.4
    r"(TNS_MIRROR_VERSION=)(\d+\.\d+\.\d+)",
    # - **Tags:** `1.0.4`, `1.0`, `latest`   (the `1.0` alias is left alone)
    r"(\*\*Tags:\*\* `)(\d+\.\d+\.\d+)",
]

PINNED_FILES = [
    "quickstart/docker-compose.minimal.yml",
    "quickstart/docker-compose.api.yml",
    "quickstart/docker-compose.api-gateway.yml",
    "quickstart/docker-compose.full.yml",
    "quickstart/.env.example",
    "server/docker-compose.yml",
    "server/DOCKERHUB.md",
    "api/DOCKERHUB.md",
    "api/README.md",
    "docs/src/quickstart.md",
]


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def project_section(text: str, path: str) -> tuple[int, int]:
    """Bounds of the ``[project]`` table, so a `version =` elsewhere is not hit."""
    start = text.find("[project]")
    if start < 0:
        raise SystemExit(f"{path}: no [project] table")
    start += len("[project]")
    nxt = re.search(r"(?m)^\[", text[start:])
    return start, start + nxt.start() if nxt else len(text)


def span(path: str, pattern: str) -> tuple[str, int, int]:
    """The file's text and the offsets of the version literal within it."""
    text = read(path)
    lo, hi = project_section(text, path) if path.endswith("pyproject.toml") else (0, len(text))
    match = re.search(pattern, text[lo:hi])
    if match is None:
        raise SystemExit(f"{path}: no version found")
    # The last group is the literal; earlier groups are context we keep.
    group = match.lastindex or 1
    return text, lo + match.start(group), lo + match.end(group)


def current(path: str, pattern: str) -> str:
    text, lo, hi = span(path, pattern)
    return text[lo:hi]


def write(path: str, pattern: str, version: str) -> bool:
    """Set this file's version. True if it changed."""
    text, lo, hi = span(path, pattern)
    if text[lo:hi] == version:
        return False
    (REPO_ROOT / path).write_text(text[:lo] + version + text[hi:], encoding="utf-8")
    return True


def pin_versions(path: str) -> list[str]:
    """Every pinned version in this file, in order."""
    text = read(path)
    return [m.group(2) for pat in PIN_PATTERNS for m in re.finditer(pat, text)]


def write_pins(path: str, version: str) -> int:
    """Set every pin in this file. Returns how many changed."""
    text = read(path)
    changed = 0
    for pattern in PIN_PATTERNS:

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            if match.group(2) != version:
                changed += 1
            return match.group(1) + version

        text = re.sub(pattern, replace, text)
    if changed:
        (REPO_ROOT / path).write_text(text, encoding="utf-8")
    return changed


def dunder_int(path: str, name: str) -> int:
    match = re.search(rf"^{name}\s*=\s*(\d+)", read(path), re.MULTILINE)
    if match is None:
        raise SystemExit(f"{path}: no {name}")
    return int(match.group(1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="release tag to check against, e.g. 1.0.4")
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"propagate {VERSION_FILE} into every derived file",
    )
    args = parser.parse_args()

    source = read(VERSION_FILE).strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", source):
        raise SystemExit(f"{VERSION_FILE}: expected a bare X.Y.Z version, got {source!r}")

    if args.write:
        changed = [label for label, path, pat in DERIVED if write(path, pat, source)]
        for label in changed:
            print(f"  updated  {label}")
        for path in PINNED_FILES:
            count = write_pins(path, source)
            if count:
                changed.append(path)
                print(f"  updated  {path}  ({count} pin{'s' if count > 1 else ''})")
        total = len(DERIVED) + len(PINNED_FILES)
        print(
            f"version {source} — {len(changed)} updated, "
            f"{total - len(changed)} already current"
        )

    versions = {label: current(path, pat) for label, path, pat in DERIVED}

    schema_versions = {
        "tns_mirror_server/__init__.py": dunder_int(
            "server/src/tns_mirror_server/__init__.py", "SCHEMA_VERSION"
        ),
        "tns_mirror_client/client.py": dunder_int(
            "client/src/tns_mirror_client/client.py", "SCHEMA_VERSION"
        ),
        "docs/src/_data/site.json": json.loads(read("docs/src/_data/site.json"))[
            "schemaVersion"
        ],
    }

    problems: list[str] = []

    def describe(values: dict[str, object]) -> str:
        width = max(len(name) for name in values)
        return "\n".join(f"      {name:<{width}}  {value}" for name, value in values.items())

    drifted = {label: got for label, got in versions.items() if got != source}

    # Pinned image tags, which a reader copies verbatim: a stale one here sends
    # someone to pull a superseded image, and nothing else would catch it.
    for path in PINNED_FILES:
        stale = sorted({got for got in pin_versions(path) if got != source})
        if stale:
            drifted[path] = ", ".join(stale)

    if drifted:
        problems.append(
            f"these do not match {VERSION_FILE} ({source}) — run "
            f"'python3 scripts/check_versions.py --write':\n" + describe(drifted)
        )

    if len(set(schema_versions.values())) != 1:
        problems.append(
            "the schema version disagrees across files:\n" + describe(schema_versions)
        )

    schema = schema_versions["tns_mirror_server/__init__.py"]
    major = source.split(".")[0]

    if major != str(schema):
        problems.append(
            f"{VERSION_FILE} says {source}, so major {major}, but the schema is v{schema}. "
            f"The major version *is* the schema version: a consumer pinning "
            f"'tns-mirror-client>={major},<{int(major) + 1}' is pinning the contract. "
            f"See schema/README.md."
        )

    # The API depends on the published client. If that range ever drifted off
    # the contract major, the API could resolve a client speaking a different
    # schema than the mirror it is pointed at.
    expected_pin = f'"tns-mirror-client>={major},<{int(major) + 1}"'
    if expected_pin not in read("api/pyproject.toml"):
        problems.append(
            f"api/pyproject.toml should depend on {expected_pin} to stay inside "
            f"the schema major it serves"
        )

    if args.tag and args.tag.removeprefix("v") != source:
        problems.append(
            f"tag {args.tag} does not match {VERSION_FILE} ({source}). Either edit "
            f"{VERSION_FILE} and run --write, or tag {source}."
        )

    if problems:
        print("Version check failed:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}\n", file=sys.stderr)
        return 1

    print(f"version {source} (from {VERSION_FILE}) — speaks schema v{schema}")
    print("  server  → Docker Hub (never PyPI)")
    print("  client  → PyPI")
    print("  api     → Docker Hub (never PyPI)")
    if args.tag:
        print(f"  tag {args.tag} matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
