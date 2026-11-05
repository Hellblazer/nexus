# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 Step 3e (nexus-rfvyy.6.12): the ``.nxexp`` export and the recovery bundle carry a
document's ``sensitive`` flag, and import raises it again.

Real engine substrate. What each group pins:

* the WRITERS: the owner dict an export writes and the ``knowledge_doc`` record a bundle writes carry
  ``sensitive: true`` for a flagged document (a round trip alone would not show a writer that dropped it
  if the importer defaulted to the same value);
* the round trips: ``.nxexp`` export then import into a new collection (a freshly registered document),
  ``.nxexp`` import back over an existing document whose flag was cleared meanwhile, and a recovery bundle
  written then restored;
* one-way: a record without the key, or with it false, never lowers a flag the target holds;
* the refusals: an engine that does not advertise ``catalog_sensitivity_supported`` fails the sensitive
  group or record and writes nothing for it;
* order: a restored note is flagged BEFORE the post-store chains fire, so an aspect hook sees it.

The target of an ``.nxexp`` copy is a second collection in the same tenant, which registers a fresh
document (``nxexp://<target>/<uri>``); the existing-document leg re-imports into the source collection.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import nexus.catalog.http_catalog_client as hcc
import nexus.catalog.recovery_bundle as rb
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.recovery_bundle import ExportSummary, export_knowledge_doc, import_bundle, write_bundle
from nexus.db.http_vector_client import HttpVectorClient
from nexus.errors import NexusError
from nexus.exporter import _locate_owner_group, export_collection, import_collection
from tests.test_wbfpw31_nxexp_owner import _coll, _owned_doc, _read_nxexp_records

pytestmark = pytest.mark.t2_tenant

_REASON = "rfvyy612 test"


def _flag(tumbler: str, value: bool = True) -> None:
    writer = make_catalog_writer()
    try:
        writer.set_sensitivity([tumbler], value, reason=_REASON)
    finally:
        writer.close()


def _sensitive(source_uri: str) -> bool:
    entry = make_catalog_reader().by_source_uri(source_uri)
    assert entry is not None, source_uri
    return entry.sensitive


@pytest.fixture
def unsupported_engine(monkeypatch):
    monkeypatch.setattr(hcc.HttpCatalogClient, "_version_flags", lambda self: {"release_version": "0.0.1"})


# ── the writers ─────────────────────────────────────────────────────────────


def test_locate_owner_group_ors_the_flag_across_a_documents_records() -> None:
    groups: dict = {}
    kw = dict(fallback_source_uri="nxexp://c/f", fallback_title="f", fallback_content_type="knowledge",
              target_collection="knowledge__x__bge-base-en-v15-768__v1")
    owner = {"source_uri": "file:///a", "title": "A", "content_type": "knowledge"}
    g, _ = _locate_owner_group(groups, {**owner, "sensitive": False, "position": 0}, **kw)
    assert g["sensitive"] is False
    g, _ = _locate_owner_group(groups, {**owner, "sensitive": True, "position": 1}, **kw)
    assert g["sensitive"] is True
    g, _ = _locate_owner_group(groups, {**owner, "position": 2}, **kw)  # an old file's record has no key
    assert g["sensitive"] is True, "one record saying so is enough, and a later one without the key does not lower it"
    for bad in ("true", 1, None):
        g2, _ = _locate_owner_group({}, {**owner, "sensitive": bad}, **kw)
        assert g2["sensitive"] is False, f"only a boolean true counts, got {bad!r}"


def test_the_bundle_record_carries_the_entry_flag() -> None:
    def exported(**entry_kw):
        body = "rfvyy612 bundle body"
        import hashlib

        chash = hashlib.sha256(body.encode()).hexdigest()
        entry = SimpleNamespace(
            tumbler="1.1.7", title="t", physical_collection="knowledge__x__bge-base-en-v15-768__v1",
            source_uri="", meta={"put_at": "2026-11-01T00:00:00+00:00"}, **entry_kw)
        rec = export_knowledge_doc(entry, [SimpleNamespace(position=0, chash=chash)], {chash: {"content": body}},
                                   ExportSummary())
        assert rec is not None
        return rec

    assert exported(sensitive=True)["sensitive"] is True
    assert exported(sensitive=False)["sensitive"] is False
    assert exported()["sensitive"] is False, "an entry from an engine without the column"


def test_the_nxexp_export_writes_sensitive_on_every_record_of_a_flagged_document(t2_service_env, tmp_path) -> None:
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer()
    owner = writer.register_owner("knowledge", "curator")
    src = _coll("sens-export")
    flagged, flagged_uri, _ = _owned_doc(writer, client, src, owner, "flagged doc", ["flagged chunk zero", "flagged chunk one"])
    plain, plain_uri, _ = _owned_doc(writer, client, src, owner, "plain doc", ["plain chunk zero"])
    _flag(flagged)

    out = tmp_path / "w.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)
    by_uri: dict[str, set] = {}
    for r in _read_nxexp_records(out):
        by_uri.setdefault(r["owner"]["source_uri"], set()).add(r["owner"]["sensitive"])
    assert by_uri == {flagged_uri: {True}, plain_uri: {False}}


# ── .nxexp round trips ──────────────────────────────────────────────────────


def test_nxexp_import_into_a_new_collection_registers_the_copy_flagged(t2_service_env, tmp_path) -> None:
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer()
    owner = writer.register_owner("knowledge", "curator")
    src, dst = _coll("sens-rt-src"), _coll("sens-rt-dst")
    flagged, flagged_uri, _ = _owned_doc(writer, client, src, owner, "rt flagged", ["rt flagged zero", "rt flagged one"])
    plain, plain_uri, _ = _owned_doc(writer, client, src, owner, "rt plain", ["rt plain zero"])
    _flag(flagged)

    out = tmp_path / "rt.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)
    result = import_collection(db=client, input_path=out, target_collection=dst)
    assert result["owned_count"] == 3

    assert _sensitive(f"nxexp://{dst}/{flagged_uri}") is True, "the copy of a flagged document is flagged"
    assert _sensitive(f"nxexp://{dst}/{plain_uri}") is False, "and a plain one is not"


def test_nxexp_import_over_an_existing_document_raises_a_cleared_flag(t2_service_env, tmp_path) -> None:
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer()
    owner = writer.register_owner("knowledge", "curator")
    src = _coll("sens-over")
    flagged, flagged_uri, _ = _owned_doc(writer, client, src, owner, "over flagged", ["over flagged zero"])
    _flag(flagged)
    out = tmp_path / "over.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)

    _flag(flagged, False)
    assert _sensitive(flagged_uri) is False, "control: the flag was cleared after the export"
    import_collection(db=client, input_path=out, target_collection=src)
    assert _sensitive(flagged_uri) is True


def test_nxexp_import_of_a_plain_record_never_lowers_a_flag(t2_service_env, tmp_path) -> None:
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer()
    owner = writer.register_owner("knowledge", "curator")
    src = _coll("sens-keep")
    doc, uri, _ = _owned_doc(writer, client, src, owner, "keep doc", ["keep zero"])
    out = tmp_path / "keep.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)  # exported while plain
    _flag(doc)
    import_collection(db=client, input_path=out, target_collection=src)
    assert _sensitive(uri) is True


def test_nxexp_import_of_a_flagged_document_into_an_engine_without_the_capability_fails_and_registers_nothing(
    t2_service_env, tmp_path, monkeypatch,
) -> None:
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer()
    owner = writer.register_owner("knowledge", "curator")
    src, dst = _coll("sens-nocap-src"), _coll("sens-nocap-dst")
    doc, uri, _ = _owned_doc(writer, client, src, owner, "nocap doc", ["nocap zero"])
    _flag(doc)
    out = tmp_path / "nocap.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)

    monkeypatch.setattr(hcc.HttpCatalogClient, "_version_flags", lambda self: {"release_version": "0.0.1"})
    with pytest.raises(NexusError):
        import_collection(db=client, input_path=out, target_collection=dst)
    assert make_catalog_reader().by_source_uri(f"nxexp://{dst}/{uri}") is None, "no unflagged copy was registered"


# ── the recovery bundle ─────────────────────────────────────────────────────

_RECORDED = "knowledge__rfvyy612__bge-base-en-v15-768__v1"


def _rec(title: str, **kw) -> dict:
    return {"record": "knowledge_doc", "source_uri": "", "collection": _RECORDED, "title": title,
            "tags": "", "category": "", "content": f"rfvyy612 body of {title}", **kw}


@pytest.fixture
def t3(t2_service_env):
    from nexus.corpus import ensure_collection_registered
    from nexus.db import make_t3

    handle = make_t3()
    ensure_collection_registered(rb.target_collection_for(_RECORDED, handle))
    return handle


def _entry(title: str, t3):
    target = rb.target_collection_for(_RECORDED, t3)
    found = [d for d in make_catalog_reader().all_documents() if d.title == title and d.physical_collection == target]
    assert len(found) == 1, (title, found)
    return found[0]


def test_a_bundle_restores_the_flag(t3, tmp_path) -> None:
    path = tmp_path / "b.jsonl"
    write_bundle(path, [_rec("sens one", sensitive=True), _rec("sens two", sensitive=False), _rec("sens old")],
                 [], ExportSummary())
    summary = import_bundle(None, None, t3, path)
    assert (summary.docs_imported, summary.docs_failed) == (3, 0)
    assert _entry("sens one", t3).sensitive is True
    assert _entry("sens two", t3).sensitive is False
    assert _entry("sens old", t3).sensitive is False, "a bundle written before this step has no key"


def test_a_bundle_record_without_the_flag_never_lowers_an_existing_one(t3, tmp_path) -> None:
    path = tmp_path / "b.jsonl"
    write_bundle(path, [_rec("sens keep")], [], ExportSummary())
    assert import_bundle(None, None, t3, path).docs_imported == 1
    _flag(str(_entry("sens keep", t3).tumbler))
    assert import_bundle(None, None, t3, path).docs_imported == 1  # the same plain record again
    assert _entry("sens keep", t3).sensitive is True


def test_a_flagged_record_is_refused_by_an_engine_without_the_capability_and_nothing_lands(
    t3, tmp_path, unsupported_engine,
) -> None:
    path = tmp_path / "b.jsonl"
    write_bundle(path, [_rec("sens refused", sensitive=True), _rec("sens plain")], [], ExportSummary())
    summary = import_bundle(None, None, t3, path)
    assert (summary.docs_imported, summary.docs_failed) == (1, 1)
    assert "sensitiv" in summary.doc_failures[0]["error"].lower()
    target = rb.target_collection_for(_RECORDED, t3)
    titles = {d.title for d in make_catalog_reader().all_documents() if d.physical_collection == target}
    assert titles == {"sens plain"}, "the refused record registered no unflagged document"


def test_the_flag_is_raised_before_the_post_store_chains_fire(t3, tmp_path, monkeypatch) -> None:
    import nexus.catalog.note_write as nw

    seen: list[bool] = []
    real = nw.fire_note_chains

    def spy(outcome, content, **kw):
        entry = make_catalog_reader().resolve(outcome.catalog_doc_id, follow_alias=False)
        seen.append(entry.sensitive)
        return real(outcome, content, **kw)

    monkeypatch.setattr(nw, "fire_note_chains", spy)
    path = tmp_path / "b.jsonl"
    write_bundle(path, [_rec("sens order", sensitive=True)], [], ExportSummary())
    assert import_bundle(None, None, t3, path).docs_imported == 1
    assert seen == [True], "an aspect hook fired by the chains must already see the flag"
