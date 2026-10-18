# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-i93tz phase 1: the subagent shared-git-state integrity check.

``conexus/hooks/scripts/routing/subagent_git_integrity_check.py`` snapshots the
primary checkout's shared git state in PreToolUse and diffs it in PostToolUse /
PostToolUseFailure. Every route below is run FOR REAL (a real ``sh -c`` against
a real git sandbox) between a real Pre and a real Post invocation of the hook
script, once with the primary as the tool cwd and once from a linked worktree.
Nothing mocks git or the filesystem.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "subagent_git_integrity_check.py"
HOOKS_JSON = REPO_ROOT / "conexus" / "hooks" / "hooks.json"
RULE = "subagent_git_integrity_check"


@dataclass
class Box:
    root: pathlib.Path
    primary: pathlib.Path
    worktree: pathlib.Path
    env: dict[str, str]
    cfg: pathlib.Path
    dropped: pathlib.Path
    base_sha: str  # develop~1

    @property
    def git_dir(self) -> pathlib.Path:
        return self.primary / ".git"

    def cwd(self, which: str) -> pathlib.Path:
        return self.primary if which == "primary" else self.worktree

    def git(self, *args: str, cwd: pathlib.Path | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.primary, env=self.env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()


def _clean_env(tmp: pathlib.Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NX_SERVICE_", "GIT_"))}
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.update(
        NEXUS_CONFIG_DIR=str(tmp / "cfg"),
        NX_DROPPED_WRITES_LOG_PATH=str(tmp / "dropped.jsonl"),
        NX_HOOK_PYTHON=sys.executable,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


@pytest.fixture
def box(tmp_path: pathlib.Path) -> Box:
    env = _clean_env(tmp_path)
    primary = tmp_path / "primary"
    worktree = tmp_path / "wt"
    primary.mkdir()

    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=primary, env=env, capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q", "-b", "develop")
    (primary / "f").write_text("1")
    git("add", "f")
    git("commit", "-q", "-m", "one")
    (primary / "f").write_text("2")
    git("commit", "-q", "-am", "two")
    git("worktree", "add", "-q", "-b", "wt", str(worktree))
    return Box(
        root=tmp_path, primary=primary, worktree=worktree, env=env, cfg=tmp_path / "cfg",
        dropped=tmp_path / "dropped.jsonl", base_sha=git("rev-parse", "develop~1"),
    )


def _hook(box: Box, event: str, command: str, cwd: pathlib.Path, *, agent: bool = True,
          tool_use_id: str = "toolu_1", session: str = "sess-1", env: dict[str, str] | None = None):
    payload = {
        "hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": command},
        "cwd": str(cwd), "session_id": session, "tool_use_id": tool_use_id,
    }
    if agent:
        payload.update(agent_id="agent-1", agent_type="developer")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(payload), capture_output=True,
        text=True, timeout=20, env=env or box.env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _report(out: dict | None) -> str:
    """The report text a Post envelope carries, or '' when it said nothing."""
    if out is None:
        return ""
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] in ("PostToolUse", "PostToolUseFailure")
    text = hso["additionalContext"]
    if hso["hookEventName"] == "PostToolUse":
        assert out["decision"] == "block" and out["reason"] == text
    else:
        assert "decision" not in out
    assert out["systemMessage"]
    return text


def _call(box: Box, command: str, cwd: pathlib.Path, *, agent: bool = True, between=None,
          tool_use_id: str = "toolu_1") -> str:
    """Pre hook, the command for real, optional action between, Post hook; the report."""
    pre = _hook(box, "PreToolUse", command, cwd, agent=agent, tool_use_id=tool_use_id)
    assert pre is None, f"Pre must be silent on a clean snapshot: {pre}"
    rc = subprocess.run(["sh", "-c", command], cwd=cwd, env=box.env, capture_output=True).returncode
    if between:
        between()
    event = "PostToolUse" if rc == 0 else "PostToolUseFailure"
    return _report(_hook(box, event, command, cwd, agent=agent, tool_use_id=tool_use_id))


def _q(p: object) -> str:
    return shlex.quote(str(p))


# ---------------------------------------------------------------------------
# Every route is detected, from the primary and from a linked worktree
# ---------------------------------------------------------------------------

def _hooks_path_setup(b: Box) -> str:
    hooks_dir = b.root / "elsewhere"
    hooks_dir.mkdir()
    (hooks_dir / "pre-commit").write_text("#!/bin/sh\n")
    b.git("config", "core.hooksPath", str(hooks_dir))
    return f"echo x > {_q(hooks_dir)}/pre-push"


#: (id, command builder given the sandbox, substrings the report must carry)
ROUTES = [
    ("raw-config-append",
     lambda b: f"printf '[core]\\n\\thooksPath=/tmp/x\\n' >> {_q(b.git_dir)}/config",
     ["config:", "core.hooksPath", "ADDED", "/tmp/x"]),
    ("hook-file-written",
     lambda b: f"echo x > {_q(b.git_dir)}/hooks/pre-commit",
     ["hooks/pre-commit", "ADDED"]),
    ("variable-held-verb",
     lambda b: "x=config; git $x user.name e",
     ["config:", "user.name", "ADDED", "'e'"]),
    ("scalar-style-config-writes",
     lambda b: ("git config core.untrackedCache true && git config feature.manyFiles true "
                "&& git config maintenance.gc.enabled false && git config core.fsmonitor true"),
     ["core.untrackedCache", "feature.manyFiles", "maintenance.gc.enabled", "core.fsmonitor"]),
    ("branch-M-of-the-checked-out-branch",
     lambda b: "git branch -M develop renamed",
     ["HEAD:", "refs/heads/renamed", "may be the orchestrator"]),
    ("update-ref",
     lambda b: f"git update-ref refs/heads/develop {b.base_sha}",
     ["refs/heads/develop:", "may be the orchestrator"]),
    ("info-exclude-write",
     lambda b: f"echo secret-pattern >> {_q(b.git_dir)}/info/exclude",
     ["info/exclude: changed", "secret-pattern"]),
    ("info-attributes-write",
     lambda b: f"echo '* filter=evil' >> {_q(b.git_dir)}/info/attributes",
     ["info/attributes: created", "filter=evil"]),
    ("write-into-the-core.hooksPath-directory",
     _hooks_path_setup,
     ["core.hooksPath dir", "pre-push", "ADDED"]),
]


def test_the_route_census_does_not_shrink() -> None:
    assert len(ROUTES) == 9 and len({r[0] for r in ROUTES}) == 9


@pytest.mark.parametrize("which", ["primary", "worktree"])
@pytest.mark.parametrize("route", ROUTES, ids=[r[0] for r in ROUTES])
def test_every_route_is_detected(box: Box, which: str, route) -> None:
    _, build, needles = route
    report = _call(box, build(box), box.cwd(which))
    missing = [n for n in needles if n.lower() not in report.lower()]
    assert report and not missing, f"missing {missing} in:\n{report or '(silent)'}"


@pytest.mark.skipif(shutil.which("scalar") is None, reason="the scalar binary is not installed")
@pytest.mark.parametrize("which", ["primary", "worktree"])
def test_real_scalar_register_is_detected(box: Box, which: str) -> None:
    """The real binary, where it exists (git ships it from 2.38)."""
    # scalar records the enlistment in the GLOBAL config, so it needs a writable one.
    (box.root / "global.gitconfig").write_text("")
    box.env["GIT_CONFIG_GLOBAL"] = str(box.root / "global.gitconfig")
    report = _call(box, f"scalar register --no-maintenance {_q(box.primary)}", box.cwd(which))
    for key in ("core.fsmonitor", "core.untrackedCache", "feature.manyFiles", "gc.auto"):
        assert key in report, f"{key} missing from:\n{report or '(silent)'}"


# ---------------------------------------------------------------------------
# Reads report nothing; non-subagent calls do nothing
# ---------------------------------------------------------------------------

READS = ["git status", "git log --oneline", "git diff", "git branch --list", "git rev-parse HEAD",
         "git config --get core.bare", "git status --short"]


@pytest.mark.parametrize("which", ["primary", "worktree"])
def test_read_only_calls_report_nothing(box: Box, which: str) -> None:
    cmds = READS + [f"cat {_q(box.git_dir)}/config", f"ls {_q(box.git_dir)}/hooks"]
    for n, command in enumerate(cmds):
        assert _call(box, command, box.cwd(which), tool_use_id=f"toolu_r{n}") == "", command
    assert not list((box.cfg / "git-integrity").rglob("*.json")), "Post must consume its snapshot"


def test_a_non_subagent_call_does_nothing(box: Box) -> None:
    command = f"printf '[core]\\n\\thooksPath=/tmp/x\\n' >> {_q(box.git_dir)}/config"
    assert _call(box, command, box.primary, agent=False) == ""
    assert not (box.cfg / "git-integrity").exists(), "no state may be written for a main-conversation call"
    assert not box.dropped.exists()


def test_a_tool_other_than_bash_does_nothing(box: Box) -> None:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": str(box.primary),
               "session_id": "s", "tool_use_id": "t", "agent_id": "a"}
    proc = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                          capture_output=True, text=True, env=box.env)
    assert proc.returncode == 0 and proc.stdout == "" and not (box.cfg / "git-integrity").exists()


def test_a_cwd_outside_any_repository_is_silent_even_without_a_snapshot(box: Box) -> None:
    outside = box.root / "outside"
    outside.mkdir()
    assert _report(_hook(box, "PostToolUse", "ls", outside)) == ""


# ---------------------------------------------------------------------------
# The concurrent orchestrator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["primary", "worktree"])
def test_an_orchestrator_commit_during_the_call_is_reported_as_possibly_the_orchestrator(box: Box, which: str) -> None:
    before = box.git("rev-parse", "develop")

    def orchestrator_commits() -> None:
        box.git("commit", "-q", "--allow-empty", "-m", "orchestrator work")

    report = _call(box, "git status", box.cwd(which), between=orchestrator_commits)
    after = box.git("rev-parse", "develop")
    assert before != after
    assert f"refs/heads/develop: {before[:12]} -> {after[:12]}" in report
    assert "may be the orchestrator" in report
    # Phase 1 never reverts.
    assert box.git("rev-parse", "develop") == after


# ---------------------------------------------------------------------------
# Fail closed on the report side
# ---------------------------------------------------------------------------

def _dropped_events(box: Box) -> list[dict]:
    return [json.loads(ln) for ln in box.dropped.read_text().splitlines() if RULE in ln]


def test_a_missing_snapshot_is_reported_not_skipped(box: Box) -> None:
    report = _report(_hook(box, "PostToolUse", "git status", box.primary, tool_use_id="never-snapshotted"))
    assert "UNVERIFIED" in report and "no readable pre-call snapshot" in report
    assert _dropped_events(box)[-1]["outcome"] == "integrity_unverified"


def test_a_payload_without_tool_use_id_is_reported(box: Box) -> None:
    pre = _hook(box, "PreToolUse", "git status", box.primary, tool_use_id="")
    assert pre and "no tool_use_id" in pre["hookSpecificOutput"]["additionalContext"]
    assert "UNVERIFIED" in _report(_hook(box, "PostToolUse", "git status", box.primary, tool_use_id=""))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_file_is_reported_not_skipped(box: Box) -> None:
    exclude = box.git_dir / "info" / "exclude"
    exclude.chmod(0)
    try:
        report = _call(box, "git status", box.primary)
    finally:
        exclude.chmod(0o644)
    assert "UNVERIFIED" in report and "info/exclude" in report and "could not read" in report


def test_a_stale_snapshot_is_pruned(box: Box) -> None:
    session_dir = box.cfg / "git-integrity" / "sess-1"
    session_dir.mkdir(parents=True)
    stale = session_dir / "old.json"
    stale.write_text("{}")
    old = time.time() - 7 * 3600
    os.utime(stale, (old, old))
    _hook(box, "PreToolUse", "git status", box.primary)
    assert not stale.exists()


def test_the_state_directory_is_private(box: Box) -> None:
    _hook(box, "PreToolUse", "git status", box.primary)
    d = box.cfg / "git-integrity" / "sess-1"
    assert d.stat().st_mode & 0o077 == 0
    assert all(f.stat().st_mode & 0o077 == 0 for f in d.iterdir())


# ---------------------------------------------------------------------------
# Channels and secrets
# ---------------------------------------------------------------------------

def test_a_change_reaches_the_subagent_user_and_the_routing_log(box: Box) -> None:
    command = f"printf '[core]\\n\\thooksPath=/tmp/x\\n' >> {_q(box.git_dir)}/config"
    _hook(box, "PreToolUse", command, box.primary)
    subprocess.run(["sh", "-c", command], check=True)
    out = _hook(box, "PostToolUse", command, box.primary)
    assert out["decision"] == "block" and "core.hooksPath" in out["reason"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "core.hooksPath" in out["hookSpecificOutput"]["additionalContext"]
    assert "git-integrity" in out["systemMessage"]
    event = _dropped_events(box)[-1]
    assert event["rule"] == RULE and event["outcome"] == "integrity_change"


def test_a_failing_command_is_still_checked_through_post_tool_use_failure(box: Box) -> None:
    command = f"printf '[core]\\n\\thooksPath=/tmp/x\\n' >> {_q(box.git_dir)}/config; exit 3"
    assert _hook(box, "PreToolUse", command, box.primary) is None
    assert subprocess.run(["sh", "-c", command]).returncode == 3
    out = _hook(box, "PostToolUseFailure", command, box.primary)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "core.hooksPath" in out["hookSpecificOutput"]["additionalContext"]
    assert "decision" not in out


def test_secret_values_are_never_reported(box: Box) -> None:
    command = (
        f"printf '[remote \"o\"]\\n\\turl = https://user:s3cr3tpw@example.invalid/x.git\\n"
        f"[http]\\n\\textraheader = Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789\\n"
        f"[credential]\\n\\thelper = store --file=/tmp/very-secret-file\\n"
        f"[user]\\n\\tname = Eve\\n' >> {_q(box.git_dir)}/config"
    )
    report = _call(box, command, box.primary)
    assert "remote.o.url" in report and "https://***@example.invalid/x.git" in report
    assert "user.name" in report and "'Eve'" in report
    for secret in ("s3cr3tpw", "abcdefghijklmnopqrstuvwxyz0123456789", "very-secret-file", "Bearer"):
        assert secret not in report, secret
        assert secret not in box.dropped.read_text(), secret
    assert "http.extraheader" in report and "credential.helper" in report  # key names are named
    assert "value not shown" in report


def test_cd_out_of_a_worktree_into_the_primary_is_covered_through_the_project_dir(box: Box) -> None:
    env = dict(box.env, CLAUDE_PROJECT_DIR=str(box.primary))
    command = f"cd {_q(box.primary)} && git config user.name sneaky"
    outside = box.root / "outside"
    outside.mkdir()
    assert _hook(box, "PreToolUse", command, outside, env=env) is None
    subprocess.run(["sh", "-c", command], check=True, env=box.env)
    assert "user.name" in _report(_hook(box, "PostToolUse", command, outside, env=env))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registered_for_pre_post_and_post_failure_on_bash() -> None:
    manifest = json.loads(HOOKS_JSON.read_text())["hooks"]
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        entries = [
            h for group in manifest.get(event, []) if group.get("matcher") == "Bash"
            for h in group["hooks"] if h["args"][-1].endswith("/routing/subagent_git_integrity_check.py")
        ]
        assert len(entries) == 1, f"{event}: expected exactly one Bash entry, found {len(entries)}"
        assert entries[0]["timeout"] <= 10
