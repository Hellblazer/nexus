# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.14 against the real engine: the recovery bundle carries each note's own tags,
category and ttl_days, and imports them back through the note writer.

Two notes with identical text share one chunk row that holds the last writer's keys (RDR-108); the
bundle takes each note's own from its catalog entry's ``meta`` (nexus-6pfgb.5). The storage-free
twin, with the legacy and retraction cases, is ``tests/catalog/test_recovery_bundle.py``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog import store_hook
from nexus.catalog.factory import make_catalog_reader
from nexus.catalog.note_write import STORED, put_note
from nexus.catalog.store_hook import NOTE_META_KEYS
from nexus.catalog.recovery_bundle import export_bundle, import_bundle, read_bundle
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp.core import store_put

pytestmark = pytest.mark.integration


def test_two_notes_sharing_a_chunk_export_and_import_their_own_keys(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "6pfgb14-shared"
    body = "6pfgb14 identical body stored under two titles with different keys"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=body, collection=subject, title="keys-A", tags="alpha,one",
                  category="design", ttl="7d", agent="agent-a")
        store_put(content=body, collection=subject, title="keys-B", tags="beta,two",
                  category="finding", agent="agent-b")
    collection = t3_collection_name(subject, t3=client)

    reader = make_catalog_reader()
    assert reader is not None
    bundle = tmp_path / "bundle.jsonl"
    summary = export_bundle(reader, client, bundle)
    _header, records = read_bundle(bundle)
    docs = {r["title"]: r for r in records
            if r["record"] == "knowledge_doc" and r["collection"] == collection}

    assert set(docs) == {"keys-A", "keys-B"}
    a, b = docs["keys-A"], docs["keys-B"]
    assert (a["tags"], a["category"], a["ttl_days"]) == (["alpha", "one"], "design", 7)
    assert (b["tags"], b["category"], b["ttl_days"]) == (["beta", "two"], "finding", None)
    assert summary.note_keys_unrecovered == 0

    # Importing the bundle back reconciles onto both notes and leaves each with its own keys.
    with patch("nexus.mcp.core._get_t3", return_value=client):
        imported = import_bundle(reader, reader, client, bundle)
    assert imported.docs_failed == 0 and imported.docs_uncertain == 0, imported.doc_failures

    for title, tags, ttl in (("keys-A", ["alpha", "one"], 7), ("keys-B", ["beta", "two"], 0)):
        doc = reader.by_source_uri(uri_for(collection, title))
        assert doc is not None
        assert (doc.meta or {}).get("tags") == tags
        assert (doc.meta or {}).get("ttl_days", 0) == ttl


def _exported(client, tmp_path, collection):
    reader = make_catalog_reader()
    assert reader is not None
    bundle = tmp_path / "bundle.jsonl"
    summary = export_bundle(reader, client, bundle)
    _header, records = read_bundle(bundle)
    return summary, {r["title"]: r for r in records
                     if r["record"] == "knowledge_doc" and r["collection"] == collection}


def _put_without_put_at(**kw):
    """``put_note`` as a client before nexus-6pfgb.15 wrote it: no ``meta.put_at``. With every other
    key unset the entry then holds no note key at all, the shape the export's row gate is for."""
    real = store_hook.catalog_store_hook_tracked

    def _hook(*args, **kwargs):
        return real(*args, **{**kwargs, "put_at": ""})

    with patch.object(store_hook, "catalog_store_hook_tracked", _hook):
        return put_note(**kw)


@pytest.mark.parametrize(
    ("put_a", "a_holds_put_at", "unrecovered"),
    [(_put_without_put_at, False, 1), (put_note, True, 0)],
    ids=["entry-with-no-note-key", "entry-with-put-at-only"],
)
def test_a_note_stored_with_no_keys_never_exports_a_departed_co_owners_keys(
    t2_service_env, tmp_path, put_a, a_holds_put_at, unrecovered,
):
    """A (every key unset) and B (keys) store the same text; B then moves to other text. A is the
    chunk's only owner, and the row still names B and holds B's tags, category and ttl_days: A must
    export none of them.

    Whether A is counted depends on its entry. One with no note key (written before nexus-6pfgb.5,
    or with every key unset before nexus-6pfgb.15) cannot say what A's keys were, so the dropped row
    values are counted. A put since nexus-6pfgb.15 always writes ``put_at``, which makes the entry
    authoritative: A's keys are known to be empty, nothing of A's is lost, and A is not counted."""
    client = HttpVectorClient(tenant=t2_service_env)
    collection = t3_collection_name("6pfgb14-departed", t3=client)
    body = "6pfgb14 body that outlives its second owner"
    a = put_a(content=body, collection=collection, title="unset-A")
    assert a.status == STORED, a.reason
    a_meta = make_catalog_reader().by_source_uri(uri_for(collection, "unset-A")).meta or {}
    assert [k for k in NOTE_META_KEYS if k in a_meta] == (["put_at"] if a_holds_put_at else []), a_meta
    b = put_note(content=body, collection=collection, title="keyed-B", tags="beta", category="finding",
                 ttl_days=7, source_agent="agent-b")
    assert b.status == STORED, b.reason
    row = client.get_by_id(collection, a.chunk_ids[0])
    # The engine returns the last writer's catalog_doc_id and title in the row's metadata: the pair
    # note_ttl.row_written_by reads, the signal the export gates on (nexus-6pfgb.34).
    assert row["catalog_doc_id"] == b.catalog_doc_id and row["title"] == "keyed-B" and row["ttl_days"] == 7
    moved = put_note(content=body + " (moved)", collection=collection, title="keyed-B", tags="beta",
                     category="finding", ttl_days=7, source_agent="agent-b")
    assert moved.status == STORED, moved.reason

    summary, docs = _exported(client, tmp_path, collection)

    assert (docs["unset-A"]["tags"], docs["unset-A"]["category"], docs["unset-A"]["ttl_days"]) == ([], "", None)
    assert docs["keyed-B"]["ttl_days"] == 7
    assert summary.note_keys_unrecovered == unrecovered


def _put_keyless(**kw):
    """``put_note`` as a client before nexus-6pfgb.5 wrote it: the chunk row carries the put's tags,
    category and ttl_days, the catalog entry none of them (and no ``put_at``)."""
    real = store_hook.catalog_store_hook_tracked

    def _hook(*args, **kwargs):
        return real(*args, **{**kwargs, "tags": "", "category": "", "ttl_days": None,
                               "source_agent": "", "put_at": ""})

    with patch.object(store_hook, "catalog_store_hook_tracked", _hook):
        return put_note(**kw)


def test_a_keyless_note_that_wrote_its_own_row_exports_the_rows_values(t2_service_env, tmp_path):
    """B (keys) then A, a pre-catalog-keys put with tags, category and a TTL of its own, store the same
    text. A's entry holds no note key; the row is A's write (A's stamp, A's title) and holds A's values,
    so the export carries them through ``row_written_by``'s accept branch (nexus-6pfgb.34). B is not
    the row's writer and keeps its own catalog keys."""
    client = HttpVectorClient(tenant=t2_service_env)
    collection = t3_collection_name("6pfgb14-lastwriter", t3=client)
    body = "6pfgb14 body written last by a note whose keys are only on the row"
    b = put_note(content=body, collection=collection, title="keyed-B", tags="beta", category="finding",
                 ttl_days=7, source_agent="agent-b")
    a = _put_keyless(content=body, collection=collection, title="legacy-A", tags="alpha",
                     category="design", ttl_days=30, source_agent="agent-a")
    assert b.status == STORED and a.status == STORED, (b.reason, a.reason)
    a_meta = make_catalog_reader().by_source_uri(uri_for(collection, "legacy-A")).meta or {}
    assert [k for k in NOTE_META_KEYS if k in a_meta] == [], a_meta
    row = client.get_by_id(collection, a.chunk_ids[0])
    assert row["catalog_doc_id"] == a.catalog_doc_id and row["title"] == "legacy-A"
    assert (row["tags"], row["category"], row["ttl_days"]) == ("alpha", "design", 30)

    summary, docs = _exported(client, tmp_path, collection)

    assert (docs["legacy-A"]["tags"], docs["legacy-A"]["category"], docs["legacy-A"]["ttl_days"]) == (
        ["alpha"], "design", 30)
    assert (docs["keyed-B"]["tags"], docs["keyed-B"]["category"], docs["keyed-B"]["ttl_days"]) == (
        ["beta"], "finding", 7)
    assert summary.note_keys_unrecovered == 0


def test_a_rejected_keyless_note_re_put_with_its_values_exports_them(t2_service_env, tmp_path):
    """The remedy ``nx catalog export`` prints: a keyless note the export could not recover, re-put
    under its title with its values by a current client, exports those values on the next export.
    The re-put lands on the same catalog entry, which then holds note keys and answers for the note."""
    client = HttpVectorClient(tenant=t2_service_env)
    collection = t3_collection_name("6pfgb14-remedy", t3=client)
    body = "6pfgb14 body whose keyless owner is re-put after a departed co-owner"
    a = _put_without_put_at(content=body, collection=collection, title="unset-A")
    b = put_note(content=body, collection=collection, title="keyed-B", tags="beta", category="finding",
                 ttl_days=7, source_agent="agent-b")
    moved = put_note(content=body + " (moved)", collection=collection, title="keyed-B", tags="beta",
                     category="finding", ttl_days=7, source_agent="agent-b")
    assert a.status == STORED and b.status == STORED and moved.status == STORED
    summary, _docs = _exported(client, tmp_path, collection)
    assert summary.note_keys_unrecovered_notes == [(collection, "unset-A")]

    again = put_note(content=body, collection=collection, title="unset-A", tags="alpha", category="design",
                     ttl_days=30, source_agent="agent-a")
    assert again.status == STORED and again.catalog_doc_id == a.catalog_doc_id, again

    second = tmp_path / "second"
    second.mkdir()
    summary, docs = _exported(client, second, collection)
    assert (docs["unset-A"]["tags"], docs["unset-A"]["category"], docs["unset-A"]["ttl_days"]) == (
        ["alpha"], "design", 30)
    assert summary.note_keys_unrecovered == 0
