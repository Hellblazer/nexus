# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-103 Phase 4 invariant: ``_repo_identity`` is stable.

The conformant collection-name migration uses repo identity to look up
the catalog owner (``Catalog.owner_for_repo(repo_hash)``) and the
legacy registry helpers use the same identity to construct legacy
collection names. If the two identities diverge for any reason
(symlinks, worktrees, environment variations), the migration would
look up an owner that does not exist OR rename a collection that
does not match the legacy name in T3.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.registry import _repo_identity


def _init_git(repo: Path) -> None:
    """Initialise a minimal git repo so ``rev-parse --git-common-dir``
    succeeds. The migration's repo-identity stability assumption rests
    on ``rev-parse``'s behaviour, so the tests must run against real
    git, not a path-only fallback.
    """
    import subprocess

    subprocess.run(
        ["git", "init", "--quiet"], cwd=repo, check=True,
        capture_output=True,
    )


def test_repo_identity_deterministic(tmp_path: Path) -> None:
    """Two ``_repo_identity`` calls against the same path return the
    same ``(name, hash)``. Path-derived; no clock or randomness."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    _init_git(repo)
    a = _repo_identity(repo)
    b = _repo_identity(repo)
    assert a == b


def test_repo_identity_path_hash_is_8_hex(tmp_path: Path) -> None:
    """Path hash slice is exactly 8 hex characters, lowercase."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    _init_git(repo)
    _, h = _repo_identity(repo)
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


def test_repo_identity_different_paths_differ(tmp_path: Path) -> None:
    repo_a = tmp_path / "alpha"
    repo_a.mkdir()
    _init_git(repo_a)
    repo_b = tmp_path / "beta"
    repo_b.mkdir()
    _init_git(repo_b)
    assert _repo_identity(repo_a) != _repo_identity(repo_b)


def test_repo_identity_worktree_resolves_to_main_repo(tmp_path: Path) -> None:
    """``_repo_identity`` uses ``rev-parse --git-common-dir`` so a
    worktree resolves to the main repo's identity. Two calls, one
    from the main repo and one from the worktree, must return the
    same ``(name, hash)``.
    """
    import subprocess

    main = tmp_path / "main"
    main.mkdir()
    _init_git(main)
    # Make at least one commit so ``git worktree add`` succeeds.
    (main / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "seed.txt"], cwd=main, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "seed", "--quiet"],
        cwd=main, check=True, capture_output=True,
    )
    worktree = tmp_path / "main-feature"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", str(worktree), "-b", "feature"],
        cwd=main, check=True, capture_output=True,
    )
    main_id = _repo_identity(main)
    worktree_id = _repo_identity(worktree)
    assert main_id == worktree_id


def test_repo_identity_falls_back_to_path_when_not_a_git_repo(
    tmp_path: Path,
) -> None:
    """Non-git directory returns identity derived from the path
    itself. The hash is still 8 hex chars; the basename is the
    directory name. Migration must NOT crash on non-git invocations
    (e.g. ad-hoc test fixtures).
    """
    repo = tmp_path / "ungitted"
    repo.mkdir()
    name, h = _repo_identity(repo)
    assert name == "ungitted"
    assert len(h) == 8


# ── nexus-u8n4r: worktree/tempdir predicate + registration guard ─────────


class TestIsWorktreeOrTempdirPath:
    """The public export lives here now; the exhaustive platform-shape
    pins stay in ``tests/test_catalog_reconcile_stale.py`` (the original
    home) via the module-level alias re-exported from there. This is a
    minimal direct-import smoke test for the new ``nexus.repo_identity``
    surface."""

    def test_worktree_marker_matches(self):
        from nexus.repo_identity import is_worktree_or_tempdir_path

        assert is_worktree_or_tempdir_path(
            "/Users/hal/.claude/worktrees/wt1/src/gone.py"
        )

    def test_clean_path_does_not_match(self):
        from nexus.repo_identity import is_worktree_or_tempdir_path

        assert not is_worktree_or_tempdir_path("/Users/hal/git/nexus/src/real.py")


class TestShouldSkipEphemeralRegistration:
    """nexus-u8n4r guard semantics: skip only when the REGISTERED path
    matches the worktree/tempdir predicate AND the owner's own repo_root
    does not (the exception that keeps throwaway worktree-rooted owners
    and the pytest tmp-dir suite registrable)."""

    def test_skips_when_registered_path_is_ephemeral_and_owner_root_is_clean(self):
        from nexus.repo_identity import should_skip_ephemeral_registration

        assert should_skip_ephemeral_registration(
            "/Users/hal/git/nexus/.claude/worktrees/agent-x/docs/foo.md",
            "/Users/hal/git/nexus",
        )

    def test_does_not_skip_when_owner_root_is_itself_ephemeral(self):
        """Population (a): a throwaway owner explicitly rooted in a
        worktree/tempdir — e.g. a gate sandbox with its own config dir."""
        from nexus.repo_identity import should_skip_ephemeral_registration

        assert not should_skip_ephemeral_registration(
            "/tmp/sandbox-repo/src/foo.py", "/tmp/sandbox-repo",
        )

    def test_does_not_skip_when_registered_path_is_clean(self):
        from nexus.repo_identity import should_skip_ephemeral_registration

        assert not should_skip_ephemeral_registration(
            "/Users/hal/git/nexus/src/foo.py", "/Users/hal/git/nexus",
        )

    def test_does_not_skip_when_owner_root_is_empty(self):
        """Known residual: curator owners normally carry an empty
        repo_root, so the guard never fires for them."""
        from nexus.repo_identity import should_skip_ephemeral_registration

        assert not should_skip_ephemeral_registration(
            "/Users/hal/.claude/worktrees/agent-x/docs/foo.md", "",
        )
        assert not should_skip_ephemeral_registration(
            "/Users/hal/.claude/worktrees/agent-x/docs/foo.md", None,
        )

    def test_does_not_skip_when_registered_path_is_empty(self):
        from nexus.repo_identity import should_skip_ephemeral_registration

        assert not should_skip_ephemeral_registration("", "/Users/hal/git/nexus")


class TestOwnerRepoRootBestEffort:
    """nexus-u8n4r review fix M1 (code-review-expert): a reader that
    RAISES must be distinguishable, via a structlog WARNING, from the
    by-design "curator owner genuinely has no repo_root" case (which
    resolves through cleanly and logs nothing)."""

    def test_missing_method_returns_empty_silently(self):
        from nexus.repo_identity import owner_repo_root_best_effort

        class _NoMethodReader:
            pass

        import structlog.testing
        with structlog.testing.capture_logs() as logs:
            result = owner_repo_root_best_effort(_NoMethodReader(), "1.1")
        assert result == ""
        assert not any(
            log_entry.get("event") == "owner_repo_root_lookup_failed_guard_inert"
            for log_entry in logs
        )

    def test_clean_empty_result_returns_empty_silently(self):
        """By-design residual: the reader answers cleanly, the owner
        just has no repo_root. No warning — this is not a failure."""
        from nexus.repo_identity import owner_repo_root_best_effort

        class _CleanReader:
            def get_owner_by_prefix(self, prefix):
                return {"tumbler_prefix": prefix, "repo_root": ""}

        import structlog.testing
        with structlog.testing.capture_logs() as logs:
            result = owner_repo_root_best_effort(_CleanReader(), "1.1")
        assert result == ""
        assert not any(
            log_entry.get("event") == "owner_repo_root_lookup_failed_guard_inert"
            for log_entry in logs
        )

    def test_raising_reader_returns_empty_and_warns(self):
        from nexus.repo_identity import owner_repo_root_best_effort

        class _BrokenReader:
            def get_owner_by_prefix(self, prefix):
                raise RuntimeError("boom")

        import structlog.testing
        with structlog.testing.capture_logs() as logs:
            result = owner_repo_root_best_effort(_BrokenReader(), "1.1")
        assert result == ""
        warned = [
            log_entry for log_entry in logs
            if log_entry.get("event") == "owner_repo_root_lookup_failed_guard_inert"
        ]
        assert len(warned) == 1
        assert warned[0]["owner"] == "1.1"


class TestReconstructAbsoluteRegisteredPath:
    """nexus-u8n4r review fix C1: undo a caller-side relativization step
    before testing the ephemeral-path predicate."""

    def test_absolute_original_wins_regardless_of_relativized_fp(self):
        from nexus.repo_identity import reconstruct_absolute_registered_path

        result = reconstruct_absolute_registered_path(
            "/Users/hal/git/nexus/.claude/worktrees/agent-x/docs/foo.md",
            ".claude/worktrees/agent-x/docs/foo.md",
            "/Users/hal/git/nexus",
        )
        assert result == "/Users/hal/git/nexus/.claude/worktrees/agent-x/docs/foo.md"

    def test_relative_fp_reconstructed_against_owner_root(self):
        from nexus.repo_identity import reconstruct_absolute_registered_path

        result = reconstruct_absolute_registered_path(
            "", ".claude/worktrees/agent-y/docs/bar.md", "/Users/hal/git/nexus",
        )
        assert result == "/Users/hal/git/nexus/.claude/worktrees/agent-y/docs/bar.md"

    def test_falls_back_when_owner_root_unknown(self):
        from nexus.repo_identity import reconstruct_absolute_registered_path

        result = reconstruct_absolute_registered_path(
            "", "docs/bar.md", "",
        )
        assert result == "docs/bar.md"
