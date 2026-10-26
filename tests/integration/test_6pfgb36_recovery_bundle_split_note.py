# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.36 against the real engine: a note ``put_note`` wrote as several pieces (RDR-223)
exports its whole text and round-trips through import.

The export used to raise ``SingleChunkInvariantError`` on any note with more than one manifest row;
347 of 433 notes on a live tenant were split, so ``nx catalog export`` could not finish there. The
storage-free twin (piece order, a missing piece) is ``tests/catalog/test_recovery_bundle.py``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog import store_hook
from nexus.catalog.factory import make_catalog_reader
from nexus.catalog.note_write import STORED, put_note
from nexus.catalog.recovery_bundle import export_bundle, import_bundle, read_bundle
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient

pytestmark = pytest.mark.integration


def _body(tag: str) -> str:
    """About 6,000 characters, several times ``NOTE_SPLIT_CHARS``, so ``put_note`` splits it."""
    return "".join(
        f"{tag} paragraph {i}: the recovery bundle must carry every piece of a split note, "
        f"joined in manifest position order, never only the first.\n\n"
        for i in range(48)
    )


def _put_keyless(**kw):
    """``put_note`` as a client before nexus-6pfgb.5 wrote it: the keys on the chunk rows only."""
    real = store_hook.catalog_store_hook_tracked

    def _hook(*args, **kwargs):
        return real(*args, **{**kwargs, "tags": "", "category": "", "ttl_days": None,
                               "source_agent": "", "put_at": ""})

    with patch.object(store_hook, "catalog_store_hook_tracked", _hook):
        return put_note(**kw)


def _stored_text(reader, client, collection: str, title: str) -> tuple[str, int]:
    doc = reader.by_source_uri(uri_for(collection, title))
    assert doc is not None, title
    rows = sorted(reader.get_manifest(str(doc.tumbler)), key=lambda r: r.position)
    pieces = [client.get_by_id(collection, r.chash) for r in rows]
    assert all(p is not None for p in pieces), title
    return "".join(p["content"] for p in pieces), len(rows)


def test_a_split_note_exports_its_whole_text_and_round_trips_through_import(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    collection = t3_collection_name("6pfgb36-split", t3=client)
    keyed_body, keyless_body = _body("keyed"), _body("keyless")
    keyed = put_note(content=keyed_body, collection=collection, title="split-keyed", tags="alpha",
                     category="design", ttl_days=7, source_agent="agent-a")
    keyless = _put_keyless(content=keyless_body, collection=collection, title="split-keyless",
                           tags="beta", category="finding", ttl_days=30, source_agent="agent-b")
    assert keyed.status == STORED and keyless.status == STORED, (keyed, keyless)

    reader = make_catalog_reader()
    assert reader is not None
    # Non-vacuity: both notes really are split, or this pins nothing the single-chunk export missed.
    for title, body in (("split-keyed", keyed_body), ("split-keyless", keyless_body)):
        text, pieces = _stored_text(reader, client, collection, title)
        assert pieces > 1, f"{title} was stored as one piece"
        assert text == body

    bundle = tmp_path / "bundle.jsonl"
    summary = export_bundle(reader, client, bundle)
    _header, records = read_bundle(bundle)
    docs = {r["title"]: r for r in records
            if r["record"] == "knowledge_doc" and r["collection"] == collection}
    assert set(docs) == {"split-keyed", "split-keyless"}
    assert docs["split-keyed"]["content"] == keyed_body
    assert docs["split-keyless"]["content"] == keyless_body
    assert (docs["split-keyed"]["tags"], docs["split-keyed"]["category"],
            docs["split-keyed"]["ttl_days"]) == ("alpha", "design", 7)
    # The keyless note wrote its own first piece, so the first piece's row values are its own.
    assert (docs["split-keyless"]["tags"], docs["split-keyless"]["category"],
            docs["split-keyless"]["ttl_days"]) == ("beta", "finding", 30)
    assert summary.notes_incomplete == 0

    imported = import_bundle(reader, reader, client, bundle)
    assert imported.docs_failed == 0 and imported.docs_uncertain == 0, imported.doc_failures
    assert imported.docs_stamp_refused == 0, imported.doc_unverified
    for title, body in (("split-keyed", keyed_body), ("split-keyless", keyless_body)):
        text, pieces = _stored_text(reader, client, collection, title)
        assert pieces > 1 and text == body, title
