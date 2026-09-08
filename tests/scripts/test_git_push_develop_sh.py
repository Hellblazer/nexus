# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/git-push-develop.sh (nexus-9wxu6): the vouched push.

Every session on the shared box commits as the same git user, so the
outbound range origin/develop..develop cannot be attributed by author. The
script pushes only when the range equals the set of commits the caller
vouches for on its command line. These tests drive a real bare origin and
two clones standing in for two sessions of one checkout.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "git-push-develop.sh"

_GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *_GIT_ID, *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _commit(work: Path, name: str) -> str:
    (work / name).write_text(name)
    _git("add", name, cwd=work)
    _git("commit", "-q", "-m", name, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


@pytest.fixture()
def repos(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "--bare", "--initial-branch=develop", cwd=origin)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "--initial-branch=develop", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    _commit(work, "base")
    _git("push", "-q", "-u", "origin", "develop", cwd=work)
    return origin, work


def _run(work: Path, *vouch: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *vouch], cwd=work, env={**os.environ, **(env or {})},
        capture_output=True, text=True, timeout=60,
    )


def _remote_tip(origin: Path) -> str:
    return _git("rev-parse", "refs/heads/develop", cwd=origin)


class TestScriptShape:
    def test_executable(self) -> None:
        assert SCRIPT.exists() and os.access(SCRIPT, os.X_OK)


class TestHappyPath:
    def test_nothing_outbound_and_nothing_vouched_is_a_noop(self, repos) -> None:
        origin, work = repos
        proc = _run(work)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("PUSH_NOOP")

    def test_fully_vouched_range_pushes(self, repos) -> None:
        origin, work = repos
        a = _commit(work, "a")
        b = _commit(work, "b")
        proc = _run(work, a, b[:7])
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == f"PUSH_OK n=2 tip={b}"
        assert _remote_tip(origin) == b

    def test_pushes_develop_from_a_detached_worktree(self, repos, tmp_path) -> None:
        origin, work = repos
        a = _commit(work, "a")
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", "--detach", str(wt), "HEAD~1", cwd=work)
        proc = _run(wt, a)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _remote_tip(origin) == a


class TestDetachedSource:
    def test_head_of_a_detached_worktree_pushes_without_touching_the_local_branch(self, repos, tmp_path) -> None:
        origin, work = repos
        peer = _commit(work, "peer-unpushed")
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", "--detach", str(wt), "origin/develop", cwd=work)
        mine = _commit(wt, "mine")
        proc = _run(wt, mine, env={"NX_PUSH_SOURCE": "HEAD"})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == f"PUSH_OK n=1 tip={mine}"
        assert _remote_tip(origin) == mine
        assert _git("rev-parse", "refs/heads/develop", cwd=work) == peer

    def test_detached_source_still_refuses_an_unvouched_commit(self, repos, tmp_path) -> None:
        origin, work = repos
        before = _remote_tip(origin)
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", "--detach", str(wt), "origin/develop", cwd=work)
        _commit(wt, "a")
        b = _commit(wt, "b")
        proc = _run(wt, b, env={"NX_PUSH_SOURCE": "HEAD"})
        assert proc.returncode == 2
        assert _remote_tip(origin) == before


class TestBackMerge:
    """The release and plugin-cut back-merges: origin/main merged into
    develop carries main-only commits nobody on develop authored."""

    def _main_ahead(self, repos, tmp_path):
        origin, work = repos
        other = tmp_path / "other"
        _git("clone", "-q", "-b", "develop", str(origin), str(other), cwd=tmp_path)
        _git("checkout", "-q", "-b", "main", cwd=other)
        rel = _commit(other, "release-only")
        _git("push", "-q", "origin", "main", cwd=other)
        _git("fetch", "-q", "origin", cwd=work)
        return origin, work, rel

    def test_vouched_merge_covers_what_it_merges_in(self, repos, tmp_path) -> None:
        origin, work, rel = self._main_ahead(repos, tmp_path)
        _git("merge", "-q", "--no-ff", "--no-edit", "origin/main", cwd=work)
        merge = _git("rev-parse", "HEAD", cwd=work)
        proc = _run(work, merge)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == f"PUSH_OK n=2 tip={merge}"
        assert _remote_tip(origin) == merge

    def test_fast_forward_back_merge_vouches_head_and_its_merged_in_branch(self, repos, tmp_path) -> None:
        """Right after a release the back-merge fast-forwards onto main's PR
        merge commit; vouching HEAD covers the release branch under it."""
        origin, work = repos
        other = tmp_path / "other"
        _git("clone", "-q", "-b", "develop", str(origin), str(other), cwd=tmp_path)
        _git("checkout", "-q", "-b", "release/x", cwd=other)
        bump = _commit(other, "bump")
        _git("checkout", "-q", "-b", "main", "origin/develop", cwd=other)
        _git("merge", "-q", "--no-ff", "--no-edit", "release/x", cwd=other)
        pr_merge = _git("rev-parse", "HEAD", cwd=other)
        _git("push", "-q", "origin", "main", cwd=other)
        _git("fetch", "-q", "origin", cwd=work)
        _git("merge", "-q", "--no-edit", "origin/main", cwd=work)
        assert _git("rev-parse", "HEAD", cwd=work) == pr_merge
        proc = _run(work, "HEAD")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == f"PUSH_OK n=2 tip={pr_merge}"
        assert bump in _git("rev-list", "refs/heads/develop", cwd=origin)

    def test_vouched_merge_does_not_cover_a_peer_commit_beneath_it(self, repos, tmp_path) -> None:
        origin, work, rel = self._main_ahead(repos, tmp_path)
        before = _remote_tip(origin)
        peer = _commit(work, "peer")
        _git("merge", "-q", "--no-edit", "origin/main", cwd=work)
        merge = _git("rev-parse", "HEAD", cwd=work)
        proc = _run(work, merge)
        assert proc.returncode == 2
        assert peer[:7] in proc.stdout and rel[:7] not in proc.stdout
        assert _remote_tip(origin) == before


class TestRefusals:
    def test_unvouched_commit_in_range_is_refused_and_named(self, repos) -> None:
        origin, work = repos
        mine = _commit(work, "mine")
        peer = _commit(work, "peer")
        before = _remote_tip(origin)
        proc = _run(work, mine)
        assert proc.returncode == 2
        assert proc.stdout.startswith("PUSH_REFUSED_FOREIGN 1 of 2")
        assert peer[:7] in proc.stdout and "peer" in proc.stdout
        assert mine[:7] not in proc.stdout.splitlines()[1]
        assert _remote_tip(origin) == before

    def test_no_vouch_with_outbound_commits_is_refused(self, repos) -> None:
        origin, work = repos
        before = _remote_tip(origin)
        _commit(work, "x")
        proc = _run(work)
        assert proc.returncode == 2
        assert proc.stdout.startswith("PUSH_REFUSED_FOREIGN 1 of 1")
        assert _remote_tip(origin) == before

    def test_already_pushed_vouch_is_stale(self, repos) -> None:
        origin, work = repos
        a = _commit(work, "a")
        assert _run(work, a).returncode == 0
        proc = _run(work, a)
        assert proc.returncode == 3
        assert proc.stdout.startswith("PUSH_REFUSED_STALE_VOUCH 1")

    def test_stale_vouch_with_other_outbound_commit_does_not_push(self, repos) -> None:
        origin, work = repos
        a = _commit(work, "a")
        assert _run(work, a).returncode == 0
        b = _commit(work, "b")
        proc = _run(work, a, b)
        assert proc.returncode == 3
        assert _remote_tip(origin) == a

    def test_bad_sha_is_refused_before_any_push(self, repos) -> None:
        origin, work = repos
        a = _commit(work, "a")
        proc = _run(work, a, "deadbeefdeadbeef")
        assert proc.returncode == 5
        assert proc.stdout.startswith("PUSH_REFUSED_BAD_SHA")
        assert _remote_tip(origin) != a

    def test_diverged_local_branch_is_refused(self, repos, tmp_path) -> None:
        origin, work = repos
        other = tmp_path / "other"
        _git("clone", "-q", "-b", "develop", str(origin), str(other), cwd=tmp_path)
        _commit(other, "remote-side")
        _git("push", "-q", "origin", "develop", cwd=other)
        a = _commit(work, "local-side")
        proc = _run(work, a)
        assert proc.returncode == 4
        assert proc.stdout.startswith("PUSH_REFUSED_DIVERGED")

    def test_missing_remote_branch_is_refused(self, repos) -> None:
        origin, work = repos
        a = _commit(work, "a")
        proc = _run(work, a, env={"NX_PUSH_BRANCH": "nope"})
        assert proc.returncode == 6
        assert proc.stdout.startswith("PUSH_REFUSED_NO_BRANCH")
