# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the verification hook pipeline.

RDR-215 bead nexus-q02nx.21 deleted ``stop_verification_hook.sh`` and
``pre_close_verification_hook.sh`` and re-declared their ``hooks.json``
entries to the ``hook_stop_verification`` / ``hook_pre_close_verification``
mcp_tools; ``TestStopHookPipeline``/``TestCloseHookPipeline`` below now
drive the ported ``nexus.hooks.stop_verification`` /
``nexus.hooks.pre_close_verification`` modules instead.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
HOOKS_JSON = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "hooks.json"
CONFIG_READER = HOOKS_DIR / "read_verification_config.py"

from tests._hook_wiring import matchers_for  # noqa: E402

_STOP_PY_DRIVER = """
import json, sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import stop_verification

raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else None
except Exception:
    payload = None
if not isinstance(payload, dict):
    payload = None
result = never_fail(lambda: stop_verification.run(payload), "stop_verification")
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(result.exit_code)
"""

_CLOSE_PY_DRIVER = """
import json, sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import pre_close_verification

raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else None
except Exception:
    payload = None
if not isinstance(payload, dict):
    payload = None
result = never_fail(lambda: pre_close_verification.run(payload), "pre_close_verification")
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(result.exit_code)
"""

#: Sentinels for :func:`_run_hook`, replacing the old STOP_HOOK/CLOSE_HOOK
#: script-path arguments now that both scripts are deleted.
STOP_HOOK = "stop"
CLOSE_HOOK = "close"

# Minimal PATH with python3 but without bd/nx. A dedicated symlink-only
# directory, NOT the parent of `which python3` -- this repo's venv bin/
# (where python3 typically resolves in a dev shell) also ships the `nx`
# console script alongside it, so using that whole directory silently
# reintroduces a REAL `nx` and defeats "without bd/nx" (nexus-4av2n:
# confirmed live -- `which python3` resolves into .venv/bin, which also
# contains `nx`, making test_on_close_true_always_allows exercise a real
# T1 session instead of the unreachable-T1 path it meant to test).
_PYTHON3 = subprocess.run(
    ["which", "python3"], capture_output=True, text=True
).stdout.strip()
_PYTHON3_ISOLATED_DIR = Path(tempfile.mkdtemp(prefix="nx-verif-integ-python3-"))
if _PYTHON3:
    (_PYTHON3_ISOLATED_DIR / "python3").symlink_to(_PYTHON3)
_MINIMAL_PATH = f"{_PYTHON3_ISOLATED_DIR}:/usr/bin:/bin"


def _run_hook(
    which: str,
    stdin: str,
    *,
    env_overrides: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": _MINIMAL_PATH,
        "HOME": os.environ.get("HOME", "/tmp"),
        **(env_overrides or {}),
    }
    driver = _STOP_PY_DRIVER if which == STOP_HOOK else _CLOSE_PY_DRIVER
    return subprocess.run(
        [sys.executable, "-c", driver],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=cwd,
    )


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"],
        capture_output=True, check=True,
    )
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        capture_output=True, check=True,
    )


@pytest.fixture
def mock_plugin_root(tmp_path_factory: pytest.TempPathFactory):
    """Create a mock CLAUDE_PLUGIN_ROOT with a configurable read_verification_config.py."""
    root = tmp_path_factory.mktemp("plugin")
    scripts_dir = root / "hooks" / "scripts"
    scripts_dir.mkdir(parents=True)

    def _make(config: dict) -> dict[str, str]:
        config_json = json.dumps(config)
        script = scripts_dir / "read_verification_config.py"
        script.write_text(
            f"import json; print({repr(config_json)})"
        )
        return {"CLAUDE_PLUGIN_ROOT": str(root)}

    return _make


# ---------------------------------------------------------------------------
# hooks.json structure tests
# ---------------------------------------------------------------------------


class TestHooksJsonStructure:
    """Verify hooks.json registration is correct."""

    def test_hooks_json_is_valid_json(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        assert "hooks" in data

    def test_hooks_json_has_stop_event(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        assert "Stop" in data["hooks"]

    def test_hooks_json_has_pretooluse_event(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        assert "PreToolUse" in data["hooks"]

    def test_hooks_json_stop_timeout(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        stop_hooks = data["hooks"]["Stop"]
        hook = stop_hooks[0]["hooks"][0]
        assert hook["timeout"] == 180

    def test_hooks_json_pretooluse_timeout(self) -> None:
        """The advisory hook must have a tight ceiling.

        Was 300s in the original RDR-024 / RDR-065 wiring; tightened
        to 5s to match the SessionStart fast-path hooks. The script
        body (read stdin, JSON out, exit 0) completes in <100ms, so
        a long ceiling masks real stalls. Pinning low (<=10s) so any
        future drift toward "minutes" trips this test rather than
        blocking every Bash tool call for that ceiling.
        """
        data = json.loads(HOOKS_JSON.read_text())
        pre_hooks = data["hooks"]["PreToolUse"]
        hook = pre_hooks[0]["hooks"][0]
        assert hook["timeout"] <= 10, (
            f"PreToolUse Bash timeout {hook['timeout']}s is too high; "
            f"the advisory hook should never need >5s. A long ceiling "
            f"masks real stalls."
        )

    def test_hooks_json_pretooluse_matcher_is_bash(self) -> None:
        """The pre-close verification hook fires on Bash, whatever position
        its entry occupies. Keyed on the HANDLER, not on ``PreToolUse[0]``:
        PreToolUse now carries a second entry (the Agent-dispatch matcher,
        nexus-qc4p1), and an index-positional assertion says nothing about
        the hook it is named for once the list has more than one member.
        This assertion has now been rewritten three times for the same
        reason -- index, then bash command string, then mcp_tool name --
        each time because the entry moved and the key was written against
        one declaration FORM. Bead nexus-17i1n moved it again, off the
        tool tier, because an ``mcp_tool`` hook cannot return a verdict
        and the gate shipped inert in 7.55.0. So it is keyed on hook
        IDENTITY across both forms now (``_names_hook``), and a fourth
        move will not need a fourth rewrite."""
        owners = matchers_for("pre_close_verification", "PreToolUse")
        assert owners == ["Bash"], owners

    def test_hooks_json_existing_hooks_unchanged(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        hooks = data["hooks"]
        assert "SessionStart" in hooks
        assert "PostCompact" in hooks
        assert "StopFailure" in hooks
        assert "SubagentStart" in hooks

    def test_hooks_json_references_valid_scripts(self) -> None:
        """All hook commands referencing hooks/scripts/ point to existing
        files.

        Checks both command shapes: the plain ``"command": "bash
        .../hooks/scripts/foo.sh"`` string, and the exec-form
        ``"command": "python3", "args": [".../hooks/scripts/foo.py", ...]``
        the four routing/version-lockstep hooks now use
        (RDR-215 bead nexus-q02nx.21's ``_interpreter.reexec_if_needed()``
        wiring). A run that checked zero references would prove nothing —
        every mcp_tool-only hooks.json would pass this vacuously — so this
        asserts it examined at least one, which is what caught this test
        going quiet in the first place: the twelve now-deleted bash
        scripts' re-declaration to mcp_tool left the single-string
        ``"command"`` branch with nothing left to match.
        """
        data = json.loads(HOOKS_JSON.read_text())
        checked = 0
        for event_name, event_hooks in data["hooks"].items():
            for hook_group in event_hooks:
                for hook in hook_group.get("hooks", []):
                    candidates: list[str] = []
                    cmd = hook.get("command", "")
                    if "hooks/scripts/" in cmd:
                        candidates.append(cmd.split("hooks/scripts/")[-1].split()[0])
                    for arg in hook.get("args", []):
                        if "hooks/scripts/" in arg:
                            candidates.append(arg.split("hooks/scripts/")[-1])
                    for script_name in candidates:
                        checked += 1
                        script_path = HOOKS_DIR / script_name
                        assert script_path.exists(), (
                            f"{event_name} references missing script: {script_path}"
                        )
        assert checked > 0, (
            "examined zero hooks/scripts/ references -- either hooks.json "
            "changed shape again (this test needs a third branch) or "
            "every hook left that directory entirely; a run that checks "
            "nothing is not a passing run"
        )


# ---------------------------------------------------------------------------
# Script existence and permissions
# ---------------------------------------------------------------------------


class TestScriptPermissions:
    """The stop/close hooks are the ported Python modules now (RDR-215
    bead nexus-q02nx.21 deleted stop_verification_hook.sh and
    pre_close_verification_hook.sh); an importable module needs no
    existence check the way a script path does. What both ports still
    shell out to -- ``read_verification_config.py`` -- is unchanged and
    still worth pinning here."""

    def test_config_reader_exists(self) -> None:
        assert CONFIG_READER.exists()


# ---------------------------------------------------------------------------
# End-to-end pipeline tests
# ---------------------------------------------------------------------------


class TestStopHookPipeline:
    """End-to-end tests for the Stop verification hook (advisory only)."""

    def test_on_stop_false_passes_through(self, mock_plugin_root) -> None:
        env = mock_plugin_root({"on_stop": False})
        payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
        result = _run_hook(STOP_HOOK, payload, env_overrides=env)
        assert result.returncode == 0
        assert json.loads(result.stdout)["decision"] == "approve"

    def test_on_stop_true_clean_repo(self, tmp_path, mock_plugin_root) -> None:
        _init_git_repo(tmp_path)
        env = mock_plugin_root({"on_stop": True})
        payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
        result = _run_hook(STOP_HOOK, payload, env_overrides=env, cwd=tmp_path)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr!r}"
        assert result.stdout.strip(), f"empty stdout, stderr={result.stderr!r}"
        # Extract last JSON line — earlier lines may be command output (e.g., nx catalog sync)
        json_line = [l for l in result.stdout.strip().splitlines() if l.startswith("{")][-1]
        assert json.loads(json_line)["decision"] == "approve"

    def test_warns_on_uncommitted_changes(self, tmp_path, mock_plugin_root) -> None:
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("modified\n")
        env = mock_plugin_root({"on_stop": True})
        payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
        result = _run_hook(STOP_HOOK, payload, env_overrides=env, cwd=tmp_path)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["decision"] == "approve"
        assert "uncommitted" in output.get("reason", "").lower()


class TestCloseHookPipeline:
    """End-to-end tests for the PreToolUse close hook.

    nexus-4av2n: no longer advisory-only -- see
    tests/hooks/test_pre_close_verification_hook.py for the deny/allow/
    override matrix. These tests cover only the fast no-op and config-gate
    paths plus the T1-unreachable capability-honest allow."""

    @staticmethod
    def _get_decision(output: dict) -> str:
        return output.get("hookSpecificOutput", {}).get("permissionDecision", "")

    def test_non_bash_tool_fast_noop(self) -> None:
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.txt", "content": "x"},
        })
        result = _run_hook(CLOSE_HOOK, payload)
        assert self._get_decision(json.loads(result.stdout)) == "allow"

    def test_non_matching_bash_fast_noop(self) -> None:
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
        })
        result = _run_hook(CLOSE_HOOK, payload)
        assert self._get_decision(json.loads(result.stdout)) == "allow"

    def test_on_close_true_with_t1_unreachable_allows_capability_honest(
        self, mock_plugin_root
    ) -> None:
        """nexus-4av2n: the hook now BLOCKS on a missing review marker when
        T1 is reachable (see tests/hooks/test_pre_close_verification_hook.py
        for that path in full). This PATH has no `nx` on it at all -- a
        capability gap, not a review gap -- so it allows (never brick a
        close over a broken T1) but stamps verification=unverified, not
        the old unconditional verification=passed."""
        env = mock_plugin_root({"on_close": True})
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd close nexus-test"},
        })
        result = _run_hook(CLOSE_HOOK, payload, env_overrides=env)
        out = json.loads(result.stdout)
        assert self._get_decision(out) == "allow"
        assert "unreachable" in out["hookSpecificOutput"].get("additionalContext", "").lower()

    def test_on_close_false_passes_through(self, mock_plugin_root) -> None:
        env = mock_plugin_root({"on_close": False})
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "bd close nexus-test"},
        })
        result = _run_hook(CLOSE_HOOK, payload, env_overrides=env)
        assert self._get_decision(json.loads(result.stdout)) == "allow"
