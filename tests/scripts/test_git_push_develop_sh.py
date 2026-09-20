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
    """Run the script. Unless a test says otherwise, the nexus-bbriq scope
    audit is given an allow-everything pathspec: these tests are about
    VOUCHING, and every one of them would otherwise refuse at the scope gate
    for a reason that has nothing to do with what it is testing. The scope
    gate has its own class below."""
    merged = {**os.environ, **(env or {})}
    if "NX_PUSH_ALLOWED_PATHS" not in merged:
        # The NAMED skip, not a wildcard allowlist: a wildcard is refused
        # outright (Sam, 2026-09-18) precisely because it would pass the
        # audit while proving nothing, and silently. These tests are about
        # VOUCHING, so they say so rather than faking a scope they do not
        # care about.
        merged.setdefault("NX_PUSH_SKIP_SCOPE_AUDIT", "vouching test, scope not under test")
    return subprocess.run(
        [str(SCRIPT), *vouch], cwd=work, env=merged,
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


class TestScopeAudit:
    """nexus-bbriq: vouching is by SHA, so it proves you MADE the commit and
    says nothing about WHAT IS IN IT.

    On 2026-09-17 a peer had staged a 740-line docs/rdr/rdr-212-*.md draft in
    the shared index. An accept commit ran `git add <two paths>` then a BARE
    `git commit`, which commits the whole index, so the peer's draft rode
    0249b0c98 through this script to origin/develop. Every vouch check
    passed, correctly.
    """

    def test_a_foreign_file_in_a_vouched_commit_refuses_the_push(self, repos) -> None:
        origin, work = repos
        before = _remote_tip(origin)
        (work / "mine.txt").write_text("mine")
        (work / "peer-draft.md").write_text("a peer's unreviewed draft")
        _git("add", "mine.txt", "peer-draft.md", cwd=work)
        _git("commit", "-q", "-m", "mine", cwd=work)
        sha = _git("rev-parse", "HEAD", cwd=work)
        proc = _run(work, sha, env={"NX_PUSH_ALLOWED_PATHS": "mine.txt"})
        assert proc.returncode == 7, proc.stdout + proc.stderr
        assert "PUSH_REFUSED_SCOPE" in proc.stdout
        assert "peer-draft.md" in proc.stdout
        assert _remote_tip(origin) == before, "the push must not have happened"

    def test_an_in_scope_commit_pushes(self, repos) -> None:
        origin, work = repos
        sha = _commit(work, "mine.txt")
        proc = _run(work, sha, env={"NX_PUSH_ALLOWED_PATHS": "mine.txt"})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == f"PUSH_OK n=1 tip={sha}"
        assert _remote_tip(origin) == sha

    def test_an_unset_allowlist_refuses_and_prints_a_pasteable_line(self, repos) -> None:
        """The gate is mandatory, not opt-in: an audit with no allowlist
        passes everything, which is the vacuous-gate class (nexus-moht0)."""
        origin, work = repos
        before = _remote_tip(origin)
        sha = _commit(work, "mine.txt")
        env = {k: v for k, v in os.environ.items() if k != "NX_PUSH_ALLOWED_PATHS"}
        proc = subprocess.run(
            [str(SCRIPT), sha], cwd=work, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 7, proc.stdout + proc.stderr
        assert "PUSH_REFUSED_SCOPE" in proc.stdout
        assert "mine.txt" in proc.stdout
        assert "NX_PUSH_ALLOWED_PATHS=" in proc.stdout
        assert _remote_tip(origin) == before

    def test_a_glob_pathspec_is_not_expanded_by_this_shell(self, repos) -> None:
        """REGRESSION. An unquoted ${NX_PUSH_ALLOWED_PATHS} word-split would
        glob-expand `docs/*` against the script's own cwd BEFORE the audit
        ever saw it, silently narrowing the allowlist to whatever happens to
        exist there. `read -ra` splits without expanding.

        Built so it can only pass for the right reason: `docs/*` must cover
        a file that does NOT exist in the working tree at push time, so a
        cwd-expanded allowlist cannot match it.
        """
        origin, work = repos
        docs = work / "docs"
        docs.mkdir()
        (docs / "kept.md").write_text("kept")
        _git("add", "docs/kept.md", cwd=work)
        _git("commit", "-q", "-m", "kept", cwd=work)
        (docs / "removed.md").write_text("removed")
        _git("add", "docs/removed.md", cwd=work)
        _git("commit", "-q", "-m", "removed", cwd=work)
        _git("rm", "-q", "docs/removed.md", cwd=work)
        _git("commit", "-q", "-m", "drop", cwd=work)
        shas = _git("rev-list", "--reverse", "origin/develop..HEAD", cwd=work).split()
        assert not (docs / "removed.md").exists(), "fixture must leave the file absent"
        proc = _run(work, *shas, env={"NX_PUSH_ALLOWED_PATHS": "docs/*"})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _remote_tip(origin) == shas[-1]

    def test_the_skip_is_loud_and_named(self, repos) -> None:
        """There is no silent skip: skipping prints the reason it was given."""
        origin, work = repos
        sha = _commit(work, "mine.txt")
        env = {k: v for k, v in os.environ.items() if k != "NX_PUSH_ALLOWED_PATHS"}
        env["NX_PUSH_SKIP_SCOPE_AUDIT"] = "rebuilding an index git mangled"
        proc = subprocess.run(
            [str(SCRIPT), sha], cwd=work, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "PUSH_SCOPE_AUDIT_SKIPPED" in proc.stderr
        assert "rebuilding an index git mangled" in proc.stderr
        assert proc.stdout.strip() == f"PUSH_OK n=1 tip={sha}", (
            "the skip notice must not pollute the machine-readable stdout contract"
        )
        assert _remote_tip(origin) == sha

    def test_the_audit_runs_after_the_vouch_checks(self, repos) -> None:
        """An unvouched commit is refused as FOREIGN, not as a scope problem:
        the scope message would send the caller to fix the wrong thing."""
        origin, work = repos
        _commit(work, "peer.txt")
        env = {k: v for k, v in os.environ.items() if k != "NX_PUSH_ALLOWED_PATHS"}
        proc = subprocess.run(
            [str(SCRIPT)], cwd=work, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "PUSH_REFUSED_FOREIGN" in proc.stdout


    def test_a_wildcard_allowlist_is_refused_and_names_the_skip(self, repos) -> None:
        """A wildcard satisfies "required" while auditing nothing, and unlike
        the named skip it leaves no trace saying so. Refused, so there is
        exactly one escape and it is visible."""
        origin, work = repos
        before = _remote_tip(origin)
        sha = _commit(work, "mine.txt")
        env = {k: v for k, v in os.environ.items()
               if k not in ("NX_PUSH_ALLOWED_PATHS", "NX_PUSH_SKIP_SCOPE_AUDIT")}
        env["NX_PUSH_ALLOWED_PATHS"] = "*"
        proc = subprocess.run(
            [str(SCRIPT), sha], cwd=work, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 7, proc.stdout + proc.stderr
        assert "matches every file" in proc.stdout
        assert "NX_PUSH_SKIP_SCOPE_AUDIT" in proc.stdout
        assert _remote_tip(origin) == before

    def test_the_file_listing_is_correct_for_a_multi_commit_range(self, repos) -> None:
        """REGRESSION. `git diff-tree` takes at most TWO tree-ish arguments;
        the rest are path filters. Passing the whole range in one call
        measured: one sha -> its own file, two shas -> the diff BETWEEN them,
        three shas -> NOTHING. This repo batches work into one push by
        convention, so the common case was the empty one, and an empty
        listing handed the caller a pasteable empty allowlist."""
        origin, work = repos
        for name in ("alpha.txt", "beta.txt", "gamma.txt"):
            _commit(work, name)
        env = {k: v for k, v in os.environ.items()
               if k not in ("NX_PUSH_ALLOWED_PATHS", "NX_PUSH_SKIP_SCOPE_AUDIT")}
        shas = _git("rev-list", "--reverse", "origin/develop..HEAD", cwd=work).split()
        assert len(shas) == 3
        proc = subprocess.run(
            [str(SCRIPT), *shas], cwd=work, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 7, proc.stdout + proc.stderr
        for name in ("alpha.txt", "beta.txt", "gamma.txt"):
            assert name in proc.stdout, f"{name} missing from the listing:\n{proc.stdout}"


    def test_a_newline_separated_allowlist_is_not_truncated(self, repos) -> None:
        """REGRESSION. `read` stops at the first newline, so a multi-line
        allowlist was read as its FIRST ENTRY ONLY and every other outbound
        file was reported as foreign -- the caller's own files, in the
        caller's own commit. Fails closed, so nothing unsafe shipped, but it
        accuses you of smuggling when the real fault is a newline (nexus-01,
        2026-09-19).
        """
        origin, work = repos
        (work / "one.txt").write_text("one")
        (work / "two.txt").write_text("two")
        _git("add", "one.txt", "two.txt", cwd=work)
        _git("commit", "-q", "-m", "pair", cwd=work)
        sha = _git("rev-parse", "HEAD", cwd=work)
        env = {k: v for k, v in os.environ.items()
               if k not in ("NX_PUSH_ALLOWED_PATHS", "NX_PUSH_SKIP_SCOPE_AUDIT")}
        env["NX_PUSH_ALLOWED_PATHS"] = "one.txt\ntwo.txt"
        proc = subprocess.run(
            [str(SCRIPT), sha], cwd=work, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == f"PUSH_OK n=1 tip={sha}"
        assert _remote_tip(origin) == sha

    def test_a_newline_separated_wildcard_is_still_refused(self, repos) -> None:
        """Folding newlines must not let a wildcard in through the back door:
        the wildcard check runs on the same folded value."""
        origin, work = repos
        before = _remote_tip(origin)
        sha = _commit(work, "mine.txt")
        env = {k: v for k, v in os.environ.items()
               if k not in ("NX_PUSH_ALLOWED_PATHS", "NX_PUSH_SKIP_SCOPE_AUDIT")}
        env["NX_PUSH_ALLOWED_PATHS"] = "src/\n*"
        proc = subprocess.run(
            [str(SCRIPT), sha], cwd=work, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 7, proc.stdout + proc.stderr
        assert "matches every file" in proc.stdout
        assert _remote_tip(origin) == before
