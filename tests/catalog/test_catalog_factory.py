# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-146 P1.2 (bead nexus-5p2ci.21): read-only Catalog mode + the
make_catalog_reader / make_catalog_writer typed factories.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.factory import (
    CatalogWriter,
    make_catalog_admin,
    make_catalog_reader,
    make_catalog_writer,
)
from nexus.catalog.tumbler import Tumbler

# nexus-aqbrk: this file is the LOCAL factory's own test — read-only Catalog
# mode over .catalog.db, the daemon-vs-direct writer fallback, and the admin
# refusals. Service mode answers all three differently AND ON PURPOSE:
# make_catalog_reader always returns a _SharedServiceCatalogHandle (so
# "None when uninitialised" is unsatisfiable rather than wrong), the handle
# has no _read_only or _db, and make_catalog_admin raises
# CatalogAdminServiceModeError outright. The service half of this surface is
# owned by tests/test_catalog_factory_service_mode_cache.py, which exercises
# make_catalog_reader/make_catalog_writer under _is_catalog_service_mode and
# pins the shared-client contract.
pytestmark = pytest.mark.usefixtures("local_catalog_backend")


@pytest.fixture(autouse=True)
def _local_t2_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Also pin the T2 stores local, not just the catalog (nexus-aqbrk).

    TestWriterDaemonRouted starts a real T2 daemon to prove the writer routes
    through it. The daemon's ``_build_dispatch_table`` enumerates every store
    with ``dir()`` + a bare ``getattr``, which trips the fail-loud ``.conn``
    guard on any service-backed store and kills startup (tracked separately
    as nexus-uqiyo — the daemon is the SQLite single-writer, so what it
    should do in service mode is a design call, not a test concern).

    This file tests the LOCAL daemon-routed writer, so it needs the local T2
    it has always assumed.
    """
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")


def _seed_catalog(d: Path) -> tuple[str, str]:
    """Init the catalog, write an owner + a document; return (owner, doc)."""
    cat = Catalog.init(d)  # git init + documents.jsonl -> is_initialized True
    owner = cat.register_owner("acme", "project", repo_hash="h1", repo_root="/tmp/acme")
    doc = cat.register(owner, "Doc One", content_type="paper", file_path="/tmp/acme/a.md")
    cat._db.close()
    return str(owner), str(doc)


# ---------------------------------------------------------------------------
# read-only Catalog mode
# ---------------------------------------------------------------------------


class TestReadOnlyCatalog:
    def test_reads_committed_state(self, tmp_path: Path) -> None:
        d = tmp_path / "cat"
        d.mkdir()
        owner, doc = _seed_catalog(d)

        ro = Catalog(d, d / ".catalog.db", read_only=True)
        try:
            entry = ro.resolve(Tumbler.parse(doc))
            assert entry is not None
            assert entry.title == "Doc One"
        finally:
            ro._db.close()

    def test_construction_does_not_write_events(self, tmp_path: Path) -> None:
        d = tmp_path / "cat"
        d.mkdir()
        _seed_catalog(d)
        events = d / "events.jsonl"
        before = events.read_bytes() if events.exists() else b""

        ro = Catalog(d, d / ".catalog.db", read_only=True)
        ro._db.close()

        after = events.read_bytes() if events.exists() else b""
        assert after == before, "read-only construction must not append to events.jsonl"

    def test_write_raises_on_readonly(self, tmp_path: Path) -> None:
        d = tmp_path / "cat"
        d.mkdir()
        _seed_catalog(d)
        ro = Catalog(d, d / ".catalog.db", read_only=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                ro.register_owner("beta", "project", repo_hash="h2", repo_root="/tmp/beta")
        finally:
            ro._db.close()


# ---------------------------------------------------------------------------
# make_catalog_reader
# ---------------------------------------------------------------------------


class TestMakeCatalogReader:
    def test_none_when_uninitialised(self) -> None:
        # autouse _isolate_catalog points NEXUS_CATALOG_PATH at a fresh
        # tmp dir that is never initialised.
        assert make_catalog_reader() is None

    def test_returns_readonly_when_initialised(self) -> None:
        from nexus.config import catalog_path

        p = catalog_path()
        _seed_catalog(p)
        reader = make_catalog_reader()
        try:
            assert reader is not None
            assert reader._read_only is True
            assert any(reader.all_documents())
        finally:
            reader._db.close()


# ---------------------------------------------------------------------------
# make_catalog_writer — direct fallback (no daemon)
# ---------------------------------------------------------------------------


class TestWriterDirectFallback:
    def test_direct_fallback_writes_to_catalog_path(self) -> None:
        from nexus.config import catalog_path

        Catalog.init(catalog_path())._db.close()  # is_initialized -> True
        writer = make_catalog_writer()
        try:
            assert isinstance(writer, CatalogWriter)
            assert writer.routed is False  # no daemon running in this test
            owner = writer.register_owner(
                "acme", "project", repo_hash="h1", repo_root="/tmp/acme"
            )
            assert isinstance(owner, Tumbler)
            doc = writer.register(owner, "Doc One", content_type="paper", file_path="/x/a.md")
            assert isinstance(doc, Tumbler)
        finally:
            writer.close()

        # Committed to catalog_path(); a fresh reader sees it.
        reader = make_catalog_reader()
        try:
            assert reader is not None
            assert any(e.title == "Doc One" for e in reader.all_documents())
        finally:
            reader._db.close()

    def test_off_whitelist_attr_raises(self) -> None:
        writer = make_catalog_writer()
        try:
            with pytest.raises(AttributeError):
                _ = writer.resolve  # a read method
            with pytest.raises(AttributeError):
                _ = writer.not_a_real_op
        finally:
            writer.close()


# ---------------------------------------------------------------------------
# make_catalog_writer — daemon-routed
# ---------------------------------------------------------------------------


def _run_daemon_in_thread(daemon, ready: threading.Event, stop_evt: threading.Event):
    import asyncio

    async def _main() -> None:
        await daemon.start()
        ready.set()
        while not stop_evt.is_set():
            await asyncio.sleep(0.05)
        await daemon.stop()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_main())
    finally:
        loop.close()




# ---------------------------------------------------------------------------
# make_catalog_admin — deep-maintenance escape hatch + single-writer guard
# (RDR-146 P1.2 review remediation: substantive-critic SIG-1, code-review H-1)
# ---------------------------------------------------------------------------


class TestMakeCatalogAdmin:
    def test_none_when_uninitialised(self) -> None:
        assert make_catalog_admin() is None

    def test_returns_full_writable_catalog_when_no_daemon(self) -> None:
        from nexus.config import catalog_path

        _seed_catalog(catalog_path())
        cat = make_catalog_admin()
        try:
            assert cat is not None
            assert cat._read_only is False
            # Full write capability (low-level ops the daemon proxy can't serve).
            owner = cat.register_owner("beta", "project", repo_hash="h2", repo_root="/tmp/beta")
            assert isinstance(owner, Tumbler)
        finally:
            cat._db.close()

    # NO test_refuses_when_daemon_live: the RDR-146 P1.2 single-writer guard
    # refused a second writer while a T2 daemon held the .catalog.db write
    # handle. That daemon is retired (nexus-i711w Stage 2 sub-stage B), so no
    # competing writer can exist to refuse. The surviving refusal is
    # service-mode (CatalogAdminServiceModeError), covered by
    # tests/catalog/test_aoqnb_admin_service_mode_refusal.py.


class TestReaderColdCache:
    """RDR-146 P1.2 (test-validator GAP-1): make_catalog_reader materialises a
    missing .catalog.db once before the mode=ro open (mode=ro cannot open a
    nonexistent file), then returns a working read-only handle."""

    def test_cold_cache_materialises_then_reads(self) -> None:
        from nexus.config import catalog_path

        p = catalog_path()
        _seed_catalog(p)  # creates .git + JSONL + .catalog.db
        (p / ".catalog.db").unlink()  # drop the SQLite cache -> cold path
        assert not (p / ".catalog.db").exists()

        reader = make_catalog_reader()
        try:
            assert reader is not None
            assert (p / ".catalog.db").exists()  # materialised on the cold path
            assert reader._read_only is True
            assert any(reader.all_documents())  # rebuilt-from-JSONL reads work
        finally:
            reader._db.close()
