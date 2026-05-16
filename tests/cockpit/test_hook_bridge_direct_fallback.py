# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for hook_bridge._emit_direct_auto (nexus-2xxl).

The direct-mode auto path is what bridge scripts hit when the RDR-112
T2 daemon is unreachable. The daemon-routing regression test in
``test_hook_bridge_daemon_routing.py`` patches ``_emit_direct_auto``
out, leaving the function body itself at 0% coverage. These tests
exercise the real implementation: config path lookup, chroma dir
creation, registry load with the hook subdir, and the SQLite write.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.cockpit import hook_bridge


_PAYLOAD = {
    "session_id": "direct-fallback-session",
    "transcript_path": "/tmp/test.jsonl",
    "cwd": "/tmp/test-project",
    "permission_mode": "bypassPermissions",
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "echo hi"},
}


_TOOL_CALL_INTENT_YAML = """
name: hook_events/tool_call_intent
tier: project
content_type: text
embed_from: match_text
dimensions:
  actor:     { type: string, required: true }
  session:   { type: string, required: true }
  project:   { type: string, required: true }
  timestamp: { type: string, required: true }
  tool:      { type: string, required: false }
take:
  enabled: false
  mode: semantic
  default_lease_seconds: 60
read:
  default_floor: 0.0
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point load_config at a tmp nexus_dir and stub the builtin registry."""
    nexus_dir = tmp_path / "nexus"
    nexus_dir.mkdir()
    chroma_dir = nexus_dir / "chroma"  # created lazily by _emit_direct_auto

    # Stub load_config to return the tmp dir.
    def _fake_load_config():
        return {"nexus_dir": str(nexus_dir)}

    from nexus import config as _config_mod
    monkeypatch.setattr(_config_mod, "load_config", _fake_load_config)

    # Stub default_builtin_dir to a tmp dir holding the hook event YAML.
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "tool_call_intent.yml").write_text(_TOOL_CALL_INTENT_YAML)
    # Also create the hooks subdir so _load_registry_with_hooks doesn't error.
    (builtin / "hooks").mkdir()
    from nexus.tuplespace import registry as _registry_mod
    monkeypatch.setattr(
        _registry_mod, "default_builtin_dir", lambda: builtin
    )

    monkeypatch.setenv("CLAUDECODE", "1")
    return nexus_dir, builtin


def _tuples_in_db(db_path: Path, subspace: str) -> list[dict]:
    """Read tuples for a subspace via a fresh sqlite connection."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, subspace, content FROM tuples WHERE subspace = ?",
            (subspace,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmitDirectAutoFallback:
    """End-to-end coverage for _emit_direct_auto (RDR-111 nexus-2xxl)."""

    def test_no_daemon_discovery_falls_through_to_direct(
        self, isolated_config, monkeypatch
    ) -> None:
        """Daemon unreachable -> _emit_direct_auto persists the tuple."""
        nexus_dir, _builtin = isolated_config
        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: nexus_dir)

        # No discovery file in nexus_dir -> find_t2_daemon returns None.
        hook_bridge.emit("PreToolUse", _PAYLOAD)

        tuples = _tuples_in_db(
            nexus_dir / "tuples.db", "hook_events/tool_call_intent"
        )
        assert len(tuples) == 1
        assert tuples[0]["subspace"] == "hook_events/tool_call_intent"

    def test_daemon_rpc_error_falls_back_to_direct(
        self, isolated_config, monkeypatch
    ) -> None:
        """Discovery succeeds but RPC raises -> direct fallback handles it."""
        nexus_dir, _ = isolated_config

        # Plant a discovery file so find_t2_daemon returns info.
        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: nexus_dir)
        import json
        import os
        uid = os.getuid()
        disc_path = nexus_dir / f"t2_addr.{uid}"
        # Use a UDS path that does not exist, forcing tcp fallback in client.
        disc_path.write_text(json.dumps({
            "uds_path": "/nonexistent.sock",
            "tcp_host": "127.0.0.1",
            "tcp_port": 1,  # any unreachable port; T2Client connection raises
        }))

        hook_bridge.emit("PreToolUse", _PAYLOAD)

        tuples = _tuples_in_db(
            nexus_dir / "tuples.db", "hook_events/tool_call_intent"
        )
        assert len(tuples) == 1

    def test_direct_path_writes_event_row(
        self, isolated_config, monkeypatch
    ) -> None:
        """Writing a tuple via direct fallback fires the events trigger."""
        nexus_dir, _ = isolated_config
        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: nexus_dir)

        hook_bridge.emit("PreToolUse", _PAYLOAD)

        conn = sqlite3.connect(str(nexus_dir / "tuples.db"))
        conn.row_factory = sqlite3.Row
        try:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE subspace = 'hook_events/tool_call_intent'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert event_count == 1

    def test_direct_path_persists_dimensions(
        self, isolated_config, monkeypatch
    ) -> None:
        """The required dimensions land in the tuple's dimensions_json blob."""
        import json

        nexus_dir, _ = isolated_config
        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: nexus_dir)

        hook_bridge.emit("PreToolUse", _PAYLOAD)

        conn = sqlite3.connect(str(nexus_dir / "tuples.db"))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT dimensions_json FROM tuples "
                "WHERE subspace = 'hook_events/tool_call_intent'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        dims = json.loads(row["dimensions_json"])
        # route_payload populates the four required dimensions.
        assert dims["session"] == "direct-fallback-session"
        assert dims["project"] == "/tmp/test-project"
        assert dims["actor"]
        assert dims["timestamp"]
        assert dims["tool"] == "Bash"

    def test_direct_path_creates_chroma_dir_if_missing(
        self, isolated_config, monkeypatch
    ) -> None:
        """PersistentClient must create ``nexus_dir/chroma`` on first use."""
        nexus_dir, _ = isolated_config
        chroma_dir = nexus_dir / "chroma"
        assert not chroma_dir.exists()

        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: nexus_dir)

        hook_bridge.emit("PreToolUse", _PAYLOAD)

        assert chroma_dir.exists() and chroma_dir.is_dir()
