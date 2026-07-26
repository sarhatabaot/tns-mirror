#!/usr/bin/env python3
"""Fail if any security waiver has expired, or is missing its paperwork.

Every suppression carries a justification *and* an expiry, and the expiry is
enforced by this hard-failing CI step. A waiver without a date is a permanent
hole that nobody ever revisits; a waiver whose date has passed is a decision that
was made for circumstances that no longer apply.

Uses only the standard library so it runs before any dependency is installed.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WAIVERS = REPO_ROOT / "security" / "waivers.yml"

REQUIRED = ("id", "reason", "expires", "owner")
_ENTRY = re.compile(r"^\s*-\s+id:", re.MULTILINE)


def parse(text: str) -> list[dict[str, str]]:
    """Read the small fixed subset of YAML this file uses.

    Deliberately not PyYAML: this runs as the first CI step, before any
    dependency is installed, so it must not need one.
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.strip().startswith("#") else ""
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            if current:
                entries.append(current)
            current = {}
            line = line.replace("- ", "", 1)
        if current is None:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip().strip("'\"")

    if current:
        entries.append(current)
    return entries


def main() -> int:
    if not WAIVERS.exists():
        print(f"no waiver file at {WAIVERS.relative_to(REPO_ROOT)} — nothing to check")
        return 0

    text = WAIVERS.read_text(encoding="utf-8")
    entries = parse(text)
    if len(entries) != len(_ENTRY.findall(text)):  # pragma: no cover - defensive
        print("could not parse the waiver file cleanly", file=sys.stderr)
        return 1

    if not entries:
        print("no active waivers")
        return 0

    today = dt.date.today()
    problems: list[str] = []

    for entry in entries:
        identifier = entry.get("id", "<no id>")

        missing = [field for field in REQUIRED if not entry.get(field)]
        if missing:
            problems.append(f"{identifier}: missing {', '.join(missing)}")
            continue

        try:
            expires = dt.date.fromisoformat(entry["expires"])
        except ValueError:
            problems.append(f"{identifier}: expires must be YYYY-MM-DD, got {entry['expires']!r}")
            continue

        if expires < today:
            problems.append(
                f"{identifier}: EXPIRED on {expires} (owner: {entry['owner']}). "
                f"Fix the finding or consciously renew the waiver."
            )
        else:
            print(f"{identifier}: valid until {expires} ({entry['owner']})")

    if problems:
        print("\nExpired or malformed waivers:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"\n{len(entries)} waiver(s) checked, all current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
