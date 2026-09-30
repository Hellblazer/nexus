# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sis0m.5: ``nx store delete --title A`` when A shares its chunk with
B in the same collection.

Identical chunk text in one collection is one T3 row by design, so two notes
with the same body share a chash and both manifests name it. The delete path
resolved that chash to its owner, found two, reaped nothing, and B's manifest
then kept the chunk: "Deleted 0 entries", exit 0, A still live, B unnamed.
The chunk row's title is its last writer's, so ``--title A`` could also miss
A outright when B was written second.

``--title`` names documents. A is tombstoned; the chunk stays because B still
holds it, and the output says so.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient

pytestmark = pytest.mark.integration

_MODEL = "bge-base-en-v15-768"


def _coll(name: str) -> str:
    return f"knowledge__sis0m5-{name}__{_MODEL}__v1"


def _note(writer, client, owner, collection: str, title: str, content: str) -> tuple[str, str, str]:
    """A store_put-shaped note. Returns ``(tumbler, source_uri, chash)``."""
    source_uri = uri_for(collection, title)
    assert source_uri is not None
    tumbler = str(writer.register(
        owner=owner, title=title, content_type="knowledge",
        physical_collection=collection, source_uri=source_uri,
    ))
    chash = hashlib.sha256(content.encode()).hexdigest()
    client.upsert_chunks_with_embeddings(
        collection, ids=[chash], documents=[content], embeddings=[],
        metadatas=[{
            "title": title, "chunk_text_hash": chash,
            "indexed_at": datetime.now(UTC).isoformat(),
        }],
    )
    writer.write_manifest(tumbler, [{"chash": chash, "position": 0}], collection=collection)
    return tumbler, source_uri, chash


def _live(client, collection: str, chash: str) -> bool:
    return chash in client.get_collection(collection).get(ids=[chash], include=[])["ids"]


@pytest.mark.parametrize("delete_first_written", [True, False], ids=["delete-first", "delete-second"])
def test_delete_title_of_a_shared_chunk_note_removes_it_and_names_the_sharer(
    t2_service_env, delete_first_written,
):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    col = _coll("shared")
    body = "sis0m5 body both notes carry"
    ta, ua, chash = _note(writer, client, owner, col, "sis0m5 note A", body)
    tb, ub, chash_b = _note(writer, client, owner, col, "sis0m5 note B", body)
    assert chash == chash_b  # the condition the test names: one shared row

    gone_title, gone_uri, gone_t = ("sis0m5 note A", ua, ta) if delete_first_written else ("sis0m5 note B", ub, tb)
    kept_title, kept_uri, kept_t = ("sis0m5 note B", ub, tb) if delete_first_written else ("sis0m5 note A", ua, ta)

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(
            main, ["store", "delete", "-c", col, "--title", gone_title, "-y"],
        )

    assert result.exit_code == 0, result.output
    assert reader.by_source_uri(gone_uri) is None, f"{gone_title} must be tombstoned:\n{result.output}"
    kept = reader.by_source_uri(kept_uri)
    assert kept is not None, "the sharing document must survive"
    assert [r.chash for r in reader.get_manifest(kept_t)] == [chash]
    assert _live(client, col, chash), "the shared chunk must stay for the surviving document"
    assert f"Deleted document {gone_title!r} ({gone_t})" in result.output, result.output
    assert kept_title in result.output and kept_t in result.output, (
        f"the output must name the document still holding the chunk:\n{result.output}"
    )


def test_delete_title_of_an_unshared_note_still_deletes_its_chunk(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    col = _coll("solo")
    t, uri, chash = _note(writer, client, owner, col, "sis0m5 solo", "sis0m5 solo body")

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(main, ["store", "delete", "-c", col, "--title", "sis0m5 solo", "-y"])

    assert result.exit_code == 0, result.output
    assert reader.by_source_uri(uri) is None
    assert "Deleted 1 entry" in result.output, result.output
    assert "kept" not in result.output, result.output


def _two_chunk_note(writer, client, owner, collection: str, title: str) -> tuple[str, str, list[str]]:
    source_uri = uri_for(collection, title)
    tumbler = str(writer.register(
        owner=owner, title=title, content_type="knowledge",
        physical_collection=collection, source_uri=source_uri,
    ))
    bodies = [f"{title} piece one", f"{title} piece two"]
    chashes = [hashlib.sha256(b.encode()).hexdigest() for b in bodies]
    client.upsert_chunks_with_embeddings(
        collection, ids=chashes, documents=bodies, embeddings=[],
        metadatas=[{"title": title, "chunk_text_hash": c,
                    "indexed_at": datetime.now(UTC).isoformat()} for c in chashes],
    )
    writer.write_manifest(
        tumbler, [{"chash": c, "position": i} for i, c in enumerate(chashes)],
        collection=collection,
    )
    return tumbler, source_uri, chashes


class _FailingWriter:
    """The real writer, with one method made to raise."""

    def __init__(self, real, fail: str) -> None:
        self._real, self._fail = real, fail

    def __getattr__(self, name):
        if name == self._fail:
            def boom(*a, **k):
                raise RuntimeError(f"injected {name} failure")
            return boom
        return getattr(self._real, name)


@pytest.mark.parametrize("fail", ["write_manifest", "delete_document"])
def test_a_failed_reap_leaves_a_multi_chunk_document_whole(t2_service_env, fail):
    """All or nothing per document. Retracting row by row could fail part
    way and leave a live note with some pieces stripped from its manifest
    while the output said it was not deleted. A retraction failure changes
    nothing; a tombstone failure puts the manifest back."""
    import nexus.catalog.factory as factory

    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    col = _coll(f"fail-{fail}")
    tumbler, uri, chashes = _two_chunk_note(writer, client, owner, col, "sis0m5 split")
    real_factory = factory.make_catalog_writer

    with patch("nexus.commands.store._t3", return_value=client), patch.object(
        factory, "make_catalog_writer",
        lambda *a, **k: _FailingWriter(real_factory(*a, **k), fail),
    ):
        result = CliRunner().invoke(main, ["store", "delete", "-c", col, "--title", "sis0m5 split", "-y"])

    assert result.exit_code != 0, result.output
    assert f"document {tumbler} was NOT deleted" in result.output, result.output
    assert reader.by_source_uri(uri) is not None, "the document must stay live"
    assert [r.chash for r in reader.get_manifest(tumbler)] == chashes, "its manifest must be whole"
    assert all(_live(client, col, c) for c in chashes), "its chunks must be untouched"


def test_confirmation_prompt_counts_only_what_the_reap_touches(t2_service_env):
    """A file-backed document with the same title is never reaped by
    --title, so the prompt must not count it."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    col = _coll("prompt")
    _t, uri, _c = _note(writer, client, owner, col, "sis0m5 prompt", "sis0m5 prompt body")
    writer.register(
        owner=owner, title="sis0m5 prompt", content_type="knowledge",
        physical_collection=col, file_path="notes/prompt.md",
    )

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(
            main, ["store", "delete", "-c", col, "--title", "sis0m5 prompt"], input="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "Found 1 document(s)" in result.output, result.output
    assert reader.by_source_uri(uri) is None


def test_a_ghost_titled_document_is_reaped_too(t2_service_env):
    """A ghost has no physical_collection, so list_by_collection never
    returns it; it is found through the manifests of the chunks titled X,
    the owner scope resolve_knowledge_doc_for_chash already uses."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    col = _coll("ghost")
    body = "sis0m5 ghost shared body"
    _tb, ub, chash = _note(writer, client, owner, col, "sis0m5 live twin", body)
    ghost = str(writer.register(owner=owner, title="sis0m5 ghost", content_type="knowledge"))
    writer.write_manifest(ghost, [{"chash": chash, "position": 0}], collection=col)
    client.upsert_chunks_with_embeddings(
        col, ids=[chash], documents=[body], embeddings=[],
        metadatas=[{"title": "sis0m5 ghost", "chunk_text_hash": chash,
                    "indexed_at": datetime.now(UTC).isoformat()}],
    )

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(main, ["store", "delete", "-c", col, "--title", "sis0m5 ghost", "-y"])

    assert result.exit_code == 0, result.output
    assert f"Deleted document 'sis0m5 ghost' ({ghost})" in result.output, result.output
    assert reader.by_source_uri(ub) is not None
    assert _live(client, col, chash)


def test_a_ghost_is_reaped_when_its_twin_wrote_the_chunk_last(t2_service_env):
    """The chunk row's title is its last writer's. With the live twin
    written after the ghost, no chunk is titled with the ghost's title, so
    the ghost must come from the catalog's own title, not from the chunks."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    col = _coll("ghost-first")
    body = "sis0m5 ghost-first shared body"
    ghost = str(writer.register(owner=owner, title="sis0m5 early ghost", content_type="knowledge"))
    chash = hashlib.sha256(body.encode()).hexdigest()
    client.upsert_chunks_with_embeddings(
        col, ids=[chash], documents=[body], embeddings=[],
        metadatas=[{"title": "sis0m5 early ghost", "chunk_text_hash": chash,
                    "indexed_at": datetime.now(UTC).isoformat()}],
    )
    writer.write_manifest(ghost, [{"chash": chash, "position": 0}], collection=col)
    # The twin writes the same chunk last, so its row now carries the twin's title.
    _tb, ub, _c = _note(writer, client, owner, col, "sis0m5 late twin", body)
    row = client.get_collection(col).get(ids=[chash], include=["metadatas"])
    assert row["metadatas"][0]["title"] == "sis0m5 late twin"  # the condition the test names

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(main, ["store", "delete", "-c", col, "--title", "sis0m5 early ghost", "-y"])

    assert result.exit_code == 0, result.output
    assert f"Deleted document 'sis0m5 early ghost' ({ghost})" in result.output, result.output
    assert reader.by_source_uri(ub) is not None
    assert _live(client, col, chash)


def test_the_kept_chunk_message_names_only_this_collections_holders(t2_service_env):
    """chash owner lookups are tenant-wide; a copy of the same text in
    another collection neither protects this chunk nor belongs in the
    "still held by" line."""
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    col, other = _coll("holders-home"), _coll("holders-other")
    body = "sis0m5 body in two collections"
    _note(writer, client, owner, col, "sis0m5 home gone", body)
    _tk, _uk, chash = _note(writer, client, owner, col, "sis0m5 home kept", body)
    t_other, _uo, _co = _note(writer, client, owner, other, "sis0m5 elsewhere", body)

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(main, ["store", "delete", "-c", col, "--title", "sis0m5 home gone", "-y"])

    assert result.exit_code == 0, result.output
    assert "sis0m5 home kept" in result.output, result.output
    assert "sis0m5 elsewhere" not in result.output and t_other not in result.output, result.output
    assert _live(client, col, chash)
