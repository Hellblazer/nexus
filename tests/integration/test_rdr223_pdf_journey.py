# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.1 / P2.5 (nexus-z0o2p.11, nexus-z0o2p.15): the PDF paths write chunks with their owner rows.

Three paths, each driven through ``doc_indexer.index_pdf`` against the REAL engine substrate every
unit test already boots (``tests/conftest.py``'s autouse ``_pin_t2_substrate``); the jar must carry
the Phase 1 routes (``scripts/build-gate-jar.sh``). Only the extractor and the chunker are faked, so
the text of every chunk is known; the pipeline buffer, the vector store, the catalog and the index
run fence are the engine's own.

* **streaming** (``streaming="always"``, the default route for a real PDF): ``pipeline_index_pdf``;
* **incremental** (more than ``_INCREMENTAL_THRESHOLD`` chunks, ``streaming="never"``):
  ``_index_pdf_incremental``;
* **small** (``streaming="never"``, few chunks): the all-at-once branch of ``index_pdf``.

The journeys (RDR-223 Test Plan 8 for each path, and the bead tests):

* the client dies after its first write request: no chunk that request wrote is ownerless (the old
  paths uploaded chunks first and wrote the owner rows afterwards);
* a full run ends with the manifest, chunks and stamp of one combined write of the document, and
  makes no ``upsert-chunks`` call;
* re-indexing an unchanged PDF re-embeds nothing and sweeps nothing;
* a forced re-index keeps the ``bib_*`` enrichment;
* the streaming run stamps the document complete only after its metadata post-pass;
* a streaming upload killed hard (no failure handler runs, the buffer keeps its flags) is finished
  by ONE rerun that re-sends the whole document from chunk 0 WITHOUT extracting the PDF again or
  embedding anything twice (the engine resets the buffer's upload flags, RDR-223 D1);
* a failure after the writer's first request leaves a freshly minted document and its chunks alone,
  and a request REFUSED before it wrote anything (a 422, a connect error) rolls the fresh
  registration back: no failed document with zero chunks;
* a refused completion stamp keeps the document and leaves the fence ``indexing``;
* a streaming run whose post-pass failed, or whose stamp failed, is finished by a rerun that sends
  only the stamp (no data request, no fence begin, no second extraction), after which the buffer goes;
* every non-streaming path stamps complete LAST: a kill in a post-store hook leaves the document
  ``indexing`` and the rerun fires the hooks again (RDR-223 D2);
* a dry run is confined to its throwaway store and sends the engine nothing, pipeline buffer included.

"Owner" means a ``catalog_document_chunks`` row of the document; "written by the run" means a chash
of the run's own chunks that the vector store holds.
"""
from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db import http_vector_client as hvc
from nexus.pdf_chunker import TextChunk
from nexus.pdf_extractor import ExtractionResult

pytestmark = [pytest.mark.integration]

_COLLECTION = "docs__z0o2p11-pdf__bge-base-en-v15-768__v1"
_CONTROL_COLLECTION = "docs__z0o2p11-control__bge-base-en-v15-768__v1"
_CATALOG_DATA_PATHS = ("/manifest/write_many", "/manifest/append")
_UPSERT_CHUNKS = "/v1/vectors/upsert-chunks"
_STORE_PUT = "/v1/vectors/store-put"
#: The two vector routes that write a chunk with no owner row. A PDF path must make no call to
#: either (the engine will refuse them); ``_Traffic.upserts`` collects both.
_OWNERLESS_CHUNK_WRITES = (_UPSERT_CHUNKS, _STORE_PUT)
_UPDATE_METADATA = "/v1/vectors/update-metadata"
_FENCE_BEGIN = "/index-run/begin"
_FENCE_COMPLETE = "/index-run/complete"


class ClientDied(BaseException):
    """The simulated death of the client process. A BaseException so that no ``except Exception``
    in the code under test can run cleanup, which a killed process does not do either."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lines(label: str, n: int, start: int = 0) -> list[str]:
    return [f"{label} page {i} z0o2p11 pdf journey unique text" for i in range(start, start + n)]


class _FakeExtractor:
    """One page per line; ``PDFExtractor().extract(...)``'s streaming and batch shapes."""

    lines: list[str] = []
    calls: list[str] = []

    def extract(self, pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None,
                allow_degraded=False):
        self.calls.append(str(pdf_path))
        pos, bounds = 0, []
        for i, line in enumerate(self.lines):
            if on_page:
                on_page(i, line, {"page_number": i + 1, "text_length": len(line)})
            bounds.append({"page_number": i + 1, "start_char": pos, "page_text_length": len(line) + 1})
            pos += len(line) + 1
        return ExtractionResult(
            text="\n".join(self.lines),
            metadata={"extraction_method": "docling", "page_count": len(self.lines),
                      "page_boundaries": bounds, "table_regions": [], "format": "markdown"})


class _FakeChunker:
    """One chunk per non-blank line of the text it is given."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def chunk(self, text, metadata):
        return [
            TextChunk(text=line, chunk_index=i, metadata={"page_number": i + 1})
            for i, line in enumerate(x for x in text.split("\n") if x.strip())
        ]


@pytest.fixture()
def fake_pdf(monkeypatch):
    """Give ``lines`` to the fake extractor and route both indexers' extraction through it."""

    def install(lines: list[str]) -> type[_FakeExtractor]:
        ex = type("Extractor", (_FakeExtractor,), {"lines": lines, "calls": []})
        for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
            monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
            monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
        return ex

    return install


def _write_pdf(tmp_path: Path, marker: str) -> Path:
    p = tmp_path / f"doc-{marker}.pdf"
    p.write_bytes(f"%PDF-1.4 z0o2p11 {marker}\n".encode())
    return p.resolve()


def _register(path: Path, marker: str, collection: str = _COLLECTION) -> tuple[str, bool]:
    from nexus.doc_indexer import _register_or_lookup_doc_id

    doc_id, created = _register_or_lookup_doc_id(
        path, f"z0o2p11-{marker}", content_type="paper", physical_collection=collection,
        with_created=True)
    assert doc_id, "catalog registration must succeed against the real service"
    return doc_id, created


def _index(path: Path, marker: str, *, streaming: str, collection: str = _COLLECTION,
           force: bool = False, force_re_embed: bool = False, hooks=None):
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_pdf

    return index_pdf(
        path, f"z0o2p11-{marker}", t3=HttpVectorClient(), collection_name=collection,
        streaming=streaming, force=force, force_re_embed=force_re_embed, hooks=hooks)


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
    """Every catalog POST (path, body, response), every vector POST path and every event in one
    ordered list, plus the count of write requests seen."""

    def __init__(self) -> None:
        self.catalog: list[tuple[str, dict, dict]] = []
        self.upserts: list[dict] = []     # bodies of upsert-chunks AND store-put requests
        self.events: list[str] = []
        self.writes = 0     # write requests seen: catalog data requests + upsert-chunks/store-put

    def data(self) -> list[tuple[str, dict, dict]]:
        return [t for t in self.catalog if t[0] in _CATALOG_DATA_PATHS]

    def total(self, key: str) -> int:
        return sum(int((resp or {}).get(key) or 0) for _, _, resp in self.data())


@contextmanager
def _traffic(
    *, die_after: int | None = None,
    error: "type[BaseException] | Callable[[str], BaseException]" = ClientDied,
) -> Iterator[_Traffic]:
    """Record the traffic; with ``die_after=k`` the client "dies" at the (k+1)-th WRITE request
    (a catalog ``write_many``/``append``, or a page of ``upsert-chunks``/``store-put``), right
    after the k-th one completed. *error* is called with the message and what it returns is
    raised: the default is a ``BaseException`` (a killed process, which runs no handler); an
    ``Exception`` class is a request that failed in a process that survives it; a function can
    build any shape, such as ``httpx.ConnectError`` (the request never reached the engine) or an
    ``httpx.HTTPStatusError`` with a 4xx (the engine refused it).

    Every ``/v1/pipeline`` request of an engine-backed client is recorded in ``events`` too
    (``"pipeline:<path>"``): the in-memory buffer of a dry run is not, because it never reaches an
    engine."""
    from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

    t = _Traffic()
    orig_cat = HttpCatalogClient._post
    orig_vec = hvc._post
    orig_store_post = RefreshableHttpStoreMixin._post
    orig_store_get = RefreshableHttpStoreMixin._get

    def _note_pipeline(self, path) -> None:
        if str(path).startswith("/v1/pipeline") and not getattr(self, "in_memory", False):
            t.events.append(f"pipeline:{path}")

    def store_post(self, path, *a, **kw):
        _note_pipeline(self, path)
        return orig_store_post(self, path, *a, **kw)

    def store_get(self, path, *a, **kw):
        _note_pipeline(self, path)
        return orig_store_get(self, path, *a, **kw)

    def _gate() -> None:
        if die_after is not None and t.writes >= die_after:
            raise error(f"client died before write request {t.writes + 1}")
        t.writes += 1

    def cat_post(self, path, body=None, **kw):
        if path in _CATALOG_DATA_PATHS:
            _gate()
        resp = orig_cat(self, path, body, **kw)
        t.catalog.append((path, body or {}, resp if isinstance(resp, dict) else {}))
        t.events.append(path)
        return resp

    def vec_post(path, body, **kw):
        if path in _OWNERLESS_CHUNK_WRITES:
            _gate()
            t.upserts.append(body)
        t.events.append(path)
        return orig_vec(path, body, **kw)

    HttpCatalogClient._post = cat_post  # type: ignore[method-assign]
    hvc._post = vec_post  # type: ignore[assignment]
    RefreshableHttpStoreMixin._post = store_post  # type: ignore[method-assign]
    RefreshableHttpStoreMixin._get = store_get  # type: ignore[method-assign]
    try:
        yield t
    finally:
        HttpCatalogClient._post = orig_cat  # type: ignore[method-assign]
        hvc._post = orig_vec  # type: ignore[assignment]
        RefreshableHttpStoreMixin._post = orig_store_post  # type: ignore[method-assign]
        RefreshableHttpStoreMixin._get = orig_store_get  # type: ignore[method-assign]


def _cap() -> int:
    from nexus.db.http_vector_client import per_collection_chunk_cap

    return per_collection_chunk_cap(_COLLECTION)


def _combined_control(tmp_path: Path, marker: str, lines: list[str]) -> str:
    """The same content as ONE combined write, into a separate collection."""
    from nexus.mcp_infra import get_catalog_writer

    ctl_path = _write_pdf(tmp_path, f"{marker}-ctl")
    ctl, _ = _register(ctl_path, f"{marker}-ctl", _CONTROL_COLLECTION)
    rows = [{"chash": _sha(x), "position": i} for i, x in enumerate(lines)]
    chunks = [{"chash": _sha(x), "text": x, "metadata": {}} for x in lines]
    get_catalog_writer().write_manifest_many(
        [(ctl, rows)], chunks=chunks, sweep=True, collection=_CONTROL_COLLECTION,
        complete={ctl: "hash-ctl"})
    return ctl


# The three paths, each with a chunk count that makes it multi-request under any chunk cap.
_PATHS = pytest.mark.parametrize(
    "streaming,label", [("always", "streaming"), ("never", "incremental")], ids=["streaming", "incremental"])


def _multi_lines(label: str) -> list[str]:
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD

    return _lines(label, max(2 * _cap() + 8, _INCREMENTAL_THRESHOLD + 30))


# ── a full run ────────────────────────────────────────────────────────────────


def test_a_small_pdf_is_one_write_many_with_sweep_on_then_its_own_stamp_and_makes_no_upsert(
    tmp_path, fake_pdf,
) -> None:
    lines = _lines("small", 10)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, "small")
    with _traffic() as t:
        n = _index(path, "small", streaming="never")

    assert n == 10
    data = t.data()
    assert [p for p, _, _ in data] == ["/manifest/write_many"], "exactly one data request"
    body = data[0][1]
    assert body["sweep"] is True and not body.get("complete"), "the stamp does not ride the write (D2)"
    assert len(body["chunks"]) == 10 and len(body["docs"][0]["rows"]) == 10
    assert t.upserts == [], "no separate chunk upload (neither upsert-chunks nor store-put)"
    write = t.events.index("/manifest/write_many")
    assert t.events.index(_FENCE_BEGIN) < write < t.events.index(_FENCE_COMPLETE)
    stamp = t.events.index(_FENCE_COMPLETE)
    assert t.events.count(_FENCE_COMPLETE) == 1 and not any(
        e in _CATALOG_DATA_PATHS for e in t.events[stamp:]), "no data request follows the stamp"
    doc, _ = _register(path, "small")
    assert _manifest(doc) == [(i, _sha(x)) for i, x in enumerate(lines)]
    assert _present(_COLLECTION, [_sha(x) for x in lines]) == {_sha(x) for x in lines}
    assert _index_state(doc) == "complete"


@_PATHS
def test_a_multi_request_pdf_ends_with_the_manifest_of_one_combined_write(
    tmp_path, fake_pdf, streaming, label,
) -> None:
    lines = _multi_lines(f"multi-{label}")
    fake_pdf(lines)
    path = _write_pdf(tmp_path, f"multi-{label}")
    ctl = _combined_control(tmp_path, f"multi-{label}", lines)

    with _traffic() as t:
        n = _index(path, f"multi-{label}", streaming=streaming)

    assert n == len(lines)
    data = t.data()
    assert len(data) >= 2, "several requests"
    assert data[0][0] == "/manifest/write_many" and not data[0][1].get("sweep")
    assert "complete" not in data[0][1], "batch 1 carries neither a sweep nor a stamp"
    assert all(p == "/manifest/append" for p, _, _ in data[1:])
    assert t.upserts == [], "no separate chunk upload (neither upsert-chunks nor store-put)"
    assert t.events.count(_FENCE_BEGIN) == 1
    assert t.events.index(_FENCE_BEGIN) < t.events.index("/manifest/write_many")
    every = [_sha(x) for x in lines]
    doc, _ = _register(path, f"multi-{label}")
    assert _manifest(doc) == _manifest(ctl) == [(i, h) for i, h in enumerate(every)]
    assert _present(_COLLECTION, every) == set(every) == _present(_CONTROL_COLLECTION, every)
    assert _index_state(doc) == "complete" == _index_state(ctl)


def test_the_streaming_run_stamps_complete_only_after_its_metadata_post_pass(tmp_path, fake_pdf) -> None:
    """The post-pass writes the title, author and extraction method after the last chunk landed. A
    process killed between a stamp and that pass would leave a document that looks complete and is
    missing them, so the stamp comes last."""
    lines = _multi_lines("stamp-order")
    fake_pdf(lines)
    path = _write_pdf(tmp_path, "stamp-order")
    with _traffic() as t:
        _index(path, "stamp-order", streaming="always")

    last_data = max(i for i, e in enumerate(t.events) if e in _CATALOG_DATA_PATHS)
    enrich = [i for i, e in enumerate(t.events) if e == _UPDATE_METADATA]
    stamp = [i for i, e in enumerate(t.events) if e == _FENCE_COMPLETE]
    assert enrich, "non-vacuity: the post-pass wrote metadata"
    assert len(stamp) == 1
    assert last_data < min(enrich) < stamp[0]
    doc, _ = _register(path, "stamp-order")
    stored = hvc.HttpVectorClient().get_collection(_COLLECTION).get(
        ids=[_sha(lines[0])], include=["metadatas"])["metadatas"][0]
    assert stored["extraction_method"] == "docling", "the post-pass ran before the document was stamped"
    assert _index_state(doc) == "complete"


# ── the client dies ───────────────────────────────────────────────────────────


def test_client_death_after_the_first_request_of_a_small_pdf_leaves_no_ownerless_chunk(
    tmp_path, fake_pdf,
) -> None:
    """One request writes the chunks and their owner rows together, so the client dying after the
    first request (its only one) has nothing ownerless to leave. The old branch uploaded the
    chunks and died writing the manifest."""
    lines = _lines("death-small", 10)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, "death-small")
    every = [_sha(x) for x in lines]

    with _traffic(die_after=1) as t:
        try:
            _index(path, "death-small", streaming="never")
        except ClientDied:
            pass
    doc, _ = _register(path, "death-small")

    written = _present(_COLLECTION, every)
    owners = {c for _, c in _manifest(doc)}
    assert written, "non-vacuity: the first request wrote chunks"
    assert written <= owners, f"chunks without an owner: {len(written - owners)}"
    assert t.writes == 1


@_PATHS
def test_client_death_after_the_first_request_leaves_no_ownerless_chunk(
    tmp_path, fake_pdf, monkeypatch, streaming, label,
) -> None:
    lines = _multi_lines(f"death-{label}")
    fake_pdf(lines)
    path = _write_pdf(tmp_path, f"death-{label}")
    every = [_sha(x) for x in lines]
    # A killed process runs no failure handling: not the fence's fail stamp, and not the heal
    # that rebuilds a failed run's manifest from its stored chunks (which would mask the very
    # ownerless chunks this asserts on).
    import nexus.doc_indexer as di

    real_fence_fail = di._fence_fail
    monkeypatch.setattr(di, "_fence_fail", lambda *a, **kw: None)

    with _traffic(die_after=1) as t:
        with pytest.raises(ClientDied):
            _index(path, f"death-{label}", streaming=streaming)
    doc, _ = _register(path, f"death-{label}")

    written = _present(_COLLECTION, every)
    owners = {c for _, c in _manifest(doc)}
    assert written, "non-vacuity: the first request wrote chunks"
    assert len(written) < len(every), "and the run really was cut short"
    assert written <= owners, f"chunks without an owner: {len(written - owners)}"
    assert _index_state(doc) != "complete"
    assert t.writes == 1

    # The rerun completes the document and leaves the whole manifest.
    monkeypatch.setattr(di, "_fence_fail", real_fence_fail)
    with _traffic():
        _index(path, f"death-{label}", streaming=streaming)
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]
    assert _index_state(doc) == "complete"


# ── re-indexing ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "streaming,size", [("always", "multi"), ("never", "multi"), ("never", "small")],
    ids=["streaming", "incremental", "small"])
def test_reindexing_an_unchanged_pdf_embeds_nothing_and_sweeps_nothing(
    tmp_path, fake_pdf, streaming, size,
) -> None:
    lines = _multi_lines(f"unchanged-{streaming}") if size == "multi" else _lines("unchanged-small", 12)
    fake_pdf(lines)
    marker = f"unchanged-{streaming}-{size}"
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]

    with _traffic() as first:
        _index(path, marker, streaming=streaming)
    assert first.total("embed_embedded") == len(every), "non-vacuity: the first run embedded them all"
    doc, _ = _register(path, marker)
    before = _manifest(doc)

    with _traffic() as again:
        _index(path, marker, streaming=streaming, force=True)

    assert len(again.data()) >= 1, "the forced re-index is a real write, not a staleness skip"
    assert again.total("embed_embedded") == 0
    assert again.total("embed_skipped") == len(every)
    assert again.total("swept") == 0
    assert again.upserts == []
    assert _manifest(doc) == before
    assert _present(_COLLECTION, every) == set(every)
    assert _index_state(doc) == "complete"


@_PATHS
def test_a_changed_pdf_drops_what_the_new_version_dropped_after_its_last_request(
    tmp_path, fake_pdf, streaming, label,
) -> None:
    v1 = _multi_lines(f"change-{label}")
    fake_pdf(v1)
    path = _write_pdf(tmp_path, f"change-{label}")
    _index(path, f"change-{label}", streaming=streaming)
    doc, _ = _register(path, f"change-{label}")
    v1_hashes = [_sha(x) for x in v1]

    keep = _cap()
    v2 = v1[:keep] + _lines(f"change-v2-{label}", len(v1) - keep - 5)
    fake_pdf(v2)
    path.write_bytes(b"%PDF-1.4 z0o2p11 change v2\n")     # a new content hash: the staleness gate opens
    with _traffic() as t:
        _index(path, f"change-{label}", streaming=streaming)
    v2_hashes = [_sha(x) for x in v2]
    dropped = [h for h in v1_hashes if h not in set(v2_hashes)]

    assert _manifest(doc) == [(i, h) for i, h in enumerate(v2_hashes)]
    assert _present(_COLLECTION, v2_hashes) == set(v2_hashes)
    assert _present(_COLLECTION, dropped) == set(), "chunks only v1 owned are swept"
    assert t.total("swept") == len(dropped)
    assert _index_state(doc) == "complete"


# ── metadata: the write merges, like the old upsert did ───────────────────────


def _stored_metadata(collection: str, chashes: list[str]) -> dict[str, dict]:
    got = hvc.HttpVectorClient().get_collection(collection).get(ids=chashes, include=["metadatas"])
    assert len(got["ids"]) == len(chashes), "every chunk is stored"
    return dict(zip(got["ids"], got["metadatas"]))


@pytest.mark.parametrize(
    "streaming,size", [("always", "multi"), ("never", "multi"), ("never", "small")],
    ids=["streaming", "incremental", "small"])
def test_a_forced_reindex_keeps_the_bib_enrichment(tmp_path, fake_pdf, streaming, size) -> None:
    """The combined routes REPLACE stored chunk metadata; the old upsert MERGED and preserved the
    ``bib_*`` enrichment ``nx enrich`` sets after indexing. Every PDF path sends the merge mode, so a
    forced re-index keeps ``bib_year``."""
    lines = _multi_lines(f"bib-{streaming}") if size == "multi" else _lines("bib-small", 12)
    fake_pdf(lines)
    marker = f"bib-{streaming}-{size}"
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]
    _index(path, marker, streaming=streaming)

    hvc.HttpVectorClient().update_chunks(_COLLECTION, every, [{"bib_year": 2020} for _ in every])
    before = _stored_metadata(_COLLECTION, every)
    assert all(m["bib_year"] == 2020 for m in before.values())

    with _traffic() as t:
        _index(path, marker, streaming=streaming, force=True)

    after = _stored_metadata(_COLLECTION, every)
    assert all(m.get("bib_year") == 2020 for m in after.values()), "the enrichment survived"
    sent = [b for p, b, _ in t.data() if "chunks" in b]
    assert sent, "non-vacuity: chunk-carrying requests were made"
    assert all(b["metadata_merge"] is True for b in sent)
    assert all("bib_year" not in (b.get("metadata_delete_keys") or []) for b in sent)


def test_a_forced_reindex_clears_the_owned_keys_a_non_streaming_pdf_dropped(tmp_path, fake_pdf) -> None:
    """The non-streaming paths hold the complete intended state of every row, so they name the keys
    they dropped (``rewrite_delete_keys``): a stale ``quality_gate_overridden`` from an earlier
    degraded run is cleared while ``bib_*`` survives."""
    lines = _lines("owned", 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, "owned")
    every = [_sha(x) for x in lines]
    _index(path, "owned", streaming="never")
    hvc.HttpVectorClient().update_chunks(
        _COLLECTION, every, [{"bib_year": 2021, "quality_gate_overridden": True} for _ in every])

    with _traffic() as t:
        _index(path, "owned", streaming="never", force=True)

    after = _stored_metadata(_COLLECTION, every)
    assert all(m.get("bib_year") == 2021 for m in after.values())
    assert all("quality_gate_overridden" not in m for m in after.values())
    sent = [b for p, b, _ in t.data() if "chunks" in b]
    assert all("quality_gate_overridden" in b["metadata_delete_keys"] for b in sent)


@pytest.mark.parametrize(
    "streaming,size", [("always", "multi"), ("never", "multi"), ("never", "small")],
    ids=["streaming", "incremental", "small"])
@pytest.mark.parametrize("re_embed", [False, True], ids=["force", "force-re-embed"])
def test_force_re_embed_reaches_the_engine_on_every_pdf_path(
    tmp_path, fake_pdf, streaming, size, re_embed,
) -> None:
    marker = f"reembed-{streaming}-{size}-{int(re_embed)}"
    lines = _multi_lines(marker) if size == "multi" else _lines(marker, 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    _index(path, marker, streaming=streaming)

    with _traffic() as t:
        _index(path, marker, streaming=streaming, force=True, force_re_embed=re_embed)

    sent = [b for p, b, _ in t.data() if "chunks" in b]
    assert sent
    if re_embed:
        assert t.total("embed_embedded") == len(lines), "--force --re-embed re-embeds every chunk"
        assert all(b["force_re_embed"] is True for b in sent)
    else:
        assert t.total("embed_embedded") == 0
        assert not any(b.get("force_re_embed") for b in sent)
    assert t.upserts == []


# ── no catalog identity ───────────────────────────────────────────────────────


@pytest.mark.parametrize("streaming", ["always", "never"])
def test_a_pdf_with_no_catalog_identity_fails_the_run_and_writes_nothing(
    tmp_path, fake_pdf, monkeypatch, streaming,
) -> None:
    from nexus.errors import CatalogIdentityMissingError

    lines = _lines(f"ownerless-{streaming}", 6)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, f"ownerless-{streaming}")
    monkeypatch.setattr("nexus.doc_indexer._register_or_lookup_doc_id", lambda *a, **kw: "")

    with _traffic() as t:
        with pytest.raises(CatalogIdentityMissingError, match="no catalog document to own"):
            _index(path, f"ownerless-{streaming}", streaming=streaming)

    assert t.writes == 0
    assert _present(_COLLECTION, [_sha(x) for x in lines]) == set()


# ── a streaming PDF of at most one batch ──────────────────────────────────────


def test_a_small_streaming_pdf_is_one_write_many_and_a_separate_stamp_after_the_post_pass(
    tmp_path, fake_pdf,
) -> None:
    """The common shape: at most 128 chunks, so the writer's one request is a ``write_many`` with
    the sweep on. Its completion is DEFERRED (the streaming run enriches metadata after the last
    chunk lands), so the ``write_many`` carries no stamp and the orchestrator sends
    ``complete_index_run`` afterwards, after the metadata post-pass."""
    lines = _lines("streamsmall", 10)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, "streamsmall")
    with _traffic() as t:
        n = _index(path, "streamsmall", streaming="always")

    assert n == 10
    data = t.data()
    assert [p for p, _, _ in data] == ["/manifest/write_many"], "exactly one data request"
    body = data[0][1]
    assert body["sweep"] is True
    assert not body.get("complete"), "the stamp is not riding the write: it is deferred"
    assert len(body["chunks"]) == 10 and len(body["docs"][0]["rows"]) == 10
    assert t.upserts == [], "no separate chunk upload"
    enrich = [i for i, e in enumerate(t.events) if e == _UPDATE_METADATA]
    stamp = [i for i, e in enumerate(t.events) if e == _FENCE_COMPLETE]
    write = t.events.index("/manifest/write_many")
    assert t.events.index(_FENCE_BEGIN) < write < min(enrich) < stamp[0] and len(stamp) == 1
    doc, _ = _register(path, "streamsmall")
    every = [_sha(x) for x in lines]
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]
    assert _present(_COLLECTION, every) == set(every)
    assert _index_state(doc) == "complete"


# ── a hard-killed streaming upload ────────────────────────────────────────────


def _buffer_evidence(path: Path) -> tuple[int, int]:
    """(chunk rows flagged uploaded, the progress counter) of *path*'s pipeline row, read through a
    fresh client the way the next process would find them."""
    from nexus.db.http_pipeline_client import HttpPipelineDB
    from nexus.doc_indexer import _sha256

    content_hash = _sha256(path)
    db = HttpPipelineDB()
    (row,) = [r for r in db._get("/v1/pipeline/list")["pipelines"]
              if r["content_hash"] == content_hash and r["pdf_path"] == str(path)]
    db._pipeline_ids[content_hash] = int(row["pipeline_id"])     # name the row, as create_pipeline would
    flagged = db.count_embedded_chunks(content_hash) - len(db.read_uploadable_chunks(content_hash))
    return flagged, int(row["chunks_uploaded"] or 0)


@pytest.mark.parametrize("lag", [False, True], ids=["counter-current", "counter-lagging"])
def test_a_hard_killed_streaming_upload_is_finished_by_one_rerun_without_extracting_again(
    tmp_path, fake_pdf, monkeypatch, lag,
) -> None:
    """A killed process runs no handler: the buffer keeps the head it flagged, the pipeline row is
    left behind, the fence stays ``indexing``. The next run finds a resumed row with chunks already
    sent and a writer whose state is gone. It must not send the remaining tail alone (that would
    replace the manifest with the tail and sweep the head): the engine resets the buffer's upload
    flags (keeping the pages, the chunks and their embeddings), the run re-sends the whole document
    from chunk 0 through a fresh writer, and that ONE rerun completes it. The PDF is not extracted
    again and no chunk is embedded twice.

    ``lag=True`` is the kill that lands between the flag and the buffered progress counter's
    flush, so the rows say the head went out and the counter says nothing did.

    The handlers that a survived failure runs are stubbed out (the failure fence and the buffer
    reset). The one thing the test does for the killed process is mark its pipeline row failed
    WITHOUT clearing the buffer: a killed process leaves its row ``running`` until the heartbeat
    goes stale, and a rerun inside that window is refused (409) rather than resumed; a stale row is
    resumed exactly as a failed one is."""
    import nexus.doc_indexer as di
    import nexus.pipeline_stages as ps
    from nexus.db.http_pipeline_client import HttpPipelineDB
    from tests._bounded_polling import bound_the_polling

    marker = f"hardkill-{int(lag)}"
    # Four streaming batches of 128, so the writer is mid-document when the kill lands. One batch
    # is ``r`` requests when the per-collection chunk cap is under 128, and the uploader flags a
    # batch only after the NEXT batch was handed over: batch 1 is flagged once 2r-1 requests went
    # out, so the kill is the 2r-th request (the writer sending batch 2's last part).
    lines = _lines(marker, 4 * 128 + 10)
    requests_per_batch = -(-128 // min(_cap(), 300))
    extractor = fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]

    def _row_left_behind_not_running(db, content_hash, first_exc):
        db.mark_failed(content_hash, error="killed")   # no clear_orphan_wal: the buffer survives
        return False

    with monkeypatch.context() as killed_process:
        killed_process.setattr(di, "_fence_fail", lambda *a, **k: None)
        killed_process.setattr(ps, "_mark_failed_and_reset_wal", _row_left_behind_not_running)
        if lag:
            real_progress = HttpPipelineDB.update_progress

            def _drop_the_uploaded_counter(self, content_hash, **fields):
                fields.pop("chunks_uploaded", None)
                if fields:
                    real_progress(self, content_hash, **fields)

            killed_process.setattr(HttpPipelineDB, "update_progress", _drop_the_uploaded_counter)
        with _traffic(die_after=2 * requests_per_batch - 1) as killed:
            with pytest.raises(ClientDied):
                _index(path, marker, streaming="always")

    doc, _ = _register(path, marker)
    written = _present(_COLLECTION, every)
    assert written and len(written) < len(every), "non-vacuity: the kill cut the upload short"
    assert written <= {c for _, c in _manifest(doc)}, "the head it wrote is owned"
    assert _index_state(doc) == "indexing"
    assert killed.writes == 2 * requests_per_batch - 1

    # Non-vacuity: what the kill left in the buffer is what the test claims. Some rows are flagged
    # uploaded; with lag=True the progress counter says none were, with lag=False it says some were.
    flagged, counter = _buffer_evidence(path)
    assert flagged > 0, "the killed run left flagged rows in the buffer"
    assert (counter == 0) if lag else (counter > 0), (flagged, counter)

    assert len(extractor.calls) == 1, "the killed run extracted the PDF once"

    # ONE rerun, no handler stubs. The pool's threads are non-daemon, so a stage that could never
    # finish would hold the whole pytest process open: bound the polling.
    with monkeypatch.context() as bounded:
        bound_the_polling(bounded)
        with _traffic() as rerun:
            n = _index(path, marker, streaming="always")

    assert n == len(every)
    assert len(extractor.calls) == 1, "the rerun did NOT extract the PDF again"
    assert "pipeline:/v1/pipeline/reset_uploaded" in rerun.events, "the engine reset the upload flags"
    assert "pipeline:/v1/pipeline/clear_wal" not in rerun.events, "the buffer was kept"
    data = rerun.data()
    assert data[0][0] == "/manifest/write_many", "the rerun starts the document over"
    assert data[0][1]["docs"][0]["rows"][0]["position"] == 0, "never a tail alone"
    assert rerun.upserts == []
    # Nothing is embedded twice: the chunks the killed run wrote are skipped, the rest embedded once.
    assert rerun.total("embed_skipped") == len(written)
    assert rerun.total("embed_embedded") == len(every) - len(written)
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]
    assert _present(_COLLECTION, every) == set(every)
    assert {c for _, c in _manifest(doc)} >= _present(_COLLECTION, every), "no ownerless chunk"
    assert _index_state(doc) == "complete"


# ── a failure after the writer's first request ────────────────────────────────


@_PATHS
def test_a_failed_second_request_leaves_the_freshly_minted_document_and_its_chunks(
    tmp_path, fake_pdf, streaming, label,
) -> None:
    """A request that FAILS in a process that survives it (an ``Exception``, not a kill) after the
    first request landed: the document was minted by this very call, and rolling it back would
    tombstone it and hide the chunks the first request wrote and owns. It is left, marked failed
    for the next run to redo."""
    marker = f"fail2-{label}"
    lines = _multi_lines(marker)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]

    with _traffic(die_after=1, error=RuntimeError) as t:
        with pytest.raises(RuntimeError, match="died before write request 2"):
            _index(path, marker, streaming=streaming)

    assert t.writes == 1
    doc, created = _register(path, marker)
    assert not created, "the document the failed call minted is still there"
    entry = _reader().resolve(doc)
    assert entry is not None
    written = _present(_COLLECTION, every)
    assert written and len(written) < len(every), "non-vacuity: request 1 landed and only it"
    assert written <= {c for _, c in _manifest(doc)}, "and its chunks are readable through their owner rows"
    assert _index_state(doc) == "failed"

    with _traffic():
        _index(path, marker, streaming=streaming)
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]
    assert _index_state(doc) == "complete"


@_PATHS
def test_a_failed_stamp_after_the_last_chunk_leaves_the_document_and_is_not_success(
    tmp_path, fake_pdf, monkeypatch, streaming, label,
) -> None:
    """Every chunk landed and the completion stamp's request fails. The chunks are written and
    owned, so the freshly minted document is NOT rolled back; the error surfaces; the document is
    not complete (the streaming run leaves the fence ``indexing``, the incremental one marks it
    failed), and a rerun finishes it."""
    marker = f"stampfail-{label}"
    lines = _multi_lines(marker)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]

    def _stamp_transport_down(self, *args, **kwargs):
        raise RuntimeError("stamp transport down")

    with monkeypatch.context() as down:
        down.setattr(HttpCatalogClient, "complete_index_run", _stamp_transport_down)
        with pytest.raises(RuntimeError, match="stamp transport down"):
            _index(path, marker, streaming=streaming)

    doc, created = _register(path, marker)
    assert not created, "the document was not rolled back"
    assert _reader().resolve(doc) is not None
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)], "every chunk landed and is owned"
    assert _present(_COLLECTION, every) == set(every)
    state = _index_state(doc)
    assert state != "complete"
    if streaming == "always":
        assert state == "indexing", "the fence is left as begin left it"

    with _traffic() as rerun:
        _index(path, marker, streaming=streaming)
    assert _index_state(doc) == "complete"
    if streaming == "always":
        # The buffer outlived the failed stamp, so the rerun sends ONLY the stamp.
        assert rerun.data() == [], "no data request"
        assert rerun.events.count(_FENCE_BEGIN) == 0 and rerun.events.count(_FENCE_COMPLETE) == 1
    else:
        assert rerun.data(), "the non-streaming path has no buffer: it redoes the document"


# ── a dry run ─────────────────────────────────────────────────────────────────


def test_a_dry_run_with_no_throwaway_store_is_refused_and_writes_nothing_to_the_engine(
    tmp_path, fake_pdf,
) -> None:
    """``index_pdf(dry_run=True)`` given no ``t3`` resolves the ENGINE's client in service mode. It
    used to preview into it with ownerless ``upsert-chunks`` requests; it is refused before it
    registers a collection or reads through the client."""
    from nexus.doc_indexer import index_pdf
    from nexus.errors import DryRunStoreError

    fake_pdf(_lines("dry-engine", 10))
    path = _write_pdf(tmp_path, "dry-engine")
    with _traffic() as t:
        with pytest.raises(DryRunStoreError, match="in-memory"):
            index_pdf(path, "z0o2p11-dry-engine", t3=None, collection_name=_COLLECTION,
                      dry_run=True, streaming="never")
    assert t.events == [] and t.catalog == [], "the engine saw nothing"
    assert t.upserts == []


@pytest.mark.parametrize("streaming,size", [("always", "multi"), ("never", "multi"), ("never", "small")],
                         ids=["streaming", "incremental", "small"])
def test_a_dry_run_into_a_throwaway_store_previews_and_sends_the_engine_nothing(
    tmp_path, fake_pdf, streaming, size,
) -> None:
    """The positive control, as ``nx index pdf --dry-run`` builds it: an in-memory store, no hooks,
    no embedding. The chunks land in the throwaway store, and no vector or catalog request reaches
    the engine."""
    from unittest.mock import MagicMock

    from nexus.db import make_t3
    from nexus.db.inmemory_vector_store import InMemoryVectorClient
    from nexus.doc_indexer import index_pdf
    from nexus.hook_registry import HookRegistry

    marker = f"dry-store-{streaming}-{size}"
    lines = _multi_lines(marker) if size == "multi" else _lines(marker, 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    store = make_t3(_client=InMemoryVectorClient(), _ef_override=MagicMock())
    with _traffic() as t:
        n = index_pdf(
            path, f"z0o2p11-{marker}", t3=store, collection_name=_COLLECTION, streaming=streaming,
            embed_fn=lambda texts, model: ([[] for _ in texts], model), hooks=HookRegistry(),
            dry_run=True)
    assert n == len(lines)
    assert t.events == [] and t.catalog == [], "the engine saw nothing, pipeline routes included"
    got = store.get_or_create_collection(_COLLECTION).get(ids=[_sha(lines[0])])
    assert got["ids"] == [_sha(lines[0])], "the preview landed in the throwaway store"


def test_a_dry_run_never_touches_the_row_of_a_real_run_of_the_same_bytes(tmp_path, fake_pdf) -> None:
    """The pipeline buffer is keyed on the file's bytes, so a dry run that used the engine's would
    share (and could flag, reset or delete) the row of a real run in flight. It builds its own
    in-memory buffer: a real run's half-finished row for the same file is exactly as it was."""
    from unittest.mock import MagicMock

    from nexus.db import make_t3
    from nexus.db.inmemory_vector_store import InMemoryVectorClient
    from nexus.doc_indexer import index_pdf
    from nexus.hook_registry import HookRegistry

    marker = "dry-shares-nothing"
    lines = _lines(marker, 4 * 128 + 10)     # four streaming batches: the kill below lands mid-document
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    # A real run killed part way leaves a row with flagged chunks (as the hard-kill journey shows).
    with pytest.MonkeyPatch.context() as killed:
        import nexus.doc_indexer as di
        import nexus.pipeline_stages as ps

        killed.setattr(di, "_fence_fail", lambda *a, **k: None)
        killed.setattr(ps, "_mark_failed_and_reset_wal",
                       lambda db, content_hash, first_exc: (db.mark_failed(content_hash, error="killed"), False)[1])
        requests_per_batch = -(-128 // min(_cap(), 300))
        with _traffic(die_after=2 * requests_per_batch - 1):
            with pytest.raises(ClientDied):
                _index(path, marker, streaming="always")
    flagged_before, counter_before = _buffer_evidence(path)
    assert flagged_before > 0, "non-vacuity: a real run's row with flagged chunks exists"

    store = make_t3(_client=InMemoryVectorClient(), _ef_override=MagicMock())
    with _traffic() as t:
        n = index_pdf(
            path, f"z0o2p11-{marker}", t3=store, collection_name=_COLLECTION, streaming="always",
            embed_fn=lambda texts, model: ([[] for _ in texts], model), hooks=HookRegistry(),
            dry_run=True)
    assert n == len(lines)
    assert t.events == [], "the dry run sent the engine nothing at all"
    assert _buffer_evidence(path) == (flagged_before, counter_before), "the real run's row is untouched"


# ── a chunk the first request dropped and a later request re-adds ──────────────


@_PATHS
def test_a_chunk_dropped_by_the_first_request_and_re_added_by_a_later_one_is_not_swept(
    tmp_path, fake_pdf, streaming, label,
) -> None:
    """The first request REPLACES the manifest, so every chunk of the previous version that it does
    not carry is "dropped" from the manifest at that moment. The sweep therefore runs after the
    LAST request, over (previous manifest minus everything this run wrote): a chunk that batch 1
    dropped and a later batch put back must survive. v2 = 128 fresh chunks, then v1's tail."""
    v1 = _multi_lines(f"readd-{label}")
    fake_pdf(v1)
    path = _write_pdf(tmp_path, f"readd-{label}")
    _index(path, f"readd-{label}", streaming=streaming)
    doc, _ = _register(path, f"readd-{label}")

    head = 128
    assert len(v1) > head + 5, "non-vacuity: v1 has a tail past the first request"
    v2 = _lines(f"readd-fresh-{label}", head) + v1[head:]
    fake_pdf(v2)
    path.write_bytes(b"%PDF-1.4 z0o2p11 readd v2\n")     # a new content hash: the staleness gate opens
    with _traffic() as t:
        _index(path, f"readd-{label}", streaming=streaming)

    v2_hashes = [_sha(x) for x in v2]
    gone = [_sha(x) for x in v1[:head]]
    assert _manifest(doc) == [(i, h) for i, h in enumerate(v2_hashes)]
    assert _present(_COLLECTION, v2_hashes) == set(v2_hashes), "the re-added tail is still stored"
    assert _present(_COLLECTION, gone) == set(), "and what only v1's head owned is swept"
    assert t.total("swept") == len(gone)
    assert _index_state(doc) == "complete"


# ── hooks and the stamp ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "streaming,size", [("always", "multi"), ("never", "multi"), ("never", "small")],
    ids=["streaming", "incremental", "small"])
def test_a_post_store_hook_that_raises_never_fails_a_stamped_document(
    tmp_path, fake_pdf, streaming, size,
) -> None:
    """Order, per path: streaming fires each batch's hooks once its request is sent and stamps LAST
    (after the post-pass and the document hooks); the incremental and small paths, like
    ``_index_document``, stamp with the write and fire the hooks after it. The second order is safe
    because ``HookRegistry`` contains a hook's ``Exception`` (logged, recorded, never raised), so a
    hook cannot turn the stamped document into a failed one. This pins that: a hook of every grain
    raises, and the document ends ``complete`` with the run reporting success."""
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_pdf
    from nexus.hook_registry import HookRegistry

    marker = f"hookraise-{streaming}-{size}"
    lines = _multi_lines(marker) if size == "multi" else _lines(marker, 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)

    def _boom(*args, **kwargs):
        raise RuntimeError("hook exploded")

    hooks = HookRegistry()
    hooks.register_batch(_boom)
    hooks.register_single(_boom)
    hooks.register_document(_boom)

    n = index_pdf(path, f"z0o2p11-{marker}", t3=HttpVectorClient(), collection_name=_COLLECTION,
                  streaming=streaming, hooks=hooks)

    assert n == len(lines)
    doc, _ = _register(path, marker)
    assert _index_state(doc) == "complete"
    assert [c for _, c in _manifest(doc)] == [_sha(x) for x in lines]


# ── a streaming run with nothing to write ─────────────────────────────────────


def test_a_zero_chunk_streaming_run_marks_the_document_failed_without_a_begin(tmp_path, fake_pdf) -> None:
    """The zero-chunk failure calls ``_fence_fail`` with no writer, so no ``begin`` ever ran. What
    the engine does with that: it stamps ``failed`` on the document whatever its state was (a
    ``complete`` document from an earlier version becomes ``failed``), and touches nothing else,
    so the earlier manifest and its chunks stay readable. No data request is sent."""
    v1 = _lines("zero-v1", 10)
    fake_pdf(v1)
    path = _write_pdf(tmp_path, "zero")
    _index(path, "zero", streaming="always")
    doc, _ = _register(path, "zero")
    before = _manifest(doc)
    assert _index_state(doc) == "complete" and len(before) == 10

    fake_pdf([])
    path.write_bytes(b"%PDF-1.4 z0o2p11 zero v2\n")
    with _traffic() as t:
        try:
            _index(path, "zero", streaming="always")
        except Exception:      # the CLI-level outcome is not the subject; the fence and manifest are
            pass

    assert t.data() == [], "nothing was written"
    assert _index_state(doc) == "failed"
    assert _manifest(doc) == before, "the earlier version stays as it was"
    assert _present(_COLLECTION, [c for _, c in before]) == {c for _, c in before}


@_PATHS
def test_a_failed_pdf_write_does_not_heal_the_manifest_from_stored_chunks(
    tmp_path, fake_pdf, monkeypatch, streaming, label,
) -> None:
    """``_fence_fail`` used to rebuild a failed run's manifest from the chunks it stored
    (``_heal_failed_document``, for paths that wrote chunks BEFORE their owner rows). The PDF
    writer's manifest is the record and every chunk it sent is owned, so the rebuild had nothing to
    do and, on a failed re-index, would replace the manifest with a fragment found by the OLD
    content hash. It is retired (nexus-z0o2p.35); this pins that nothing rebuilds a manifest."""
    import nexus.catalog.manifest_heal as mh
    import nexus.doc_indexer as di

    marker = f"noheal-{label}"
    lines = _multi_lines(marker)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    healed: list[str] = []
    assert not hasattr(di, "_heal_failed_document")
    monkeypatch.setattr(mh, "heal_manifest_gaps", lambda *a, **k: healed.append(a))

    with _traffic(die_after=1, error=RuntimeError):
        with pytest.raises(RuntimeError, match="died before write request 2"):
            _index(path, marker, streaming=streaming)

    assert healed == []
    doc, _ = _register(path, marker)
    assert _index_state(doc) == "failed"


# ── a request REFUSED before it wrote anything rolls the fresh registration back ─────────────


def _status_error(code: int) -> Callable[[str], BaseException]:
    req = httpx.Request("POST", "http://engine/v1/catalog/manifest/write_many")

    def make(message: str) -> BaseException:
        return httpx.HTTPStatusError(message, request=req, response=httpx.Response(code, request=req))

    return make


_REFUSED_FIRST_REQUEST = {
    "422": _status_error(422),
    "connect-error": lambda message: httpx.ConnectError(message),
}


@pytest.fixture()
def minted(monkeypatch):
    """The catalog document ids this call minted, as ``index_pdf`` saw them (id, created)."""
    import nexus.doc_indexer as di

    seen: list[tuple[str, bool]] = []
    real = di._register_or_lookup_doc_id

    def _recording(*a, with_created=False, **kw):
        out = real(*a, with_created=with_created, **kw)
        if with_created and isinstance(out, tuple):
            seen.append(out)
        return out

    monkeypatch.setattr(di, "_register_or_lookup_doc_id", _recording)
    return seen


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """A connect error is retried with a 2s/4s/8s backoff (``nexus.retry``); the journeys that
    refuse a request with one do not need to wait it out. Only that module's clock is replaced."""
    import nexus.retry as retry

    class _NoBackoff:
        def sleep(self, seconds: float) -> None:
            return None

        def __getattr__(self, name: str):
            return getattr(time, name)

    monkeypatch.setattr(retry, "time", _NoBackoff())


_THREE_PATHS = pytest.mark.parametrize(
    "streaming,size", [("always", "multi"), ("never", "multi"), ("never", "small")],
    ids=["streaming", "incremental", "small"])


@_THREE_PATHS
@pytest.mark.parametrize("refusal", sorted(_REFUSED_FIRST_REQUEST))
def test_a_first_request_the_engine_or_the_network_refused_rolls_the_fresh_document_back(
    tmp_path, fake_pdf, minted, streaming, size, refusal,
) -> None:
    """The writer's first request never wrote anything (a 422 the engine answered, a connection that
    was never made), so the document this very call minted is a phantom: no chunk, no owner row. It
    is rolled back. The registration used to be kept because the write reported "started" BEFORE the
    request was sent, which left a failed document with zero chunks."""
    marker = f"refused1-{refusal}-{streaming}-{size}"
    lines = _multi_lines(marker) if size == "multi" else _lines(marker, 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]

    with _traffic(die_after=0, error=_REFUSED_FIRST_REQUEST[refusal]) as t:
        with pytest.raises((httpx.HTTPStatusError, httpx.ConnectError)):
            _index(path, marker, streaming=streaming)

    assert t.writes == 0, "no write request reached the engine"
    assert minted[0][1] is True, "non-vacuity: this call minted the document"
    doc_id = minted[0][0]
    assert {d for d, _ in minted} == {doc_id}, "one document"
    assert _reader().resolve(doc_id) is None, "the phantom registration was rolled back"
    assert _present(_COLLECTION, every) == set(), "and nothing was written"


@_THREE_PATHS
def test_a_first_request_that_failed_in_a_way_that_leaves_its_outcome_open_keeps_the_document(
    tmp_path, fake_pdf, minted, streaming, size,
) -> None:
    """The control for the test above: a 500 may have landed, so the fresh document is KEPT (marked
    failed for the next run to redo) rather than tombstoned over chunks that may be stored."""
    marker = f"refused1-500-{streaming}-{size}"
    lines = _multi_lines(marker) if size == "multi" else _lines(marker, 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)

    with _traffic(die_after=0, error=_status_error(500)):
        with pytest.raises(httpx.HTTPStatusError):
            _index(path, marker, streaming=streaming)

    assert minted[0][1] is True and {d for d, _ in minted} == {minted[0][0]}
    entry = _reader().resolve(minted[0][0])
    assert entry is not None, "the document is kept"
    assert _index_state(minted[0][0]) == "failed"


@contextmanager
def _committed_then_lost(second: Callable[[], BaseException]) -> Iterator[None]:
    """Inside ``_traffic``: every chunk-carrying catalog request COMMITS on the engine, its response
    is reset (``ReadError``), and the refreshable client's single retry, made inside its ``except``
    block, fails with *second()*. Only the second error propagates, with the first as its
    ``__context__``: the shape a supervisor restart mid-``write_many`` produces."""
    wrapped = HttpCatalogClient._post

    def lost(self, path, body=None, **kw):
        if path not in _CATALOG_DATA_PATHS:
            return wrapped(self, path, body, **kw)
        wrapped(self, path, body, **kw)
        try:
            raise httpx.ReadError("connection reset after the commit")
        except httpx.ReadError:
            raise second()

    HttpCatalogClient._post = lost  # type: ignore[method-assign]
    try:
        yield
    finally:
        HttpCatalogClient._post = wrapped  # type: ignore[method-assign]


@_THREE_PATHS
@pytest.mark.parametrize("retry_error", ["connect-error", "401"])
def test_a_request_that_committed_and_whose_retry_failed_cleanly_keeps_the_fresh_document(
    tmp_path, fake_pdf, minted, streaming, size, retry_error,
) -> None:
    """The engine commits the writer's first request, the response is reset, and the client's one
    retry is refused (a connect error while the service restarts, or a 401). Only the retry's error
    reaches the writer; judged alone it reads "wrote nothing", so the rollback deletes a document
    whose chunks (with owner rows) are stored and leaves them ownerless. The document is kept."""
    marker = f"committed-lost-{retry_error}-{streaming}-{size}"
    lines = _multi_lines(marker) if size == "multi" else _lines(marker, 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    make = {"connect-error": lambda: httpx.ConnectError("refused on the retry"),
            "401": lambda: _status_error(401)("retry refused")}[retry_error]

    with _traffic():
        with _committed_then_lost(make):
            with pytest.raises((httpx.HTTPStatusError, httpx.ConnectError)):
                _index(path, marker, streaming=streaming)

    assert minted[0][1] is True, "non-vacuity: this call minted the document"
    doc_id = minted[0][0]
    assert {d for d, _ in minted} == {doc_id}, "one document"
    assert _reader().resolve(doc_id) is not None, "the document is kept"
    assert _manifest(doc_id), "with the manifest the committed request wrote"
    assert _present(_COLLECTION, [_sha(x) for x in lines]), "and its chunks"


# ── a refused completion stamp ───────────────────────────────────────────────────────────


@_THREE_PATHS
def test_a_refused_stamp_keeps_the_fresh_document_and_its_chunks_and_leaves_the_fence_indexing(
    tmp_path, fake_pdf, monkeypatch, minted, streaming, size,
) -> None:
    """The engine refuses the completion stamp (it counts the manifest rows and finds a different
    number than the stamp names). Every request of the write had already landed, so the document
    this call minted has its chunks with owner rows: it is KEPT, not rolled back, the refusal
    propagates, and the fence is left ``indexing`` (a refusal is not a failure of the run; the next
    run redoes the document). The refusal is REAL: the stamp is sent with a row count off by one."""
    from nexus.errors import IndexRunVerifyRefused

    marker = f"refusedstamp-{streaming}-{size}"
    lines = _multi_lines(marker) if size == "multi" else _lines(marker, 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]
    real_complete = HttpCatalogClient.complete_index_run

    def _off_by_one(self, doc_id, content_hash, chunk_count, *a, **kw):
        return real_complete(self, doc_id, content_hash, chunk_count + 1, *a, **kw)

    with monkeypatch.context() as refused:
        refused.setattr(HttpCatalogClient, "complete_index_run", _off_by_one)
        with _traffic() as t:
            with pytest.raises(IndexRunVerifyRefused):
                _index(path, marker, streaming=streaming)

    assert minted[0][1] is True, "non-vacuity: this call minted the document"
    doc_id = minted[0][0]
    assert {d for d, _ in minted} == {doc_id}, "one document"
    assert _reader().resolve(doc_id) is not None, "the document is kept"
    assert _manifest(doc_id) == [(i, h) for i, h in enumerate(every)], "with every chunk owned"
    assert _present(_COLLECTION, every) == set(every)
    assert _index_state(doc_id) == "indexing", "the fence is left as begin left it"

    # The rerun (a real stamp) completes it.
    with _traffic():
        _index(path, marker, streaming=streaming)
    assert _index_state(doc_id) == "complete"


# ── a streaming run whose post-pass failed: the rerun sends only the stamp ──────────────


def _pipeline_rows(path: Path) -> list[dict]:
    from nexus.db.http_pipeline_client import HttpPipelineDB
    from nexus.doc_indexer import _sha256

    content_hash = _sha256(path)
    return [r for r in HttpPipelineDB()._get("/v1/pipeline/list")["pipelines"]
            if r["content_hash"] == content_hash and r["pdf_path"] == str(path)]


def test_a_failed_post_pass_is_finished_by_a_rerun_that_sends_only_the_stamp(
    tmp_path, fake_pdf, monkeypatch,
) -> None:
    """Every chunk was uploaded and the metadata post-pass failed: the run leaves the document
    ``indexing`` and the pipeline buffer (all chunks flagged) in place. The rerun is a genuine TAIL:
    it extracts nothing, begins no fence, sends no data request, and stamps through
    ``complete_index_run``; the buffer is deleted only after that stamp landed."""
    import nexus.pipeline_stages as ps

    marker = "tail"
    lines = _multi_lines(marker)
    extractor = fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]

    with monkeypatch.context() as failing:
        failing.setattr(ps, "_enrich_metadata_from_extraction", lambda *a, **kw: False)
        with _traffic() as first:
            n = _index(path, marker, streaming="always")
    assert n == len(every)
    doc, _ = _register(path, marker)
    assert _index_state(doc) == "indexing", "the failed post-pass left the document unstamped"
    assert first.events.count(_FENCE_COMPLETE) == 0
    (row,) = _pipeline_rows(path)
    flagged, counter = _buffer_evidence(path)
    assert flagged == len(every) and counter == len(every), "a genuine tail: every chunk is flagged"
    assert row["status"] == "failed", "handed back so the rerun resumes it"
    assert len(extractor.calls) == 1

    with _traffic() as rerun:
        assert _index(path, marker, streaming="always") == len(every)

    assert len(extractor.calls) == 1, "the rerun extracted nothing"
    assert rerun.data() == [], "no data request"
    assert rerun.events.count(_FENCE_BEGIN) == 0, "no fence begin"
    assert rerun.events.count(_FENCE_COMPLETE) == 1, "one stamp, through complete_index_run"
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]
    assert _index_state(doc) == "complete"
    assert _pipeline_rows(path) == [], "the buffer goes after the stamp landed"


# ── decision D2: the stamp is the last thing a non-streaming path does ───────────────────


@pytest.mark.parametrize(
    "streaming,size", [("always", "multi"), ("never", "multi"), ("never", "small")],
    ids=["streaming", "incremental", "small"])
def test_a_kill_in_a_post_store_hook_leaves_the_document_indexing_and_the_rerun_fires_the_hooks_again(
    tmp_path, fake_pdf, streaming, size,
) -> None:
    """A process killed in a post-store hook (the document-grain hook runs after the last write on
    every path) must leave a document that is NOT stamped complete, so the next run redoes it and the
    hook (taxonomy assignment, aspect enqueue) runs again. With the stamp riding the write it did
    not: the document read complete and nothing would ever fire the hooks for it."""
    from nexus.hook_registry import HookRegistry

    marker = f"hookkill-{streaming}-{size}"
    lines = _multi_lines(marker) if size == "multi" else _lines(marker, 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]
    fired: list[str] = []
    killed = {"armed": True}

    def _document_hook(*a, **kw):
        fired.append("document")
        if killed["armed"]:
            raise ClientDied("killed in the document-grain hook")

    hooks = HookRegistry()
    hooks.register_document(_document_hook)

    with pytest.raises(ClientDied):
        _index(path, marker, streaming=streaming, hooks=hooks)
    doc, _ = _register(path, marker)
    assert fired == ["document"], "non-vacuity: the kill landed in the hook"
    assert _index_state(doc) == "indexing", "the stamp had not been sent"
    assert _present(_COLLECTION, every) == set(every), "every chunk had landed"

    killed["armed"] = False
    with _traffic():
        _index(path, marker, streaming=streaming, hooks=hooks)
    assert fired == ["document", "document"], "the rerun fired the hook again"
    assert _index_state(doc) == "complete"
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]


def test_a_kill_in_the_small_pdfs_catalog_enrichment_leaves_the_document_indexing_and_the_rerun_redoes_it(
    tmp_path, fake_pdf, monkeypatch,
) -> None:
    """nexus-z0o2p.34: the small PDF's catalog enrichment (title, author, year, chunk count) ran AFTER
    the stamp, so a process killed there left a complete document with a stem title that the next
    run skipped as fresh. It now runs ahead of the stamp: a kill in it leaves the fence ``indexing``
    with every chunk landed, and the rerun redoes the document and its enrichment."""
    import nexus.pipeline_stages as stages

    marker = "enrichkill-small"
    lines = _lines(marker, 12)
    fake_pdf(lines)
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]
    calls: list[str] = []
    killed = {"armed": True}
    real = stages._catalog_pdf_hook

    def _enrich(*a, **kw):
        calls.append("enrich")
        if killed["armed"]:
            raise ClientDied("killed in the catalog enrichment")
        return real(*a, **kw)

    monkeypatch.setattr(stages, "_catalog_pdf_hook", _enrich)

    with pytest.raises(ClientDied):
        _index(path, marker, streaming="never")
    doc, _ = _register(path, marker)
    assert calls == ["enrich"], "non-vacuity: the kill landed in the enrichment"
    assert _index_state(doc) == "indexing", "the stamp had not been sent"
    assert _present(_COLLECTION, every) == set(every), "every chunk had landed"

    killed["armed"] = False
    _index(path, marker, streaming="never")
    assert calls == ["enrich", "enrich"], "the rerun redid the enrichment"
    assert _index_state(doc) == "complete"
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]
