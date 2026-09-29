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
import shlex
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import msgpack
import numpy as np
import pytest
from click.testing import CliRunner

import nexus.catalog.http_catalog_client as hcc
import nexus.exporter as exporter_mod
from nexus.aspect_readers import uri_for
from nexus.catalog.collection_name import owner_segment_for_tumbler
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.limits import QUOTAS
from nexus.errors import NexusError
from nexus.exporter import (
    _accumulate_owner_group,
    _manifest_rows,
    _resolve_import_owner_tumbler,
    export_collection,
    import_collection,
)

# Not integration-marked (nexus-wbfpw.38): the substrate provisions itself
# and CI's default selection must run these import-owner pins.

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


def test_manifest_rows_orders_and_renumbers_colliding_positions():

    # Distinct positions: kept verbatim, ordered by position.
    assert _manifest_rows([(3, "a"), (0, "b")]) == [
        {"chash": "b", "position": 0}, {"chash": "a", "position": 3},
    ]
    # A mixed file can give two chunks position 0 (explicit owner position
    # plus a position-less record's running count): keep order, renumber,
    # never hand write_manifest a duplicate primary key.
    assert _manifest_rows([(0, "a"), (0, "b"), (2, "c")]) == [
        {"chash": "a", "position": 0},
        {"chash": "b", "position": 1},
        {"chash": "c", "position": 2},
    ]


# ── Round trip: multi-batch documents stay owned ────────────────────────────


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

    # Copy, not move (Sam, 2026-09-27): dst gets its own documents under the
    # target-qualified identity; src's documents are untouched.
    for orig_uri, orig_id, chashes in (
        (doc_a_uri, doc_a_id, doc_a_chashes), (doc_b_uri, doc_b_id, doc_b_chashes),
    ):
        copy = reader.by_source_uri(f"nxexp://{dst}/{orig_uri}")
        assert copy is not None
        assert copy.physical_collection == dst
        assert str(copy.tumbler) != orig_id
        rows = sorted(reader.get_manifest(str(copy.tumbler)), key=lambda r: r.position)
        assert [r.chash for r in rows] == chashes

        orig = reader.by_source_uri(orig_uri)
        assert orig is not None
        assert str(orig.tumbler) == orig_id
        assert orig.physical_collection == src
        orig_rows = sorted(reader.get_manifest(orig_id), key=lambda r: r.position)
        assert [r.chash for r in orig_rows] == chashes, "source manifest must survive the import"

    # The source collection is still live, and both collections answer search
    # (the bead's acceptance criterion names search, not just get).
    for chash in doc_a_chashes + doc_b_chashes:
        got = client.get_collection(src).get(ids=[chash], include=[])
        assert chash in got["ids"], f"{chash} no longer live in {src}"
    for coll in (src, dst):
        hits = client.search(
            "wbfpw31 doc a chunk one", [coll], n_results=10,
            threshold=float("inf"), structured=True,
        )
        assert set(doc_a_chashes) <= set(hits["ids"]), f"search on {coll} missed doc A"

    # Re-importing lands on the same copy documents, never a sibling.
    again = import_collection(db=client, input_path=out, target_collection=dst)
    assert again["owned_count"] == 5
    copy_a = reader.by_source_uri(f"nxexp://{dst}/{doc_a_uri}")
    rows_again = sorted(reader.get_manifest(str(copy_a.tumbler)), key=lambda r: r.position)
    assert [r.chash for r in rows_again] == doc_a_chashes
    assert [r.chash for r in sorted(reader.get_manifest(doc_a_id), key=lambda r: r.position)] == doc_a_chashes


# ── Legacy export (no owner, no doc_id): one document per import file ──────


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

    doc = reader.by_source_uri(f"nxexp://{dst}/{doc_uri}")
    assert doc is not None
    assert doc.physical_collection == dst
    rows = sorted(reader.get_manifest(str(doc.tumbler)), key=lambda r: r.position)
    assert [r.chash for r in rows] == chashes


# ── One owner group failing does not strand the others ─────────────────────


def test_one_failed_owner_group_does_not_strand_the_rest(t2_service_env, tmp_path, monkeypatch):

    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")

    src = _coll("src-partial")
    dst = _coll("dst-partial")
    _, bad_uri, _ = _owned_doc(writer, client, src, owner, "wbfpw31 Bad Doc", ["wbfpw31 bad chunk"])
    _, good_uri, good_chashes = _owned_doc(
        writer, client, src, owner, "wbfpw31 Good Doc", ["wbfpw31 good chunk"],
    )
    out = tmp_path / "partial.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)

    real = exporter_mod._resolve_owner_document

    def _fail_bad(group, *a, **kw):
        if group["source_uri"] == bad_uri:
            raise RuntimeError("injected register failure")
        return real(group, *a, **kw)

    monkeypatch.setattr(exporter_mod, "_resolve_owner_document", _fail_bad)

    with pytest.raises(NexusError, match=r"1 of 2 owner documents.*injected register failure"):
        import_collection(db=client, input_path=out, target_collection=dst)

    good = reader.by_source_uri(f"nxexp://{dst}/{good_uri}")
    assert good is not None, "the group after (or before) the failure must still be written"
    rows = reader.get_manifest(str(good.tumbler))
    assert [r.chash for r in rows] == good_chashes


# ── Export fails loud when the catalog cannot answer ────────────────────────


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


def test_resolve_import_owner_tumbler_reads_the_collection_row_not_the_name(t2_service_env):
    """nexus-wbfpw.33: the owner comes from the collection's catalog row.
    An owner segment is not always tumbler-derived (gate-xr789's
    code__arcaneum-2ad2825c), so a slug segment must resolve through the
    row, and a numeric-looking segment with no row must NOT be parsed."""

    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner_tumbler = writer.register_owner(
        "wbfpw33-owner-test", "repo", repo_hash="wbfpw33deadbeefcafe",
    )
    slug = f"code__wbfpw33slug-2ad2825c__{_MODEL}__v1"
    writer.register_collection(
        slug, content_type="code", owner_id=str(owner_tumbler), embedding_model=_MODEL,
    )
    assert _resolve_import_owner_tumbler(slug, reader, writer) == owner_tumbler

    # What real writers store: the name's hyphenated owner segment ("1-1").
    hyphen = f"code__wbfpw33hyphen-row__{_MODEL}__v1"
    writer.register_collection(
        hyphen, content_type="code",
        owner_id=owner_segment_for_tumbler(str(owner_tumbler)), embedding_model=_MODEL,
    )
    assert _resolve_import_owner_tumbler(hyphen, reader, writer) == owner_tumbler

    curator = writer.register_owner("knowledge", "curator")
    seg = owner_segment_for_tumbler(str(owner_tumbler))
    unregistered = f"code__{seg}__{_MODEL}__v1-unregistered"
    assert _resolve_import_owner_tumbler(unregistered, reader, writer) == curator
    # A name that does not even parse (non-canonical model segment) resolves too.
    odd = "knowledge__wbfpw33-seam__all-minilm-l6-v2-384__v1"
    assert _resolve_import_owner_tumbler(odd, reader, writer) == curator


def test_import_into_slug_owned_code_collection_is_owned(t2_service_env, tmp_path):
    """The conexus-sdyq failure end to end: re-import into a code collection
    whose owner segment is a slug, --skip-existing, chunks already stored."""

    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("wbfpw33-slug-repo", "repo", repo_hash="wbfpw33slugrepocafe")
    dst = f"code__wbfpw33e2e-2ad2825c__{_MODEL}__v1"
    slug = "wbfpw33e2e-2ad2825c"
    # The row holds the slug as its owner, the shape a collection first
    # registered by name carries (and what the chunk write used to leave here
    # on every collection); the resolver must fall through it.
    writer.register_collection(dst, content_type="code", owner_id=slug, embedding_model=_MODEL)

    records = []
    for i in range(2):
        content = f"wbfpw33 slug chunk {i}"
        chash = hashlib.sha256(content.encode()).hexdigest()
        client.upsert_chunks_with_embeddings(
            dst, ids=[chash], documents=[content], embeddings=[],
            metadatas=[{"chunk_text_hash": chash, "indexed_at": datetime.now(UTC).isoformat()}],
        )
        records.append({"id": chash, "document": content, "metadata": {"chunk_text_hash": chash}})

    f = tmp_path / "slug.nxexp"
    _write_hand_crafted_nxexp(f, dst, records)
    result = import_collection(db=client, input_path=f, target_collection=dst, skip_existing=True)
    assert result["owned_count"] == 2

    # The chunk write kept the slug (nexus-7tys2), so the import found no owner on
    # the row and no document in the collection and landed under the curator. That
    # minted document is now what names the collection's owner (nexus-6pbwx): the
    # curator's segment replaced the slug, and a later resolve reads it from the row.
    curator = writer.register_owner("knowledge", "curator")
    assert reader.get_collection(dst)["owner_id"] == owner_segment_for_tumbler(str(curator))
    assert _resolve_import_owner_tumbler(dst, reader, writer) == curator
    doc = reader.by_source_uri(f"nxexp://{dst}/{f.name}")
    assert doc is not None
    assert doc.physical_collection == dst
    assert doc.tumbler.owner_address() == curator

    # The mixed state (nexus-6pbwx): the import's curator document came first, a real repo
    # document joins afterwards (nx index repo into the restored collection). The curator's
    # segment was only provisional, so the repo owner replaces it and the next resolve
    # returns the repo owner, not the curator.
    existing = writer.register(
        owner=owner, title="wbfpw33 existing", content_type="code",
        physical_collection=dst, source_uri=f"file:///wbfpw33/{dst}/existing.py",
    )
    assert existing is not None
    assert reader.get_collection(dst)["owner_id"] == owner_segment_for_tumbler(str(owner))
    assert _resolve_import_owner_tumbler(dst, reader, writer) == owner

    # A slug-owned collection that ALREADY holds a live document resolves to that
    # document's owner: the document gave the row the owner segment when it landed.
    seeded = f"code__wbfpw33seeded-2ad2825c__{_MODEL}__v1"
    writer.register_collection(
        seeded, content_type="code", owner_id="wbfpw33seeded-2ad2825c", embedding_model=_MODEL,
    )
    seeded_doc = writer.register(
        owner=owner, title="wbfpw33 seeded", content_type="code",
        physical_collection=seeded, source_uri=f"file:///wbfpw33/{seeded}/seeded.py",
    )
    assert seeded_doc is not None
    assert reader.get_collection(seeded)["owner_id"] == owner_segment_for_tumbler(str(owner))
    assert _resolve_import_owner_tumbler(seeded, reader, writer) == owner
    for r in records:
        assert r["id"] in client.get_collection(dst).get(ids=[r["id"]], include=[])["ids"]


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


# ── Legacy doc_id records: owned whether their document is live, dead or gone ─


def test_legacy_doc_id_records_are_owned_even_when_skipped(t2_service_env, tmp_path):
    """The gate-xr789 shape (conexus-sdyq): chunks already in the target with
    no manifest, carrying meta.doc_id that names a live document, a
    tombstoned one, or none at all, re-imported with --skip-existing. The
    per-batch hook never fires for skipped records, so only the owner path
    can make them live."""

    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    dst = _coll("legacy-docid")

    live_doc = str(writer.register(
        owner=owner, title="wbfpw31 live legacy", content_type="knowledge",
        physical_collection=dst, source_uri=uri_for(dst, "wbfpw31 live legacy"),
    ))
    dead_doc = str(writer.register(
        owner=owner, title="wbfpw31 dead legacy", content_type="knowledge",
        physical_collection=dst, source_uri=uri_for(dst, "wbfpw31 dead legacy"),
    ))
    writer.delete_document(dead_doc)
    missing_doc = "1.99999.31"

    plan = [
        (live_doc, "wbfpw31 legacy live 0"), (live_doc, "wbfpw31 legacy live 1"),
        (dead_doc, "wbfpw31 legacy dead 0"), (dead_doc, "wbfpw31 legacy dead 1"),
        (missing_doc, "wbfpw31 legacy missing 0"),
    ]
    records = []
    per_doc: dict[str, int] = {}
    for doc_id, content in plan:
        chash = hashlib.sha256(content.encode()).hexdigest()
        idx = per_doc.get(doc_id, 0)
        per_doc[doc_id] = idx + 1
        meta = {"chunk_text_hash": chash, "doc_id": doc_id, "chunk_index": idx}
        client.upsert_chunks_with_embeddings(
            dst, ids=[chash], documents=[content], embeddings=[],
            metadatas=[{**meta, "indexed_at": datetime.now(UTC).isoformat()}],
        )
        records.append({"id": chash, "document": content, "metadata": meta, "chash": chash})

    by_doc = {d: [r["chash"] for r in records if r["metadata"]["doc_id"] == d]
              for d in (live_doc, dead_doc, missing_doc)}
    for r in records:
        r.pop("chash")

    f = tmp_path / "legacy-docid.nxexp"
    _write_hand_crafted_nxexp(f, dst, records)
    result = import_collection(db=client, input_path=f, target_collection=dst, skip_existing=True)
    assert result["skipped_count"] == 5
    assert result["owned_count"] == 5

    for chashes in by_doc.values():
        for chash in chashes:
            got = client.get_collection(dst).get(ids=[chash], include=[])
            assert chash in got["ids"], f"{chash} not live in {dst}"

    # The live original document keeps its chunks, in chunk_index order.
    rows = sorted(reader.get_manifest(live_doc), key=lambda r: r.position)
    assert [r.chash for r in rows] == by_doc[live_doc]
    # Dead and missing originals each get their own new document.
    for orig in (dead_doc, missing_doc):
        doc = reader.by_source_uri(f"nxexp://{dst}/{f.name}#{orig}")
        assert doc is not None, orig
        assert doc.physical_collection == dst
        rows = sorted(reader.get_manifest(str(doc.tumbler)), key=lambda r: r.position)
        assert [r.chash for r in rows] == by_doc[orig]


def test_live_document_with_owner_and_legacy_chunks_keeps_all_of_them(t2_service_env, tmp_path):
    """A live document whose chunks arrive partly as owner-tagged records
    (with a stale meta.doc_id beside the owner) and partly as legacy
    doc_id-only records. Both resolve to the same document; the manifest
    is a whole-document replace, so the rows must be merged into one
    write, never written group by group."""

    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    dst = _coll("mixed-owner-legacy")
    title = "wbfpw31 mixed doc"
    doc_uri = uri_for(dst, title)
    doc = str(writer.register(
        owner=owner, title=title, content_type="knowledge",
        physical_collection=dst, source_uri=doc_uri,
    ))

    records = []
    chashes = []
    for i in range(4):
        content = f"wbfpw31 mixed chunk {i}"
        chash = hashlib.sha256(content.encode()).hexdigest()
        meta = {"chunk_text_hash": chash, "doc_id": doc, "chunk_index": i}
        client.upsert_chunks_with_embeddings(
            dst, ids=[chash], documents=[content], embeddings=[],
            metadatas=[{**meta, "indexed_at": datetime.now(UTC).isoformat()}],
        )
        rec = {"id": chash, "document": content, "metadata": meta}
        if i < 2:
            rec["owner"] = {
                "source_uri": doc_uri, "title": title,
                "content_type": "knowledge", "position": i,
            }
        records.append(rec)
        chashes.append(chash)

    f = tmp_path / "mixed.nxexp"
    _write_hand_crafted_nxexp(f, dst, records)
    result = import_collection(db=client, input_path=f, target_collection=dst, skip_existing=True)
    assert result["owned_count"] == 4

    rows = sorted(reader.get_manifest(doc), key=lambda r: r.position)
    assert [r.chash for r in rows] == chashes
    for chash in chashes:
        assert chash in client.get_collection(dst).get(ids=[chash], include=[])["ids"]


def test_import_leaves_an_existing_documents_current_manifest_alone(t2_service_env, tmp_path):
    """nexus-wbfpw.40 (Sam, 2026-09-29: keep existing). An import that
    resolves to a live document which already owns chunks must not replace
    its manifest with the file's rows: the engine's manifest write deletes
    every row for the document first, so an older export imported over a
    re-put note hid the correction and made it reapable. The file's chunks
    the document does not own stay unowned, and the result says how many.
    Not integration-marked, so CI's default selection runs it."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    coll = _coll("keep-existing")
    doc, _uri, (v1,) = _owned_doc(writer, client, coll, owner, "wbfpw40 note", ["wbfpw40 version one"])

    old_export = tmp_path / "old.nxexp"
    export_collection(db=client, collection_name=coll, output_path=old_export)

    # The note is re-put: its manifest now names only v2.
    v2_text = "wbfpw40 version two, the correction"
    v2 = hashlib.sha256(v2_text.encode()).hexdigest()
    client.upsert_chunks_with_embeddings(
        coll, ids=[v2], documents=[v2_text], embeddings=[],
        metadatas=[{"title": "wbfpw40 note", "chunk_text_hash": v2,
                    "indexed_at": datetime.now(UTC).isoformat()}],
    )
    writer.write_manifest(doc, [{"chash": v2, "position": 0}], collection=coll)

    result = import_collection(db=client, input_path=old_export, target_collection=coll, skip_existing=True)

    assert [r.chash for r in reader.get_manifest(doc)] == [v2], "the import replaced the current manifest"
    assert v2 in client.get_collection(coll).get(ids=[v2], include=[])["ids"], "the correction must stay visible"
    assert v1 not in client.get_collection(coll).get(ids=[v1], include=[])["ids"], "the old export must not resurrect v1"
    assert result["owned_count"] == 0
    assert result["unowned_count"] == 1
    assert result["unowned_documents"] == [{"tumbler": doc, "title": "wbfpw40 note"}]

    # The CLI turns that into a command the operator can run as printed.
    with patch("nexus.commands.store._t3", return_value=client):
        cli = CliRunner().invoke(main, ["store", "import", str(old_export), "-c", coll])
    assert cli.exit_code == 0, cli.output
    assert f"nx store delete -c {coll} --title 'wbfpw40 note'" in cli.output, cli.output

    # Control: re-importing a CURRENT export is a no-op that reports its chunk owned.
    current_export = tmp_path / "current.nxexp"
    export_collection(db=client, collection_name=coll, output_path=current_export)
    again = import_collection(db=client, input_path=current_export, target_collection=coll, skip_existing=True)
    assert [r.chash for r in reader.get_manifest(doc)] == [v2]
    assert (again["owned_count"], again["unowned_count"]) == (1, 0)


def test_legacy_doc_id_import_leaves_an_existing_documents_manifest_alone(t2_service_env, tmp_path):
    """nexus-wbfpw.40 review round: the legacy meta.doc_id shape reached the
    same replace through the per-batch manifest_write_batch_hook, which
    fired for each upserted batch BEFORE the keep-existing check and then
    looked like an existing manifest to it. No skip_existing, so the hook
    path is exercised."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    coll = _coll("keep-existing-legacy")
    doc, _uri, (v2,) = _owned_doc(writer, client, coll, owner, "wbfpw40 legacy note", ["wbfpw40 legacy current"])

    v3_text = "wbfpw40 legacy stale export chunk"
    v3 = hashlib.sha256(v3_text.encode()).hexdigest()
    f = tmp_path / "legacy.nxexp"
    _write_hand_crafted_nxexp(f, coll, [{
        "id": v3, "document": v3_text,
        "metadata": {"chunk_text_hash": v3, "doc_id": doc, "chunk_index": 0},
    }])
    result = import_collection(db=client, input_path=f, target_collection=coll)

    assert [r.chash for r in reader.get_manifest(doc)] == [v2], "the legacy import replaced the current manifest"
    assert v2 in client.get_collection(coll).get(ids=[v2], include=[])["ids"]
    assert (result["owned_count"], result["unowned_count"]) == (0, 1)


def test_a_failed_manifest_read_never_overwrites(t2_service_env, tmp_path, monkeypatch):
    """If the existing manifest cannot be read, the document is reported as
    failed and its manifest is not written: an unread manifest may hold
    chunks the write would hide."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    coll = _coll("keep-existing-readfail")
    doc, _uri, (v1,) = _owned_doc(writer, client, coll, owner, "wbfpw40 readfail note", ["wbfpw40 readfail v1"])
    out = tmp_path / "readfail.nxexp"
    export_collection(db=client, collection_name=coll, output_path=out)

    def _boom(self, doc_id):
        raise RuntimeError("wbfpw40 injected manifest read failure")

    real_get = hcc.HttpCatalogClient.get_manifest
    monkeypatch.setattr(hcc.HttpCatalogClient, "get_manifest", _boom)
    writes: list[str] = []
    real_write = hcc.HttpCatalogClient.write_manifest
    monkeypatch.setattr(hcc.HttpCatalogClient, "write_manifest",
                        lambda self, d, rows, **kw: (writes.append(d), real_write(self, d, rows, **kw))[1])
    with pytest.raises(NexusError, match="injected manifest read failure"):
        import_collection(db=client, input_path=out, target_collection=coll, skip_existing=True)
    assert doc not in writes
    monkeypatch.setattr(hcc.HttpCatalogClient, "get_manifest", real_get)
    monkeypatch.setattr(hcc.HttpCatalogClient, "write_manifest", real_write)
    assert [r.chash for r in reader.get_manifest(doc)] == [v1]


def test_delete_then_import_restores_a_document_from_the_file(t2_service_env, tmp_path):
    """The remedy nx store import prints for unowned records: delete the
    document, then import. The re-import must own the file's chunks."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    coll = _coll("delete-then-import")
    doc, _uri, (v1,) = _owned_doc(writer, client, coll, owner, "wbfpw40 restore note", ["wbfpw40 restore v1"])
    out = tmp_path / "restore.nxexp"
    export_collection(db=client, collection_name=coll, output_path=out)
    v2_text = "wbfpw40 restore v2"
    v2 = hashlib.sha256(v2_text.encode()).hexdigest()
    client.upsert_chunks_with_embeddings(
        coll, ids=[v2], documents=[v2_text], embeddings=[],
        metadatas=[{"title": "wbfpw40 restore note", "chunk_text_hash": v2,
                    "indexed_at": datetime.now(UTC).isoformat()}],
    )
    writer.write_manifest(doc, [{"chash": v2, "position": 0}], collection=coll)

    with patch("nexus.commands.store._t3", return_value=client):
        deleted = CliRunner().invoke(main, ["store", "delete", "--title", "wbfpw40 restore note", "-c", coll, "--yes"])
    assert deleted.exit_code == 0, deleted.output
    result = import_collection(db=client, input_path=out, target_collection=coll)
    assert (result["owned_count"], result["unowned_count"]) == (1, 0), result
    assert v1 in client.get_collection(coll).get(ids=[v1], include=[])["ids"], "the restored chunk must be visible"


def test_the_printed_delete_command_quotes_a_hostile_title(t2_service_env, tmp_path):
    """A catalog title is user data; the command nx store import prints must
    round-trip through a shell as exactly one --title argument."""
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    coll = _coll("hostile-title")
    title = 'x" ; echo INJECTED $(id) `id` ; "'
    doc, _uri, _ = _owned_doc(writer, client, coll, owner, title, ["wbfpw40 hostile v1"])
    out = tmp_path / "hostile.nxexp"
    export_collection(db=client, collection_name=coll, output_path=out)
    v2_text = "wbfpw40 hostile v2"
    v2 = hashlib.sha256(v2_text.encode()).hexdigest()
    client.upsert_chunks_with_embeddings(
        coll, ids=[v2], documents=[v2_text], embeddings=[],
        metadatas=[{"title": title, "chunk_text_hash": v2, "indexed_at": datetime.now(UTC).isoformat()}],
    )
    writer.write_manifest(doc, [{"chash": v2, "position": 0}], collection=coll)
    with patch("nexus.commands.store._t3", return_value=client):
        cli = CliRunner().invoke(main, ["store", "import", str(out), "-c", coll])
    assert cli.exit_code == 0, cli.output
    [line] = [ln.strip() for ln in cli.output.splitlines() if ln.strip().startswith("nx store delete")]
    assert shlex.split(line) == ["nx", "store", "delete", "-c", coll, "--title", title], line
