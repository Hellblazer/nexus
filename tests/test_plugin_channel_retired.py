# SPDX-License-Identifier: AGPL-3.0-or-later
"""The anchored plugin channel (RDR-197, plugin-vX.Y.Z-n cuts) is retired.

Sam retired it on 2026-10-26 (nexus-rbmja.8); the plugin-only fast lane in
the ``release`` skill replaced it. This lint pins that the cut machinery
stays gone and that no tracked file still points at it.

Non-vacuity: the scan must cover at least ``MIN_SCANNED`` tracked files, so
a failed ``git ls-files`` or a wrong root cannot pass it by examining
nothing. The scan itself is :func:`scan`, a function over a file list and a
reader, so ``test_scan_*`` can plant an offender and an exempt hit and watch
it report the first and skip the second (a lint that was never shown to fire
proves nothing, nexus-moht0).

A tracked file that is not valid UTF-8 (the PNGs, PDFs, a bundle) is read with
``errors="replace"`` and still scanned: skipping it would let a text needle
hide in a file only because it also holds binary bytes, and the count would
say it was examined. An unreadable file (``OSError``) is an offender, not a
skip.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
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
    "cut-plugin-release",
    "plugin_cut_back_merge",
    "plugin-release.yml",
    "plugin-release/",
    "plugin_release_workflow",
    "check_cut_ledger_clean",
    "plugin-cut-rehearsal",
)

#: Record locations, by full path or prefix: history is not rewritten.
EXEMPT_PREFIXES = ("docs/rdr/",)
EXEMPT_FILES = (
    "CHANGELOG.md",
    "conexus/CHANGELOG.md",
    # This lint names every retired thing.
    "tests/test_plugin_channel_retired.py",
    # Asserts the skill's fast-lane section carries none of them, so it must
    # name them.
    "tests/scripts/test_release_skill_fast_lane.py",
    # The drift test's release window (condition d) and its channel helpers
    # still read the cut BRANCH NAME ``plugin-release/{version}-{n}``. Nothing
    # produces such a branch any more, so the window never opens; deleting that
    # machinery is its own change (the RDR-197 invariants are pinned by these
    # three files), and only that one spelling is exempt here.
    "scripts/plugin_channel.py",
    "tests/test_plugin_channel.py",
    "tests/test_plugin_release_drift_ledger.py",
)

MIN_SCANNED = 3000


def is_exempt(rel: str) -> bool:
    return rel.startswith(EXEMPT_PREFIXES) or rel in EXEMPT_FILES


def scan(
    files: Iterable[str], read: Callable[[str], str]
) -> tuple[int, list[str]]:
    """(files scanned, offenders). *read* returns a file's text and may raise
    ``OSError``; an exempt file is neither scanned nor counted."""
    scanned = 0
    offenders: list[str] = []
    for rel in files:
        if is_exempt(rel):
            continue
        try:
            text = read(rel)
        except OSError as exc:
            offenders.append(f"{rel}: unreadable ({exc})")
            continue
        scanned += 1
        offenders += [f"{rel}: {name}" for name in RETIRED_NAMES if name in text]
    return scanned, offenders


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return [p for p in out.split("\0") if p and (REPO / p).is_file()]


def _read(rel: str, root: Path = REPO) -> str:
    return (root / rel).read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("rel", DELETED_PATHS)
def test_retired_path_is_gone(rel: str) -> None:
    assert not (REPO / rel).exists(), f"{rel} is retired (nexus-rbmja.8) and must not exist"


def test_no_tracked_file_names_a_retired_script() -> None:
    scanned, offenders = scan(_tracked(), _read)
    assert scanned >= MIN_SCANNED, f"scan covered {scanned} files, expected at least {MIN_SCANNED}"
    assert not offenders, "retired plugin-channel names still referenced:\n" + "\n".join(offenders)


# ---- the scan itself: positive controls ---------------------------------------


@pytest.mark.parametrize("name", RETIRED_NAMES)
def test_scan_reports_each_retired_name_in_a_planted_file(name: str) -> None:
    """Mutation: drop a name from RETIRED_NAMES, or skip the file."""
    scanned, offenders = scan(["scripts/x.py"], {"scripts/x.py": f"call {name} now"}.__getitem__)
    assert scanned == 1
    assert offenders == [f"scripts/x.py: {name}"]


@pytest.mark.parametrize(
    "rel",
    ["docs/rdr/rdr-197-x.md", "CHANGELOG.md", "conexus/CHANGELOG.md", "tests/scripts/test_release_skill_fast_lane.py"],
)
def test_scan_skips_an_exempt_file_and_does_not_count_it(rel: str) -> None:
    """Mutation: drop the exemption (the planted hit then reports) or count it."""
    scanned, offenders = scan([rel], lambda _r: "cut_plugin_release")
    assert (scanned, offenders) == (0, [])


@pytest.mark.parametrize("rel", ["services/CHANGELOG.md", "sn/CHANGELOG.md", "web/docs/rdr/x.md"])
def test_exemptions_are_full_paths_not_basenames_or_substrings(rel: str) -> None:
    """The old lint exempted every file NAMED CHANGELOG.md. Mutation: match on
    the basename or on a substring."""
    scanned, offenders = scan([rel], lambda _r: "cut_plugin_release")
    assert scanned == 1 and offenders == [f"{rel}: cut_plugin_release"]


def test_scan_counts_a_clean_file_and_reports_none() -> None:
    assert scan(["a.py", "b.py"], lambda _r: "nothing here") == (2, [])


def test_scan_reports_an_unreadable_file_instead_of_skipping_it() -> None:
    """Mutation: swallow OSError (the old lint did, for decode errors too)."""

    def read(_rel: str) -> str:
        raise PermissionError("denied")

    scanned, offenders = scan(["a.py"], read)
    assert scanned == 0
    assert len(offenders) == 1 and offenders[0].startswith("a.py: unreadable")


def test_a_binary_file_is_scanned_not_skipped(tmp_path: Path) -> None:
    """A tracked file that fails UTF-8 decoding is still read, and a needle in
    its text bytes is found. Mutation: skip on UnicodeDecodeError."""
    blob = tmp_path / "b.bin"
    blob.write_bytes(b"\xff\xfe\x00 cut_plugin_release \x80")
    scanned, offenders = scan(["b.bin"], lambda rel: _read(rel, tmp_path))
    assert scanned == 1 and offenders == ["b.bin: cut_plugin_release"]
    with pytest.raises(UnicodeDecodeError):
        blob.read_text(encoding="utf-8")  # the strict read the lint no longer uses


def test_the_name_list_keeps_every_known_spelling_and_a_real_floor() -> None:
    """The positive controls above iterate RETIRED_NAMES, so they cannot notice
    a name dropped from it. Mutation: delete any of these from RETIRED_NAMES,
    or lower MIN_SCANNED."""
    for spelling in (
        "cut_plugin_release", "cut-plugin-release", "plugin_cut_back_merge",
        "plugin-release.yml", "plugin-release/", "plugin_release_workflow",
        "check_cut_ledger_clean", "plugin-cut-rehearsal",
    ):
        assert spelling in RETIRED_NAMES, spelling
    assert MIN_SCANNED >= 3000
