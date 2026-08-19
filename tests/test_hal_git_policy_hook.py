# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coverage for Hal's extracted personal git-policy hook (nexus-ww9fw,
2026-08-18): wildcard `git add` staging + push-to-main.

These two checks used to live inside the plugin's
``git_add_all_redirects_to_explicit_paths.py`` (RDR-121 Phase 2 hook 3).
Hal ruled 2026-08-18 that they are his own standing workflow preferences,
not general conexus-plugin features, and moved them into a standalone
user-level hook delivered outside this repo. This file tests
``tests/fixtures/hal_git_policy_hook.py`` -- a checked-in COPY of that
extraction (see the fixture's own header) -- so the behavior stays under
CI even though the real installed copy lives outside version control.
Drift between the fixture and Hal's actually-installed copy is accepted;
see the fixture's docstring.

Retargeted from (now deleted) ``tests/test_routing_git_add_all.py`` and
``tests/test_routing_no_direct_push_to_main.py``, minus the tests that
were specific to the PLUGIN's registry/hooks.json wiring and the
nexus-vscgz repo-scope guard -- the extracted hook has neither (it is
installed by Hal wherever he wants, not gated to nexus checkouts).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
HOOK = PROJECT_ROOT / "tests" / "fixtures" / "hal_git_policy_hook.py"


def _run(payload: dict):
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=20, env=os.environ.copy(),
    )


def _decision(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _bash(cmd: str, cwd: str | None = None) -> dict:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))


def test_script_exists():
    assert HOOK.exists()


# ── Rule 1: wildcard `git add` ──────────────────────────────────────────────


def test_git_add_dash_A_denies():
    d = _decision(_run(_bash("git add -A")))
    assert d["permissionDecision"] == "deny"
    assert "explicit" in d["reason"].lower() or "path" in d["reason"].lower()


def test_git_add_dot_denies():
    d = _decision(_run(_bash("git add .")))
    assert d["permissionDecision"] == "deny"


def test_git_add_all_long_flag_denies():
    d = _decision(_run(_bash("git add --all")))
    assert d["permissionDecision"] == "deny"


def test_git_add_all_with_pathspec_denies():
    d = _decision(_run(_bash("git add --all src/")))
    assert d["permissionDecision"] == "deny"


def test_chained_git_add_dot_denies():
    """`git status && git add . && git commit` still triggers."""
    d = _decision(_run(_bash("git status && git add . && git commit -m foo")))
    assert d["permissionDecision"] == "deny"


def test_git_add_explicit_paths_allows():
    d = _decision(_run(_bash("git add src/foo.py tests/test_foo.py")))
    assert d["permissionDecision"] == "allow"


def test_git_add_single_dotfile_allows():
    """`git add .gitignore` is explicit, not wildcard."""
    d = _decision(_run(_bash("git add .gitignore")))
    assert d["permissionDecision"] == "allow"


def test_git_status_allows():
    d = _decision(_run(_bash("git status")))
    assert d["permissionDecision"] == "allow"


def test_non_git_command_allows():
    d = _decision(_run(_bash("ls -A")))
    assert d["permissionDecision"] == "allow"


def test_non_bash_allows():
    d = _decision(_run({"tool_name": "Edit", "tool_input": {"file_path": "x"}}))
    assert d["permissionDecision"] == "allow"


def test_wildcard_add_escape_allows():
    d = _decision(_run(_bash(
        "git add -A  # routing-allow: scripted bootstrap of fresh repo"
    )))
    assert d["permissionDecision"] == "allow"


def test_empty_stdin_allows():
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="", capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    d = json.loads(proc.stdout)["hookSpecificOutput"]
    assert d["permissionDecision"] == "allow"


def test_escape_on_nonmatching_command_logs_nothing(tmp_path, monkeypatch):
    """nexus-mzvwa.8's match-first-escape-second lesson applies here too:
    an escape token on a non-matching command must not log a phantom
    escape event."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log))
    d = _decision(_run(
        _bash("bd close nexus-xyz --reason done  # routing-allow: gate satisfied")
    ))
    assert d["permissionDecision"] == "allow"
    assert not log.exists() or log.read_text().strip() == "", (
        "non-matching annotated command must log NOTHING (phantom escape)"
    )


def test_escape_on_matching_command_logs_true_escape(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log))
    d = _decision(_run(
        _bash("git add -A  # routing-allow: scripted bootstrap of fresh repo")
    ))
    assert d["permissionDecision"] == "allow"
    events = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(events) == 1
    assert events[0]["outcome"] == "escape"
    assert events[0]["rule"] == "nexus_git_policy"


# ── Rule 2: push-to-main (nexus-vduer) ──────────────────────────────────────
#
# THE INCIDENT (2026-07-23, self-reported, in the nexus repo). During the
# P4b Phase 0c commit the orchestrator pushed directly to main. Session
# restarts had left the working tree on main and verify-branch-before-commit
# was a MEMORY-ONLY control, so it failed the way memory-only controls fail.
# No damage, but the lesson was: mechanize, not write a retro note. The
# checkout was ALREADY on main, so a bare `git push` inherited the target
# from the branch's upstream -- a matcher looking for the literal token
# would have missed the exact event it exists to prevent. So the tests
# drive real git repos rather than asserting on strings.


@pytest.fixture()
def repo_on(tmp_path):
    def _make(branch: str):
        slug = branch.replace("/", "-")
        origin = tmp_path / f"origin-{slug}"
        origin.mkdir(parents=True)
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


def test_bare_push_from_a_checkout_on_main_is_blocked(repo_on):
    """THE regression. No "main" anywhere in the command."""
    work = repo_on("main")
    out = _decision(_run(_bash("git push", str(work))))
    assert out["permissionDecision"] == "deny", out
    assert "PRs only" in out["permissionDecisionReason"]


def test_bare_push_from_a_feature_branch_is_allowed(repo_on):
    work = repo_on("feature/x")
    out = _decision(_run(_bash("git push", str(work))))
    assert out["permissionDecision"] == "allow", out


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


@pytest.mark.parametrize("cmd", [
    "git push origin v1.2.3",
    "git push origin engine-service-v0.1.56",
    "git push --tags",
    "git push origin refs/tags/v1.2.3",
])
def test_tag_pushes_are_allowed_even_from_main(cmd, repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


@pytest.mark.parametrize("cmd", [
    "git push --follow-tags",              # bare: pushes the branch too
    "git push --follow-tags origin main",
    "git push --tags origin main",         # explicit branch refspec alongside tags
    "git push --tags origin HEAD:main",
])
def test_tag_flags_do_not_exempt_a_branch_push(cmd, repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "deny", (
        f"{cmd!r} pushes the BRANCH as well as tags -- a tag flag must not "
        f"blanket-exempt it: {out}"
    )


@pytest.mark.parametrize("cmd", [
    "git push --tags",
    "git push --tags origin",
])
def test_a_bare_tags_push_is_still_allowed(cmd, repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


def test_follow_tags_from_a_feature_branch_is_allowed(repo_on):
    work = repo_on("feature/x")
    out = _decision(_run(_bash("git push --follow-tags", str(work))))
    assert out["permissionDecision"] == "allow", out


def test_push_to_main_escape_permits_the_release_version_bump(repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash(
        "git push origin main  # routing-allow: release version bump",
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


def test_bare_push_with_stdout_redirect_from_main_is_still_blocked(repo_on):
    """nexus-cr4lp B1: a phantom refspec manufactured from shell redirection
    tokens (``>``, ``/dev/null``) must not defeat the guard."""
    work = repo_on("main")
    out = _decision(_run(_bash("git push > /dev/null", str(work))))
    assert out["permissionDecision"] == "deny", out
    assert "PRs only" in out["permissionDecisionReason"]


def test_fails_open_outside_a_repo(tmp_path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    out = _decision(_run(_bash("git push", str(bare))))
    assert out["permissionDecision"] == "allow", out


def test_master_default_branch_is_also_protected(tmp_path):
    """Unlike the plugin's departed check, this hook has no nexus-repo
    scope guard -- Hal installs it wherever he wants, so it must protect
    `master` (his other repos' default) the same as `main`."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "--bare", "--initial-branch=master", cwd=origin)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "--initial-branch=master", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    (work / "f.txt").write_text("x")
    _git("add", "f.txt", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i", cwd=work)
    _git("push", "-q", "-u", "origin", "master", cwd=work)
    out = _decision(_run(_bash("git push", str(work))))
    assert out["permissionDecision"] == "deny", out


# ---------------------------------------------------------------------------
# nexus-2e874: malformed quoting must degrade safely, never silently bypass.
# ---------------------------------------------------------------------------


def test_wildcard_add_with_unbalanced_quote_is_still_denied():
    """nexus-2e874: an unbalanced quote in the same segment used to make
    shlex reject it and the whole segment was silently SKIPPED -- a full
    bypass of rule 1. The degraded whitespace fallback keeps the
    `git add -A` anchor visible."""
    d = _decision(_run(_bash('git add -A "oops')))
    assert d["permissionDecision"] == "deny"


def test_push_to_main_with_unbalanced_quote_is_still_blocked(repo_on):
    """nexus-2e874 live specimen: `git push origin main --receive-pack="x`
    was ALLOWed with zero context (rule 2 fully bypassed)."""
    work = repo_on("feature/x")
    out = _decision(_run(_bash(
        'git push origin main --receive-pack="unterminated', str(work),
    )))
    assert out["permissionDecision"] == "deny", out


def test_unbalanced_quote_on_a_feature_push_is_still_allowed(repo_on):
    """The degraded parse must not over-deny: a malformed-quote push whose
    destination is NOT protected stays allowed."""
    work = repo_on("feature/x")
    out = _decision(_run(_bash(
        'git push origin feature/x --receive-pack="unterminated', str(work),
    )))
    assert out["permissionDecision"] == "allow", out


def test_quote_inside_the_verb_is_still_blocked(repo_on):
    """Review Important-1 (nexus-2e874): a quote INSIDE the verb fractures
    the quote-as-space variant ('gi', 't', ...) -- the quote-removed
    variant must catch it."""
    work = repo_on("feature/x")
    out = _decision(_run(_bash('gi"t push origin main', str(work))))
    assert out["permissionDecision"] == "deny", out
