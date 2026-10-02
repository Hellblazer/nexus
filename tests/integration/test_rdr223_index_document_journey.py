# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.3 (nexus-z0o2p.13): ``doc_indexer._index_document`` writes chunks with their owner rows.

Drives the production function against the REAL engine substrate every unit test already boots
(``tests/conftest.py``'s autouse ``_pin_t2_substrate``); the jar must carry the Phase 1 routes
(``scripts/build-gate-jar.sh``). ``_index_document`` is what ``nx index md``, ``nx index rdr`` and
the DEVONthink markdown path all call.

The journeys (bead nexus-z0o2p.13 tests; RDR-223 Test Plan 8 for this path):

* a document that fits one request is exactly one ``write_manifest_many`` (sweep on, chunks) and
  makes no ``upsert-chunks`` call; its completion stamp is its own request after the post-store
  hooks (RDR-223 decision D2);
* a kill in a post-store hook leaves the document ``indexing``, and the rerun fires the hooks again;
* a document of several requests ends with the same manifest, chunks and stamp as one combined
  write;
* the client dies after its first write request: no chunk that request wrote is ownerless (the
  old path uploaded every chunk first and wrote the owner rows afterwards);
* a forced re-index of an unchanged document re-embeds nothing and sweeps nothing;
* a changed document drops what the new version dropped, after its last request;
* a request that fails fails the run, and the fence records it.

"Owner" means a ``catalog_document_chunks`` row of the document; "written by the run" means a chash
of the run's own chunks that the vector store holds.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db import http_vector_client as hvc
from tests._chunk_seed import seed_chunks_direct

pytestmark = [pytest.mark.integration]

_COLLECTION = "docs__z0o2p13-index__bge-base-en-v15-768__v1"
_CONTROL_COLLECTION = "docs__z0o2p13-control__bge-base-en-v15-768__v1"
_CATALOG_DATA_PATHS = ("/manifest/write_many", "/manifest/append")
_UPSERT_CHUNKS = "/v1/vectors/upsert-chunks"
_FENCE_BEGIN = "/index-run/begin"
_FENCE_COMPLETE = "/index-run/complete"


class ClientDied(BaseException):
    """The simulated death of the client process. A BaseException so that no ``except Exception``
    in the code under test can run cleanup, which a killed process does not do either."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lines(label: str, n: int, start: int = 0) -> list[str]:
    return [f"{label} line {i} z0o2p13 index-document journey unique text" for i in range(start, start + n)]


def _line_chunks(file_path: Path, content_hash: str, target_model: str, now_iso: str, corpus: str):
    """One chunk per non-blank line, shaped like ``_markdown_chunks``' output."""
    from nexus.metadata_schema import make_chunk_metadata

    prepared: list[tuple[str, str, dict]] = []
    pos = 0
    for line in file_path.read_text().splitlines():
        if not line.strip():
            continue
        h = _sha(line)
        meta = make_chunk_metadata(
            content_type="markdown", chunk_text_hash=h, content_hash=content_hash,
            chunk_start_char=pos, chunk_end_char=pos + len(line), indexed_at=now_iso,
            embedding_model=target_model, title="z0o2p13", tags="markdown", category="prose")
        pos += len(line) + 1
        prepared.append((h, line, meta))
    return prepared


def _write_file(tmp_path: Path, marker: str, lines: list[str]) -> Path:
    p = tmp_path / f"doc-{marker}.md"
    p.write_text("\n".join(lines) + "\n")
    return p.resolve()


def _register(path: Path, marker: str, collection: str = _COLLECTION) -> tuple[str, bool]:
    from nexus.doc_indexer import _register_or_lookup_doc_id

    doc_id, created = _register_or_lookup_doc_id(
        path, f"z0o2p13-{marker}", content_type="prose", physical_collection=collection,
        with_created=True)
    assert doc_id, "catalog registration must succeed against the real service"
    return doc_id, created


def _index(path: Path, marker: str, *, collection: str = _COLLECTION, force: bool = False,
           force_re_embed: bool = False, hooks=None) -> tuple[str, int]:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _index_document

    doc_id, created = _register(path, marker, collection)
    n = _index_document(
        path, f"z0o2p13-{marker}", _line_chunks, t3=HttpVectorClient(),
        collection_name=collection, force=force, force_re_embed=force_re_embed,
        doc_id=doc_id, doc_just_created=created, hooks=hooks)
    return doc_id, n


def _reader():
    from nexus.catalog.factory import make_catalog_reader

    return make_catalog_reader()


def _present(collection: str, chashes: list[str]) -> set[str]:
    return set(hvc.HttpVectorClient().existing_ids(collection, chashes))


def _manifest(doc_id: str) -> list[tuple[int, str]]:
    return [(r.position, r.chash) for r in _reader().get_manifest(doc_id)]


def _index_state(doc_id: str) -> str | None:
    entry = _reader().resolve(doc_id)
    assert entry is not None
    return entry.index_state


class _Traffic:
    """Every catalog POST (path, body, response) and every ``upsert-chunks`` POST, in order."""

    def __init__(self) -> None:
        self.catalog: list[tuple[str, dict, dict]] = []
        self.upserts: list[dict] = []
        self.writes = 0     # write requests seen: catalog data requests + upsert-chunks pages

    def data(self) -> list[tuple[str, dict, dict]]:
        return [t for t in self.catalog if t[0] in _CATALOG_DATA_PATHS]

    def total(self, key: str) -> int:
        return sum(int((resp or {}).get(key) or 0) for _, _, resp in self.data())


@contextmanager
def _traffic(*, die_after: int | None = None) -> Iterator[_Traffic]:
    """Record the traffic; with ``die_after=k`` the client "dies" at the (k+1)-th WRITE request
    (a catalog ``write_many``/``append``, or a page of ``upsert-chunks``), right after the k-th
    one completed."""
    t = _Traffic()
    orig_cat = HttpCatalogClient._post
    orig_vec = hvc._post

    def _gate() -> None:
        if die_after is not None and t.writes >= die_after:
            raise ClientDied(f"client died before write request {t.writes + 1}")
        t.writes += 1

    def cat_post(self, path, body=None, **kw):
        if path in _CATALOG_DATA_PATHS:
            _gate()
        resp = orig_cat(self, path, body, **kw)
        t.catalog.append((path, body or {}, resp if isinstance(resp, dict) else {}))
        return resp

    def vec_post(path, body, **kw):
        if path == _UPSERT_CHUNKS:
            _gate()
            t.upserts.append(body)
        return orig_vec(path, body, **kw)

    HttpCatalogClient._post = cat_post  # type: ignore[method-assign]
    hvc._post = vec_post  # type: ignore[assignment]
    try:
        yield t
    finally:
        HttpCatalogClient._post = orig_cat  # type: ignore[method-assign]
        hvc._post = orig_vec  # type: ignore[assignment]


# ── one request ───────────────────────────────────────────────────────────────


def test_a_document_that_fits_one_request_is_one_write_many_with_sweep_on_then_its_own_stamp(tmp_path) -> None:
    lines = _lines("single", 10)
    path = _write_file(tmp_path, "single", lines)
    with _traffic() as t:
        doc, n = _index(path, "single")

    assert n == 10
    data = t.data()
    assert [p for p, _, _ in data] == ["/manifest/write_many"], "exactly one data request"
    body = data[0][1]
    assert body["sweep"] is True
    assert not body.get("complete"), "the stamp does not ride the write (D2): it follows the hooks"
    assert len(body["chunks"]) == 10 and len(body["docs"][0]["rows"]) == 10
    # The chunk write is the manifest write: no separate vector upload, and the manifest hook did
    # not write the manifest a second time.
    assert t.upserts == []
    paths = [p for p, _, _ in t.catalog]
    assert paths.count("/manifest/write_many") == 1
    # The fence begins before the first byte of content lands, and the stamp is the last request.
    assert paths.index(_FENCE_BEGIN) < paths.index("/manifest/write_many")
    assert paths.count(_FENCE_COMPLETE) == 1 and paths[-1] == _FENCE_COMPLETE
    assert data[0][2]["embed_embedded"] == 10
    assert _manifest(doc) == [(i, _sha(line)) for i, line in enumerate(lines)]
    assert _present(_COLLECTION, [_sha(x) for x in lines]) == {_sha(x) for x in lines}
    assert _index_state(doc) == "complete"


# ── several requests ──────────────────────────────────────────────────────────


def test_a_document_larger_than_one_request_ends_with_the_same_manifest_as_one_combined_write(
    tmp_path,
) -> None:
    from nexus.db.http_vector_client import per_collection_chunk_cap
    from nexus.mcp_infra import get_catalog_writer

    cap = per_collection_chunk_cap(_COLLECTION)
    n_lines = 2 * cap + 8
    lines = _lines("multi", n_lines)
    path = _write_file(tmp_path, "multi", lines)

    # The control: the same content as ONE combined write, into a separate collection.
    ctl_path = _write_file(tmp_path, "multi-ctl", lines)
    ctl, _ = _register(ctl_path, "multi-ctl", _CONTROL_COLLECTION)
    rows = [{"chash": _sha(x), "position": i} for i, x in enumerate(lines)]
    chunks = [{"chash": _sha(x), "text": x, "metadata": {}} for x in lines]
    cat = get_catalog_writer()
    cat.write_manifest_many(
        [(ctl, rows)], chunks=chunks, sweep=True, collection=_CONTROL_COLLECTION,
        complete={ctl: "hash-ctl"})

    with _traffic() as t:
        doc, n = _index(path, "multi")

    assert n == n_lines
    data = t.data()
    assert len(data) == 3, "a document of 2*cap+8 chunks is three data requests"
    assert data[0][0] == "/manifest/write_many" and not data[0][1].get("sweep")
    assert "complete" not in data[0][1]
    assert [p for p, _, _ in data[1:]] == ["/manifest/append"] * 2
    paths = [p for p, _, _ in t.catalog]
    assert paths[-1] == _FENCE_COMPLETE
    assert paths.index(_FENCE_BEGIN) < paths.index("/manifest/write_many")
    assert t.upserts == []
    every = [_sha(x) for x in lines]
    assert _manifest(doc) == _manifest(ctl) == [(i, h) for i, h in enumerate(every)]
    assert _present(_COLLECTION, every) == set(every) == _present(_CONTROL_COLLECTION, every)
    assert _index_state(doc) == "complete" == _index_state(ctl)


# ── the client dies ───────────────────────────────────────────────────────────


def test_client_death_after_the_first_write_request_leaves_no_ownerless_chunk(tmp_path) -> None:
    from nexus.db.http_vector_client import per_collection_chunk_cap

    cap = per_collection_chunk_cap(_COLLECTION)
    lines = _lines("death", 2 * cap + 8)
    path = _write_file(tmp_path, "death", lines)
    every = [_sha(x) for x in lines]

    with _traffic(die_after=1) as t:
        with pytest.raises(ClientDied):
            _index(path, "death")
    doc, _ = _register(path, "death")

    written = _present(_COLLECTION, every)
    owners = {c for _, c in _manifest(doc)}
    assert written, "non-vacuity: the first request wrote chunks"
    assert len(written) < len(every), "and the run really was cut short"
    assert written <= owners, f"chunks without an owner: {len(written - owners)}"
    assert _index_state(doc) != "complete"
    assert t.writes == 1

    # The rerun completes the document and leaves the whole manifest.
    with _traffic():
        _index(path, "death")
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]
    assert _index_state(doc) == "complete"


# ── a kill in a post-store hook ───────────────────────────────────────────────


def test_a_kill_in_a_post_store_hook_leaves_the_document_indexing_and_the_rerun_fires_the_hooks_again(
    tmp_path,
) -> None:
    """RDR-223 decision D2. The stamp used to ride the write, so a process killed in a post-store
    hook (taxonomy assignment, aspect enqueue) left a document that read complete, and nothing
    would ever fire those hooks for it. The stamp is now the last request: the killed run leaves the
    fence ``indexing``, and the next run redoes the document and fires the hooks again."""
    from nexus.hook_registry import HookRegistry

    lines = _lines("hookkill", 10)
    path = _write_file(tmp_path, "hookkill", lines)
    every = [_sha(x) for x in lines]
    fired: list[str] = []
    armed = {"kill": True}

    def _batch_hook(*a, **kw):
        fired.append("batch")
        if armed["kill"]:
            raise ClientDied("killed in a post-store hook")

    hooks = HookRegistry()
    hooks.register_batch(_batch_hook)

    with pytest.raises(ClientDied):
        _index(path, "hookkill", hooks=hooks)
    doc, _ = _register(path, "hookkill")
    assert fired == ["batch"], "non-vacuity: the kill landed in the hook"
    assert _index_state(doc) == "indexing", "the stamp had not been sent"
    assert _present(_COLLECTION, every) == set(every), "every chunk had landed"

    armed["kill"] = False
    with _traffic() as rerun:
        _index(path, "hookkill", hooks=hooks)
    assert fired == ["batch", "batch"], "the rerun fired the hook again"
    assert [p for p, _, _ in rerun.data()] == ["/manifest/write_many"], "and redid the document"
    assert _index_state(doc) == "complete"
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]


def test_a_kill_in_the_markdown_catalog_enrichment_leaves_the_document_indexing_and_the_rerun_redoes_it(
    tmp_path, monkeypatch,
) -> None:
    """nexus-z0o2p.34: ``index_markdown`` registers the document's title, year and chunk count
    (``_catalog_markdown_hook``) after ``_index_document`` returns, which used to be after the
    stamp: a process killed there left a complete document the next run skipped as fresh. The stamp
    now follows the enrichment, so the kill leaves the fence ``indexing`` and the rerun redoes both."""
    import nexus.doc_indexer as di
    from nexus.db.http_vector_client import HttpVectorClient

    path = _write_file(tmp_path, "enrichkill", _lines("enrichkill", 10))
    calls: list[str] = []
    armed = {"kill": True}
    real = di._catalog_markdown_hook

    def _enrich(*a, **kw):
        calls.append("enrich")
        if armed["kill"]:
            raise ClientDied("killed in the markdown catalog enrichment")
        return real(*a, **kw)

    monkeypatch.setattr(di, "_catalog_markdown_hook", _enrich)

    def _run() -> int:
        return di.index_markdown(
            path, "z0o2p13-enrichkill", t3=HttpVectorClient(), collection_name=_COLLECTION)

    with pytest.raises(ClientDied):
        _run()
    doc, _ = _register(path, "enrichkill")
    assert calls == ["enrich"], "non-vacuity: the kill landed in the enrichment"
    assert _index_state(doc) == "indexing", "the stamp had not been sent"
    landed = _manifest(doc)
    assert landed, "non-vacuity: the write had landed"
    assert _present(_COLLECTION, [h for _, h in landed]) == {h for _, h in landed}, "every chunk had landed"

    armed["kill"] = False
    _run()
    assert calls == ["enrich", "enrich"], "the rerun redid the enrichment"
    assert _index_state(doc) == "complete"
    assert _manifest(doc) == landed


# ── re-indexing ───────────────────────────────────────────────────────────────


def test_forced_reindex_of_an_unchanged_document_embeds_nothing_and_sweeps_nothing(tmp_path) -> None:
    from nexus.db.http_vector_client import per_collection_chunk_cap

    cap = per_collection_chunk_cap(_COLLECTION)
    lines = _lines("unchanged", cap + 5)
    path = _write_file(tmp_path, "unchanged", lines)
    every = [_sha(x) for x in lines]

    with _traffic() as first:
        doc, _ = _index(path, "unchanged")
    assert first.total("embed_embedded") == len(every), "non-vacuity: the first run embedded them all"
    before = _manifest(doc)

    with _traffic() as again:
        _index(path, "unchanged", force=True)

    assert len(again.data()) == 2, "the forced re-index is a real write, not a staleness skip"
    assert again.total("embed_embedded") == 0
    assert again.total("embed_skipped") == len(every)
    assert again.total("swept") == 0
    assert _manifest(doc) == before
    assert _present(_COLLECTION, every) == set(every)
    assert _index_state(doc) == "complete"


def test_a_changed_document_drops_what_the_new_version_dropped_after_its_last_request(tmp_path) -> None:
    from nexus.db.http_vector_client import per_collection_chunk_cap

    cap = per_collection_chunk_cap(_COLLECTION)
    v1 = _lines("change", cap + 6)
    path = _write_file(tmp_path, "change", v1)
    doc, _ = _index(path, "change")
    v1_hashes = [_sha(x) for x in v1]
    assert _present(_COLLECTION, v1_hashes) == set(v1_hashes)

    # v2 keeps the first `cap` lines, replaces the tail and grows to three requests.
    kept = v1[:cap]
    v2 = kept + _lines("change-v2", cap + 4)
    _write_file(tmp_path, "change", v2)
    with _traffic() as t:
        _index(path, "change")
    v2_hashes = [_sha(x) for x in v2]
    dropped = [_sha(x) for x in v1[cap:]]

    assert len(t.data()) == 3
    assert _manifest(doc) == [(i, h) for i, h in enumerate(v2_hashes)]
    assert _present(_COLLECTION, v2_hashes) == set(v2_hashes)
    assert _present(_COLLECTION, dropped) == set(), "chunks only v1 owned are swept"
    assert t.total("swept") == len(dropped)
    assert _index_state(doc) == "complete"


# ── failure ───────────────────────────────────────────────────────────────────


def test_a_request_that_fails_fails_the_run_and_the_fence_records_it(tmp_path, monkeypatch) -> None:
    from nexus.db.http_vector_client import per_collection_chunk_cap

    cap = per_collection_chunk_cap(_COLLECTION)
    lines = _lines("fails", cap + 5)
    path = _write_file(tmp_path, "fails", lines)

    def boom(self, *a, **kw):
        raise RuntimeError("append refused by the test")

    monkeypatch.setattr(HttpCatalogClient, "append_manifest_chunks", boom)
    with pytest.raises(RuntimeError, match="append refused by the test"):
        _index(path, "fails")
    doc, _ = _register(path, "fails")
    assert _index_state(doc) == "failed"


def test_a_document_with_no_catalog_identity_fails_the_run_and_writes_nothing(tmp_path, monkeypatch) -> None:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _index_document
    from nexus.errors import CatalogIdentityMissingError

    lines = _lines("ownerless", 5)
    path = _write_file(tmp_path, "ownerless", lines)
    monkeypatch.setattr("nexus.doc_indexer._register_or_lookup_doc_id", lambda *a, **kw: "")

    with _traffic() as t:
        with pytest.raises(CatalogIdentityMissingError, match="no catalog document to own 5 chunk"):
            _index_document(
                path, "z0o2p13-ownerless", _line_chunks, t3=HttpVectorClient(),
                collection_name=_COLLECTION)

    assert t.writes == 0
    assert _present(_COLLECTION, [_sha(x) for x in lines]) == set()


# ── metadata: the write merges, like the old upsert did ───────────────────────


def _stored_metadata(collection: str, chashes: list[str], *, non_live: bool = False) -> dict[str, dict]:
    kw = {"include_non_live": True} if non_live else {}
    got = hvc.HttpVectorClient().get_collection(collection).get(ids=chashes, include=["metadatas"], **kw)
    assert len(got["ids"]) == len(chashes), "every chunk is stored"
    return dict(zip(got["ids"], got["metadatas"]))


@pytest.mark.parametrize("re_embed", [False, True], ids=["force", "force-re-embed"])
def test_forced_reindex_keeps_enrichment_and_clears_the_owned_keys_the_document_dropped(
    tmp_path, re_embed,
) -> None:
    """The combined routes REPLACE stored chunk metadata; the old upsert MERGED and preserved the
    ``bib_*`` enrichment (``nx enrich`` sets it), clearing only the keys the writer owns and
    dropped (``rewrite_delete_keys``). ``_index_document`` sends the merge mode, so a forced
    re-index keeps ``bib_year`` and clears a stale owned key. With ``--re-embed`` the same holds
    through the engine's insert branch, and every chunk is really re-embedded through the real
    writer (``force_re_embed`` reaches the engine)."""
    from nexus.db.http_vector_client import per_collection_chunk_cap

    cap = per_collection_chunk_cap(_COLLECTION)
    marker = f"enrich-{int(re_embed)}"
    lines = _lines(marker, cap + 3)
    path = _write_file(tmp_path, marker, lines)
    every = [_sha(x) for x in lines]
    _index(path, marker)

    # Another writer's enrichment, plus a stale value of a key the indexer owns and no longer sends.
    hvc.HttpVectorClient().update_chunks(
        _COLLECTION, every, [{"bib_year": 2020, "quality_gate_overridden": True} for _ in every])
    before = _stored_metadata(_COLLECTION, every)
    assert all(m["bib_year"] == 2020 and m["quality_gate_overridden"] is True for m in before.values())

    with _traffic() as t:
        _index(path, marker, force=True, force_re_embed=re_embed)

    after = _stored_metadata(_COLLECTION, every)
    assert all(m.get("bib_year") == 2020 for m in after.values()), "enrichment survived"
    assert all("quality_gate_overridden" not in m for m in after.values()), \
        "an owned key the write dropped is cleared"
    assert all(m["content_hash"] == before[c]["content_hash"] for c, m in after.items())
    sent = [b for p, b, _ in t.data() if "chunks" in b]
    assert sent, "non-vacuity: chunk-carrying requests were made"
    assert all(b["metadata_merge"] is True for b in sent)
    assert all("quality_gate_overridden" in b["metadata_delete_keys"] for b in sent)
    assert all("bib_year" not in b["metadata_delete_keys"] for b in sent)
    if re_embed:
        assert t.total("embed_embedded") == len(every), "--force --re-embed re-embeds every chunk"
        assert all(b["force_re_embed"] is True for b in sent)
    else:
        assert t.total("embed_embedded") == 0
        assert not any(b.get("force_re_embed") for b in sent)


def test_per_chunk_metadata_is_what_the_old_write_path_stored(tmp_path) -> None:
    """Read each chunk's stored metadata back and compare it to what the previous path
    (a plain ``upsert_chunks_with_embeddings`` into a control collection, the call the removed
    ``_upsert_skip_reembed`` made, rebuilt here with substrate SQL) stored for the same chunk;
    only the write time differs."""
    from datetime import UTC, datetime

    from nexus.corpus import index_model_for_collection
    from nexus.metadata_schema import rewrite_delete_keys

    lines = _lines("meta", 6)
    path = _write_file(tmp_path, "meta", lines)
    every = [_sha(x) for x in lines]
    _index(path, "meta")

    prepared = _line_chunks(
        path, "control-hash", index_model_for_collection(_CONTROL_COLLECTION),
        datetime.now(UTC).isoformat(), "z0o2p13-meta")
    # The same chunks, stored the way the old path stored a FIRST write (the stored row is the
    # metadata dict the client sent, merged into nothing), into a control collection. They carry
    # no owner row, which the write routes refuse from RDR-223 Phase 3, so they go in with
    # substrate SQL.
    metas = [dict(m) for _, _, m in prepared]
    # Non-vacuity, kept from the old path: the writer drops owned keys from its dict, so the
    # delete_keys the old route call named were non-empty. With a first write into an empty
    # collection they stripped nothing, which is why plain SQL is equivalent.
    assert rewrite_delete_keys(metas)
    seed_chunks_direct(
        _CONTROL_COLLECTION, [p[0] for p in prepared], [p[1] for p in prepared],
        metas, embed=True)

    new = _stored_metadata(_COLLECTION, every)
    # The old path stored the chunks with no owner row, which live(c) hides.
    old = _stored_metadata(_CONTROL_COLLECTION, every, non_live=True)
    volatile = {"indexed_at", "content_hash"}
    for chash in every:
        assert {k: v for k, v in new[chash].items() if k not in volatile} == \
            {k: v for k, v in old[chash].items() if k not in volatile}, chash
        assert new[chash]["indexed_at"], "the write time is stored"
        assert new[chash]["chunk_text_hash"] == chash
