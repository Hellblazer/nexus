# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.31 against the real engine: a put never adopts, re-sources or re-keys another note.

Two sequences from the nexus-6pfgb.15 confirmation review turned a permanent note into an expiring
one through the put path's identity reconcile (the chash dedup in ``catalog_store_hook_tracked``):

1. a titled permanent note A, then the same text stored with NO title and ``ttl=1d``: the dedup
   adopted A, blanked its ``source_uri`` and stamped ``ttl_days``/``put_at`` on it;
2. a legacy permanent note A (empty ``source_uri``), then the same text under ANOTHER title with
   ``ttl=1d``: A was adopted, given the put's source_uri and the TTL.

``nx store expire`` then retired A. Now each put gets a document of its own (identical text under
another title, or none, is a second document sharing the chunk, RDR-108) and A is never written.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp.core import store_put
from tests._catalog_fixture_ops import ActiveCatalog, documents_by_title
from tests._chunk_seed import seed_chunks_direct

pytestmark = pytest.mark.integration


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _chash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


class _World:
    def __init__(self, tenant: str, subject: str) -> None:
        self.client = HttpVectorClient(tenant=tenant)
        self.subject = subject
        self.collection: str | None = None

    def put(self, title: str, body: str, *, ttl: str | None = None) -> None:
        kwargs = {"ttl": ttl} if ttl else {}
        with patch("nexus.mcp.core._get_t3", return_value=self.client):
            store_put(content=body, collection=self.subject, title=title, **kwargs)
        self.collection = t3_collection_name(self.subject, t3=self.client)

    def holders(self, body: str) -> list:
        """The documents whose manifest holds the text's chunk."""
        reader = make_catalog_reader()
        assert reader is not None
        return [reader.resolve(t) for t in reader.docs_for_chashes([_chash(body)]).get(_chash(body), [])]

    def age(self, doc, days: int, body: str) -> None:
        """Move a note's own clock back (its catalog ``put_at``) and its chunk row's ``indexed_at``,
        and put the row's ``ttl_days`` at 1 as the last TTL writer leaves it."""
        aged = _days_ago(days)
        make_catalog_writer().update(doc.tumbler, meta={"put_at": aged})
        seed_chunks_direct(
            self.collection, ids=[_chash(body)], documents=[body],
            metadatas=[{"indexed_at": aged, "ttl_days": 1}], embed=True)

    def chunk_served(self, body: str) -> bool:
        return self.client.get_by_id(self.collection, _chash(body)) is not None


def _snapshot(doc) -> tuple:
    return (doc.title, doc.source_uri, dict(doc.meta))


def test_an_untitled_ttl_put_of_a_titled_notes_text_leaves_the_note_permanent(t2_service_env):
    """Repro 1."""
    w = _World(t2_service_env, "6pfgb31-untitled")
    body = "6pfgb31 untitled ttl put of the text a titled permanent note holds"
    w.put("note-A", body)
    a = w.holders(body)[0]
    assert a.title == "note-A" and a.source_uri == uri_for(w.collection, "note-A"), "control"
    before = _snapshot(a)

    w.put("", body, ttl="1d")

    docs = w.holders(body)
    assert len(docs) == 2, [(d.title, d.source_uri) for d in docs]
    mine = next(d for d in docs if d.tumbler == a.tumbler)
    assert _snapshot(mine) == before, "the titled note is not re-sourced or re-keyed"
    assert "ttl_days" not in mine.meta
    other = next(d for d in docs if d.tumbler != a.tumbler)
    assert other.title == "" and other.meta.get("ttl_days") == 1

    w.age(other, 2, body)
    w.client.expire()

    survivors = [d for d in w.holders(body) if d.tumbler == a.tumbler]
    assert survivors and _snapshot(survivors[0]) == before, "the permanent note survives the sweep"
    assert w.chunk_served(body), "its chunk is still served"


def test_a_second_title_ttl_put_of_a_legacy_notes_text_leaves_the_note_permanent(t2_service_env):
    """Repro 2: the legacy note has an empty source_uri, so only its title tells it from the put."""
    w = _World(t2_service_env, "6pfgb31-legacy")
    w.put("legacy-seed", "6pfgb31 legacy seed, only to register the collection")
    body = "6pfgb31 legacy permanent note text, then a ttl put under another title"
    cat = ActiveCatalog()
    owner = cat.register_owner("6pfgb31-legacy-owner", "curator")
    tumbler = cat.register(
        owner, "legacy-A", content_type="knowledge", physical_collection=w.collection,
        meta={"doc_id": _chash(body)})
    seed_chunks_direct(
        w.collection, ids=[_chash(body)], documents=[body], embed=True,
        metadatas=[{"indexed_at": _days_ago(500), "chunk_text_hash": _chash(body),
                    "catalog_doc_id": str(tumbler), "title": "legacy-A"}])
    cat.append_manifest_chunks(str(tumbler), [{"chash": _chash(body), "position": 0}], collection=w.collection)
    cat.resync_chunk_count_cache(str(tumbler))
    a = next(d for d in w.holders(body) if d.title == "legacy-A")
    assert a.source_uri == "", "control: a legacy note with no source_uri"
    before = _snapshot(a)

    w.put("other-title", body, ttl="1d")

    docs = w.holders(body)
    assert sorted(d.title for d in docs) == ["legacy-A", "other-title"]
    mine = next(d for d in docs if d.tumbler == a.tumbler)
    assert _snapshot(mine) == before, "the legacy note is not re-sourced or re-keyed"
    other = next(d for d in docs if d.tumbler != a.tumbler)
    assert other.source_uri == uri_for(w.collection, "other-title") and other.meta.get("ttl_days") == 1

    w.age(other, 2, body)
    w.client.expire()

    assert len(documents_by_title("legacy-A")) == 1, "the legacy permanent note survives the sweep"
    assert w.chunk_served(body)


def test_identical_content_under_a_second_title_is_still_a_second_document(t2_service_env):
    """nexus-enej7 holds."""
    w = _World(t2_service_env, "6pfgb31-enej7")
    body = "6pfgb31 identical body under two titles"
    w.put("shared-A", body)
    w.put("shared-B", body)

    docs = w.holders(body)
    assert sorted((d.title, d.source_uri) for d in docs) == [
        ("shared-A", uri_for(w.collection, "shared-A")), ("shared-B", uri_for(w.collection, "shared-B"))]


def test_a_same_title_reput_reconciles_onto_its_own_note(t2_service_env):
    w = _World(t2_service_env, "6pfgb31-same")
    body = "6pfgb31 same title twice, the second with a ttl"
    w.put("same", body)
    first = w.holders(body)[0]

    w.put("same", body, ttl="3d")

    docs = w.holders(body)
    assert len(docs) == 1 and docs[0].tumbler == first.tumbler
    assert docs[0].meta.get("ttl_days") == 3 and docs[0].source_uri == uri_for(w.collection, "same")


def test_an_untitled_reput_does_not_multiply_untitled_documents(t2_service_env):
    """With a titled note holding the text, two untitled puts make one untitled document, not two."""
    w = _World(t2_service_env, "6pfgb31-untitled2")
    body = "6pfgb31 two untitled puts beside a titled note"
    w.put("note-A", body)
    w.put("", body)
    w.put("", body, ttl="2d")

    docs = w.holders(body)
    assert sorted(d.title for d in docs) == ["", "note-A"], [(d.title, d.source_uri) for d in docs]
    assert next(d for d in docs if d.title == "").meta.get("ttl_days") == 2
    assert "ttl_days" not in next(d for d in docs if d.title == "note-A").meta
