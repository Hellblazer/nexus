# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-143 P1.3: ``version_lockstep_hook.py`` logic tests.

The hook is the blocking, stdlib-only SessionStart entry point for the
plugin<->CLI version lockstep (Shape B). Contract:

- Read plugin version from ``${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json``.
- Read the marker ``~/.config/nexus/cli_lockstep_marker``.
- marker == plugin version  -> silent, no stdout, no dispatch.
- mismatch (or missing marker) -> emit additionalContext nudge JSON AND
  dispatch the detached action passing the target plugin version, then
  return immediately (never wedge synchronous SessionStart, CA-4).
- The hook NEVER writes the marker (the detached action owns that, on
  confirmed upgrade only).
- Any failure is swallowed: the hook always completes without raising
  and emits nothing on error (fail-safe exit 0).

These tests import the script as a module (stdlib-only, so it imports
cleanly under the test interpreter) and exercise the logic functions
with monkeypatched seams. A separate test pins that the script also runs
end-to-end under a bare interpreter.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus" / "hooks" / "scripts" / "version_lockstep_hook.py"
)


def _load_module():
    """Load the hook script as a fresh module object."""
    spec = importlib.util.spec_from_file_location("version_lockstep_hook", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


@pytest.fixture()
def plugin_root(tmp_path: Path) -> Path:
    """A fake CLAUDE_PLUGIN_ROOT with a plugin.json carrying a version."""
    pj = tmp_path / ".claude-plugin"
    pj.mkdir(parents=True)
    (pj / "plugin.json").write_text(json.dumps({"name": "conexus", "version": "9.9.9"}))
    return tmp_path


class TestScriptPresence:
    def test_script_exists(self) -> None:
        assert SCRIPT.exists(), (
            f"hooks.json wiring (P1.5) references {SCRIPT}; "
            f"if it moves SessionStart breaks silently"
        )


class TestReadPluginVersion:
    def test_reads_version_from_plugin_json(self, mod, plugin_root, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        assert mod.read_plugin_version() == "9.9.9"

    def test_missing_root_returns_none(self, mod, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert mod.read_plugin_version() is None

    def test_missing_file_returns_none(self, mod, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        assert mod.read_plugin_version() is None

    def test_malformed_json_returns_none(self, mod, tmp_path, monkeypatch) -> None:
        pj = tmp_path / ".claude-plugin"
        pj.mkdir()
        (pj / "plugin.json").write_text("{not json")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        assert mod.read_plugin_version() is None


class TestMarker:
    def test_marker_path_honors_env_override(self, mod, tmp_path, monkeypatch) -> None:
        target = tmp_path / "marker"
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(target))
        assert mod.marker_path() == target

    def test_default_marker_location(self, mod, monkeypatch) -> None:
        monkeypatch.delenv("NX_LOCKSTEP_MARKER", raising=False)
        p = mod.marker_path()
        assert p.name == "cli_lockstep_marker"
        assert p.parent.name == "nexus"
        assert ".config" in str(p)

    def test_read_marker_missing_returns_none(self, mod, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "absent"))
        assert mod.read_marker() is None

    def test_read_marker_strips_whitespace(self, mod, tmp_path, monkeypatch) -> None:
        m = tmp_path / "marker"
        m.write_text("5.7.0\n")
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(m))
        assert mod.read_marker() == "5.7.0"


class TestNudgeContract:
    def test_build_context_is_sessionstart_additional_context(self, mod) -> None:
        payload = json.loads(mod.build_context("9.9.9"))
        hs = payload["hookSpecificOutput"]
        assert hs["hookEventName"] == "SessionStart"
        assert "9.9.9" in hs["additionalContext"]

    def test_nudge_has_no_em_dash(self, mod) -> None:
        assert "—" not in mod.build_context("9.9.9")


class TestMainOrchestration:
    def test_match_is_silent_and_no_dispatch(
        self, mod, plugin_root, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        marker = tmp_path / "marker"
        marker.write_text("9.9.9")
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        assert capsys.readouterr().out.strip() == ""
        assert dispatched == []

    def test_mismatch_emits_nudge_and_dispatches(
        self, mod, plugin_root, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        marker = tmp_path / "marker"
        marker.write_text("1.0.0")  # stale
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert dispatched == ["9.9.9"]

    def test_missing_marker_treated_as_mismatch(
        self, mod, plugin_root, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "absent"))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert dispatched == ["9.9.9"]

    def test_hook_never_writes_marker(
        self, mod, plugin_root, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        marker = tmp_path / "marker"  # does not exist
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
        monkeypatch.setattr(mod, "dispatch_action", lambda v: None)

        mod.main()

        assert not marker.exists(), "the HOOK must never write the marker (action owns it)"

    def test_unreadable_plugin_version_is_silent_no_dispatch(
        self, mod, tmp_path, monkeypatch, capsys
    ) -> None:
        # No plugin.json -> read_plugin_version None -> nothing to do.
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "absent"))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        assert capsys.readouterr().out.strip() == ""
        assert dispatched == []

    def test_main_swallows_exceptions(self, mod, monkeypatch, capsys) -> None:
        def boom() -> str:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mod, "read_plugin_version", boom)
        # Must not raise; fail-safe exit 0.
        mod.main()
        assert capsys.readouterr().out.strip() == ""


class TestDispatchIsNonBlocking:
    def test_dispatch_returns_immediately_for_slow_action(
        self, mod, monkeypatch
    ) -> None:
        """dispatch_action must detach (not wait). We stub Popen and assert
        the hook does not call .wait()/.communicate() on the child."""
        calls: dict[str, object] = {}

        class FakePopen:
            def __init__(self, *a, **k):
                calls["args"] = a[0] if a else k.get("args")
                calls["started"] = True
                calls["start_new_session"] = k.get("start_new_session")
                calls["stdout"] = k.get("stdout")
                calls["stderr"] = k.get("stderr")
                calls["stdin"] = k.get("stdin")

            def wait(self, *a, **k):  # pragma: no cover - must not be called
                calls["waited"] = True

            def communicate(self, *a, **k):  # pragma: no cover
                calls["communicated"] = True

        monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
        mod.dispatch_action("9.9.9")

        assert calls.get("started") is True
        assert "waited" not in calls
        assert "communicated" not in calls
        # Detach contract: own session (so a SIGTERM to the parent group does
        # not kill the in-flight upgrade) and no inherited stdio.
        assert calls.get("start_new_session") is True
        assert calls.get("stdout") is mod.subprocess.DEVNULL
        assert calls.get("stderr") is mod.subprocess.DEVNULL
        assert calls.get("stdin") is mod.subprocess.DEVNULL
        # The detached command must carry the target version as an argv token.
        flat = " ".join(map(str, calls["args"])) if isinstance(calls["args"], (list, tuple)) else str(calls["args"])
        assert "9.9.9" in flat
        assert "version_lockstep_action.py" in flat


class TestRunsUnderBareInterpreter:
    def test_end_to_end_match_silent(self, plugin_root, tmp_path) -> None:
        """Invoke the script as a subprocess (mimics _run_python_hook.sh)
        with a matching marker: expect clean exit 0 and empty stdout."""
        import os

        marker = tmp_path / "marker"
        marker.write_text("9.9.9")
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        env["NX_LOCKSTEP_MARKER"] = str(marker)
        r = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, timeout=20, env=env,
        )
        assert r.returncode == 0
        assert r.stdout.strip() == ""
