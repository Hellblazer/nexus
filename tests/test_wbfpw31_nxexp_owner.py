# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.31 (RDR-192 Step 5, client): ``.nxexp`` import must register
an owner document before storing chunks, so imported chunks stay LIVE past
the RDR-192 grace window instead of being reaped as manifest-less.

Real engine substrate (``t2_service_env``) for every test that exercises
export/import against a live catalog: owner resolution reads
``docs_for_chashes``/``get_manifests``/``resolve_many`` and writes
``register``/``write_manifest`` on the real ``HttpCatalogClient``, which a
mocked T3 client cannot stand in for. ``test_accumulate_owner_group_*`` is
the one pure-unit exception (no catalog or T3 call at all).
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import msgpack
import numpy as np
import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog.collection_name import owner_segment_for_tumbler
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.limits import QUOTAS
from nexus.errors import NexusError
from nexus.exporter import (
    _accumulate_owner_group,
    _resolve_import_owner_tumbler,
    export_collection,
    import_collection,
)

# nexus-wbfpw.31: only the tests that touch a real catalog/T3 substrate are
# integration-marked (per-function below); test_accumulate_owner_group_*
# is pure-unit (fakes only) and stays in the default fast suite.

_MODEL = "bge-base-en-v15-768"
_DIM = 768


def _coll(name: str) -> str:
    return f"knowledge__wbfpw31-{name}__{_MODEL}__v1"


def _owned_doc(writer, client, collection: str, owner_tumbler, title: str, contents: list[str]):
    """Register one catalog document under *owner_tumbler* and upsert +
    manifest each of *contents* as its ordered chunks -- the same shape
    the real knowledge write path (``catalog_store_hook_tracked`` +
    ``T3Database.put`` + the manifest hook) produces, built directly so
    the test controls chunk count and position explicitly.

    Returns ``(tumbler_str, source_uri, [chash, ...])``.
    """

    source_uri = uri_for(collection, title)
    tumbler = writer.register(
        owner=owner_tumbler, title=title, content_type="knowledge",
        physical_collection=collection, source_uri=source_uri,
    )
    chashes: list[str] = []
    for content in contents:
        chash = hashlib.sha256(content.encode()).hexdigest()
        client.upsert_chunks_with_embeddings(
            collection, ids=[chash], documents=[content], embeddings=[],
            metadatas=[{
                "title": title, "chunk_text_hash": chash,
                "indexed_at": datetime.now(UTC).isoformat(),
            }],
        )
        chashes.append(chash)
    writer.write_manifest(
        str(tumbler),
        [{"chash": c, "position": i} for i, c in enumerate(chashes)],
        collection=collection,
    )
    return str(tumbler), source_uri, chashes


def _read_nxexp_records(path: Path) -> list[dict]:
    with open(path, "rb") as f:
        f.readline()  # header
        with gzip.GzipFile(fileobj=f) as gz:
            return list(msgpack.Unpacker(gz, raw=False))


def _write_hand_crafted_nxexp(
    path: Path, collection_name: str, records: list[dict],
    model: str = _MODEL, dim: int = _DIM,
) -> None:
    """A ``.nxexp`` file built record-by-record rather than through
    :func:`export_collection` -- for pinning the pre-nexus-wbfpw.31 export
    shape (no ``owner`` key, and, for the oldest vintage, no ``doc_id``
    either) that a real, already-produced backup file can carry.
    """
    header = {
        "format_version": 1,
        "collection_name": collection_name,
        "database_type": collection_name.split("__")[0],
        "embedding_model": model,
        "record_count": len(records),
        "embedding_dim": dim,
        "exported_at": "2026-01-01T00:00:00+00:00",
        "pipeline_version": "nexus-1",
    }
    rng = np.random.default_rng(seed=31)
    with open(path, "wb") as f:
        f.write(json.dumps(header).encode() + b"\n")
        with gzip.GzipFile(fileobj=f, mode="wb") as gz:
            for r in records:
                r = dict(r)
                r.setdefault(
                    "embedding",
                    rng.standard_normal(dim).astype(np.float32).tobytes(),
                )
                gz.write(msgpack.packb(r, use_bin_type=True))


# ── Unit: _accumulate_owner_group (no catalog, no T3) ───────────────────────


def test_accumulate_owner_group_position_and_uri_synthesis():

    target = "knowledge__x__bge-base-en-v15-768__v1"
    groups: dict = {}

    # Explicit source_uri + explicit position: honored verbatim.
    _accumulate_owner_group(
        groups,
        {"source_uri": "file:///a", "title": "A", "content_type": "knowledge", "position": 3},
        "chashA",
        fallback_source_uri="nxexp://col/f", fallback_title="f",
        fallback_content_type="knowledge", target_collection=target,
    )
    assert groups["file:///a"]["rows"] == [(3, "chashA")]
    assert groups["file:///a"]["content_type"] == "knowledge"

    # Title-only owner (no source_uri): synthesizes the SAME chroma://
    # convention catalog_store_hook_tracked uses for a title-only note.
    _accumulate_owner_group(
        groups, {"title": "B"}, "chashB",
        fallback_source_uri="nxexp://col/f", fallback_title="f",
        fallback_content_type="knowledge", target_collection=target,
    )
    synthesized = uri_for(target, "B")
    assert groups[synthesized]["rows"] == [(0, "chashB")]

    # No owner field at all: file-fallback identity, sequential position.
    _accumulate_owner_group(
        groups, None, "chashC",
        fallback_source_uri="nxexp://col/f", fallback_title="f",
        fallback_content_type="knowledge", target_collection=target,
    )
    _accumulate_owner_group(
        groups, None, "chashD",
        fallback_source_uri="nxexp://col/f", fallback_title="f",
        fallback_content_type="knowledge", target_collection=target,
    )
    assert groups["nxexp://col/f"]["rows"] == [(0, "chashC"), (1, "chashD")]
    assert groups["nxexp://col/f"]["title"] == "f"


# ── Round trip: multi-batch documents stay owned ────────────────────────────


@pytest.mark.integration
def test_round_trip_multi_batch_document_stays_owned(t2_service_env, tmp_path, monkeypatch):

    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    knowledge_owner = writer.register_owner("knowledge", "curator")

    src = _coll("src-rt")
    dst = _coll("dst-rt")

    doc_a_id, doc_a_uri, doc_a_chashes = _owned_doc(
        writer, client, src, knowledge_owner, "wbfpw31 Doc A",
        ["wbfpw31 doc a chunk zero", "wbfpw31 doc a chunk one", "wbfpw31 doc a chunk two"],
    )
    doc_b_id, doc_b_uri, doc_b_chashes = _owned_doc(
        writer, client, src, knowledge_owner, "wbfpw31 Doc B",
        ["wbfpw31 doc b chunk zero", "wbfpw31 doc b chunk one"],
    )

    # Shrink the page size well below either document's chunk count so
    # BOTH export and import must span more than one 300-record batch —
    # the exact shape a per-batch manifest write gets wrong (see
    # import_collection's own docstring).
    monkeypatch.setattr(
        "nexus.exporter.QUOTAS", dataclasses.replace(QUOTAS, MAX_RECORDS_PER_WRITE=2),
    )

    out = tmp_path / "rt.nxexp"
    export_result = export_collection(db=client, collection_name=src, output_path=out)
    assert export_result["exported_count"] == 5

    records = _read_nxexp_records(out)
    owned_records = [r for r in records if r.get("owner")]
    assert len(owned_records) == 5, "every exported chunk has a live manifested owner"
    for r in owned_records:
        assert r["owner"]["source_uri"] in (doc_a_uri, doc_b_uri)
        assert r["owner"]["content_type"] == "knowledge"

    import_result = import_collection(db=client, input_path=out, target_collection=dst)
    assert import_result["imported_count"] == 5
    assert import_result["owned_count"] == 5

    for chash in doc_a_chashes + doc_b_chashes:
        got = client.get_collection(dst).get(ids=[chash], include=[])
        assert chash in got["ids"], f"{chash} not live in {dst}"

    new_doc_a = reader.by_source_uri(doc_a_uri)
    assert new_doc_a is not None
    assert new_doc_a.physical_collection == dst
    rows_a = sorted(reader.get_manifest(str(new_doc_a.tumbler)), key=lambda r: r.position)
    assert [r.chash for r in rows_a] == doc_a_chashes

    new_doc_b = reader.by_source_uri(doc_b_uri)
    assert new_doc_b is not None
    rows_b = sorted(reader.get_manifest(str(new_doc_b.tumbler)), key=lambda r: r.position)
    assert [r.chash for r in rows_b] == doc_b_chashes


# ── Legacy export (no owner, no doc_id): one document per import file ──────


@pytest.mark.integration
def test_legacy_export_gets_file_fallback_owner_and_is_idempotent(t2_service_env, tmp_path):

    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()

    dst = _coll("legacy-fallback")
    contents = [f"wbfpw31 legacy chunk {i}" for i in range(3)]
    records = []
    for content in contents:
        chash = hashlib.sha256(content.encode()).hexdigest()
        records.append({
            "id": chash, "document": content,
            "metadata": {"chunk_text_hash": chash},
        })

    legacy_file = tmp_path / "legacy.nxexp"
    _write_hand_crafted_nxexp(legacy_file, dst, records)

    result1 = import_collection(db=client, input_path=legacy_file, target_collection=dst)
    assert result1["imported_count"] == 3
    assert result1["owned_count"] == 3

    expected_uri = f"nxexp://{dst}/{legacy_file.name}"
    doc = reader.by_source_uri(expected_uri)
    assert doc is not None
    assert doc.title == legacy_file.name
    rows = sorted(reader.get_manifest(str(doc.tumbler)), key=lambda r: r.position)
    assert [r.chash for r in rows] == [r["id"] for r in records]

    # Re-importing the SAME file must reconcile onto the SAME document,
    # never mint a sibling (skip_existing not needed for idempotency here
    # -- write_manifest is a replace, and by_source_uri finds the row).
    result2 = import_collection(db=client, input_path=legacy_file, target_collection=dst)
    assert result2["owned_count"] == 3
    doc_again = reader.by_source_uri(expected_uri)
    assert doc_again is not None
    assert doc_again.tumbler == doc.tumbler


# ── --skip-existing: a skipped record still ends up owned ──────────────────


@pytest.mark.integration
def test_skip_existing_records_still_end_up_owned(t2_service_env, tmp_path):

    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    knowledge_owner = writer.register_owner("knowledge", "curator")

    src = _coll("src-skip")
    dst = _coll("dst-skip")
    doc_id, doc_uri, chashes = _owned_doc(
        writer, client, src, knowledge_owner, "wbfpw31 Skip Doc",
        ["wbfpw31 skip chunk zero", "wbfpw31 skip chunk one"],
    )

    out = tmp_path / "skip.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)

    first = import_collection(db=client, input_path=out, target_collection=dst)
    assert first["imported_count"] == 2
    assert first["skipped_count"] == 0
    assert first["owned_count"] == 2

    second = import_collection(
        db=client, input_path=out, target_collection=dst, skip_existing=True,
    )
    assert second["skipped_count"] == 2, "every chunk already exists in dst"
    assert second["owned_count"] == 2, "group membership is unconditional on skip_existing"

    doc = reader.by_source_uri(doc_uri)
    assert doc is not None
    rows = sorted(reader.get_manifest(str(doc.tumbler)), key=lambda r: r.position)
    assert [r.chash for r in rows] == chashes


# ── Export fails loud when the catalog cannot answer ────────────────────────


@pytest.mark.integration
def test_export_fails_loud_when_catalog_unreachable(t2_service_env, tmp_path, monkeypatch):

    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    col = _coll("export-fail")
    _owned_doc(writer, client, col, owner, "wbfpw31 Fail Doc", ["wbfpw31 one lone chunk"])

    class _BrokenReader:
        def docs_for_chashes(self, chashes):
            raise RuntimeError("catalog offline")

    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda: _BrokenReader(),
    )

    with pytest.raises(NexusError, match="catalog is unreachable"):
        export_collection(db=client, collection_name=col, output_path=tmp_path / "fail.nxexp")


# ── Owner-tumbler resolution: code/docs use the collection's owner segment ─


@pytest.mark.integration
def test_resolve_import_owner_tumbler_uses_owner_segment_for_code_collection(t2_service_env):

    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner_tumbler = writer.register_owner(
        "wbfpw31-owner-test", "repo", repo_hash="wbfpw31deadbeefcafe",
    )
    seg = owner_segment_for_tumbler(str(owner_tumbler))
    collection = f"code__{seg}__{_MODEL}__v1"

    resolved = _resolve_import_owner_tumbler(collection, reader, writer)
    assert resolved == owner_tumbler


@pytest.mark.integration
def test_resolve_import_owner_tumbler_uses_knowledge_curator_for_knowledge_collection(
    t2_service_env,
):

    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    expected = writer.register_owner("knowledge", "curator")

    # A knowledge collection's four-segment name IS conformant, but its
    # owner_id segment ("wbfpw31-owner-branch") is an arbitrary subject
    # slug, never a tumbler-derived one -- this must NOT be parsed as a
    # tumbler; every knowledge document is owned by the ONE curator.
    collection = _coll("owner-branch")
    resolved = _resolve_import_owner_tumbler(collection, reader, writer)
    assert resolved == expected
