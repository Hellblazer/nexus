# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-nifd: tests for the fixture-cache leak guard hooks added
to ``tests/conftest.py``.

The guard runs at session start (snapshots leaked-file baseline) and
session finish (computes delta + fails if any new fixture-cache
file landed in the REAL ``~/.config/nexus/``). Direct testing of
``pytest_sessionfinish`` requires running pytest inside pytest;
instead we test the helper functions and the prefix list.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.conftest as conftest_mod
from tests.conftest import (
    _FIXTURE_CACHE_PREFIXES,
    _check_fixture_cache_leaks,
    _scan_fixture_cache_files,
)


class TestFixtureCachePrefixes:
    def test_prefix_list_covers_known_fixture_repos(self) -> None:
        """The prefix list must contain every fixture name the
        2026-05-08 leak audit identified. Adding a new fixture
        without updating the list is a soft regression: the new
        fixture's cache files would leak silently.
        """
        # Sourced from the bead's description (2026-05-08 audit
        # of 1,707 leaked files in ~/.config/nexus/).
        from_audit = {
            "nexus-rich0", "nexus-mini0", "code-repo", "prose-repo",
            "pdf-repo", "stage-b-repo", "sentinel-repo", "test-repo",
            "nx-shakeout-",
        }
        assert from_audit.issubset(set(_FIXTURE_CACHE_PREFIXES))


class TestScanFixtureCacheFiles:
    def test_returns_empty_when_real_dir_missing(self, tmp_path: Path) -> None:
        """When ``~/.config/nexus/`` doesn't exist (CI sandbox), the
        scan returns empty rather than raising — keeps the guard
        non-fatal in environments without an existing config dir.
        """
        with patch.object(Path, "home", return_value=tmp_path):
            assert _scan_fixture_cache_files() == set()

    def test_picks_up_files_with_fixture_prefix(self, tmp_path: Path) -> None:
        """A file matching one of ``_FIXTURE_CACHE_PREFIXES`` is
        flagged. A file with an unmatched prefix is not.
        """
        cfg = tmp_path / ".config" / "nexus"
        cfg.mkdir(parents=True)
        match = cfg / "code-repo-deadbeef.cache"
        match.write_text("payload")
        no_match = cfg / "real-user-project-cafef00d.cache"
        no_match.write_text("payload")
        not_cache = cfg / "code-repo-deadbeef.txt"
        not_cache.write_text("payload")

        with patch.object(Path, "home", return_value=tmp_path):
            found = _scan_fixture_cache_files()
        assert match in found
        assert no_match not in found
        assert not_cache not in found

    def test_picks_up_all_known_prefixes(self, tmp_path: Path) -> None:
        """Every prefix in the allow-list actually triggers the
        scan; reverting any prefix removal would re-allow that
        leakage class. Lock the contract by seeding one file per
        prefix.
        """
        cfg = tmp_path / ".config" / "nexus"
        cfg.mkdir(parents=True)
        for prefix in _FIXTURE_CACHE_PREFIXES:
            (cfg / f"{prefix}-abcd1234.cache").write_text("x")

        with patch.object(Path, "home", return_value=tmp_path):
            found = _scan_fixture_cache_files()
        # One per prefix.
        assert len(found) == len(_FIXTURE_CACHE_PREFIXES), (
            f"expected {len(_FIXTURE_CACHE_PREFIXES)} flagged files, "
            f"got {len(found)}: {sorted(p.name for p in found)}"
        )


class TestCheckFixtureCacheLeaksControllerOnlyEnforcement:
    """nexus-pfuns round 2: this guard used to run unconditionally on
    every xdist process. A worker's own ``session.exitstatus`` mutation
    is silently discarded by xdist (same masking class as
    ``_check_real_config_dir_mutations``), and WORSE, its own
    ``unlink()`` best-effort cleanup ERASED the leaked file before the
    controller (or any other worker) ever scanned -- a self-masking
    guard that detected real leaks and silently destroyed the evidence
    on every ``-n auto`` run. Fixed by gating the whole check behind
    ``_is_controller_or_serial`` (set once in ``pytest_sessionstart``).
    These tests simulate a mutation directly against the pure function
    (non-vacuity: the guard actually reports what it claims to)."""

    def test_noop_when_not_controller_or_serial(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The worker-side branch: a leaked file must be left ALONE
        (not unlinked) and the session must not be failed."""
        cfg = tmp_path / ".config" / "nexus"
        cfg.mkdir(parents=True)
        leaked = cfg / "code-repo-deadbeef.cache"
        leaked.write_text("payload")

        monkeypatch.setattr(conftest_mod, "_is_controller_or_serial", False)
        monkeypatch.setattr(conftest_mod, "_fixture_cache_baseline", set())
        fake_session = SimpleNamespace(exitstatus=0)

        with patch.object(Path, "home", return_value=tmp_path):
            _check_fixture_cache_leaks(fake_session)

        assert leaked.exists(), "worker must never delete evidence"
        assert fake_session.exitstatus == 0, "worker must never mutate exitstatus"

    def test_detects_and_cleans_up_when_controller_or_serial(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The controller/serial-side branch: a leaked file IS reported
        (session failed) and cleaned up (unlinked)."""
        cfg = tmp_path / ".config" / "nexus"
        cfg.mkdir(parents=True)
        leaked = cfg / "code-repo-deadbeef.cache"
        leaked.write_text("payload")

        monkeypatch.setattr(conftest_mod, "_is_controller_or_serial", True)
        monkeypatch.setattr(conftest_mod, "_fixture_cache_baseline", set())
        fake_session = SimpleNamespace(exitstatus=0)

        with patch.object(Path, "home", return_value=tmp_path):
            _check_fixture_cache_leaks(fake_session)

        assert not leaked.exists(), "controller must clean up the leak"
        assert fake_session.exitstatus == 1, "controller must fail the session"

    def test_no_leak_leaves_session_untouched(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        cfg = tmp_path / ".config" / "nexus"
        cfg.mkdir(parents=True)

        monkeypatch.setattr(conftest_mod, "_is_controller_or_serial", True)
        monkeypatch.setattr(conftest_mod, "_fixture_cache_baseline", set())
        fake_session = SimpleNamespace(exitstatus=0)

        with patch.object(Path, "home", return_value=tmp_path):
            _check_fixture_cache_leaks(fake_session)

        assert fake_session.exitstatus == 0
