# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): ``nx collection re-embed`` against an
engine that refuses ownerless chunk writes.

``_reembed_collection`` reads a live page of a collection and writes it back with
``force_re_embed``. A chunk can lose its owner between the two, because a note
was superseded (or a document deleted) by another process in that window. The
engine then refuses the WHOLE request: ``upsert-chunks`` is 422 when any chash in
it has no live manifest row. Without a client answer the command aborts on the
first race with no way to resume.

The answer (decided in the Phase 2 gate, T2 ``rdr-223-phase2-gate-crosswalk`` F2):
on that refusal, re-read the batch through the live-filtered get and resend only
the chashes that are still owned. The engine keeps refusing the whole request; the
client is the one that narrows it.

The first test runs the real engine substrate (``t2_service_env``) and supersedes a
note between the read and the write, so the refusal is the engine's own. The
others pin the client's branches without an engine.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import nexus.db.http_vector_client as hvc
from nexus.commands.collection import _reembed_collection
from nexus.db.engine_reasons import OWNERLESS_CHUNK_WRITE_REASON
from nexus.db.http_vector_client import VectorServiceError

_MODEL = "bge-base-en-v15-768"
_COLLECTION = f"knowledge__z0o2p24-reembed__{_MODEL}__v1"


class _RecordingHooks:
    """Stands in for the post-store chains: records what was fired, runs nothing."""

    def __init__(self) -> None:
        self.fired: list[list[str]] = []

    def fire_store_chains(self, ids, collection, documents, **_kwargs) -> None:
        self.fired.append(list(ids))


def _put_note(client, *, title: str, content: str) -> tuple[str, str]:
    """Write one note through the real combined write; returns ``(tumbler, chash)``."""
    from nexus.catalog.note_write import write_note
    from nexus.catalog.store_hook import (
        catalog_store_hook_tracked,
        note_content_hash,
        note_manifest_metadata,
        note_pieces,
    )

    pieces = note_pieces(content, _COLLECTION)
    doc_id, manifest_metadatas = note_manifest_metadata(pieces)
    tumbler, _created = catalog_store_hook_tracked(
        title=title, doc_id=doc_id, collection_name=_COLLECTION,
    )
    write_note(
        catalog_doc_id=tumbler, collection=_COLLECTION, pieces=pieces, title=title,
        content_hash=note_content_hash(content, manifest_metadatas),
    )
    assert len(manifest_metadatas) == 1, "a short note is one chunk"
    return tumbler, manifest_metadatas[0]["chunk_text_hash"]


def _physical_ids(client) -> set[str]:
    """Every chunk row in the collection, owned or not."""
    out = client.get_collection(_COLLECTION).get(include=[], limit=300, include_non_live=True)
    return set(out.get("ids") or [])


def test_a_chunk_that_loses_its_owner_mid_run_is_skipped_and_the_rest_are_rewritten(
    t2_service_env,
):
    client = hvc.HttpVectorClient(tenant=t2_service_env)
    _t1, c1 = _put_note(client, title="z0o2p24-keep-1", content="first note that stays")
    _t2, c2_old = _put_note(client, title="z0o2p24-race", content="second note, about to be superseded")
    _t3, c3 = _put_note(client, title="z0o2p24-keep-2", content="third note that stays")

    real_upsert = client.upsert_chunks
    upserts: list[list[str]] = []
    superseded: dict[str, str] = {}

    def racing_upsert(collection, ids, documents, *args, **kwargs):
        upserts.append(list(ids))
        if not superseded:
            # Another process supersedes note 2 AFTER the command read its page and BEFORE the
            # write lands: the old chunk loses its only owner and is swept.
            _tumbler, new_chash = _put_note(
                client, title="z0o2p24-race", content="second note, now with different text",
            )
            superseded["new"] = new_chash
        return real_upsert(collection, ids, documents, *args, **kwargs)

    client.upsert_chunks = racing_upsert  # type: ignore[method-assign]
    hooks = _RecordingHooks()

    processed, skipped = _reembed_collection(
        client, _COLLECTION, _MODEL, dry_run=False, hooks=hooks,
    )

    assert sorted(upserts[0]) == sorted([c1, c2_old, c3]), "the first request carried the whole page"
    assert len(upserts) == 2, "one refusal, then one resend narrowed to the chunks still owned"
    assert sorted(upserts[1]) == sorted([c1, c3])
    assert (processed, skipped) == (2, 1)
    assert sorted(hooks.fired[-1]) == sorted([c1, c3]), "the chains fire for what was written only"
    live = set(client.get_collection(_COLLECTION).get(include=[], limit=300).get("ids") or [])
    assert live == {c1, c3, superseded["new"]}
    assert c2_old not in _physical_ids(client), "the superseded chunk was not resurrected ownerless"


def _refusal() -> VectorServiceError:
    return VectorServiceError(
        "POST /v1/vectors/upsert-chunks → HTTP 422: refusing an ownerless chunk write",
        code=422, reason=OWNERLESS_CHUNK_WRITE_REASON,
    )


def _fake_db(live_ids_per_read: list[list[str]], upsert_effects: list):
    """A db whose collection reads ``(ids, documents, metadatas)`` for a page, then answers each
    live re-read from *live_ids_per_read*, and whose ``upsert_chunks`` follows *upsert_effects*."""
    col = MagicMock()
    page = {
        "ids": ["a", "b", "c"], "documents": ["ta", "tb", "tc"],
        "metadatas": [{"x": 1}, {"x": 2}, {"x": 3}],
    }
    reads = iter(live_ids_per_read)

    def _get(ids=None, limit=None, offset=0, include=None, **_kw):
        if ids is None:
            return page if offset == 0 else {"ids": [], "documents": [], "metadatas": []}
        live = next(reads)
        keep = [i for i in ids if i in live]
        return {"ids": keep, "documents": [dict(zip(page["ids"], page["documents"]))[i] for i in keep]}

    col.count.return_value = 3
    col.get.side_effect = _get
    db = MagicMock()
    db.get_collection.return_value = col
    db.upsert_chunks.side_effect = upsert_effects
    return db


def test_a_refusal_for_another_reason_is_not_swallowed():
    other = VectorServiceError("POST /v1/vectors/upsert-chunks → HTTP 422: nope", code=422, reason="x")
    db = _fake_db([], [other])
    with pytest.raises(VectorServiceError):
        _reembed_collection(db, _COLLECTION, _MODEL, dry_run=False, hooks=_RecordingHooks())
    assert db.upsert_chunks.call_count == 1


def test_a_chunk_is_never_resent_once_a_re_read_says_it_lost_its_owner():
    db = _fake_db([["a", "c"]], [_refusal(), None])
    hooks = _RecordingHooks()
    processed, skipped = _reembed_collection(db, _COLLECTION, _MODEL, dry_run=False, hooks=hooks)
    sent = [c.args[1] for c in db.upsert_chunks.call_args_list]
    assert sent == [["a", "b", "c"], ["a", "c"]]
    assert (processed, skipped) == (2, 1)


def test_every_chunk_gone_means_nothing_is_resent():
    db = _fake_db([[]], [_refusal()])
    hooks = _RecordingHooks()
    processed, skipped = _reembed_collection(db, _COLLECTION, _MODEL, dry_run=False, hooks=hooks)
    assert db.upsert_chunks.call_count == 1
    assert (processed, skipped) == (0, 3)
    assert hooks.fired == []


def test_a_refusal_the_re_read_cannot_explain_is_raised_after_a_bounded_number_of_tries():
    # The engine keeps refusing chunks the live get still returns: no progress is possible, so the
    # command must fail loudly rather than loop.
    db = _fake_db([["a", "b", "c"]] * 10, [_refusal()] * 10)
    with pytest.raises(VectorServiceError):
        _reembed_collection(db, _COLLECTION, _MODEL, dry_run=False, hooks=_RecordingHooks())
    assert 2 <= db.upsert_chunks.call_count <= 5
