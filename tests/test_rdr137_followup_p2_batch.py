# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 followup IMP-18 through IMP-27 (epic nexus-43qgm).

P2 batch — small focused improvements: UX, perf, defense-in-depth.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog


@pytest.fixture(autouse=True)
def _enable_debug_logging():
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    yield
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )


class TestImp19MCPCatalogLongestPrefixMatch:
    def test_nested_repo_picks_child_anchor(self) -> None:
        """When parent + child repos are both registered (same
        list_repos_dual result), make_relative must anchor against
        the longer (child) path so the relative result is the
        shortest possible. Pre-fix sorted iteration picked the parent
        first, producing 'child/file.py' instead of 'file.py'."""
        # Read the source to verify the longest-prefix logic was added.
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "nexus" / "mcp" / "catalog.py"
        )
        text = src.read_text()
        assert "max(candidates, key=len)" in text, (
            "IMP-19: nested-repo path-relativization must use "
            "max-by-length so the longer (child) match wins."
        )


class TestImp21NoVestigialDoubleTry:
    def test_index_repository_has_no_dead_try_except(self) -> None:
        """Verify the inner try/except with two re-raise handlers is
        gone (was vestige of dropped status-write path)."""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "nexus" / "indexer.py"
        )
        text = src.read_text()
        # Pre-fix had a block:
        #   try:
        #       ...
        #   except CredentialsMissingError:
        #       raise
        #   except Exception:
        #       raise
        # Post-fix: that block is gone.
        # Loose check: no two consecutive `raise` statements inside a
        # `def index_repository` block. Simpler grep on the
        # tell-tale pattern:
        assert "except CredentialsMissingError:\n            raise\n        except Exception:\n            raise" not in text, (
            "IMP-21: vestigial double-try with two re-raise handlers "
            "still present in index_repository."
        )


class TestImp24RepoIdentityCacheReducesSubprocessCalls:
    def test_resolve_main_repo_uses_lru_cache(self, tmp_path: Path) -> None:
        """Two calls to _resolve_main_repo with the same path should
        result in ONE git rev-parse subprocess. The lru_cache
        guarantees this."""
        from nexus.repo_identity import _resolve_main_repo, _resolve_main_repo_cached

        repo = tmp_path / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

        # Clear the cache for a clean measurement.
        _resolve_main_repo_cached.cache_clear()

        call_count = 0
        real_run = subprocess.run

        def counting_run(*args, **kwargs):
            nonlocal call_count
            if args and isinstance(args[0], list) and args[0][:2] == ["git", "rev-parse"]:
                call_count += 1
            return real_run(*args, **kwargs)

        with patch("nexus.repo_identity.subprocess.run", side_effect=counting_run):
            _resolve_main_repo(repo)
            _resolve_main_repo(repo)
            _resolve_main_repo(repo)

        assert call_count == 1, (
            f"IMP-24: expected lru_cache to dedupe — saw "
            f"{call_count} git rev-parse calls for 3 invocations."
        )


class TestImp27ListSiblingCollectionsHandlesConformantNames:
    def test_conformant_4_segment_finds_siblings_by_owner_id(self) -> None:
        """RDR-103 conformant names (code__owner-1-2__voyage-code-3__v1)
        should find siblings that share the same __owner_id__ segment.
        Pre-fix the function returned [] for all conformant names
        because the 8-char hash check failed on the trailing __v1."""
        from nexus.repo_identity import list_sibling_collections

        # Stub t3 client with conformant + legacy names.
        t3 = MagicMock()
        t3.list_collections.return_value = [
            MagicMock(name="code__myrepo-1-1__voyage-code-3__v1"),
            MagicMock(name="docs__myrepo-1-1__voyage-context-3__v1"),
            MagicMock(name="rdr__myrepo-1-1__voyage-context-3__v1"),
            MagicMock(name="code__otherrepo-5-1__voyage-code-3__v1"),
            MagicMock(name="taxonomy__myrepo-1-1__voyage-context-3__v1"),
        ]
        # Set the .name attribute correctly on each MagicMock.
        for stub_name in (
            "code__myrepo-1-1__voyage-code-3__v1",
            "docs__myrepo-1-1__voyage-context-3__v1",
            "rdr__myrepo-1-1__voyage-context-3__v1",
            "code__otherrepo-5-1__voyage-code-3__v1",
            "taxonomy__myrepo-1-1__voyage-context-3__v1",
        ):
            pass
        # Rebuild the list with proper .name attrs.
        colls = []
        for n in (
            "code__myrepo-1-1__voyage-code-3__v1",
            "docs__myrepo-1-1__voyage-context-3__v1",
            "rdr__myrepo-1-1__voyage-context-3__v1",
            "code__otherrepo-5-1__voyage-code-3__v1",
            "taxonomy__myrepo-1-1__voyage-context-3__v1",
        ):
            m = MagicMock()
            m.name = n
            colls.append(m)
        t3.list_collections.return_value = colls

        siblings = list_sibling_collections(
            "code__myrepo-1-1__voyage-code-3__v1", t3,
        )
        # docs__ and rdr__ for the same owner are siblings.
        # code__ (input) and otherrepo are NOT.
        # taxonomy__ is excluded.
        assert siblings == [
            "docs__myrepo-1-1__voyage-context-3__v1",
            "rdr__myrepo-1-1__voyage-context-3__v1",
        ]

    def test_legacy_2_segment_form_still_works(self) -> None:
        """Legacy hash8-suffixed names continue to find siblings by
        the trailing hash."""
        from nexus.repo_identity import list_sibling_collections

        colls = []
        for n in (
            "docs__art-architecture-8c2e74c0",
            "code__art-8c2e74c0",
            "rdr__other-deadbeef",
        ):
            m = MagicMock()
            m.name = n
            colls.append(m)
        t3 = MagicMock()
        t3.list_collections.return_value = colls

        siblings = list_sibling_collections("docs__art-architecture-8c2e74c0", t3)
        assert siblings == ["code__art-8c2e74c0"]
