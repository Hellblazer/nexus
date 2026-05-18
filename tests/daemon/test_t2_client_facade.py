# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P3.2 (nexus-vm3t): T2Client drop-in compatibility tests.

The 47 ``with t2_ctx() as db: db.<facade>(...)`` call sites across
src/nexus rely on T2Database's facade delegate methods (db.put,
db.get, db.search, ...). Phase 3 flipped t2_ctx to return T2Client in
daemon mode but T2Client had only store proxies (client.memory.put,
client.plans.save_plan, ...) — every facade call site silently broke.

This test file locks in the facade-delegate contract. Every facade
method must:
- Exist with the same signature as the T2Database counterpart
  (signature-parity check via inspect.signature)
- Delegate to the appropriate store proxy with the call-site args
  intact (round-trip check against a live daemon)
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from pathlib import Path

import pytest

from nexus.catalog.tumbler import DocumentRecord, LinkRecord, OwnerRecord
from nexus.daemon.t2_client import T2Client
from nexus.daemon.t2_daemon import T2Daemon
from nexus.db.t2 import T2Database


# ---------------------------------------------------------------------------
# Fixture: a real T2Daemon + T2Client
# ---------------------------------------------------------------------------


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
def t2db(tmp_path: Path) -> T2Database:
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "config"


@pytest.fixture
def daemon_client(t2db: T2Database, config_dir: Path):
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    client = T2Client(uds_path=daemon.uds_path)
    try:
        yield client
    finally:
        client.close()
        _stop_daemon(daemon, loop)


# ---------------------------------------------------------------------------
# Signature parity — every facade method on T2Database must exist on T2Client
# with the same parameter names (modulo self).
# ---------------------------------------------------------------------------


FACADE_METHODS = [
    "put",
    "get",
    "resolve_title",
    "search",
    "list_entries",
    "get_projects_with_prefix",
    "search_glob",
    "search_by_tag",
    "get_all",
    "delete",
    "find_overlapping_memories",
    "merge_memories",
    "flag_stale_memories",
    "save_plan",
    "search_plans",
    "list_plans",
    "plan_exists",
    "log_relevance",
    "log_relevance_batch",
    "get_relevance_log",
    "expire_relevance_log",
    "expire",
]


class TestFacadeSignatureParity:
    @pytest.mark.parametrize("name", FACADE_METHODS)
    def test_method_exists(self, name: str) -> None:
        """T2Client must expose every facade method T2Database exposes."""
        assert hasattr(T2Client, name), (
            f"T2Client missing facade method {name!r}; the 47 "
            f"`with t2_ctx() as db: db.{name}(...)` call sites would "
            f"break under daemon mode."
        )

    @pytest.mark.parametrize("name", FACADE_METHODS)
    def test_signature_parity(self, name: str) -> None:
        """Parameter names match between T2Database.<facade> and
        T2Client.<facade> (minus self)."""
        db_sig = inspect.signature(getattr(T2Database, name))
        client_sig = inspect.signature(getattr(T2Client, name))
        db_params = list(db_sig.parameters)[1:]  # drop self
        client_params = list(client_sig.parameters)[1:]  # drop self
        assert db_params == client_params, (
            f"{name}: parameter list mismatch.\n"
            f"  T2Database: {db_params}\n"
            f"  T2Client:   {client_params}"
        )


# ---------------------------------------------------------------------------
# Round-trip: every key facade method actually works through the daemon
# ---------------------------------------------------------------------------


class TestFacadeRoundTrip:
    def test_put_then_get_by_id(self, daemon_client: T2Client) -> None:
        row_id = daemon_client.put(
            project="vm3t-test", title="note.md", content="hello"
        )
        assert row_id > 0
        entry = daemon_client.get(id=row_id)
        assert entry is not None
        assert entry["content"] == "hello"

    def test_get_by_project_title(self, daemon_client: T2Client) -> None:
        daemon_client.put(
            project="vm3t-test", title="alpha.md", content="alpha body"
        )
        entry = daemon_client.get(project="vm3t-test", title="alpha.md")
        assert entry is not None
        assert entry["content"] == "alpha body"

    def test_resolve_title_exact(self, daemon_client: T2Client) -> None:
        daemon_client.put(
            project="vm3t-test", title="alpha.md", content="alpha body"
        )
        resolved, candidates = daemon_client.resolve_title(
            project="vm3t-test", title="alpha.md"
        )
        assert resolved is not None
        assert resolved["content"] == "alpha body"
        assert candidates == []

    def test_search(self, daemon_client: T2Client) -> None:
        daemon_client.put(
            project="vm3t-test", title="note.md", content="uniqueterm content"
        )
        results = daemon_client.search(query="uniqueterm")
        assert len(results) >= 1
        assert any(r["title"] == "note.md" for r in results)

    def test_list_entries(self, daemon_client: T2Client) -> None:
        daemon_client.put(project="vm3t-test", title="a.md", content="a")
        daemon_client.put(project="vm3t-test", title="b.md", content="b")
        entries = daemon_client.list_entries(project="vm3t-test")
        titles = {e["title"] for e in entries}
        assert {"a.md", "b.md"} <= titles

    def test_delete_by_id(self, daemon_client: T2Client) -> None:
        row_id = daemon_client.put(
            project="vm3t-test", title="todelete.md", content="bye"
        )
        ok = daemon_client.delete(id=row_id)
        assert ok is True
        # Subsequent get returns None
        assert daemon_client.get(id=row_id) is None

    def test_save_plan_then_list(self, daemon_client: T2Client) -> None:
        daemon_client.save_plan(
            query="how to vm3t",
            plan_json='{"steps":[]}',
            outcome="success",
            tags="test",
            project="vm3t-test",
        )
        plans = daemon_client.list_plans(project="vm3t-test", limit=10)
        assert any(p["query"] == "how to vm3t" for p in plans)


# ---------------------------------------------------------------------------
# Context manager still works alongside facade methods
# ---------------------------------------------------------------------------


class TestContextManagerWithFacade:
    def test_with_block_uses_facade_methods(self, daemon_client: T2Client) -> None:
        """The integration pattern that broke in production:
        ``with t2_ctx() as db: db.put(...)``."""
        with daemon_client as db:
            row_id = db.put(
                project="vm3t-test", title="ctx.md", content="ctx body"
            )
            entry = db.get(id=row_id)
        assert entry is not None
        assert entry["content"] == "ctx body"
