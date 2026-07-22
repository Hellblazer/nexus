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
