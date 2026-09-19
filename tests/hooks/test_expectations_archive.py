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

from nexus.hooks import expectations

REPO = Path(__file__).resolve().parents[2]
PLUGIN_LIB = REPO / "conexus" / "hooks" / "scripts" / "expectations.sh"
#: The differential drives the PLUGIN copy now that the reference copy
#: is deleted (RDR-215 bead nexus-q02nx.14). It moved rather than died
#: on purpose: this copy is what the wired hooks.json entries actually
#: run until bead .21 re-points them, so it is the copy worth being
#: equal to. The "bash" param goes when it does.
SUBAGENT_STOP = REPO / "conexus" / "hooks" / "scripts" / "subagent-stop.sh"


#: Which implementation `_bash` drives, set per-test by `impl` below.
_IMPL = "bash"


@pytest.fixture(params=["bash", "python"], autouse=True)
def impl(request):
    """Run every assertion here against BOTH implementations.

    RDR-215 bead nexus-q02nx.9 ports this library to
    ``nexus.hooks.expectations``. A straight retarget would delete the only
    coverage of the bash library while it is STILL the live production path
    (bead .14 repoints consumers, not this one), so both are driven instead
    and each assertion becomes a differential. Drop the "bash" param in .14.
    """
    global _IMPL
    _IMPL = request.param
    yield request.param
    _IMPL = "bash"


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _bash(script: str, state: Path) -> subprocess.CompletedProcess:
    """Drive one ledger verb, through bash or the module.

    The script strings this file passes are bare verb names
    (``expectations_archive``, ``expectations_sweep``), so the python side
    dispatches on the name rather than parsing shell.
    """
    if _IMPL == "python":
        # Dispatch VERB BY VERB. An earlier version matched the whole script
        # against one verb name, so a composite two-verb script (the
        # archive-before-sweep ordering tests) matched nothing, raised,
        # was caught by a bare except, and fell through to the bash call
        # below — under BOTH params. Those tests ran bash twice under
        # different labels while reporting python coverage, which is the
        # vacuous-gate shape this file's own obligation guard exists to
        # prevent and could not see. Found by the bead .9 critique.
        verbs = [line.strip() for line in textwrap.dedent(script).splitlines() if line.strip()]
        supported = {
            "expectations_archive": expectations.expectations_archive,
            "expectations_sweep": expectations.expectations_sweep,
        }
        unsupported = [v for v in verbs if v not in supported]
        if unsupported:
            # LOUD, not a silent bash fallback: a test id that says
            # "python" must never quietly run bash instead.
            raise _UnsupportedInPython(
                f"{unsupported!r} is not a ported verb. Mark this test "
                "bash-only rather than letting it claim python coverage."
            )
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
    env = dict(os.environ, XDG_STATE_HOME=str(state), HOME=str(state / "home"))
    return subprocess.run(
        ["bash", "-c", f"source {PLUGIN_LIB}\n{textwrap.dedent(script)}"],
        capture_output=True, text=True, env=env,
    )


class _UnsupportedInPython(Exception):
    """This script is a shell construction, not a single ported verb."""


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


# The reference copy `tests/e2e/lib/expectations.sh` is DELETED (RDR-215
# bead nexus-q02nx.14); the ledger is `nexus.hooks.expectations`. The
# byte-identity class that lived here went with it — a parity assert with
# one side missing either errors or passes vacuously, and vacuous is
# worse. The PLUGIN copy `conexus/hooks/scripts/expectations.sh` still
# exists and is still what the wired hooks.json entries run; bead
# nexus-q02nx.21 deletes it in the same change that re-points those
# entries, which is also where this file's `impl` fixture loses its
# "bash" param.

