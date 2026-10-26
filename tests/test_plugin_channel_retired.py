# SPDX-License-Identifier: AGPL-3.0-or-later
"""The anchored plugin channel (RDR-197, plugin-vX.Y.Z-n cuts) is retired.

Sam retired it on 2026-10-26 (nexus-rbmja.8); the plugin-only fast lane in
the ``release`` skill replaced it. This lint pins that the cut machinery
stays gone and that no tracked file still points at it.

Non-vacuity: the scan must cover at least ``MIN_SCANNED`` tracked files, so
a failed ``git ls-files`` or a wrong root cannot pass it by examining
nothing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parent.parent

#: Paths that must not exist.
DELETED_PATHS = (
    "scripts/cut_plugin_release.py",
    "scripts/plugin_cut_back_merge.sh",
    "scripts/check_cut_ledger_clean.py",
    ".github/workflows/plugin-release.yml",
    "tests/e2e/plugin-cut-rehearsal",
    "tests/test_cut_plugin_release.py",
    "tests/test_plugin_release_workflow.py",
)

#: Names no tracked file may carry.
RETIRED_NAMES = (
    "cut_plugin_release",
    "plugin_cut_back_merge",
    "plugin-release.yml",
    "check_cut_ledger_clean",
    "plugin-cut-rehearsal",
)

#: Record locations: history is not rewritten.
EXEMPT_PREFIXES = ("docs/rdr/", "scripts/experiments/out/")
EXEMPT_BASENAMES = ("CHANGELOG.md", ".test_durations.json")
#: Files whose job is to name the retired things: this lint, and the skill
#: test that asserts the fast-lane section carries none of them.
EXEMPT_FILES = (
    "tests/test_plugin_channel_retired.py",
    "tests/scripts/test_release_skill_fast_lane.py",
)

MIN_SCANNED = 1000


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return [p for p in out.split("\0") if p and (REPO / p).is_file()]


@pytest.mark.parametrize("rel", DELETED_PATHS)
def test_retired_path_is_gone(rel: str) -> None:
    assert not (REPO / rel).exists(), f"{rel} is retired (nexus-rbmja.8) and must not exist"


def test_no_tracked_file_names_a_retired_script() -> None:
    files = _tracked()
    scanned = 0
    offenders: list[str] = []
    for rel in files:
        if rel.startswith(EXEMPT_PREFIXES) or Path(rel).name in EXEMPT_BASENAMES or rel in EXEMPT_FILES:
            continue
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        offenders += [f"{rel}: {name}" for name in RETIRED_NAMES if name in text]
    assert scanned >= MIN_SCANNED, f"scan covered {scanned} files, expected at least {MIN_SCANNED}"
    assert not offenders, "retired plugin-channel names still referenced:\n" + "\n".join(offenders)
