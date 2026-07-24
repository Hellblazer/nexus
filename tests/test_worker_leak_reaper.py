# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1odsl: the suite must not leave aspect-worker daemons running.

Observed 2026-07-24 (second sighting; the first was a leaked pytest postgres
recorded on nexus-g37fr): five ``nx daemon aspect-worker start`` processes were
still alive hours after their pytest runs finished, each pointing at a
``pytest-of-*`` tmpdir config dir.

WHY THE EXISTING TEARDOWN DOES NOT COVER IT: the tests call
``aspect_worker.stop_worker()``, which stops the IN-PROCESS singleton thread.
The leak is a different object — ``ensure_aspect_worker_daemon`` spawns a
DETACHED ``nx daemon aspect-worker start`` subprocess (Popen with
start_new_session), and nothing in the test path reaps that.

WHY IT IS NOT MERELY UNTIDY: each leaked daemon keeps polling. On this box the
leaked workers produced a 1,375-entry burst of 401s against the PRODUCTION
cloud endpoint (``/v1/aspects/queue/reclaim_stale``) on 2026-07-10 — test
daemons hammering prod with a stale token, which during a log review was
initially indistinguishable from a live product defect. They also make
``pgrep aspect-worker`` useless for answering "is the real worker running",
a question doctor and several runbooks ask.

THE SCOPING RULE THIS PINS: reaping is confined to processes whose
``--config-dir`` lies under the pytest tmp root. A broad "kill anything named
aspect-worker" would kill the developer's REAL worker — the exact
pattern-match-and-kill mistake the 2026-07-24 orphan-cleanup lesson warns
about. These tests exist mostly to hold that boundary.
"""
from __future__ import annotations

from pathlib import Path

from tests.conftest import reap_leaked_aspect_workers


class _FakeProc:
    def __init__(self, pid: int, cmdline: str) -> None:
        self.pid = pid
        self.cmdline = cmdline


def _lister(*procs: _FakeProc):
    return lambda: [(p.pid, p.cmdline) for p in procs]


class TestReaperScoping:
    def test_reaps_a_worker_under_the_pytest_tmp_root(self, tmp_path: Path) -> None:
        killed: list[int] = []
        leaked = _FakeProc(
            4242,
            f"python nx daemon aspect-worker start --config-dir {tmp_path}/x/.config/nexus --tenant default",
        )
        reaped = reap_leaked_aspect_workers(
            tmp_root=tmp_path, _list_procs=_lister(leaked), _kill=killed.append,
        )
        assert reaped == [4242]
        assert killed == [4242]

    def test_spares_a_worker_outside_the_tmp_root(self, tmp_path: Path) -> None:
        """The developer's real worker lives under ~/.config/nexus. Killing it
        because it shares a process name is the failure this scoping prevents."""
        killed: list[int] = []
        real = _FakeProc(
            99,
            "python nx daemon aspect-worker start --config-dir /Users/dev/.config/nexus --tenant default",
        )
        reaped = reap_leaked_aspect_workers(
            tmp_root=tmp_path, _list_procs=_lister(real), _kill=killed.append,
        )
        assert reaped == []
        assert killed == [], "must never signal a worker outside the pytest tmp root"

    def test_spares_unrelated_processes_that_mention_the_tmp_root(
        self, tmp_path: Path,
    ) -> None:
        """Matching on the tmp root ALONE is not enough — a pytest-spawned
        editor, shell, or postgres may carry the same path."""
        killed: list[int] = []
        other = _FakeProc(7, f"postgres -D {tmp_path}/pgdata")
        reaped = reap_leaked_aspect_workers(
            tmp_root=tmp_path, _list_procs=_lister(other), _kill=killed.append,
        )
        assert reaped == []
        assert killed == []

    def test_reaps_only_the_leaked_one_in_a_mixed_population(
        self, tmp_path: Path,
    ) -> None:
        killed: list[int] = []
        procs = _lister(
            _FakeProc(1, f"nx daemon aspect-worker start --config-dir {tmp_path}/a/.config/nexus"),
            _FakeProc(2, "nx daemon aspect-worker start --config-dir /Users/dev/.config/nexus"),
            _FakeProc(3, f"postgres -D {tmp_path}/pg"),
        )
        reaped = reap_leaked_aspect_workers(
            tmp_root=tmp_path, _list_procs=procs, _kill=killed.append,
        )
        assert reaped == [1]
        assert killed == [1]

    def test_matches_across_the_macos_var_symlink(self, tmp_path: Path) -> None:
        """/var/folders/... vs /private/var/folders/... must still match.

        macOS symlinks /var -> /private/var. pytest hands the tmp root over in
        the /var form while a spawned process carries the resolved /private/var
        form, so a plain string prefix compare matches NOTHING. Every other test
        here builds both sides from the same `tmp_path` and therefore cannot see
        it — this reaper passed all of them and still reaped zero real daemons
        on its first end-to-end run.
        """
        killed: list[int] = []
        unresolved = Path(str(tmp_path).replace("/private/var", "/var", 1))
        leaked = _FakeProc(
            8, f"nx daemon aspect-worker start --config-dir {tmp_path}/w/.config/nexus",
        )
        reaped = reap_leaked_aspect_workers(
            tmp_root=unresolved, _list_procs=_lister(leaked), _kill=killed.append,
        )
        assert reaped == [8], (
            "tmp root and process path must be compared RESOLVED — see the "
            "macOS /var -> /private/var symlink"
        )

    def test_a_kill_failure_never_raises(self, tmp_path: Path) -> None:
        """Reaping runs at session teardown. An exception there would turn a
        tidy-up into a suite error and mask the real result."""
        def _boom(_pid: int) -> None:
            raise ProcessLookupError("already gone")

        leaked = _FakeProc(5, f"nx daemon aspect-worker start --config-dir {tmp_path}/.config/nexus")
        reaped = reap_leaked_aspect_workers(
            tmp_root=tmp_path, _list_procs=_lister(leaked), _kill=_boom,
        )
        assert reaped == []          # not counted as reaped — it was already gone

    def test_lister_failure_never_raises(self, tmp_path: Path) -> None:
        def _boom():
            raise OSError("ps unavailable")

        assert reap_leaked_aspect_workers(
            tmp_root=tmp_path, _list_procs=_boom, _kill=lambda _p: None,
        ) == []
