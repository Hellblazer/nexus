"""Tests for expectations_archive (bead nexus-4bqre.1).

The archive preserves RDR-184 ledgers past expectations_sweep's 7-day reap.
Its acceptance is behavioural, not textual: idempotent, append-only, ordered
BEFORE the sweep, and never able to fail the hook it runs on.
"""

from __future__ import annotations

import os
import shutil
import textwrap
from pathlib import Path

import pytest

from nexus.hooks import expectations

REPO = Path(__file__).resolve().parents[2]
#: The SubagentStop hook that must call archive before sweep. Ported to
#: Python at RDR-215 bead nexus-q02nx.17 (``src/nexus/hooks/subagent_stop.py``);
#: the bash original is no longer wired in hooks.json (SubagentStop
#: dispatches to the ``hook_subagent_stop`` mcp_tool) and is retired at
#: bead nexus-q02nx.21 along with the shell ledger library it sourced --
#: this constant follows the wiring, not the file that used to carry it.
SUBAGENT_STOP = REPO / "src" / "nexus" / "hooks" / "subagent_stop.py"


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


class _UnsupportedVerb(Exception):
    """A test passed a verb name this helper does not dispatch."""


def _run(script: str, state: Path) -> _Result:
    """Run one or more ledger verbs against ``nexus.hooks.expectations``.

    ``script`` is one or more bare verb names (``expectations_archive``,
    ``expectations_sweep``), one per line -- the composite two-verb form
    drives the archive-before-sweep ordering tests below. Verb-by-verb
    dispatch (rather than matching the whole script against one name)
    matters here: a composite script run as a single lookup would match
    nothing and silently do nothing, which the bead .9 critique found
    happening under the retired bash-differential setup.
    """
    verbs = [line.strip() for line in textwrap.dedent(script).splitlines() if line.strip()]
    supported = {
        "expectations_archive": expectations.expectations_archive,
        "expectations_sweep": expectations.expectations_sweep,
    }
    unsupported = [v for v in verbs if v not in supported]
    if unsupported:
        raise _UnsupportedVerb(f"{unsupported!r} is not a ledger verb this helper knows.")
    prior = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(state)
    try:
        for verb in verbs:
            supported[verb]()
    finally:
        if prior is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = prior
    return _Result()


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
        proc = _run("expectations_archive", state)
        assert proc.returncode == 0, proc.stderr
        assert (_archive(state) / "s1.expectations").read_text() == (
            "a\tSTART\tid1\tconexus:developer\n"
        )

    def test_second_run_adds_new_files_without_duplicating_or_mutating(self, state):
        first = _live(state) / "s1.expectations"
        first.write_text("row1\n")
        _run("expectations_archive", state)
        before = (_archive(state) / "s1.expectations").stat()

        (_live(state) / "s2.expectations").write_text("row2\n")
        proc = _run("expectations_archive", state)
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
        _run("expectations_archive", state)

        live.write_text("row1\nrow2\n")
        os.utime(live, (live.stat().st_atime + 10, live.stat().st_mtime + 10))
        _run("expectations_archive", state)

        assert (_archive(state) / "s1.expectations").read_text() == "row1\nrow2\n"

    def test_never_deletes_an_archived_ledger_absent_from_live(self, state):
        # This is the whole point: a swept ledger must survive in the archive.
        (_archive(state)).mkdir(parents=True, exist_ok=True)
        (_archive(state) / "gone.expectations").write_text("historical\n")
        (_live(state) / "s1.expectations").write_text("row\n")
        _run("expectations_archive", state)
        assert (_archive(state) / "gone.expectations").read_text() == "historical\n"

    def test_survives_an_empty_and_a_missing_live_dir(self, state):
        assert _run("expectations_archive", state).returncode == 0
        shutil.rmtree(_live(state))
        assert _run("expectations_archive", state).returncode == 0


class TestArchiveWinsTheRaceWithSweep:
    """The bead's ordering requirement, asserted rather than asserted-in-prose."""

    def test_archive_preserves_a_ledger_the_sweep_then_reaps(self, state):
        stale = _live(state) / "old.expectations"
        stale.write_text("START\tid\tconexus:developer\n")
        old = stale.stat().st_mtime - (9 * 86400)
        os.utime(stale, (old, old))

        proc = _run("expectations_archive\nexpectations_sweep", state)
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

        _run("expectations_sweep\nexpectations_archive", state)
        assert not (_archive(state) / "old.expectations").exists()

    def test_subagent_stop_calls_archive_before_sweep(self):
        # Re-pointed at the Python port (bead nexus-q02nx.21): the bash
        # trigger this used to read is no longer wired into hooks.json and
        # is retired along with the shell ledger library. The ordering
        # guarantee itself is unchanged -- src/nexus/hooks/subagent_stop.py
        # carries forward the same nexus-4bqre.1 comment and the same two
        # calls.
        body = SUBAGENT_STOP.read_text()
        assert "_exp.expectations_archive()" in body, "the trigger is not wired at all"
        assert body.index("_exp.expectations_archive()") < body.index(
            "_exp.expectations_sweep()"
        ), "archive must precede sweep in nexus.hooks.subagent_stop"
