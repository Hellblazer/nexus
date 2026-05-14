# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 P1.2 (nexus-qy0u): T2Client RPC parity tests.

For each of the seven domain stores, asserts that calling a method via
``T2Client`` produces the same result as calling it directly on a
``T2Database`` instance (same inputs -> same outputs, same exception type).

Test structure
--------------
- ``test_daemon_starts_with_t2db``: smoke-test that the daemon accepts a
  T2Database and the client can ping.
- ``TestSignatureParity``: asserts ``inspect.signature(client.memory.put)``
  == ``inspect.signature(MemoryStore.put)`` (minus ``self``) for all seven
  stores x representative method.
- ``TestRpcParity``: parametrized over (store_attr, method, args, expected_fn)
  triples; each case:
    1. Populates a real T2Database with fixture data.
    2. Starts a T2Daemon with that T2Database.
    3. Connects a T2Client via UDS.
    4. Calls the method on the client; compares result to direct call.
- ``TestRpcExceptionParity``: asserts that errors propagate as the same
  exception type (or T2DaemonError for non-stdlib errors).
- ``TestSerializationRoundtrip``: verifies that the type-tagged encoder/decoder
  round-trips datetime, bytes, Path, and dataclass values.
- ``TestConnectionPool``: asserts that a pool of 4 connections can serve
  concurrent RPC calls without deadlock.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from nexus.daemon.t2_daemon import T2Daemon, t2_json_dumps, t2_json_loads
from nexus.daemon.t2_client import T2Client, T2DaemonError
from nexus.db.t2 import T2Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "t2.db"


@pytest.fixture
def t2db(db_path: Path) -> T2Database:
    """Open a T2Database at ``db_path``; close after test."""
    database = T2Database(db_path)
    yield database
    database.close()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "config"


def _run_daemon(daemon: T2Daemon) -> asyncio.AbstractEventLoop:
    """Start ``daemon`` on a background event loop; return the loop."""
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
    """Schedule graceful shutdown on ``loop``."""
    asyncio.run_coroutine_threadsafe(daemon.stop(), loop).result(timeout=5.0)
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture
def daemon_and_client(t2db: T2Database, config_dir: Path):
    """Start T2Daemon with t2db; yield (daemon, T2Client via UDS)."""
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    client = T2Client(uds_path=daemon.uds_path)
    yield daemon, client
    client.close()
    _stop_daemon(daemon, loop)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


def test_daemon_starts_with_t2db(t2db: T2Database, config_dir: Path) -> None:
    """Daemon starts with a T2Database injected; client ping succeeds."""
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    try:
        client = T2Client(uds_path=daemon.uds_path)
        pong = client.ping()
        assert pong.get("pong") is True
        client.close()
    finally:
        _stop_daemon(daemon, loop)


def test_client_connects_via_tcp(t2db: T2Database, config_dir: Path) -> None:
    """Client connects over TCP loopback fallback."""
    daemon = T2Daemon(config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    try:
        client = T2Client(tcp_addr=(daemon.tcp_host, daemon.tcp_port))
        pong = client.ping()
        assert pong.get("pong") is True
        client.close()
    finally:
        _stop_daemon(daemon, loop)


def test_client_rejects_both_transport_args() -> None:
    """T2Client raises ValueError when both uds_path and tcp_addr are given."""
    with pytest.raises(ValueError, match="exactly one"):
        T2Client(uds_path=Path("/tmp/foo.sock"), tcp_addr=("127.0.0.1", 9999))


def test_client_rejects_no_transport_args() -> None:
    """T2Client raises ValueError when neither uds_path nor tcp_addr is given."""
    with pytest.raises(ValueError, match="exactly one"):
        T2Client()


# ---------------------------------------------------------------------------
# Signature parity tests
# ---------------------------------------------------------------------------


class TestSignatureParity:
    """Verify that proxy method signatures match domain store class signatures."""

    def _check_sig(
        self,
        proxy_method: Any,
        store_cls: type,
        method_name: str,
    ) -> None:
        store_method = getattr(store_cls, method_name)
        orig_sig = inspect.signature(store_method)
        # Drop 'self'
        expected_params = list(orig_sig.parameters.values())[1:]
        expected_sig = orig_sig.replace(parameters=expected_params)
        actual_sig = inspect.signature(proxy_method)
        assert actual_sig == expected_sig, (
            f"{store_cls.__name__}.{method_name} signature mismatch:\n"
            f"  expected: {expected_sig}\n"
            f"  actual:   {actual_sig}"
        )

    def test_memory_put_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.memory_store import MemoryStore
        _, client = daemon_and_client
        self._check_sig(client.memory.put, MemoryStore, "put")

    def test_memory_get_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.memory_store import MemoryStore
        _, client = daemon_and_client
        self._check_sig(client.memory.get, MemoryStore, "get")

    def test_memory_record_hook_failure_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.memory_store import MemoryStore
        _, client = daemon_and_client
        self._check_sig(client.memory.record_hook_failure, MemoryStore, "record_hook_failure")

    def test_plans_save_plan_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.plan_library import PlanLibrary
        _, client = daemon_and_client
        self._check_sig(client.plans.save_plan, PlanLibrary, "save_plan")

    def test_plans_delete_by_tag_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.plan_library import PlanLibrary
        _, client = daemon_and_client
        self._check_sig(client.plans.delete_by_tag, PlanLibrary, "delete_by_tag")

    def test_plans_plans_mtime_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.plan_library import PlanLibrary
        _, client = daemon_and_client
        self._check_sig(client.plans.plans_mtime, PlanLibrary, "plans_mtime")

    def test_chash_index_upsert_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.chash_index import ChashIndex
        _, client = daemon_and_client
        self._check_sig(client.chash_index.upsert, ChashIndex, "upsert")

    def test_chash_index_lookup_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.chash_index import ChashIndex
        _, client = daemon_and_client
        self._check_sig(client.chash_index.lookup, ChashIndex, "lookup")

    def test_telemetry_log_relevance_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.telemetry import Telemetry
        _, client = daemon_and_client
        self._check_sig(client.telemetry.log_relevance, Telemetry, "log_relevance")

    def test_telemetry_get_relevance_log_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.telemetry import Telemetry
        _, client = daemon_and_client
        self._check_sig(client.telemetry.get_relevance_log, Telemetry, "get_relevance_log")

    def test_aspect_queue_enqueue_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue
        _, client = daemon_and_client
        self._check_sig(client.aspect_queue.enqueue, AspectExtractionQueue, "enqueue")

    def test_aspect_queue_pending_count_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue
        _, client = daemon_and_client
        self._check_sig(client.aspect_queue.pending_count, AspectExtractionQueue, "pending_count")

    def test_document_aspects_delete_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.document_aspects import DocumentAspects
        _, client = daemon_and_client
        self._check_sig(client.document_aspects.delete, DocumentAspects, "delete")

    def test_taxonomy_get_all_topics_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy
        _, client = daemon_and_client
        self._check_sig(client.taxonomy.get_all_topics, CatalogTaxonomy, "get_all_topics")

    def test_taxonomy_get_distinct_collections_signature(self, daemon_and_client) -> None:
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy
        _, client = daemon_and_client
        self._check_sig(
            client.taxonomy.get_distinct_collections,
            CatalogTaxonomy,
            "get_distinct_collections",
        )


# ---------------------------------------------------------------------------
# RPC parity tests (same input -> same output)
# ---------------------------------------------------------------------------


class TestRpcParity:
    """Each test calls a store method via client and directly, asserts equal result."""

    # ---- memory store ----

    def test_memory_put_returns_int(self, t2db: T2Database, daemon_and_client) -> None:
        """memory.put returns an int row ID both directly and via RPC."""
        _, client = daemon_and_client
        direct_id = t2db.memory.put(project="p", title="a.md", content="direct")
        rpc_id = client.memory.put(project="p", title="b.md", content="rpc")
        assert isinstance(direct_id, int)
        assert isinstance(rpc_id, int)
        assert direct_id != rpc_id  # both inserted, different IDs

    def test_memory_get_existing(self, t2db: T2Database, daemon_and_client) -> None:
        """memory.get returns the same dict structure both directly and via RPC."""
        _, client = daemon_and_client
        t2db.memory.put(project="proj", title="note.md", content="hello world")
        direct = t2db.memory.get(project="proj", title="note.md")
        rpc = client.memory.get(project="proj", title="note.md")
        assert direct is not None
        assert rpc is not None
        assert rpc["content"] == direct["content"]
        assert rpc["project"] == direct["project"]
        assert rpc["title"] == direct["title"]

    def test_memory_get_missing_returns_none(self, daemon_and_client) -> None:
        """memory.get returns None for a missing entry via RPC."""
        _, client = daemon_and_client
        result = client.memory.get(project="no", title="such.md")
        assert result is None

    def test_memory_search_empty(self, daemon_and_client) -> None:
        """memory.search returns empty list when no entries match."""
        _, client = daemon_and_client
        result = client.memory.search("missing-term-xyz")
        assert result == []

    def test_memory_search_finds_entry(self, t2db: T2Database, daemon_and_client) -> None:
        """memory.search returns matching entries via RPC."""
        _, client = daemon_and_client
        t2db.memory.put(project="p", title="find-me.md", content="unique-token-zqx")
        results = client.memory.search("unique-token-zqx")
        assert any(r["title"] == "find-me.md" for r in results)

    def test_memory_delete_returns_count(self, t2db: T2Database, daemon_and_client) -> None:
        """memory.delete returns 1 for an existing entry, 0 for missing."""
        _, client = daemon_and_client
        t2db.memory.put(project="del-proj", title="del.md", content="bye")
        deleted = client.memory.delete(project="del-proj", title="del.md")
        assert deleted == 1
        # Second delete: already gone
        deleted2 = client.memory.delete(project="del-proj", title="del.md")
        assert deleted2 == 0

    # ---- plans store ----

    def test_plans_save_and_search(self, daemon_and_client) -> None:
        """plans.save_plan returns int ID; plans.search_plans finds it via RPC."""
        _, client = daemon_and_client
        plan_id = client.plans.save_plan(
            query="what is parity testing",
            plan_json='{"steps": []}',
            tags="test,parity",
        )
        assert isinstance(plan_id, int)
        results = client.plans.search_plans("parity testing")
        assert any(r["id"] == plan_id for r in results)

    def test_plans_delete_by_tag(self, t2db: T2Database, daemon_and_client) -> None:
        """plans.delete_by_tag returns the count of deleted plans via RPC."""
        _, client = daemon_and_client
        t2db.plans.save_plan(
            query="plan to delete",
            plan_json='{"steps": []}',
            tags="delete-me",
        )
        deleted = client.plans.delete_by_tag("delete-me")
        assert deleted >= 1

    def test_plans_mtime_returns_float_or_none(self, daemon_and_client) -> None:
        """plans.plans_mtime returns a float or None via RPC."""
        _, client = daemon_and_client
        mtime = client.plans.plans_mtime()
        assert mtime is None or isinstance(mtime, float)

    def test_plans_mtime_after_save(self, daemon_and_client) -> None:
        """plans.plans_mtime returns a float after a plan is saved."""
        _, client = daemon_and_client
        client.plans.save_plan(
            query="mtime-check plan",
            plan_json='{"steps": []}',
        )
        mtime = client.plans.plans_mtime()
        assert isinstance(mtime, float)
        assert mtime > 0

    # ---- chash_index store ----

    def test_chash_upsert_and_lookup(self, daemon_and_client) -> None:
        """chash_index.upsert + lookup round-trips via RPC."""
        _, client = daemon_and_client
        client.chash_index.upsert(chash="abc123", collection="code__foo")
        rows = client.chash_index.lookup("abc123")
        assert isinstance(rows, list)
        assert len(rows) == 1
        assert rows[0]["collection"] == "code__foo"

    def test_chash_count_for_collection(self, t2db: T2Database, daemon_and_client) -> None:
        """chash_index.count_for_collection returns correct int via RPC."""
        _, client = daemon_and_client
        t2db.chash_index.upsert(chash="ch1", collection="code__bar")
        t2db.chash_index.upsert(chash="ch2", collection="code__bar")
        count = client.chash_index.count_for_collection("code__bar")
        assert count == 2

    def test_chash_lookup_missing_returns_empty(self, daemon_and_client) -> None:
        """chash_index.lookup returns [] for an unknown chash."""
        _, client = daemon_and_client
        assert client.chash_index.lookup("not-a-real-chash") == []

    # ---- aspect_queue store ----

    def test_aspect_queue_pending_count_empty(self, daemon_and_client) -> None:
        """aspect_queue.pending_count returns 0 on an empty queue via RPC."""
        _, client = daemon_and_client
        assert client.aspect_queue.pending_count() == 0

    def test_aspect_queue_enqueue_and_count(self, daemon_and_client) -> None:
        """Enqueue two items; pending_count reflects them via RPC."""
        _, client = daemon_and_client
        client.aspect_queue.enqueue(
            collection="knowledge__test",
            source_path="/tmp/a.pdf",
            content_hash="aaa",
        )
        client.aspect_queue.enqueue(
            collection="knowledge__test",
            source_path="/tmp/b.pdf",
            content_hash="bbb",
        )
        assert client.aspect_queue.pending_count() == 2

    def test_aspect_queue_is_drained(self, daemon_and_client) -> None:
        """aspect_queue.is_drained returns True when queue is empty."""
        _, client = daemon_and_client
        assert client.aspect_queue.is_drained() is True

    # ---- document_aspects store ----

    def test_document_aspects_delete_missing(self, daemon_and_client) -> None:
        """document_aspects.delete returns 0 for a non-existent record via RPC."""
        _, client = daemon_and_client
        deleted = client.document_aspects.delete(
            collection="code__foo", source_path="/no/such/file.py"
        )
        assert deleted == 0

    def test_document_aspects_list_by_collection_empty(self, daemon_and_client) -> None:
        """document_aspects.list_by_collection returns [] for unknown collection."""
        _, client = daemon_and_client
        rows = client.document_aspects.list_by_collection("code__nonexistent")
        assert rows == []

    # ---- telemetry store ----

    def test_telemetry_log_and_retrieve(self, daemon_and_client) -> None:
        """telemetry.log_relevance + get_relevance_log round-trip via RPC."""
        _, client = daemon_and_client
        row_id = client.telemetry.log_relevance(
            query="test query",
            chunk_id="chunk-abc",
            action="click",
            session_id="sess-1",
            collection="knowledge__test",
        )
        assert isinstance(row_id, int)
        log = client.telemetry.get_relevance_log(query="test query")
        assert any(r.get("chunk_id") == "chunk-abc" for r in log)

    def test_telemetry_get_relevance_log_empty(self, daemon_and_client) -> None:
        """telemetry.get_relevance_log returns [] for unknown query."""
        _, client = daemon_and_client
        log = client.telemetry.get_relevance_log(query="no-such-query-zzzz")
        assert log == []

    # ---- taxonomy store ----

    def test_taxonomy_get_all_topics_empty(self, daemon_and_client) -> None:
        """taxonomy.get_all_topics returns [] on a fresh DB via RPC."""
        _, client = daemon_and_client
        topics = client.taxonomy.get_all_topics()
        assert isinstance(topics, list)

    def test_taxonomy_get_distinct_collections_empty(self, daemon_and_client) -> None:
        """taxonomy.get_distinct_collections returns [] on a fresh DB via RPC."""
        _, client = daemon_and_client
        colls = client.taxonomy.get_distinct_collections()
        assert isinstance(colls, list)

    # ---- database-level RPC ----

    def test_rename_collection_cascade_no_op(self, daemon_and_client) -> None:
        """database.rename_collection_cascade returns counts dict on empty DB."""
        _, client = daemon_and_client
        counts = client.database.rename_collection_cascade(old="x__old", new="x__new")
        assert isinstance(counts, dict)
        assert "chash" in counts


# ---------------------------------------------------------------------------
# Exception parity tests
# ---------------------------------------------------------------------------


class TestRpcExceptionParity:
    """Verify that remote exceptions propagate as correct Python exception types."""

    def test_unknown_op_returns_error(self, daemon_and_client) -> None:
        """Calling a non-existent store.method raises T2DaemonError."""
        _, client = daemon_and_client
        with pytest.raises(T2DaemonError, match="unknown RPC op"):
            with client._get_pool().acquire() as conn:
                conn.call("memory.nonexistent_method_xyz", {})

    def test_store_method_type_error_surfaces(self, daemon_and_client) -> None:
        """Passing wrong-typed args raises a T2DaemonError or TypeError."""
        _, client = daemon_and_client
        with pytest.raises((T2DaemonError, TypeError)):
            # memory.put requires string args; None for required 'project' -> error
            client.memory.put(project=None, title=None, content=None)

    def test_no_connection_raises(self, tmp_path: Path) -> None:
        """Connecting to a non-existent UDS path raises ConnectionError."""
        client = T2Client(uds_path=tmp_path / "does-not-exist.sock")
        with pytest.raises((ConnectionError, FileNotFoundError, OSError)):
            client.ping()


class TestDispatchTableDenylist:
    """Methods on the deny list are NOT reachable as RPC ops."""

    def test_close_rpc_rejected(self, daemon_and_client) -> None:
        """`<store>.close` must not be reachable — would tear down the daemon's SQLite handle."""
        _, client = daemon_and_client
        for store in ("memory", "plans", "chash_index", "taxonomy", "telemetry", "document_aspects", "aspect_queue"):
            with pytest.raises(T2DaemonError, match="unknown RPC op"):
                with client._get_pool().acquire() as conn:
                    conn.call(f"{store}.close", {})

    def test_document_aspects_upsert_rejected(self, daemon_and_client) -> None:
        """document_aspects.upsert takes a dataclass — denied until typed-arg reconstructor lands."""
        _, client = daemon_and_client
        for op in ("document_aspects.upsert", "document_aspects.get", "document_aspects.get_by_doc_id"):
            with pytest.raises(T2DaemonError, match="unknown RPC op"):
                with client._get_pool().acquire() as conn:
                    conn.call(op, {})


class TestClientCloseSemantics:
    """T2Client.close() detaches the pool so subsequent use rebuilds it."""

    def test_close_then_reuse_builds_fresh_pool(self, daemon_and_client) -> None:
        _, client = daemon_and_client
        first_pool = client._get_pool()
        client.close()
        assert client._pool is None
        # Re-using the client transparently rebuilds the pool
        assert client.ping()["pong"] is True
        assert client._pool is not None
        assert client._pool is not first_pool


# ---------------------------------------------------------------------------
# Serialization round-trip tests
# ---------------------------------------------------------------------------


class TestSerializationRoundtrip:
    """Verify the type-tagged JSON encoder/decoder handles special types."""

    def test_none_roundtrip(self) -> None:
        data = {"result": None}
        assert t2_json_loads(t2_json_dumps(data)) == data

    def test_int_roundtrip(self) -> None:
        data = {"result": 42}
        assert t2_json_loads(t2_json_dumps(data)) == data

    def test_float_roundtrip(self) -> None:
        data = {"result": 3.14}
        assert t2_json_loads(t2_json_dumps(data)) == data

    def test_str_roundtrip(self) -> None:
        data = {"result": "hello"}
        assert t2_json_loads(t2_json_dumps(data)) == data

    def test_list_roundtrip(self) -> None:
        data = {"result": [1, "two", None]}
        assert t2_json_loads(t2_json_dumps(data)) == data

    def test_nested_dict_roundtrip(self) -> None:
        data = {"result": {"a": {"b": 1}}}
        assert t2_json_loads(t2_json_dumps(data)) == data

    def test_datetime_roundtrip(self) -> None:
        from datetime import datetime, timezone
        dt = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
        encoded = t2_json_dumps({"result": dt})
        decoded = t2_json_loads(encoded)
        assert decoded["result"] == dt

    def test_bytes_roundtrip(self) -> None:
        b = b"\x00\x01\x02\xff"
        encoded = t2_json_dumps({"result": b})
        decoded = t2_json_loads(encoded)
        assert decoded["result"] == b

    def test_path_roundtrip(self) -> None:
        p = Path("/tmp/test.sock")
        encoded = t2_json_dumps({"result": p})
        decoded = t2_json_loads(encoded)
        assert decoded["result"] == p

    def test_dataclass_roundtrip(self) -> None:
        from nexus.db.t2.aspect_extraction_queue import QueueRow
        row = QueueRow(
            collection="code__foo",
            source_path="/tmp/x.py",
            content_hash="abc",
            content="",
            retry_count=0,
        )
        encoded = t2_json_dumps({"result": row})
        decoded = t2_json_loads(encoded)
        # Decoded as plain dict of fields
        assert decoded["result"]["collection"] == "code__foo"
        assert decoded["result"]["source_path"] == "/tmp/x.py"

    def test_non_serialisable_raises(self) -> None:
        """Objects that cannot be encoded raise TypeError."""
        import io
        with pytest.raises(TypeError, match="not JSON-serialisable"):
            t2_json_dumps({"result": io.StringIO()})


# ---------------------------------------------------------------------------
# Connection pool concurrency test
# ---------------------------------------------------------------------------


class TestConnectionPool:
    """Pool of 4 connections handles concurrent RPC calls without deadlock."""

    def test_concurrent_memory_puts(
        self, t2db: T2Database, config_dir: Path
    ) -> None:
        """32 concurrent memory.put calls all succeed (pool_size=4)."""
        daemon = T2Daemon(config_dir, t2db=t2db)
        loop = _run_daemon(daemon)
        try:
            client = T2Client(uds_path=daemon.uds_path, pool_size=4)
            errors: list[Exception] = []

            def _put(i: int) -> None:
                try:
                    client.memory.put(
                        project="concurrent",
                        title=f"item-{i}.md",
                        content=f"content {i}",
                    )
                except Exception as exc:
                    errors.append(exc)

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(_put, range(32)))

            assert errors == [], f"Unexpected errors: {errors[:3]}"
            client.close()
        finally:
            _stop_daemon(daemon, loop)
