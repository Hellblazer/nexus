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
  hook_event_name: { type: string, required: true }
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
  hook_event_name: { type: string, required: true }
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


class TestDaemonModeSkip:
    """Under NX_STORAGE_MODE=daemon the bridge must skip cleanly, not
    race the daemon's migration runner via direct open_tuples_db.
    """

    def test_emit_skips_under_daemon_mode(
        self, db_conn, hook_index, hook_registry
    ) -> None:
        from nexus.cockpit import hook_bridge

        called = []

        def _fake_out(**kwargs):
            called.append(kwargs)
            return "id"

        with patch("nexus.cockpit.hook_bridge._direct_out", _fake_out):
            with patch.dict(
                os.environ,
                {"CLAUDECODE": "1", "NX_STORAGE_MODE": "daemon"},
            ):
                hook_bridge.emit(
                    "PreToolUse",
                    PRETOOLUSE_PAYLOAD,
                    conn=db_conn,
                    index=hook_index,
                    registry=hook_registry,
                )

        assert called == [], (
            "bridge must not write under NX_STORAGE_MODE=daemon — would "
            "race daemon migration runner (RDR-112 §9)"
        )


class TestOpenTuplesDbGate:
    """open_tuples_db must refuse under NX_STORAGE_MODE=daemon unless the
    explicit opt-out is set (which the daemon itself uses).
    """

    def test_open_tuples_db_raises_under_daemon_mode(
        self, tmp_path, monkeypatch
    ) -> None:
        from nexus.db import DaemonModeDiagnosticError
        from nexus.tuplespace.store import open_tuples_db

        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
        with pytest.raises(DaemonModeDiagnosticError):
            open_tuples_db(tmp_path / "t.db")

    def test_open_tuples_db_allows_opt_out_under_daemon_mode(
        self, tmp_path, monkeypatch
    ) -> None:
        """Daemon process itself passes allow_direct_in_daemon_mode=True."""
        from nexus.tuplespace.store import open_tuples_db

        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
        conn = open_tuples_db(
            tmp_path / "t.db", allow_direct_in_daemon_mode=True
        )
        conn.close()


class TestBridgeDisableGate:
    """NX_BRIDGE_DISABLE is the per-bridge escape hatch (CLAUDECODE-independent)."""

    def test_emit_skips_when_nx_bridge_disable_set(
        self, db_conn, hook_index, hook_registry
    ) -> None:
        from nexus.cockpit import hook_bridge

        called = []

        def _fake_out(**kwargs):
            called.append(kwargs)
            return "id"

        with patch("nexus.cockpit.hook_bridge._direct_out", _fake_out):
            with patch.dict(
                os.environ, {"CLAUDECODE": "1", "NX_BRIDGE_DISABLE": "1"}
            ):
                hook_bridge.emit(
                    "PreToolUse",
                    PRETOOLUSE_PAYLOAD,
                    conn=db_conn,
                    index=hook_index,
                    registry=hook_registry,
                )

        assert called == [], (
            "NX_BRIDGE_DISABLE must suppress emission even when CLAUDECODE is set"
        )


class TestDimensionValueCaps:
    """User-controlled dim values are length-capped before storage."""

    def test_cwd_capped_at_512_bytes(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        payload = dict(STOP_PAYLOAD)
        payload["cwd"] = "/" + "a" * 5000
        _, dims, _ = route_payload("Stop", payload)
        assert len(dims["project"]) == 512

    def test_session_id_capped_at_512_bytes(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        payload = dict(STOP_PAYLOAD)
        payload["session_id"] = "x" * 5000
        _, dims, _ = route_payload("Stop", payload)
        assert len(dims["session"]) == 512
        assert len(dims["actor"]) == 512  # actor derives from session_id

    def test_tool_name_capped_at_512_bytes(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        payload = dict(PRETOOLUSE_PAYLOAD)
        payload["tool_name"] = "T" * 5000
        _, dims, _ = route_payload("PreToolUse", payload)
        assert len(dims["tool"]) == 512


class TestContentByteTruncation:
    """_build_content truncates by bytes (not codepoints) so the chroma
    quota validator cannot reject high-unicode payloads.
    """

    def test_emoji_payload_stays_under_safe_chunk_bytes(self) -> None:
        from nexus.cockpit.hook_bridge import _build_content
        from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES

        # 4100 emoji (each 4 bytes when UTF-8 encoded) gives 16,400 raw bytes,
        # past SAFE_CHUNK_BYTES=12288 AND past MAX_DOCUMENT_BYTES=16384.
        payload = {"tool_response": "\U0001F600" * 4100}
        out = _build_content("PostToolUse", payload)
        assert len(out.encode("utf-8")) <= SAFE_CHUNK_BYTES, (
            f"byte-len {len(out.encode('utf-8'))} exceeds SAFE_CHUNK_BYTES={SAFE_CHUNK_BYTES}"
        )

    def test_ascii_payload_unchanged_when_under_cap(self) -> None:
        from nexus.cockpit.hook_bridge import _build_content

        payload = {"prompt": "hello world"}
        out = _build_content("UserPromptSubmit", payload)
        assert "hello world" in out


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
    argv: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a bridge script with given stdin, return CompletedProcess."""
    script_path = _SCRIPTS_DIR / script_name
    run_env = dict(os.environ)
    if env is not None:
        run_env.update(env)
    cmd = [sys.executable, str(script_path)]
    if argv:
        cmd.extend(argv)
    return subprocess.run(
        cmd,
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

    def test_output_for_hook_permissionrequest_is_pure(self) -> None:
        """output_for_hook is pure Python, not gated on env; PermissionRequest always allows."""
        from nexus.cockpit.hook_bridge import output_for_hook

        out = output_for_hook("PermissionRequest")
        assert out is not None
        assert "allow" in out


class TestStopArgvFallback:
    """orb_bridge_stop.py must accept the variant via argv so a payload without
    hook_event_name is not silently stored as ``Stop`` when it was ``StopFailure``.
    """

    def test_stop_script_picks_up_stopfailure_from_argv(self) -> None:
        """No hook_event_name in payload + argv[1]=StopFailure → emit as StopFailure."""
        # Strip hook_event_name to simulate CC payloads that omit it.
        payload = {k: v for k, v in STOPFAILURE_PAYLOAD.items() if k != "hook_event_name"}
        # Without CLAUDECODE set, emit is a no-op but the script still runs
        # through hook_type discrimination — assert exit 0 and no stdout.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = _run_script(
            "orb_bridge_stop.py",
            json.dumps(payload),
            env=env,
            argv=["StopFailure"],
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


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
# nexus-usic: hook_event_name discriminator dimension on collapsed subspaces
# ---------------------------------------------------------------------------


class TestEventTypeDiscriminator:
    """Stop/StopFailure and SessionStart/SessionEnd carry hook_event_name dim."""

    def test_stop_dim_hook_event_name_is_stop(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        _, dims, _ = route_payload("Stop", STOP_PAYLOAD)
        assert dims["hook_event_name"] == "Stop"

    def test_stopfailure_dim_hook_event_name_is_stopfailure(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        _, dims, _ = route_payload("StopFailure", STOPFAILURE_PAYLOAD)
        assert dims["hook_event_name"] == "StopFailure"

    def test_sessionstart_dim_hook_event_name_is_sessionstart(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        _, dims, _ = route_payload("SessionStart", SESSION_START_PAYLOAD)
        assert dims["hook_event_name"] == "SessionStart"

    def test_sessionend_dim_hook_event_name_is_sessionend(self) -> None:
        from nexus.cockpit.hook_bridge import route_payload

        _, dims, _ = route_payload("SessionEnd", SESSION_END_PAYLOAD)
        assert dims["hook_event_name"] == "SessionEnd"

    def test_stop_match_text_is_bare_when_no_last_message(self) -> None:
        """Stop without last_assistant_message embeds the bare hook type.

        Earlier revisions embedded session_id+cwd here, but those are
        session-constants — they collapse every turn into the same vector.
        The discriminator lives in the hook_event_name DIMENSION, not in
        match_text.
        """
        from nexus.cockpit.hook_bridge import route_payload

        _, _, match_text = route_payload("Stop", STOP_PAYLOAD)
        assert match_text == "Stop"

    def test_stop_match_text_uses_last_assistant_message_when_present(self) -> None:
        """When the payload carries last_assistant_message, prefer it."""
        from nexus.cockpit.hook_bridge import route_payload

        payload = dict(STOP_PAYLOAD)
        payload["last_assistant_message"] = "Tests pass. Tagging release."
        _, _, match_text = route_payload("Stop", payload)
        assert "Stop" in match_text
        assert "Tests pass" in match_text

    def test_sessionstart_match_text_is_bare(self) -> None:
        """SessionStart/SessionEnd have no per-event content — match_text is bare."""
        from nexus.cockpit.hook_bridge import route_payload

        _, _, match_text = route_payload("SessionStart", SESSION_START_PAYLOAD)
        assert match_text == "SessionStart"
        _, _, match_text = route_payload("SessionEnd", SESSION_END_PAYLOAD)
        assert match_text == "SessionEnd"

    def test_all_hooks_populate_hook_event_name(self) -> None:
        """hook_event_name is populated on every dim dict, not just collapsed
        subspaces. Defensive design: removes the silent-drop class where a
        future schema marks the dim required on a subspace whose hook type
        forgot to populate it.
        """
        from nexus.cockpit.hook_bridge import route_payload

        for hook_type, payload in [
            ("PreToolUse", PRETOOLUSE_PAYLOAD),
            ("PostToolUse", POSTTOOLUSE_PAYLOAD),
            ("SubagentStop", SUBAGENT_STOP_PAYLOAD),
            ("UserPromptSubmit", USER_PROMPT_PAYLOAD),
            ("Notification", NOTIFICATION_PAYLOAD),
        ]:
            _, dims, _ = route_payload(hook_type, payload)
            assert dims["hook_event_name"] == hook_type, (
                f"hook_event_name should equal the hook type for {hook_type}"
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

    def test_step1_layout_state_and_connection_manifest_present(self) -> None:
        """RDR-111 Step 1 ships three subspace sets: hook_events/* (7),
        connection_manifest (1), layout_state/<profile> (1). All must load.
        """
        from nexus.cockpit.hook_bridge import _load_registry_with_hooks

        registry = _load_registry_with_hooks(_PROD_HOOKS_DIR)
        # connection_manifest is concrete (no params).
        cm = registry.get_schema_for("connection_manifest")
        assert cm is not None
        assert "producer" in cm.dimensions
        assert "consumer" in cm.dimensions

        # layout_state is templated by profile.
        ls = registry.get_schema_for("layout_state/dev")
        assert ls is not None
        # event_type here is the layout_state stylesheet selector — keyed
        # on subspace path. Distinct from hook_event_name (the hook-event
        # discriminator). The two dims must coexist without collision.
        assert "event_type" in ls.dimensions
        assert "profile" in ls.dimensions

    def test_production_collapsed_subspaces_require_hook_event_name(self) -> None:
        """assistant_turn_ended and session_lifecycle must require hook_event_name."""
        from nexus.cockpit.hook_bridge import _load_registry_with_hooks

        registry = _load_registry_with_hooks(_PROD_HOOKS_DIR)
        for subspace in (
            "hook_events/assistant_turn_ended",
            "hook_events/session_lifecycle",
        ):
            tmpl = registry.get_schema_for(subspace)
            dims = tmpl.dimensions
            assert "hook_event_name" in dims, (
                f"{subspace}: hook_event_name discriminator required (nexus-usic)"
            )
            assert dims["hook_event_name"]["required"] is True


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

        # File existence proves _emit_direct_auto opened the path; the
        # row count proves a tuple was actually written (not just that
        # the SQLite file was created and then silently failed). Per the
        # project "exact assertions, not file-exists" rule.
        assert (nexus_dir / "tuples.db").exists()
        verify_conn = sqlite3.connect(nexus_dir / "tuples.db")
        try:
            (count,) = verify_conn.execute(
                "SELECT COUNT(*) FROM tuples WHERE subspace = ?",
                ("hook_events/assistant_turn_ended",),
            ).fetchone()
        finally:
            verify_conn.close()
        assert count == 1, (
            f"expected exactly one assistant_turn_ended tuple, got {count}"
        )


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
        assert warns[0]["error_type"] == "RegistryLoadError"

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

    def test_close_singleton_at_exit_closes_conns_and_clears_dict(
        self, tmp_path, monkeypatch
    ) -> None:
        """The atexit body itself must close all cached conns and clear the dict."""
        from nexus.cockpit import hook_bridge

        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()
        monkeypatch.setattr(
            "nexus.config.load_config",
            lambda: {"nexus_dir": str(nexus_dir)},
        )
        monkeypatch.setenv("CLAUDECODE", "1")

        hook_bridge._reset_singleton_for_tests()
        # Prime the cache so we have a real connection to close.
        resources = hook_bridge._get_or_init_resources()
        assert resources is not None
        conn = resources[0]
        assert len(hook_bridge._singleton) == 1

        # Directly invoke the atexit body.
        hook_bridge._close_singleton_at_exit()

        assert hook_bridge._singleton == {}
        # The conn must now be closed — any operation should raise
        # ProgrammingError.
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

        hook_bridge._reset_singleton_for_tests()

    def test_warm_path_skips_load_config(
        self, tmp_path, monkeypatch
    ) -> None:
        """After init, repeat _get_or_init_resources must not re-read config."""
        from nexus.cockpit import hook_bridge

        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()

        call_count = {"n": 0}

        def _counting_load_config():
            call_count["n"] += 1
            return {"nexus_dir": str(nexus_dir)}

        monkeypatch.setattr("nexus.config.load_config", _counting_load_config)
        monkeypatch.setenv("CLAUDECODE", "1")

        hook_bridge._reset_singleton_for_tests()
        hook_bridge._get_or_init_resources()  # cold path — reads config
        first = call_count["n"]
        hook_bridge._get_or_init_resources()  # warm path — must skip
        hook_bridge._get_or_init_resources()
        assert call_count["n"] == first, (
            f"warm path should not re-read load_config; "
            f"calls went {first} -> {call_count['n']}"
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

        # _reset_singleton_for_tests() now clears _atexit_registered too
        # (substantive review: prior version left a test-order foot-gun).
        hook_bridge._reset_singleton_for_tests()

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


class TestSchemaViolationLogged:
    """SubspaceSchemaError must surface under its own structured event."""

    def test_missing_required_dim_logs_schema_violation(
        self, db_conn, hook_index, hook_registry, monkeypatch
    ) -> None:
        from structlog.testing import capture_logs

        from nexus.cockpit import hook_bridge
        from nexus.tuplespace.api import SubspaceSchemaError

        monkeypatch.setenv("CLAUDECODE", "1")

        def _raise_schema(**_kwargs):
            raise SubspaceSchemaError("missing required dim 'hook_event_name'")

        monkeypatch.setattr(
            "nexus.cockpit.hook_bridge._direct_out",
            _raise_schema,
        )

        with capture_logs() as logs:
            # Must not raise — bridge contract is exit 0 on any error.
            hook_bridge.emit(
                "Stop",
                STOP_PAYLOAD,
                conn=db_conn,
                index=hook_index,
                registry=hook_registry,
            )

        violations = [
            r for r in logs
            if r.get("event") == "hook_bridge_schema_violation"
        ]
        assert violations, (
            f"expected hook_bridge_schema_violation event, got {logs!r}"
        )
        assert violations[0]["log_level"] == "error"
        assert violations[0]["hook_type"] == "Stop"
        # The dimension list must include hook_event_name so a debugger
        # can see exactly what was sent.
        assert "hook_event_name" in violations[0]["dimensions"]


class TestSqliteOperationalErrorTransient:
    """sqlite3.OperationalError (e.g. database-locked) is logged as transient."""

    def test_locked_db_logs_transient_warning(
        self, db_conn, hook_index, hook_registry, monkeypatch
    ) -> None:
        from structlog.testing import capture_logs

        from nexus.cockpit import hook_bridge

        monkeypatch.setenv("CLAUDECODE", "1")

        def _raise_locked(**_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("nexus.cockpit.hook_bridge._direct_out", _raise_locked)

        with capture_logs() as logs:
            hook_bridge.emit(
                "Stop",
                STOP_PAYLOAD,
                conn=db_conn,
                index=hook_index,
                registry=hook_registry,
            )

        transients = [
            r for r in logs
            if r.get("event") == "hook_bridge_transient"
        ]
        assert transients, f"expected hook_bridge_transient WARN, got {logs!r}"
        assert transients[0]["log_level"] == "warning"
        assert transients[0]["error_type"] == "OperationalError"


class TestSingletonPoisonInvalidation:
    """A closed conn or corrupted db poisons the singleton; invalidate + ERROR."""

    def test_programming_error_invalidates_singleton(
        self, tmp_path, monkeypatch
    ) -> None:
        from structlog.testing import capture_logs

        from nexus.cockpit import hook_bridge

        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()
        monkeypatch.setattr(
            "nexus.config.load_config",
            lambda: {"nexus_dir": str(nexus_dir)},
        )
        monkeypatch.setenv("CLAUDECODE", "1")

        hook_bridge._reset_singleton_for_tests()
        # Prime so we have a real cached triple to invalidate.
        hook_bridge._get_or_init_resources()
        assert len(hook_bridge._singleton) == 1

        def _raise_poison(**_kwargs):
            raise sqlite3.ProgrammingError("Cannot operate on a closed database")

        monkeypatch.setattr("nexus.cockpit.hook_bridge._direct_out", _raise_poison)

        with capture_logs() as logs:
            hook_bridge.emit("Stop", STOP_PAYLOAD)

        poisons = [
            r for r in logs
            if r.get("event") == "hook_bridge_singleton_poison"
        ]
        assert poisons, f"expected hook_bridge_singleton_poison, got {logs!r}"
        assert poisons[0]["log_level"] == "error"
        assert poisons[0]["error_type"] == "ProgrammingError"
        # Singleton must have been cleared so a future call rebuilds.
        assert hook_bridge._singleton == {}
        assert hook_bridge._cached_key is None

        hook_bridge._reset_singleton_for_tests()

    def test_database_error_also_invalidates_singleton(
        self, tmp_path, monkeypatch
    ) -> None:
        from nexus.cockpit import hook_bridge

        nexus_dir = tmp_path / "nexus"
        nexus_dir.mkdir()
        monkeypatch.setattr(
            "nexus.config.load_config",
            lambda: {"nexus_dir": str(nexus_dir)},
        )
        monkeypatch.setenv("CLAUDECODE", "1")

        hook_bridge._reset_singleton_for_tests()
        hook_bridge._get_or_init_resources()
        assert len(hook_bridge._singleton) == 1

        monkeypatch.setattr(
            "nexus.cockpit.hook_bridge._direct_out",
            lambda **_: (_ for _ in ()).throw(sqlite3.DatabaseError("file is not a database")),
        )

        hook_bridge.emit("Stop", STOP_PAYLOAD)
        assert hook_bridge._singleton == {}

        hook_bridge._reset_singleton_for_tests()


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
