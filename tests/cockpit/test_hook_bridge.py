# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/nexus/cockpit/hook_bridge.py — hook-to-tuple bridge.

TDD-first: these tests define the contract for the bridge library and the
seven per-hook-type scripts. Written before implementation per project TDD
conventions.

Coverage:
- route_payload: one test per hook type asserting (subspace, dimensions, match_text)
- output_for_hook: one test per hook type asserting correct output contract
- Script exit-0 on malformed JSON
- CLAUDECODE-unset skips emission but still produces transparent-allow output
- api.out call shape for PreToolUse, Stop, Notification
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import chromadb
import pytest

# ---------------------------------------------------------------------------
# Inline subspace YAML stubs for the seven hook-event subspaces.
# These match the canonical names from RDR-111 lines 387-393.
# ---------------------------------------------------------------------------

_TOOL_CALL_INTENT_YAML = """
name: hook_events/tool_call_intent
tier: session
content_type: text
embed_from: match_text
dimensions:
  actor:      { type: string, required: true }
  session:    { type: string, required: true }
  project:    { type: string, required: true }
  timestamp:  { type: string, required: true }
  tool:       { type: string, required: false }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 10
tiers: [session]
retention_seconds: 86400
"""

_TOOL_CALL_COMPLETED_YAML = """
name: hook_events/tool_call_completed
tier: session
content_type: text
embed_from: match_text
dimensions:
  actor:      { type: string, required: true }
  session:    { type: string, required: true }
  project:    { type: string, required: true }
  timestamp:  { type: string, required: true }
  tool:       { type: string, required: false }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 10
tiers: [session]
retention_seconds: 86400
"""

_AGENT_COMPLETED_YAML = """
name: hook_events/agent_completed
tier: session
content_type: text
embed_from: match_text
dimensions:
  actor:      { type: string, required: true }
  session:    { type: string, required: true }
  project:    { type: string, required: true }
  timestamp:  { type: string, required: true }
  workflow:   { type: string, required: false }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 10
tiers: [session]
retention_seconds: 86400
"""

_ASSISTANT_TURN_ENDED_YAML = """
name: hook_events/assistant_turn_ended
tier: session
content_type: text
embed_from: match_text
dimensions:
  actor:      { type: string, required: true }
  session:    { type: string, required: true }
  project:    { type: string, required: true }
  timestamp:  { type: string, required: true }
  event_type: { type: string, required: true }
  intent:     { type: string, required: false }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 10
tiers: [session]
retention_seconds: 86400
"""

_USER_PROMPT_YAML = """
name: hook_events/user_prompt
tier: session
content_type: text
embed_from: match_text
dimensions:
  actor:      { type: string, required: true }
  session:    { type: string, required: true }
  project:    { type: string, required: true }
  timestamp:  { type: string, required: true }
  priority:   { type: string, required: false }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 10
tiers: [session]
retention_seconds: 86400
"""

_SESSION_LIFECYCLE_YAML = """
name: hook_events/session_lifecycle
tier: session
content_type: text
embed_from: match_text
dimensions:
  actor:      { type: string, required: true }
  session:    { type: string, required: true }
  project:    { type: string, required: true }
  timestamp:  { type: string, required: true }
  event_type: { type: string, required: true }
  workflow:   { type: string, required: false }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 10
tiers: [session]
retention_seconds: 86400
"""

_NOTIFICATION_YAML = """
name: hook_events/notification
tier: session
content_type: text
embed_from: match_text
dimensions:
  actor:      { type: string, required: true }
  session:    { type: string, required: true }
  project:    { type: string, required: true }
  timestamp:  { type: string, required: true }
  intent:     { type: string, required: false }
take:
  enabled: false
  mode: semantic
  floor: 0.30
  margin: 0.05
read:
  default_floor: 0.20
  default_n: 10
tiers: [session]
retention_seconds: 86400
"""

_ALL_HOOK_YAMLS = [
    ("hook_events_tool_call_intent.yml", _TOOL_CALL_INTENT_YAML),
    ("hook_events_tool_call_completed.yml", _TOOL_CALL_COMPLETED_YAML),
    ("hook_events_agent_completed.yml", _AGENT_COMPLETED_YAML),
    ("hook_events_assistant_turn_ended.yml", _ASSISTANT_TURN_ENDED_YAML),
    ("hook_events_user_prompt.yml", _USER_PROMPT_YAML),
    ("hook_events_session_lifecycle.yml", _SESSION_LIFECYCLE_YAML),
    ("hook_events_notification.yml", _NOTIFICATION_YAML),
]


@pytest.fixture
def hook_builtin_dir(tmp_path: Path) -> Path:
    """Write hook-event subspace YAMLs into a tmp builtin dir for tests."""
    d = tmp_path / "builtin"
    d.mkdir()
    for fname, content in _ALL_HOOK_YAMLS:
        (d / fname).write_text(content)
    return d


@pytest.fixture
def hook_registry(hook_builtin_dir: Path):
    from nexus.tuplespace.registry import Registry

    return Registry.load(hook_builtin_dir)


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    from nexus.tuplespace.store import open_tuples_db

    db_path = tmp_path / "tuples.db"
    conn = open_tuples_db(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def chroma_client():
    client = chromadb.EphemeralClient()
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture
def hook_index(hook_registry, chroma_client):
    from nexus.tuplespace.index import TupleIndex

    return TupleIndex.from_registry(hook_registry, chroma_client)


# ---------------------------------------------------------------------------
# Sample payloads matching the verified spike shapes (CA-6 + RDR-111 §RF-1)
# ---------------------------------------------------------------------------

_BASE_ENV = {
    "session_id": "test-session-abc",
    "transcript_path": "/tmp/test.jsonl",
    "cwd": "/projects/nexus",
    "permission_mode": "bypassPermissions",
}

PRETOOLUSE_PAYLOAD: dict[str, Any] = {
    **_BASE_ENV,
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "ls -la"},
}

POSTTOOLUSE_PAYLOAD: dict[str, Any] = {
    **_BASE_ENV,
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "ls -la"},
    "tool_response": "file1.py\nfile2.py\n",
}

STOP_PAYLOAD: dict[str, Any] = {
    **_BASE_ENV,
    "hook_event_name": "Stop",
    "stop_hook_active": False,
}

STOPFAILURE_PAYLOAD: dict[str, Any] = {
    **_BASE_ENV,
    "hook_event_name": "StopFailure",
    "stop_hook_active": False,
}

SUBAGENT_STOP_PAYLOAD: dict[str, Any] = {
    **_BASE_ENV,
    "hook_event_name": "SubagentStop",
    "agent_id": "agent-abc123",
    "agent_type": "general-purpose",
    "effort": {"level": "xhigh"},
    "stop_hook_active": False,
    "agent_transcript_path": "/tmp/subagents/agent-abc123.jsonl",
    "last_assistant_message": "Task complete. All tests pass.",
}

USER_PROMPT_PAYLOAD: dict[str, Any] = {
    **_BASE_ENV,
    "hook_event_name": "UserPromptSubmit",
    "prompt": "Please run the tests and summarise the results.",
}

SESSION_START_PAYLOAD: dict[str, Any] = {
    **_BASE_ENV,
    "hook_event_name": "SessionStart",
}

SESSION_END_PAYLOAD: dict[str, Any] = {
    **_BASE_ENV,
    "hook_event_name": "SessionEnd",
}

NOTIFICATION_PAYLOAD: dict[str, Any] = {
    **_BASE_ENV,
    "hook_event_name": "Notification",
    "message": "Claude is waiting for your input",
    "notification_type": "idle_prompt",
}


# ---------------------------------------------------------------------------
# Tests: route_payload — (subspace, dimensions, match_text) shape
# ---------------------------------------------------------------------------


class TestRoutePayload:
    """route_payload returns (subspace, dimensions, match_text) or None."""

    def test_pretooluse_routes_to_tool_call_intent(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("PreToolUse", PRETOOLUSE_PAYLOAD)
        assert result is not None
        subspace, dims, match_text = result
        assert subspace == "hook_events/tool_call_intent"
        assert dims["session"] == "test-session-abc"
        assert dims["project"] == "/projects/nexus"
        assert "actor" in dims
        assert "timestamp" in dims
        assert dims.get("tool") == "Bash"
        assert isinstance(match_text, str)

    def test_posttooluse_routes_to_tool_call_completed(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("PostToolUse", POSTTOOLUSE_PAYLOAD)
        assert result is not None
        subspace, dims, match_text = result
        assert subspace == "hook_events/tool_call_completed"
        assert dims["session"] == "test-session-abc"
        assert dims.get("tool") == "Bash"
        assert isinstance(match_text, str)

    def test_stop_routes_to_assistant_turn_ended(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("Stop", STOP_PAYLOAD)
        assert result is not None
        subspace, dims, match_text = result
        assert subspace == "hook_events/assistant_turn_ended"
        assert dims["session"] == "test-session-abc"
        assert isinstance(match_text, str)

    def test_stopfailure_routes_to_assistant_turn_ended(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("StopFailure", STOPFAILURE_PAYLOAD)
        assert result is not None
        subspace, dims, match_text = result
        assert subspace == "hook_events/assistant_turn_ended"

    def test_subagent_stop_routes_to_agent_completed(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("SubagentStop", SUBAGENT_STOP_PAYLOAD)
        assert result is not None
        subspace, dims, match_text = result
        assert subspace == "hook_events/agent_completed"
        assert dims["session"] == "test-session-abc"
        assert isinstance(match_text, str)
        # match_text is the last assistant message for SubagentStop
        assert "Task complete" in match_text

    def test_user_prompt_routes_to_user_prompt(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("UserPromptSubmit", USER_PROMPT_PAYLOAD)
        assert result is not None
        subspace, dims, match_text = result
        assert subspace == "hook_events/user_prompt"
        assert dims["session"] == "test-session-abc"
        assert "Please run the tests" in match_text

    def test_session_start_routes_to_session_lifecycle(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("SessionStart", SESSION_START_PAYLOAD)
        assert result is not None
        subspace, dims, match_text = result
        assert subspace == "hook_events/session_lifecycle"
        assert dims["session"] == "test-session-abc"

    def test_session_end_routes_to_session_lifecycle(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("SessionEnd", SESSION_END_PAYLOAD)
        assert result is not None
        subspace, dims, match_text = result
        assert subspace == "hook_events/session_lifecycle"

    def test_notification_routes_to_notification(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("Notification", NOTIFICATION_PAYLOAD)
        assert result is not None
        subspace, dims, match_text = result
        assert subspace == "hook_events/notification"
        assert dims["session"] == "test-session-abc"
        assert "waiting" in match_text

    def test_unknown_hook_type_returns_none(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        result = route_payload("UnknownHookType", {"session_id": "x"})
        assert result is None

    def test_required_dimensions_present(self) -> None:
        """actor, session, project, timestamp always present."""
        from nexus.cockpit.hook_bridge import route_payload

        for hook_type, payload in [
            ("PreToolUse", PRETOOLUSE_PAYLOAD),
            ("PostToolUse", POSTTOOLUSE_PAYLOAD),
            ("Stop", STOP_PAYLOAD),
            ("SubagentStop", SUBAGENT_STOP_PAYLOAD),
            ("UserPromptSubmit", USER_PROMPT_PAYLOAD),
            ("SessionStart", SESSION_START_PAYLOAD),
            ("Notification", NOTIFICATION_PAYLOAD),
        ]:
            result = route_payload(hook_type, payload)
            assert result is not None, f"route_payload returned None for {hook_type}"
            _, dims, _ = result
            for req in ("actor", "session", "project", "timestamp"):
                assert req in dims, f"Missing required dim {req!r} for {hook_type}"
                assert dims[req] != "", f"Required dim {req!r} is empty for {hook_type}"


# ---------------------------------------------------------------------------
# Tests: output_for_hook — stdout contract
# ---------------------------------------------------------------------------


class TestOutputForHook:
    """output_for_hook returns a string or None depending on hook type."""

    def test_pretooluse_emits_none_observe_only(self) -> None:
        """CA-8 spike result: bridge is observe-only, no permissionDecision."""
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("PreToolUse")
        # observe-only: emit nothing to avoid interfering with other hooks
        assert out is None

    def test_permissionrequest_emits_transparent_allow(self) -> None:
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("PermissionRequest")
        assert out is not None
        parsed = json.loads(out)
        # PermissionRequest needs explicit allow
        assert parsed["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_posttooluse_emits_none(self) -> None:
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("PostToolUse")
        assert out is None

    def test_stop_emits_none(self) -> None:
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("Stop")
        assert out is None

    def test_stopfailure_emits_none(self) -> None:
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("StopFailure")
        assert out is None

    def test_session_end_emits_none(self) -> None:
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("SessionEnd")
        assert out is None

    def test_session_start_emits_none(self) -> None:
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("SessionStart")
        assert out is None

    def test_subagent_stop_emits_none(self) -> None:
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("SubagentStop")
        assert out is None

    def test_user_prompt_emits_none(self) -> None:
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("UserPromptSubmit")
        assert out is None

    def test_notification_emits_none(self) -> None:
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("Notification")
        assert out is None


# ---------------------------------------------------------------------------
# Tests: emit — calls api.out with correct args (mock tuplespace)
# ---------------------------------------------------------------------------


class TestEmit:
    """emit() calls api.out with the correct args in direct mode."""

    def _make_mock_tuplespace(self):
        """Return a mock that satisfies the api.out call signature."""
        mock_out = MagicMock(return_value="abc123")
        return mock_out

    def test_pretooluse_emit_calls_out(
        self, db_conn, hook_index, hook_registry, tmp_path
    ) -> None:
        from nexus.cockpit import hook_bridge

        called_args: list[dict] = []

        def _fake_out(*, conn, index, registry, subspace, content, dimensions, match_text=None, ttl_seconds=None):
            called_args.append({
                "subspace": subspace,
                "content": content,
                "dimensions": dimensions,
                "match_text": match_text,
            })
            return "fake-tuple-id"

        with patch("nexus.cockpit.hook_bridge._direct_out", _fake_out):
            hook_bridge.emit(
                "PreToolUse",
                PRETOOLUSE_PAYLOAD,
                conn=db_conn,
                index=hook_index,
                registry=hook_registry,
            )

        assert len(called_args) == 1
        assert called_args[0]["subspace"] == "hook_events/tool_call_intent"
        dims = called_args[0]["dimensions"]
        assert dims["session"] == "test-session-abc"
        assert dims["tool"] == "Bash"

    def test_stop_emit_calls_out(
        self, db_conn, hook_index, hook_registry
    ) -> None:
        from nexus.cockpit import hook_bridge

        called_args: list[dict] = []

        def _fake_out(*, conn, index, registry, subspace, content, dimensions, match_text=None, ttl_seconds=None):
            called_args.append({"subspace": subspace, "dimensions": dimensions})
            return "fake-id"

        with patch("nexus.cockpit.hook_bridge._direct_out", _fake_out):
            hook_bridge.emit(
                "Stop",
                STOP_PAYLOAD,
                conn=db_conn,
                index=hook_index,
                registry=hook_registry,
            )

        assert len(called_args) == 1
        assert called_args[0]["subspace"] == "hook_events/assistant_turn_ended"

    def test_notification_emit_calls_out(
        self, db_conn, hook_index, hook_registry
    ) -> None:
        from nexus.cockpit import hook_bridge

        called_args: list[dict] = []

        def _fake_out(*, conn, index, registry, subspace, content, dimensions, match_text=None, ttl_seconds=None):
            called_args.append({
                "subspace": subspace,
                "match_text": match_text,
            })
            return "fake-id"

        with patch("nexus.cockpit.hook_bridge._direct_out", _fake_out):
            hook_bridge.emit(
                "Notification",
                NOTIFICATION_PAYLOAD,
                conn=db_conn,
                index=hook_index,
                registry=hook_registry,
            )

        assert len(called_args) == 1
        assert called_args[0]["subspace"] == "hook_events/notification"
        assert "waiting" in (called_args[0]["match_text"] or "")

    def test_emit_unknown_hook_type_no_call(
        self, db_conn, hook_index, hook_registry
    ) -> None:
        """Unknown hook types produce no api.out call (route_payload returns None)."""
        from nexus.cockpit import hook_bridge

        called = []

        def _fake_out(**kwargs):
            called.append(kwargs)
            return "id"

        with patch("nexus.cockpit.hook_bridge._direct_out", _fake_out):
            hook_bridge.emit(
                "PreCompact",
                {"session_id": "x", "trigger": "manual", "cwd": "/tmp"},
                conn=db_conn,
                index=hook_index,
                registry=hook_registry,
            )

        assert called == []


# ---------------------------------------------------------------------------
# Tests: CLAUDECODE-unset skips emission but still emits correct stdout
# ---------------------------------------------------------------------------


class TestClaudecodeGate:
    """RF-5: side effects skipped when CLAUDECODE not set in environment."""

    def test_emit_skips_out_when_claudecode_unset(
        self, db_conn, hook_index, hook_registry
    ) -> None:
        from nexus.cockpit import hook_bridge

        called = []

        def _fake_out(**kwargs):
            called.append(kwargs)
            return "id"

        env_without_claudecode = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        with patch("nexus.cockpit.hook_bridge._direct_out", _fake_out):
            with patch.dict(os.environ, env_without_claudecode, clear=True):
                hook_bridge.emit(
                    "PreToolUse",
                    PRETOOLUSE_PAYLOAD,
                    conn=db_conn,
                    index=hook_index,
                    registry=hook_registry,
                )

        assert called == [], "api.out must not be called when CLAUDECODE is not set"

    def test_permissionrequest_output_still_emits_when_claudecode_unset(self) -> None:
        """Even without CLAUDECODE, PermissionRequest must output transparent allow."""
        from nexus.cockpit.hook_bridge import output_for_hook

        # output_for_hook is pure — not gated on CLAUDECODE
        out = output_for_hook("PermissionRequest")
        assert out is not None
        parsed = json.loads(out)
        assert parsed["hookSpecificOutput"]["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# Tests: script exit-0 on malformed JSON
# ---------------------------------------------------------------------------

# Find the scripts directory relative to this test file
_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "nx" / "hooks" / "scripts"
)

_SCRIPT_NAMES = [
    "orb_bridge_pretooluse.py",
    "orb_bridge_posttooluse.py",
    "orb_bridge_stop.py",
    "orb_bridge_subagent_stop.py",
    "orb_bridge_user_prompt_submit.py",
    "orb_bridge_session.py",
    "orb_bridge_notification.py",
]


def _run_script(
    script_name: str,
    stdin_data: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a bridge script with given stdin, return CompletedProcess."""
    script_path = _SCRIPTS_DIR / script_name
    run_env = dict(os.environ)
    if env is not None:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(script_path)],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=run_env,
        timeout=10,
    )


class TestScriptMalformedJson:
    """Each script exits 0 even on malformed JSON input."""

    @pytest.mark.parametrize("script_name", _SCRIPT_NAMES)
    def test_exits_0_on_malformed_json(self, script_name: str) -> None:
        result = _run_script(script_name, "NOT VALID JSON {{{")
        assert result.returncode == 0, (
            f"{script_name} exited {result.returncode} on malformed JSON.\n"
            f"stderr: {result.stderr}"
        )

    @pytest.mark.parametrize("script_name", _SCRIPT_NAMES)
    def test_exits_0_on_empty_stdin(self, script_name: str) -> None:
        result = _run_script(script_name, "")
        assert result.returncode == 0, (
            f"{script_name} exited {result.returncode} on empty stdin.\n"
            f"stderr: {result.stderr}"
        )


class TestScriptClaudecodeGate:
    """Scripts skip side-effects when CLAUDECODE is not in environment."""

    def test_pretooluse_observe_only_no_stdout_when_claudecode_unset(self) -> None:
        """PreToolUse is observe-only: no output even with CLAUDECODE unset."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = _run_script(
            "orb_bridge_pretooluse.py",
            json.dumps(PRETOOLUSE_PAYLOAD),
            env=env,
        )
        assert result.returncode == 0
        # observe-only: no stdout
        assert result.stdout.strip() == ""

    def test_permissionrequest_not_a_script_but_output_for_hook_is_pure(self) -> None:
        """output_for_hook is pure Python, not gated on env; PermissionRequest always allows."""
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("PermissionRequest")
        assert out is not None
        assert "allow" in out


class TestScriptCorrectOutput:
    """Scripts produce correct stdout for their hook type."""

    def test_pretooluse_script_no_stdout_observe_only(self) -> None:
        """CA-8 spike: observe-only, no stdout from PreToolUse bridge."""
        result = _run_script(
            "orb_bridge_pretooluse.py",
            json.dumps(PRETOOLUSE_PAYLOAD),
            env={"CLAUDECODE": "1", "CLAUDE_PROJECT_DIR": "/projects/nexus"},
        )
        assert result.returncode == 0
        # observe-only: no stdout
        assert result.stdout.strip() == ""

    def test_stop_script_no_stdout(self) -> None:
        result = _run_script(
            "orb_bridge_stop.py",
            json.dumps(STOP_PAYLOAD),
            env={"CLAUDECODE": "1", "CLAUDE_PROJECT_DIR": "/projects/nexus"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_notification_script_no_stdout(self) -> None:
        result = _run_script(
            "orb_bridge_notification.py",
            json.dumps(NOTIFICATION_PAYLOAD),
            env={"CLAUDECODE": "1", "CLAUDE_PROJECT_DIR": "/projects/nexus"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# nexus-usic: event_type discriminator dimension on collapsed subspaces
# ---------------------------------------------------------------------------


class TestEventTypeDiscriminator:
    """Stop/StopFailure and SessionStart/SessionEnd carry event_type dim."""

    def test_stop_dim_event_type_is_stop(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        _, dims, _ = route_payload("Stop", STOP_PAYLOAD)
        assert dims["event_type"] == "Stop"

    def test_stopfailure_dim_event_type_is_stopfailure(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        _, dims, _ = route_payload("StopFailure", STOPFAILURE_PAYLOAD)
        assert dims["event_type"] == "StopFailure"

    def test_sessionstart_dim_event_type_is_sessionstart(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        _, dims, _ = route_payload("SessionStart", SESSION_START_PAYLOAD)
        assert dims["event_type"] == "SessionStart"

    def test_sessionend_dim_event_type_is_sessionend(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        _, dims, _ = route_payload("SessionEnd", SESSION_END_PAYLOAD)
        assert dims["event_type"] == "SessionEnd"

    def test_stop_match_text_includes_session_and_cwd(self) -> None:
        """match_text must carry more than the literal hook name (semantic richness)."""
        from nexus.cockpit.hook_bridge import route_payload

        _, _, match_text = route_payload("Stop", STOP_PAYLOAD)
        assert "Stop" in match_text
        assert "test-session-abc" in match_text
        assert "/projects/nexus" in match_text

    def test_sessionstart_match_text_includes_session_and_cwd(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        _, _, match_text = route_payload("SessionStart", SESSION_START_PAYLOAD)
        assert "SessionStart" in match_text
        assert "test-session-abc" in match_text
        assert "/projects/nexus" in match_text

    def test_non_collapsed_hooks_have_no_event_type(self) -> None:
        """Only the collapsed subspaces need the discriminator."""
        from nexus.cockpit.hook_bridge import route_payload

        for hook_type, payload in [
            ("PreToolUse", PRETOOLUSE_PAYLOAD),
            ("PostToolUse", POSTTOOLUSE_PAYLOAD),
            ("SubagentStop", SUBAGENT_STOP_PAYLOAD),
            ("UserPromptSubmit", USER_PROMPT_PAYLOAD),
            ("Notification", NOTIFICATION_PAYLOAD),
        ]:
            _, dims, _ = route_payload(hook_type, payload)
            assert "event_type" not in dims, (
                f"event_type should only be on collapsed subspaces, found on {hook_type}"
            )


# ---------------------------------------------------------------------------
# nexus-este: production-YAML schema test + _emit_direct_auto integration test
# ---------------------------------------------------------------------------


_PROD_HOOKS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "nx"
    / "tuplespace"
    / "builtin"
)


class TestProductionYamlSchemas:
    """The seven production YAMLs in nx/tuplespace/builtin/hooks/ load cleanly
    and use the embed_from=match_text contract the production bridge depends on.
    """

    def test_production_yamls_load_via_load_registry_with_hooks(self) -> None:
        from nexus.cockpit.hook_bridge import _load_registry_with_hooks

        registry = _load_registry_with_hooks(_PROD_HOOKS_DIR)
        # All seven hook-event subspaces must resolve through the registry.
        for subspace in (
            "hook_events/tool_call_intent",
            "hook_events/tool_call_completed",
            "hook_events/agent_completed",
            "hook_events/assistant_turn_ended",
            "hook_events/user_prompt",
            "hook_events/session_lifecycle",
            "hook_events/notification",
        ):
            tmpl = registry.get_schema_for(subspace)
            assert tmpl is not None, f"Production YAML missing for {subspace}"
            assert tmpl.embed_from == "match_text", (
                f"{subspace}: production YAMLs must embed match_text "
                f"(got {tmpl.embed_from!r})"
            )

    def test_production_collapsed_subspaces_require_event_type(self) -> None:
        """assistant_turn_ended and session_lifecycle must require event_type."""
        from nexus.cockpit.hook_bridge import _load_registry_with_hooks

        registry = _load_registry_with_hooks(_PROD_HOOKS_DIR)
        for subspace in (
            "hook_events/assistant_turn_ended",
            "hook_events/session_lifecycle",
        ):
            tmpl = registry.get_schema_for(subspace)
            dims = tmpl.dimensions
            assert "event_type" in dims, (
                f"{subspace}: event_type discriminator required (nexus-usic)"
            )
            assert dims["event_type"]["required"] is True


class TestEmitDirectAuto:
    """Integration test of the production self-initialisation path."""

    def test_emit_direct_auto_round_trips_via_default_paths(
        self, tmp_path, monkeypatch
    ) -> None:
        """_emit_direct_auto opens default paths, writes a tuple, then read() finds it."""
        from nexus.cockpit import hook_bridge

        # Redirect nexus_dir to a tmp_path so the test never touches the user's
        # real ~/.config/nexus.
        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()

        def _fake_load_config():
            return {"nexus_dir": str(nexus_dir)}

        monkeypatch.setattr(
            "nexus.config.load_config",
            _fake_load_config,
        )

        hook_bridge._reset_singleton_for_tests()
        monkeypatch.setenv("CLAUDECODE", "1")
        try:
            hook_bridge.emit("Stop", STOP_PAYLOAD)
        finally:
            hook_bridge._reset_singleton_for_tests()

        # tuples.db should now exist; that alone proves _emit_direct_auto
        # opened the production self-initialisation path end-to-end.
        assert (nexus_dir / "tuples.db").exists()


# ---------------------------------------------------------------------------
# nexus-yx9i: registry-load-failure structured warning + singleton caching
# ---------------------------------------------------------------------------


class TestRegistryLoadFailureWarning:
    """Wheel-install silent-drop: registry load failure must emit a WARN."""

    def test_unavailable_registry_logs_warning_and_returns(
        self, tmp_path, monkeypatch
    ) -> None:
        from structlog.testing import capture_logs

        from nexus.cockpit import hook_bridge
        from nexus.tuplespace.registry import RegistryLoadError

        hook_bridge._reset_singleton_for_tests()
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setattr(
            "nexus.config.load_config",
            lambda: {"nexus_dir": str(tmp_path)},
        )

        def _boom(_builtin):
            raise RegistryLoadError("simulated wheel-install missing builtin dir")

        monkeypatch.setattr(
            "nexus.cockpit.hook_bridge._load_registry_with_hooks",
            _boom,
        )

        # Must not raise — silent-drop is acceptable behaviour but it must
        # be observable via structured logging.
        with capture_logs() as logs:
            hook_bridge.emit("Stop", STOP_PAYLOAD)

        warns = [
            r for r in logs
            if r.get("event") == "hook_bridge_registry_unavailable"
        ]
        assert warns, (
            f"expected hook_bridge_registry_unavailable WARN, got {logs!r}"
        )
        assert warns[0]["log_level"] == "warning"
        assert "remediation" in warns[0]

        hook_bridge._reset_singleton_for_tests()

    def test_fast_path_skips_lock_when_cached(
        self, tmp_path, monkeypatch
    ) -> None:
        """After init, _get_or_init_resources must hit the dict without taking the lock."""
        from nexus.cockpit import hook_bridge

        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()
        monkeypatch.setattr(
            "nexus.config.load_config",
            lambda: {"nexus_dir": str(nexus_dir)},
        )
        monkeypatch.setenv("CLAUDECODE", "1")

        hook_bridge._reset_singleton_for_tests()
        # Prime the cache.
        hook_bridge._get_or_init_resources()

        # Patch the lock with a sentinel that records acquire() calls. The
        # fast-path returns BEFORE entering the with-block, so acquire() must
        # not be invoked on a warm cache.
        acquired: list[bool] = []
        real_lock = hook_bridge._singleton_lock

        class _SpyLock:
            def __enter__(self):
                acquired.append(True)
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        monkeypatch.setattr(hook_bridge, "_singleton_lock", _SpyLock())
        result = hook_bridge._get_or_init_resources()
        assert result is not None
        assert acquired == [], (
            "fast path must skip the lock when the cache is warm"
        )

        hook_bridge._reset_singleton_for_tests()

    def test_atexit_handler_registered_on_first_init(
        self, tmp_path, monkeypatch
    ) -> None:
        """atexit cleanup hook is registered exactly once across emit() calls."""
        import atexit as _atexit

        from nexus.cockpit import hook_bridge

        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()
        monkeypatch.setattr(
            "nexus.config.load_config",
            lambda: {"nexus_dir": str(nexus_dir)},
        )
        monkeypatch.setenv("CLAUDECODE", "1")

        hook_bridge._reset_singleton_for_tests()
        # Reset the registration flag so this test is order-independent.
        hook_bridge._atexit_registered = False

        registered: list[Any] = []
        real_register = _atexit.register

        def _counting_register(fn, *a, **kw):
            registered.append(fn)
            return real_register(fn, *a, **kw)

        monkeypatch.setattr(_atexit, "register", _counting_register)

        hook_bridge.emit("Stop", STOP_PAYLOAD)
        hook_bridge.emit("Stop", STOP_PAYLOAD)

        assert registered.count(hook_bridge._close_singleton_at_exit) == 1

        hook_bridge._reset_singleton_for_tests()

    def test_singleton_caches_resources_across_calls(
        self, tmp_path, monkeypatch
    ) -> None:
        """Second emit() in the same process must not reopen chroma."""
        from nexus.cockpit import hook_bridge

        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()
        monkeypatch.setattr(
            "nexus.config.load_config",
            lambda: {"nexus_dir": str(nexus_dir)},
        )
        monkeypatch.setenv("CLAUDECODE", "1")

        hook_bridge._reset_singleton_for_tests()

        # Observe that two emit() calls share one cached resource triple.
        hook_bridge.emit("Stop", STOP_PAYLOAD)
        snapshot_after_first = dict(hook_bridge._singleton)
        hook_bridge.emit("Stop", STOP_PAYLOAD)
        snapshot_after_second = dict(hook_bridge._singleton)

        assert snapshot_after_first == snapshot_after_second
        assert len(snapshot_after_first) == 1, (
            "exactly one (db, chroma) key should be cached"
        )

        hook_bridge._reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# nexus-hrz7: UnknownSubspaceError -> structured WARN (ordering hazard guard)
# ---------------------------------------------------------------------------


class TestUnknownSubspaceWarning:
    """When the registry lacks the requested subspace, log WARN — do not crash."""

    def test_unknown_subspace_emits_warning_not_exception(
        self, tmp_path, monkeypatch
    ) -> None:
        from structlog.testing import capture_logs

        from nexus.cockpit import hook_bridge
        from nexus.tuplespace.registry import UnknownSubspaceError

        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()
        monkeypatch.setattr(
            "nexus.config.load_config",
            lambda: {"nexus_dir": str(nexus_dir)},
        )
        monkeypatch.setenv("CLAUDECODE", "1")

        def _raise_unknown(**_kwargs):
            raise UnknownSubspaceError("subspace not registered")

        monkeypatch.setattr(
            "nexus.cockpit.hook_bridge._direct_out",
            _raise_unknown,
        )

        hook_bridge._reset_singleton_for_tests()
        with capture_logs() as logs:
            # Must not raise.
            hook_bridge.emit("Stop", STOP_PAYLOAD)

        warns = [
            r for r in logs
            if r.get("event") == "hook_bridge_unknown_subspace"
        ]
        assert warns, (
            f"expected hook_bridge_unknown_subspace WARN, got {logs!r}"
        )
        assert warns[0]["log_level"] == "warning"
        assert "remediation" in warns[0]
        assert warns[0]["subspace"] == "hook_events/assistant_turn_ended"

        hook_bridge._reset_singleton_for_tests()
