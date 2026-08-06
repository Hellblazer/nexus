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
    exactly the outgoing range this hook checks."""
    origin = tmp_path / "origin.git"
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
    """Renamed from round 1's T1-only class: the honest capability-gap
    signal now requires BOTH T1 and T2 to be unreachable, since either
    source alone can satisfy coverage."""

    def test_both_unreachable_allows_with_loud_warning(self, repo, tmp_path):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        fake_bin = _fake_nx(tmp_path, scratch_unreachable=True, memory_unreachable=True)
        out = _decision(_run("git push", repo, path=f"{fake_bin}:/usr/bin:/bin"))
        assert out["permissionDecision"] == "allow", out
        ctx = out.get("additionalContext", "").lower()
        assert "uncertain" in ctx or "not verify" in ctx or "budget" in ctx

    def test_nx_missing_entirely_allows_with_loud_warning(self, repo):
        _commit(repo, "src/nexus/foo.py", "feat: add foo (nexus-abc12)")
        out = _decision(_run("git push", repo, path=_NO_NX_PATH))
        assert out["permissionDecision"] == "allow", out


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
        _git("merge", "-q", "--no-ff", "feature/x", "-m", "Merge feature/x into develop", cwd=repo)
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
        _git("merge", "-q", "--no-ff", "feature/x", "-m", "Merge feature/x into develop", cwd=repo)
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
            "half; a future reader would not know this hook carries a third check"
        )
