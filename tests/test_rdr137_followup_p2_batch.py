# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 followup IMP-18 through IMP-27 (epic nexus-43qgm).

P2 batch — small focused improvements: UX, perf, defense-in-depth.
"""
from __future__ import annotations

import logging
import os
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
        from nexus.bounded_subprocess import run_bounded as real_run  # noqa: PLC0415 — test-local import, same idiom as this file's siblings

        def counting_run(*args, **kwargs):
            nonlocal call_count
            if args and isinstance(args[0], list) and args[0][:2] == ["git", "rev-parse"]:
                call_count += 1
            return real_run(*args, **kwargs)

        with patch("nexus.repo_identity.run_bounded", side_effect=counting_run):
            _resolve_main_repo(repo)
            _resolve_main_repo(repo)
            _resolve_main_repo(repo)

        assert call_count == 1, (
            f"IMP-24: expected lru_cache to dedupe — saw "
            f"{call_count} git rev-parse calls for 3 invocations."
        )


def test_resolve_main_repo_does_not_cache_a_failed_lookup(tmp_path: Path) -> None:
    """nexus-mdbds.9 review: a transient git failure must not name later lookups after the
    worktree for the life of the process."""
    from nexus.repo_identity import _resolve_main_repo, _resolve_main_repo_cached

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    main = tmp_path / "mainrepo"
    main.mkdir()
    for argv in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "i"],
                 ["worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side"]):
        subprocess.run(["git", *argv], cwd=main, env=env, check=True, capture_output=True, timeout=60)
    wt = tmp_path / "wt"
    _resolve_main_repo_cached.cache_clear()
    with patch("nexus.repo_identity.run_bounded", side_effect=subprocess.TimeoutExpired("git", 10)):
        assert _resolve_main_repo(wt) == wt  # the fallback, for this call only
    assert _resolve_main_repo(wt).resolve() == main.resolve()


def test_resolve_main_repo_caches_a_directory_outside_any_repo(tmp_path: Path) -> None:
    """IMP-24 holds for a non-repo directory too: doc_indexer probes each document's parent."""
    from nexus.bounded_subprocess import run_bounded as real_run  # noqa: PLC0415 — test-local import, same idiom as this file's siblings
    from nexus.repo_identity import _resolve_main_repo, _resolve_main_repo_cached

    plain = tmp_path / "plain"
    plain.mkdir()
    calls = []

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real_run(*args, **kwargs)

    _resolve_main_repo_cached.cache_clear()
    env = {"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    with patch.dict(os.environ, env), patch("nexus.repo_identity.run_bounded", side_effect=counting):
        assert _resolve_main_repo(plain) == plain
        assert _resolve_main_repo(plain) == plain
        assert _resolve_main_repo(plain) == plain
    assert len(calls) == 1
