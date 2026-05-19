# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 6shq.1 (nexus-lj2l): Catalog read-path parity across direct
and daemon backends.

Substrate test for the higher-level ``nexus.catalog.Catalog`` wrapper.
Phase 4 (nexus-uar6) flipped ``nx catalog`` CLI sites to the daemon-aware
T3 client but deferred the Catalog wrapper itself; it still opened a
local ``CatalogDB(.catalog.db)`` independent of the daemon-owned
``memory.db``. lj2l ships the duck-typed ``ExecuteProxy`` over
``T2Client.catalog`` so ``Catalog`` can swap its backing handle without
forking the call sites.

Scope (read-path): ``find``, ``resolve``, ``get_manifest``,
``list_collections``, ``links_from``, ``links_to``,
``resolve_span_text``. Each seeds the same data through ``Catalog`` once
in direct mode and once via ``ExecuteProxy``, then compares the return
values. Write-path parity for the ``rebuild`` / ``transaction`` /
``bulk_load_documents`` surfaces is deferred to 6shq.2-6shq.6 with the
CLI call-site flips.

Cursor-vs-list compatibility (load-bearing): ``CatalogDB.execute`` returns
``sqlite3.Cursor``; ``CatalogStore.execute`` returns ``list[tuple]``.
``ExecuteProxy.execute`` wraps the list response in a ``_ResultCursor``
so existing ``.fetchone()`` / ``.fetchall()`` / iteration sites in
``catalog.py`` / ``catalog_links.py`` / ``catalog_writes.py`` /
``catalog_sync.py`` / ``projector.py`` work unchanged.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from nexus.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.daemon.t2_client import T2Client
from nexus.daemon.t2_daemon import T2Daemon
from nexus.db.t2 import T2Database


# ---------------------------------------------------------------------------
# Fixtures
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
        # RDR-112 6shq.2 (nexus-3gdg): ``reset_cache`` clears the
        # process-singleton T2Client if any test in this suite
        # triggered ``open_catalog``. The parity tests construct the
        # Catalog directly via ``Catalog(..., db=ExecuteProxy(client))``
        # without going through ``open_catalog``, so ``_t2_client``
        # is normally ``None`` here and ``reset_cache`` is a no-op.
        # Kept for defence-in-depth against future test additions
        # that exercise the open_catalog path.
        from nexus.catalog import reset_cache
        reset_cache()
        client.close()
        _stop_daemon(daemon, loop)


@pytest.fixture
def disable_consistency_rebuild(monkeypatch):
    """Short-circuit ``_SyncOps._ensure_consistent``.

    The parity tests seed each backend directly through ``Catalog``; the
    rebuild path uses ``cat._db.rebuild`` / ``cat._db.transaction``,
    which are write-path methods deferred to 6shq.2-6shq.6. Without
    this fixture, fresh-construction would trip the consistency rebuild
    on the daemon-mode Catalog and fail with AttributeError on the
    proxy. Read-path parity does not depend on the rebuild; both
    backends start from empty SQLite + empty JSONL and accumulate
    state through the same ``register`` / ``link`` calls.
    """
    from nexus.catalog import catalog_sync
    monkeypatch.setattr(
        catalog_sync._SyncOps,
        "_ensure_consistent",
        lambda self: None,
    )


@pytest.fixture
def catalog_pair(tmp_path: Path, daemon_client: T2Client, disable_consistency_rebuild):
    """Return (cat_direct, cat_daemon) opened on fresh, isolated catalog dirs.

    Both Catalogs start empty; tests seed them with identical operations
    and then exercise the seven read methods on each.
    """
    from nexus.catalog.catalog_proxy import ExecuteProxy

    direct_dir = tmp_path / "direct"
    daemon_dir = tmp_path / "daemon"
    Catalog.init(direct_dir)
    Catalog.init(daemon_dir)

    cat_direct = Catalog(direct_dir, direct_dir / ".catalog.db")
    proxy = ExecuteProxy(daemon_client)
    cat_daemon = Catalog(daemon_dir, daemon_dir / ".catalog.db", db=proxy)
    return cat_direct, cat_daemon


# ---------------------------------------------------------------------------
# Read-path parity tests
# ---------------------------------------------------------------------------


class TestFindParity:
    def test_find_by_title_matches(self, catalog_pair):
        cat_direct, cat_daemon = catalog_pair
        # Seed both backends with the same operations.
        for cat in (cat_direct, cat_daemon):
            owner = cat.register_owner(
                name="parity_test",
                owner_type="curator",
                description="parity seed",
            )
            cat.register(
                owner=owner,
                title="The Annotated Turing",
                content_type="text",
                physical_collection="docs__parity",
                chunk_count=1,
                head_hash="deadbeef" * 8,
            )
        rs_direct = cat_direct.find("Annotated Turing")
        rs_daemon = cat_daemon.find("Annotated Turing")
        # Titles + content_type + physical_collection must match; the
        # tumbler identity can differ between independent registrations
        # because each side allocates its own owner number. Compare
        # the structural fields.
        assert [r.title for r in rs_direct] == [r.title for r in rs_daemon]
        assert [r.content_type for r in rs_direct] == [
            r.content_type for r in rs_daemon
        ]
        assert [r.physical_collection for r in rs_direct] == [
            r.physical_collection for r in rs_daemon
        ]

    def test_find_filtered_by_content_type(self, catalog_pair):
        cat_direct, cat_daemon = catalog_pair
        for cat in (cat_direct, cat_daemon):
            owner = cat.register_owner(name="ct", owner_type="curator")
            cat.register(
                owner=owner,
                title="Doc-A",
                content_type="text",
                physical_collection="docs__a",
                chunk_count=1,
                head_hash="a" * 64,
            )
            cat.register(
                owner=owner,
                title="Code-A",
                content_type="code",
                physical_collection="code__a",
                chunk_count=1,
                head_hash="b" * 64,
            )
        rs_direct = cat_direct.find("A", content_type="code")
        rs_daemon = cat_daemon.find("A", content_type="code")
        assert {r.title for r in rs_direct} == {r.title for r in rs_daemon}
        assert all(r.content_type == "code" for r in rs_direct)
        assert all(r.content_type == "code" for r in rs_daemon)

    def test_find_empty_when_no_match(self, catalog_pair):
        cat_direct, cat_daemon = catalog_pair
        assert cat_direct.find("missing") == []
        assert cat_daemon.find("missing") == []


class TestResolveParity:
    def test_resolve_returns_entry(self, catalog_pair):
        cat_direct, cat_daemon = catalog_pair
        results = []
        for cat in (cat_direct, cat_daemon):
            owner = cat.register_owner(name="r", owner_type="curator")
            tumbler = cat.register(
                owner=owner,
                title="Resolved",
                content_type="text",
                physical_collection="docs__r",
                chunk_count=1,
                head_hash="c" * 64,
            )
            entry = cat.resolve(tumbler)
            results.append(entry)
        direct_entry, daemon_entry = results
        assert direct_entry is not None
        assert daemon_entry is not None
        assert direct_entry.title == daemon_entry.title
        assert direct_entry.content_type == daemon_entry.content_type
        assert direct_entry.head_hash == daemon_entry.head_hash

    def test_resolve_missing_tumbler_returns_none(self, catalog_pair):
        cat_direct, cat_daemon = catalog_pair
        missing = Tumbler.parse("1.999.999.0")
        assert cat_direct.resolve(missing) is None
        assert cat_daemon.resolve(missing) is None


class TestListCollectionsParity:
    def test_list_after_register(self, catalog_pair):
        cat_direct, cat_daemon = catalog_pair
        names_direct = []
        names_daemon = []
        for cat, sink in ((cat_direct, names_direct), (cat_daemon, names_daemon)):
            # ``list_collections`` reads from the ``collections`` table,
            # which is populated by ``register_collection`` (or by the
            # init-time backfill from pre-existing documents). A fresh
            # catalog only sees collections that have been explicitly
            # registered.
            cat.register_collection(
                "docs__lc_one",
                content_type="text",
                owner_id="1.1",
            )
            cat.register_collection(
                "docs__lc_two",
                content_type="text",
                owner_id="1.1",
            )
            for row in cat.list_collections():
                sink.append(row["name"])
        assert sorted(names_direct) == sorted(names_daemon)
        assert "docs__lc_one" in names_direct
        assert "docs__lc_two" in names_direct


class TestGetManifestParity:
    def test_manifest_empty_when_no_chunks(self, catalog_pair):
        cat_direct, cat_daemon = catalog_pair
        # get_manifest reads from document_chunks; an entry registered
        # without chunks (no chash inserts) returns []. Both sides
        # should agree.
        manifests = []
        for cat in (cat_direct, cat_daemon):
            owner = cat.register_owner(name="m", owner_type="curator")
            tumbler = cat.register(
                owner=owner,
                title="No-chunks",
                content_type="text",
                physical_collection="docs__m",
                chunk_count=0,
                head_hash="f" * 64,
            )
            manifests.append(cat.get_manifest(str(tumbler)))
        assert manifests[0] == manifests[1]
        assert manifests[0] == []


class TestLinksFromToParity:
    def test_links_from_and_to(self, catalog_pair):
        cat_direct, cat_daemon = catalog_pair
        from_lists = []
        to_lists = []
        for cat in (cat_direct, cat_daemon):
            owner = cat.register_owner(name="lk", owner_type="curator")
            a = cat.register(
                owner=owner,
                title="A",
                content_type="text",
                physical_collection="docs__lk",
                chunk_count=1,
                head_hash="1" * 64,
            )
            b = cat.register(
                owner=owner,
                title="B",
                content_type="text",
                physical_collection="docs__lk",
                chunk_count=1,
                head_hash="2" * 64,
            )
            cat.link(a, b, "cites", created_by="parity_test")
            from_lists.append(
                [(lk.from_tumbler, lk.to_tumbler, lk.link_type) for lk in cat.links_from(a)]
            )
            to_lists.append(
                [(lk.from_tumbler, lk.to_tumbler, lk.link_type) for lk in cat.links_to(b)]
            )
        assert from_lists[0] == from_lists[1]
        assert to_lists[0] == to_lists[1]
        assert len(from_lists[0]) == 1
        assert from_lists[0][0][2] == "cites"


class TestResolveSpanTextParity:
    def test_no_chunk_returns_none(self, catalog_pair):
        cat_direct, cat_daemon = catalog_pair
        # With no chunks registered, resolve_span_text returns None on
        # both sides. Span-resolution write paths (chunk registration)
        # are deferred to 6shq.2-6shq.6 so this read-path parity check
        # asserts the empty-state contract.
        spans = []
        for cat in (cat_direct, cat_daemon):
            owner = cat.register_owner(name="sp", owner_type="curator")
            tumbler = cat.register(
                owner=owner,
                title="Span-doc",
                content_type="text",
                physical_collection="docs__sp",
                chunk_count=0,
                head_hash="3" * 64,
            )
            spans.append(cat.resolve_span_text(tumbler, ""))
        assert spans[0] == spans[1]
        assert spans[0] is None


# ---------------------------------------------------------------------------
# ExecuteProxy unit contract
# ---------------------------------------------------------------------------


class TestExecuteProxyContract:
    """Direct tests of the proxy surface; independent of Catalog."""

    def test_execute_returns_cursor_like_result(self, daemon_client: T2Client) -> None:
        from nexus.catalog.catalog_proxy import ExecuteProxy
        proxy = ExecuteProxy(daemon_client)
        # SELECT against the bare schema; documents table is empty.
        cursor = proxy.execute("SELECT COUNT(*) FROM documents")
        assert cursor.fetchone() == (0,)
        # fetchall on a fresh SELECT returns the full list.
        cursor2 = proxy.execute("SELECT * FROM documents")
        assert cursor2.fetchall() == []

    def test_execute_with_params(self, daemon_client: T2Client) -> None:
        from nexus.catalog.catalog_proxy import ExecuteProxy
        proxy = ExecuteProxy(daemon_client)
        # INSERT a single owner and SELECT it back.
        proxy.execute(
            "INSERT INTO owners (tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("1.42", "exec-proxy-test", "curator", "", "", ""),
        )
        proxy.commit()
        cursor = proxy.execute(
            "SELECT name FROM owners WHERE tumbler_prefix = ?", ("1.42",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "exec-proxy-test"

    def test_fetchone_on_empty_returns_none(self, daemon_client: T2Client) -> None:
        from nexus.catalog.catalog_proxy import ExecuteProxy
        proxy = ExecuteProxy(daemon_client)
        cursor = proxy.execute("SELECT * FROM owners WHERE tumbler_prefix = ?", ("nope",))
        assert cursor.fetchone() is None

    def test_iteration_yields_rows(self, daemon_client: T2Client) -> None:
        from nexus.catalog.catalog_proxy import ExecuteProxy
        proxy = ExecuteProxy(daemon_client)
        for i in range(3):
            proxy.execute(
                "INSERT INTO owners (tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"1.{100 + i}", f"iter-{i}", "curator", "", "", ""),
            )
        proxy.commit()
        cursor = proxy.execute(
            "SELECT name FROM owners WHERE tumbler_prefix LIKE '1.10%' ORDER BY tumbler_prefix"
        )
        names = [row[0] for row in cursor]
        assert names == ["iter-0", "iter-1", "iter-2"]

    def test_transaction_not_implemented(self, daemon_client: T2Client) -> None:
        from nexus.catalog.catalog_proxy import ExecuteProxy
        proxy = ExecuteProxy(daemon_client)
        with pytest.raises(NotImplementedError, match="transaction"):
            with proxy.transaction():
                pass

    def test_rebuild_not_implemented(self, daemon_client: T2Client) -> None:
        """``rebuild`` must fail loud with recovery hint (review I-2).

        The bare ``AttributeError`` pattern that an absent method would
        surface is inconsistent with the ``transaction()`` /
        ``bulk_load_documents()`` stubs. lj2l ships explicit stubs
        across all three so the failure surface is predictable.
        """
        from nexus.catalog.catalog_proxy import ExecuteProxy
        proxy = ExecuteProxy(daemon_client)
        with pytest.raises(NotImplementedError, match="rebuild"):
            proxy.rebuild([], [], [])

    def test_bulk_load_documents_not_implemented(self, daemon_client: T2Client) -> None:
        from nexus.catalog.catalog_proxy import ExecuteProxy
        proxy = ExecuteProxy(daemon_client)
        with pytest.raises(NotImplementedError, match="bulk_load_documents"):
            proxy.bulk_load_documents()


class TestCollectionHealthDaemonMode:
    """Regression test for review I-1.

    ``collection_health._default_catalog_stats_fn`` reached
    ``cat._db._conn`` directly; under daemon mode that attribute does
    not exist on ``ExecuteProxy`` so the bare ``except Exception``
    silently returned zero stats for every collection. lj2l switched
    the call to the public ``cat._db.execute(...)`` surface so the
    daemon path returns actual rows.
    """

    def test_default_stats_works_under_daemon_proxy(
        self,
        catalog_pair,
        monkeypatch,
    ) -> None:
        from nexus.collection_health import _default_catalog_stats_fn
        cat_direct, cat_daemon = catalog_pair

        # Seed both backends with one document in a known collection.
        col_name = "docs__health_probe"
        for cat in (cat_direct, cat_daemon):
            owner = cat.register_owner(name="hp", owner_type="curator")
            cat.register(
                owner=owner,
                title="Health Probe",
                content_type="text",
                physical_collection=col_name,
                chunk_count=1,
                head_hash="9" * 64,
            )

        # Verify the daemon path returns the same shape as direct mode.
        # Use monkeypatch to swap ``_open_catalog`` so the function
        # under test reads the proxy-backed Catalog directly (rather
        # than going through ``open_cached`` which would key on the
        # process-default catalog_path).
        from nexus import collection_health
        monkeypatch.setattr(
            collection_health, "_open_catalog", lambda: cat_daemon,
        )
        result = _default_catalog_stats_fn(col_name)
        assert result["last_indexed"] is not None, (
            "daemon-mode catalog stats must return a real indexed_at "
            "instead of silently returning None (review I-1)"
        )
        assert result["orphan_count"] >= 1, (
            "daemon-mode orphan_count must reflect actual documents"
        )

        # Direct-mode parity check (regression: did the call-site swap
        # break direct mode?).
        monkeypatch.setattr(
            collection_health, "_open_catalog", lambda: cat_direct,
        )
        direct = _default_catalog_stats_fn(col_name)
        assert direct["last_indexed"] is not None
        assert direct["orphan_count"] == result["orphan_count"]
