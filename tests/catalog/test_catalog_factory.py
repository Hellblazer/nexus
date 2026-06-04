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
    make_catalog_reader,
    make_catalog_writer,
)
from nexus.catalog.tumbler import Tumbler


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


class TestWriterDaemonRouted:
    def test_routes_through_daemon_when_up(self, tmp_path: Path) -> None:
        cd = Path(tempfile.mkdtemp(prefix="nxwr-", dir="/tmp"))
        try:
            from nexus.daemon.t2_daemon import T2Daemon

            daemon = T2Daemon(config_dir=cd, db_path=tmp_path / "memory.db")
            ready, stop_evt = threading.Event(), threading.Event()
            th = threading.Thread(
                target=_run_daemon_in_thread, args=(daemon, ready, stop_evt), daemon=True
            )
            th.start()
            assert ready.wait(timeout=10)
            try:
                writer = make_catalog_writer(config_dir=cd)
                assert writer.routed is True
                owner = writer.register_owner(
                    "acme", "project", repo_hash="h1", repo_root="/tmp/acme"
                )
                assert isinstance(owner, Tumbler)
                writer.close()
            finally:
                stop_evt.set()
                th.join(timeout=10)
        finally:
            shutil.rmtree(cd, ignore_errors=True)
