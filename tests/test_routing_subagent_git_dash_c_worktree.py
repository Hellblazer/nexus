# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``git -C <linked worktree>`` carve-out of the subagent git-write guard.

A subagent's Bash cwd is the session's start directory, the primary checkout,
even when its session works in a linked worktree; the cwd does not persist
between a subagent's calls. So a developer agent dispatched by a session cannot
commit in that session's worktree: ``cd <wt> && git commit`` runs from the
primary cwd and the guard denies it (2026-11-18: tamsin's RDR-252 Step 3 and
baton's RDR-253 Step 5 both stalled on this, each needing Sam to rule).

The carve-out admits exactly one shape: the whole command is ONE literal
``git -C <absolute path> <verb> ...`` invocation, the path is POSITIVELY proven
to be a linked worktree by ``git rev-parse`` run there, the verb is on an
allowlist that runs no arbitrary command, and nothing else in the text spells a
git write. Everything else falls through to the unchanged guard.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPT = (
    PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing"
    / "subagent_git_write_requires_orchestrator.py"
)
AGENT_ID = "aworker-x-6f59dab8bbb14864"
D = chr(36)  # dollar
BT = chr(96)  # backtick


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "isolated-nexus-config"))
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_REMOTE_TEST_HOSTS", raising=False)
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "dropped_writes.jsonl"))


@pytest.fixture()
def primary(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "primary"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


@pytest.fixture()
def worktree(primary: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt)], cwd=primary, check=True)
    return wt


def _verdict(cmd: str, cwd: pathlib.Path) -> dict:
    payload = {
        "tool_name": "Bash", "tool_input": {"command": cmd},
        "agent_id": AGENT_ID, "agent_type": "worker-x", "cwd": str(cwd),
    }
    env = {**os.environ, "NX_HOOK_PYTHON": sys.executable}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=20, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return {} if proc.stdout == "" else json.loads(proc.stdout)["hookSpecificOutput"]


def _allowed(cmd: str, cwd: pathlib.Path) -> bool:
    return "permissionDecision" not in _verdict(cmd, cwd)


def test_an_agent_in_the_primary_cwd_commits_in_its_sessions_worktree(primary, worktree):
    """The shapes a developer agent needs, run from the primary cwd."""
    wt = str(worktree)
    allowed = [
        f"git -C {wt} add f.txt",
        f"git -C {wt} add src/a.py tests/test_a.py",
        f"git -C {wt} commit -m 'fix(engine): owner stamper; deadline timer'",
        f'git -C {wt} commit -m "wip step 3"',
        f"git -C '{wt}' commit -m msg",
        f"git -C {wt} rebase origin/develop",
        f"git -C {wt} reset --hard abc1234",
        f"git -C {wt} restore f.txt",
        f"git -C {wt} stash",
        f"git -C {wt} cherry-pick abc1234",
    ]
    blocked = [c for c in allowed if not _allowed(c, primary)]
    assert not blocked, blocked


def test_the_same_shapes_aimed_at_the_primary_still_deny(primary, worktree):
    """-C naming the primary checkout, a non-repo or a relative path proves nothing."""
    p = str(primary)
    cmds = [
        f"git -C {p} commit -m msg",
        f"git -C {p} add f.txt",
        f"git -C {worktree}/../primary commit -m msg",
        f"git -C {primary.parent / 'nowhere'} commit -m msg",
        "git -C wt commit -m msg",
        "git -C . commit -m msg",
    ]
    passed = [c for c in cmds if _allowed(c, primary)]
    assert not passed, passed


def test_anything_beyond_one_literal_invocation_falls_through(primary, worktree):
    """A second command, an expansion, a redirect, a second repository or an
    option that runs a command is never admitted by the carve-out."""
    wt, p = str(worktree), str(primary)
    cmds = [
        f"git -C {wt} commit -m x && git commit -m y",
        f"git -C {wt} commit -m x; git checkout -- f.txt",
        f"git -C {wt} commit -m x | git reset --hard",
        f"git -C {wt} commit -m x\ngit -C {p} reset --hard",
        f'git -C {wt} commit -m "{D}(git -C {p} reset --hard)"',
        f"git -C {wt} commit -m {BT}git -C {p} reset --hard{BT}",
        f"git -C {wt} -C {p} commit -m x",
        f"git -C {wt} --git-dir={p}/.git commit -m x",
        f"git -C {wt} --work-tree={p} checkout -- f.txt",
        f"git -C {wt} -c core.worktree={p} commit -m x",
        f"GIT_DIR={p}/.git git -C {wt} commit -m x",
        f"git -C {wt} submodule foreach 'git -C {p} reset --hard'",
        f"git -C {wt} rebase -x 'git -C {p} reset --hard' origin/develop",
        f"git -C {wt} rebase --exec='touch x' origin/develop",
        f"git -C {wt} bisect run 'git -C {p} reset --hard'",
        f"git -C {wt} filter-branch --tree-filter 'rm -rf .' HEAD",
        f"git -C {wt} commit -m 'see git -C {p} reset --hard'",
        f"git -C {wt} commit -m x > {p}/out.txt",
        f"git -C {wt} config user.name x",
        f"git -C {wt} remote set-url origin /tmp/elsewhere",
    ]
    passed = [c for c in cmds if _allowed(c, primary)]
    assert not passed, passed


def test_reads_and_the_main_conversation_are_untouched(primary, worktree):
    """Control: read-only git and a git -C read pass as before."""
    assert _allowed(f"git -C {worktree} status --short", primary)
    assert _allowed(f"git -C {worktree} log --oneline -3", primary)
