# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 nexus-rplay.14: the Monitor-driven ``nx tuple watch`` CLI loop,
its SessionStart arming and the 30-minute re-arm rule were deleted
outright (Sam's decision of 2026-09-16, T2 nexus_rdr/211-decision-channel-
delivery-2026-09-16). Push delivery is now the session's own nexus MCP
server pushing over the Claude Code channel, with the ``UserPromptSubmit``
drain hook as the unconditional floor beneath it.

A straggler mention of the deleted surface reads as still-live: a docstring
or a skill instructing an agent to arm a command that no longer exists is
worse than silence, because it sends the reader chasing a dead end instead
of the real mechanism (``tuple_subscribe`` + the channel). This census
holds every one of the eight identifying literal strings of that surface
to zero, everywhere except a fixed allowlist of historical records where
the deleted shape is the whole point of the record.

Deliberately EXACT-SUBSTRING, not a regex with word boundaries or import
resolution: the point is textual absence, not whether some clever
respelling would still parse as Python. A false negative here is a
respelling nobody would plausibly write by accident; a false positive
costs one line in the allowlist, named and justified.

Non-vacuity (the nexus-moht0 doctrine): a scan that walked zero files would
report a hollow, unfalsifiable "clean" — the SCANNED_FILE_FLOOR assertion
below fails loud if the walk itself is broken, before the banned-string
assertion ever gets a chance to pass by finding nothing to check.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The eight literal strings that identify the deleted surface. Exact
#: substrings, checked independently per line.
BANNED_STRINGS: tuple[str, ...] = (
    "nexus.tuple_watch",
    "tuple_watch.py",
    "nx tuple watch",
    "tuple watch --instance",
    "MAILBOX WATCH",
    "watcher_alive",
    "tuple_watch_cmd",
    "_check_tuple_watch_permission",
)

#: Every directory this census walks, relative to the repo root.
_SCAN_ROOTS: tuple[str, ...] = ("src", "tests", "conexus", "docs", "scripts", "web")

#: Directories never descended into: VCS internals, caches, and build output.
_SKIP_DIR_NAMES = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".pytest_cache",
    "dist", "build",
})

#: Historical records where the deleted surface's exact shape is the
#: record, not a straggler: RDR files narrate what was designed and later
#: removed, and changelogs narrate what shipped and what was deleted in a
#: past release -- both are supposed to say the old name.
_ALLOW_PREFIXES: tuple[str, ...] = ("docs/rdr/",)
_ALLOW_EXACT: frozenset[str] = frozenset({
    "docs/wire-contract-pending.md",
    "conexus/CHANGELOG.md",
})


def _is_allowlisted(rel_posix: str) -> bool:
    if rel_posix.startswith(_ALLOW_PREFIXES):
        return True
    if rel_posix in _ALLOW_EXACT:
        return True
    # CHANGELOG*.md anywhere in the tree (root CHANGELOG.md, and any other
    # changelog a subtree might carry) -- same historical-record reasoning
    # as the two exact entries above, generalised to the naming pattern.
    name = rel_posix.rsplit("/", 1)[-1]
    return name.startswith("CHANGELOG") and name.endswith(".md")


def _scanned_files() -> list[Path]:
    # This test's own file is excluded deliberately: it names all eight
    # banned strings itself, as the very list it checks other files
    # against, and self-matching there is not a straggler.
    self_path = Path(__file__).resolve()
    out: list[Path] = []
    for root_name in _SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name not in _SKIP_DIR_NAMES:
                        stack.append(entry)
                    continue
                if entry.resolve() == self_path:
                    continue
                out.append(entry)
    return out


def _offenders() -> list[str]:
    offenders: list[str] = []
    for path in _scanned_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if _is_allowlisted(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle in BANNED_STRINGS:
                if needle in line:
                    offenders.append(f"{rel}:{lineno}: {needle!r} in {line.strip()[:160]!r}")
    return offenders


#: A scan that walked fewer files than this is broken, not clean --
#: src/nexus alone carries several hundred Python files.
SCANNED_FILE_FLOOR = 500


def test_the_scan_sees_real_files() -> None:
    """Non-vacuity: a scan of zero (or a suspiciously small number of)
    files would make the assertion below pass by finding nothing to check,
    which is the false-clean failure this whole census exists to avoid."""
    count = len(_scanned_files())
    assert count >= SCANNED_FILE_FLOOR, (
        f"scanned only {count} files across {_SCAN_ROOTS}; expected at least "
        f"{SCANNED_FILE_FLOOR} -- the walk itself looks broken, not the tree clean"
    )


def test_no_straggler_references_to_the_deleted_watcher() -> None:
    offenders = _offenders()
    assert offenders == [], (
        "RDR-211 nexus-rplay.14 deleted the CLI mailbox-watch loop, its "
        "SessionStart arming and the 30-minute re-arm rule outright. A "
        "straggler mention of the deleted surface below reads as still "
        "live -- fix the reference (paraphrase, past tense, or point at "
        "the replacement: tuple_subscribe + the Claude Code channel), or "
        "add the file to this test's allowlist if it is a genuine "
        "historical record:\n  " + "\n  ".join(offenders)
    )
