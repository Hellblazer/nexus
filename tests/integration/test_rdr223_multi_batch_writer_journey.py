# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.0 (nexus-z0o2p.10): the multi-batch combined writer against the REAL engine.

Runs on the shared engine substrate every unit test already boots (``tests/conftest.py``'s autouse
``_pin_t2_substrate``); the jar must be built from a tree that carries the Phase 1 routes
(``scripts/build-gate-jar.sh``). Every write goes through ``get_catalog_writer()``, the closed
``CATALOG_WRITE_OPS`` proxy real callers use, so an op missing from the whitelist fails here.

The journeys (bead nexus-z0o2p.10 tests; RDR-223 Minimum Viable Validation, client side):

* a document of 1, 2 and 5 batches ends equal to one combined write;
* the client dies after request k: every chunk the run wrote has an owner row;
* the client dies after batch 1 of 3 on a previously complete document: not stamped complete, and
  a rerun rewrites it to the full manifest;
* an unchanged 5-batch document re-indexed: zero re-embeds, nothing swept;
* a drop list longer than 300 is swept through trailing sweep-only appends, and only after the
  last data append;
* a repeated position fails the writer's assert.

"Owner" means a ``catalog_document_chunks`` row of the document; "written by the run" means a chash
of the run's own rows that the vector store holds.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.multi_batch_write import (
    MultiBatchDocumentWriter,
    RepeatedPositionError,
    write_document,
)

pytestmark = [pytest.mark.integration]

_COLLECTION = "docs__z0o2p-writer__bge-base-en-v15-768__v1"
_CONTROL_COLLECTION = "docs__z0o2p-control__bge-base-en-v15-768__v1"
_DATA_PATHS = ("/manifest/write_many", "/manifest/append")


class ClientDied(Exception):
    """The simulated death of the client process between two requests."""


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _texts(label: str, start: int, n: int) -> list[str]:
    return [f"{label} chunk {i} z0o2p-writer-journey unique text" for i in range(start, start + n)]


def _batch(label: str, start: int, n: int) -> tuple[list[dict], list[dict]]:
    """``n`` chunks at positions ``start..start+n-1``; the chunk text is its own identity."""
    texts = _texts(label, start, n)
    rows = [{"chash": _chash(t), "position": start + i} for i, t in enumerate(texts)]
    chunks = [
        {"chash": _chash(t), "text": t, "metadata": {"position": start + i, "label": label}}
        for i, t in enumerate(texts)
    ]
    return rows, chunks


def _batches(label: str, sizes: list[int]) -> list[tuple[list[dict], list[dict]]]:
    out, start = [], 0
    for n in sizes:
        out.append(_batch(label, start, n))
        start += n
    return out


def _chashes(batches) -> list[str]:
    return [r["chash"] for rows, _ in batches for r in rows]


def _register(tmp_path: Path, marker: str, collection: str = _COLLECTION) -> str:
    from nexus.doc_indexer import _register_or_lookup_doc_id

    p = tmp_path / f"doc-{marker}.pdf"
    p.write_bytes(f"%PDF-1.4 z0o2p writer fake content [{marker}]\n".encode())
    doc_id = _register_or_lookup_doc_id(
        p.resolve(), f"z0o2p-{marker}", content_type="paper", physical_collection=collection)
    assert doc_id, "catalog registration must succeed against the real service"
    return doc_id


def _writer_proxy():
    from nexus.mcp_infra import get_catalog_writer

    return get_catalog_writer()


def _reader():
    from nexus.catalog.factory import make_catalog_reader

    return make_catalog_reader()


def _present(collection: str, chashes: list[str]) -> set[str]:
    from nexus.db.http_vector_client import HttpVectorClient

    return set(HttpVectorClient().existing_ids(collection, chashes))


def _manifest(doc_id: str) -> list[tuple[int, str]]:
    return [(r.position, r.chash) for r in _reader().get_manifest(doc_id)]


def _index_state(doc_id: str) -> str | None:
    entry = _reader().resolve(doc_id)
    assert entry is not None
    return entry.index_state


@contextmanager
def _traffic(*, die_after: int | None = None) -> Iterator[list[tuple[str, dict]]]:
    """Record every catalog POST; with ``die_after=k`` the client "dies" at the (k+1)-th DATA
    request (``write_many`` or ``append``), i.e. right after the k-th one completed."""
    log: list[tuple[str, dict]] = []
    orig = HttpCatalogClient._post
    data_seen = {"n": 0}

    def _post(self, path, body=None, **kw):
        if path in _DATA_PATHS:
            if die_after is not None and data_seen["n"] >= die_after:
                raise ClientDied(f"client died before data request {data_seen['n'] + 1}")
            data_seen["n"] += 1
        log.append((path, body or {}))
        return orig(self, path, body, **kw)

    HttpCatalogClient._post = _post  # type: ignore[method-assign]
    try:
        yield log
    finally:
        HttpCatalogClient._post = orig  # type: ignore[method-assign]


def _write_and_die_without_cleanup(cat, batches, *, doc_id, content_hash) -> None:
    """The document is written the way a client that is killed writes it: NO cleanup runs.
    ``write_document`` marks the fence failed on an exception, which a killed process never does,
    so a death test through it would pass for a reason that has nothing to do with the protocol."""
    w = MultiBatchDocumentWriter(
        cat, doc_id=doc_id, collection=_COLLECTION, content_hash=content_hash)
    for rows, chunks in batches:
        w.add_batch(rows, chunks)
    w.finish()


def _data_requests(log) -> list[tuple[str, dict]]:
    return [(p, b) for p, b in log if p in _DATA_PATHS]


# ── final state equals one combined write ─────────────────────────────────────


@pytest.mark.parametrize("sizes", [[10], [5, 5], [2, 2, 2, 2, 2]], ids=["1-batch", "2-batches", "5-batches"])
def test_multi_batch_final_state_equals_one_combined_write(tmp_path, sizes) -> None:
    cat = _writer_proxy()
    doc = _register(tmp_path, f"final-{len(sizes)}")
    ctl = _register(tmp_path, f"ctl-{len(sizes)}", _CONTROL_COLLECTION)
    batches = _batches("final", sizes)
    every = _chashes(batches)

    # The control: ONE combined write of the whole document in a separate collection.
    all_rows = [r for rows, _ in batches for r in rows]
    all_chunks = [c for _, chunks in batches for c in chunks]
    cat.write_manifest_many(
        [(ctl, all_rows)], chunks=all_chunks, sweep=True, collection=_CONTROL_COLLECTION,
        complete={ctl: "hash-ctl"})

    with _traffic() as log:
        res = write_document(cat, batches, doc_id=doc, collection=_COLLECTION,
                             content_hash="hash-final")

    assert _manifest(doc) == _manifest(ctl) != []
    assert len(_manifest(doc)) == 10
    assert _present(_COLLECTION, every) == set(every) == _present(_CONTROL_COLLECTION, every)
    assert _index_state(doc) == "complete" == _index_state(ctl)
    assert res.completed and res.batches == len(sizes) and res.distinct_chashes == 10
    data = _data_requests(log)
    assert len(data) == len(sizes)
    assert data[0][0] == "/manifest/write_many"
    assert [p for p, _ in data[1:]] == ["/manifest/append"] * (len(sizes) - 1)
    if len(sizes) == 1:
        assert data[0][1]["sweep"] is True and data[0][1]["complete"] == {doc: "hash-final"}
    else:
        assert not data[0][1].get("sweep") and "complete" not in data[0][1]
        # The stamp is a separate call, after the last append.
        assert [p for p, _ in log][-1] == "/index-run/complete"


# ── the client dies after request k ───────────────────────────────────────────


@pytest.mark.parametrize("k", [1, 2, 3, 4])
def test_client_death_after_request_k_leaves_no_ownerless_chunk(tmp_path, k) -> None:
    cat = _writer_proxy()
    doc = _register(tmp_path, f"death-{k}")
    # A previous, complete version: its chunks will be dropped by this run, and their sweep is
    # deferred to the last append, which a dead client never sends.
    old = _batches("old-version", [3, 3])
    write_document(cat, old, doc_id=doc, collection=_COLLECTION, content_hash="hash-old")
    assert _index_state(doc) == "complete"

    new = _batches("new-version", [2, 2, 2, 2, 2])
    with _traffic(die_after=k):
        with pytest.raises(ClientDied):
            _write_and_die_without_cleanup(cat, new, doc_id=doc, content_hash="hash-new")

    landed = _chashes(new)[: 2 * k]
    not_sent = _chashes(new)[2 * k:]
    owners = {c for _, c in _manifest(doc)}
    # Non-vacuity: the run really did land k batches...
    assert owners == set(landed) and len(landed) == 2 * k
    # ...and everything this run wrote that the store holds has an owner; the batches never sent
    # left no chunk behind.
    written = _present(_COLLECTION, _chashes(new))
    assert written == set(landed)
    assert written <= owners
    assert _present(_COLLECTION, not_sent) == set()
    # The previous version's chunks are the accepted leftovers: the sweep is deferred, never early.
    assert _present(_COLLECTION, _chashes(old)) == set(_chashes(old))
    assert _index_state(doc) != "complete"


def test_client_death_after_batch_one_of_three_is_not_stamped_and_a_rerun_replaces_it(tmp_path) -> None:
    cat = _writer_proxy()
    doc = _register(tmp_path, "death-rerun")
    old = _batches("rerun-old", [4])
    write_document(cat, old, doc_id=doc, collection=_COLLECTION, content_hash="hash-old")
    assert _index_state(doc) == "complete"

    new = _batches("rerun-new", [3, 3, 3])
    with _traffic(die_after=1):
        with pytest.raises(ClientDied):
            _write_and_die_without_cleanup(cat, new, doc_id=doc, content_hash="hash-new")
    # Batch 1 landed, the stamp did not: the next run must not skip this document.
    assert len(_manifest(doc)) == 3
    assert _index_state(doc) != "complete"

    res = write_document(cat, new, doc_id=doc, collection=_COLLECTION, content_hash="hash-new")
    assert res.completed
    assert [c for _, c in _manifest(doc)] == _chashes(new)
    assert len(_manifest(doc)) == 9
    assert _index_state(doc) == "complete"
    assert _present(_COLLECTION, _chashes(new)) == set(_chashes(new))


# ── an unchanged document re-indexed ──────────────────────────────────────────


def test_unchanged_five_batch_document_reindexed_embeds_nothing_and_sweeps_nothing(tmp_path) -> None:
    cat = _writer_proxy()
    doc = _register(tmp_path, "unchanged")
    batches = _batches("unchanged", [2, 2, 2, 2, 2])
    first = write_document(cat, batches, doc_id=doc, collection=_COLLECTION, content_hash="hash-u")
    assert first.embed_embedded == 10           # non-vacuity: the first run really embedded

    with _traffic() as log:
        again = write_document(cat, batches, doc_id=doc, collection=_COLLECTION,
                               content_hash="hash-u")

    assert again.embed_embedded == 0
    assert again.embed_skipped == 10
    assert again.swept == 0
    # Batch 1's drop list holds the other eight chashes; the writer subtracts what this run wrote,
    # so no sweep is sent, not even a sweep-only append.
    for _, body in _data_requests(log):
        assert not body.get("sweep_chashes")
    assert len(_data_requests(log)) == 5
    assert _present(_COLLECTION, _chashes(batches)) == set(_chashes(batches))
    assert [c for _, c in _manifest(doc)] == _chashes(batches)
    assert _index_state(doc) == "complete"


# ── the deferred sweep ────────────────────────────────────────────────────────


def test_dropped_chunks_are_swept_after_the_last_append_only(tmp_path) -> None:
    cat = _writer_proxy()
    doc = _register(tmp_path, "sweep-after")
    old = _batches("sweep-old", [3, 3])
    write_document(cat, old, doc_id=doc, collection=_COLLECTION, content_hash="hash-old")
    old_c = _chashes(old)

    new = _batches("sweep-new", [2, 2, 2])
    w = MultiBatchDocumentWriter(cat, doc_id=doc, collection=_COLLECTION, content_hash="hash-new")
    w.add_batch(*new[0])
    w.add_batch(*new[1])      # sends batch 1 (write_many, sweep off)
    w.add_batch(*new[2])      # sends batch 2 (append)
    # Two of three requests have landed and nothing old was swept.
    assert _present(_COLLECTION, old_c) == set(old_c)
    res = w.finish()          # the last append, carrying the sweep
    assert res.swept == 6
    assert _present(_COLLECTION, old_c) == set()
    assert _present(_COLLECTION, _chashes(new)) == set(_chashes(new))
    assert _index_state(doc) == "complete"


def test_drop_list_longer_than_300_is_swept_through_sweep_only_appends(tmp_path) -> None:
    cat = _writer_proxy()
    doc = _register(tmp_path, "sweep-long")
    old = _batches("long-old", [150, 155])              # 305 chunks
    write_document(cat, old, doc_id=doc, collection=_COLLECTION, content_hash="hash-old")
    old_c = _chashes(old)
    assert len(_present(_COLLECTION, old_c)) == 305

    new = _batches("long-new", [2, 2])
    with _traffic() as log:
        res = write_document(cat, new, doc_id=doc, collection=_COLLECTION,
                             content_hash="hash-new")
    appends = [b for p, b in log if p == "/manifest/append"]
    assert [len(b["sweep_chashes"]) for b in appends if b.get("sweep_chashes")] == [300, 5]
    # The sweep-only append is the LAST data-path request, after the data append.
    assert appends[-1]["rows"] == [] and "chunks" not in appends[-1]
    assert res.swept == 305
    assert _present(_COLLECTION, old_c) == set()
    assert _index_state(doc) == "complete"


# ── the position assert ───────────────────────────────────────────────────────


def test_a_repeated_position_fails_the_writer_before_the_request(tmp_path) -> None:
    cat = _writer_proxy()
    doc = _register(tmp_path, "repeat")
    w = MultiBatchDocumentWriter(cat, doc_id=doc, collection=_COLLECTION, content_hash="hash-r")
    w.add_batch(*_batch("repeat-a", 0, 2))
    with _traffic() as log:
        with pytest.raises(RepeatedPositionError):
            w.add_batch(*_batch("repeat-b", 1, 2))     # position 1 again, a different chash
    assert log == []
    assert _manifest(doc) == []


# ── the chunk cap ─────────────────────────────────────────────────────────────


def test_chunk_cap_env_override_above_300_never_puts_more_than_300_chunks_in_a_request(
    tmp_path, monkeypatch,
) -> None:
    """NX_ONNX_LOCAL_UPSERT_CHUNK_CAP has no upper bound and is read at import, so the override is
    applied to the module constant; the writer still clamps every combined-write request."""
    import nexus.db.http_vector_client as hvc

    monkeypatch.setattr(hvc, "_ONNX_LOCAL_UPSERT_CHUNK_CAP", 1000)
    monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda *a, **k: "onnx-local")
    assert hvc.per_collection_chunk_cap(_COLLECTION) == 1000

    cat = _writer_proxy()
    doc = _register(tmp_path, "cap-override")
    big = _batches("cap-override", [650])
    with _traffic() as log:
        res = write_document(cat, big, doc_id=doc, collection=_COLLECTION,
                             content_hash="hash-cap")
    sizes = [len(b.get("chunks") or []) for _, b in _data_requests(log)]
    assert sizes == [300, 300, 50]
    assert max(sizes) <= 300
    assert len(_manifest(doc)) == 650 and res.completed
    assert _present(_COLLECTION, _chashes(big)) == set(_chashes(big))
    assert _index_state(doc) == "complete"
