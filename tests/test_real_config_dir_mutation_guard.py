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
        assert changed == ["ADDED new_file.json"]

    def test_modified_file_reported(self) -> None:
        before = {"backfill_state.json": (100, 20)}
        after = {"backfill_state.json": (200, 25)}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == ["MODIFIED backfill_state.json"]

    def test_modified_file_same_size_different_mtime_reported(self) -> None:
        """The exact overwrite shape found in the wild (nexus-pfuns): a
        pre-existing file whose content changed but happens to land at
        the same byte size must still be caught via mtime alone."""
        before = {"lockstep.log": (100, 4684)}
        after = {"lockstep.log": (999, 4684)}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == ["MODIFIED lockstep.log"]

    def test_removed_file_reported(self) -> None:
        before = {"gone.json": (100, 10)}
        after: dict[str, tuple[int, int]] = {}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == ["REMOVED gone.json"]

    def test_unchanged_file_not_reported(self) -> None:
        before = {"stable.json": (100, 10)}
        after = {"stable.json": (100, 10)}
        assert _diff_config_dir_snapshots(before, after) == []

    def test_multiple_changes_all_reported_sorted(self) -> None:
        before = {"a.json": (1, 1), "b.json": (1, 1)}
        after = {"a.json": (2, 1), "c.json": (1, 1)}
        changed = _diff_config_dir_snapshots(before, after)
        assert changed == ["ADDED c.json", "MODIFIED a.json", "REMOVED b.json"]

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
        assert changed == ["MODIFIED backfill_state.json"]


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
            "index.log",
        }
        assert expected.issubset(set(_REAL_CONFIG_DIR_ALLOWLIST_PREFIXES))

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
        assert _is_allowlisted_config_dir_path("index.log") is True

    def test_is_allowlisted_rejects_unrelated_path(self) -> None:
        assert _is_allowlisted_config_dir_path("backfill_state.json") is False
        assert _is_allowlisted_config_dir_path("routing_log.jsonl") is False
        assert _is_allowlisted_config_dir_path("lockstep.log") is False
        assert _is_allowlisted_config_dir_path("logs/deferred_labeling.log") is False
        assert _is_allowlisted_config_dir_path("t1_addr.default") is False
