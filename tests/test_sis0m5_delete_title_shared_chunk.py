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
