# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 nexus-6s8v: hook_bridge daemon-mode routing.

Verifies the post-flip behaviour of ``_ROUTING_TBA = "daemon"``:

1. With CLAUDECODE set and a reachable daemon, ``emit()`` routes through
   the daemon's ``tuplespace.out`` RPC (no direct sqlite touch).
2. With CLAUDECODE set and NO daemon discovery file, ``emit()`` falls
   back to direct mode (existing behaviour, defense-in-depth).
3. Without CLAUDECODE, no side effect (RF-5 gate, unchanged).
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest

from nexus.cockpit import hook_bridge
from nexus.daemon.t2_daemon import T2Daemon
from nexus.daemon.tuplespace_service import TuplespaceService
from nexus.tuplespace.registry import Registry


_TASKS_YAML = """
name: hook_events/tool_call_intent
tier: project
content_type: text
embed_from: match_text
dimensions:
  actor: { type: string, required: true }
  session: { type: string, required: true }
  project: { type: string, required: true }
  timestamp: { type: string, required: true }
  tool: { type: string, required: false }
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
def test_registry(tmp_path: Path) -> Registry:
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "tool_call_intent.yml").write_text(_TASKS_YAML)
    return Registry.load(builtin)


def _run_daemon(daemon: T2Daemon) -> asyncio.AbstractEventLoop:
    started = threading.Event()
    loop = asyncio.new_event_loop()

    def _thread() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    started.wait(timeout=5.0)
    return loop


def _stop_daemon(daemon: T2Daemon, loop: asyncio.AbstractEventLoop) -> None:
    asyncio.run_coroutine_threadsafe(daemon.stop(), loop).result(timeout=5.0)
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture
def chroma_client():
    client = chromadb.EphemeralClient()
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


_PAYLOAD = {
    "session_id": "test-session-xyz",
    "cwd": "/tmp/test-project",
    "tool_name": "Bash",
    "tool_input": {"command": "echo hi"},
}


class TestDaemonRouting:
    def test_emit_routes_through_daemon_when_reachable(
        self,
        tmp_path: Path,
        test_registry: Registry,
        chroma_client,
        monkeypatch,
    ) -> None:
        """With a reachable daemon, emit() calls tuplespace.out RPC.

        We assert behaviourally: the daemon's TuplespaceService records
        the post in its tuples.db, and direct-mode is NOT touched (the
        _emit_direct_auto path is patched to raise so we catch any
        fallthrough).
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        tuples_db_path = config_dir / "tuples.db"

        service = TuplespaceService(
            tuples_db_path=tuples_db_path,
            chroma_client=chroma_client,
            registry=test_registry,
        )
        daemon = T2Daemon(
            config_dir=config_dir,
            tuples_db_path=tuples_db_path,
            tuplespace_service=service,
        )
        loop = _run_daemon(daemon)

        # Point discovery at the test daemon's config_dir.
        # find_t2_daemon() consults nexus_config_dir() by default — patch it.
        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: config_dir)
        # Also gate the discovery path used by hook_bridge directly.

        # Set CLAUDECODE so RF-5 doesn't short-circuit.
        monkeypatch.setenv("CLAUDECODE", "1")

        # If anything falls through to direct mode, blow up.
        def _no_direct(*_args, **_kwargs):
            raise AssertionError(
                "_emit_direct_auto was called even though daemon was reachable"
            )

        try:
            with patch.object(hook_bridge, "_emit_direct_auto", _no_direct):
                hook_bridge.emit("PreToolUse", _PAYLOAD)
        finally:
            _stop_daemon(daemon, loop)

        # The post landed in tuples.db via the daemon. Open a separate
        # connection (daemon is stopped now) and count rows.
        import sqlite3
        conn = sqlite3.connect(str(tuples_db_path))
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM tuples WHERE subspace = ?",
                ("hook_events/tool_call_intent",),
            ).fetchone()
        finally:
            conn.close()
        assert count == 1, f"expected 1 daemon-posted tuple, got {count}"

    def test_emit_falls_back_to_direct_when_no_daemon(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """No discovery file -> _emit_direct_auto is called."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        # Point discovery at an empty dir so find_t2_daemon returns None.
        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: config_dir)
        monkeypatch.setenv("CLAUDECODE", "1")

        called: list[dict] = []

        def _fake_direct(*, subspace, content, dimensions, match_text):
            called.append({"subspace": subspace})

        with patch.object(hook_bridge, "_emit_direct_auto", _fake_direct):
            hook_bridge.emit("PreToolUse", _PAYLOAD)

        assert len(called) == 1
        assert called[0]["subspace"] == "hook_events/tool_call_intent"

    def test_emit_skips_without_claudecode(
        self,
        monkeypatch,
    ) -> None:
        """RF-5: no CLAUDECODE => no side effect, regardless of routing."""
        monkeypatch.delenv("CLAUDECODE", raising=False)

        called: list[dict] = []

        def _fake_direct(*args, **kwargs):
            called.append({})

        with patch.object(hook_bridge, "_emit_direct_auto", _fake_direct):
            hook_bridge.emit("PreToolUse", _PAYLOAD)

        assert called == []

    def test_routing_constant_is_daemon(self) -> None:
        """Regression: nexus-6s8v flipped _ROUTING_TBA to 'daemon'."""
        assert hook_bridge._ROUTING_TBA == "daemon"
