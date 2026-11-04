# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-147 Step 1 (nexus-fai28): the recovery bundle carries a note's entity key.

The key lives in the catalog entry's ``meta`` and is not a ``NOTE_META_KEYS`` member, so the export
read only tags, category and ttl_days, and an import re-put the note with no key, which retracts the
one the source held. The export test is pure (a fake entry and chunk rows through
``export_knowledge_doc``); the import test runs on the engine substrate and reads the catalog entry
back.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

import nexus.catalog.recovery_bundle as rb
from nexus.catalog.factory import make_catalog_reader
from nexus.catalog.recovery_bundle import ExportSummary, export_knowledge_doc, import_bundle, write_bundle
from nexus.corpus import ensure_collection_registered
from nexus.db import make_t3

_RECORDED = "knowledge__fai28rec__bge-base-en-v15-768__v1"


def _exported(meta: dict, body: str = "fai28 recovery body") -> dict:
    chash = hashlib.sha256(body.encode()).hexdigest()
    entry = SimpleNamespace(
        tumbler="1.1.7", title="Customer record", physical_collection=_RECORDED, source_uri="", meta=meta)
    rows = [SimpleNamespace(position=0, chash=chash)]
    record = export_knowledge_doc(entry, rows, {chash: {"content": body}}, ExportSummary())
    assert record is not None, "the fake note was not exported: the assertions below would be vacuous"
    return record


def test_the_export_carries_the_entry_entity_key():
    record = _exported({"put_at": "2026-11-01T00:00:00+00:00", "entity_key": "CUST-0018"})
    assert record["entity_key"] == "CUST-0018"


def test_the_export_of_an_entry_without_a_key_carries_an_empty_one():
    assert _exported({"put_at": "2026-11-01T00:00:00+00:00"})["entity_key"] == ""
    assert _exported({"put_at": "2026-11-01T00:00:00+00:00", "entity_key": ""})["entity_key"] == ""


def test_a_legacy_entry_with_only_a_key_still_exports_it():
    """An entry holding none of the note keys takes the legacy path for tags and the rest; its key is
    read on its own."""
    assert _exported({"entity_key": "CUST-0018"})["entity_key"] == "CUST-0018"


@pytest.mark.t2_tenant
def test_an_import_stamps_the_exported_key_and_an_old_bundle_without_one_stamps_none(t2_service_env, tmp_path):
    t3 = make_t3()
    target = rb.target_collection_for(_RECORDED, t3)
    ensure_collection_registered(target)

    keyed = _exported({"put_at": "2026-11-01T00:00:00+00:00", "entity_key": "CUST-0018"}, "fai28 keyed body")
    keyed["title"] = "fai28 keyed"
    old = {k: v for k, v in _exported({"put_at": "2026-11-01T00:00:00+00:00"}, "fai28 old body").items()
           if k != "entity_key"}
    old["title"] = "fai28 old"
    path = tmp_path / "bundle.jsonl"
    write_bundle(path, [keyed, old], [], ExportSummary())

    summary = import_bundle(None, None, t3, path)

    assert (summary.docs_imported, summary.docs_failed) == (2, 0)
    docs = {d.title: d for d in make_catalog_reader().all_documents() if d.physical_collection == target}
    assert docs["fai28 keyed"].meta["entity_key"] == "CUST-0018"
    assert not docs["fai28 old"].meta.get("entity_key")
