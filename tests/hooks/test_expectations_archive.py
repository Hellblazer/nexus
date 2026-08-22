"""Tests for expectations_archive (bead nexus-4bqre.1).

The archive preserves RDR-184 ledgers past expectations_sweep's 7-day reap.
Its acceptance is behavioural, not textual: idempotent, append-only, ordered
BEFORE the sweep, and never able to fail the hook it runs on.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "tests" / "e2e" / "lib" / "expectations.sh"
PLUGIN_LIB = REPO / "conexus" / "hooks" / "scripts" / "expectations.sh"
SUBAGENT_STOP = REPO / "conexus" / "hooks" / "scripts" / "subagent-stop.sh"


def _bash(script: str, state: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, XDG_STATE_HOME=str(state), HOME=str(state / "home"))
    return subprocess.run(
        ["bash", "-c", f"source {LIB}\n{textwrap.dedent(script)}"],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def state(tmp_path: Path) -> Path:
    st = tmp_path / "state"
    (st / "nexus" / "orchestration").mkdir(parents=True)
    (st / "home").mkdir()
    return st


def _live(state: Path) -> Path:
    return state / "nexus" / "orchestration"


def _archive(state: Path) -> Path:
    return state / "nexus" / "orchestration-archive"


class TestArchiveLeg:
    def test_copies_live_ledgers_into_the_archive(self, state):
        (_live(state) / "s1.expectations").write_text("a\tSTART\tid1\tconexus:developer\n")
        proc = _bash("expectations_archive", state)
        assert proc.returncode == 0, proc.stderr
        assert (_archive(state) / "s1.expectations").read_text() == (
            "a\tSTART\tid1\tconexus:developer\n"
        )

    def test_second_run_adds_new_files_without_duplicating_or_mutating(self, state):
        first = _live(state) / "s1.expectations"
        first.write_text("row1\n")
        _bash("expectations_archive", state)
        before = (_archive(state) / "s1.expectations").stat()

        (_live(state) / "s2.expectations").write_text("row2\n")
        proc = _bash("expectations_archive", state)
        assert proc.returncode == 0

        names = sorted(p.name for p in _archive(state).glob("*.expectations"))
        assert names == ["s1.expectations", "s2.expectations"]
        after = (_archive(state) / "s1.expectations").stat()
        assert (after.st_size, after.st_mtime) == (before.st_size, before.st_mtime)

    def test_refreshes_an_archived_ledger_that_has_since_grown(self, state):
        # Ledgers grow by append. A pure skip-if-exists archive would freeze
        # the first snapshot and lose every row written afterwards.
        live = _live(state) / "s1.expectations"
        live.write_text("row1\n")
        _bash("expectations_archive", state)

        live.write_text("row1\nrow2\n")
        os.utime(live, (live.stat().st_atime + 10, live.stat().st_mtime + 10))
        _bash("expectations_archive", state)

        assert (_archive(state) / "s1.expectations").read_text() == "row1\nrow2\n"

    def test_never_deletes_an_archived_ledger_absent_from_live(self, state):
        # This is the whole point: a swept ledger must survive in the archive.
        (_archive(state)).mkdir(parents=True, exist_ok=True)
        (_archive(state) / "gone.expectations").write_text("historical\n")
        (_live(state) / "s1.expectations").write_text("row\n")
        _bash("expectations_archive", state)
        assert (_archive(state) / "gone.expectations").read_text() == "historical\n"

    def test_survives_an_empty_and_a_missing_live_dir(self, state):
        assert _bash("expectations_archive", state).returncode == 0
        import shutil
        shutil.rmtree(_live(state))
        assert _bash("expectations_archive", state).returncode == 0


class TestArchiveWinsTheRaceWithSweep:
    """The bead's ordering requirement, asserted rather than asserted-in-prose."""

    def test_archive_preserves_a_ledger_the_sweep_then_reaps(self, state):
        stale = _live(state) / "old.expectations"
        stale.write_text("START\tid\tconexus:developer\n")
        old = stale.stat().st_mtime - (9 * 86400)
        os.utime(stale, (old, old))

        proc = _bash("expectations_archive\nexpectations_sweep", state)
        assert proc.returncode == 0
        assert not stale.exists(), "fixture bug: sweep did not reap the stale ledger"
        assert (_archive(state) / "old.expectations").exists(), (
            "the archive ran before the sweep but the ledger was still lost"
        )

    def test_reversed_order_loses_the_ledger(self, state):
        # Proves the ordering assertion above is not vacuous: run the two in
        # the wrong order and the data is gone.
        stale = _live(state) / "old.expectations"
        stale.write_text("row\n")
        old = stale.stat().st_mtime - (9 * 86400)
        os.utime(stale, (old, old))

        _bash("expectations_sweep\nexpectations_archive", state)
        assert not (_archive(state) / "old.expectations").exists()

    def test_subagent_stop_calls_archive_before_sweep(self):
        body = SUBAGENT_STOP.read_text()
        assert "expectations_archive" in body, "the trigger is not wired at all"
        assert body.index("expectations_archive") < body.index("\nexpectations_sweep"), (
            "archive must precede sweep in subagent-stop.sh"
        )


class TestBothLibraryCopiesStayIdentical:
    """CLAUDE.md: edit tests/e2e/lib/expectations.sh, copy it over, never the reverse."""

    def test_plugin_copy_is_byte_identical(self):
        assert LIB.read_bytes() == PLUGIN_LIB.read_bytes()

    def test_both_copies_define_the_archive_function(self):
        for path in (LIB, PLUGIN_LIB):
            assert "expectations_archive()" in path.read_text(), path
