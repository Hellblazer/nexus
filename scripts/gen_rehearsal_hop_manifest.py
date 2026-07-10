#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate the rehearsal OLD_TAG changeset-snapshot manifest (nexus-gm38i).

Writes ``tests/data/rehearsal_old_tag_changesets.json``: the set of Liquibase
``(id, author)`` changeset pairs present in the changelog tree at the
schema-upgrade rehearsal's OLD_TAG. The seed-coverage lint
(``tests/test_rehearsal_seed_coverage_lint.py``) subtracts this snapshot from
the HEAD changelog's FORCE-RLS row-DML changeset set to derive what the
old-to-HEAD hop actually runs — and fails CI if the rehearsal's declared seed
coverage drifts from it.

Why a committed snapshot instead of git-at-test-time: the lint then has NO
skip path (no tag-fetch dependency in Python CI, no network) and cannot
vacuously pass — and because release tags are immutable, a snapshot keyed by
tag can never rot silently: either it is for the current OLD_TAG (content
immutable) or the lint's tag-parity check fails loudly.

INTEGRITY FIELD (nexus-4sl9k, substantive-critic Critical on the first cut):
because everything in the snapshot is SUBTRACTED from the required-coverage
set, a single hand-added JSON entry (claiming a new hop changeset "already
existed at OLD_TAG") would silently defeat the gate with zero diff to the
declaration or the Java rehearsal. The manifest therefore carries a sha256
over its canonical content, computed ONLY here — from git truth — and
recomputed/verified by the lint on every run. A hand-edit that does not
re-run this script breaks the hash loudly; re-running the script regenerates
from the immutable tag, which cannot contain the fabricated entry. Residual
(documented, accepted): deliberately forging the hash by hand is possible —
but that is no longer an accident or a low-scrutiny diff, and requires the
same intent as editing the lint's own assertions.

Run this ONLY when OLD_TAG rotates (the engine-release skill's post-deploy
"bump downstream refs" step):

    uv run python scripts/gen_rehearsal_hop_manifest.py

The OLD_TAG is read from SchemaUpgradeRehearsalIntegrationTest.java — never
passed by hand, so the manifest cannot be generated for a tag the rehearsal
does not actually use. A parse miss aborts loudly (the run.sh nexus-b6qlf
lesson: derivations must fail loud, never fall back).

This module doubles as the shared home for the OLD_TAG parse and the
integrity-hash canonicalization (``pyproject.toml`` puts ``scripts`` on the
test pythonpath) — the lint imports them from here so the two sides can
never regex- or hash-drift.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
JAVA_TEST = (
    REPO_ROOT
    / "service/src/test/java/dev/nexus/service/SchemaUpgradeRehearsalIntegrationTest.java"
)
CHANGELOG_PATHSPEC = "service/src/main/resources/db/changelog"
MANIFEST_PATH = REPO_ROOT / "tests/data/rehearsal_old_tag_changesets.json"

_OLD_TAG_RE = re.compile(
    r'private\s+static\s+final\s+String\s+OLD_TAG\s*=\s*"([^"]+)"\s*;'
)
_XSD_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"


def parse_old_tag(java_source: str) -> str:
    """Strict OLD_TAG parse — a miss raises, never falls back (nexus-b6qlf)."""
    m = _OLD_TAG_RE.search(java_source)
    if m is None:
        raise AssertionError(
            "could not parse OLD_TAG out of SchemaUpgradeRehearsalIntegrationTest"
            ".java — the constant moved or was renamed; fix _OLD_TAG_RE in "
            "scripts/gen_rehearsal_hop_manifest.py (shared with the "
            "seed-coverage lint). No fallback (nexus-b6qlf lesson)."
        )
    return m.group(1)


def manifest_integrity(tag: str, changesets: list[dict[str, str]]) -> str:
    """Canonical content hash over (tag, changesets) — nexus-4sl9k."""
    canonical = json.dumps(
        {"tag": tag, "changesets": sorted(changesets, key=lambda c: (c["file"], c["id"], c["author"]))},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def main() -> None:
    old_tag = parse_old_tag(JAVA_TEST.read_text())

    try:
        files = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", old_tag, "--", CHANGELOG_PATHSPEC],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except subprocess.CalledProcessError as exc:
        sys.exit(
            f"FATAL: git ls-tree failed for tag {old_tag!r} "
            f"(stderr: {exc.stderr.strip()}) — is the tag fetched locally? "
            f"(git fetch origin tag {old_tag})"
        )
    xml_files = [f for f in files if f.endswith(".xml")]
    if not xml_files:
        sys.exit(
            f"FATAL: tag {old_tag!r} yielded no changelog files under "
            f"{CHANGELOG_PATHSPEC} — wrong tag, or the changelog moved."
        )

    changesets: list[dict[str, str]] = []
    for path in sorted(xml_files):
        content = subprocess.run(
            ["git", "show", f"{old_tag}:{path}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        root = ET.fromstring(content)
        for cs in root.iter(f"{_XSD_NS}changeSet"):
            changesets.append(
                {
                    "id": cs.get("id", ""),
                    "author": cs.get("author", ""),
                    "file": Path(path).name,
                }
            )

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(
            {
                "_comment": (
                    "Generated by scripts/gen_rehearsal_hop_manifest.py — "
                    "regenerate ONLY when SchemaUpgradeRehearsalIntegrationTest"
                    ".java's OLD_TAG rotates; NEVER hand-edit (the integrity "
                    "hash is verified by tests/test_rehearsal_seed_coverage_"
                    "lint.py, nexus-gm38i / nexus-4sl9k)."
                ),
                "tag": old_tag,
                "integrity": manifest_integrity(old_tag, changesets),
                "changesets": changesets,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(  # noqa: T201 — operator-facing script output (check_engine_release_floor.py precedent)
        f"wrote {MANIFEST_PATH} ({len(changesets)} changesets at {old_tag})"
    )


if __name__ == "__main__":
    main()
