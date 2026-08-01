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


# --- the postgres half (nexus-1odsl, 2026-07-25) -----------------------------
#
# The bead reported SIX workers and THREE postmasters. Only the worker half was
# fixed. postgres leaks for a DIFFERENT reason and so needs different coverage:
# tests/_engine_substrate.py does register an atexit teardown, but atexit does
# not run on SIGKILL, a double Ctrl-C, or a crash -- i.e. exactly how a long
# suite actually gets aborted. 21 stale pytest session dirs were present on the
# dev box when this landed.

from tests.conftest import reap_leaked_test_daemons, stale_pytest_roots  # noqa: E402


def _lister2(*procs: _FakeProc):
    return lambda: [(p.pid, p.cmdline) for p in procs]


class TestPostgresLeakClass:
    def test_reaps_a_postmaster_rooted_in_the_tmp_root(self, tmp_path: Path) -> None:
        killed: list[tuple[int, str]] = []
        pg = _FakeProc(31, f"/opt/pg/bin/postgres -D {tmp_path}/pgdata -p 5433")
        reaped = reap_leaked_test_daemons(
            tmp_root=tmp_path, _list_procs=_lister2(pg),
            _kill=lambda pid, sig: killed.append((pid, sig)),
        )
        assert reaped == [("postgres", 31)]

    def test_postgres_gets_SIGQUIT_not_SIGTERM(self, tmp_path: Path) -> None:
        """SIGTERM is postgres's SMART shutdown: it waits for clients to
        disconnect and can hang forever on a stranded cluster. These clusters
        are throwaway (fsync=off), so immediate shutdown is correct and a
        graceful one buys nothing while risking a hung teardown."""
        killed: list[tuple[int, str]] = []
        procs = _lister2(
            _FakeProc(31, f"/opt/pg/bin/postgres -D {tmp_path}/pgdata"),
            _FakeProc(32, f"nx daemon aspect-worker start --config-dir {tmp_path}/.config/nexus"),
        )
        reap_leaked_test_daemons(
            tmp_root=tmp_path, _list_procs=procs,
            _kill=lambda pid, sig: killed.append((pid, sig)),
        )
        assert (31, "QUIT") in killed, f"postgres must get SIGQUIT: {killed}"
        assert (32, "TERM") in killed, f"the worker must still get SIGTERM: {killed}"

    def test_spares_the_developers_real_postgres(self, tmp_path: Path) -> None:
        """The whole reason the original reaper refused to match postgres."""
        killed: list[tuple[int, str]] = []
        real = _FakeProc(40, "/usr/local/pgsql/bin/postgres -D /Users/dev/pgdata")
        reaped = reap_leaked_test_daemons(
            tmp_root=tmp_path, _list_procs=_lister2(real),
            _kill=lambda pid, sig: killed.append((pid, sig)),
        )
        assert reaped == []
        assert killed == [], "never signal a database outside the pytest tmp root"

    def test_spares_pg_ctl_which_also_carries_dash_D(self, tmp_path: Path) -> None:
        """pg_ctl is a transient CLI that takes the SAME -D flag. Matching on
        the flag alone would sweep it up, which is why the class test keys on
        the executable's basename."""
        killed: list[tuple[int, str]] = []
        ctl = _FakeProc(41, f"/opt/pg/bin/pg_ctl -D {tmp_path}/pgdata status")
        reaped = reap_leaked_test_daemons(
            tmp_root=tmp_path, _list_procs=_lister2(ctl),
            _kill=lambda pid, sig: killed.append((pid, sig)),
        )
        assert reaped == [] and killed == []

    def test_spares_postgres_worker_subprocesses(self, tmp_path: Path) -> None:
        """postgres's own children appear as `postgres: checkpointer`. Killing
        the postmaster reaps them, so signalling each individually is both
        redundant and a way to half-kill a cluster."""
        killed: list[tuple[int, str]] = []
        child = _FakeProc(42, f"postgres: checkpointer -D {tmp_path}/pgdata")
        reaped = reap_leaked_test_daemons(
            tmp_root=tmp_path, _list_procs=_lister2(child),
            _kill=lambda pid, sig: killed.append((pid, sig)),
        )
        assert reaped == [] and killed == []


class TestStaleRootDiscovery:
    """The START pass. A session-END reaper cannot fire when the run is KILLED,
    which is the case that leaks -- so stranded daemons are only ever reachable
    from a LATER session."""

    def test_finds_sibling_sessions_and_excludes_the_current_one(
        self, tmp_path: Path,
    ) -> None:
        root = tmp_path / "pytest-of-dev"
        root.mkdir()
        current = root / "pytest-9"
        for name in ("pytest-7", "pytest-8", "pytest-9"):
            (root / name).mkdir()

        found = {p.name for p in stale_pytest_roots(current)}
        assert found == {"pytest-7", "pytest-8"}
        assert "pytest-9" not in found, (
            "reaping the CURRENT session's own root would kill the run in progress"
        )

    def test_returns_nothing_when_the_parent_is_not_a_pytest_root(
        self, tmp_path: Path,
    ) -> None:
        """Guards against pointing the reaper at an arbitrary directory whose
        children merely look session-shaped."""
        odd = tmp_path / "somewhere" / "pytest-1"
        odd.mkdir(parents=True)
        assert stale_pytest_roots(odd) == []

    def test_survives_a_missing_root(self, tmp_path: Path) -> None:
        assert stale_pytest_roots(tmp_path / "nope" / "pytest-1") == []


class TestPathsWithSpaces:
    """`ps -eo command=` returns argv joined by spaces, UNQUOTED.

    Found by review (2026-07-25), not by the tests above: every one of them
    builds paths from pytest's `tmp_path`, which never contains a space, so the
    naive `split()[0]` looked correct forever. On a box whose temp root does
    contain one, the path truncates, containment fails, and a real leaked
    daemon is left running with NO error -- a silent false negative, which is
    the worst shape for a reaper to fail in.
    """

    def test_a_data_dir_containing_a_space_is_still_matched(self, tmp_path) -> None:
        root = tmp_path / "sp ace"
        root.mkdir()
        killed: list[tuple[int, str]] = []
        pg = _FakeProc(70, f"/opt/pg/bin/postgres -D {root}/pgdata -p 5433")
        reaped = reap_leaked_test_daemons(
            tmp_root=root, _list_procs=_lister2(pg),
            _kill=lambda pid, sig: killed.append((pid, sig)),
        )
        assert reaped == [("postgres", 70)], (
            "a data dir with a space in it was not matched -- the leak would be "
            "left running silently"
        )

    def test_a_config_dir_containing_a_space_is_still_matched(self, tmp_path) -> None:
        root = tmp_path / "sp ace"
        root.mkdir()
        killed: list[tuple[int, str]] = []
        w = _FakeProc(
            71,
            f"nx daemon aspect-worker start --config-dir {root}/x/.config/nexus --tenant default",
        )
        reaped = reap_leaked_test_daemons(
            tmp_root=root, _list_procs=_lister2(w),
            _kill=lambda pid, sig: killed.append((pid, sig)),
        )
        assert reaped == [("aspect-worker", 71)]

    def test_the_safety_property_survives_the_space_handling(self, tmp_path) -> None:
        """Widening the match must not widen it past the root.

        The join-more-tokens loop is the risky part of this fix: it must stop
        rather than keep swallowing tokens until something matches.
        """
        root = tmp_path / "sp ace"
        root.mkdir()
        killed: list[tuple[int, str]] = []
        real = _FakeProc(72, "/usr/local/pgsql/bin/postgres -D /Users/dev/pgdata")
        reaped = reap_leaked_test_daemons(
            tmp_root=root, _list_procs=_lister2(real),
            _kill=lambda pid, sig: killed.append((pid, sig)),
        )
        assert reaped == [] and killed == []

    def test_a_relative_path_is_refused(self, tmp_path) -> None:
        """A relative value would resolve against the REAPER's cwd, not the
        target process's cwd at spawn time -- a different directory entirely."""
        killed: list[tuple[int, str]] = []
        rel = _FakeProc(73, "postgres -D relative/pgdata")
        reaped = reap_leaked_test_daemons(
            tmp_root=tmp_path, _list_procs=_lister2(rel),
            _kill=lambda pid, sig: killed.append((pid, sig)),
        )
        assert reaped == [] and killed == []
