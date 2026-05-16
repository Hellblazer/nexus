# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cockpit panels under NX_STORAGE_MODE=daemon (RDR-112 §A2, nexus-x65c).

The cockpit's active-claims and recent-events panels must NOT open
a second SQLite handle on ``tuples.db`` when a daemon owns it. Under
``NX_STORAGE_MODE=daemon`` they route through the daemon's
``tuplespace.list_active_claims`` / ``tuplespace.recent_events`` RPCs.

Discovery failures surface as ``ClickException`` rather than a silent
fallback to direct-read; the boundary is the contract.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from click.testing import CliRunner

from nexus.commands.cockpit import cockpit_group
from nexus.daemon.t2_daemon import T2Daemon
from nexus.daemon.tuplespace_service import TuplespaceService
from nexus.tuplespace.api import out, take
from nexus.tuplespace.registry import Registry


_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:     { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:   { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.0
  margin: 0.0
  default_lease_seconds: 300
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
    (builtin / "tasks.yml").write_text(_TASKS_YAML)
    return Registry.load(builtin)


@pytest.fixture
def chroma_client():
    client = chromadb.EphemeralClient()
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


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


def _seed_claimed_tuple(daemon: T2Daemon) -> None:
    """Post a tuple and immediately claim it via the daemon's service conn."""
    service = daemon._tuplespace_service
    tid = out(
        conn=service._conn,
        index=service._index,
        registry=service._registry,
        subspace="tasks/nexus",
        content="active-claim target",
        dimensions={
            "status": "open",
            "priority": "P2",
            "created_by": "agent-X",
        },
    )
    result = take(
        conn=service._conn,
        index=service._index,
        registry=service._registry,
        subspace="tasks/nexus",
        query="active-claim",
        claimant="agent-A",
        lease_seconds=300.0,
    )
    assert result is not None, "seed take must succeed"
    return tid


# ---------------------------------------------------------------------------
# Daemon-mode tests
# ---------------------------------------------------------------------------


class TestCockpitPanelsDaemonMode:
    """Under NX_STORAGE_MODE=daemon, panels route via the daemon."""

    def test_active_claims_panel_routes_through_daemon(
        self,
        tmp_path: Path,
        test_registry: Registry,
        chroma_client,
        monkeypatch,
    ) -> None:
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

        _seed_claimed_tuple(daemon)

        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: config_dir)
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        # Direct-read fallback must NOT fire under daemon mode.
        with patch(
            "nexus.commands.cockpit.sqlite3.connect",
            side_effect=AssertionError(
                "panel opened a direct sqlite3.connect under daemon mode"
            ),
        ):
            try:
                runner = CliRunner()
                result = runner.invoke(
                    cockpit_group, ["show", "active-claims"]
                )
            finally:
                _stop_daemon(daemon, loop)

        assert result.exit_code == 0, result.output
        assert "Active Claims" in result.output
        assert "tasks/nexus" in result.output
        assert "agent-A" in result.output

    def test_recent_events_panel_routes_through_daemon(
        self,
        tmp_path: Path,
        test_registry: Registry,
        chroma_client,
        monkeypatch,
    ) -> None:
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
        # Posting a tuple fires the events trigger.
        out(
            conn=service._conn,
            index=service._index,
            registry=service._registry,
            subspace="tasks/nexus",
            content="event seed",
            dimensions={
                "status": "open",
                "priority": "P1",
                "created_by": "agent-X",
            },
        )

        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: config_dir)
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        with patch(
            "nexus.commands.cockpit.sqlite3.connect",
            side_effect=AssertionError(
                "panel opened a direct sqlite3.connect under daemon mode"
            ),
        ):
            try:
                runner = CliRunner()
                result = runner.invoke(
                    cockpit_group, ["show", "recent-events", "--limit", "5"]
                )
            finally:
                _stop_daemon(daemon, loop)

        assert result.exit_code == 0, result.output
        assert "Recent Events" in result.output
        assert "tasks/nexus" in result.output

    def test_daemon_mode_missing_discovery_fails_loud(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """No discovery file under daemon mode -> ClickException, not silent fallback."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        from nexus.daemon import discovery as _disc
        monkeypatch.setattr(_disc, "nexus_config_dir", lambda: config_dir)
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(cockpit_group, ["show", "active-claims"])
        assert result.exit_code != 0
        assert "discovery file" in result.output.lower() or "daemon" in result.output.lower()


# ---------------------------------------------------------------------------
# Standalone-mode regression (existing direct-read path preserved)
# ---------------------------------------------------------------------------


class TestCockpitPanelsStandaloneMode:
    """Without NX_STORAGE_MODE, the existing direct-read still works."""

    def test_standalone_active_claims_still_uses_direct_read(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from nexus.tuplespace.store import open_tuples_db

        db_path = tmp_path / "tuples.db"
        c = open_tuples_db(db_path)
        c.close()  # schema only — empty DB renders cleanly

        monkeypatch.setenv("NX_COCKPIT_TUPLES_DB", str(db_path))
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)

        runner = CliRunner()
        result = runner.invoke(cockpit_group, ["show", "active-claims"])
        assert result.exit_code == 0, result.output
