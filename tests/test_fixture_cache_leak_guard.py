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
from unittest.mock import patch

from tests.conftest import (
    _FIXTURE_CACHE_PREFIXES,
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
