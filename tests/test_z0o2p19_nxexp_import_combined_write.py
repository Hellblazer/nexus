# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z0o2p.19 (RDR-223 P2.9, client): the ``.nxexp`` import writes every chunk together with its
owner row through the catalog manifest routes, carrying the exported vectors.

Real engine substrate (``t2_service_env``): the properties under test (a chunk never lands without an
owner, the vectors stay byte-identical, a document reads ``complete``) live on the engine side of the
wire and a mocked client cannot stand in for them.

* Test Plan 9 (RDR-223): a multi-page file with scattered positions imported this way. Vectors
  byte-identical to the export, the engine embeds nothing, scattered positions end in manifest order,
  and a second import leaves the documents as they are (keep-existing, nexus-wbfpw.40).
* Test Plan 8 (RDR-223): the client dies after the first request, and after page 3. No chunk that was
  written is without an owner, documents whose last page landed are already ``complete``, and a rerun
  finishes the rest (a document this file left ``indexing`` or ``failed`` is resumed with the append
  form; any other document that owns chunks stays kept).
* The import makes no call to ``/v1/vectors/upsert-chunks`` (bead requirement 8).
* Sam's rulings of 2026-09-30: the exported vector replaces a stored one and the mismatches are
  reported; ``taxonomy__*`` is refused up front.
* A refused completion stamp leaves the fence ``indexing`` (RDR-223 client rule).
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import msgpack
import numpy as np
import pytest
from click.testing import CliRunner

import nexus.catalog.http_catalog_client as hcc
import nexus.db.http_vector_client as hvc
import nexus.exporter as exporter_mod
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.multi_document_write import MultiDocumentImportWriter
from nexus.corpus import index_model_for_collection
from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.limits import QUOTAS
from nexus.errors import BatchWriteFailedError, NexusError
from nexus.exporter import _file_sha256, export_collection, import_collection
from tests._chunk_seed import seed_chunks_direct

_MODEL = "bge-base-en-v15-768"
_DIM = 768
_PAGE = 7
_DATA_PATHS = ("/manifest/write_many", "/manifest/append", "/manifest/append_many")


def _coll(name: str) -> str:
    return f"code__z0o2p19-{name}__{_MODEL}__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _vec(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(_DIM).astype(np.float32)


def _write_nxexp(path: Path, collection: str, records: list[dict], *, model: str = _MODEL) -> None:
    header = {
        "format_version": 1, "collection_name": collection,
        "database_type": collection.split("__")[0], "embedding_model": model,
        "record_count": len(records), "embedding_dim": _DIM,
        "exported_at": "2026-01-01T00:00:00+00:00", "pipeline_version": "nexus-1",
    }
    with open(path, "wb") as f:
        f.write(json.dumps(header).encode() + b"\n")
        with gzip.GzipFile(fileobj=f, mode="wb") as gz:
            for r in records:
                gz.write(msgpack.packb(r, use_bin_type=True))


def _read_nxexp(path: Path) -> list[dict]:
    with open(path, "rb") as f:
        f.readline()
        with gzip.GzipFile(fileobj=f) as gz:
            return list(msgpack.Unpacker(gz, raw=False))


def _shaped_file(collection: str, docs: int = 8, seed: int = 19):
    """A multi-page file: several documents of 1 to 7 chunks each, positions scattered (not
    contiguous, with gaps), every record carrying an ``owner`` and a vector that is NOT what the
    engine's embedder would produce (so a re-embed cannot pass for a passthrough), records in chash
    order so a document's chunks land on different pages.

    Returns ``(records, expected)``; ``expected`` maps a document's source_uri to its chashes in
    position order."""
    rng = np.random.default_rng(seed)
    records: list[dict] = []
    expected: dict[str, list[str]] = {}
    for d in range(docs):
        uri = f"file:///z0o2p19/{collection}/mod{d}.py"
        rows: list[tuple[int, str]] = []
        for k in range(1 + (d % 6) + (1 if d >= 6 else 0)):
            text = f"z0o2p19 module {d} chunk {k} body"
            chash = _chash(text)
            position = k * 3 + (d % 2)
            records.append({
                "id": chash, "document": text,
                "metadata": {"chunk_text_hash": chash, "source_path": f"/z0o2p19/mod{d}.py"},
                "embedding": rng.standard_normal(_DIM).astype(np.float32).tobytes(),
                "owner": {"source_uri": uri, "title": f"mod{d}.py", "content_type": "code",
                          "position": position},
            })
            rows.append((position, chash))
        expected[uri] = [c for _, c in sorted(rows)]
    records.sort(key=lambda r: r["id"])
    return records, expected


def _page_of_each_doc(records: list[dict], page: int = _PAGE) -> dict[str, tuple[int, int]]:
    """``uri -> (first page, last page)`` of a document's records, pages of *page* records."""
    out: dict[str, tuple[int, int]] = {}
    for i, r in enumerate(records):
        u = r["owner"]["source_uri"]
        lo, hi = out.get(u, (i // page, i // page))
        out[u] = (min(lo, i // page), max(hi, i // page))
    return out


@pytest.fixture
def small_pages(monkeypatch):
    monkeypatch.setattr(
        "nexus.exporter.QUOTAS", dataclasses.replace(QUOTAS, MAX_RECORDS_PER_WRITE=_PAGE))


def _record_combined_writes(monkeypatch) -> list[tuple[str, dict, list[str]]]:
    """``(route, result, doc ids)`` of every combined write (the engine's counters live in the result)."""
    seen: list[tuple[str, dict, list[str]]] = []
    for name in ("write_manifest_many", "append_manifest_many"):
        real = getattr(hcc.HttpCatalogClient, name)

        def _wrap(self, docs, *a, _real=real, _name=name, **kw):
            out = _real(self, docs, *a, **kw)
            seen.append((_name, out, [d for d, _ in docs]))
            return out

        monkeypatch.setattr(hcc.HttpCatalogClient, name, _wrap)
    return seen


def _manifest(reader, doc_id: str) -> list[tuple[int, str]]:
    return [(r.position, r.chash) for r in reader.get_manifest(doc_id)]


def _stored_vectors(client, tmp_path: Path, coll: str) -> dict[str, bytes]:
    out = tmp_path / f"stored-{coll[-12:]}-{len(list(tmp_path.iterdir()))}.nxexp"
    export_collection(db=client, collection_name=coll, output_path=out)
    return {r["id"]: r["embedding"] for r in _read_nxexp(out)}


class _ClientDied(Exception):
    """The simulated death of the client."""


def _die_at_data_request(monkeypatch, n: int) -> None:
    """Raise :class:`_ClientDied` at the (n+1)-th data request (catalog or vector write)."""
    seen = {"n": 0}

    def _tick(path: str) -> None:
        seen["n"] += 1
        if seen["n"] > n:
            raise _ClientDied(f"client died before data request {seen['n']}")

    real_cat_post = hcc.HttpCatalogClient._post
    real_vec_post = hvc._post

    def _cat_post(self, path, body=None, **kw):
        if path in _DATA_PATHS:
            _tick(path)
        return real_cat_post(self, path, body, **kw)

    def _vec_post(path, body, **kw):
        if path.endswith("/upsert-chunks"):
            _tick(path)
        return real_vec_post(path, body, **kw)

    monkeypatch.setattr(hcc.HttpCatalogClient, "_post", _cat_post)
    monkeypatch.setattr(hvc, "_post", _vec_post)


def _die_at_page(monkeypatch, n: int) -> None:
    """Raise :class:`_ClientDied` when the import starts its (n+1)-th page: pages 1..n completed."""
    real = exporter_mod._OwnerImport.flush
    seen = {"n": 0}

    def _flush(self, page):
        seen["n"] += 1
        if seen["n"] > n:
            raise _ClientDied(f"client died before page {seen['n']}")
        return real(self, page)

    monkeypatch.setattr(exporter_mod._OwnerImport, "flush", _flush)


# ── Test Plan 9 ─────────────────────────────────────────────────────────────


def test_scattered_multi_page_import_keeps_vectors_positions_and_completes_every_document(
    t2_service_env, tmp_path, small_pages, monkeypatch,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("shape")
    records, expected = _shaped_file(dst)
    f = tmp_path / "shape.nxexp"
    _write_nxexp(f, dst, records)
    spans = _page_of_each_doc(records)
    assert any(lo != hi for lo, hi in spans.values()), "the fixture must span pages"
    writes = _record_combined_writes(monkeypatch)

    result = import_collection(db=client, input_path=f, target_collection=dst)

    assert (result["imported_count"], result["owned_count"], result["skipped_count"]) == (
        len(records), len(records), 0), result
    # The engine embedded nothing and stored every supplied vector.
    assert writes, "the import must go through the combined routes"
    assert sum(int(r.get("embed_embedded") or 0) for _, r, _ in writes) == 0
    assert sum(int(r.get("vectors_supplied") or 0) for _, r, _ in writes) == len(records)
    assert result["vector_mismatches"] == 0
    # Vectors byte-identical to the file.
    assert {r["id"]: r["embedding"] for r in records} == _stored_vectors(client, tmp_path, dst)
    # Scattered positions end in manifest order, and every document reads complete.
    for uri, chashes in expected.items():
        entry = reader.by_source_uri(uri)
        assert entry is not None, uri
        rows = _manifest(reader, str(entry.tumbler))
        assert [c for _, c in rows] == chashes, uri
        positions = [p for p, _ in rows]
        assert positions == sorted(positions) and len(set(positions)) == len(positions)
        assert entry.index_state == "complete", (uri, entry.index_state)

    # A second import leaves the documents as they are: keep-existing skips every record.
    n_writes = len(writes)
    again = import_collection(db=client, input_path=f, target_collection=dst)
    assert again["imported_count"] == 0 and again["skipped_count"] == len(records), again
    assert again["owned_count"] == len(records) and again["unowned_count"] == 0, again
    assert len(writes) == n_writes, "a second import must write nothing"
    for uri, chashes in expected.items():
        entry = reader.by_source_uri(uri)
        assert [c for _, c in _manifest(reader, str(entry.tumbler))] == chashes


def test_each_document_is_swept_and_stamped_on_its_own_last_page_not_at_the_end(
    t2_service_env, tmp_path, small_pages, monkeypatch,
):
    """The prepass tells the importer each document's record count, so the stamp rides that
    document's last request. Observed from outside: a record of the stream's LAST page is still to
    come, yet a document whose records all came earlier is already complete."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("lastpage")
    records, expected = _shaped_file(dst)
    f = tmp_path / "lastpage.nxexp"
    _write_nxexp(f, dst, records)
    spans = _page_of_each_doc(records)
    last_page = max(hi for _, hi in spans.values())
    early = [u for u, (_, hi) in spans.items() if hi < last_page]
    assert early and any(hi == last_page for _, hi in spans.values()), "non-vacuity"

    _die_at_page(monkeypatch, last_page)          # pages 0..last_page-1 complete, then the client dies
    with pytest.raises(_ClientDied):
        import_collection(db=client, input_path=f, target_collection=dst)
    for uri in early:
        assert reader.by_source_uri(uri).index_state == "complete", uri
    for uri, (_, hi) in spans.items():
        if hi == last_page:
            assert reader.by_source_uri(uri).index_state != "complete", uri


def test_import_makes_no_upsert_chunks_call(t2_service_env, tmp_path, small_pages, monkeypatch):
    client = HttpVectorClient(tenant=t2_service_env)
    dst = _coll("noupsert")
    records, _ = _shaped_file(dst, docs=4)
    f = tmp_path / "noupsert.nxexp"
    _write_nxexp(f, dst, records)
    vector_paths: list[str] = []
    catalog_paths: list[str] = []
    real_vec_post = hvc._post
    real_cat_post = hcc.HttpCatalogClient._post

    def _vec_spy(path, body, **kw):
        vector_paths.append(path)
        return real_vec_post(path, body, **kw)

    def _cat_spy(self, path, body=None, **kw):
        catalog_paths.append(path)
        return real_cat_post(self, path, body, **kw)

    monkeypatch.setattr(hvc, "_post", _vec_spy)
    monkeypatch.setattr(hcc.HttpCatalogClient, "_post", _cat_spy)
    import_collection(db=client, input_path=f, target_collection=dst)
    assert not [p for p in vector_paths if p.endswith("/upsert-chunks")], vector_paths
    assert set(catalog_paths) & set(_DATA_PATHS), (
        "the spies saw no combined write: they are not watching the import")


# ── Test Plan 8, and resume ─────────────────────────────────────────────────


@pytest.mark.parametrize("dies_after", ["first request", "page 3"])
@pytest.mark.parametrize("fence", ["failed", "indexing"])
def test_client_death_leaves_no_ownerless_chunk_and_a_rerun_finishes_the_documents(
    t2_service_env, tmp_path, small_pages, monkeypatch, dies_after, fence,
):
    """``fence``: a Python exception makes the importer mark its open documents ``failed``; a SIGKILL
    leaves them ``indexing``. The rerun resumes both."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll(f"death-{dies_after.split()[0]}-{fence}")
    records, expected = _shaped_file(dst)
    f = tmp_path / "death.nxexp"
    _write_nxexp(f, dst, records)
    all_chashes = [r["id"] for r in records]
    spans = _page_of_each_doc(records)
    if fence == "indexing":
        monkeypatch.setattr(MultiDocumentImportWriter, "abort", lambda self, error: None)

    with monkeypatch.context() as m:
        if dies_after == "page 3":
            _die_at_page(m, 3)
        else:
            _die_at_data_request(m, 1)
        with pytest.raises(_ClientDied):
            import_collection(db=client, input_path=f, target_collection=dst)

    stored = client.existing_ids(dst, all_chashes)
    assert stored, "non-vacuity: what the dead client wrote must have landed"
    owned: set[str] = set()
    states: dict[str, str | None] = {}
    for uri in expected:
        entry = reader.by_source_uri(uri)
        if entry is not None:
            owned |= {c for _, c in _manifest(reader, str(entry.tumbler))}
            states[uri] = entry.index_state
    assert stored <= owned, f"{len(stored - owned)} chunk(s) the dead client wrote have no owner"
    if dies_after == "page 3":
        done = {u for u, (_, hi) in spans.items() if hi < 3}
        assert done and done < set(expected), "non-vacuity: some documents finished, some did not"
        assert {u for u, s in states.items() if s == "complete"} == done, states
        open_docs = set(expected) - done
        assert {states.get(u) for u in open_docs} <= {fence, None, ""}, states
    else:
        assert "complete" not in states.values()

    # The rerun's FIRST request already leaves every stored chunk owned: a document this file left
    # indexing/failed is finished with the append form, never replaced (a replace would drop its dead
    # run's rows for the pages the rerun has not reached yet).
    with monkeypatch.context() as m:
        _die_at_data_request(m, 1)
        with pytest.raises(_ClientDied):
            import_collection(db=client, input_path=f, target_collection=dst)
    stored = client.existing_ids(dst, all_chashes)
    owned = set()
    for uri in expected:
        entry = reader.by_source_uri(uri)
        if entry is not None:
            owned |= {c for _, c in _manifest(reader, str(entry.tumbler))}
    assert stored <= owned, f"{len(stored - owned)} stored chunk(s) lost their owner during the rerun"

    # ...and a full rerun finishes everything.
    rerun = import_collection(db=client, input_path=f, target_collection=dst)
    assert rerun["unowned_count"] == 0, rerun
    for uri, chashes in expected.items():
        entry = reader.by_source_uri(uri)
        assert [c for _, c in _manifest(reader, str(entry.tumbler))] == chashes, uri
        assert entry.index_state == "complete", (uri, entry.index_state)
    assert {r["id"]: r["embedding"] for r in records} == _stored_vectors(client, tmp_path, dst)


@pytest.mark.parametrize("state,hash_is_file,resumes", [
    ("indexing", True, True),
    ("failed", True, True),
    ("indexing", False, False),
    ("failed", False, False),
    ("complete", True, False),
    ("complete", False, False),
])
def test_only_a_document_this_file_left_unfinished_is_resumed_any_other_that_owns_chunks_is_kept(
    t2_service_env, tmp_path, monkeypatch, state, hash_is_file, resumes,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    cat = make_catalog_writer(priority="interactive")
    owner = cat.register_owner("knowledge", "curator")
    dst = _coll(f"resume-{state}-{int(hash_is_file)}")
    uri = f"file:///z0o2p19/{dst}/resume.py"
    texts = [f"z0o2p19 resume chunk {i}" for i in range(4)]
    chashes = [_chash(t) for t in texts]
    records = [{
        "id": c, "document": t, "metadata": {"chunk_text_hash": c},
        "embedding": _vec(100 + i).tobytes(),
        "owner": {"source_uri": uri, "title": "resume.py", "content_type": "code", "position": i},
    } for i, (c, t) in enumerate(zip(chashes, texts))]
    f = tmp_path / "resume.nxexp"
    _write_nxexp(f, dst, records)
    # The earlier state: the document owns its first two chunks, with the fence in `state`.
    doc = str(cat.register(owner=owner, title="resume.py", content_type="code",
                           physical_collection=dst, source_uri=uri))
    cat.write_manifest_many(
        [(doc, [{"chash": chashes[i], "position": i} for i in range(2)])], collection=dst,
        chunks=[{"chash": chashes[i], "text": texts[i], "metadata": {}} for i in range(2)])
    file_hash = _file_sha256(f)
    cat.begin_index_run(doc, file_hash if hash_is_file else "another-files-hash", "earlier-run", dst)
    if state == "failed":
        cat.fail_index_run(doc, "earlier failure")
    elif state == "complete":
        cat.complete_index_run(doc, file_hash if hash_is_file else "another-files-hash", 2)
    assert reader.resolve(doc).index_state == state
    writes = _record_combined_writes(monkeypatch)

    result = import_collection(db=client, input_path=f, target_collection=dst)

    if resumes:
        assert [p for p, _ in _manifest(reader, doc)] == [0, 1, 2, 3]
        assert [c for _, c in _manifest(reader, doc)] == chashes
        assert reader.resolve(doc).index_state == "complete"
        assert result["unowned_count"] == 0 and result["owned_count"] == 4, result
        assert [r for r in writes if r[0] == "write_manifest_many" and doc in r[2]] == [], (
            "a resumed document is finished with the append form, never the replacing write_many")
        assert any(r[0] == "append_manifest_many" and doc in r[2] for r in writes)
    else:
        assert [c for _, c in _manifest(reader, doc)] == chashes[:2], "the import changed a document it must keep"
        assert reader.resolve(doc).index_state == state
        assert not [r for r in writes if doc in r[2]], "no write of any kind reaches a kept document"
        assert result["unowned_count"] == 2 and result["skipped_count"] == 4, result
        assert client.existing_ids(dst, chashes[2:]) == set(), "a kept document's other chunks are not stored"


# ── The fence ───────────────────────────────────────────────────────────────


def test_a_refused_stamp_leaves_the_document_indexing_and_fails_the_import(
    t2_service_env, tmp_path, small_pages, monkeypatch,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("refused")
    records, expected = _shaped_file(dst, docs=4)
    f = tmp_path / "refused.nxexp"
    _write_nxexp(f, dst, records)
    spans = _page_of_each_doc(records)
    victim_uri = next(u for u, (lo, hi) in spans.items() if hi > lo)      # stamps on an append
    victim: dict[str, str] = {}
    failed: list[str] = []
    real_fail = hcc.HttpCatalogClient.fail_index_run
    real_append = hcc.HttpCatalogClient.append_manifest_many

    def _append(self, docs, *a, **kw):
        # The engine is told a row count that cannot match the victim's manifest, so it refuses the stamp.
        if kw.get("complete"):
            skewed = {}
            for d, (h, n) in kw["complete"].items():
                e = reader.resolve(d)
                if e is not None and e.source_uri == victim_uri:
                    victim["doc"] = d
                    n += 1
                skewed[d] = (h, n)
            kw["complete"] = skewed
        return real_append(self, docs, *a, **kw)

    def _fail(self, doc_id, error):
        failed.append(doc_id)
        return real_fail(self, doc_id, error)

    monkeypatch.setattr(hcc.HttpCatalogClient, "append_manifest_many", _append)
    monkeypatch.setattr(hcc.HttpCatalogClient, "fail_index_run", _fail)
    with pytest.raises(NexusError, match=r"refus"):
        import_collection(db=client, input_path=f, target_collection=dst)

    assert victim, "non-vacuity: the victim's stamp was sent"
    assert reader.by_source_uri(victim_uri).index_state == "indexing"
    assert victim["doc"] not in failed, "a refused stamp leaves the fence as it was; it is not failed"
    others = [u for u in expected if u != victim_uri]
    assert others and all(reader.by_source_uri(u).index_state == "complete" for u in others)


def test_a_document_the_engine_fails_on_a_page_does_not_stop_the_others(
    t2_service_env, tmp_path, small_pages, monkeypatch,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("pagefail")
    records, expected = _shaped_file(dst, docs=5)
    f = tmp_path / "pagefail.nxexp"
    _write_nxexp(f, dst, records)
    spans = _page_of_each_doc(records)
    # A document with rows on several pages: fail it on its second request.
    victim_uri = next(u for u, (lo, hi) in spans.items() if hi > lo)
    calls = {"n": 0, "doc": None}
    for name in ("write_manifest_many", "append_manifest_many"):
        real = getattr(hcc.HttpCatalogClient, name)

        def _wrap(self, docs, *a, _real=real, **kw):
            out = _real(self, docs, *a, **kw)
            for d, _rows in docs:
                e = reader.resolve(d)
                if e is not None and e.source_uri == victim_uri:
                    calls["n"] += 1
                    calls["doc"] = d
                    if calls["n"] == 2:
                        out = dict(out)
                        out["failed_doc_ids"] = [*out.get("failed_doc_ids", []), d]
            return out

        monkeypatch.setattr(hcc.HttpCatalogClient, name, _wrap)

    with pytest.raises(NexusError, match=r"document \d+(\.\d+)+") as exc:
        import_collection(db=client, input_path=f, target_collection=dst)

    assert str(calls["doc"]) in str(exc.value), "the failure names the document by tumbler"
    for uri in expected:
        state = reader.by_source_uri(uri).index_state
        if uri == victim_uri:
            assert state == "failed", state
        else:
            assert state == "complete", (uri, state)


# ── Positions ───────────────────────────────────────────────────────────────


def test_colliding_positions_keep_every_chunk_owned_and_leave_the_legitimate_ones_in_place(
    t2_service_env, tmp_path, small_pages,
):
    """Records of one document claiming a position already taken (a mixed-vintage file) go past the
    document's highest position, in arrival order. A legitimate record never moves, so the order the
    export recorded is what the manifest shows."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("collide")
    uri = f"file:///z0o2p19/{dst}/collide.py"
    claims = [0, 1, 2, 3, 1, 2]          # the last two collide with positions 1 and 2
    texts = [f"z0o2p19 collide chunk {k}" for k in range(len(claims))]
    records = [{
        "id": _chash(t), "document": t, "metadata": {"chunk_text_hash": _chash(t)},
        "embedding": _vec(200 + k).tobytes(),
        "owner": {"source_uri": uri, "title": "collide.py", "content_type": "code", "position": p},
    } for k, (t, p) in enumerate(zip(texts, claims))]
    records.sort(key=lambda r: r["id"])
    f = tmp_path / "collide.nxexp"
    _write_nxexp(f, dst, records)

    result = import_collection(db=client, input_path=f, target_collection=dst)

    assert result["owned_count"] == len(claims), result
    entry = reader.by_source_uri(uri)
    rows = dict(_manifest(reader, str(entry.tumbler)))
    assert len(rows) == len(claims) and entry.index_state == "complete"
    by_chash = {r["id"]: r["owner"]["position"] for r in records}
    arrival = [r["id"] for r in records]
    first_claim: dict[int, str] = {}
    colliders: list[str] = []
    for c in arrival:
        p = by_chash[c]
        if p in first_claim:
            colliders.append(c)
        else:
            first_claim[p] = c
    for p, c in first_claim.items():
        assert rows[p] == c, f"the legitimate chunk for position {p} moved"
    assert [rows[p] for p in sorted(rows) if p > 3] == colliders, "colliders go past the highest position, in arrival order"


def test_one_chash_at_two_positions_stamps_the_row_count_not_the_distinct_count(
    t2_service_env, tmp_path, monkeypatch,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("tworows")
    uri = f"file:///z0o2p19/{dst}/tworows.py"
    text = "z0o2p19 the same text twice"
    chash = _chash(text)
    other = "z0o2p19 and one more"
    recs = [(chash, text, 0), (chash, text, 1), (_chash(other), other, 2)]
    records = [{
        "id": c, "document": t, "metadata": {"chunk_text_hash": c}, "embedding": _vec(300 + p).tobytes(),
        "owner": {"source_uri": uri, "title": "tworows.py", "content_type": "code", "position": p},
    } for c, t, p in recs]
    f = tmp_path / "tworows.nxexp"
    _write_nxexp(f, dst, records)
    # Two-record pages: the stamp rides an APPEND (page 2) and must count the three ROWS.
    monkeypatch.setattr(
        "nexus.exporter.QUOTAS", dataclasses.replace(QUOTAS, MAX_RECORDS_PER_WRITE=2))

    result = import_collection(db=client, input_path=f, target_collection=dst)

    assert not result["unowned_count"]
    entry = reader.by_source_uri(uri)
    rows = _manifest(reader, str(entry.tumbler))
    assert [c for _, c in rows] == [chash, chash, _chash(other)]
    assert entry.index_state == "complete", "three rows, two distinct chashes: the stamp counts rows"


# ── Sam's rulings ───────────────────────────────────────────────────────────


def test_the_exported_vector_replaces_a_stored_one_including_a_chash_another_document_shares(
    t2_service_env, tmp_path, small_pages,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    cat = make_catalog_writer(priority="interactive")
    owner = cat.register_owner("knowledge", "curator")
    dst = _coll("overwrite")
    uri_y = f"file:///z0o2p19/{dst}/y.py"
    shared_text, lone_text, fresh_text = "z0o2p19 shared by two", "z0o2p19 stored ownerless", "z0o2p19 brand new"
    shared, lone, fresh = _chash(shared_text), _chash(lone_text), _chash(fresh_text)
    # Stored before the import: `shared` owned by live document X, `lone` ownerless, both with
    # vectors that are NOT the file's.
    seed_chunks_direct(dst, [shared, lone], [shared_text, lone_text],
                       [{"chunk_text_hash": shared, "bib_year": 2020}, {"chunk_text_hash": lone}],
                       embeddings=[_vec(1).tolist(), _vec(2).tolist()])
    doc_x = str(cat.register(owner=owner, title="x.py", content_type="code", physical_collection=dst,
                             source_uri=f"file:///z0o2p19/{dst}/x.py"))
    cat.write_manifest(doc_x, [{"chash": shared, "position": 0}], collection=dst)
    file_vecs = {shared: _vec(11), lone: _vec(12), fresh: _vec(13)}
    records = [{
        "id": c, "document": t, "metadata": {"chunk_text_hash": c},
        "embedding": file_vecs[c].tobytes(),
        "owner": {"source_uri": uri_y, "title": "y.py", "content_type": "code", "position": p},
    } for p, (c, t) in enumerate([(shared, shared_text), (lone, lone_text), (fresh, fresh_text)])]
    f = tmp_path / "overwrite.nxexp"
    _write_nxexp(f, dst, records)

    result = import_collection(db=client, input_path=f, target_collection=dst)

    assert result["vector_mismatches"] == 2, result      # `shared` and `lone` differed; `fresh` was new
    assert result["owned_count"] == 3 and result["unowned_count"] == 0
    stored = _stored_vectors(client, tmp_path, dst)
    assert stored == {c: v.tobytes() for c, v in file_vecs.items()}, "every vector is the file's"
    assert [c for _, c in _manifest(reader, doc_x)] == [shared], "document X still owns the shared chunk"
    kept = client.get_collection(dst).get(ids=[shared], include=["metadatas"])["metadatas"][0]
    assert kept.get("bib_year") == 2020, "metadata is merged: a key the export omits survives on a shared chunk"


def test_the_cli_says_how_many_stored_vectors_the_file_replaced(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    dst = _coll("clivec")
    text = "z0o2p19 cli vector"
    chash = _chash(text)
    seed_chunks_direct(dst, [chash], [text], [{"chunk_text_hash": chash}], embeddings=[_vec(60).tolist()])
    f = tmp_path / "clivec.nxexp"
    _write_nxexp(f, dst, [{
        "id": chash, "document": text, "metadata": {"chunk_text_hash": chash},
        "embedding": _vec(61).tobytes(),
        "owner": {"source_uri": f"file:///z0o2p19/{dst}/c.py", "title": "c.py", "content_type": "code",
                  "position": 0}}])
    with patch("nexus.commands.store._t3", return_value=client):
        cli = CliRunner().invoke(main, ["store", "import", str(f), "-c", dst])
    assert cli.exit_code == 0, cli.output
    assert "1 stored vector differed from the file's and was replaced by it" in cli.output, cli.output


def test_a_skipped_record_still_gets_its_owner_and_keeps_the_stored_vector(
    t2_service_env, tmp_path,
):
    """--skip-existing: a chunk stored earlier without an owner gains one; its payload is not sent, so
    the stored vector stays (the file's vector replaces only what is written with a payload)."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("skipowner")
    uri = f"file:///z0o2p19/{dst}/skip.py"
    text = "z0o2p19 stored without an owner"
    chash = _chash(text)
    seed_chunks_direct(dst, [chash], [text], [{"chunk_text_hash": chash}], embeddings=[_vec(40).tolist()])
    assert client.existing_ids(dst, [chash]) == {chash}
    f = tmp_path / "skip.nxexp"
    _write_nxexp(f, dst, [{
        "id": chash, "document": text, "metadata": {"chunk_text_hash": chash},
        "embedding": _vec(41).tobytes(),
        "owner": {"source_uri": uri, "title": "skip.py", "content_type": "code", "position": 0}}])

    result = import_collection(db=client, input_path=f, target_collection=dst, skip_existing=True)

    assert (result["imported_count"], result["skipped_count"], result["owned_count"]) == (0, 1, 1), result
    entry = reader.by_source_uri(uri)
    assert [c for _, c in _manifest(reader, str(entry.tumbler))] == [chash]
    assert entry.index_state == "complete"
    assert _stored_vectors(client, tmp_path, dst) == {chash: _vec(40).tobytes()}


def test_a_taxonomy_target_is_refused_up_front_before_anything_is_written(t2_service_env, tmp_path, monkeypatch):
    client = HttpVectorClient(tenant=t2_service_env)
    dst = "taxonomy__z0o2p19_centroids"
    f = tmp_path / "tax.nxexp"
    _write_nxexp(f, dst, [{
        "id": "topic-1", "document": "", "metadata": {}, "embedding": _vec(50).tobytes()}])
    posts: list[str] = []
    real_vec_post = hvc._post
    monkeypatch.setattr(hvc, "_post", lambda path, body, **kw: (posts.append(path), real_vec_post(path, body, **kw))[1])
    real_cat_post = hcc.HttpCatalogClient._post
    monkeypatch.setattr(hcc.HttpCatalogClient, "_post",
                        lambda self, path, body=None, **kw: (posts.append(path), real_cat_post(self, path, body, **kw))[1])

    with pytest.raises(NexusError, match="not supported.*Nothing was written"):
        import_collection(db=client, input_path=f, target_collection=dst)

    assert not [p for p in posts if "upsert" in p or "manifest" in p or "register" in p], posts


def test_the_engine_embedding_anyway_fails_the_import_loudly(t2_service_env, tmp_path, small_pages, monkeypatch):
    """If the engine re-embeds although every chunk carried its vector, the byte-identical property
    is gone; the import must not report success."""
    client = HttpVectorClient(tenant=t2_service_env)
    dst = _coll("embeds")
    records, _ = _shaped_file(dst, docs=2)
    f = tmp_path / "embeds.nxexp"
    _write_nxexp(f, dst, records)
    real = hcc.HttpCatalogClient.write_manifest_many

    def _lying(self, docs, *a, **kw):
        out = dict(real(self, docs, *a, **kw))
        out["embed_embedded"] = 1
        return out

    monkeypatch.setattr(hcc.HttpCatalogClient, "write_manifest_many", _lying)
    with pytest.raises(BatchWriteFailedError, match="embedded 1 chunk"):
        import_collection(db=client, input_path=f, target_collection=dst)


# ── The gate-xr789 shape ────────────────────────────────────────────────────


def test_gate_xr789_shaped_import_owns_chunks_already_stored_ownerless_across_several_pages(
    t2_service_env, tmp_path, small_pages,
):
    """The shape conexus-sdyq reported (T2 review-460161aa2): a code collection whose owner segment
    is a slug, every chunk already stored with no manifest row, records carrying only a legacy
    ``meta.doc_id`` that names a live document, a tombstoned one or none at all, re-imported with
    --skip-existing, documents larger than a page. Every chunk ends owned and every document
    complete, and the stored vectors are untouched (skip-existing sends no payload)."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    cat = make_catalog_writer(priority="interactive")
    owner = cat.register_owner("knowledge", "curator")
    dst = f"code__z0o2p19slug-2ad2825c__{_MODEL}__v1"
    cat.register_collection(dst, content_type="code", owner_id="z0o2p19slug-2ad2825c", embedding_model=_MODEL)
    live = str(cat.register(owner=owner, title="live legacy", content_type="code", physical_collection=dst,
                            source_uri=f"file:///z0o2p19/{dst}/live.py"))
    dead = str(cat.register(owner=owner, title="dead legacy", content_type="code", physical_collection=dst,
                            source_uri=f"file:///z0o2p19/{dst}/dead.py"))
    cat.delete_document(dead)
    plan = {live: 9, dead: 8, "1.99999.19": 5}
    records, by_doc = [], {}
    stored: dict[str, bytes] = {}
    for n, (doc_id, count) in enumerate(plan.items()):
        for i in range(count):
            text = f"z0o2p19 xr789 {n} chunk {i}"
            c = _chash(text)
            seed_chunks_direct(dst, [c], [text], [{"chunk_text_hash": c}], embeddings=[_vec(700 + n * 20 + i).tolist()])
            stored[c] = _vec(700 + n * 20 + i).tobytes()
            by_doc.setdefault(doc_id, []).append(c)
            records.append({"id": c, "document": text,
                            "metadata": {"chunk_text_hash": c, "doc_id": doc_id, "chunk_index": i},
                            "embedding": _vec(9000 + n * 20 + i).tobytes()})
    records.sort(key=lambda r: r["id"])
    f = tmp_path / "xr789.nxexp"
    _write_nxexp(f, dst, records)
    assert client.existing_ids(dst, list(stored)) == set(stored), "non-vacuity: stored, ownerless"

    result = import_collection(db=client, input_path=f, target_collection=dst, skip_existing=True)

    assert (result["imported_count"], result["skipped_count"]) == (0, len(records)), result
    assert result["owned_count"] == len(records) and result["unowned_count"] == 0
    assert [c for _, c in _manifest(reader, live)] == by_doc[live], "the live document keeps chunk_index order"
    assert reader.resolve(live).index_state == "complete"
    for orig in (dead, "1.99999.19"):
        entry = reader.by_source_uri(f"nxexp://{dst}/{f.name}#{orig}")
        assert entry is not None, orig
        assert [c for _, c in _manifest(reader, str(entry.tumbler))] == by_doc[orig]
        assert entry.index_state == "complete"
    assert _stored_vectors(client, tmp_path, dst) == stored, "skip-existing sent no payload: vectors untouched"


# ── Fix round 3 (nexus-z0o2p.19) ────────────────────────────────────────────


def _a_dead_run_left(cat, dst: str, name: str, state: str, file_hash: str, n_owned: int = 2):
    """A document of the file that already owns its first *n_owned* chunks, its fence in *state*."""
    owner = cat.register_owner("knowledge", "curator")
    uri = f"file:///z0o2p19/{dst}/{name}.py"
    texts = [f"z0o2p19 {name} chunk {i}" for i in range(4)]
    chashes = [_chash(t) for t in texts]
    doc = str(cat.register(owner=owner, title=f"{name}.py", content_type="code",
                           physical_collection=dst, source_uri=uri))
    cat.write_manifest_many(
        [(doc, [{"chash": chashes[i], "position": i} for i in range(n_owned)])], collection=dst,
        chunks=[{"chash": chashes[i], "text": texts[i], "metadata": {}} for i in range(n_owned)])
    cat.begin_index_run(doc, file_hash, "earlier-run", dst)
    if state == "failed":
        cat.fail_index_run(doc, "earlier failure")
    elif state == "complete":
        cat.complete_index_run(doc, file_hash, n_owned)
    records = [{
        "id": c, "document": t, "metadata": {"chunk_text_hash": c}, "embedding": _vec(400 + i).tobytes(),
        "owner": {"source_uri": uri, "title": f"{name}.py", "content_type": "code", "position": i},
    } for i, (c, t) in enumerate(zip(chashes, texts))]
    return doc, records


@pytest.mark.parametrize("state,unfinished", [("indexing", True), ("failed", True), ("complete", False)])
def test_a_kept_document_is_described_by_what_it_is_not_always_as_a_different_chunk_list(
    t2_service_env, tmp_path, monkeypatch, state, unfinished,
):
    """A document another run left ``indexing`` or ``failed`` (a different export, an index run) is kept
    by the same rule as a finished one, but it is not "a document with a different chunk list": it is
    unfinished. The message says so and what the user can do."""
    client = HttpVectorClient(tenant=t2_service_env)
    cat = make_catalog_writer(priority="interactive")
    dst = _coll(f"keptmsg-{state}")
    f = tmp_path / "keptmsg.nxexp"
    doc, records = _a_dead_run_left(cat, dst, "keptmsg", state, "another-exports-hash")
    _write_nxexp(f, dst, records)
    instances: list = []
    real_finish = exporter_mod._OwnerImport.finish

    def _spy(self):
        instances.append(self)
        return real_finish(self)

    monkeypatch.setattr(exporter_mod._OwnerImport, "finish", _spy)
    with patch("nexus.commands.store._t3", return_value=client):
        cli = CliRunner().invoke(main, ["store", "import", str(f), "-c", dst])
    assert cli.exit_code == 0, cli.output
    out = " ".join(cli.output.split())
    if unfinished:
        assert "left unfinished by another run" in out, out
        assert "already exist with a different chunk list" not in out, out
    else:
        assert "already exist with a different chunk list" in out, out
        assert "left unfinished" not in out, out
    assert f"nx store delete -c {dst} --title keptmsg.py" in out, out
    (owner_import,) = instances
    assert all(not k["file"] and not k["existing"] for k in owner_import._kept.values()), (
        "a kept document's chash sets are released once all its records have been seen")


def test_two_owner_groups_that_resolve_to_one_document_are_one_document_with_the_combined_count(
    t2_service_env, tmp_path, monkeypatch,
):
    """A document in another collection is COPIED into the target under ``nxexp://<target>/<uri>``. A
    file that also names that qualified identity literally has two owner groups resolving to ONE
    document. Its total is the sum: stamping it complete at the first group's count and then failing
    the second group's rows would flip a complete document to failed."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    cat = make_catalog_writer(priority="interactive")
    owner = cat.register_owner("knowledge", "curator")
    dst, src = _coll("twogroups"), _coll("twogroups-src")
    uri_a = f"file:///z0o2p19/{src}/two.py"
    cat.register(owner=owner, title="two.py", content_type="code", physical_collection=src, source_uri=uri_a)
    qualified = f"nxexp://{dst}/{uri_a}"
    texts = [f"z0o2p19 two groups chunk {i}" for i in range(6)]
    records = []
    for i, t in enumerate(texts):
        records.append({
            "id": _chash(t), "document": t, "metadata": {"chunk_text_hash": _chash(t)},
            "embedding": _vec(500 + i).tobytes(),
            "owner": {"source_uri": uri_a if i < 3 else qualified, "title": "two.py",
                      "content_type": "code", "position": i}})
    records.sort(key=lambda r: r["id"])
    f = tmp_path / "twogroups.nxexp"
    _write_nxexp(f, dst, records)
    monkeypatch.setattr(
        "nexus.exporter.QUOTAS", dataclasses.replace(QUOTAS, MAX_RECORDS_PER_WRITE=2))
    failed: list[str] = []
    real_fail = hcc.HttpCatalogClient.fail_index_run
    monkeypatch.setattr(hcc.HttpCatalogClient, "fail_index_run",
                        lambda self, d, e: (failed.append(d), real_fail(self, d, e))[1])

    result = import_collection(db=client, input_path=f, target_collection=dst)

    assert not failed
    assert (result["owned_count"], result["unowned_count"]) == (6, 0), result
    entry = reader.by_source_uri(qualified)
    assert entry is not None and entry.index_state == "complete"
    assert sorted(c for _, c in _manifest(reader, str(entry.tumbler))) == sorted(r["id"] for r in records)


def test_the_summary_and_the_cli_report_documents_whose_replaced_chunks_were_not_swept(
    t2_service_env, tmp_path, monkeypatch,
):
    """The engine's sweep fails open: the document reads complete and its superseded chunks stay. The
    import says so instead of summing the count and dropping it."""
    client = HttpVectorClient(tenant=t2_service_env)
    dst = _coll("sweepskip")
    records, _ = _shaped_file(dst, docs=3)
    f = tmp_path / "sweepskip.nxexp"
    _write_nxexp(f, dst, records)
    real = hcc.HttpCatalogClient.write_manifest_many

    def _skipping(self, docs, *a, **kw):
        out = dict(real(self, docs, *a, **kw))
        out["sweep_skipped"] = int(out.get("sweep_skipped") or 0) + 1
        return out

    monkeypatch.setattr(hcc.HttpCatalogClient, "write_manifest_many", _skipping)
    with patch("nexus.commands.store._t3", return_value=client):
        cli = CliRunner().invoke(main, ["store", "import", str(f), "-c", dst])
    assert cli.exit_code == 0, cli.output
    out = " ".join(cli.output.split())
    assert "could not be swept" in out and "nx t3 gc" in out, out


def test_a_legacy_local_target_is_written_with_the_model_it_is_registered_with(t2_service_env, tmp_path):
    """A two-segment name carries no model. Its export header holds the prefix-based guess (what
    ``export_collection`` writes), which in a local install is not the model the collection is
    registered with, and the engine compares the request's ``embedding_model`` with the registered
    one. The import must send the registered model, or the first request is refused."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = "knowledge__z0o2p19-legacy-model"
    guess = index_model_for_collection(dst)
    assert guess != _MODEL, "non-vacuity: the header's guess must differ from the local model"
    uri = f"file:///z0o2p19/{dst}/legacy.md"
    texts = ["z0o2p19 legacy one", "z0o2p19 legacy two"]
    records = [{
        "id": _chash(t), "document": t, "metadata": {"chunk_text_hash": _chash(t)},
        "embedding": _vec(600 + i).tobytes(),
        "owner": {"source_uri": uri, "title": "legacy.md", "content_type": "knowledge", "position": i},
    } for i, t in enumerate(texts)]
    f = tmp_path / "legacy.nxexp"
    _write_nxexp(f, dst, records, model=guess)

    result = import_collection(db=client, input_path=f, target_collection=dst)

    assert (result["imported_count"], result["owned_count"]) == (2, 2), result
    assert reader.by_source_uri(uri).index_state == "complete"
    assert {r["id"]: r["embedding"] for r in records} == _stored_vectors(client, tmp_path, dst)


def test_a_cce_collection_is_paged_at_the_write_cap_not_at_the_embed_cap(
    t2_service_env, tmp_path, monkeypatch,
):
    """The 64-row cap on a CCE collection bounds the ENGINE'S EMBEDDING latency; an import embeds
    nothing, so it pages at the record-write cap."""
    client = HttpVectorClient(tenant=t2_service_env)
    dst = _coll("pagecap")
    records, _ = _shaped_file(dst, docs=4)
    f = tmp_path / "pagecap.nxexp"
    _write_nxexp(f, dst, records)
    monkeypatch.setattr(hvc, "per_collection_chunk_cap", lambda *a, **kw: 2)
    pages: list[int] = []
    real = exporter_mod._OwnerImport.flush
    monkeypatch.setattr(exporter_mod._OwnerImport, "flush",
                        lambda self, page: (pages.append(len(page)), real(self, page))[1])

    import_collection(db=client, input_path=f, target_collection=dst)

    assert pages and max(pages) == len(records), pages


def test_the_snapshot_minus_written_sweep_list_runs_through_trailing_sweeps_against_the_real_engine(
    t2_service_env, monkeypatch,
):
    """A fresh document whose pre-run manifest has 650 chunks is rewritten as two rows (one of them an
    old chunk). The deferred sweep is the begin snapshot minus what the run wrote, 649 chashes: 300
    ride the last data append, two sweep-only appends carry the rest, and the stamp rides the last.
    Every other piece is pinned by a fake or by a Java test; this drives the composition end to end."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    cat = make_catalog_writer(priority="interactive")
    owner = cat.register_owner("knowledge", "curator")
    dst = _coll("trailsweep")
    uri = f"file:///z0o2p19/{dst}/big.py"
    doc = str(cat.register(owner=owner, title="big.py", content_type="code",
                           physical_collection=dst, source_uri=uri))
    rng = np.random.default_rng(650)

    def chunk(text: str) -> dict:
        return {"chash": _chash(text), "text": text, "metadata": {},
                "embedding": rng.standard_normal(_DIM).astype(np.float32).tolist()}

    old = [chunk(f"z0o2p19 old chunk {i}") for i in range(650)]
    rows = [{"chash": c["chash"], "position": i} for i, c in enumerate(old)]
    cat.write_manifest_many([(doc, rows[:300])], collection=dst, chunks=old[:300], embedding_model=_MODEL)
    cat.append_manifest_many([(doc, rows[300:600])], collection=dst, chunks=old[300:600], embedding_model=_MODEL)
    cat.append_manifest_many([(doc, rows[600:])], collection=dst, chunks=old[600:], embedding_model=_MODEL)
    assert len(_manifest(reader, doc)) == 650

    appends: list[dict] = []
    real_append = hcc.HttpCatalogClient.append_manifest_many

    def _spy(self, docs, *a, **kw):
        out = real_append(self, docs, *a, **kw)
        appends.append({"sweep": (kw.get("sweep_chashes") or {}).get(doc), "complete": kw.get("complete"),
                        "swept": out.get("swept"), "skipped": out.get("sweep_skipped")})
        return out

    monkeypatch.setattr(hcc.HttpCatalogClient, "append_manifest_many", _spy)
    new = chunk("z0o2p19 the one new chunk")
    w = MultiDocumentImportWriter(
        cat, collection=dst, content_hash=_chash("the file"), embedding_model=_MODEL,
        force_re_embed=True, metadata_merge=True)
    w.register_document(doc, total_rows=2, max_position=1)
    w.write_page({doc: [{"chash": new["chash"], "position": w.claim_position(doc, 0)}]},
                 {new["chash"]: new})
    res = w.write_page({doc: [{"chash": old[0]["chash"], "position": w.claim_position(doc, 1)}]},
                       {old[0]["chash"]: old[0]})

    assert res.finished == [doc]
    assert [len(a["sweep"] or []) for a in appends] == [300, 300, 49], appends
    assert [a["complete"] is not None for a in appends] == [False, False, True]
    assert sum(int(a["swept"] or 0) for a in appends) == 649 and not any(a["skipped"] for a in appends)
    assert w.sweep_skipped == 0
    survivors = {c["chash"] for c in old} | {new["chash"]}
    stored = set()
    ids = sorted(survivors)
    for i in range(0, len(ids), 300):
        stored |= client.existing_ids(dst, ids[i:i + 300])
    assert stored == {old[0]["chash"], new["chash"]}, "every swept chunk is gone and the two live ones stay"
    assert [c for _, c in _manifest(reader, doc)] == [new["chash"], old[0]["chash"]]
    assert reader.resolve(doc).index_state == "complete"
