# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``git push`` to main is blocked outside the release flow (nexus-vduer).

THE INCIDENT (2026-07-23, self-reported). During the P4b Phase 0c commit the
orchestrator pushed directly to main. Session restarts had left the working
tree on main — Hal's reinstall checkout — and verify-branch-before-commit was a
MEMORY-ONLY control, so it failed the way memory-only controls fail. No damage
(parity restored by fast-forwarding develop to the same commit), but the
RDR-184 escalation contract says mechanize, not write a retro note.

THE LOAD-BEARING DETAIL, and what most of these tests exist for: the incident
did not involve anyone typing "main". The checkout was ALREADY on main, so a
bare ``git push`` inherited the target from the branch's upstream. A matcher
that looked for the literal token would have missed the exact event it exists
to prevent. So the tests drive real git repos — a real checkout on main with a
real upstream — rather than asserting on strings.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
# CONSOLIDATED into the git-hygiene hook (Hal decision 2026-07-25): RDR-121/125
# cap routing rules at FOUR cross-plugin, and this would have been the fifth.
# The cap's own message names consolidation as the sanctioned path, so both
# checks share one script and therefore one subprocess spawn per Bash call.
HOOK = (
    PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing"
    / "git_add_all_redirects_to_explicit_paths.py"
)


def _run(payload: dict):
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=20, env=os.environ.copy(),
    )


def _decision(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _bash(cmd: str, cwd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo_on(tmp_path):
    """Build a real origin + clone, checked out on the requested branch.

    Real repos rather than string fixtures: the bug being guarded is about what
    git RESOLVES a bare push to, which a stub cannot reproduce.
    """
    def _make(branch: str):
        slug = branch.replace("/", "-")   # feature/x must not nest directories
        origin = tmp_path / f"origin-{slug}"
        origin.mkdir()
        _git("init", "-q", "--bare", "--initial-branch=main", cwd=origin)

        work = tmp_path / f"work-{slug}"
        work.mkdir()
        _git("init", "-q", "--initial-branch=main", cwd=work)
        _git("remote", "add", "origin", str(origin), cwd=work)
        (work / "f.txt").write_text("x")
        _git("add", "f.txt", cwd=work)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i",
             cwd=work)
        _git("push", "-q", "-u", "origin", "main", cwd=work)
        if branch != "main":
            _git("checkout", "-q", "-b", branch, cwd=work)
            _git("push", "-q", "-u", "origin", branch, cwd=work)
        return work
    return _make


# ── The incident's exact shape ──────────────────────────────────────────────


def test_bare_push_from_a_checkout_on_main_is_blocked(repo_on):
    """THE regression. No "main" anywhere in the command."""
    work = repo_on("main")
    out = _decision(_run(_bash("git push", str(work))))
    assert out["permissionDecision"] == "deny", out
    assert "PRs only" in out["permissionDecisionReason"]


def test_bare_push_from_a_feature_branch_is_allowed(repo_on):
    """Non-regression, and the case that must stay fast: ordinary work."""
    work = repo_on("feature/x")
    out = _decision(_run(_bash("git push", str(work))))
    assert out["permissionDecision"] == "allow", out


# ── Explicit forms ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin +main",
    "git push origin develop:main",
    "git push -f origin main",
    "git push origin refs/heads/main",
])
def test_explicit_main_refspecs_are_blocked(cmd, repo_on):
    work = repo_on("feature/x")   # branch is irrelevant; the refspec decides
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "deny", f"{cmd}: {out}"


@pytest.mark.parametrize("cmd", [
    "git push origin develop",
    "git push origin main:develop",      # main is the SOURCE, develop the target
    "git push origin feature/x",
])
def test_pushes_to_other_branches_are_allowed(cmd, repo_on):
    work = repo_on("feature/x")
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


# ── Tag pushes: the release flow must keep working ─────────────────────────


@pytest.mark.parametrize("cmd", [
    "git push origin v1.2.3",
    "git push origin engine-service-v0.1.56",
    "git push --tags",
    "git push origin refs/tags/v1.2.3",
])
def test_tag_pushes_are_allowed_even_from_main(cmd, repo_on):
    """Tagging is the release publish step and is not a branch update.

    Checked FROM main deliberately — that is where a release tag is cut, so a
    guard that keyed on the current branch alone would break the release.
    """
    work = repo_on("main")
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


# ── Escape + compound + fail-open ──────────────────────────────────────────


def test_routing_allow_escape_permits_the_release_version_bump(repo_on):
    """The one sanctioned direct-to-main commit. Rare, deliberate, auditable."""
    work = repo_on("main")
    out = _decision(_run(_bash(
        "git push origin main  # routing-allow: release version bump, contributing.md",
        str(work),
    )))
    assert out["permissionDecision"] == "allow", out


def test_push_hidden_in_a_compound_command_is_caught(repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash("uv run pytest -q && git push", str(work))))
    assert out["permissionDecision"] == "deny", out


def test_non_push_git_commands_are_untouched(repo_on):
    work = repo_on("main")
    for cmd in ("git status", "git log --oneline -3", "git fetch", "git diff"):
        out = _decision(_run(_bash(cmd, str(work))))
        assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


def test_fails_open_outside_a_repo(tmp_path):
    """A workflow guard, not a security boundary: undeterminable git state must
    not brick pushes. Matches the sibling hooks' fail_closed=False contract."""
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    out = _decision(_run(_bash("git push", str(bare))))
    assert out["permissionDecision"] == "allow", out


# ── Registration: an unwired hook enforces nothing ─────────────────────────


def test_the_consolidated_hook_is_wired_and_stays_within_the_cap():
    """A hook that exists but does not run enforces nothing.

    Also pins the CONSOLIDATION: this check must not reacquire its own registry
    rule, because that is the fifth cross-plugin routing rule and RDR-121/125
    cap it at four. Splitting it back out silently breaks that budget.
    """
    hooks = (PROJECT_ROOT / "conexus" / "hooks" / "hooks.json").read_text()
    registry_path = (
        PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml"
    )
    registry = registry_path.read_text()

    assert "git_add_all_redirects_to_explicit_paths.py" in hooks, (
        "the consolidated hook is not wired into hooks.json"
    )
    assert "no_direct_push_to_main" not in hooks, (
        "the standalone hook is registered again — that is the 5th routing rule"
    )
    assert "nexus-vduer" in registry, (
        "the registry rationale no longer records the push-to-main half; a "
        "future reader would not know this hook carries two checks"
    )
