# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qo7ie.36: ``destructive_action_advice``, the table-driven advise-mode PreToolUse hook.

``conexus/hooks/scripts/routing/destructive_action_advice.py`` carries the machine-checkable
bright lines of the destructive-action guard rules as rows of one table. These tests pin, per
row, a command that must match, a near-miss that must not, and the advice text naming the rule;
that the hook only advises (no permission decision) while ``MODE`` is ``"advise"``; that the deny
path exists and works when the constant is flipped; and that a call matching no row never
imports the ``_lib`` framework.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "destructive_action_advice.py"

_spec = importlib.util.spec_from_file_location("destructive_action_advice", SCRIPT)
assert _spec and _spec.loader
daa = importlib.util.module_from_spec(_spec)
sys.modules["destructive_action_advice"] = daa
_spec.loader.exec_module(daa)


def _payload(command: str, tool: str = "Bash", cwd: str | None = None, **tool_input) -> dict:
    return {
        "tool_name": tool,
        "tool_input": {"command": command, **tool_input},
        "cwd": cwd or str(REPO_ROOT),
        "session_id": "daa-test",
    }


def _ids(command: str, **kw) -> list[str]:
    extra = kw.pop("tool_input", {})
    return [h.row.id for h in daa.decide(_payload(command, **kw, **extra))]


# (row id, label, command, extra tool_input). One match and one near-miss per row at least.
MATCHES: list[tuple[str, str, str, dict]] = [
    ("pattern_kill", "pkill", "pkill -f pytest", {}),
    ("pattern_kill", "killall", "killall -9 python", {}),
    ("pattern_kill", "kill-pgrep-subst", "kill -9 $(pgrep -f lab-remote)", {}),
    ("pattern_kill", "kill-backticks", "kill `pgrep -f lab-remote`", {}),
    ("pattern_kill", "kill-ps-subst", "kill $(ps aux | grep x | awk '{print $2}')", {}),
    ("pattern_kill", "xargs-kill-after-ps", "ps aux | grep lab-remote | awk '{print $2}' | xargs kill", {}),
    ("pattern_kill", "wrapped-ssh", "ssh qwent 'pkill -f pytest'", {}),
    ("pattern_kill", "wrapped-lab-remote", "lab-remote --host qwent -- killall python", {}),
    ("pattern_kill", "env-assign-prefix", "FOO=1 sudo pkill -f x", {}),
    ("branch_bulk_delete", "census-example", "git branch --list 'worktree-agent-*' | xargs git branch -D | wc -l", {}),
    ("branch_bulk_delete", "glob-target", "git branch -D 'worktree-agent-*'", {}),
    ("branch_bulk_delete", "xargs-no-pipe-to-counter", "git branch --list 'x-*' | xargs git branch -d", {}),
    ("branch_bulk_delete", "names-from-substitution", "git branch -D $(git branch --list 'x-*')", {}),
    ("branch_bulk_delete", "output-to-tail", "git branch -D a1 2>&1 | tail -1", {}),
    ("branch_bulk_delete", "output-to-devnull", "git branch -D a1 > /dev/null", {}),
    ("branch_bulk_delete", "long-delete-flag", "git -C /tmp/r branch --delete 'old-*'", {}),
    ("stash_with_worktrees", "census-example", "git stash -- scripts/ci_status.py && pytest -x && git stash pop", {}),
    ("stash_with_worktrees", "bare", "git stash", {}),
    ("stash_with_worktrees", "push", "git stash push -m wip", {}),
    ("stash_with_worktrees", "apply", "git stash apply", {}),
    ("bg_pipe_masks_exit", "census-example", "bash run.sh --guided 2>&1 | tail -15", {"run_in_background": True}),
    ("bg_pipe_masks_exit", "trailing-amp", "bash run.sh 2>&1 | tail -5 &", {}),
    ("bg_pipe_masks_exit", "nohup", "nohup bash run.sh 2>&1 | grep -v noise", {}),
    ("bg_pipe_masks_exit", "tee-last", "uv run pytest tests 2>&1 | tee out.log", {"run_in_background": True}),
    ("whole_disk_scan", "grep-home", 'grep -rn "sk-ant" ~', {}),
    ("whole_disk_scan", "ssh-find-windows-profile", """ssh qwent 'find /mnt/c/Users/sam -name "*.log"'""", {}),
    ("whole_disk_scan", "find-root", "find / -name '*.pyc'", {}),
    ("whole_disk_scan", "find-home-deep", "find $HOME -maxdepth 3 -name x", {}),
    ("whole_disk_scan", "rg-users", "rg token /Users/sam", {}),
    ("whole_disk_scan", "grep-volumes", "grep -R foo /Volumes/SanHell", {}),
    ("whole_disk_scan", "mdfind", "mdfind kMDItemFSName=foo", {}),
    ("shared_glob_delete", "tmp-prefix-glob", "rm -rf /tmp/nexus_t2_substrate_pg_*", {}),
    ("shared_glob_delete", "find-mtime-pytest-of", "find $TMPDIR/pytest-of-sam -newer stamp -exec rm -rf {} +", {}),
    ("shared_glob_delete", "tmp-star", "rm -rf /tmp/*", {}),
    ("shared_glob_delete", "pytest-basetemp-glob", "rm -rf $TMPDIR/pytest-of-sam/pytest-*", {}),
    ("shared_glob_delete", "macos-user-tmpdir", "rm -rf /var/folders/57/abc/T/nexus_*", {}),
    ("shared_glob_delete", "find-delete-mtime", "find /tmp -mtime +1 -delete", {}),
    ("hook_verb_by_hand", "session_start-call", """uv run python -c "from nexus.hooks import session_start; session_start('trace')" """, {}),
    ("hook_verb_by_hand", "nx-hook", "uv run nx-hook session-start", {}),
    ("hook_verb_by_hand", "entry-main", "python -m nexus._hook_runtime.entry session-start", {}),
    ("hook_verb_by_hand", "home-only-is-not-enough", "HOME=$S uv run nx-hook session-start", {}),
]

NEAR_MISSES: list[tuple[str, str, str, dict]] = [
    ("pattern_kill", "literal-pid", "kill 48213", {}),
    ("pattern_kill", "own-bg-pid", "sleep 30 & kill $!", {}),
    ("pattern_kill", "pgrep-read", "pgrep -fl pytest", {}),
    ("pattern_kill", "grep-for-the-word", "grep -n pkill CLAUDE.md", {}),
    ("pattern_kill", "man-page", "man pkill", {}),
    ("pattern_kill", "xargs-kill-without-reader", "cat pids.txt | xargs kill", {}),
    ("branch_bulk_delete", "one-named-branch", "git branch -D worktree-agent-a1b2c3", {}),
    ("branch_bulk_delete", "list-read", "git branch --list 'worktree-agent-*'", {}),
    ("branch_bulk_delete", "merged-delete", "git branch -d feature/x", {}),
    ("branch_bulk_delete", "stderr-only-devnull", "git branch -D a1 2>/dev/null", {}),
    ("branch_bulk_delete", "several-named", "git branch -D a1 b2 c3", {}),
    ("stash_with_worktrees", "list", "git stash list", {}),
    ("stash_with_worktrees", "show", "git stash show -p", {}),
    ("stash_with_worktrees", "show-file-copy", "git show HEAD:scripts/ci_status.py > scripts/ci_status.py", {}),
    ("bg_pipe_masks_exit", "census-allowed-redirect", "bash run.sh --guided > $OUT/run.log 2>&1; ec=$?; exit $ec", {"run_in_background": True}),
    ("bg_pipe_masks_exit", "foreground-pipe", "git log | head", {}),
    ("bg_pipe_masks_exit", "bg-without-pipe", "bash run.sh --guided", {"run_in_background": True}),
    ("bg_pipe_masks_exit", "bg-redirect-then-pipe", "bash run.sh > run.log 2>&1 | tail -3", {"run_in_background": True}),
    ("bg_pipe_masks_exit", "foreground-2>&1-pipe", "uv run pytest tests/hooks 2>&1 | tail -20", {}),
    ("whole_disk_scan", "scoped-find", "find ~/git/nexus/src -name '*.py'", {}),
    ("whole_disk_scan", "depth-bounded-tmp", "find /tmp/x-123 -maxdepth 2 -name '*.log'", {}),
    ("whole_disk_scan", "depth-bounded-home", "find ~ -maxdepth 1 -name x", {}),
    ("whole_disk_scan", "grep-not-recursive", "grep foo ~/notes.txt", {}),
    ("whole_disk_scan", "grep-r-scoped", "grep -rn foo src/", {}),
    ("whole_disk_scan", "rg-scoped", "rg token ~/git/nexus/src", {}),
    ("whole_disk_scan", "mdfind-onlyin", "mdfind -onlyin ~/git/nexus foo", {}),
    ("shared_glob_delete", "private-dir-glob", "rm -rf /tmp/claude-501/s1/scratchpad/out/*", {}),
    ("shared_glob_delete", "named-private-tree", "rm -rf ~/tmp/dev-a/run42", {}),
    ("shared_glob_delete", "not-recursive", "rm /tmp/x_*", {}),
    ("shared_glob_delete", "find-private-dir", "find /tmp/claude-501/s1 -mtime +1 -delete", {}),
    ("shared_glob_delete", "find-without-time-test", "find /tmp -name x -delete", {}),
    ("hook_verb_by_hand", "pytest-fence", "uv run pytest tests/hooks", {}),
    ("hook_verb_by_hand", "side-effect-free-renderer", """uv run python -c "from nexus.hooks import render_session_start as r; print(len(r('x')))" """, {}),
    ("hook_verb_by_hand", "scratch-home-and-config", "HOME=$S NEXUS_CONFIG_DIR=$S/cfg uv run nx-hook session-start", {}),
    ("hook_verb_by_hand", "grep-for-the-word", "grep -rn nx-hook src", {}),
]


@pytest.mark.parametrize(("row", "label", "command", "extra"), MATCHES, ids=lambda v: v if isinstance(v, str) else "")
def test_row_matches(row: str, label: str, command: str, extra: dict) -> None:
    assert row in _ids(command, tool_input=extra), f"{row}/{label}: {command!r} must match"


@pytest.mark.parametrize(("row", "label", "command", "extra"), NEAR_MISSES, ids=lambda v: v if isinstance(v, str) else "")
def test_near_miss_does_not_match(row: str, label: str, command: str, extra: dict) -> None:
    assert row not in _ids(command, tool_input=extra), f"{row}/{label}: {command!r} must NOT match"


# ---- the two rows that need a real machine state --------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "primary"
    r.mkdir()
    _git("init", "-q", "-b", "main", cwd=r)
    _git("config", "user.email", "t@example.com", cwd=r)
    _git("config", "user.name", "t", cwd=r)
    (r / "a.txt").write_text("hello\n")
    _git("add", "a.txt", cwd=r)
    _git("commit", "-qm", "init", cwd=r)
    return r


STASH_POP = "git stash pop"


def test_stash_advises_only_when_the_repository_has_more_than_one_worktree(repo: Path, tmp_path: Path) -> None:
    assert "stash_with_worktrees" not in _ids(STASH_POP, cwd=str(repo)), "one worktree: nothing shared"
    _git("worktree", "add", "-q", "-b", "feature/x", str(tmp_path / "wt"), cwd=repo)
    assert "stash_with_worktrees" in _ids(STASH_POP, cwd=str(repo)), "primary of a repo with a linked worktree"
    assert "stash_with_worktrees" in _ids(STASH_POP, cwd=str(tmp_path / "wt")), "the linked worktree itself"
    assert "stash_with_worktrees" not in _ids("git stash list", cwd=str(repo)), "read forms stay quiet"


def test_stash_is_quiet_when_the_worktree_state_is_undeterminable(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert "stash_with_worktrees" not in _ids(STASH_POP, cwd=str(not_a_repo))


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("pgrep") is None, reason="needs pgrep and ps")
def test_running_script_edit_advises_only_for_a_script_a_live_shell_runs(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash, "bash is the interpreter under test"
    running = tmp_path / "daa_running_probe.sh"
    running.write_text("#!/bin/bash\nsleep 60\n")
    idle = tmp_path / "daa_idle_probe.sh"
    idle.write_text("#!/bin/bash\ntrue\n")
    proc = subprocess.Popen([bash, str(running)])
    try:
        def edit_ids(path: Path, tool: str = "Edit") -> list[str]:
            payload = {"tool_name": tool, "tool_input": {"file_path": str(path)}, "cwd": str(tmp_path)}
            return [h.row.id for h in daa.decide(payload)]

        hits = daa.decide({"tool_name": "Edit", "tool_input": {"file_path": str(running)}, "cwd": str(tmp_path)})
        assert [h.row.id for h in hits] == ["running_script_edit"]
        assert str(proc.pid) in hits[0].note, "the advice names the pid"
        assert edit_ids(running, "Write") == ["running_script_edit"]
        assert edit_ids(running, "MultiEdit") == ["running_script_edit"]
        assert edit_ids(idle) == [], "a script nothing is running"
        assert edit_ids(tmp_path / "daa_running_probe.py") == [], "not a shell script"
        # Bash spellings of the same write
        assert _ids(f"sed -i.bak 's/a/b/' {running}", cwd=str(tmp_path)) == ["running_script_edit"]
        assert _ids(f"cat > {running} <<'EOF'\necho hi\nEOF", cwd=str(tmp_path)) == ["running_script_edit"]
        assert _ids(f"cat > {idle} <<'EOF'\necho hi\nEOF", cwd=str(tmp_path)) == []
        assert _ids(f"cat {running}", cwd=str(tmp_path)) == [], "reading it is fine"
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("pgrep") is None, reason="needs pgrep and ps")
def test_a_viewer_of_the_script_is_not_a_runner(tmp_path: Path) -> None:
    script = tmp_path / "daa_viewed_probe.sh"
    script.write_text("#!/bin/bash\ntrue\n")
    viewer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(script)])
    try:
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(script)}, "cwd": str(tmp_path)}
        assert daa.decide(payload) == [], "a process that merely has the script's name in its arguments"
    finally:
        viewer.kill()
        viewer.wait(timeout=10)


def test_an_extensionless_file_with_a_shell_shebang_counts_as_a_script(tmp_path: Path) -> None:
    sh = tmp_path / "runner"
    sh.write_text("#!/usr/bin/env bash\ntrue\n")
    py = tmp_path / "tool"
    py.write_text("#!/usr/bin/env python3\n")
    assert daa._is_shell_script(str(sh))
    assert not daa._is_shell_script(str(py))
    assert not daa._is_shell_script(str(tmp_path / "missing"))


# ---- table integrity ------------------------------------------------------------------------


def test_every_row_has_a_match_a_near_miss_and_named_advice() -> None:
    ids = [r.id for r in daa.ROWS]
    assert len(ids) == len(set(ids)) == 8, ids
    matched = {m[0] for m in MATCHES} | {"running_script_edit", "stash_with_worktrees"}
    missed = {m[0] for m in NEAR_MISSES} | {"running_script_edit"}
    assert set(ids) <= matched, set(ids) - matched
    assert set(ids) <= missed, set(ids) - missed
    for row in daa.ROWS:
        assert row.advice.strip(), row.id
        assert row.source.startswith("feedback_destructive_action_guards"), (row.id, row.source)
        assert "(R" in row.source, f"{row.id}: the rule source names its census R-number"
        assert row.tools, row.id


def test_the_advice_names_the_rule_and_the_safe_alternative() -> None:
    for row in daa.ROWS:
        text = daa.render([daa.Hit(row, "")])
        assert f"[{row.id}]" in text, "the rendered advice names the row"
        assert "advice" in text
    # A safe alternative is spelled out in every row's advice.
    for row, needle in {
        "pattern_kill": "`kill <pid>`",
        "branch_bulk_delete": "one `git branch -D <name>` at a time",
        "stash_with_worktrees": "git show HEAD:f > f",
        "bg_pipe_masks_exit": "ec=$?",
        "whole_disk_scan": "-maxdepth",
        "shared_glob_delete": "exact name",
        "hook_verb_by_hand": "scratch directory",
        "running_script_edit": "move it",
    }.items():
        advice = next(r.advice for r in daa.ROWS if r.id == row)
        assert needle in advice, (row, needle)


def test_advise_is_the_mode_and_no_row_denies_today() -> None:
    assert daa.MODE == "advise"
    assert all(r.mode in (None, "advise") for r in daa.ROWS)
    assert all(daa.row_mode(r) == "advise" for r in daa.ROWS)
    assert daa.row_mode(daa.ROWS[0], "deny") == "deny", "the switch exists"
    assert daa.row_mode(daa.ROWS[0], "bogus") == "advise", "an unknown mode is advise"


def test_a_crashing_predicate_is_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_ctx):
        raise RuntimeError("induced")

    crashing = daa.Row("pattern_kill", "s (R1)", daa._BASH, boom, "x", hints=("kill",))
    monkeypatch.setattr(daa, "ROWS", (crashing,))
    assert daa.decide(_payload("pkill -f x")) == []


def test_other_tools_and_malformed_inputs_match_nothing() -> None:
    assert daa.decide({"tool_name": "Read", "tool_input": {"file_path": "x.sh"}}) == []
    assert daa.decide({"tool_name": "Bash"}) == []
    assert daa.decide({"tool_name": "Bash", "tool_input": {"command": None}}) == []
    assert daa.decide({"tool_name": "Bash", "tool_input": "pkill x"}) == []
    assert daa.decide({}) == []


# ---- end to end: the process, the envelope, the cost-relevant import discipline --------------


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NX_SERVICE", "NX_ENGINE"))}
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    env["NEXUS_CONFIG_DIR"] = str(cfg)
    env["NX_DROPPED_WRITES_LOG_PATH"] = str(cfg / "dropped_writes.jsonl")
    return env


def _run(payload_text: str, tmp_path: Path, *, mode: str | None = None) -> subprocess.CompletedProcess:
    if mode is None:
        argv = [sys.executable, str(SCRIPT)]
    else:
        driver = (
            "import importlib.util, sys\n"
            "s = importlib.util.spec_from_file_location('daa', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
            "m.MODE = sys.argv[2]\n"
            "m.main()\n"
        )
        argv = [sys.executable, "-c", driver, str(SCRIPT), mode]
    return subprocess.run(
        argv, input=payload_text, capture_output=True, text=True, timeout=30, env=_env(tmp_path),
    )


def test_a_match_advises_with_context_and_no_decision(tmp_path: Path) -> None:
    proc = _run(json.dumps(_payload("pkill -f pytest")), tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out, "advise mode never decides"
    assert "[pattern_kill]" in out["additionalContext"]
    assert "kill <pid>" in out["additionalContext"], "the safe alternative rides along"
    log = (tmp_path / "cfg" / "dropped_writes.jsonl").read_text()
    assert '"rule":"destructive_action_advice"' in log and '"outcome":"advise_pattern_kill"' in log


def test_two_matching_rows_share_one_envelope(tmp_path: Path) -> None:
    cmd = "git branch --list 'x-*' | xargs git branch -D | wc -l ; pkill -f y"
    out = json.loads(_run(json.dumps(_payload(cmd)), tmp_path).stdout)["hookSpecificOutput"]
    assert "[pattern_kill]" in out["additionalContext"] and "[branch_bulk_delete]" in out["additionalContext"]


@pytest.mark.parametrize(
    "text",
    [json.dumps(_payload("ls -la src | head -5")), json.dumps({"tool_name": "Read", "tool_input": {}}), "", "not json", "[]"],
)
def test_no_match_is_silent_and_exits_zero(text: str, tmp_path: Path) -> None:
    proc = _run(text, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "no decision, no output"


def _imports_after(payload: dict, tmp_path: Path) -> set[str]:
    driver = (
        "import importlib.util, io, json, sys\n"
        "s = importlib.util.spec_from_file_location('daa', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
        "sys.stdin = io.StringIO(sys.argv[2])\n"
        "try:\n"
        "    m.main()\n"
        "finally:\n"
        "    sys.stderr.write(json.dumps(sorted(k for k in ('_lib', 'shlex', 'subprocess') if k in sys.modules)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", driver, str(SCRIPT), json.dumps(payload)],
        capture_output=True, text=True, timeout=30, env=_env(tmp_path),
    )
    return set(json.loads(proc.stderr.strip().splitlines()[-1]))


def test_the_fast_path_imports_neither_the_framework_nor_shlex(tmp_path: Path) -> None:
    """The measured cost of a hook is its cold start. A call matching no row must not pay for
    ``_lib`` (999 lines, telemetry, endpoint resolution) or for tokenising."""
    assert _imports_after(_payload("ls -la"), tmp_path) == set()
    assert _imports_after({"tool_name": "Edit", "tool_input": {"file_path": "/x/README.md"}}, tmp_path) == set()
    assert _imports_after(_payload("pkill -f x"), tmp_path) >= {"_lib", "shlex"}, "a match does import them"


def test_deny_mode_is_implemented_but_not_active(tmp_path: Path) -> None:
    payload = json.dumps(_payload("pkill -f pytest"))
    advise = json.loads(_run(payload, tmp_path).stdout)["hookSpecificOutput"]
    assert "permissionDecision" not in advise

    out = json.loads(_run(payload, tmp_path, mode="deny").stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "[pattern_kill]" in out["permissionDecisionReason"]
    assert "routing-allow" in out["permissionDecisionReason"], "deny mode carries the escape clause"

    escaped = json.dumps(_payload("pkill -f pytest  # routing-allow: user said to stop my own run"))
    proc = _run(escaped, tmp_path, mode="deny")
    assert proc.returncode == 0 and proc.stdout == "", "a valid escape lets the call through in deny mode"
    log = (tmp_path / "cfg" / "dropped_writes.jsonl").read_text()
    assert '"outcome":"escape"' in log and '"outcome":"deny"' in log


# ---- wiring ---------------------------------------------------------------------------------


def test_the_hook_is_registered_once_on_bash_and_the_edit_tools() -> None:
    manifest = json.loads((REPO_ROOT / "conexus" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    groups = [
        g for g in manifest["hooks"]["PreToolUse"]
        if any("destructive_action_advice.py" in " ".join(h.get("args", [])) for h in g["hooks"])
    ]
    assert len(groups) == 1
    assert set(groups[0]["matcher"].split("|")) == {"Bash", "Edit", "Write", "MultiEdit"}
    assert len(groups[0]["hooks"]) == 1 and groups[0]["hooks"][0]["timeout"] == 5


def test_the_registry_names_the_rule_and_it_is_fail_open() -> None:
    text = (SCRIPT.parent / "registry.yaml").read_text(encoding="utf-8")
    block = text.split("  destructive_action_advice:\n", 1)[1]
    assert "hook_script: destructive_action_advice.py" in block
    assert "fail_closed: false" in block
