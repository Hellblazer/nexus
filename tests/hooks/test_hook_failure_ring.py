# SPDX-License-Identifier: AGPL-3.0-or-later
"""A swallowed hook crash saves its input to a bounded, redacted local ring, and
``nx hook replay`` runs it again with the crash surfacing (nexus-3ceda.3)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus._hook_runtime import failure_ring
from nexus._hook_runtime._io import HookResult, never_fail
from nexus.commands.hook import hook_group
from nexus.mcp import hooks as _hooks
from tests._child_encoding import PYTHON_CHILD_ENCODING

_CRASHING_VERB = (
    "from nexus._hook_runtime._io import HookResult\n"
    "def run(payload):\n"
    "    if payload and payload.get('tool_name') == 'Boom':\n"
    "        raise RuntimeError('crashed on ' + payload['tool_name'])\n"
    "    return HookResult(stdout='fine')\n"
)


def _boom(payload: dict | None) -> HookResult:
    raise RuntimeError(f"tool-tier crash on {payload!r}")


def _raise() -> HookResult:
    raise RuntimeError("deliberate")


def test_a_recorded_entry_is_redacted_and_private(tmp_path: Path) -> None:
    payload = {
        "prompt": "use token=abcdef123456 please",
        "tool_input": {"command": "ls -la", "api_key": "sk-live-1", "Authorization": "Bearer x"},
        "items": [1, True, None, "password: hunter22"],
    }
    env = {"NX_FOO": "1", "NX_API_KEY": "k", "CLAUDE_PROJECT_DIR": "/p", "NEXUS_TOKEN": "t",
           "HOME": "/home/u", "AWS_SECRET": "s"}
    path = failure_ring.record("probe", payload, RuntimeError("secret=abcdef123456 leaked"),
                               module="m.v", env=env, directory=tmp_path, now=1_700_000_000.0)
    assert path is not None and path.parent == tmp_path
    entry = failure_ring.load(path)
    assert entry["hook"] == "probe" and entry["module"] == "m.v"
    assert entry["payload"]["prompt"] == "use token=[redacted] please"
    assert entry["payload"]["tool_input"] == {"command": "ls -la", "api_key": "[redacted]",
                                              "Authorization": "[redacted]"}
    assert entry["payload"]["items"] == [1, True, None, "password: [redacted]"]
    assert entry["env"] == {"CLAUDE_PROJECT_DIR": "/p", "NX_FOO": "1"}
    assert "abcdef123456" not in path.read_text(encoding="utf-8")
    assert entry["error_type"] == "RuntimeError"
    assert entry["error"] == "secret=[redacted] leaked"
    assert entry["traceback"].startswith("RuntimeError") or "RuntimeError" in entry["traceback"]
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_the_switch_turns_capture_off(tmp_path: Path) -> None:
    ring = tmp_path / "ring"
    assert failure_ring.record("probe", {}, RuntimeError("x"), env={failure_ring.DISABLE_ENV: "0"},
                               directory=ring) is None
    assert not ring.exists()


def test_recording_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    assert failure_ring.record("probe", {"a": object()}, RuntimeError("x"), env={},
                               directory=blocker / "ring") is None


def test_an_oversized_entry_is_cut_to_fit(tmp_path: Path) -> None:
    big = {"tool_input": {"content": "x" * (failure_ring.MAX_ENTRY_BYTES * 3)}}
    path = failure_ring.record("probe", big, RuntimeError("x"), env={}, directory=tmp_path)
    assert path is not None
    assert path.stat().st_size <= failure_ring.MAX_ENTRY_BYTES
    entry = failure_ring.load(path)
    assert entry["truncated"] is True
    assert "[cut " in entry["payload"]["tool_input"]["content"]


def test_the_ring_keeps_the_newest_within_both_caps(tmp_path: Path) -> None:
    paths = [failure_ring.record("probe", {"n": i}, RuntimeError("x"), env={}, directory=tmp_path,
                                 now=1_700_000_000.0 + i) for i in range(5)]
    stranger = tmp_path / "keep-me.json"
    stranger.write_text("{}", encoding="utf-8")
    assert failure_ring.prune(tmp_path, max_entries=3) == 2
    assert failure_ring.entries(tmp_path) == paths[2:]
    one = paths[4].stat().st_size
    assert failure_ring.prune(tmp_path, max_bytes=one) == 2
    assert failure_ring.entries(tmp_path) == paths[4:]
    assert stranger.exists(), "prune touched a file that is not the ring's"


def test_record_prunes_to_the_entry_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(failure_ring, "MAX_ENTRIES", 2)
    for i in range(4):
        failure_ring.record("probe", {"n": i}, RuntimeError("x"), env={}, directory=tmp_path,
                            now=1_700_000_000.0 + i)
    kept = [failure_ring.load(p)["payload"]["n"] for p in failure_ring.entries(tmp_path)]
    assert kept == [2, 3]


def test_load_refuses_what_is_not_an_entry(tmp_path: Path) -> None:
    other = tmp_path / "x.json"
    other.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a hook-failure entry"):
        failure_ring.load(other)
    other.write_text(json.dumps({"hook": "h", "payload": {}, "format": 99}), encoding="utf-8")
    with pytest.raises(ValueError, match="format 99"):
        failure_ring.load(other)


def test_never_fail_saves_the_payload_and_names_it_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(failure_ring.DISABLE_ENV, raising=False)
    result = never_fail(_raise, "probe", payload=lambda: {"session_id": "s-1"}, module="m.v")
    assert result == HookResult(crashed=True)
    (path,) = failure_ring.entries(tmp_path / "hook-failures")
    assert failure_ring.load(path)["payload"] == {"session_id": "s-1"}
    assert f"replay with: nx hook replay {path}" in capsys.readouterr().err


def test_a_crash_while_reading_the_payload_still_records_and_still_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

    def bad_payload() -> object:
        raise OSError("payload gone")

    assert never_fail(_raise, "probe", payload=bad_payload).crashed
    assert failure_ring.entries(tmp_path / "hook-failures") == []


def test_the_command_tier_records_and_replay_raises_the_same_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "probe_verb.py").write_text(_CRASHING_VERB, encoding="utf-8")
    config = tmp_path / "config"
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "NEXUS_CONFIG_DIR": str(config),
           "_NX_HOOK_TEST_VERB_OVERRIDE": json.dumps({"probe": "probe_verb"})}
    env.pop(failure_ring.DISABLE_ENV, None)
    proc = subprocess.run(
        [sys.executable, "-m", "nexus._hook_runtime.entry", "probe"], capture_output=True, text=True,
        encoding=PYTHON_CHILD_ENCODING, input=json.dumps({"tool_name": "Boom", "session_id": "s-9"}),
        env=env, check=False,
    )
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr
    (path,) = failure_ring.entries(config / "hook-failures")
    entry = failure_ring.load(path)
    assert entry["module"] == "probe_verb"
    assert entry["payload"] == {"tool_name": "Boom", "session_id": "s-9"}
    assert f"nx hook replay {path}" in proc.stderr

    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(RuntimeError, match="crashed on Boom"):
        failure_ring.replay(path)
    out = CliRunner().invoke(hook_group, ["replay", str(path)])
    assert isinstance(out.exception, RuntimeError) and "crashed on Boom" in str(out.exception)

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    listed = CliRunner().invoke(hook_group, ["failures", "--json"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output.splitlines()[0])["path"] == str(path)


def test_replay_of_a_fixed_verb_prints_its_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "fixed_verb.py").write_text(_CRASHING_VERB, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    path = failure_ring.record("probe", {"tool_name": "Calm"}, RuntimeError("old"), module="fixed_verb",
                               env={}, directory=tmp_path / "ring")
    out = CliRunner().invoke(hook_group, ["replay", str(path)])
    assert out.exit_code == 0, out.output
    assert "fine" in out.output


def test_the_tool_tier_records_the_run_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    spec = _hooks.HookToolSpec(name="probe_boom", run=_boom, timeout_s=30.0)
    tool = _hooks._make_tool_function(spec)
    tool(session_id="s-2")
    (path,) = failure_ring.entries(tmp_path / "hook-failures")
    entry = failure_ring.load(path)
    assert entry["hook"] == "hook_probe_boom"
    assert entry["module"] == __name__
    assert entry["payload"] == {"session_id": "s-2"}


def test_failures_says_so_when_the_ring_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    out = CliRunner().invoke(hook_group, ["failures"])
    assert out.exit_code == 0 and "No saved hook failures" in out.output
