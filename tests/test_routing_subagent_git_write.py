# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-184 Gap-4 mechanization (nexus-s88vq):
subagent_git_write_requires_orchestrator.

Subagents (PreToolUse payloads carrying ``agent_id`` — the documented
subagent-origin marker) are denied ``git commit`` / ``git add`` in the
PRIMARY checkout. The main conversation (no ``agent_id``), read-only git,
linked-worktree agents, and ``# routing-allow:`` escapes all pass.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
HOOK_SCRIPT = (
    PROJECT_ROOT
    / "conexus"
    / "hooks"
    / "scripts"
    / "routing"
    / "subagent_git_write_requires_orchestrator.py"
)

AGENT_ID = "aworker-x-6f59dab8bbb14864"


def _run(payload: dict, env_extra: dict[str, str] | None = None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=20, env=env,
    )


def _decision(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _bash(cmd: str, *, agent: bool = True, cwd: str | None = None) -> dict:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if agent:
        payload["agent_id"] = AGENT_ID
        payload["agent_type"] = "worker-x"
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))


@pytest.fixture()
def shared_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A PRIMARY git checkout (git-dir == git-common-dir)."""
    repo = tmp_path / "shared"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


@pytest.fixture()
def linked_worktree(shared_repo: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    (shared_repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=shared_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=shared_repo, check=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt)],
        cwd=shared_repo, check=True,
    )
    return wt


def test_script_exists():
    assert HOOK_SCRIPT.exists()


def test_registered_in_hooks_json():
    hooks = json.loads(
        (PROJECT_ROOT / "conexus" / "hooks" / "hooks.json").read_text()
    )
    commands = [
        h["command"]
        for entry in hooks["hooks"]["PreToolUse"]
        for h in entry.get("hooks", [])
    ]
    assert any("subagent_git_write_requires_orchestrator.py" in c for c in commands)


def test_registered_in_registry_yaml():
    text = (
        PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml"
    ).read_text()
    assert "subagent_git_write_requires_orchestrator:" in text


class TestDeny:
    def test_subagent_commit_in_shared_tree_denied(self, shared_repo):
        out = _decision(_run(_bash("git commit -m msg", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny"
        assert "orchestrator" in out["permissionDecisionReason"].lower()

    def test_subagent_add_in_shared_tree_denied(self, shared_repo):
        out = _decision(_run(_bash("git add src/file.py", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny"

    def test_compound_command_denied(self, shared_repo):
        out = _decision(
            _run(_bash("uv run pytest && git add x.py && git commit -m done", cwd=str(shared_repo)))
        )
        assert out["permissionDecision"] == "deny"

    def test_global_flag_form_denied(self, shared_repo):
        out = _decision(
            _run(_bash(f"git -C {shared_repo} commit -m msg", cwd=str(shared_repo)))
        )
        assert out["permissionDecision"] == "deny"


class TestAllow:
    def test_main_conversation_commit_allowed(self, shared_repo):
        out = _decision(_run(_bash("git commit -m msg", agent=False, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow"

    def test_subagent_readonly_git_allowed(self, shared_repo):
        for cmd in ("git status", "git diff", "git log --oneline", "git show HEAD"):
            out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
            assert out["permissionDecision"] == "allow", cmd

    def test_subagent_nongit_allowed(self, shared_repo):
        out = _decision(_run(_bash("ls -la && echo commit", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow"

    def test_commit_substring_not_subcommand_allowed(self, shared_repo):
        out = _decision(_run(_bash("git log --grep=commit", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow"

    def test_linked_worktree_commit_allowed(self, linked_worktree):
        """Worktree-isolated agents own their tree — their local commits are
        the documented harvest choreography, never blocked."""
        out = _decision(_run(_bash("git commit -m wt", cwd=str(linked_worktree))))
        assert out["permissionDecision"] == "allow"

    def test_non_repo_cwd_fails_open(self, tmp_path):
        out = _decision(_run(_bash("git commit -m msg", cwd=str(tmp_path / "norepo"))))
        assert out["permissionDecision"] == "allow"

    def test_escape_token_allows_and_logs(self, shared_repo, tmp_path):
        out = _decision(
            _run(_bash("git commit -m msg # routing-allow: orchestrator sanctioned", cwd=str(shared_repo)))
        )
        assert out["permissionDecision"] == "allow"
        log = (tmp_path / "log.jsonl").read_text()
        assert '"outcome": "escape"' in log or '"escape"' in log

    def test_junk_stdin_fails_open(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="not json", capture_output=True, text=True, timeout=20,
            env={**os.environ},
        )
        assert proc.returncode == 0


# ── nexus-ays2l: the WORKING-TREE-DESTROYING verbs ──────────────────────────
#
# The original verb set was {"commit", "add"} — strictly narrower than the set
# of git verbs that can destroy an orchestrator's uncommitted work. `git add`
# mutates only the INDEX and destroys nothing; `git checkout -- <path>` and
# `git restore <path>` and `git stash` mutate the WORKING TREE and delete
# uncommitted edits outright. The guard blocked the harmless-but-untidy verbs
# and permitted the destructive ones.
#
# Damage signature that produced the bead (2026-07-24): three silent reversions
# of src/nexus/upgrade_finish.py over ~10 minutes with two subagents live,
# sibling files edited in the same window untouched, NO stash entry and NO
# reflog entry — the trace `git checkout -- <path>` leaves and `git stash`
# does not. Attribution was never proven; what IS established is that the
# guard would not have stopped any subagent that ran those verbs.


_DESTRUCTIVE_INVOCATIONS = [
    "git checkout -- src/nexus/upgrade_finish.py",
    "git checkout HEAD -- src/nexus/upgrade_finish.py",
    "git restore src/nexus/upgrade_finish.py",
    "git stash",
    "git stash push -m wip",
    "git clean -fd",
    "git reset --hard HEAD",
    "git rm -f src/nexus/upgrade_finish.py",
]


@pytest.mark.parametrize("cmd", _DESTRUCTIVE_INVOCATIONS)
def test_destructive_verbs_denied_in_shared_tree(cmd, shared_repo):
    out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
    assert out["permissionDecision"] == "deny", f"{cmd} was permitted: {out}"
    assert "uncommitted" in out["permissionDecisionReason"].lower()


@pytest.mark.parametrize("cmd", [
    "git stash list",
    "git stash show -p",
    "git show HEAD:src/nexus/upgrade_finish.py",
    "git status",
    "git diff",
    "git log --oneline -5",
])
def test_read_only_inspection_still_allowed(cmd, shared_repo):
    """Reviewers must keep working. The bead's stated preference: allowlist the
    read-only invocations rather than blanket-denying the verb."""
    out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
    assert out["permissionDecision"] == "allow", f"{cmd} was blocked: {out}"


@pytest.mark.parametrize("cmd", _DESTRUCTIVE_INVOCATIONS)
def test_destructive_verbs_allowed_in_linked_worktree(cmd, linked_worktree):
    """A worktree-isolated agent owns its tree — including destroying it."""
    out = _decision(_run(_bash(cmd, cwd=str(linked_worktree))))
    assert out["permissionDecision"] == "allow", f"{cmd} was blocked: {out}"


def test_main_conversation_unaffected_by_the_widening(shared_repo):
    """The rule targets subagents. The orchestrator resets its own tree."""
    out = _decision(_run(_bash("git checkout -- x.py", agent=False, cwd=str(shared_repo))))
    assert out["permissionDecision"] == "allow"


def test_routing_allow_escape_still_works(shared_repo):
    out = _decision(_run(_bash(
        "git checkout -- x.py  # routing-allow: orchestrator asked me to revert this",
        cwd=str(shared_repo),
    )))
    assert out["permissionDecision"] == "allow"


# ── Fail mode is SPLIT by what is at stake (Hal ruling 2026-07-25, item 3) ──


def test_destructive_verb_fails_CLOSED_when_worktree_undeterminable(tmp_path):
    """A non-repo cwd makes `git rev-parse` fail, so worktree state is
    undeterminable. Destroyers deny anyway: 'I could not tell whether this tree
    is shared' is not a licence to destroy one."""
    not_a_repo = tmp_path / "bare"
    not_a_repo.mkdir()
    out = _decision(_run(_bash("git checkout -- x.py", cwd=str(not_a_repo))))
    assert out["permissionDecision"] == "deny", out
    reason = out["permissionDecisionReason"].lower()
    assert "could not be determined" in reason and "fail closed" in reason


def test_index_verbs_still_fail_OPEN_when_worktree_undeterminable(tmp_path):
    """Unchanged for commit/add: a flaky `git rev-parse` must never wedge agent
    work over tidiness, and `add` destroys nothing."""
    not_a_repo = tmp_path / "bare2"
    not_a_repo.mkdir()
    for cmd in ("git add -A", "git commit -m x"):
        out = _decision(_run(_bash(cmd, cwd=str(not_a_repo))))
        assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


def test_bare_stash_is_not_mistaken_for_a_read(shared_repo):
    """`git stash list` reads; `git stash` STASHES. Only an exact match against
    the read-only allowlist may pass."""
    assert _decision(_run(_bash("git stash", cwd=str(shared_repo))))["permissionDecision"] == "deny"
    assert _decision(_run(_bash("git stash list", cwd=str(shared_repo))))["permissionDecision"] == "allow"


def test_destructive_verb_hidden_in_a_compound_command_is_caught(shared_repo):
    """Segment splitting must see past `&&` — the realistic shape is a cleanup
    tail on an otherwise innocuous command."""
    out = _decision(_run(_bash(
        "pytest -q && git checkout -- src/nexus/upgrade_finish.py", cwd=str(shared_repo),
    )))
    assert out["permissionDecision"] == "deny", out


def test_unbalanced_quote_commit_is_still_denied(shared_repo):
    """nexus-2e874: a stray quote in any argument used to make shlex reject
    the segment and the guard silently skipped it -- a subagent `git commit`
    (or stash) became invisible. The degraded whitespace fallback keeps the
    `git <subcommand>` anchor visible."""
    out = _decision(_run(_bash('git commit -m "unterminated', cwd=str(shared_repo))))
    assert out["permissionDecision"] == "deny", out


def test_unbalanced_quote_nongit_segment_stays_allowed(shared_repo):
    """The degraded parse stays anchored: a non-git command whose
    unterminated quoted string merely MENTIONS `git commit` never matches
    (the segment's first token is not `git`)."""
    out = _decision(_run(_bash('echo "later run git commit -m x', cwd=str(shared_repo))))
    assert out["permissionDecision"] == "allow", out


def test_quote_inside_the_subcommand_is_still_denied(shared_repo):
    """Review Important-1 (nexus-2e874): quote INSIDE the verb -- the
    quote-removed degraded variant must still anchor `git commit`."""
    out = _decision(_run(_bash('git com"mit -m msg', cwd=str(shared_repo))))
    assert out["permissionDecision"] == "deny", out
