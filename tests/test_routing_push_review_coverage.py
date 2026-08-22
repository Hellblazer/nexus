# SPDX-License-Identifier: AGPL-3.0-or-later
"""``git push`` is blocked when the outgoing range carries a gated-path
commit with no review-completed marker (nexus-4av2n scope (b)).

CONSOLIDATED into the same script as the wildcard-add and push-to-main
checks (see test_routing_no_direct_push_to_main.py's header for why): the
RDR-121/125 cross-plugin aggregate cap on PreToolUse:Bash routing rules is
AT FOUR (3 in conexus's registry.yaml + 1 in sn's), so a fourth logical
check sharing this script's single registry entry is the sanctioned path,
not a fifth rule.

ROUND 2 (this file): after code-review-expert (T2 [21539], FIX-FIRST) and
substantive-critic (T2 [21540], not-justified) both returned round 1,
this file adds coverage for: dual-source T1+T2 marker lookup
(Critical-1), release-branch exemption (Critical-2), tag-push exemption
(Critical-3a), merge-commit visibility (Critical-3b), loud truncation on
the commit-scan cap (Significant-a), and tests/** joining the gated
prefixes (Significant-c). Real git repos throughout (bare origin + work
clone with a real upstream), matching test_routing_no_direct_push_to_main.py's
convention.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil as _shutil
import subprocess
import sys
import tempfile

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
HOOK = (
    PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing"
    / "git_add_all_redirects_to_explicit_paths.py"
)

# Isolated PATH component carrying ONLY python3 -- not the parent of `which
# python3`, which in this repo's dev venv also ships the real `nx` console
# script (confirmed live during nexus-4av2n round 1: it silently defeated
# an earlier cut of this same isolation pattern in a sibling test file).
_PYTHON3_REAL = _shutil.which("python3") or sys.executable
_PYTHON3_ISOLATED_DIR = pathlib.Path(tempfile.mkdtemp(prefix="nx-push-review-python3-"))
if _PYTHON3_REAL:
    (_PYTHON3_ISOLATED_DIR / "python3").symlink_to(_PYTHON3_REAL)
_NO_NX_PATH = f"{_PYTHON3_ISOLATED_DIR}:/usr/bin:/bin"


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_out(*args: str, cwd) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout.strip()


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.delenv("NX_REVIEW_GATE_OVERRIDE", raising=False)


@pytest.fixture
def repo(tmp_path):
    """A real origin + work clone on `develop`, upstream tracked, with a
    single initial commit already pushed -- so any FURTHER commits are
    exactly the outgoing range this hook checks.

    nexus-vscgz: the bare origin is deliberately named `nexus.git` (basename
    `nexus` once the `.git` suffix is stripped) so `_repo_scope_is_nexus`'s
    Signal A recognizes this fixture as nexus-scoped -- otherwise rule 3
    (this whole file's subject) now no-ops here by design.
    """
    origin = tmp_path / "nexus.git"
    origin.mkdir()
    _git("init", "-q", "--bare", "--initial-branch=develop", cwd=origin)

    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "--initial-branch=develop", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    (work / "README.md").write_text("init\n")
    _git("add", "README.md", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=work)
    _git("push", "-q", "-u", "origin", "develop", cwd=work)
    return work


def _commit(work, rel_path: str, message: str) -> str:
    full = work / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("x\n")
    _git("add", rel_path, cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message, cwd=work)
    return _git_out("rev-parse", "HEAD", cwd=work)


def _fake_nx(
    tmp_path,
    *,
    scratch: str = "No scratch entries.",
    scratch_rc: int = 0,
    scratch_unreachable: bool = False,
    memory_by_query: dict[str, str] | None = None,
    memory_default: str = "No results found.",
    memory_rc: int = 0,
    memory_unreachable: bool = False,
    memory_flaky_once: bool = False,
    sleep_seconds: float = 0.0,
    call_log: pathlib.Path | None = None,
) -> pathlib.Path:
    """A fake `nx` CLI (python, not bash -- avoids shell-quoting hazards for
    an embedded case table) covering BOTH subcommands the hook now calls:
    `nx scratch list` (T1) and `nx memory search <query>` (T2 fallback,
    nexus-4av2n round 2 Critical-1). `memory_by_query` maps a QUERY
    SUBSTRING to the raw stdout returned for that call (first match wins);
    anything unmatched gets `memory_default`. `*_unreachable=True` makes
    every call to that subcommand fail (nonzero exit), regardless of the
    configured text -- the CAPABILITY-gap path, distinct from a reachable
    call that legitimately finds nothing.

    `memory_flaky_once` (nexus-xtv8y): the FIRST `memory search` call for
    any given query fails, every later one succeeds — a transient, as
    distinct from `memory_unreachable`'s permanent failure. Exists because
    the coverage lookup now retries once before concluding it failed, and
    a retry nobody tests is a retry that can silently stop happening.

    `sleep_seconds` (nexus-4av2n round 3): makes EVERY invocation (scratch
    or memory) sleep before responding -- deterministically reproduces a
    slow-but-working `nx`, the shape the round-3 deadline fix targets,
    without depending on real subprocess-spawn latency (which varies by
    machine/load and is NOT what these tests should be asserting on).
    `call_log` (round 3): when given, every invocation appends one line
    (subcommand + query, if any) to this file -- lets tests assert on call
    COUNT deterministically instead of timing.
    """
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(exist_ok=True)
    script = fake_bin / "nx"
    cases = memory_by_query or {}
    log_line = (
        f"with open({str(call_log)!r}, 'a') as _f:\n"
        "    _f.write(' '.join(args) + chr(10))\n"
        if call_log is not None else ""
    )
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "args = sys.argv[1:]\n"
        f"{log_line}"
        f"time.sleep({sleep_seconds!r})\n"
        "if args[:2] == ['scratch', 'list']:\n"
        f"    if {scratch_unreachable!r}:\n"
        "        sys.exit(1)\n"
        f"    sys.stdout.write({scratch!r})\n"
        f"    sys.exit({scratch_rc!r})\n"
        "if args[:2] == ['memory', 'search']:\n"
        f"    if {memory_unreachable!r}:\n"
        "        sys.exit(1)\n"
        f"    if {memory_flaky_once!r}:\n"
        "        import os, hashlib\n"
        "        q = args[2] if len(args) > 2 else ''\n"
        "        stamp = os.path.join(\n"
        "            os.path.dirname(os.path.abspath(__file__)),\n"
        "            '.flaky-' + hashlib.sha1(q.encode()).hexdigest()[:12],\n"
        "        )\n"
        "        if not os.path.exists(stamp):\n"
        "            open(stamp, 'w').close()\n"
        "            sys.exit(1)\n"
        "    query = args[2] if len(args) > 2 else ''\n"
        f"    cases = {cases!r}\n"
        "    for k, v in cases.items():\n"
        "        if k in query:\n"
        "            sys.stdout.write(v)\n"
        f"            sys.exit({memory_rc!r})\n"
        f"    sys.stdout.write({memory_default!r})\n"
        f"    sys.exit({memory_rc!r})\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o755)
    return fake_bin


def _t1_marker(tags: str, content: str) -> str:
    return f"[abcd1234] {tags}  flagged=False\n  {content}\n"


def _t2_marker(project_title: str, content: str) -> str:
    return f"[1] {project_title}  (developer, 2026-08-06T00:00:00Z)\n  {content}\n"


def _run(cmd: str, cwd, *, path: str, env_extra: dict[str, str] | None = None):
    env = os.environ.copy()
    env["PATH"] = path
    if env_extra:
        env.update(env_extra)
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=30, env=env,
    )


def _decision(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


class TestExemptPushes:
    def test_docs_only_commit_is_exempt(self, repo, tmp_path):
        _commit(repo, "docs/x.md", "docs: add x")
        out = _decision(_run("git push", repo, path=_NO_NX_PATH))
        assert out["permissionDecision"] == "allow", out

    def test_tests_only_commit_is_gated(self, repo, tmp_path):
        """ROUND 2 (Significant-c, both reviewers): tests/** is no longer
        exempt -- a standalone test-quality regression is squarely the
        reviewers' own mandate."""
        _commit(repo, "tests/test_x.py", "test: add x (nexus-tst12)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch="No scratch entries.",
            memory_default="No results found.",
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out
        assert "nexus-tst12" in out["permissionDecisionReason"]

    def test_no_new_commits_is_a_noop(self, repo):
        out = _decision(_run("git push", repo, path=_NO_NX_PATH))
        assert out["permissionDecision"] == "allow", out

    def test_non_push_git_commands_are_untouched(self, repo, tmp_path):
        _commit(repo, "src/x.py", "feat: x (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)
        for cmd in ("git status", "git log --oneline -3", "git diff"):
            out = _decision(_run(cmd, repo, path=f"{fake_bin}:/usr/bin:/bin"))
            assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


class TestUncoveredPushIsBlocked:
    """Genuinely missing == unreachable-neither-source-covers-it after BOTH
    T1 and T2 are consulted and reachable."""

    def test_gated_commit_with_no_marker_anywhere_denies(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)  # T1 empty, T2 "No results found." -- both reachable
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out
        assert "nexus-abc12" in out["permissionDecisionReason"]
        assert "nexus-4av2n" in out["permissionDecisionReason"]

    def test_service_source_path_is_gated(self, repo, tmp_path):
        _commit(repo, "service/src/main/java/Foo.java", "feat: java foo (nexus-jav12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out

    def test_conexus_plugin_path_is_gated(self, repo, tmp_path):
        _commit(repo, "conexus/hooks/scripts/x.sh", "feat: hook x (nexus-hok12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out

    def test_uncovered_commit_names_sha_and_subject_and_paths(self, repo, tmp_path):
        sha = _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        reason = out["permissionDecisionReason"]
        assert sha[:12] in reason
        assert "add foo" in reason
        assert "src/nexus/foo.py" in reason
        assert "NX_REVIEW_GATE_OVERRIDE" in reason

    def test_marker_for_unrelated_bead_does_not_cover_in_either_source(self, repo, tmp_path):
        """Entry-anchored match: an unrelated bead's marker in EITHER T1 or
        T2 must not satisfy this push's coverage check."""
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch=_t1_marker("review-completed,nexus-other", "review-completed: nexus-other"),
            memory_by_query={"nexus-other": _t2_marker("nexus/x", "review-completed: nexus-other")},
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out


class TestDualSourceCoverage:
    """nexus-4av2n round 2 Critical-1: a marker visible ONLY in T2 (the
    MCP-scratch-write / stale-CLI-lease shape the critic reproduced live)
    must still satisfy coverage -- T1 alone is no longer authoritative."""

    def test_t1_covers_without_touching_t2(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch=_t1_marker("review-completed,nexus-abc12", "review-completed: nexus-abc12"),
            memory_unreachable=True,  # if T2 were consulted, this would fail loud
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out

    def test_t2_covers_when_t1_reachable_but_empty(self, repo, tmp_path):
        """THE regression this round fixes: T1 reachable-and-empty (the
        scope-mismatch shape) must fall back to T2 rather than denying
        outright."""
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch="No scratch entries.",
            memory_by_query={
                "nexus-abc12": _t2_marker("nexus/review-nexus-abc12", "review-completed: nexus-abc12 clean")
            },
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out

    def test_t2_covers_when_t1_totally_unreachable(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch_unreachable=True,
            memory_by_query={
                "nexus-abc12": _t2_marker("nexus/review-nexus-abc12", "review-completed: nexus-abc12 clean")
            },
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out

    def test_t2_range_marker_covers_when_t1_range_absent(self, repo, tmp_path):
        _commit(repo, "src/a.py", "feat: a (nexus-aaa11)")
        tip = _commit(repo, "src/b.py", "feat: b (nexus-bbb22)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch="No scratch entries.",
            memory_by_query={tip: _t2_marker("nexus/range", f"review-completed: range {tip}")},
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out

    def test_both_sources_reachable_and_empty_still_denies(self, repo, tmp_path):
        """Dual-source widens WHERE coverage can be found; it must not
        weaken WHETHER genuine absence still denies."""
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)  # both reachable, both empty by default
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out


class TestCoveredPushIsAllowed:
    def test_per_bead_marker_covers_matching_commit(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch=_t1_marker("review-completed,nexus-abc12", "review-completed: nexus-abc12 clean"),
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out

    def test_range_marker_covers_the_whole_push(self, repo, tmp_path):
        _commit(repo, "src/a.py", "feat: a (nexus-aaa11)")
        tip = _commit(repo, "src/b.py", "feat: b (nexus-bbb22)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch=_t1_marker("review-completed", f"review-completed: range {tip} covers everything"),
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out

    def test_mixed_range_one_covered_one_not_still_denies(self, repo, tmp_path):
        _commit(repo, "src/a.py", "feat: a (nexus-aaa11)")
        _commit(repo, "src/b.py", "feat: b (nexus-bbb22)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch=_t1_marker("review-completed,nexus-aaa11", "review-completed: nexus-aaa11 only"),
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out
        assert "nexus-bbb22" in out["permissionDecisionReason"]


class TestOverride:
    def test_env_override_downgrades_deny_to_loud_allow(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run(
            "git push", repo, path=f"{fake_bin}:/usr/bin:/bin",
            env_extra={"NX_REVIEW_GATE_OVERRIDE": "1"},
        ))
        assert out["permissionDecision"] == "allow", out
        assert "OVERRIDE" in out.get("additionalContext", "")

    def test_routing_allow_escape_token_also_works(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run(
            "git push  # routing-allow: reviewed out of band, ticket 123",
            repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "allow", out


class TestCapabilityGapBothSourcesDown:
    """A gate that cannot check must not degrade to permitting (nexus-xtv8y).

    These two cases used to ALLOW with a warning, on the rationale that a
    broken verification path must not brick every push. On 2026-08-21
    that rationale did what it ignores: the lookup failed, the guard
    warned, and a genuinely unreviewed commit went to origin — the one
    time the check mattered. The push is still not bricked; it costs one
    deliberate, audited env var, which is the difference between an
    escape hatch and a hole.

    The lookup retries once before concluding it failed, so a transient
    does not cost a push either.
    """

    def test_both_unreachable_denies(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path, scratch_unreachable=True, memory_unreachable=True)
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out
        ctx = out.get("permissionDecisionReason", "") + out.get("additionalContext", "")
        assert "NX_REVIEW_GATE_OVERRIDE=1" in ctx, (
            "a deny must name its own unbrick path"
        )

    def test_both_unreachable_still_yields_to_the_audited_override(self, repo, tmp_path):
        """The escape hatch is what makes failing closed affordable."""
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path, scratch_unreachable=True, memory_unreachable=True)
        out = _decision(_run(
            "NX_REVIEW_GATE_OVERRIDE=1 git push",
            repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "allow", out

    def test_nx_missing_entirely_denies(self, repo):
        """No nx means no way to VERIFY and no way to WRITE a marker. That
        is a broken environment in a nexus checkout, not a licence."""
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        out = _decision(_run("git push", repo, path=_NO_NX_PATH))
        assert out["permissionDecision"] == "deny", out


class TestTagPushExemption:
    """nexus-4av2n round 2 Critical-3a: a pure tag push must never be
    denied by the review-coverage check, even with unpushed, uncovered,
    gated local commits present -- the exact GH #1402 failure class."""

    @pytest.mark.parametrize("cmd", [
        "git push origin v1.2.3",
        "git push origin engine-service-v0.1.56",
        "git push --tags",
        "git push origin refs/tags/v1.2.3",
    ])
    def test_pure_tag_push_with_uncovered_local_commits_is_allowed(self, cmd, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")  # unpushed, uncovered
        fake_bin = _fake_nx(tmp_path)  # both sources empty -- would deny a branch push
        out = _decision(_run(cmd, repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", f"{cmd}: {out}"

    def test_follow_tags_still_checks_the_branch_it_also_pushes(self, repo, tmp_path):
        """--follow-tags pushes the branch too (see the sibling push-to-main
        check's own docstring) -- it must NOT be treated as tag-exempt."""
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run("git push --follow-tags", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out


class TestReleaseBranchExemption:
    """nexus-4av2n round 2 Critical-2: AGENTS.md's own release Step 7
    first-pushes a release/vX.Y.Z branch whose sole commit touches conexus/
    (gated) and carries no bead id. Must not deny."""

    def test_first_push_of_release_branch_is_exempt_with_loud_info(self, repo, tmp_path):
        _git("checkout", "-q", "-b", "release/v9.9.9", cwd=repo)
        _commit(repo, "conexus/PENDING_RELEASE.md", "chore(release): conexus 9.9.9")
        fake_bin = _fake_nx(tmp_path)  # would deny a normal branch push
        out = _decision(_run(
            "git push -u origin release/v9.9.9", repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "allow", out
        assert "release" in out.get("additionalContext", "").lower()

    def test_mixed_compound_push_still_checks_the_non_release_segment(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        _git("checkout", "-q", "-b", "release/v9.9.9", cwd=repo)
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run(
            "git push origin develop && git push -u origin release/v9.9.9",
            repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "deny", out


class TestMergeCommits:
    """nexus-4av2n round 2 Critical-3b: bare `diff-tree` returns nothing
    for a merge commit by git's own default -- every merge (including
    AGENTS.md's mandated back-merge) was silently ungated regardless of
    content. Fixed by diffing against the first parent."""

    def test_merge_commit_bringing_in_gated_content_is_caught(self, repo, tmp_path):
        _git("checkout", "-q", "-b", "feature/x", cwd=repo)
        _commit(repo, "src/feature.py", "feat: feature work (nexus-mrg12)")
        _git("checkout", "-q", "develop", cwd=repo)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-ff",
             "feature/x", "-m", "Merge feature/x into develop", cwd=repo)
        fake_bin = _fake_nx(tmp_path)  # both sources empty
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out
        assert "src/feature.py" in out["permissionDecisionReason"]

    def test_merge_commit_covered_by_range_marker(self, repo, tmp_path):
        """A merge commit's OWN message rarely carries a bead id (git's
        default merge message names the branch, not a bead) -- its
        intended coverage path is the per-range marker naming the tip sha,
        exactly like any other bead-id-less gated commit."""
        _git("checkout", "-q", "-b", "feature/x", cwd=repo)
        _commit(repo, "src/feature.py", "feat: feature work (nexus-mrg12)")
        _git("checkout", "-q", "develop", cwd=repo)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-ff",
             "feature/x", "-m", "Merge feature/x into develop", cwd=repo)
        tip = _git_out("rev-parse", "HEAD", cwd=repo)
        fake_bin = _fake_nx(
            tmp_path,
            scratch=_t1_marker("review-completed", f"review-completed: range {tip}"),
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out


class TestTruncationIsLoud:
    """nexus-4av2n round 2 Significant-a: exceeding the commit-scan cap
    must never silently read as 'ok' -- test-seam env var
    NX_PUSH_GATE_MAX_COMMITS_SCANNED lets this run fast without creating
    50+ real commits."""

    def test_truncated_range_with_full_coverage_of_scanned_subset_still_warns(self, repo, tmp_path):
        _commit(repo, "src/a.py", "feat: a (nexus-aaa11)")
        _commit(repo, "src/b.py", "feat: b (nexus-bbb22)")
        _commit(repo, "src/c.py", "feat: c (nexus-ccc33)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch=_t1_marker("review-completed", "review-completed: range covers all somehow"),
        )
        out = _decision(_run(
            "git push", repo, path=f"{fake_bin}:/usr/bin:/bin",
            env_extra={"NX_PUSH_GATE_MAX_COMMITS_SCANNED": "2"},
        ))
        # Range marker text above doesn't name the real tip sha, so it will
        # NOT satisfy range coverage -- the point here is purely: truncation
        # must be visible in the output regardless of decision.
        ctx = out.get("additionalContext", "") or out.get("permissionDecisionReason", "")
        assert "3 commits" in ctx or "only the 2" in ctx.lower() or "not checked" in ctx.lower(), out


class TestDeadlineBudget:
    """nexus-4av2n round 3 (Critical, substantive-critic closure
    verification): the round-2 call-COUNT budget did not bound wall-clock
    time -- the real 2026-07-31 incident shape (5-8 uncovered gated
    commits) measured 6.49s-6.91s live, exceeding the hook's own 5s
    PreToolUse timeout. Replaced by a wall-clock ``_Deadline``
    (NX_PUSH_GATE_DEADLINE_SECONDS test seam here) that the hook enforces
    on itself, denying deterministically rather than ever risking a
    harness kill. These tests use a SLEEP-STUBBED `nx` so the deadline trip
    is deterministic and fast, never dependent on real subprocess-spawn
    latency."""

    def test_deadline_exceeded_denies_deterministically(self, repo, tmp_path):
        """(a) deadline-trip deny path via a stubbed slow `nx`."""
        ids = [f"nexus-bud{i:02d}" for i in range(5)]
        for i, bid in enumerate(ids):
            _commit(repo, f"src/f{i}.py", f"feat: f{i} ({bid})")
        # Slow-but-working nx: every call sleeps 0.35s. With a 0.5s
        # deadline, T1 (0.35s) + the range-tip T2 check (another 0.35s,
        # elapsed ~0.70s > 0.5s deadline) exhausts the budget before ANY
        # per-bead T2 lookup can even be attempted -- deterministic trip.
        fake_bin = _fake_nx(tmp_path, scratch="No scratch entries.", sleep_seconds=0.35)
        out = _decision(_run(
            "git push", repo, path=f"{fake_bin}:/usr/bin:/bin",
            env_extra={"NX_PUSH_GATE_DEADLINE_SECONDS": "0.5"},
        ))
        assert out["permissionDecision"] == "deny", out
        reason = out["permissionDecisionReason"]
        assert "0.5" in reason or "wall-clock" in reason.lower() or "deadline" in reason.lower(), reason
        # The remedy leads with the range-marker command (fastest fix for
        # a multi-commit push), per the round-3 ask.
        assert "nx memory put" in reason, reason
        assert reason.index("range") < reason.index("nx memory put")

    def test_deadline_exceeded_names_the_override_remedy(self, repo, tmp_path):
        _commit(repo, "src/f0.py", "feat: f0 (nexus-bud00)")
        fake_bin = _fake_nx(tmp_path, scratch="No scratch entries.", sleep_seconds=0.35)
        out = _decision(_run(
            "git push", repo, path=f"{fake_bin}:/usr/bin:/bin",
            env_extra={"NX_PUSH_GATE_DEADLINE_SECONDS": "0.1"},
        ))
        assert out["permissionDecision"] == "deny", out
        assert "NX_REVIEW_GATE_OVERRIDE" in out["permissionDecisionReason"]

    def test_deadline_override_downgrades_to_loud_allow(self, repo, tmp_path):
        _commit(repo, "src/f0.py", "feat: f0 (nexus-bud00)")
        fake_bin = _fake_nx(tmp_path, scratch="No scratch entries.", sleep_seconds=0.35)
        out = _decision(_run(
            "git push", repo, path=f"{fake_bin}:/usr/bin:/bin",
            env_extra={
                "NX_PUSH_GATE_DEADLINE_SECONDS": "0.1",
                "NX_REVIEW_GATE_OVERRIDE": "1",
            },
        ))
        assert out["permissionDecision"] == "allow", out
        assert "OVERRIDE" in out.get("additionalContext", "")

    def test_fast_path_single_call_when_t1_covers(self, repo, tmp_path):
        """(b) wall-clock ceiling test on the fast path, via call-COUNTING
        with the stub, not timing (deterministic). Verifies the round-3
        reorder fix: when T1 alone covers everything, the hook must not
        spend a second `nx` call on the range-tip T2 lookup (round-2
        regression -- it ran unconditionally before the per-commit pass)."""
        _commit(repo, "src/f0.py", "feat: f0 (nexus-cov00)")
        call_log = tmp_path / "calls.log"
        fake_bin = _fake_nx(
            tmp_path,
            scratch=_t1_marker("review-completed,nexus-cov00", "review-completed: nexus-cov00"),
            call_log=call_log,
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out
        # A silent "ok" carries no additionalContext; a "warn" (e.g. a
        # capability gap) is ALSO permissionDecision=="allow" at the JSON
        # level, so this must be checked too -- otherwise a broken fake nx
        # crashing on every call would still pass the bare allow assertion.
        assert not out.get("additionalContext"), out
        calls = call_log.read_text().splitlines() if call_log.exists() else []
        assert len(calls) == 1, calls
        assert calls[0].startswith("scratch"), calls

    def test_the_2026_07_31_shape_resolves_within_deadline_as_genuine_absence(
        self, repo, tmp_path
    ):
        """(c) the 07-31 incident shape (5 uncovered gated commits, no
        coverage anywhere) with a REALISTICALLY FAST (not sleep-stubbed)
        nx must resolve WITHIN the default deadline and deny as a genuine
        absence -- not trip the deadline path. Asserts deterministically
        which of the two deny flavors fires, per the round-3 test ask."""
        ids = [f"nexus-bud{i:02d}" for i in range(5)]
        for i, bid in enumerate(ids):
            _commit(repo, f"src/f{i}.py", f"feat: f{i} ({bid})")
        fake_bin = _fake_nx(tmp_path, scratch="No scratch entries.")  # no sleep -- fast
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out
        reason = out["permissionDecisionReason"]
        for bid in ids:
            assert bid in reason, reason
        # Genuine-absence flavor, NOT the deadline flavor.
        assert "wall-clock budget" not in reason
        assert "VERIFIED within the hook's" not in reason


class TestNoUpstreamFirstPush:
    def test_first_push_of_a_new_branch_falls_back_to_merge_base(self, repo, tmp_path):
        _git("checkout", "-q", "-b", "feature/new-thing", cwd=repo)
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run(
            "git push -u origin feature/new-thing", repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "deny", out
        assert "nexus-abc12" in out["permissionDecisionReason"]

    def test_fails_open_with_no_remote_history_at_all(self, tmp_path):
        work = tmp_path / "isolated"
        work.mkdir()
        _git("init", "-q", "--initial-branch=develop", cwd=work)
        (work / "src").mkdir()
        (work / "src" / "x.py").write_text("x\n")
        _git("add", "src/x.py", cwd=work)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
             "-m", "feat: x (nexus-iso123)", cwd=work)
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run(
            "git push origin develop", work, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] in ("allow", "deny"), out


class TestMalformedAndFastPaths:
    def test_empty_stdin_allows(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input="", capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_non_bash_tool_allows(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "x"}}),
            capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_non_push_command_allows(self, repo):
        out = _decision(_run("git status", repo, path=_NO_NX_PATH))
        assert out["permissionDecision"] == "allow", out


@pytest.fixture
def foreign_repo(tmp_path):
    """Like `repo`, but deliberately NOT nexus-scoped (nexus-vscgz): the
    bare origin's basename is `otherproject`, not `nexus`, and no conexus
    plugin marker exists anywhere under tmp_path."""
    origin = tmp_path / "otherproject.git"
    origin.mkdir()
    _git("init", "-q", "--bare", "--initial-branch=develop", cwd=origin)

    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "--initial-branch=develop", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    (work / "README.md").write_text("init\n")
    _git("add", "README.md", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=work)
    _git("push", "-q", "-u", "origin", "develop", cwd=work)
    return work


class TestRepoScope:
    """nexus-vscgz: rule 3 is nexus-repo policy, not a global standing rule
    -- a foreign repo pushing a gated-path commit with zero coverage
    anywhere must be ALLOWED, which today (pre-fix) would deny."""

    def test_foreign_repo_gated_commit_with_no_marker_is_allowed(self, foreign_repo, tmp_path):
        _commit(foreign_repo, "src/x.py", "feat: x, no bead id, no marker anywhere")
        fake_bin = _fake_nx(tmp_path)  # would deny in a nexus-scoped repo
        out = _decision(_run("git push", foreign_repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out

    def test_foreign_repo_gated_commit_with_no_nx_on_path_is_allowed(self, foreign_repo):
        """Confirms the ALLOW is because the repo is out of scope, not
        because the unreachable-nx capability-gap warn path happened to
        also allow -- no `nx` on PATH at all, and still a plain allow with
        no coverage-gap warning."""
        _commit(foreign_repo, "src/x.py", "feat: x, no bead id, no marker anywhere")
        out = _decision(_run("git push", foreign_repo, path=_NO_NX_PATH))
        assert out["permissionDecision"] == "allow", out
        assert not out.get("additionalContext"), out

    def test_trailing_slash_dot_git_origin_is_still_nexus_scoped(self, tmp_path):
        """Ported from the deleted rule-2 suite (nexus-ww9fw split):
        normalization-order regression, previously reproduced live --
        ``.../nexus.git/`` (trailing slash AFTER ``.git``) must still
        resolve its last path component to ``nexus``. The first cut
        stripped ``.git`` before the slash, so the suffix survived and a
        genuine nexus remote read as foreign -- which post-split would
        silently switch rule 3 OFF. Asserted here via rule 3: a gated-path
        commit with no coverage must DENY, proving the repo resolved
        in-scope."""
        origin = tmp_path / "nexus.git"
        origin.mkdir()
        _git("init", "-q", "--bare", "--initial-branch=develop", cwd=origin)
        work = tmp_path / "work-trailing-slash"
        work.mkdir()
        _git("init", "-q", "--initial-branch=develop", cwd=work)
        _git("remote", "add", "origin", str(origin) + "/", cwd=work)
        (work / "README.md").write_text("init\n")
        _git("add", "README.md", cwd=work)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=work)
        _git("push", "-q", "-u", "origin", "develop", cwd=work)
        _commit(work, "src/x.py", "feat: x, no bead id, no marker anywhere")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run("git push", work, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out

    def test_renamed_origin_with_plugin_marker_still_in_scope(self, tmp_path):
        """Ported from the deleted rule-2 suite (nexus-ww9fw split):
        nexus-w3apo -- Signal A resolving DETERMINATELY to a non-``nexus``
        basename must still consult the marker (Signal B). A nexus
        checkout behind a renamed/local origin (scratch clone, mirror) is
        exactly the shape that silently lost its guard when Signal B was
        fallback-only. Asserted via rule 3: gated-path commit, no
        coverage, marker present -> DENY."""
        origin = tmp_path / "nexus-mirror"
        origin.mkdir()
        _git("init", "-q", "--bare", "--initial-branch=develop", cwd=origin)
        work = tmp_path / "work-renamed"
        work.mkdir()
        _git("init", "-q", "--initial-branch=develop", cwd=work)
        _git("remote", "add", "origin", str(origin), cwd=work)
        marker_dir = work / "conexus" / ".claude-plugin"
        marker_dir.mkdir(parents=True)
        (marker_dir / "plugin.json").write_text("{}")
        _git("add", "conexus/.claude-plugin/plugin.json", cwd=work)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=work)
        _git("push", "-q", "-u", "origin", "develop", cwd=work)
        _commit(work, "src/x.py", "feat: x, no bead id, no marker anywhere")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run("git push", work, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out


class TestF1RedirectionStripping:
    """nexus-cr4lp F1 (T2 nexus/guard-evidence-cluster-root-cause-2026-08-
    18, LEG A): shell redirection tokens (``2>&1``, ``>``, etc.) must not
    be read as phantom refspecs. Pre-fix, ``git push -u origin
    release/v7.9.0 2>&1`` read the destination list as
    ``['release/v7.9.0', '2>&1']``, and the release/* exemption's
    ``all(d.startswith('release/'))`` failed on the bogus second entry --
    denying a legitimate, PR-gated release-branch push."""

    def test_release_branch_push_with_trailing_stderr_redirect_stays_exempt(
        self, repo, tmp_path
    ):
        _git("checkout", "-q", "-b", "release/v9.9.9", cwd=repo)
        _commit(repo, "conexus/PENDING_RELEASE.md", "chore(release): conexus 9.9.9")
        fake_bin = _fake_nx(tmp_path)  # would deny a normal branch push
        out = _decision(_run(
            "git push -u origin release/v9.9.9 2>&1",
            repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "allow", out
        assert "release" in out.get("additionalContext", "").lower()


class TestF2EnvPrefixOverride:
    """nexus-cr4lp F2: an inline ``NX_REVIEW_GATE_OVERRIDE=1`` prefix on
    the push command itself must be PARSED (the push segment recognized,
    rules apply) and HONORED as an override -- not a silent, unaudited
    no-op via a parser miss (B2: pre-fix, ``_push_tokens`` required
    ``tokens[0] == "git"``, so the env-prefixed form was invisible and the
    whole coverage check silently never ran)."""

    def test_inline_env_prefixed_push_is_recognized_and_overridden(
        self, repo, tmp_path
    ):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)  # both sources empty -- would deny
        out = _decision(_run(
            "NX_REVIEW_GATE_OVERRIDE=1 git push", repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "allow", out
        assert "OVERRIDE" in out.get("additionalContext", ""), out

    def test_inline_override_emits_an_escape_routing_event(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run(
            "NX_REVIEW_GATE_OVERRIDE=1 git push", repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "allow", out
        log = tmp_path / "log.jsonl"
        events = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        assert any(e["outcome"] == "escape" for e in events), events

    def test_ambient_env_override_also_emits_an_escape_routing_event(self, repo, tmp_path):
        """Every override -- inline OR ambient env -- is audited the same
        way; this is the non-regression counterpart to the inline test
        above."""
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run(
            "git push", repo, path=f"{fake_bin}:/usr/bin:/bin",
            env_extra={"NX_REVIEW_GATE_OVERRIDE": "1"},
        ))
        assert out["permissionDecision"] == "allow", out
        log = tmp_path / "log.jsonl"
        events = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        assert any(e["outcome"] == "escape" for e in events), events


class TestF4RemedySeparateCallWarning:
    """nexus-cr4lp F4: the shared root cause of every report in the
    guard-evidence cluster is a PreToolUse deny aborting the WHOLE Bash
    call, remedy bundled ahead of the gated command included. Every deny
    message's Remedy block must lead with that warning."""

    def test_uncovered_deny_remedy_opens_with_separate_call_warning(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path)
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out
        reason = out["permissionDecisionReason"]
        assert "SEPARATE tool call" in reason, reason
        assert reason.index("Remedy") < reason.index("SEPARATE tool call")

    def test_deadline_scan_remedy_opens_with_separate_call_warning(self, repo, tmp_path):
        ids = [f"nexus-bud{i:02d}" for i in range(5)]
        for i, bid in enumerate(ids):
            _commit(repo, f"src/f{i}.py", f"feat: f{i} ({bid})")
        fake_bin = _fake_nx(tmp_path, scratch="No scratch entries.", sleep_seconds=0.35)
        out = _decision(_run(
            "git push", repo, path=f"{fake_bin}:/usr/bin:/bin",
            env_extra={"NX_PUSH_GATE_DEADLINE_SECONDS": "0.5"},
        ))
        assert out["permissionDecision"] == "deny", out
        reason = out["permissionDecisionReason"]
        assert "SEPARATE tool call" in reason, reason
        assert reason.index("Remedy") < reason.index("SEPARATE tool call")


class TestB3T2TitleOnlyMarker:
    """nexus-cr4lp B3 (latent, found during guard-evidence-cluster
    forensics): a T2 marker whose bead id lives ONLY in the TITLE (this
    hook's OWN printed ``-t review-<bead-id>`` form) must satisfy
    coverage even when the CONTENT carries no bare bead id."""

    def test_t2_marker_covers_when_bead_id_is_only_in_the_title(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-b3ttl)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch="No scratch entries.",
            memory_by_query={
                "nexus-b3ttl": _t2_marker(
                    "nexus/review-nexus-b3ttl",
                    "review-completed: clean, no bead id restated here",
                )
            },
        )
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out


class TestF5RemedyRoundTripReal:
    """nexus-cr4lp F5: each remedy string the hook PRINTS, executed via
    the REAL ``nx`` CLI (THIS checkout's dev build -- never the live
    production install, see
    feedback_check_install_mode_before_diagnosing.md) as its OWN
    subprocess call, must actually satisfy the hook's own T1/T2 lookup
    afterward. Uses the real engine-backed T2/T1 substrate
    (``t2_service_env``, tests/_engine_substrate.py) -- skips cleanly if
    the dev ``nx`` console script cannot be found next to
    ``sys.executable`` (e.g. a non-uv invocation)."""

    @staticmethod
    def _real_nx_dir() -> pathlib.Path | None:
        d = pathlib.Path(sys.executable).parent
        return d if (d / "nx").exists() else None

    @pytest.mark.parametrize("remedy_kind", ["t1_range_marker", "t2_bead_marker"])
    def test_printed_remedy_satisfies_the_hooks_own_lookup(
        self, remedy_kind, repo, t2_service_env
    ):
        real_nx_dir = self._real_nx_dir()
        if real_nx_dir is None:
            pytest.skip("dev checkout `nx` console script not found next to sys.executable")
        real_nx = str(real_nx_dir / "nx")

        bead_id = f"nexus-r5{'a' if remedy_kind == 't1_range_marker' else 'b'}01"
        sha = _commit(repo, "src/nexus/f5.py", f"feat: f5 remedy round-trip ({bead_id})")

        session_id = f"cr4lp-f5-{remedy_kind}"
        write_env = os.environ.copy()
        write_env["NX_SESSION_ID"] = session_id
        write_env["NX_T1_ALLOW_SHARED_FALLBACK"] = "1"

        if remedy_kind == "t1_range_marker":
            write_cmd = [
                real_nx, "scratch", "put", f"review-completed: range {sha}",
                "--tags", "review-completed",
            ]
        else:
            write_cmd = [
                real_nx, "memory", "put", f"review-completed: {bead_id}",
                "-p", "nexus-cr4lp-f5-test", "-t", f"review-{bead_id}",
            ]

        # The remedy write is ITS OWN subprocess call -- never bundled
        # with the gated command. That bundling is the shared root cause
        # every report in the guard-evidence cluster traces to (T2
        # nexus/guard-evidence-cluster-root-cause-2026-08-18).
        wproc = subprocess.run(
            write_cmd, cwd=repo, env=write_env, capture_output=True, text=True, timeout=60,
        )
        assert wproc.returncode == 0, wproc.stderr

        out = _decision(_run(
            "git push", repo, path=f"{real_nx_dir}:/usr/bin:/bin",
            env_extra={
                "NX_SESSION_ID": session_id,
                "NX_T1_ALLOW_SHARED_FALLBACK": "1",
            },
        ))
        assert out["permissionDecision"] == "allow", out


class TestRegistryStillAtCap:
    def test_no_new_routing_registry_entry_added(self):
        """This check must NOT acquire its own registry.yaml rule -- that
        would be the 5th cross-plugin routing rule, and RDR-121/125 cap it
        at 4 (see test_routing_registry_aggregate_cap.py)."""
        registry_path = (
            PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml"
        )
        registry = registry_path.read_text()
        assert "push_review_coverage" not in registry, (
            "a standalone registry entry for the push-review-coverage check "
            "was added -- this must stay consolidated into "
            "git_add_all_redirects_to_explicit_paths to respect the cap"
        )
        assert "nexus-4av2n" in registry, (
            "the registry rationale does not record the push-review-coverage "
            "check; a future reader would not know this hook enforces it"
        )


class TestWiring:
    """Moved from the now-deleted tests/test_routing_git_add_all.py
    (nexus-ww9fw, 2026-08-18): these generic hook-wiring checks are not
    about the wildcard-add rule that used to share this script -- they
    just confirm the SCRIPT (still named
    `git_add_all_redirects_to_explicit_paths.py` for registry/hooks.json
    compatibility, per the module docstring's HISTORY note) is registered
    and wired. They belong with this file now, since this is the sole
    surviving check the script enforces."""

    def test_registry_has_rule(self):
        yaml = pytest.importorskip("yaml")
        reg = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml"
        rule = (yaml.safe_load(reg.read_text()) or {}).get("rules", {}).get(
            "git_add_all_redirects_to_explicit_paths"
        )
        assert rule is not None

    def test_hooks_json_registers(self):
        hooks_json = PROJECT_ROOT / "conexus" / "hooks" / "hooks.json"
        data = json.loads(hooks_json.read_text())
        found = any(
            "git_add_all_redirects_to_explicit_paths.py" in h.get("command", "")
            for entry in data["hooks"]["PreToolUse"] if entry.get("matcher") == "Bash"
            for h in entry.get("hooks", [])
        )
        assert found

    def test_registry_no_longer_claims_wildcard_add_or_push_to_main_as_live(self):
        """nexus-ww9fw: the registry rationale must not describe rules 1/2
        as CURRENT plugin behavior -- only as history."""
        reg = PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml"
        registry = reg.read_text()
        assert "HISTORY" in registry
        assert "nexus-vduer" in registry, (
            "incident archaeology should still be traceable from the registry"
        )


class TestMalformedQuotingNeverBypasses:
    """nexus-2e874: an unbalanced quote anywhere in the command used to make
    shlex reject the segment inside `_push_tokens`, which silently DROPPED
    it -- `push_segments` came back empty and the whole hook fast-no-op'd
    with a bare allow, a full silent bypass of the review-coverage gate.
    The degraded quote-blanked whitespace fallback keeps the `git push`
    anchor visible."""

    def test_unbalanced_quote_push_with_uncovered_commit_is_still_denied(
        self, repo, tmp_path
    ):
        _commit(repo, "src/nexus/x.py", "feat: x (nexus-tst34)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch="No scratch entries.",
            memory_default="No results found.",
        )
        out = _decision(_run(
            'git push origin develop --receive-pack="unterminated',
            repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "deny", out
        assert "nexus-tst34" in out["permissionDecisionReason"]

    def test_unbalanced_quote_with_no_outgoing_gated_commit_stays_allowed(
        self, repo, tmp_path
    ):
        """The degraded parse must not over-deny: same malformed quoting,
        docs-only outgoing range -- still exempt."""
        _commit(repo, "docs/x.md", "docs: x")
        out = _decision(_run(
            'git push origin develop --receive-pack="unterminated',
            repo, path=_NO_NX_PATH,
        ))
        assert out["permissionDecision"] == "allow", out

    def test_quote_inside_the_verb_is_still_gated(self, repo, tmp_path):
        """Review Important-1 (nexus-2e874): quote INSIDE the git verb --
        the quote-removed degraded variant must still see the push."""
        _commit(repo, "src/nexus/y.py", "feat: y (nexus-tst56)")
        fake_bin = _fake_nx(
            tmp_path,
            scratch="No scratch entries.",
            memory_default="No results found.",
        )
        out = _decision(_run(
            'gi"t push origin develop',
            repo, path=f"{fake_bin}:/usr/bin:/bin",
        ))
        assert out["permissionDecision"] == "deny", out


class TestUnverifiableCoverageFailsClosed:
    """nexus-xtv8y: the incident this fix exists for, pinned end to end."""

    def test_the_2026_08_21_shape_is_now_denied(self, repo, tmp_path):
        """One session pushed develop and carried a sibling's unreviewed
        commit. The T2 lookup failed, the guard warned, and the commit
        reached origin unreviewed."""
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path, scratch_unreachable=True, memory_unreachable=True)
        out = _decision(_run("git push origin develop", repo,
                             path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out

    def test_a_deny_names_the_blocking_commits_author(self, repo, tmp_path):
        """On a shared checkout the commit blocking your push is often not
        yours — `git push` sends the branch, not your commits. Naming the
        author turns "why am I blocked" into "coordinate with them"
        rather than into a reflex override."""
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path, scratch_unreachable=False, memory_unreachable=False)
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "deny", out
        ctx = out.get("permissionDecisionReason", "") + out.get("additionalContext", "")
        assert "author:" in ctx, (
            f"deny message does not attribute the blocking commit: {ctx}"
        )

    def test_a_transient_lookup_failure_does_not_cost_a_push(self, repo, tmp_path):
        """The lookup retries once, so a single flake is absorbed. Without
        this, failing closed would turn every blip into a blocked push —
        the objection the old fail-open was answering."""
        fake_bin = _fake_nx(
            tmp_path,
            memory_flaky_once=True,
            memory_by_query={
                "nexus-abc12": _t2_marker(
                    "nexus/review-nexus-abc12",
                    "review-completed: nexus-abc12",
                ),
            },
        )
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out
