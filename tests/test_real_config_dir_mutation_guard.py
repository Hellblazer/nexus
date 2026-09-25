# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-pfuns: tests for the real-config-dir mutation guard hooks added
to ``tests/conftest.py`` (``_check_real_config_dir_mutations`` and its
helpers).

The guard runs at session start (snapshots the REAL ``~/.config/nexus/``)
and session finish (diffs against the baseline; fails on any add/remove/
modify not on the allowlist). Direct testing of ``pytest_sessionfinish``
requires running pytest inside pytest; instead this tests the pure helper
functions directly, matching ``tests/test_fixture_cache_leak_guard.py``'s
approach for the sibling nexus-nifd guard.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tests.conftest import (
    _REAL_CONFIG_DIR_ALLOWLIST_PREFIXES,
    _diff_config_dir_snapshots,
    _is_allowlisted_config_dir_path,
    _snapshot_real_config_dir,
)


class TestSnapshotRealConfigDir:
    def test_returns_empty_when_real_dir_missing(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            assert _snapshot_real_config_dir() == {}

    def test_captures_relative_path_mtime_and_size(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".config" / "nexus"
        (cfg / "sub").mkdir(parents=True)
        f = cfg / "sub" / "state.json"
        f.write_text("hello")

        with patch.object(Path, "home", return_value=tmp_path):
            snap = _snapshot_real_config_dir()

        assert "sub/state.json" in snap
        mtime_ns, size = snap["sub/state.json"]
        st = f.stat()
        assert mtime_ns == st.st_mtime_ns
        assert size == st.st_size == 5

    def test_ignores_directories(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".config" / "nexus"
        (cfg / "empty_dir").mkdir(parents=True)

        with patch.object(Path, "home", return_value=tmp_path):
            snap = _snapshot_real_config_dir()

        assert snap == {}


class TestDiffConfigDirSnapshots:
    """Non-vacuity pin: simulates a mutation (added / modified / removed)
    against a pure in-memory snapshot pair and asserts the guard's diff
    logic actually reports each one -- no real filesystem or pytest
    sub-session required."""

    def test_added_file_reported(self) -> None:
        before: dict[str, tuple[int, int]] = {}
        after = {"new_file.json": (100, 10)}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == [("ADDED", "new_file.json")]

    def test_modified_file_reported(self) -> None:
        before = {"backfill_state.json": (100, 20)}
        after = {"backfill_state.json": (200, 25)}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == [("MODIFIED", "backfill_state.json")]

    def test_modified_file_same_size_different_mtime_reported(self) -> None:
        """The exact overwrite shape found in the wild (nexus-pfuns): a
        pre-existing file whose content changed but happens to land at
        the same byte size must still be caught via mtime alone."""
        before = {"lockstep.log": (100, 4684)}
        after = {"lockstep.log": (999, 4684)}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == [("MODIFIED", "lockstep.log")]

    def test_removed_file_reported(self) -> None:
        before = {"gone.json": (100, 10)}
        after: dict[str, tuple[int, int]] = {}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == [("REMOVED", "gone.json")]

    def test_unchanged_file_not_reported(self) -> None:
        before = {"stable.json": (100, 10)}
        after = {"stable.json": (100, 10)}
        assert _diff_config_dir_snapshots(before, after) == []

    def test_multiple_changes_all_reported_sorted(self) -> None:
        before = {"a.json": (1, 1), "b.json": (1, 1)}
        after = {"a.json": (2, 1), "c.json": (1, 1)}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == [
            ("ADDED", "c.json"), ("MODIFIED", "a.json"), ("REMOVED", "b.json"),
        ]

    def test_allowlisted_addition_not_reported(self) -> None:
        before: dict[str, tuple[int, int]] = {}
        after = {"aspect_worker_addr.default": (100, 245)}
        assert _diff_config_dir_snapshots(before, after) == []

    def test_allowlisted_modification_not_reported(self) -> None:
        before = {"current_session": (1, 36)}
        after = {"current_session": (2, 36)}
        assert _diff_config_dir_snapshots(before, after) == []

    def test_non_allowlisted_change_alongside_allowlisted_one(self) -> None:
        """An allowlisted path changing must never mask a real,
        non-allowlisted leak reported in the same diff."""
        before = {"aspect_worker_addr.default": (1, 245), "backfill_state.json": (1, 10)}
        after = {"aspect_worker_addr.default": (2, 245), "backfill_state.json": (2, 15)}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == [("MODIFIED", "backfill_state.json")]

    def test_index_log_truncation_is_reachable_and_reported(self) -> None:
        """nexus-wjkc7 regression pin: ``index.log`` used to sit in BOTH
        ``_REAL_CONFIG_DIR_ALLOWLIST_PREFIXES`` and
        ``_APPEND_ONLY_REAL_CONFIG_LOGS``. The allowlist match runs first,
        inside this function, so a truncation/rewrite of ``index.log``
        never even reached the diff -- making the append-only set's
        stricter (still-catches-a-truncation) rule for it unreachable.
        Now that ``index.log`` is allowlist-free, a shrink must show up
        here as a real diff entry."""
        before = {"index.log": (1, 500)}
        after = {"index.log": (2, 10)}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == [("MODIFIED", "index.log")], (
            "a non-append write to index.log must reach the diff, not be "
            "silently absorbed by the allowlist"
        )


class TestAllowlist:
    def test_known_ambient_writers_present(self) -> None:
        """Every writer measured/reasoned during nexus-pfuns must stay on
        the allowlist -- a silent removal would re-trip the guard on
        ambient, non-test-induced activity."""
        expected = {
            "aspect_worker_addr.",
            "logs/aspect_worker_daemon",
            "logs/mcp.log",
            "current_session",
            "t1_session_lease.",
            "context/",
            "mineru.pid",
        }
        assert expected.issubset(set(_REAL_CONFIG_DIR_ALLOWLIST_PREFIXES))

    def test_index_log_is_not_on_the_allowlist(self) -> None:
        """nexus-wjkc7: ``index.log`` is governed solely by
        ``_APPEND_ONLY_REAL_CONFIG_LOGS`` now -- it must never come back to
        this allowlist, which would make that stricter rule unreachable
        again (the allowlist's prefix match runs first, inside
        ``_diff_config_dir_snapshots``)."""
        assert "index.log" not in _REAL_CONFIG_DIR_ALLOWLIST_PREFIXES

    def test_dead_t1_addr_carveout_removed(self) -> None:
        """nexus-pfuns round 2 (coordinator directive): `t1_addr.*` was the
        retired RDR-149 P4 Chroma-lease format with zero live writers
        (`grep -rn "t1_addr\\." src/` returns only comments/docstrings
        noting readers were ported OFF it). A dead carve-out is a vacuous
        allowlist entry -- it would mask a real leak of that exact shape.
        Must never be re-added without a demonstrated live writer."""
        assert "t1_addr." not in _REAL_CONFIG_DIR_ALLOWLIST_PREFIXES

    def test_is_allowlisted_matches_prefix(self) -> None:
        assert _is_allowlisted_config_dir_path("t1_session_lease.abc123") is True
        assert _is_allowlisted_config_dir_path("current_session") is True
        assert _is_allowlisted_config_dir_path("logs/mcp.log") is True
        assert _is_allowlisted_config_dir_path("logs/mcp.log.1") is True

    def test_context_l1_cache_dir_is_allowlisted(self) -> None:
        """nexus-pfuns follow-up (2026-09-25): ``context/<repo>-<hash>.txt``
        is the RDR-072 per-repo Knowledge Map cache
        (``nexus.context.generate_context_l1`` /
        ``CONTEXT_L1_DIR = _ctx_nexus_config_dir() / "context"``), written by
        any live ``nx index repo`` background dispatch (the post-commit
        hook's ``--on-locked=skip`` run -- the same mechanism already
        covered by ``logs/index-`` and ``locks/`` above; this is a THIRD
        artifact of that one mechanism), by ``nx taxonomy`` rebuilds, and by
        ``nx context refresh``, all run by a live Claude Code session on
        this box independent of pytest. MEASURED 2026-09-25: a full
        ``pytest -n auto`` run (0 failures) exited 1 over exactly
        ``MODIFIED context/tmp-d0f036b9.txt``, coinciding with a real
        session's SessionStart hook picking up a freshly regenerated
        Knowledge Map built from the live cloud store -- no test in this
        repo produces that content (every direct ``generate_context_l1``/
        ``refresh_context_l1`` call in tests/test_context.py passes an
        explicit ``output_path=tmp_path/...``, bypassing this directory
        entirely, and every ``nx`` subprocess invocation in
        tests/test_mineru_cmd.py-adjacent suites and elsewhere isolates
        ``NEXUS_CONFIG_DIR`` -- see ``_isolate_config_dir``). The writer
        itself (``nexus.context._ctx_nexus_config_dir``) honours
        ``NEXUS_CONFIG_DIR`` too, so a test that bypasses isolation would
        still have to reach the real path through the same escape class
        the guard's own docstring already anticipates.
        """
        assert _is_allowlisted_config_dir_path("context/tmp-d0f036b9.txt") is True
        assert _is_allowlisted_config_dir_path("context/nexus-571b8edd.txt") is True
        # legacy global fallback file (CONTEXT_L1_PATH) is NOT under this
        # prefix and stays reported -- narrower than a blanket "context*"
        # match would be.
        assert _is_allowlisted_config_dir_path("context_l1.txt") is False

    def test_mineru_pid_is_allowlisted(self) -> None:
        """``mineru.pid`` (``nexus._mineru_pid._pid_file_path``) is the live
        MinerU server's own PID/port/started_at registration file,
        rewritten whenever the server (re)starts -- including a restart
        triggered by an operator's ``nx`` reinstall on this box, independent
        of any test. Same class as the already-allowlisted
        ``aspect_worker_addr.`` (a live daemon's own state file): every
        test that touches this path isolates ``NEXUS_CONFIG_DIR`` first
        (tests/test_mineru_cmd.py, tests/test_mineru_config_drift.py,
        tests/test_mineru_spawn_logging.py, tests/daemon/
        test_mineru_lifecycle.py -- all `monkeypatch.setenv`, no delenv),
        and the writer itself resolves through `nexus_config_dir()`, so it
        honours the same override. MEASURED 2026-09-25: the same day's
        earlier run tripped on `mineru.pid` alongside `last_seen_version`
        during a peer's operator-driven `nx` reinstall.
        """
        assert _is_allowlisted_config_dir_path("mineru.pid") is True
        # nested is still reported -- only the config-dir-root file is ambient.
        assert _is_allowlisted_config_dir_path("sub/mineru.pid") is False

    def test_service_registry_election_flocks_are_allowlisted(self) -> None:
        """ServiceRegistry per-scope election flocks churn independently of
        pytest and must not trip the guard.

        `service_registry.py:202` builds them as
        `{tier}_elect.{scope_key}.lock` at the config ROOT, and
        `sweep_dead_t1_elect_locks` (health.py:1641) reaps dead ones, so
        they both APPEAR and VANISH while any live daemon or MCP server is
        running. Observed 2026-08-23: a full local-service-gate run failed
        with `REMOVED aspect_worker_elect.default.lock` as its ONLY finding
        (592 passed, 0 failed) while 14 nx-mcp processes served this box --
        the file was back, mtime three minutes later, before the run
        finished.

        Not a per-tier prefix, because that is whack-a-mole: five such locks
        across four tiers existed on the box that day (aspect_worker,
        mineru, t1 x2, t2). Root level only, and the `_elect.` +
        `.lock` shape together -- a bare `.lock` allowance would mask real
        leaks.
        """
        for tier in ("aspect_worker", "mineru", "t1", "t2", "storage_service"):
            rel = f"{tier}_elect.default.lock"
            assert _is_allowlisted_config_dir_path(rel) is True, rel
        assert _is_allowlisted_config_dir_path(
            "t1_elect.24c01b46-040f-41f3-8d7c-add0293a0ee5.lock"
        ) is True

    def test_election_flock_allowance_stays_narrow(self) -> None:
        """The carve-out must not become a blanket .lock allowance."""
        assert _is_allowlisted_config_dir_path("something.lock") is False
        assert _is_allowlisted_config_dir_path("data_token_mint_lock.abc") is False
        # nested is still reported -- only the root-level registry shape is ambient
        assert _is_allowlisted_config_dir_path("sub/t1_elect.default.lock") is False
        # `_elect.` without the .lock suffix is not the flock
        assert _is_allowlisted_config_dir_path("t1_elect.default.json") is False

    def test_is_allowlisted_rejects_unrelated_path(self) -> None:
        assert _is_allowlisted_config_dir_path("backfill_state.json") is False
        assert _is_allowlisted_config_dir_path("routing_log.jsonl") is False
        assert _is_allowlisted_config_dir_path("lockstep.log") is False
        assert _is_allowlisted_config_dir_path("logs/deferred_labeling.log") is False
        assert _is_allowlisted_config_dir_path("t1_addr.default") is False
        # nexus-wjkc7: index.log is governed by _APPEND_ONLY_REAL_CONFIG_LOGS
        # only -- the allowlist must reject it so a truncation reaches the
        # diff instead of being silently exempted.
        assert _is_allowlisted_config_dir_path("index.log") is False
