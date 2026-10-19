# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-i93tz phase 1: the subagent shared-git-state integrity check.

``conexus/hooks/scripts/routing/subagent_git_integrity_check.py`` snapshots the
primary checkout's shared git state in PreToolUse and diffs it in PostToolUse /
PostToolUseFailure, and a main-conversation PreToolUse drains what subagents
queued. Every route below is run FOR REAL (a real ``sh -c`` against a real git
sandbox) between a real Pre and a real Post invocation of the hook script, once
with the primary as the tool cwd and once from a linked worktree. Nothing mocks
git or the filesystem; the one in-process fixture exists to SEE what the script
hands the routing log, which a subprocess cannot show.
"""
from __future__ import annotations

import importlib.util
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
PENDING = REPO_ROOT / "conexus" / "PENDING_RELEASE.md"
REGISTRY = REPO_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml"
RULE = "subagent_git_integrity_check"
SESSION = "sess-1"


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


@pytest.fixture
def tracked(box: Box) -> Box:
    """``box`` plus a bare ``origin`` the primary tracks, so upstream-tracking
    flows have something real to track. Set up BEFORE any snapshot."""
    origin = box.root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(origin)], env=box.env, check=True)
    box.git("remote", "add", "origin", str(origin))
    box.git("push", "-q", "origin", "develop")
    box.git("fetch", "-q", "origin")
    box.git("branch", "-q", "--track", "tracked", "origin/develop")
    return box


def _hook(box: Box, event: str, command: str, cwd: pathlib.Path, *, agent: bool = True,
          tool_use_id: str = "toolu_1", session: str = SESSION, env: dict[str, str] | None = None,
          umask0: bool = False):
    payload = {
        "hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": command},
        "cwd": str(cwd), "session_id": session, "tool_use_id": tool_use_id,
    }
    if agent:
        payload.update(agent_id="agent-1", agent_type="developer")
    argv = [sys.executable, str(SCRIPT)]
    if umask0:
        argv = ["sh", "-c", 'umask 0 && exec "$0" "$@"', *argv]
    proc = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, timeout=20, env=env or box.env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _text(out: dict | None) -> str:
    """The one copy of the report a Post envelope carries, or '' when it said
    nothing. Asserts the shape: a block carries ``reason`` and no
    ``additionalContext`` (not both), a failing call carries ``additionalContext``
    and no decision, and a banner rides only with a block."""
    if out is None:
        return ""
    if "decision" in out:
        assert out["decision"] == "block"
        assert "hookSpecificOutput" not in out, "reason and additionalContext must not both carry the text"
        assert out["systemMessage"]
        return out["reason"]
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] in ("PostToolUse", "PostToolUseFailure", "PreToolUse")
    assert "reason" not in out
    return hso["additionalContext"]


def _is_block(out: dict | None) -> bool:
    return bool(out) and ("decision" in out or "systemMessage" in out)


def _call_raw(box: Box, command: str, cwd: pathlib.Path, *, agent: bool = True, between=None,
              tool_use_id: str = "toolu_1", env: dict[str, str] | None = None, post_cwd: pathlib.Path | None = None):
    """Pre hook, the command for real, optional action between, Post hook; the Post envelope."""
    pre = _hook(box, "PreToolUse", command, cwd, agent=agent, tool_use_id=tool_use_id, env=env)
    assert pre is None, f"Pre must be silent on a clean snapshot: {pre}"
    rc = subprocess.run(["sh", "-c", command], cwd=cwd, env=box.env, capture_output=True).returncode
    if between:
        between()
    event = "PostToolUse" if rc == 0 else "PostToolUseFailure"
    return _hook(box, event, command, post_cwd or cwd, agent=agent, tool_use_id=tool_use_id, env=env)


def _call(box: Box, command: str, cwd: pathlib.Path, **kw) -> str:
    return _text(_call_raw(box, command, cwd, **kw))


def _drain(box: Box, session: str = SESSION, env: dict[str, str] | None = None) -> str:
    """A main-conversation PreToolUse on Bash: what the orchestrator is told."""
    out = _hook(box, "PreToolUse", "ls", box.primary, agent=False, tool_use_id="main-1", session=session, env=env)
    if out is None:
        return ""
    assert set(out) == {"hookSpecificOutput"}, out
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    return out["hookSpecificOutput"]["additionalContext"]


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


#: (id, severity, command builder given the sandbox, substrings the report must carry).
#: CHANGE routes block the subagent's call result and show a banner; INFO routes
#: reach only the orchestrator's drain.
ROUTES = [
    ("raw-config-append", "CHANGE",
     lambda b: f"printf '[core]\\n\\thooksPath=/tmp/x\\n' >> {_q(b.git_dir)}/config",
     ["config:", "core.hooksPath", "ADDED", "/tmp/x"]),
    ("hook-file-written", "CHANGE",
     lambda b: f"echo x > {_q(b.git_dir)}/hooks/pre-commit",
     ["hooks/pre-commit", "ADDED"]),
    ("variable-held-verb", "CHANGE",
     lambda b: "x=config; git $x user.name e",
     ["config:", "user.name", "ADDED", "'e'"]),
    ("scalar-style-config-writes", "CHANGE",
     lambda b: ("git config core.untrackedCache true && git config feature.manyFiles true "
                "&& git config maintenance.gc.enabled false && git config core.fsmonitor true"),
     ["core.untrackedCache", "feature.manyFiles", "maintenance.gc.enabled", "core.fsmonitor"]),
    ("sparse-checkout-init", "CHANGE",  # a real run: detected through extensions.worktreeConfig
     lambda b: "git sparse-checkout init --cone",
     ["extensions.worktreeConfig", "ADDED"]),
    ("gh-repo-set-default", "CHANGE",  # what `gh repo set-default` writes, simulated with git config
     lambda b: "git config remote.origin.gh-resolved base",
     ["remote.origin.gh-resolved", "ADDED"]),
    ("branch-M-of-the-checked-out-branch", "INFO",
     lambda b: "git branch -M develop renamed",
     ["HEAD:", "refs/heads/renamed", "may be the orchestrator"]),
    ("update-ref", "INFO",
     lambda b: f"git update-ref refs/heads/develop {b.base_sha}",
     ["refs/heads/develop:", "may be the orchestrator"]),
    ("info-exclude-write", "CHANGE",
     lambda b: f"echo secret-pattern >> {_q(b.git_dir)}/info/exclude",
     ["info/exclude: changed", "secret-pattern"]),
    ("info-attributes-write", "CHANGE",
     lambda b: f"echo '* filter=evil' >> {_q(b.git_dir)}/info/attributes",
     ["info/attributes: created", "filter=evil"]),
    ("write-into-the-core.hooksPath-directory", "CHANGE",
     _hooks_path_setup,
     ["core.hooksPath dir", "pre-push", "ADDED"]),
]


def test_the_route_census_does_not_shrink() -> None:
    assert len(ROUTES) == 11 and len({r[0] for r in ROUTES}) == 11
    assert {r[1] for r in ROUTES} == {"CHANGE", "INFO"}


@pytest.mark.parametrize("which", ["primary", "worktree"])
@pytest.mark.parametrize("route", ROUTES, ids=[r[0] for r in ROUTES])
def test_every_route_is_detected(box: Box, which: str, route) -> None:
    _, severity, build, needles = route
    out = _call_raw(box, build(box), box.cwd(which))
    drained = _drain(box)
    seen = _text(out) + "\n" + drained
    missing = [n for n in needles if n.lower() not in seen.lower()]
    assert not missing, f"missing {missing} in:\n{seen}"
    if severity == "CHANGE":
        assert _is_block(out), f"a CHANGE route must block with a banner: {out}"
        assert "[CHANGE]" in drained, drained
    else:
        assert out is None, f"an INFO route must not block, banner or speak to the subagent: {out}"
        assert "[INFO]" in drained and "[CHANGE]" not in drained, drained


@pytest.mark.skipif(
    shutil.which("scalar") is None,
    reason="the scalar binary is not installed; the same keys are pinned without the binary by the "
           "scalar-style-config-writes route, so this skip leaves scalar's writes examined",
)
@pytest.mark.parametrize("which", ["primary", "worktree"])
def test_real_scalar_register_is_detected(box: Box, which: str) -> None:
    """The real binary, where it exists (git ships it from 2.38)."""
    # scalar records the enlistment in the GLOBAL config, so it needs a writable one.
    (box.root / "global.gitconfig").write_text("")
    box.env["GIT_CONFIG_GLOBAL"] = str(box.root / "global.gitconfig")
    report = _call(box, f"scalar register --no-maintenance {_q(box.primary)}", box.cwd(which))
    # core.fsmonitor is set only where scalar finds an fsmonitor daemon (macOS and
    # Windows, not the Linux CI runners), so assert keys scalar writes everywhere.
    for key in ("core.untrackedCache", "feature.manyFiles", "gc.auto"):
        assert key in report, f"{key} missing from:\n{report or '(silent)'}"


# ---------------------------------------------------------------------------
# A: the orchestrator is told, through the drain
# ---------------------------------------------------------------------------

def test_a_subagent_finding_reaches_the_orchestrator_once(box: Box) -> None:
    command = f"printf '[core]\\n\\thooksPath=/tmp/x\\n' >> {_q(box.git_dir)}/config"
    assert _call(box, command, box.primary)
    first = _drain(box)
    assert "core.hooksPath" in first and "[CHANGE]" in first and "developer (agent-1)" in first
    assert _drain(box) == "", "a drained finding must not be delivered twice"
    assert not list((box.cfg / "git-integrity" / SESSION).glob("findings.jsonl*"))


def test_the_drain_is_session_scoped_and_pre_tool_use_only(box: Box) -> None:
    _call(box, f"echo x > {_q(box.git_dir)}/hooks/pre-commit", box.primary)
    assert _drain(box, session="some-other-session") == ""
    # A main-conversation PostToolUse does not claim it.
    assert _hook(box, "PostToolUse", "ls", box.primary, agent=False, tool_use_id="m2") is None
    assert "hooks/pre-commit" in _drain(box)


def test_two_subagents_both_reach_the_orchestrator(box: Box) -> None:
    _call(box, f"echo a > {_q(box.git_dir)}/hooks/one", box.primary, tool_use_id="t1")
    _call(box, f"echo b > {_q(box.git_dir)}/hooks/two", box.primary, tool_use_id="t2")
    drained = _drain(box)
    assert "hooks/one" in drained and "hooks/two" in drained and "2 finding(s)" in drained


def test_the_findings_file_and_state_directories_are_private(box: Box) -> None:
    command = f"echo x > {_q(box.git_dir)}/hooks/pre-commit"
    _hook(box, "PreToolUse", command, box.primary, umask0=True)
    subprocess.run(["sh", "-c", command], check=True)
    _hook(box, "PostToolUse", command, box.primary, umask0=True)
    findings = box.cfg / "git-integrity" / SESSION / "findings.jsonl"
    assert findings.exists() and findings.stat().st_mode & 0o777 == 0o600
    for d in (box.cfg / "git-integrity", box.cfg / "git-integrity" / SESSION):
        assert d.stat().st_mode & 0o777 == 0o700, f"{d} is {oct(d.stat().st_mode & 0o777)}"


def test_the_main_conversation_fast_path_imports_nothing_it_does_not_need(box: Box) -> None:
    """With nothing pending the main-conversation call must stay at bare-interpreter cost."""
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(box.primary),
               "session_id": SESSION, "tool_use_id": "m"}
    # This test reads the child's own import list, and the storage-touch guard's child shim
    # (tests/_storage_touch_shim/sitecustomize.py, on PYTHONPATH in every default run) imports
    # json itself. The hook script touches no storage, so leave the shim out of this one child.
    from tests._storage_touch_guard import CHILD_LOG_ENV, SHIM_DIR

    env = {k: v for k, v in box.env.items() if k != CHILD_LOG_ENV}
    kept = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and os.path.realpath(p) != str(SHIM_DIR)]
    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        env.pop("PYTHONPATH", None)
    proc = subprocess.run([sys.executable, "-X", "importtime", str(SCRIPT)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0 and proc.stdout == ""
    imported = {ln.rsplit("|", 1)[-1].strip() for ln in proc.stderr.splitlines() if ln.startswith("import time:")}
    assert imported, "importtime printed nothing: the check below would be vacuous"
    assert not imported & {"json", "hashlib", "_lib", "_endpoint_resolve"}, sorted(imported)


def test_the_fast_path_resolves_the_same_config_dir_as_the_shared_helper(monkeypatch, tmp_path) -> None:
    mod = _load_module(monkeypatch)
    for env in ({"NEXUS_CONFIG_DIR": str(tmp_path / "a")}, {"NX_CONFIG_DIR": str(tmp_path / "b")}, {}):
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.delenv("NX_CONFIG_DIR", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        assert mod._config_base() == str(mod._lib._default_config_dir()), env


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
    assert _drain(box) == ""


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
    assert _text(_hook(box, "PostToolUse", "ls", outside)) == ""


# ---------------------------------------------------------------------------
# C: no false reports on routine flows
# ---------------------------------------------------------------------------

def _no_block_no_banner(out: dict | None) -> None:
    assert out is None, f"a routine flow must say nothing to the subagent or the user: {out}"


@pytest.mark.parametrize("which", ["primary", "worktree"])
def test_routine_tracking_flows_are_info_only(tracked: Box, which: str) -> None:
    b = tracked
    new_wt = b.root / "wt2"
    flows = [
        f"git worktree add -q -b feature-x {_q(new_wt)} origin/develop",
        "git push -q -u origin wt",
        "git branch -q --set-upstream-to=origin/develop wt",
        "git branch -q -D tracked",
        "git checkout -q -b feature-y origin/develop",
        # What `gh pr checkout` and editors write beside a branch.
        "git config branch.wt.gh-merge-base develop && git config branch.wt.description d"
        " && git config branch.wt.vscode-merge-base origin/develop",
    ]
    infos = 0
    for n, command in enumerate(flows):
        cwd = b.cwd(which)
        if command.startswith("git push") and which == "primary":
            command = "git push -q -u origin develop"
        if command.startswith("git checkout") and which == "primary":
            command = "git branch -q --track feature-y origin/develop"
        out = _call_raw(b, command, cwd, tool_use_id=f"flow{n}")
        _no_block_no_banner(out)
        drained = _drain(b)
        assert "[CHANGE]" not in drained, f"{command}\n{drained}"
        infos += "[INFO]" in drained
    assert infos >= 3, "the info lines must reach the orchestrator (a flow that wrote nothing would be vacuous)"


def test_a_core_change_beside_a_tracking_write_still_blocks(tracked: Box) -> None:
    out = _call_raw(tracked, "git branch -q --set-upstream-to=origin/develop wt && git config core.editor vi", tracked.worktree)
    text = _text(out)
    assert _is_block(out) and "core.editor" in text and "branch.wt.remote" in text
    assert "[CHANGE]" in text and "[INFO]" in text


def test_a_concurrent_orchestrator_commit_is_info_not_a_change(box: Box) -> None:
    before = box.git("rev-parse", "develop")
    out = _call_raw(box, "git status", box.worktree,
                    between=lambda: box.git("commit", "-q", "--allow-empty", "-m", "orchestrator work"))
    after = box.git("rev-parse", "develop")
    assert before != after
    _no_block_no_banner(out)
    drained = _drain(box)
    assert f"refs/heads/develop: {before[:12]} -> {after[:12]}" in drained
    assert "may be the orchestrator" in drained and "[CHANGE]" not in drained
    assert box.git("rev-parse", "develop") == after  # phase 1 never reverts


def test_a_commit_in_an_unrelated_repository_is_silent(box: Box) -> None:
    scratch = box.root / "scratch"
    scratch.mkdir()
    for args in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", *args], cwd=scratch, env=box.env, check=True)
    env = dict(box.env, CLAUDE_PROJECT_DIR=str(box.primary))
    out = _call_raw(box, "git commit -q --allow-empty -m scratch && git config user.name scratch", scratch, env=env)
    _no_block_no_banner(out)
    assert _drain(box, env=env) == ""
    assert not list((box.cfg / "git-integrity").rglob("*.json")), "no snapshot may leak"


def test_the_project_repository_is_covered_from_any_cwd(box: Box) -> None:
    outside = box.root / "outside"
    outside.mkdir()
    env = dict(box.env, CLAUDE_PROJECT_DIR=str(box.primary))
    command = f"cd {_q(box.primary)} && git config user.name sneaky"
    assert "user.name" in _call(box, command, outside, env=env)


def test_cwd_drift_between_pre_and_post_is_neither_a_false_report_nor_a_missed_one(box: Box) -> None:
    outside = box.root / "outside"
    outside.mkdir()
    # No change: Post runs from a directory that is in no repository.
    assert _call_raw(box, "git status", box.worktree, post_cwd=outside, tool_use_id="d1") is None
    assert _drain(box) == ""
    # A real change, Post from elsewhere: still compared against the stored roots.
    out = _call_raw(box, "git config user.name drift", box.worktree, post_cwd=outside, tool_use_id="d2")
    assert "user.name" in _text(out) and "git directory is gone" not in _text(out)
    # And Post from some other repository.
    other = box.root / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, env=box.env, check=True)
    assert _call_raw(box, "git status", box.primary, post_cwd=other, tool_use_id="d3") is None


def test_a_post_that_finds_no_repository_still_consumes_the_snapshot(box: Box) -> None:
    outside = box.root / "outside"
    outside.mkdir()
    _hook(box, "PreToolUse", "ls", box.primary, tool_use_id="leak")
    assert list((box.cfg / "git-integrity" / SESSION).glob("leak.json"))
    assert _hook(box, "PostToolUse", "ls", outside, tool_use_id="leak") is None
    assert not list((box.cfg / "git-integrity").rglob("leak.json"))


def _coverage_notes(box: Box) -> list[dict]:
    if not box.dropped.exists():
        return []
    return [json.loads(ln) for ln in box.dropped.read_text().splitlines()
            if RULE in ln and "integrity_coverage_note" in ln]


def test_a_reftable_repository_is_a_coverage_note_not_a_report(box: Box) -> None:
    (box.git_dir / "reftable").mkdir()  # the marker the script keys on
    for n in range(2):
        out = _call_raw(box, "git status", box.worktree, tool_use_id=f"rt{n}",
                        between=lambda: (box.git_dir / "refs" / "heads" / "develop").write_text(box.base_sha + "\n"))
        _no_block_no_banner(out)
    assert _drain(box) == ""
    assert len(_coverage_notes(box)) == 1, "one note per session, not one per call"


def test_a_real_reftable_repository_is_silent(tmp_path: pathlib.Path) -> None:
    env = _clean_env(tmp_path)
    primary = tmp_path / "rt"
    primary.mkdir()
    probe = subprocess.run(["git", "init", "-q", "--ref-format=reftable", "-b", "develop"], cwd=primary, env=env,
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"this git cannot create a reftable repository ({probe.stderr.strip()[:80]}); "
                    "the marker-based test above pins the same branch of the script")
    (primary / "f").write_text("1")
    subprocess.run(["git", "add", "f"], cwd=primary, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=primary, env=env, check=True)
    shell = Box(root=tmp_path, primary=primary, worktree=primary, env=env, cfg=tmp_path / "cfg",
                dropped=tmp_path / "dropped.jsonl", base_sha="")
    out = _call_raw(shell, "git commit -q --allow-empty -m two", primary)
    _no_block_no_banner(out)
    assert _drain(shell) == "" and len(_coverage_notes(shell)) == 1


def test_a_hooks_directory_over_the_cap_is_a_coverage_note_not_a_change(box: Box) -> None:
    for n in range(305):
        (box.git_dir / "hooks" / f"sample-{n:03d}").write_text("x")
    for n in range(2):
        _no_block_no_banner(_call_raw(box, "git status", box.worktree, tool_use_id=f"cap{n}"))
    assert _drain(box) == ""
    notes = _coverage_notes(box)
    assert len(notes) == 1 and notes[0]["outcome"] == "integrity_coverage_note"


# ---------------------------------------------------------------------------
# Fail closed on the report side
# ---------------------------------------------------------------------------

def _dropped_events(box: Box) -> list[dict]:
    return [json.loads(ln) for ln in box.dropped.read_text().splitlines() if RULE in ln]


def test_a_missing_snapshot_is_reported_not_skipped(box: Box) -> None:
    out = _hook(box, "PostToolUse", "git status", box.primary, tool_use_id="never-snapshotted")
    report = _text(out)
    assert "UNVERIFIED" in report and "no readable pre-call snapshot" in report
    assert not _is_block(out), "an unverifiable call is reported, not blocked"
    assert _dropped_events(box)[-1]["outcome"] == "integrity_unverified"
    assert "UNVERIFIED" in _drain(box)


def test_a_payload_without_tool_use_id_is_reported(box: Box) -> None:
    pre = _hook(box, "PreToolUse", "git status", box.primary, tool_use_id="")
    assert pre and "tool_use_id" in pre["hookSpecificOutput"]["additionalContext"]
    assert "UNVERIFIED" in _text(_hook(box, "PostToolUse", "git status", box.primary, tool_use_id=""))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_file_is_reported_not_skipped(box: Box) -> None:
    exclude = box.git_dir / "info" / "exclude"
    exclude.chmod(0)
    try:
        report = _call(box, "git status", box.primary)
    finally:
        exclude.chmod(0o644)
    assert "UNVERIFIED" in report and "info/exclude" in report and "could not read" in report


def test_a_stale_snapshot_is_pruned_but_a_pending_finding_is_not(box: Box) -> None:
    session_dir = box.cfg / "git-integrity" / SESSION
    session_dir.mkdir(parents=True)
    stale, findings = session_dir / "old.json", session_dir / "findings.jsonl"
    stale.write_text("{}")
    findings.write_text("")
    old = time.time() - 7 * 3600
    for p in (stale, findings):
        os.utime(p, (old, old))
    _hook(box, "PreToolUse", "git status", box.primary)
    assert not stale.exists()
    assert findings.exists(), "a queued finding must outlive the six-hour snapshot rule"


def test_old_session_directories_are_reached_however_many_there_are(box: Box) -> None:
    base = box.cfg / "git-integrity"
    base.mkdir(parents=True)
    old = time.time() - 3 * 86400
    for n in range(120):  # far more than the old 50-entry scan window
        d = base / f"fresh-{n:03d}"
        d.mkdir()
    for name in ("aaa-old", "zzz-old", "mmm-old"):
        d = base / name
        d.mkdir()
        (d / "x.json").write_text("{}")
        os.utime(d, (old, old))
    _hook(box, "PreToolUse", "git status", box.primary)
    left = {p.name for p in base.iterdir()}
    assert not left & {"aaa-old", "zzz-old", "mmm-old"}, left
    assert sum(n.startswith("fresh-") for n in left) == 120
    # Bounded: the cross-session scan runs at most once an hour.
    late = base / "late-old"
    late.mkdir()
    os.utime(late, (old, old))
    _hook(box, "PreToolUse", "git status", box.primary, tool_use_id="again")
    assert late.exists()


# ---------------------------------------------------------------------------
# D: ids that name the parent directory
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["..", ".", "...", ""])
def test_a_dotted_or_empty_session_id_cannot_reach_the_config_directory(box: Box, bad: str) -> None:
    box.cfg.mkdir(parents=True, exist_ok=True)
    victim = box.cfg / "stale-config-file.json"
    victim.write_text("{}")
    old = time.time() - 30 * 3600
    os.utime(victim, (old, old))
    pre = _hook(box, "PreToolUse", "git status", box.primary, session=bad)
    assert victim.exists(), "a session id of dots must never make the pruner touch the config directory"
    assert pre and "session_id" in pre["hookSpecificOutput"]["additionalContext"]
    assert not (box.cfg / "git-integrity").exists() or not list((box.cfg / "git-integrity").rglob("*.json"))
    post = _hook(box, "PostToolUse", "git status", box.primary, session=bad)
    assert "UNVERIFIED" in _text(post)
    assert victim.exists()


@pytest.mark.parametrize("bad", ["..", "."])
def test_a_dotted_tool_use_id_is_refused(box: Box, bad: str) -> None:
    pre = _hook(box, "PreToolUse", "git status", box.primary, tool_use_id=bad)
    assert pre and "tool_use_id" in pre["hookSpecificOutput"]["additionalContext"]
    assert not list((box.cfg / "git-integrity").rglob("*.json"))


# ---------------------------------------------------------------------------
# Channels and secrets
# ---------------------------------------------------------------------------

def test_a_change_reaches_the_subagent_user_orchestrator_and_the_routing_log(box: Box) -> None:
    command = f"printf '[core]\\n\\thooksPath=/tmp/x\\n' >> {_q(box.git_dir)}/config"
    _hook(box, "PreToolUse", command, box.primary)
    subprocess.run(["sh", "-c", command], check=True)
    out = _hook(box, "PostToolUse", command, box.primary)
    assert out["decision"] == "block" and "core.hooksPath" in out["reason"]
    assert "hookSpecificOutput" not in out, "one copy of the text"
    assert "git-integrity" in out["systemMessage"]
    assert "core.hooksPath" in _drain(box)
    event = _dropped_events(box)[-1]
    assert event["rule"] == RULE and event["outcome"] == "integrity_change"


def test_a_failing_command_is_still_checked_through_post_tool_use_failure(box: Box) -> None:
    command = f"printf '[core]\\n\\thooksPath=/tmp/x\\n' >> {_q(box.git_dir)}/config; exit 3"
    assert _hook(box, "PreToolUse", command, box.primary) is None
    assert subprocess.run(["sh", "-c", command]).returncode == 3
    out = _hook(box, "PostToolUseFailure", command, box.primary)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "core.hooksPath" in out["hookSpecificOutput"]["additionalContext"]
    assert "decision" not in out and "reason" not in out
    assert "core.hooksPath" in _drain(box)


SECRET_CONFIG = [
    ("url.https://x-access-token:ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@github.com/.insteadOf", "https://github.com/"),
    ("credential.https://tok:sekrit@host.username", "u"),
    ("remote.origin.url", "https://h/r.git?access_token=abc123secret99"),
    ("remote.second.url", "https://us/er:pa/ss@host/r"),
    ("alias.x", '!curl -H "Authorization: token abcd1234"'),
]
SECRETS = [
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "x-access-token", "sekrit", "tok:", "abc123secret99",
    "access_token", "pa/ss", "er:pa", "abcd1234", "Authorization", "curl",
]


def _write_secret_config(box: Box) -> None:
    for key, value in SECRET_CONFIG:
        box.git("config", key, value)


def _no_secret(text: str, where: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked into {where}:\n{text}"


def _load_module(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("sgic_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_secrets_in_key_names_and_values_are_never_reported(box: Box) -> None:
    out = _call_raw(box, "true", box.primary, between=lambda: _write_secret_config(box))
    report, drained = _text(out), _drain(box)
    for where, text in (("report", report), ("systemMessage", out["systemMessage"]), ("drained context", drained)):
        _no_secret(text, where)
    for needle in ("url.<redacted>.insteadOf", "credential.<redacted>.username", "https://h/r.git?<removed>",
                   "https://***@host/r", "alias.x"):
        assert needle in report, f"{needle!r} missing from:\n{report}"
    assert "value not shown" in report
    _no_secret(box.dropped.read_text() if box.dropped.exists() else "", "the dropped-writes log")


def test_no_state_file_holds_a_secret_either(box: Box) -> None:
    _write_secret_config(box)  # present at snapshot time
    _hook(box, "PreToolUse", "true", box.primary)
    files = [p for p in box.cfg.rglob("*") if p.is_file()]
    assert files, "the check below would be vacuous"
    for p in files:
        _no_secret(p.read_text(), str(p))


def test_the_routing_log_record_carries_no_secret_and_is_not_empty(box: Box, monkeypatch, capsys) -> None:
    """The log record's ``command_fragment`` is examined directly: the dropped-writes
    record omits it, so a file assertion on that log proves nothing."""
    mod = _load_module(monkeypatch)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(box.cfg))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(box.primary))
    events: list[dict] = []
    monkeypatch.setattr(mod._lib, "log_routing_event", lambda **kw: events.append(kw))
    base = {"tool_name": "Bash", "tool_input": {"command": "true"}, "cwd": str(box.primary),
            "session_id": SESSION, "tool_use_id": "inproc", "agent_id": "a1", "agent_type": "developer"}
    mod.body({**base, "hook_event_name": "PreToolUse"})
    _write_secret_config(box)
    mod.body({**base, "hook_event_name": "PostToolUse"})
    assert events, "the script logged nothing: this test would be vacuous"
    fragments = [e["command_fragment"] for e in events if e.get("command_fragment")]
    assert fragments and any("alias.x" in f for f in fragments), events
    for e in events:
        _no_secret(json.dumps(e, default=str), "a log_routing_event call")
    _no_secret(capsys.readouterr().out, "stdout")


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
    assert "user.name" in _text(_hook(box, "PostToolUse", command, outside, env=env))


# ---------------------------------------------------------------------------
# E: the stated limits are the true ones
# ---------------------------------------------------------------------------

def test_the_documented_gaps_are_real_gaps(box: Box) -> None:
    """Each limit PENDING_RELEASE and the docstring state is pinned as a limit,
    so wording that claims coverage fails here when the behaviour changes."""
    for command, why in (
        (f"git -C {_q(box.primary)} add -A", "the primary's index"),
        ("git sparse-checkout set --cone .", "a linked worktree's own config.worktree"),
    ):
        (box.primary / "new-file").write_text("x")
        out = _call_raw(box, command, box.worktree, tool_use_id=why.replace(" ", "-"))
        text = _text(out) + _drain(box)
        # sparse-checkout set may also add extensions.worktreeConfig once; that
        # shared-config key is a legitimate detection, the worktree-local part is not.
        assert "config.worktree" not in text, (why, text)
        # The box's own path is in the report; hellmini's tmp dir is tmp.noindex.
        named = text
        for root in {str(box.root.resolve()), str(box.root)}:
            named = named.replace(root, "<box>")
        assert "index" not in named.lower(), (why, text)
    hooks_sub = box.git_dir / "hooks" / "sub"
    hooks_sub.mkdir()
    (hooks_sub / "inner").write_text("1")
    out = _call_raw(box, "true", box.primary, between=lambda: (hooks_sub / "inner").write_text("2"), tool_use_id="sub")
    assert _text(out) == "", "hooks/ subdirectories are documented as not hashed"


def _docs() -> dict[str, str]:
    pending = next(ln for ln in PENDING.read_text().splitlines() if "(nexus-i93tz): phase 1" in ln)
    docstring = SCRIPT.read_text().split('"""')[1]
    registry = REGISTRY.read_text().split("subagent_git_integrity_check:")[1]
    return {"pending": pending, "docstring": docstring, "registry": registry}


def test_the_wording_states_the_limits_and_makes_no_false_claim() -> None:
    docs = _docs()
    for where, text in docs.items():
        low = " ".join(text.lower().split())
        assert "cannot reach the parent" in low, where
        assert "next bash call" in low, where
        assert "nothing documented carries" not in low, where
    for where in ("pending", "docstring"):
        low = " ".join(docs[where].lower().split())
        for limit in ("index", "backgrounded", "not been measured", "config.worktree", "top level"):
            assert limit in low, f"{where} must state the limit {limit!r}"
    assert "Agent" in docs["docstring"] and "not built" in docs["docstring"]
    assert "two launches" in " ".join(docs["pending"].lower().split())


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
