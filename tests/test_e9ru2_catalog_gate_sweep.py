# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-e9ru2: sweep of the remaining local ``Catalog.is_initialized``
gates — siblings of the nexus-f1itv store_hook fix.

Same defect class, found by inverse-grep after f1itv: pre-service-era
local presence checks that short-circuit (or side-effect) before the
factory's service-mode routing is consulted. On a virgin service-mode
box (no local catalog dir — EXPECTED, the Java service owns the catalog):

- ``pipeline_stages._catalog_pdf_hook`` silently skipped registration;
- ``doc_indexer._catalog_markdown_hook`` silently skipped registration;
- ``doc_indexer._register_or_lookup_doc_id`` AUTO-CREATED a local SQLite
  catalog (nexus-fq3b, pre-service era) that the service never reads;
- ``collection_audit._open_catalog_conn`` returned a raw connection to
  the FROZEN migration-source ``.catalog.db`` on migrated boxes, so
  audit legs reported against stale data (module precedent for the
  service-mode degrade: nexus-9613q.4).

Service mode is driven the production way — ``NX_STORAGE_BACKEND_CATALOG=
service`` (narrowest-wins over the suite-wide sqlite pin) — and only the
HTTP boundary class is faked, so ``_SharedServiceCatalogHandle`` /
``_ServiceCatalogWriter`` routing stays production code (same fidelity
argument as tests/test_f1itv_store_hook_fresh_box.py).

SQLite opt-out semantics stay pinned by the pre-existing suites
(tests/test_catalog_pdf_hook.py, tests/test_doc_indexer.py, ...) which
run under the suite-wide sqlite pin, plus the auto-init test here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import nexus.catalog.factory as factory

_COLLECTION = "docs__sweep__voyage-context-3__v1"


@pytest.fixture()
def service_mode_fresh_box(tmp_path, monkeypatch):
    """Service mode ON + a virgin box: catalog_path() -> nonexistent dir."""
    monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
    cat_path = tmp_path / "no-catalog-here"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_path))
    factory.reset_shared_service_catalog_client_for_tests()
    yield cat_path
    factory.reset_shared_service_catalog_client_for_tests()


class _FakeHttpCatalogClient:
    """Fake at the HTTP boundary; fresh-box reads, recording writes."""

    instances: list["_FakeHttpCatalogClient"] = []

    def __init__(self, *_a, **_kw) -> None:
        self.registered: list[dict] = []
        self.updated: list[dict] = []
        self.owners: list[tuple[str, str]] = []
        _FakeHttpCatalogClient.instances.append(self)

    # -- reads (via _SharedServiceCatalogHandle) --
    def curator_owner_tumbler_by_name(self, name):
        return None  # fresh box: no curator owner yet

    def by_file_path(self, owner, file_path):
        return None  # fresh box: nothing registered yet

    # -- writes (via _ServiceCatalogWriter, CATALOG_WRITE_OPS) --
    def register_owner(self, name, owner_type):
        self.owners.append((name, owner_type))
        return f"owner:{name}"

    def register(self, **fields):
        self.registered.append(fields)
        return "1.1.42"

    def update(self, tumbler, **fields):
        self.updated.append({"tumbler": tumbler, **fields})

    def close(self):
        pass


@pytest.fixture()
def fake_client(monkeypatch):
    _FakeHttpCatalogClient.instances = []
    monkeypatch.setattr(
        "nexus.catalog.http_catalog_client.HttpCatalogClient",
        _FakeHttpCatalogClient,
    )
    return _FakeHttpCatalogClient


def test_pdf_hook_fresh_box_service_mode_registers(
    service_mode_fresh_box, fake_client, tmp_path
):
    from nexus.pipeline_stages import _catalog_pdf_hook

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    _catalog_pdf_hook(
        pdf_path=pdf,
        collection_name=_COLLECTION,
        title="Sweep Paper",
        corpus="",
        chunk_count=7,
    )
    (client,) = fake_client.instances
    assert client.owners == [("standalone-pdfs", "curator")]
    (reg,) = client.registered
    assert reg["title"] == "Sweep Paper"
    assert reg["content_type"] == "paper"
    assert reg["physical_collection"] == _COLLECTION
    assert reg["chunk_count"] == 7, (
        "fresh service-mode box must register the PDF via the service "
        "catalog; a silent skip is the nexus-e9ru2 bug"
    )


def test_markdown_hook_fresh_box_service_mode_registers(
    service_mode_fresh_box, fake_client, tmp_path
):
    from nexus.doc_indexer import _catalog_markdown_hook

    md = tmp_path / "note.md"
    md.write_text("---\ntitle: Sweep Note\ncreated: 2026-07-21\n---\nbody\n")
    _catalog_markdown_hook(
        md, _COLLECTION, "prose", "", 3,
    )
    (client,) = fake_client.instances
    assert client.owners == [("standalone-docs", "curator")]
    (reg,) = client.registered
    assert reg["title"] == "Sweep Note"
    assert reg["content_type"] == "prose"
    assert reg["physical_collection"] == _COLLECTION
    assert reg["chunk_count"] == 3
    assert reg["year"] == 2026


def test_register_or_lookup_fresh_box_no_local_catalog_created(
    service_mode_fresh_box, fake_client, tmp_path
):
    """Service mode must neither skip NOR auto-init a local catalog: the
    nexus-fq3b auto-init is an SQLite-opt-out-mode concern only. Pre-fix,
    this call created ``.catalog.db`` on every fresh service-mode box —
    a new local SQLite substrate the service never reads, whose mere
    existence flips the other stale gates into 'passing'."""
    from nexus.doc_indexer import _register_or_lookup_doc_id

    doc = tmp_path / "pre.md"
    doc.write_text("preflight body\n")
    doc_id = _register_or_lookup_doc_id(
        doc,
        "",
        content_type="prose",
        physical_collection=_COLLECTION,
        title="Preflight",
    )
    assert doc_id == "1.1.42", "preflight must register via the service catalog"
    cat_path: Path = service_mode_fresh_box
    assert not cat_path.exists(), (
        "service mode must not auto-init a local catalog (new local SQLite "
        "substrate; nexus-e9ru2 site 3 / no-SQLite-destination directive)"
    )
    (client,) = fake_client.instances
    (reg,) = client.registered
    assert reg["title"] == "Preflight"
    assert reg["chunk_count"] == 0, "preflight registers with chunk_count=0"


def test_register_or_lookup_sqlite_mode_still_auto_inits(tmp_path, monkeypatch):
    """SQLite opt-out mode keeps the nexus-fq3b auto-init: a fresh local
    path gets a catalog created and a real registration lands in it."""
    cat_path = tmp_path / "fresh-local-catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_path))
    # suite-wide pin already forces sqlite mode; assert it to keep this
    # test honest if the pin ever changes
    from nexus.db.storage_mode import StorageBackend, storage_backend_for

    assert storage_backend_for("catalog") == StorageBackend.SQLITE

    from nexus.doc_indexer import _register_or_lookup_doc_id

    doc = tmp_path / "pre.md"
    doc.write_text("preflight body\n")
    doc_id = _register_or_lookup_doc_id(
        doc,
        "",
        content_type="prose",
        physical_collection=_COLLECTION,
        title="Preflight",
    )
    assert cat_path.exists(), "sqlite mode must keep the fq3b auto-init"
    assert doc_id, "auto-init + local registration must yield a tumbler"


def test_audit_catalog_conn_is_none_in_service_mode(tmp_path, monkeypatch):
    """On a MIGRATED box (frozen local .catalog.db present) the audit
    helper must not hand out a connection to stale data in service mode —
    same degrade the module already applies to taxonomy raw access
    (nexus-9613q.4)."""
    from nexus.catalog import Catalog

    cat_path = tmp_path / "frozen-catalog"
    Catalog.init(cat_path)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_path))

    from nexus.collection_audit import _open_catalog_conn

    monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
    assert _open_catalog_conn() is None, (
        "service mode: the local .catalog.db is a frozen migration source; "
        "audit legs must degrade (orphans=[]) rather than report stale data"
    )

    monkeypatch.delenv("NX_STORAGE_BACKEND_CATALOG")
    conn = _open_catalog_conn()
    try:
        assert conn is not None, "sqlite mode with an initialised catalog keeps the conn"
    finally:
        if conn is not None:
            conn.close()


def test_markdown_hook_service_down_is_loud_not_silent(
    service_mode_fresh_box, fake_client, tmp_path, monkeypatch
):
    """nexus-ou4tb (e9ru2 review site): post-sweep, service-down reaches the
    markdown hook's except block on fresh boxes — it must WARN + audit like
    the pdf hook, not swallow at DEBUG."""
    from nexus.doc_indexer import _catalog_markdown_hook

    def _boom(self, name):
        raise ConnectionError("service unreachable")

    monkeypatch.setattr(
        _FakeHttpCatalogClient, "curator_owner_tumbler_by_name", _boom
    )

    audit_rows: list[dict] = []
    import nexus.hook_registry as hook_registry

    monkeypatch.setattr(
        hook_registry,
        "record_catalog_hook_failure",
        lambda **kw: audit_rows.append(kw),
    )

    md = tmp_path / "note.md"
    md.write_text("body\n")
    _catalog_markdown_hook(md, _COLLECTION, "prose", "", 1)  # must not raise
    (row,) = audit_rows
    assert row["hook_name"] == "catalog_markdown_hook"
    assert row["collection"] == _COLLECTION
    assert "unreachable" in row["error"]
