# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.11 (RDR-192 S5, client test): MVV (b) visibility half and
MVV (c), against the real engine substrate.

MVV (b): a store_put re-put whose same-call reap fails leaves the old chunk
physically in T3 (``_reap_superseded_note_chunks`` is best-effort by design
and swallows the delete error). Before S5 (nexus-wbfpw.10) raw ``search()``
still returned that stranded chunk. Under live(c) it must return only the
current note, while the old chunk stays countable: the manifest-less census
lists it in the ``superseded`` bucket, which is what the RDR-192 reaper
works from.

MVV (c): live(c) must not hide anything that has a live own-collection
owner. Three write paths that produce one: a note that was never re-put
(MCP ``store_put``), a document written through the combined-write path
the ChunkBatcher flush uses (``write_manifest_many`` with ``chunks=`` and
``sweep=True``), and a chunk brought in by ``nx store import`` with its
manifest.

Deliberately not ``integration``-marked: the substrate provisions itself,
and the default selection is what CI runs, so a marker would leave this pin
unexercised.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import patch

from click.testing import CliRunner

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.cli import main
from nexus.corpus import t3_collection_name
import nexus.db as nexus_db
from nexus.db.http_vector_client import HttpVectorClient
from nexus.exporter import export_collection
from nexus.mcp.core import store_put


def _search_ids(client, collection: str, text: str) -> list[str]:
    """Raw ``search()`` over the wire, queried with the chunk's own text so
    an exact self-match ranks at or near the top; ``n_results`` is generous
    because this is a visibility probe, not a relevance test."""
    return client.search(text, [collection], n_results=50, structured=True).get("ids") or []


def _get_ids(client, collection: str, chash: str) -> list[str]:
    return client.get_collection(collection).get(ids=[chash], include=[]).get("ids") or []


def _manifest_chashes(collection: str, title: str) -> tuple[str, set[str]]:
    reader = make_catalog_reader()
    assert reader is not None
    doc = reader.by_source_uri(uri_for(collection, title))
    assert doc is not None, f"{title!r} was never registered in {collection}"
    return str(doc.tumbler), {r.chash for r in reader.get_manifest(str(doc.tumbler))}


class _DeleteRaises:
    """A T3 handle whose collection ``delete`` raises and records the ids it
    was asked to delete; everything else goes to the real handle."""

    def __init__(self, real, attempted: list[str]) -> None:
        self._real = real
        self._attempted = attempted

    def get_collection(self, name: str):
        return _CollectionDeleteRaises(self._real.get_collection(name), self._attempted)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class _CollectionDeleteRaises:
    def __init__(self, real, attempted: list[str]) -> None:
        self._real = real
        self._attempted = attempted

    def delete(self, *args, ids=None, **kwargs):
        self._attempted.extend(ids or [])
        raise RuntimeError("wbfpw.11 fault injection: reap delete failed")

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_reput_with_failed_reap_search_returns_only_the_current_note(
    t2_service_env, monkeypatch,
):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "wbfpw11-reap-fail"
    title = "wbfpw11 reap-fail note"
    v1 = "wbfpw11 first version: heron migration notes from the northern marsh"
    v2 = "wbfpw11 second version: completely rewritten text about lighthouse lenses"

    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=v1, collection=subject, title=title)
    collection = t3_collection_name(subject, t3=client)
    tumbler, v1_chashes = _manifest_chashes(collection, title)
    assert len(v1_chashes) == 1, v1_chashes
    (v1_chash,) = v1_chashes
    assert v1_chash in _search_ids(client, collection, v1), "control: v1 is searchable before the re-put"

    # The reap imports make_t3 from nexus.db at call time, so this patch
    # reaches its delete and nothing that goes through _get_t3.
    attempted: list[str] = []
    real_make_t3 = nexus_db.make_t3
    monkeypatch.setattr(
        nexus_db, "make_t3", lambda *a, **kw: _DeleteRaises(real_make_t3(*a, **kw), attempted),
    )
    with patch("nexus.mcp.core._get_t3", return_value=client):
        result = store_put(content=v2, collection=subject, title=title)
    monkeypatch.setattr(nexus_db, "make_t3", real_make_t3)

    assert "Stored" in result, f"a failed reap must not fail the put: {result!r}"
    assert v1_chash in attempted, (
        "the reap never tried to delete v1, so the fault never fired and this "
        f"test proves nothing: attempted={attempted!r}"
    )
    tumbler2, v2_chashes = _manifest_chashes(collection, title)
    assert tumbler2 == tumbler, "a re-put under the same title reconciles onto one document"
    assert len(v2_chashes) == 1 and v1_chash not in v2_chashes, v2_chashes
    (v2_chash,) = v2_chashes

    v1_hits = _search_ids(client, collection, v1)
    v2_hits = _search_ids(client, collection, v2)
    assert v2_chash in v2_hits, "the current note must be searchable"
    assert v1_chash not in v1_hits, (
        "raw search returned the superseded chunk the failed reap left behind; "
        "live(c) must hide a chunk with no live own-collection manifest owner"
    )
    assert v1_chash not in v2_hits
    assert _get_ids(client, collection, v1_chash) == [], "raw get must hide v1 too"

    # The chunk is still there physically, and the census puts it where the
    # RDR-192 reaper will find it.
    census = client.manifest_less_census(collection, limit=300)
    assert v1_chash in census["chashes"]["superseded"], census
    assert census["owners"][v1_chash]["owner_tumbler"] == tumbler, census["owners"][v1_chash]
    assert v2_chash not in {h for hs in census["chashes"].values() for h in hs}, (
        "the current chunk has its own manifest row and must not appear in the census"
    )


def test_chunks_with_a_live_owner_stay_visible_on_every_write_path(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("wbfpw11-live", "curator")
    runner = CliRunner()
    rows: dict[str, tuple[str, str, str]] = {}  # name -> (collection, chash, text)

    # 1. A note that was never re-put.
    subject = "wbfpw11-steady"
    text = "wbfpw11 steady note: stored once and never touched again"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=text, collection=subject, title="wbfpw11 steady")
    collection = t3_collection_name(subject, t3=client)
    _tumbler, chashes = _manifest_chashes(collection, "wbfpw11 steady")
    assert len(chashes) == 1, chashes
    rows["never re-put note"] = (collection, next(iter(chashes)), text)
    model_token = collection.split("__")[2]

    # 2. The combined-write path: chunk and manifest in one write_many
    # request with sweep on, as a ChunkBatcher flush sends it.
    collection = f"knowledge__wbfpw11-combined__{model_token}__v1"
    text = "wbfpw11 combined write: chunk and manifest landed in one request"
    chash = hashlib.sha256(text.encode()).hexdigest()
    doc = str(writer.register(
        owner, "wbfpw11-combined.md", content_type="knowledge",
        file_path="/tmp/wbfpw11/combined.md", physical_collection=collection, chunk_count=1,
    ))
    writer.write_manifest_many(
        [(doc, [{"chash": chash, "position": 0}])],
        collection=collection,
        sweep=True,
        chunks=[{"chash": chash, "text": text, "metadata": {"indexed_at": datetime.now(UTC).isoformat()}}],
    )
    rows["combined write"] = (collection, chash, text)

    # 3. nx store import of an export whose note carries its manifest.
    src = f"knowledge__wbfpw11-export-src__{model_token}__v1"
    title, text = "wbfpw11 imported", "wbfpw11 imported note: exported with its manifest, then imported"
    source_uri = uri_for(src, title)
    assert source_uri is not None
    src_doc = str(writer.register(
        owner=owner, title=title, content_type="knowledge",
        physical_collection=src, source_uri=source_uri,
    ))
    chash = hashlib.sha256(text.encode()).hexdigest()
    client.upsert_chunks_with_embeddings(
        src, ids=[chash], documents=[text], embeddings=[],
        metadatas=[{"title": title, "chunk_text_hash": chash, "indexed_at": datetime.now(UTC).isoformat()}],
    )
    writer.write_manifest(src_doc, [{"chash": chash, "position": 0}], collection=src)
    export = tmp_path / "wbfpw11.nxexp"
    export_collection(db=client, collection_name=src, output_path=export)
    target_subject = "wbfpw11-imported"
    with patch("nexus.commands.store._t3", return_value=client):
        imported = runner.invoke(main, ["store", "import", str(export), "-c", target_subject])
    assert imported.exit_code == 0, imported.output
    target = t3_collection_name(target_subject, t3=client)
    reader = make_catalog_reader()
    assert reader is not None
    imported_docs = [
        d for d in reader.all_documents()
        if d.physical_collection == target and d.title == title
    ]
    assert len(imported_docs) == 1, imported_docs
    chashes = {r.chash for r in reader.get_manifest(str(imported_docs[0].tumbler))}
    assert chashes == {chash}, "the import must bring the manifest with the chunk"
    rows["imported with manifest"] = (target, chash, text)

    assert len(rows) == 3
    hidden = {
        name: {"search": chash in _search_ids(client, coll, text), "get": chash in _get_ids(client, coll, chash)}
        for name, (coll, chash, text) in rows.items()
    }
    assert all(v["search"] and v["get"] for v in hidden.values()), (
        f"live(c) hid a chunk that has a live own-collection owner: {hidden}"
    )
