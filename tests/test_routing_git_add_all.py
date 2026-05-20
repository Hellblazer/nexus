# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 Phase 2 hook 3: git_add_all_redirects_to_explicit_paths.

Detects ``git add -A``, ``git add .``, ``git add --all`` and denies
with a redirect to explicit-path staging. Standing rule from
``feedback_no_git_add_all.md``: wildcard adds pull in unrelated
untracked drafts.
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
    / "nx"
    / "hooks"
    / "scripts"
    / "routing"
    / "git_add_all_redirects_to_explicit_paths.py"
)


def _run(payload: dict, env_extra: dict[str, str] | None = None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=10, env=env,
    )


def _decision(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))


def test_script_exists():
    assert HOOK_SCRIPT.exists()


# Positive: each wildcard form denies


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


# Negative


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


# Escape


def test_escape_allows():
    d = _decision(_run(_bash(
        "git add -A  # routing-allow: scripted bootstrap of fresh repo"
    )))
    assert d["permissionDecision"] == "allow"


# Malformed


def test_empty_stdin_allows():
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="", capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    d = json.loads(proc.stdout)["hookSpecificOutput"]
    assert d["permissionDecision"] == "allow"


# Registry + hooks.json wiring


def test_registry_has_rule():
    yaml = pytest.importorskip("yaml")
    reg = PROJECT_ROOT / "nx" / "hooks" / "scripts" / "routing" / "registry.yaml"
    rule = (yaml.safe_load(reg.read_text()) or {}).get("rules", {}).get(
        "git_add_all_redirects_to_explicit_paths"
    )
    assert rule is not None


def test_hooks_json_registers():
    hooks_json = PROJECT_ROOT / "nx" / "hooks" / "hooks.json"
    data = json.loads(hooks_json.read_text())
    found = any(
        "git_add_all_redirects_to_explicit_paths.py" in h.get("command", "")
        for entry in data["hooks"]["PreToolUse"] if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    )
    assert found
