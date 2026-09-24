# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/git-push-develop.sh (nexus-9wxu6): the vouched push.

Every session on the shared box commits as the same git user, so the
outbound range origin/develop..develop cannot be attributed by author. The
script pushes only when the range equals the set of commits the caller
vouches for on its command line. These tests drive a real bare origin and
two clones standing in for two sessions of one checkout.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

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
    gate has its own class below.

    Same shape for the nexus-agctp push lock: every test in this file
    EXCEPT ``TestPushLock`` predates the lock and has no opinion on it, so
    it gets the named skip by default too -- a caller that sets
    ``NX_SERVICE_PORT`` (``TestPushLock`` pointing at a real, per-test
    engine substrate) is deliberately exercising the lock and must not
    have it silently skipped out from under it.
    """
    merged = {**os.environ, **(env or {})}
    if "NX_PUSH_ALLOWED_PATHS" not in merged:
        # The NAMED skip, not a wildcard allowlist: a wildcard is refused
        # outright (Sam, 2026-09-18) precisely because it would pass the
        # audit while proving nothing, and silently. These tests are about
        # VOUCHING, so they say so rather than faking a scope they do not
        # care about.
        merged.setdefault("NX_PUSH_SKIP_SCOPE_AUDIT", "vouching test, scope not under test")
    if "NX_SERVICE_PORT" not in merged:
        merged.setdefault("NX_PUSH_SKIP_LOCK", "pre-existing test, lock not under test")
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
        env["NX_PUSH_SKIP_LOCK"] = "scope-audit test, lock not under test"
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
        env["NX_PUSH_SKIP_LOCK"] = "scope-audit test, lock not under test"
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


class TestPushLock:
    """nexus-agctp: the tuple-space mutex the script takes on
    ``lock/ci-develop-push`` immediately before ``git push`` and releases
    right after, whether the push succeeded or failed.

    Real engine substrate, not a mock (test-authoring's fixture-MVV-is-not-
    the-live-path rule): the script shells out to the installed ``nx``
    binary, and only a real tuple-space round trip proves the CLI flags
    and the JSON shapes this script parses (``claim_id``, ``claimant``,
    ``lease_until``, ``claim_state``) actually line up.

    Every test points ``nx tuple`` at a FRESH, per-test tenant on that
    substrate (never the box's own ambient ``lock/ci-develop-push`` row,
    which real sessions may hold) via a minted tenant-bound token --
    tenant isolation (RLS) keeps two tenants' rows of the identical
    resource name from colliding, verified directly against the
    substrate before this class was written: a second tenant's ``rd`` of
    a first tenant's ``lock/<resource>`` row returned nothing. Each test
    also points ``NEXUS_CONFIG_DIR`` at an isolated, empty directory, so
    the CLI's own self-heal (config.yml / a running local daemon's
    lease) can never quietly re-resolve to this box's REAL ambient
    service out from under a deliberately bad ``NX_SERVICE_PORT`` --
    confirmed necessary here: an earlier hand probe with a bogus port but
    the real ``NEXUS_CONFIG_DIR`` silently succeeded against this box's
    live daemon instead of failing.
    """

    @staticmethod
    def _engine_state() -> dict:
        from tests._engine_substrate import ensure_engine
        from tests.db._service_fixture import jar_freshness_skip_reason

        reason = jar_freshness_skip_reason()
        if reason is not None:
            pytest.skip(f"engine substrate: {reason}")
        try:
            return ensure_engine()
        except RuntimeError as exc:
            pytest.skip(f"engine substrate unavailable: {exc}")

    @staticmethod
    def _mint(state: dict) -> tuple[str, str]:
        from tests._engine_substrate import mint_test_tenant

        return mint_test_tenant(state)

    @staticmethod
    def _without_this_worktrees_venv(env: dict) -> dict:
        """Strip this worktree's own ``.venv/bin`` from PATH.

        ``uv run pytest`` prepends the worktree's venv to PATH for the
        pytest process itself, and a subprocess env built from
        ``os.environ`` inherits it -- so a bare ``nx`` there resolves to
        THIS worktree's own editable install, not the installed
        generation a real invocation of this script (never run through
        ``uv run``) would find. That editable install IS a dev-checkout
        process, so the nexus-a2qhz production-write guard refuses its
        write outright -- confirmed directly: these tests failed with
        ``ProductionWriteGuardError`` before this strip was added, even
        though every write here targets the throwaway engine substrate.
        Removing the worktree's venv from PATH makes the subprocess `nx`
        resolve the same way a real push does.
        """
        venv_bin = str(REPO_ROOT / ".venv" / "bin")
        parts = [p for p in env.get("PATH", "").split(os.pathsep) if p != venv_bin]
        return {**env, "PATH": os.pathsep.join(parts)}

    @staticmethod
    def _installed_nx_standin(tmp_path: Path) -> Path:
        """A stand-in "installed nx generation" for tests that need the
        script's nx-resolution (review finding 2) to succeed normally, not
        refuse.

        `_without_this_worktrees_venv` above strips this worktree's own
        `.venv/bin` because that IS the dev-checkout install the resolver
        is correct to refuse -- but on a box with no
        `scripts/reinstall-tool.sh`-installed generation (every GitHub
        Actions runner: confirmed directly, CI run 35900352343 failed
        every TestPushLock case expecting a normal push with
        PUSH_REFUSED_LOCK_DEV_CHECKOUT_NX, plus a bare FileNotFoundError
        for 'nx' from a test's own direct subprocess call), stripping
        `.venv/bin` leaves NOTHING on PATH for either the script or these
        tests' own direct `nx` calls to find. This directory -- under
        `tmp_path`, so always outside this repo and outside any `.venv/`
        -- holds a tiny wrapper that `exec`s the real venv `nx` by its own
        absolute path, standing in for "an installed generation" without
        being one: prepending it to PATH satisfies the resolver (not
        under `.venv/`, not under this checkout) while still running the
        exact `nx` this dev checkout has.

        A wrapper, not a symlink straight to `.venv/bin/nx`: harmless
        here either way, since that script's own shebang is an ABSOLUTE
        path to `.venv/bin/python` (confirmed directly, so the kernel
        follows it regardless of how the script itself was reached) --
        but the wrapper form matches this repo's own installed-`nx` shim
        pattern (`~/.local/bin/nx`) and costs nothing extra.
        """
        # NOT `.resolve()`: `sys.executable` is `.venv/bin/python3`, where
        # the venv's `nx` console script actually lives as a sibling --
        # `uv run` gives THIS worktree's own `.venv/bin/python3` directly,
        # not a further symlink to it. `.resolve()` follows that path's
        # OWN symlink chain past `.venv/bin/` to uv's shared interpreter
        # install (`~/.local/share/uv/python/...`), which has no `nx` at
        # all -- confirmed directly: resolving landed one directory too
        # far and skipped every TestPushLock case needing this stand-in.
        real_nx = Path(sys.executable).parent / "nx"
        if not real_nx.exists():
            pytest.skip(f"no venv nx entry point at {real_nx} to stand in for an installed generation")
        standin_dir = tmp_path / "installed-nx-standin"
        standin_dir.mkdir(exist_ok=True)
        wrapper = standin_dir / "nx"
        wrapper.write_text(f"#!/usr/bin/env bash\nexec {shlex.quote(str(real_nx))} \"$@\"\n")
        wrapper.chmod(0o755)
        return standin_dir

    @classmethod
    def _with_installed_nx_standin(cls, env: dict, tmp_path: Path) -> dict:
        """`_without_this_worktrees_venv` plus the stand-in prepended to
        PATH -- the combination every TestPushLock test that expects the
        script to proceed normally (not refuse on nx-resolution) needs.

        The stand-in changes which PATH ENTRY resolves `nx` (satisfying
        the SCRIPT's own `.venv/`-path check), but the process it execs
        is still this dev checkout's own `nx`/`nexus` -- there is no
        other kind available on a box with no installed generation (every
        CI runner). So its real writes still trip the SEPARATE
        nexus-a2qhz production-write guard, which checks where the
        RUNNING process's `nexus` package resolves from, not the PATH
        used to reach it -- confirmed directly: without this, every test
        using the stand-in failed with ProductionWriteGuardError instead
        of reaching the throwaway engine substrate at all. The guard's
        own docstring names the fix for exactly this shape: "a test that
        spawns a subprocess needing the REAL guard behavior ... against
        the test substrate must set the real NX_ALLOW_PROD_WRITE env var
        explicitly for that subprocess's own environment." Every write
        under this stand-in targets ONLY the per-test throwaway engine
        (tests/_engine_substrate.py), never anything real.
        """
        env = cls._without_this_worktrees_venv(env)
        standin_dir = cls._installed_nx_standin(tmp_path)
        env["PATH"] = f"{standin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["NX_ALLOW_PROD_WRITE"] = (
            "TestPushLock nx-standin: every write targets a throwaway "
            "per-test engine substrate, never a real install"
        )
        return env

    @classmethod
    def _lock_env(cls, state: dict, token: str, tmp_path: Path, *, label: str) -> dict:
        parsed = urlparse(state["base_url"])
        cfg_dir = tmp_path / f"nexus-config-isolated-{label}"
        cfg_dir.mkdir(exist_ok=True)
        env = cls._with_installed_nx_standin({**os.environ}, tmp_path)
        # tests/conftest.py's own autouse engine-substrate fixture sets
        # NX_SERVICE_URL in THIS pytest process's os.environ (for in-process
        # T2 store construction) -- and NX_SERVICE_URL outranks NX_SERVICE_
        # HOST/PORT in resolve_service_endpoint's real precedence, so left
        # in place it silently overrides the HOST/PORT override below and
        # makes these tests exercise a leg they never intended to. An empty
        # string, NOT a pop: `_run()` merges `{**os.environ, **env}`, so a
        # key ABSENT from `env` leaves whatever `os.environ` already has
        # untouched -- only a key genuinely PRESENT in `env` (even "") wins
        # the merge (confirmed directly: popping alone left the leaked
        # NX_SERVICE_URL in the subprocess's env).
        env["NX_SERVICE_URL"] = ""
        env["NEXUS_CONFIG_DIR"] = str(cfg_dir)
        env["NX_SERVICE_HOST"] = parsed.hostname or "127.0.0.1"
        env["NX_SERVICE_PORT"] = str(parsed.port)
        env["NX_SERVICE_TOKEN"] = token
        env["NX_PUSH_SKIP_SCOPE_AUDIT"] = "lock test, scope not under test"
        return env

    def test_a_lock_held_by_another_claimant_refuses_and_names_the_holder(
        self, repos, tmp_path,
    ) -> None:
        origin, work = repos
        state = self._engine_state()
        _tenant, token = self._mint(state)
        env = self._lock_env(state, token, tmp_path, label="held")

        out = subprocess.run(
            ["nx", "tuple", "out", "lock/ci-develop-push", "--key", "resource=ci-develop-push"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stdout + out.stderr
        claim = subprocess.run(
            ["nx", "tuple", "in", "lock/ci-develop-push", "--pattern", "resource=ci-develop-push",
             "--claimant", "peer-session", "--lease-s", "900", "--timeout-s", "0"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert claim.returncode == 0, claim.stdout + claim.stderr

        before = _remote_tip(origin)
        sha = _commit(work, "mine.txt")
        proc = _run(work, sha, env=env)
        assert proc.returncode == 8, proc.stdout + proc.stderr
        assert proc.stdout.startswith("PUSH_REFUSED_LOCK_HELD")
        assert "peer-session" in proc.stdout
        assert "lease_until=" in proc.stdout
        # Review finding 1: mutual exclusion only holds if every pusher
        # resolves the SAME tuple-space service and tenant -- the refusal
        # must name what THIS invocation resolved, so a split-brain (two
        # sessions pointed at different services) is visible from the
        # message alone.
        assert f"endpoint={env['NX_SERVICE_HOST']}:{env['NX_SERVICE_PORT']}" in proc.stdout, proc.stdout
        assert "tenant=" in proc.stdout
        assert _remote_tip(origin) == before, "a held lock must not let the push through"

    def test_a_free_lock_allows_the_push_and_is_released_afterward(
        self, repos, tmp_path,
    ) -> None:
        origin, work = repos
        state = self._engine_state()
        _tenant, token = self._mint(state)
        env = self._lock_env(state, token, tmp_path, label="free")

        sha = _commit(work, "mine.txt")
        proc = _run(work, sha, env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == f"PUSH_OK n=1 tip={sha}"
        assert _remote_tip(origin) == sha
        # Review finding 1: the claimed/released diagnostics go to stderr
        # (stdout stays the PUSH_OK machine-readable contract) and name
        # what this invocation resolved, on both outcomes.
        assert "PUSH_LOCK_CLAIMED" in proc.stderr, proc.stderr
        assert "PUSH_LOCK_RELEASED" in proc.stderr, proc.stderr
        assert f"endpoint={env['NX_SERVICE_HOST']}:{env['NX_SERVICE_PORT']}" in proc.stderr, proc.stderr

        rd = subprocess.run(
            ["nx", "tuple", "rd", "lock/ci-develop-push", "--pattern", "resource=ci-develop-push", "--json"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert rd.returncode == 0, rd.stdout + rd.stderr
        rows = json.loads(rd.stdout)
        assert rows, "the script's own `out` must have created the lock row"
        assert rows[0]["claim_state"] is None, (
            f"the lock must be released (claim_state null) after a successful push: {rows[0]}"
        )

    def test_an_unreachable_tuple_space_refuses_unless_the_skip_is_set(
        self, repos, tmp_path,
    ) -> None:
        origin, work = repos
        cfg_dir = tmp_path / "nexus-config-isolated-unreachable"
        cfg_dir.mkdir(exist_ok=True)
        env = self._with_installed_nx_standin({**os.environ}, tmp_path)
        env["NX_SERVICE_URL"] = ""  # see _lock_env's comment: pop alone does not survive _run()'s merge
        env["NEXUS_CONFIG_DIR"] = str(cfg_dir)
        env["NX_SERVICE_HOST"] = "127.0.0.1"
        env["NX_SERVICE_PORT"] = "1"  # nothing listens: connection refused
        env["NX_SERVICE_TOKEN"] = "bogus"
        env["NX_PUSH_SKIP_SCOPE_AUDIT"] = "lock test, scope not under test"

        before = _remote_tip(origin)
        sha = _commit(work, "mine.txt")
        proc = _run(work, sha, env=env)
        assert proc.returncode == 9, proc.stdout + proc.stderr
        assert proc.stdout.startswith("PUSH_REFUSED_LOCK_UNREACHABLE")
        # Review finding 1: named even on the unreachable path -- the
        # endpoint this invocation TRIED is still resolvable from env,
        # independent of whether the tuple space itself answered.
        assert "endpoint=127.0.0.1:1" in proc.stdout, proc.stdout
        assert _remote_tip(origin) == before, "an unreachable lock must not let the push through"

        env["NX_PUSH_SKIP_LOCK"] = "tuple space unreachable in this test, verifying the escape"
        proc2 = _run(work, sha, env=env)
        assert proc2.returncode == 0, proc2.stdout + proc2.stderr
        assert proc2.stdout.strip() == f"PUSH_OK n=1 tip={sha}"
        assert _remote_tip(origin) == sha

    # Stub `nx` for the malformed-claim regression below: a genuinely
    # successful claim (rc 0) whose JSON response this script cannot parse
    # a claim_id out of. No real engine is involved -- this test is about
    # the SCRIPT's own response-parsing robustness, not the tuple space.
    _MALFORMED_CLAIM_STUB_NX = """#!/usr/bin/env bash
set -euo pipefail
log="${STUB_NX_LOG:?}"
printf '%s\\n' "$*" >> "$log"
case "${1:-} ${2:-}" in
  "tuple out")
    echo "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    ;;
  "tuple in")
    echo '{"tuple": {"id": "deadbeef", "claim_state": "claimed"}}'
    ;;
  "tuple release")
    echo "STUB_NX: release must never be called with no parseable claim id" >&2
    exit 1
    ;;
  "tuple rd")
    echo '[]'
    ;;
  "config get")
    echo "not set"
    ;;
  "daemon service")
    echo "no lease" >&2
    exit 1
    ;;
  *)
    echo "stub-nx: unhandled invocation: $*" >&2
    exit 1
    ;;
esac
"""

    def test_a_malformed_claim_response_fails_loud_without_orphaning_or_releasing(
        self, repos, tmp_path,
    ) -> None:
        """Ship-blocker (code-review round N, scripts/git-push-develop.sh
        ~412-416): a bare `x="$(cmd)"` claim-id-parse assignment aborts the
        WHOLE script under `set -e` the instant `cmd` is nonzero -- which
        happens BEFORE `_lock_claim_id` is ever set, so the EXIT trap finds
        it empty and releases nothing, even though `nx tuple in` really did
        succeed and a live claim now exists server-side. That orphans the
        lock for its full 900s lease and blocks every push on the box, with
        only a raw traceback to show for it.

        A stub `nx` earlier on PATH stands in for that exact response shape
        -- `tuple in` exits 0 (a genuine claim) but its JSON carries no
        `claim_id` -- proving the fix guards the RESPONSE SHAPE, not
        whatever the real engine happens to return today. Asserts: (a) a
        clean, named failure (not a traceback), (b) `git push` never runs,
        and (c) `nx tuple release` is NEVER called with a garbage/empty
        claim id -- the log line-per-invocation record must contain no
        "release" call at all.
        """
        origin, work = repos
        stub_dir = tmp_path / "stub-nx-bin"
        stub_dir.mkdir()
        stub_log = tmp_path / "stub-nx.log"
        stub = stub_dir / "nx"
        stub.write_text(self._MALFORMED_CLAIM_STUB_NX)
        stub.chmod(0o755)

        env = {**os.environ}
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        env["STUB_NX_LOG"] = str(stub_log)
        env["NX_PUSH_SKIP_SCOPE_AUDIT"] = "lock test, scope not under test"
        env["NX_SERVICE_PORT"] = "0"  # any value: only to opt OUT of _run()'s pre-existing-test auto-skip

        before = _remote_tip(origin)
        sha = _commit(work, "mine.txt")
        proc = _run(work, sha, env=env)
        assert proc.returncode == 11, proc.stdout + proc.stderr
        assert "PUSH_LOCK_RELEASE_FAILED" in proc.stdout
        assert "claim id could not be parsed" in proc.stdout
        assert _remote_tip(origin) == before, "a claim whose id could not be parsed must not let the push through"

        log_text = stub_log.read_text() if stub_log.exists() else ""
        assert "release" not in log_text, (
            f"nx tuple release must never be called with no parseable claim id:\n{log_text}"
        )
        assert "tuple in" in log_text, f"the stub must have actually been asked to claim:\n{log_text}"

    def test_only_a_dev_checkout_or_venv_nx_on_path_refuses_distinctly(
        self, repos, tmp_path,
    ) -> None:
        """Review finding 2: `uv run` / an activated venv put a checkout's
        own `.venv/bin` ahead of the installed generation on PATH, so a
        bare `nx` there resolves to a DEV-CHECKOUT editable install --
        which the nexus-a2qhz production-write guard refuses to write
        through, reading identically to a genuinely unreachable tuple
        space (PUSH_REFUSED_LOCK_UNREACHABLE) and teaching operators to
        reach for NX_PUSH_SKIP_LOCK for the wrong reason.

        PATH here is the REAL ambient PATH with every directory that
        contains an `nx` file removed, plus a `.venv/bin/nx` stub
        prepended -- so the only `nx` this process can find is
        disqualified, deterministically, regardless of what genuinely is
        or is not installed on the host running this test.
        """
        origin, work = repos
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        stub = venv_bin / "nx"
        stub.write_text("#!/usr/bin/env bash\necho stub-nx-should-never-run\nexit 1\n")
        stub.chmod(0o755)

        kept_dirs = [
            d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not (Path(d) / "nx").exists()
        ]
        env = {**os.environ}
        env["PATH"] = os.pathsep.join([str(venv_bin), *kept_dirs])
        env["NX_PUSH_SKIP_SCOPE_AUDIT"] = "lock test, scope not under test"
        env["NX_SERVICE_PORT"] = "0"  # any value: only to opt OUT of _run()'s pre-existing-test auto-skip

        before = _remote_tip(origin)
        sha = _commit(work, "mine.txt")
        proc = _run(work, sha, env=env)
        assert proc.returncode == 10, proc.stdout + proc.stderr
        assert proc.stdout.startswith("PUSH_REFUSED_LOCK_DEV_CHECKOUT_NX")
        assert _remote_tip(origin) == before, "no qualifying nx must not let the push through"
