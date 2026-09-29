# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-205 ledger's two PROJECTORS, driven as Claude Code drives them (nexus-egm7p).

Sibling of ``test_ledger_writers_command_tier_end_to_end.py`` (nexus-5l8i8),
same shape, same hazard, one tier over: ``hook_subagent_start_tuple`` and
``hook_subagent_stop_tuple`` were wired as ``mcp_tool`` entries, whose
invocation depends on this session's own ``plugin:conexus:nexus`` MCP
connection being up. A disconnect during the SubagentStart/SubagentStop
window silently drops the projection -- ``project()`` is never reached at
all, so nothing is even logged (see ``nexus.hooks.tuple_ledger_project``'s
own per-session log, which stays empty on this exact failure). Moving both
to the command tier removes that dependency: the harness spawns the
``nx-hook`` subprocess directly, MCP session or none.

**Two differences from the RDR-184 sibling, both load-bearing:**

1. The RDR-184 writers append to a local TSV file; these two POST to a real
   (here, mocked) engine over HTTP. So the positive tests below assert on
   the mock engine's captured request body -- subspace/keys/dims -- rather
   than on a local file's content.
2. The RDR-184 writers' command-tier verb calls straight through with no
   detachment concern (they were always synchronous). These two run
   SYNCHRONOUSLY too (nexus-wgalh, Sam 2026-09-27): nexus-egm7p first wired
   them ``"async": true``, and Claude Code kills async hooks at ``claude -p``
   teardown, so a last-act SubagentStop could lose its REPORT tuple. The
   verb module calls ``tuple_ledger_project.project()`` directly, never
   ``tuple_projection.run_start``/``run_stop``'s in-process daemon thread,
   which would die with this short-lived process.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._hook_wiring import HOOKS_JSON, NX_HOOK_SHIM, REPO_ROOT, command_verb, events_for
from tests.hooks.test_tuple_ledger_project import (
    _assistant_text_entry,
    _sendmessage_entry,
    _write_data_token_lease,
    _write_transcript,
    mock_engine,
)

_SESSION_ID = "e2e-nxegm7p-tuple-projector"
_AGENT_ID = "aworkere2eegm7ptupleproj01"
_AGENT_TYPE = "worktree-developer"


def _wired_argv(verb: str) -> list[str]:
    """The argv hooks.json runs for *verb* -- the real shim invocation, read
    from the shipped file rather than hand-typed (mirrors
    ``test_ledger_writers_command_tier_end_to_end.py``'s helper of the same
    shape)."""
    data = json.loads(HOOKS_JSON.read_text())
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                if command_verb(hook) == verb and hook.get("command") == "python3":
                    shim = NX_HOOK_SHIM.replace("${CLAUDE_PLUGIN_ROOT}", str(REPO_ROOT / "conexus"))
                    return [sys.executable, shim, verb]
    pytest.fail(f"hooks.json wires no python3/nx_hook_shim.py entry for verb {verb!r}")


def _run_verb_with_no_mcp_server(
    verb: str, payload: dict, *, config_dir: Path, xdg_state_home: Path,
    service_url: str,
) -> subprocess.CompletedProcess[str]:
    """Spawn *verb* exactly as its hooks.json entry would, in an environment
    that cannot reach any MCP server -- no inherited ``CLAUDE_*`` connection
    markers, no plugin-session identity. ``NX_SERVICE_URL`` points the
    projector straight at the mock engine below, and ``NEXUS_CONFIG_DIR``
    is where the data-token lease was written; both are read by
    ``tuple_ledger_project`` directly, never through MCP.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "XDG_STATE_HOME": str(xdg_state_home),
        "NEXUS_CONFIG_DIR": str(config_dir),
        "NX_SERVICE_URL": service_url,
    }
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
        if key in os.environ:
            env[key] = os.environ[key]
    return subprocess.run(
        _wired_argv(verb),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


@pytest.fixture(scope="module", autouse=True)
def _require_nx_hook():
    if shutil.which("nx-hook") is None:
        pytest.skip(
            "nx-hook is not on PATH. It is a console script, so an install "
            "generation predating its declaration has no shim."
        )


# ── Wiring: non-vacuity + mutation-verify (mirrors the RDR-184 sibling) ─────


def test_both_projectors_are_wired_on_the_command_tier_not_mcp_tool() -> None:
    """If either entry is ever moved back to ``mcp_tool`` -- the exact shape
    that dropped rows during an MCP disconnect -- this fails immediately,
    before any subprocess runs."""
    data = json.loads(HOOKS_JSON.read_text())
    found_start = found_stop = False
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                assert hook.get("tool") not in (
                    "hook_subagent_start_tuple",
                    "hook_subagent_stop_tuple",
                ), (
                    f"found an mcp_tool entry for {hook.get('tool')!r} — this is "
                    "the exact wiring that drops RDR-205 ledger rows during an "
                    "MCP disconnect (nexus-egm7p). Wire it through "
                    "nx_hook_shim.py instead."
                )
                if command_verb(hook) == "subagent-start-tuple":
                    found_start = True
                    assert "async" not in hook, (
                        "subagent-start-tuple must run synchronously (nexus-wgalh): "
                        "an async hook is killed at claude -p teardown"
                    )
                    assert hook.get("timeout", 0) >= 20, (
                        "subagent-start-tuple needs a timeout covering two "
                        "5 s-bounded POSTs plus interpreter starts"
                    )
                if command_verb(hook) == "subagent-stop-tuple":
                    found_stop = True
                    assert "async" not in hook, (
                        "subagent-stop-tuple must run synchronously (nexus-wgalh): "
                        "an async hook is killed at claude -p teardown"
                    )
                    assert hook.get("timeout", 0) >= 20, (
                        "subagent-stop-tuple needs a timeout covering two "
                        "5 s-bounded POSTs plus interpreter starts"
                    )
    assert found_start, "subagent-start-tuple is not wired as a command-tier verb anywhere"
    assert found_stop, "subagent-stop-tuple is not wired as a command-tier verb anywhere"


def test_reverting_to_the_old_mcp_tool_entries_is_caught(tmp_path: Path) -> None:
    """Mutation-verify the check above against the EXACT pre-fix shape."""
    old_shape = {
        "hooks": {
            "SubagentStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "mcp_tool",
                            "server": "plugin:conexus:nexus",
                            "tool": "hook_subagent_start_tuple",
                            "input": {
                                "session_id": "${session_id}",
                                "agent_id": "${agent_id}",
                                "agent_type": "${agent_type}",
                                "task": "${task}",
                            },
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "SubagentStop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "mcp_tool",
                            "server": "plugin:conexus:nexus",
                            "tool": "hook_subagent_stop_tuple",
                            "input": {
                                "session_id": "${session_id}",
                                "agent_id": "${agent_id}",
                                "agent_type": "${agent_type}",
                            },
                            "timeout": 10,
                        }
                    ],
                }
            ],
        }
    }
    perturbed = tmp_path / "hooks.json"
    perturbed.write_text(json.dumps(old_shape))

    assert events_for("subagent_start_tuple", hooks_json=perturbed) == ["SubagentStart"], (
        "the old shape is still recognised as WIRED (correct — it fires on a "
        "real event), which is what makes the next assertion the real proof"
    )
    assert events_for("subagent_stop_tuple", hooks_json=perturbed) == ["SubagentStop"]
    for entry in old_shape["hooks"]["SubagentStart"][0]["hooks"]:
        assert command_verb(entry) is None, (
            "the old mcp_tool entry resolved to a command-tier verb, which "
            "means this mutation no longer represents the pre-fix shape"
        )
    for entry in old_shape["hooks"]["SubagentStop"][0]["hooks"]:
        assert command_verb(entry) is None


# ── Positive proof: the real wired command, no MCP server, row lands ───────


def test_subagent_start_tuple_posts_the_start_row_with_no_mcp_server_reachable(
    tmp_path: Path, mock_engine,
) -> None:
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    xdg_state_home = tmp_path / "state"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    session_id = f"{_SESSION_ID}-start"
    t0 = time.monotonic()
    proc = _run_verb_with_no_mcp_server(
        "subagent-start-tuple",
        {
            "session_id": session_id,
            "hook_event_name": "SubagentStart",
            "agent_id": _AGENT_ID,
            "agent_type": _AGENT_TYPE,
            "task": "implement nexus-egm7p",
        },
        config_dir=config_dir,
        xdg_state_home=xdg_state_home,
        service_url=engine.base_url,
    )
    elapsed = time.monotonic() - t0
    # Printed so a CI log carries the wall time: since nexus-wgalh the entry
    # runs synchronously, so this is added latency on every SubagentStart.
    print(f"[nexus-egm7p] subagent-start-tuple wall time: {elapsed:.3f}s")

    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "", (
        f"subagent-start-tuple must be stdout-silent on every path (got {proc.stdout!r})"
    )

    assert len(engine.requests) == 1, (
        "project() was not called with the right subspace/keys/dims -- no "
        f"request reached the mock engine. stderr={proc.stderr!r}"
    )
    body = engine.requests[0]
    assert body["subspace"] == f"ledger/{session_id}"
    assert body["keys"] == {"agent_id": _AGENT_ID, "kind": "start"}
    assert body["dims"] == {"agent_type": _AGENT_TYPE}
    assert engine.auth_headers[0] == "Bearer fresh-data-token"


def test_subagent_stop_tuple_posts_the_report_row_with_no_mcp_server_reachable(
    tmp_path: Path, mock_engine,
) -> None:
    """Same proof for the stop side, PLUS the transport half of the VERIFY
    fix: the command tier receives the full SubagentStop payload, which
    carries ``agent_transcript_path`` -- something the retired mcp_tool
    registration never forwarded at all (its ``fields`` tuple named only
    session_id/agent_id/agent_type). A real-shaped transcript with a
    SendMessage report (the field-name bug's own failure mode) proves both
    the transport fix and the extraction fix in one call.
    """
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    xdg_state_home = tmp_path / "state"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    session_id = f"{_SESSION_ID}-stop"
    transcript = _write_transcript(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "do the thing"}},
        _sendmessage_entry(
            "Outcome: implemented and committed.\n"
            "VERIFY: commit=cafe123\n"
            "VERIFY: t2=nexus/checkpoint",
        ),
    ])

    t0 = time.monotonic()
    proc = _run_verb_with_no_mcp_server(
        "subagent-stop-tuple",
        {
            "session_id": session_id,
            "hook_event_name": "SubagentStop",
            "agent_id": _AGENT_ID,
            "agent_type": _AGENT_TYPE,
            "agent_transcript_path": str(transcript),
            "stop_hook_active": False,
        },
        config_dir=config_dir,
        xdg_state_home=xdg_state_home,
        service_url=engine.base_url,
    )
    elapsed = time.monotonic() - t0
    print(f"[nexus-egm7p] subagent-stop-tuple wall time: {elapsed:.3f}s")

    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "", (
        f"subagent-stop-tuple must be stdout-silent on every path (got {proc.stdout!r})"
    )

    assert len(engine.requests) == 1, (
        "project() was not called with the right subspace/keys/dims -- no "
        f"request reached the mock engine. stderr={proc.stderr!r}"
    )
    body = engine.requests[0]
    assert body["subspace"] == f"ledger/{session_id}"
    assert body["keys"] == {"agent_id": _AGENT_ID, "kind": "report"}
    assert body["dims"] == {
        "agent_type": _AGENT_TYPE,
        "verify": "present",
        "commit": "cafe123",
        "t2_ref": "nexus/checkpoint",
    }


# ── A FAILED projection logs at warning and still writes nothing to stdout ──
#
# nexus-8he82: the command-tier verbs now log the projection outcome, and a
# FAILED one logs at warning. The in-process outcome tests patch ``_emit``, so
# they cannot show that the real log path stays off stdout, which is the
# hook's decision channel. These run the verb through this checkout's own
# ``nx-hook`` (the one beside the interpreter running the tests), not whatever
# generation is on PATH, because an older generation has no outcome logging
# and would pass vacuously.


def _checkout_nx_hook() -> str:
    path = Path(sys.executable).parent / "nx-hook"
    if not path.exists():
        pytest.fail(f"no nx-hook beside {sys.executable}; run uv sync so this checkout installs it")
    return str(path)


def _run_checkout_verb(
    verb: str, payload: dict, *, config_dir: Path, xdg_state_home: Path,
    service_url: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "XDG_STATE_HOME": str(xdg_state_home),
        "NEXUS_CONFIG_DIR": str(config_dir),
    }
    if service_url:
        env["NX_SERVICE_URL"] = service_url
    return subprocess.run(
        [_checkout_nx_hook(), verb], input=json.dumps(payload),
        capture_output=True, text=True, timeout=60, env=env,
    )


def _hook_log(config_dir: Path) -> str:
    log = config_dir / "logs" / "hook.log"
    return log.read_text() if log.exists() else ""


@pytest.mark.parametrize(
    ("verb", "event_name", "payload_edit"),
    [
        # A start needs an agent_type; a report needs a session_id. Missing
        # what the kind requires is FAILED, not IGNORED or SKIPPED.
        ("subagent-start-tuple", "SubagentStart", {"agent_type": ""}),
        ("subagent-stop-tuple", "SubagentStop", {"session_id": ""}),
    ],
)
def test_an_incomplete_payload_logs_a_failure_and_keeps_stdout_empty(
    tmp_path: Path, verb: str, event_name: str, payload_edit: dict,
) -> None:
    config_dir = tmp_path / "config"
    payload = {"session_id": f"{_SESSION_ID}-fail", "hook_event_name": event_name,
               "agent_id": _AGENT_ID, "agent_type": _AGENT_TYPE, **payload_edit}
    proc = _run_checkout_verb(verb, payload, config_dir=config_dir, xdg_state_home=tmp_path / "state")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout == "", f"a FAILED outcome leaked onto stdout: {proc.stdout!r}"
    assert "tuple_projection_write_failed" in _hook_log(config_dir)


def test_a_refused_post_logs_a_failure_and_keeps_stdout_empty(tmp_path: Path, mock_engine) -> None:
    engine = mock_engine(status=503)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")
    proc = _run_checkout_verb(
        "subagent-start-tuple",
        {"session_id": f"{_SESSION_ID}-503", "hook_event_name": "SubagentStart",
         "agent_id": _AGENT_ID, "agent_type": _AGENT_TYPE},
        config_dir=config_dir, xdg_state_home=tmp_path / "state", service_url=engine.base_url,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout == "", f"a FAILED outcome leaked onto stdout: {proc.stdout!r}"
    assert len(engine.requests) == 1  # it really reached the POST
    assert "tuple_projection_write_failed" in _hook_log(config_dir)


def test_a_missing_lease_logs_skipped_at_info_and_keeps_stdout_empty(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    proc = _run_checkout_verb(
        "subagent-start-tuple",
        {"session_id": f"{_SESSION_ID}-nolease", "hook_event_name": "SubagentStart",
         "agent_id": _AGENT_ID, "agent_type": _AGENT_TYPE},
        config_dir=config_dir, xdg_state_home=tmp_path / "state",
    )
    assert proc.returncode == 0 and proc.stdout == ""
    log = _hook_log(config_dir)
    assert "tuple_projection_skipped" in log
    assert "tuple_projection_write_failed" not in log
