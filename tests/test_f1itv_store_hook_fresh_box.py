# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f1itv: catalog_store_hook must register via the service-mode
catalog on a FRESH box (no local catalog dir).

The pre-fix gate checked ``Catalog.is_initialized(catalog_path())`` — a
LOCAL filesystem probe — before consulting the factory, and returned ""
on every virgin service-mode install. Established (migrated) boxes passed
the gate only because the frozen migration-source ``.catalog.db`` still
exists on disk. Result on fresh boxes: the document was never registered
in the engine catalog, ``catalog_doc_id=""`` flowed into the manifest
hook, and ``manifest_hook_batch_missing_doc_identity`` fired with no
manifest written (found in the 6.15.0 post-release shakeout).

The fix delegates presence semantics to ``make_catalog_reader()``: it
returns ``None`` only in the SQLite opt-out mode with an uninitialised
local catalog; in service mode the factory always returns a handle (the
Java service owns the catalog — factory.py's own contract). The SQLite
opt-out skip stays pinned by
``test_catalog_knowledge_hook.py::test_skipped_when_not_initialized``.
"""
from __future__ import annotations

import pytest

import nexus.catalog.factory as factory
from nexus.catalog.store_hook import catalog_store_hook

#: Full sha256 hex chunk natural-id (RDR-180 wire shape).
_DOC_ID = "a" * 64
_COLLECTION = "knowledge__knowledge__bge-base-en-v15-768__v1"


@pytest.fixture(autouse=True)
def _service_mode_fresh_box(tmp_path, monkeypatch):
    # Service mode ON (overrides the suite-wide sqlite pin) ...
    monkeypatch.setattr(factory, "_is_catalog_service_mode", lambda: True)
    # ... on a genuinely fresh box: catalog_path() resolves to a directory
    # that does not exist. The pre-fix gate keyed on exactly this.
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog-here"))
    factory.reset_shared_service_catalog_client_for_tests()
    yield
    factory.reset_shared_service_catalog_client_for_tests()


class _FakeHttpCatalogClient:
    """Stateful fake at the HTTP boundary (house pattern:
    ``test_catalog_factory_service_mode_cache.py``). The hook reaches it
    through the REAL factory routing — ``_SharedServiceCatalogHandle`` for
    reads, ``_ServiceCatalogWriter`` (write-op whitelist) for writes — so
    the gate under test and everything below it stay production code.
    """

    instances: list["_FakeHttpCatalogClient"] = []

    def __init__(self, *_a, **_kw) -> None:
        self.registered: list[dict] = []
        self.owners: list[tuple[str, str]] = []
        _FakeHttpCatalogClient.instances.append(self)

    # -- reads (via _SharedServiceCatalogHandle) --
    def by_doc_id(self, doc_id):
        return None  # fresh box: nothing registered yet

    def curator_owner_tumbler_by_name(self, name):
        return None  # fresh box: no curator owner yet

    def find(self, query, content_type=None):
        return []  # fresh box: no ghost entries to reconcile

    # -- writes (via _ServiceCatalogWriter, CATALOG_WRITE_OPS) --
    def register_owner(self, name, owner_type):
        self.owners.append((name, owner_type))
        return f"owner:{name}"

    def register(self, *, owner, title, content_type, physical_collection, meta):
        self.registered.append(
            {
                "owner": owner,
                "title": title,
                "content_type": content_type,
                "physical_collection": physical_collection,
                "meta": meta,
            }
        )
        return "1.1.42"

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


def test_fresh_box_service_mode_registers(fake_client):
    tumbler = catalog_store_hook(
        title="fresh-box-note",
        doc_id=_DOC_ID,
        collection_name=_COLLECTION,
    )
    assert tumbler == "1.1.42", (
        "on a fresh service-mode box the hook must register through the "
        "service catalog and return its tumbler; '' means the stale local "
        "is_initialized gate short-circuited registration (nexus-f1itv)"
    )
    (client,) = fake_client.instances
    assert client.owners == [("knowledge", "curator")], (
        "fresh box has no curator owner; the hook must create it server-side"
    )
    (reg,) = client.registered
    assert reg["title"] == "fresh-box-note"
    assert reg["content_type"] == "knowledge"
    assert reg["physical_collection"] == _COLLECTION
    assert reg["meta"] == {"doc_id": _DOC_ID}


def test_fresh_box_service_mode_dedups_by_doc_id(fake_client, monkeypatch):
    """Existing service-side entry short-circuits to its tumbler — the
    dedup read must also run on a fresh box (no local catalog)."""

    class _Existing:
        tumbler = "1.1.7"

    monkeypatch.setattr(
        _FakeHttpCatalogClient, "by_doc_id", lambda self, doc_id: _Existing()
    )
    tumbler = catalog_store_hook(
        title="fresh-box-note",
        doc_id=_DOC_ID,
        collection_name=_COLLECTION,
    )
    assert tumbler == "1.1.7"
    (client,) = fake_client.instances
    assert client.registered == [], "dedup hit must not mint a new document"


def test_fresh_box_service_unreachable_is_loud_not_silent(fake_client, monkeypatch):
    """critic-f1itv Medium: the fix changes fresh-box + service-down from a
    SILENT "" (the old local gate returned before any I/O) to the loud
    ou4tb path — best-effort "" return, but WARNING + audit row recorded.
    Pin the new trigger condition at runtime, not just via the static
    source-text scan in test_ou4tb_catalog_hook_loudness.py."""

    def _unreachable(self, doc_id):
        raise ConnectionError("service unreachable (fresh box, transient)")

    monkeypatch.setattr(_FakeHttpCatalogClient, "by_doc_id", _unreachable)

    audit_rows: list[dict] = []
    import nexus.hook_registry as hook_registry

    monkeypatch.setattr(
        hook_registry,
        "record_catalog_hook_failure",
        lambda **kw: audit_rows.append(kw),
    )

    tumbler = catalog_store_hook(
        title="fresh-box-note",
        doc_id=_DOC_ID,
        collection_name=_COLLECTION,
    )
    assert tumbler == "", "hook is best-effort: service-down must not raise"
    (row,) = audit_rows
    assert row["hook_name"] == "catalog_store_hook"
    assert row["source_path"] == _DOC_ID
    assert row["collection"] == _COLLECTION
    assert "unreachable" in row["error"]
