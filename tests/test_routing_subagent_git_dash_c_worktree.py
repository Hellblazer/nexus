# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``git -C <linked worktree>`` carve-out of the subagent git-write guard.

A subagent's Bash cwd is the session's start directory, the primary checkout,
even when its session works in a linked worktree; the cwd does not persist
between a subagent's calls. So a developer agent dispatched by a session cannot
commit in that session's worktree: ``cd <wt> && git commit`` runs from the
primary cwd and the guard denies it (2026-11-18: tamsin's RDR-252 Step 3 and
baton's RDR-253 Step 5 both stalled on this, each needing Sam to rule).

The carve-out admits exactly one shape: the whole command is ONE literal
``git -C <absolute path> <verb> ...`` invocation, git proves the path is a
linked worktree's ROOT, the verb and every flag are on an allowlist (spelled
exactly: git accepts option prefixes, so a blocklist leaks), and nothing else in
the text spells a git write. Everything else falls through to the unchanged
guard. Each refusal below is built so that only the rule it names can deny it.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from tests.hooks.audience_harness import flagged_home

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPT = (
    PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing"
    / "subagent_git_write_requires_orchestrator.py"
)
HARNESS = PROJECT_ROOT / "tests" / "hooks" / "audience_harness.py"
AGENT_ID = "aworker-x-6f59dab8bbb14864"


#: nexus-sby49: the guard's full rule is the maintainer's. These tests build their
#: own repos with no nexus marker, so they run under an account home that carries
#: the flag (``tests/hooks/audience_harness.py`` is the only seam; the box's real
#: home never decides a verdict). The marker path is covered in
#: ``test_routing_subagent_git_write.py`` and ``test_audience.py``.
_HOME: list[pathlib.Path | None] = [None]


@pytest.fixture(autouse=True)
def _account_home(tmp_path):
    home = tmp_path / "acct-home"
    _HOME[0] = pathlib.Path(flagged_home(home))
    yield
    _HOME[0] = None


def _guard_argv() -> list[str]:
    assert _HOME[0] is not None
    return [sys.executable, str(HARNESS), str(_HOME[0]), str(SCRIPT)]
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
    (wt / "sub").mkdir()
    return wt


def _verdict(cmd: str, cwd: pathlib.Path) -> dict:
    payload = {
        "tool_name": "Bash", "tool_input": {"command": cmd},
        "agent_id": AGENT_ID, "agent_type": "worker-x", "cwd": str(cwd),
    }
    env = {**os.environ, "NX_HOOK_PYTHON": sys.executable}
    proc = subprocess.run(
        _guard_argv(), input=json.dumps(payload),
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
        f"git -C {wt} add -- src/a.py tests/test_a.py",
        f"git -C {wt} commit -m 'fix(engine): owner stamper; deadline timer'",
        f'git -C {wt} commit -m "wip step 3"',
        f"git -C {wt} commit -F /tmp/msg.txt",
        f"git -C {wt} commit --amend --no-edit",
        f"git -C '{wt}' commit -m msg",
        f"git -C {wt} rebase origin/develop",
        f"git -C {wt} rebase --onto origin/develop abc1234",
        f"git -C {wt} rebase --continue",
        f"git -C {wt} reset --soft 'HEAD~1'",
        f"git -C {wt} reset -- f.txt",
        f"git -C {wt} switch -c step-4",
        f"git -C {wt} branch step-4 abc1234",
        f"git -C {wt} cherry-pick abc1234",
        f"git -C {wt} merge --ff-only origin/develop",
        f"git -C {wt} commit -m '-x is a value here'",
        f"git -C {wt} add -- -leading-dash.txt",
        f"git -C {wt} commit --message=msg",
    ]
    blocked = [c for c in allowed if not _allowed(c, primary)]
    assert not blocked, blocked


def test_the_same_shapes_aimed_at_the_primary_still_deny(primary, worktree):
    """-C naming the primary, a non-repo, a relative path, a subdirectory or a
    worktree's administrative git dir proves nothing."""
    p = str(primary)
    cmds = [
        f"git -C {p} commit -m msg",
        f"git -C {p} add f.txt",
        f"git -C {worktree}/../primary commit -m msg",
        f"git -C {primary.parent / 'nowhere'} commit -m msg",
        "git -C wt commit -m msg",
        "git -C . commit -m msg",
        f"git -C {worktree}/sub commit -m msg",
        f"git -C {p}/.git/worktrees/wt commit -m msg",
    ]
    passed = [c for c in cmds if _allowed(c, primary)]
    assert not passed, passed


def test_git_option_prefixes_cannot_reach_a_refused_flag(primary, worktree):
    """Review round 1's ship-blocker: git reads `--exe`, `--ex=` and `--e` as
    `--exec`. No git text sits in these commands, so only the flag allowlist
    can deny them."""
    wt = str(worktree)
    cmds = [
        f"git -C {wt} rebase --exe 'touch /tmp/x' abc1234",
        f"git -C {wt} rebase --ex='touch /tmp/x' abc1234",
        f"git -C {wt} rebase --e 'touch /tmp/x' abc1234",
        f"git -C {wt} rebase --exec 'touch /tmp/x' abc1234",
        f"git -C {wt} rebase -x 'touch /tmp/x' abc1234",
        f"git -C {wt} rebase -ix 'touch /tmp/x' abc1234",
        f"git -C {wt} rebase --autostash abc1234",
        f"git -C {wt} merge -s ours abc1234",
        f"git -C {wt} merge --strat ours abc1234",
        f"git -C {wt} commit -am msg",
        f"git -C {wt} commit --mess msg",
        f"git -C {wt} commit -m msg --template=/tmp/t",
        f"git -C {wt} commit -m=x",
        f"git -C {wt} commit --all=x -m msg",
        f"git -C {wt} add -u -x",
    ]
    passed = [c for c in cmds if _allowed(c, primary)]
    assert not passed, passed


def test_discarding_work_and_shared_refs_are_never_admitted(primary, worktree):
    """The carve-out cannot prove the worktree is the agent's own session's, so
    nothing it admits discards uncommitted work or moves refs every worktree
    shares (review round 1: stash pop took the primary's stash entry)."""
    wt = str(worktree)
    cmds = [
        f"git -C {wt} reset --hard abc1234",
        f"git -C {wt} checkout .",
        f"git -C {wt} checkout f.txt",
        f"git -C {wt} checkout HEAD f.txt",
        f"git -C {wt} checkout -b step-4",
        f"git -C {wt} merge --abort",
        f"git -C {wt} rebase --abort",
        f"git -C {wt} rebase --skip",
        f"git -C {wt} rebase --quit",
        f"git -C {wt} cherry-pick --abort",
        f"git -C {wt} reset --keep abc1234",
        f"git -C {wt} restore f.txt",
        f"git -C {wt} checkout -- f.txt",
        f"git -C {wt} checkout -f wt-branch",
        f"git -C {wt} checkout -B other abc1234",
        f"git -C {wt} switch -C other",
        f"git -C {wt} switch --discard-changes other",
        f"git -C {wt} stash",
        f"git -C {wt} stash pop",
        f"git -C {wt} tag -d v1",
        f"git -C {wt} branch -D other",
        f"git -C {wt} branch -m other renamed",
        f"git -C {wt} rm -f f.txt",
        f"git -C {wt} clean -fd",
    ]
    passed = [c for c in cmds if _allowed(c, primary)]
    assert not passed, passed


def test_anything_beyond_one_literal_invocation_falls_through(primary, worktree):
    """A second command, an expansion, a redirect, a second repository or a git
    write named inside an argument is never admitted by the carve-out."""
    wt, p = str(worktree), str(primary)
    cmds = [
        f"git -C {wt} commit -m x && git commit -m y",
        f"git -C {wt} commit -m x && touch /tmp/y",
        f"git -C {wt} commit -m x; touch /tmp/y",
        f"git -C {wt} commit -m x || touch /tmp/y",
        f"git -C {wt} commit -m x\ntouch /tmp/y",
        f'git -C {wt} commit -m "{D}(touch /tmp/y)"',
        f"git -C {wt} commit -m {BT}touch /tmp/y{BT}",
        f"git -C {wt} rebase --ex 'x=g; y=it; {D}x{D}y -C {p} reset --hard' abc1234",
        f"git -C {wt} switch --ignore-other-worktrees develop",
        f"git -C {wt} branch -u origin/develop",
        f"git -C {wt} branch --set-upstream-to=origin/develop",
        f"git -C {wt} stash clear",
        f"git -C {wt} commit -m x; git checkout -- f.txt",
        f"git -C {wt} commit -m x | git reset --hard",
        f"git -C {wt} commit -m x\ngit -C {p} reset --hard",
        f'git -C {wt} commit -m "{D}(git -C {p} reset --hard)"',
        f"git -C {wt} commit -m {BT}git -C {p} reset --hard{BT}",
        f"git -C {wt} -C {p} commit -m x",
        f"git -C {wt} --git-dir={p}/.git commit -m x",
        f"git -C {wt} -c core.worktree={p} commit -m x",
        f"GIT_DIR={p}/.git git -C {wt} commit -m x",
        f"git -C {wt} commit -m 'see git -C {p} reset --hard'",
        f"git -C {wt} commit -m x > {p}/out.txt",
        f"git -C {wt} reset --soft HEAD~1",
        f"git -C {wt} config user.name x",
    ]
    passed = [c for c in cmds if _allowed(c, primary)]
    assert not passed, passed


def test_the_deny_message_leads_with_the_working_shape(primary):
    reason = _verdict(f"git -C {primary} commit -m x", primary)["permissionDecisionReason"]
    first = reason.splitlines()[0]
    assert "git -C <absolute worktree root>" in first, first
    assert "'HEAD~1'" in first and "-F <file>" in first, first


def test_reads_are_untouched(primary, worktree):
    """Control: read-only git passes without the carve-out, as before."""
    assert _allowed(f"git -C {worktree} status --short", primary)
    assert _allowed(f"git -C {worktree} log --oneline -3", primary)
