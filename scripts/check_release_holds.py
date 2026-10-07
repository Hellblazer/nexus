#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Refuse an engine release whose tree carries a held changeset (nexus-3wh8d.28).

An engine tag is cut from develop's tip, so a changeset that has landed on develop ahead of its release gates
ships with the next engine tag of ANY purpose: to the cloud on deploy, and to every local install through the
client's REQUIRED_ENGINE_VERSION pin. RDR-225's ``vectors-030-1`` is the case that made this a script: a one-way
walk on develop while its read path, reviews and production rehearsal were still open.

``scripts/release-holds.txt`` names the held changesets, one per line::

    <changeset-id> <bead> <reason...>

Blank lines and ``#`` comments are ignored. The check fails when a held id is defined anywhere under
``service/src/main/resources/db/changelog``. Releasing a changeset is deleting its line in a reviewed commit.

A hold naming no changeset in the tree is an error, never a pass: a typo would otherwise release the changeset
it meant to hold. A missing holds file is an error too.

Run by the engine-release skill before the tag is pushed, and by ``engine-service-release.yml`` before it
creates the release, against the tagged tree. Exit 0: nothing held. Exit 1: held changesets present (one
``RELEASE_HOLD`` line each). Exit 2: the holds file is malformed or names a missing changeset.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HOLDS_FILE = Path("scripts") / "release-holds.txt"
CHANGELOG_DIR = Path("service") / "src" / "main" / "resources" / "db" / "changelog"

_CHANGESET_ID = re.compile(r'<changeSet\b[^>]*?\bid="([^"]+)"')


class HoldsError(Exception):
    """The holds file is missing, malformed, or names a changeset the tree does not define."""


@dataclass(frozen=True)
class Hold:
    changeset: str
    bead: str
    reason: str
    file: str


def _read_holds(root: Path) -> list[tuple[str, str, str]]:
    path = root / HOLDS_FILE
    if not path.is_file():
        raise HoldsError(f"{HOLDS_FILE} not found under {root}")
    holds = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            raise HoldsError(f"{HOLDS_FILE} line {n}: expected '<changeset-id> <bead> <reason>', got {raw!r}")
        holds.append((parts[0], parts[1], parts[2]))
    return holds


def _defined_changesets(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    d = root / CHANGELOG_DIR
    if d.is_dir():
        for xml in sorted(d.rglob("*.xml")):
            for cid in _CHANGESET_ID.findall(xml.read_text(encoding="utf-8")):
                found.setdefault(cid, str(xml.relative_to(root)))
    return found


def check(root: Path) -> list[Hold]:
    """Return the held changesets the tree at ``root`` defines; raise HoldsError on a bad holds file."""
    holds = _read_holds(root)
    defined = _defined_changesets(root)
    missing = [cid for cid, _, _ in holds if cid not in defined]
    if missing:
        raise HoldsError(f"{HOLDS_FILE} holds changesets the tree does not define: {', '.join(missing)}")
    return [Hold(cid, bead, reason, defined[cid]) for cid, bead, reason in holds]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Refuse an engine release whose tree carries a held changeset.")
    ap.add_argument("--root", type=Path, default=Path("."), help="tree to check (default: the current directory)")
    args = ap.parse_args(argv)
    try:
        held = check(args.root)
    except HoldsError as e:
        print(f"RELEASE_HOLDS_INVALID {e}")
        return 2
    for h in held:
        print(f"RELEASE_HOLD {h.changeset} ({h.file}) held by {h.bead}: {h.reason}")
    if held:
        print("Refusing: this tree carries held changesets. Release one by deleting its line in "
              f"{HOLDS_FILE} in a reviewed commit, once its bead says it may ship.")
        return 1
    print("RELEASE_HOLDS_CLEAR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
