# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for scripts/agent-worktree-preflight.sh (nexus-5kwkf).

Real git fixtures only — tmp_path repos with actual linked worktrees, no
mocks (house style, tests/AGENTS.md). The bead's root cause: the dispatch
harness cuts agent worktrees from the DEFAULT branch's tip, not the
session's current branch, so a worktree can be silently stale relative to
the branch the agent believes it is working from. This script is the agent-
side mitigation: verify isolation, then verify (and if possible recover)
ancestry of a required base commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "agent-worktree-preflight.sh"
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "config", "user.email", "preflight-test@example.com")
    _git(path, "config", "user.name", "Preflight Test")
    _git(path, "config", "commit.gpgsign", "false")


def _commit(path: Path, message: str, fname: str = "f.txt", content: str | None = None) -> str:
    (path / fname).write_text(content if content is not None else message)
    _git(path, "add", fname)
    # Identity inline: clones do not inherit _init_repo's per-repo config, and
    # CI runners have no ambient git identity to auto-detect (hostname yields
    # "(none)" -> git refuses with exit 128), so a bare commit in a cloned
    # fixture repo is environment-dependent without this.
    _git(
        path,
        "-c",
        "user.email=preflight-test@example.com",
        "-c",
        "user.name=Preflight Test",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _git(path, "rev-parse", "HEAD")


def _run_preflight(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


@pytest.fixture
def main_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A primary (non-worktree) checkout with two commits on develop."""
    main = tmp_path / "main"
    _init_repo(main)
    sha1 = _commit(main, "c1")
    _git(main, "branch", "-m", "develop")
    sha2 = _commit(main, "c2")
    return main, sha1, sha2


def test_primary_checkout_refused(main_repo: tuple[Path, str, str]) -> None:
    """Running inside the shared primary checkout (not a worktree) refuses
    with exit 2 and makes no git-state change — this is the exact failure
    mode from nexus-5kwkf's isolation-absence incident."""
    main, sha1, sha2 = main_repo

    result = _run_preflight(main, sha2)

    assert result.returncode == 2
    assert "PREFLIGHT_FAIL_PRIMARY_CHECKOUT" in result.stdout
    assert str(main) in result.stdout
    # No recovery attempted: HEAD untouched.
    assert _git(main, "rev-parse", "HEAD") == sha2


def test_up_to_date_worktree_passes_no_recovery(
    main_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    main, sha1, sha2 = main_repo
    wt = tmp_path / "wt-uptodate"
    _git(main, "worktree", "add", "-b", "feature-uptodate", str(wt), sha2)

    result = _run_preflight(wt, sha2)

    assert result.returncode == 0
    assert result.stdout.strip() == f"PREFLIGHT_OK head={sha2} recovered=no"
    assert _git(wt, "rev-parse", "HEAD") == sha2


def test_stale_clean_worktree_ff_recovers(
    main_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """Stale-but-clean worktree (the RDR-149... err nexus-5kwkf steady-state
    case): agent worktree cut from an older commit than develop's tip.
    Preflight ff-only-merges up to REQUIRED_SHA and reports recovered=yes."""
    main, sha1, sha2 = main_repo
    wt = tmp_path / "wt-stale"
    _git(main, "worktree", "add", "-b", "feature-stale", str(wt), sha1)
    assert _git(wt, "rev-parse", "HEAD") == sha1

    result = _run_preflight(wt, sha2)

    assert result.returncode == 0
    assert result.stdout.strip() == f"PREFLIGHT_OK head={sha2} recovered=yes"
    assert _git(wt, "rev-parse", "HEAD") == sha2


def test_diverged_worktree_refused_tree_untouched(
    main_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """Worktree has its own commit that isn't an ancestor of REQUIRED_SHA
    (and vice versa) — ff-only recovery is impossible. Must refuse loudly
    rather than attempt a real (non-ff) merge, and must leave the worktree
    exactly as it was."""
    main, sha1, sha2 = main_repo
    wt = tmp_path / "wt-diverged"
    _git(main, "worktree", "add", "-b", "feature-diverged", str(wt), sha1)
    diverged_sha = _commit(wt, "diverged commit", fname="diverged.txt", content="d")

    result = _run_preflight(wt, sha2)

    assert result.returncode == 3
    assert "PREFLIGHT_FAIL_DIVERGED" in result.stdout
    # Tree untouched: HEAD unchanged, no merge/rebase state left behind.
    assert _git(wt, "rev-parse", "HEAD") == diverged_sha
    assert _git(wt, "status", "--porcelain") == ""


def test_dirty_stale_worktree_refused_dirt_intact(
    main_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """A stale worktree with uncommitted changes must never be touched by
    an automated ff-only merge — refuse before attempting recovery, and
    leave the dirt exactly as found."""
    main, sha1, sha2 = main_repo
    wt = tmp_path / "wt-dirty"
    _git(main, "worktree", "add", "-b", "feature-dirty", str(wt), sha1)
    (wt / "f.txt").write_text("locally modified, uncommitted")

    result = _run_preflight(wt, sha2)

    assert result.returncode == 4
    assert "PREFLIGHT_FAIL_DIRTY_TREE" in result.stdout
    assert _git(wt, "rev-parse", "HEAD") == sha1
    assert (wt / "f.txt").read_text() == "locally modified, uncommitted"


def test_explicit_garbage_ref_refused_as_bad_sha(
    main_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """An unresolvable REQUIRED_SHA must not crash the script via errexit
    with git's raw fatal output — it is a named, guarded refusal."""
    main, sha1, sha2 = main_repo
    wt = tmp_path / "wt-garbage-sha"
    _git(main, "worktree", "add", "-b", "feature-garbage-sha", str(wt), sha1)

    result = _run_preflight(wt, "not-a-real-ref-at-all")

    assert result.returncode == 5
    assert result.stdout.strip() == "PREFLIGHT_FAIL_BAD_SHA not-a-real-ref-at-all"
    assert result.stderr == ""  # no raw git fatal leaking through
    assert _git(wt, "rev-parse", "HEAD") == sha1


def test_explicit_wellformed_nonexistent_sha_refused_as_bad_sha(
    main_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """A syntactically valid full 40-hex sha that names no real object must
    not silently pass `git rev-parse` and misfold into the diverged path —
    it is caught by the same guarded resolve-and-verify step."""
    main, sha1, sha2 = main_repo
    wt = tmp_path / "wt-fake-sha"
    _git(main, "worktree", "add", "-b", "feature-fake-sha", str(wt), sha1)
    fake_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    result = _run_preflight(wt, fake_sha)

    assert result.returncode == 5
    assert result.stdout.strip() == f"PREFLIGHT_FAIL_BAD_SHA {fake_sha}"
    assert _git(wt, "rev-parse", "HEAD") == sha1


def test_default_prefers_local_develop_over_origin_when_ahead(
    tmp_path: Path,
) -> None:
    """When REQUIRED_SHA is omitted and local `develop` has advanced beyond
    `origin/develop` (this project's own routine batched-push workflow),
    recovery must land on LOCAL develop's tip, not the stale origin ref —
    otherwise preflight silently under-recovers in exactly the window that
    caused the nexus-5kwkf incidents."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    sha1 = _commit(origin, "c1")
    _git(origin, "branch", "-m", "develop")
    sha2 = _commit(origin, "c2")

    main = tmp_path / "main"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(main)], check=True, capture_output=True
    )
    # Local develop advances past origin/develop (unpushed, as this project's
    # own workflow routinely leaves it for extended windows).
    sha3_local_only = _commit(main, "c3 local-only, not pushed to origin")
    assert _git(main, "rev-parse", "origin/develop") == sha2
    assert _git(main, "rev-parse", "develop") == sha3_local_only

    wt = tmp_path / "wt-local-ahead"
    _git(main, "worktree", "add", "-b", "feature-local-ahead", str(wt), sha1)

    result = _run_preflight(wt)  # no REQUIRED_SHA arg

    assert result.returncode == 0
    assert result.stdout.strip() == f"PREFLIGHT_OK head={sha3_local_only} recovered=yes"
    assert _git(wt, "rev-parse", "HEAD") == sha3_local_only
    # origin/develop was never touched or fetched.
    assert _git(main, "rev-parse", "origin/develop") == sha2


def test_worktree_subdirectory_invocation_passes(
    main_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """Preflight must work when invoked from a subdirectory of the
    worktree, not just its root — git commands resolve the repo from any
    depth, and the script must not assume cwd is the worktree root."""
    main, sha1, sha2 = main_repo
    wt = tmp_path / "wt-subdir"
    _git(main, "worktree", "add", "-b", "feature-subdir", str(wt), sha2)
    subdir = wt / "nested" / "deeper"
    subdir.mkdir(parents=True)

    result = _run_preflight(subdir, sha2)

    assert result.returncode == 0
    assert result.stdout.strip() == f"PREFLIGHT_OK head={sha2} recovered=no"


def test_primary_checkout_subdirectory_invocation_refused(
    main_repo: tuple[Path, str, str],
) -> None:
    """Refusal must fire even when invoked from a subdirectory of the
    primary checkout, not just its root."""
    main, sha1, sha2 = main_repo
    subdir = main / "nested" / "deeper"
    subdir.mkdir(parents=True)

    result = _run_preflight(subdir, sha2)

    assert result.returncode == 2
    assert "PREFLIGHT_FAIL_PRIMARY_CHECKOUT" in result.stdout
    assert str(main) in result.stdout
    assert _git(main, "rev-parse", "HEAD") == sha2


def test_default_falls_back_to_origin_develop_when_no_local_develop(
    tmp_path: Path,
) -> None:
    """When the clone never created a local `develop` branch (checked out
    some other default branch), the fallback to `origin/develop` must
    still work — exercising the second leg of the resolution order."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    sha1 = _commit(origin, "c1")
    _git(origin, "checkout", "-b", "develop")
    sha2 = _commit(origin, "c2")
    _git(origin, "checkout", "-b", "trunk", sha1)

    main = tmp_path / "main"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "trunk", str(origin), str(main)],
        check=True,
        capture_output=True,
    )
    # No local `develop` branch: clone only materializes the checked-out
    # branch locally; other branches remain remote-tracking-only.
    assert (
        subprocess.run(
            ["git", "-C", str(main), "rev-parse", "-q", "--verify", "refs/heads/develop"],
            capture_output=True,
        ).returncode
        != 0
    )
    assert _git(main, "rev-parse", "origin/develop") == sha2

    wt = tmp_path / "wt-no-local-develop"
    _git(main, "worktree", "add", "-b", "feature-no-local-develop", str(wt), sha1)

    result = _run_preflight(wt)  # no REQUIRED_SHA arg

    assert result.returncode == 0
    assert result.stdout.strip() == f"PREFLIGHT_OK head={sha2} recovered=yes"
    assert _git(wt, "rev-parse", "HEAD") == sha2


def test_default_refused_when_neither_develop_ref_exists(tmp_path: Path) -> None:
    """No local `develop`, no `origin/develop` at all (not even a remote
    configured) — must refuse loudly rather than guess some other ref."""
    main = tmp_path / "main-no-develop"
    _init_repo(main)
    _commit(main, "c1")
    _git(main, "branch", "-m", "trunk")
    sha2 = _commit(main, "c2")

    wt = tmp_path / "wt-no-develop-anywhere"
    _git(main, "worktree", "add", "-b", "feature-no-develop", str(wt), sha2)

    result = _run_preflight(wt)  # no REQUIRED_SHA arg

    assert result.returncode == 5
    assert result.stdout.strip() == "PREFLIGHT_FAIL_BAD_SHA develop|origin/develop"


def test_omitted_required_sha_resolves_origin_develop_without_fetching(
    tmp_path: Path,
) -> None:
    """With REQUIRED_SHA omitted, the script must resolve a repo-local
    develop ref (never fetch) — here local `develop` and `origin/develop`
    coincide post-clone, so this exercises the never-fetch guarantee
    specifically. Proven by adding a THIRD commit to the fixture remote
    after cloning: if the script fetched, recovery would land on that
    third commit; since it must not fetch, recovery lands on the second
    (already-known) commit instead. See
    test_default_prefers_local_develop_over_origin_when_ahead and
    test_default_falls_back_to_origin_develop_when_no_local_develop for
    the local-vs-origin resolution order itself."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    sha1 = _commit(origin, "c1")
    _git(origin, "branch", "-m", "develop")
    sha2 = _commit(origin, "c2")

    main = tmp_path / "main"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(main)], check=True, capture_output=True
    )
    assert _git(main, "rev-parse", "origin/develop") == sha2

    # A commit landing upstream AFTER the clone must be invisible to a
    # script that never fetches.
    sha3 = _commit(origin, "c3 (post-clone, must stay unfetched)")
    assert sha3 != sha2

    wt = tmp_path / "wt-implicit"
    _git(main, "worktree", "add", "-b", "feature-implicit", str(wt), sha1)

    result = _run_preflight(wt)  # no REQUIRED_SHA arg

    assert result.returncode == 0
    assert result.stdout.strip() == f"PREFLIGHT_OK head={sha2} recovered=yes"
    assert _git(wt, "rev-parse", "HEAD") == sha2
    assert _git(main, "rev-parse", "origin/develop") == sha2  # still unfetched
