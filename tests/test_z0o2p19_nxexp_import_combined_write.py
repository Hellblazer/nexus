# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z0o2p.19 (RDR-223 P2.9, client): the ``.nxexp`` import writes every chunk together with its
owner row through the catalog manifest routes, carrying the exported vectors.

Real engine substrate (``t2_service_env``): the properties under test (a chunk never lands without an
owner, the vectors stay byte-identical, a document reads ``complete``) live on the engine side of the
wire and a mocked client cannot stand in for them.

* Test Plan 9 (RDR-223): a gate-xr789-shaped file imported this way. Vectors byte-identical to the
  export, the engine embeds nothing, scattered positions end in manifest order, and a second import
  leaves the documents as they are.
* Test Plan 8 (RDR-223): the client dies after the first page. No chunk that request wrote is without
  an owner, and a rerun finishes the import.
* The import makes no call to ``/v1/vectors/upsert-chunks`` (bead requirement 8).
* A refused completion stamp leaves the fence ``indexing`` (RDR-223 client rule).
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
from pathlib import Path

import msgpack
import numpy as np
import pytest

import nexus.catalog.http_catalog_client as hcc
import nexus.db.http_vector_client as hvc
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.multi_document_write import MultiDocumentImportWriter
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.limits import QUOTAS
from nexus.errors import IndexRunVerifyRefused, NexusError
from nexus.exporter import export_collection, import_collection

_MODEL = "bge-base-en-v15-768"
_DIM = 768
_PAGE = 7
_DATA_PATHS = ("/manifest/write_many", "/manifest/append", "/manifest/append_many")


def _coll(name: str) -> str:
    return f"code__z0o2p19-{name}__{_MODEL}__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_nxexp(path: Path, collection: str, records: list[dict]) -> None:
    header = {
        "format_version": 1, "collection_name": collection,
        "database_type": collection.split("__")[0], "embedding_model": _MODEL,
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
    """A gate-xr789-shaped file: several documents of 1 to 7 chunks each, positions scattered (not
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


def _pages_of(records: list[dict], uri: str, page: int = _PAGE) -> set[int]:
    return {i // page for i, r in enumerate(records) if r["owner"]["source_uri"] == uri}


@pytest.fixture
def small_pages(monkeypatch):
    monkeypatch.setattr(
        "nexus.exporter.QUOTAS", dataclasses.replace(QUOTAS, MAX_RECORDS_PER_WRITE=_PAGE))


def _record_combined_writes(monkeypatch) -> list[tuple[str, dict]]:
    """Record every combined write's ``(route, result)`` (the engine's counters live in the result)."""
    seen: list[tuple[str, dict]] = []
    for name in ("write_manifest_many", "append_manifest_many"):
        real = getattr(hcc.HttpCatalogClient, name)

        def _wrap(self, *a, _real=real, _name=name, **kw):
            out = _real(self, *a, **kw)
            seen.append((_name, out))
            return out

        monkeypatch.setattr(hcc.HttpCatalogClient, name, _wrap)
    return seen


def _manifest(reader, doc_id: str) -> list[tuple[int, str]]:
    return [(r.position, r.chash) for r in reader.get_manifest(doc_id)]


# ── Test Plan 9 ─────────────────────────────────────────────────────────────


def test_gate_shaped_import_keeps_vectors_positions_and_completes_every_document(
    t2_service_env, tmp_path, small_pages, monkeypatch,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("shape")
    records, expected = _shaped_file(dst)
    f = tmp_path / "shape.nxexp"
    _write_nxexp(f, dst, records)
    assert any(len(_pages_of(records, u)) > 1 for u in expected), "the fixture must span pages"
    writes = _record_combined_writes(monkeypatch)

    result = import_collection(db=client, input_path=f, target_collection=dst)

    assert (result["imported_count"], result["owned_count"], result["skipped_count"]) == (
        len(records), len(records), 0), result
    # The engine embedded nothing and stored every supplied vector.
    assert writes, "the import must go through the combined routes"
    assert sum(int(r.get("embed_embedded") or 0) for _, r in writes) == 0
    assert sum(int(r.get("vectors_supplied") or 0) for _, r in writes) == len(records)
    # Vectors byte-identical to the file.
    out = tmp_path / "back.nxexp"
    export_collection(db=client, collection_name=dst, output_path=out)
    back = {r["id"]: r["embedding"] for r in _read_nxexp(out)}
    assert {r["id"]: r["embedding"] for r in records} == back
    # Scattered positions end in manifest order, and every document reads complete.
    for uri, chashes in expected.items():
        entry = reader.by_source_uri(uri)
        assert entry is not None, uri
        rows = _manifest(reader, str(entry.tumbler))
        assert [c for _, c in rows] == chashes, uri
        positions = [p for p, _ in rows]
        assert positions == sorted(positions) and len(set(positions)) == len(positions)
        assert entry.index_state == "complete", (uri, entry.index_state)

    # A second import leaves the documents as they are: no duplicate rows, no chunk written twice.
    n_writes = len(writes)
    again = import_collection(db=client, input_path=f, target_collection=dst)
    assert again["imported_count"] == 0 and again["skipped_count"] == len(records), again
    assert again["owned_count"] == len(records) and again["unowned_count"] == 0, again
    assert len(writes) == n_writes, "a second import must write nothing"
    for uri, chashes in expected.items():
        entry = reader.by_source_uri(uri)
        assert [c for _, c in _manifest(reader, str(entry.tumbler))] == chashes


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
    assert set(catalog_paths) & set(_DATA_PATHS), (
        "the spies saw no combined write: they are not watching the import")
    assert not [p for p in vector_paths if p.endswith("/upsert-chunks")], vector_paths


# ── Test Plan 8 ─────────────────────────────────────────────────────────────


class _ClientDied(Exception):
    """The simulated death of the client between two data requests."""


def test_client_dying_after_the_first_page_leaves_no_ownerless_chunk(
    t2_service_env, tmp_path, small_pages, monkeypatch,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("death")
    records, expected = _shaped_file(dst)
    f = tmp_path / "death.nxexp"
    _write_nxexp(f, dst, records)
    all_chashes = [r["id"] for r in records]

    seen = {"n": 0}

    def _count_or_die(path: str) -> None:
        seen["n"] += 1
        if seen["n"] > 1:
            raise _ClientDied(f"client died before data request {seen['n']}")

    real_cat_post = hcc.HttpCatalogClient._post
    real_vec_post = hvc._post

    def _cat_post(self, path, body=None, **kw):
        if path in _DATA_PATHS:
            _count_or_die(path)
        return real_cat_post(self, path, body, **kw)

    def _vec_post(path, body, **kw):
        if path.endswith("/upsert-chunks"):
            _count_or_die(path)
        return real_vec_post(path, body, **kw)

    monkeypatch.setattr(hcc.HttpCatalogClient, "_post", _cat_post)
    monkeypatch.setattr(hvc, "_post", _vec_post)
    with pytest.raises(_ClientDied):
        import_collection(db=client, input_path=f, target_collection=dst)
    monkeypatch.setattr(hcc.HttpCatalogClient, "_post", real_cat_post)
    monkeypatch.setattr(hvc, "_post", real_vec_post)

    stored = client.existing_ids(dst, all_chashes)
    assert stored, "non-vacuity: the first page's chunks must have landed"
    owned: set[str] = set()
    states: list[str | None] = []
    for uri in expected:
        entry = reader.by_source_uri(uri)
        if entry is not None:
            owned |= {c for _, c in _manifest(reader, str(entry.tumbler))}
            states.append(entry.index_state)
    assert stored <= owned, f"{len(stored - owned)} chunk(s) the dead client wrote have no owner"
    assert "complete" not in states, "a document the dead client never finished must not read complete"

    # A rerun finishes the import: every document whole and complete.
    rerun = import_collection(db=client, input_path=f, target_collection=dst)
    assert rerun["unowned_count"] == 0, rerun
    for uri, chashes in expected.items():
        entry = reader.by_source_uri(uri)
        assert [c for _, c in _manifest(reader, str(entry.tumbler))] == chashes, uri
        assert entry.index_state == "complete", (uri, entry.index_state)


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
    victim_uri = next(iter(expected))
    victim = {}
    real_complete = hcc.HttpCatalogClient.complete_index_run
    failed: list[str] = []
    real_fail = hcc.HttpCatalogClient.fail_index_run

    def _complete(self, doc_id, content_hash, chunk_count):
        entry = reader.resolve(doc_id)
        if entry is not None and entry.source_uri == victim_uri:
            victim["doc"] = doc_id
            raise IndexRunVerifyRefused(
                doc_id=doc_id, referenced=chunk_count, present=chunk_count - 1, missing=1,
                chunk_count=chunk_count)
        return real_complete(self, doc_id, content_hash, chunk_count)

    def _fail(self, doc_id, error):
        failed.append(doc_id)
        return real_fail(self, doc_id, error)

    monkeypatch.setattr(hcc.HttpCatalogClient, "complete_index_run", _complete)
    monkeypatch.setattr(hcc.HttpCatalogClient, "fail_index_run", _fail)
    with pytest.raises(NexusError, match=r"refus"):
        import_collection(db=client, input_path=f, target_collection=dst)

    assert reader.by_source_uri(victim_uri).index_state == "indexing"
    assert victim["doc"] not in failed, "a refused stamp leaves the fence as it was; it is not failed"
    for uri in expected:
        if uri != victim_uri:
            assert reader.by_source_uri(uri).index_state == "complete", uri


# ── Positions ───────────────────────────────────────────────────────────────


def test_colliding_positions_keep_every_chunk_owned(t2_service_env, tmp_path, small_pages):
    """Two records of one document claiming one position (a mixed-vintage file): the chunk is
    neither dropped nor allowed to overwrite the other's manifest row."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    dst = _coll("collide")
    rng = np.random.default_rng(3)
    uri = f"file:///z0o2p19/{dst}/collide.py"
    records = []
    chashes = []
    for k in range(3):
        text = f"z0o2p19 collide chunk {k}"
        chash = _chash(text)
        chashes.append(chash)
        records.append({
            "id": chash, "document": text, "metadata": {"chunk_text_hash": chash},
            "embedding": rng.standard_normal(_DIM).astype(np.float32).tobytes(),
            "owner": {"source_uri": uri, "title": "collide.py", "content_type": "code", "position": 0},
        })
    f = tmp_path / "collide.nxexp"
    _write_nxexp(f, dst, records)
    result = import_collection(db=client, input_path=f, target_collection=dst)
    assert result["owned_count"] == 3, result
    entry = reader.by_source_uri(uri)
    rows = _manifest(reader, str(entry.tumbler))
    assert sorted(c for _, c in rows) == sorted(chashes)
    assert len({p for p, _ in rows}) == 3, rows


# ── The deferred sweep, against the real engine ─────────────────────────────


def test_the_deferred_sweep_removes_what_the_replace_dropped_and_spares_what_another_document_owns(
    t2_service_env,
):
    """The trailing sweep-only ``append_many`` (no rows, ``sweep_chashes`` only) is the one request the
    import rarely sends (keep-existing makes a first-seen document's previous manifest empty), so it
    is exercised here directly: a document's manifest is replaced, the chashes it dropped are swept
    after the last append, a dropped chash another document still owns survives, and the document
    reads ``complete``."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    cat = make_catalog_writer(priority="interactive")
    coll = _coll("sweep")
    owner = cat.register_owner("knowledge", "curator")

    def _doc(title: str) -> str:
        return str(cat.register(
            owner=owner, title=title, content_type="code", physical_collection=coll,
            source_uri=f"file:///z0o2p19/{coll}/{title}"))

    texts = {n: f"z0o2p19 sweep chunk {n}" for n in ("x", "y", "w", "z")}
    ch = {n: _chash(t) for n, t in texts.items()}

    def _payload(*names):
        return [{"chash": ch[n], "text": texts[n], "metadata": {"chunk_text_hash": ch[n]}} for n in names]

    doc, other = _doc("subject.py"), _doc("other.py")
    cat.write_manifest_many(
        [(doc, [{"chash": ch[n], "position": i} for i, n in enumerate(("x", "y", "w"))])],
        chunks=_payload("x", "y", "w"), collection=coll)
    cat.write_manifest_many(
        [(other, [{"chash": ch["w"], "position": 0}])], chunks=_payload("w"), collection=coll)

    w = MultiDocumentImportWriter(cat, collection=coll, content_hash="a" * 64)
    rows = [{"chash": ch["z"], "position": w.claim_position(doc, 0)}]
    res = w.write_page({doc: rows}, {ch["z"]: _payload("z")[0]})
    assert res.written == [doc]
    # Nothing is swept before finish().
    assert client.existing_ids(coll, [ch["x"], ch["y"], ch["w"]]) == {ch["x"], ch["y"], ch["w"]}
    done = w.finish()

    assert done.completed == [doc] and not done.failed, done
    assert client.existing_ids(coll, [ch["x"], ch["y"]]) == set(), "the dropped, unshared chunks are swept"
    assert client.existing_ids(coll, [ch["w"], ch["z"]]) == {ch["w"], ch["z"]}
    assert [c for _, c in _manifest(reader, doc)] == [ch["z"]]
    assert reader.resolve(doc).index_state == "complete"
    assert [c for _, c in _manifest(reader, other)] == [ch["w"]]
