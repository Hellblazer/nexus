# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-r3cdg: deleting a note in its home collection must work after the
note was imported into ANOTHER collection.

Shakeout 7.64.1 Surface F F2 (T2 nexus/shakeout-7.64.1-local-driver-2026-09-28):
``.nxexp`` import gives the target collection its own copy document, whose
manifest names the same chash as the original. The delete path resolved the
chash to its owning document catalog-wide, saw two owners, called it
ambiguous and reaped nothing. The original's manifest row then protected the
chunk, so ``nx store delete --title`` printed "Deleted 0 entries" and
exited 0 with the note still live.

The engine's delete anti-join and live(c) are both scoped to the chunk's own
collection, so an owner in another collection never protects the chunk. The
reap must use the same scope. These tests assert the consequences (the
document is gone, the chunk is gone, the copy survives), not a log line.
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
from nexus.errors import CollectionNotFoundError
from nexus.exporter import export_collection, import_collection
from nexus.mcp.core import store_delete

pytestmark = pytest.mark.integration

_MODEL = "bge-base-en-v15-768"


def _coll(name: str) -> str:
    return f"knowledge__r3cdg-{name}__{_MODEL}__v1"


def _note(writer, client, owner, collection: str, title: str, content: str) -> tuple[str, str, str]:
    """A store_put-shaped note: knowledge content type, no file_path, one
    chunk, one manifest row. Returns ``(tumbler, source_uri, chash)``."""
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


def _exported_and_imported(client, tmp_path, src: str, dst: str) -> None:
    out = tmp_path / "r3cdg.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)
    result = import_collection(db=client, input_path=out, target_collection=dst)
    assert result["owned_count"] >= 1


def _live_in(client, collection: str, chash: str) -> bool:
    try:
        return chash in client.get_collection(collection).get(ids=[chash], include=[])["ids"]
    except CollectionNotFoundError:
        # Deleting the last chunk leaves the collection with no rows.
        return False


def _assert_home_gone_copy_kept(reader, client, *, src, dst, uri, chash) -> None:
    assert reader.by_source_uri(uri) is None, "the home document must be tombstoned"
    assert not _live_in(client, src, chash), "the chunk must leave the home collection"
    copy = reader.by_source_uri(f"nxexp://{dst}/{uri}")
    assert copy is not None, "the imported copy is another collection's document and must survive"
    assert [r.chash for r in reader.get_manifest(str(copy.tumbler))] == [chash]
    assert _live_in(client, dst, chash), "the imported copy's chunk must stay live"


def test_cli_delete_by_title_after_cross_collection_import(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    src, dst = _coll("cli-src"), _coll("cli-dst")
    _, uri, chash = _note(writer, client, owner, src, "r3cdg cli note", "r3cdg cli note body")
    _exported_and_imported(client, tmp_path, src, dst)

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(
            main, ["store", "delete", "-c", src, "--title", "r3cdg cli note", "-y"],
        )

    assert result.exit_code == 0, result.output
    assert "Deleted 1 entry" in result.output, result.output
    assert "retained" not in result.output, result.output
    _assert_home_gone_copy_kept(reader, client, src=src, dst=dst, uri=uri, chash=chash)


def test_cli_delete_by_id_after_cross_collection_import(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    src, dst = _coll("id-src"), _coll("id-dst")
    _, uri, chash = _note(writer, client, owner, src, "r3cdg id note", "r3cdg id note body")
    _exported_and_imported(client, tmp_path, src, dst)

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(main, ["store", "delete", "-c", src, "--id", chash])

    assert result.exit_code == 0, result.output
    _assert_home_gone_copy_kept(reader, client, src=src, dst=dst, uri=uri, chash=chash)


def test_mcp_store_delete_after_cross_collection_import(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    src, dst = _coll("mcp-src"), _coll("mcp-dst")
    _, uri, chash = _note(writer, client, owner, src, "r3cdg mcp note", "r3cdg mcp note body")
    _exported_and_imported(client, tmp_path, src, dst)

    with patch("nexus.mcp.core._get_t3", return_value=client):
        out = store_delete(chash, collection=src)

    assert out == f"Deleted: {chash} from {src}", out
    _assert_home_gone_copy_kept(reader, client, src=src, dst=dst, uri=uri, chash=chash)


def test_same_collection_shared_chash_is_still_retained(t2_service_env):
    """Control: the scoping must not weaken the within-collection rule. Two
    notes in ONE collection sharing a chash stay ambiguous; neither is
    reaped and the chunk stays (nexus-dleg's documented contract)."""
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    col = _coll("shared")
    body = "r3cdg shared body"
    _, uri_a, chash = _note(writer, client, owner, col, "r3cdg twin a", body)
    _, uri_b, _ = _note(writer, client, owner, col, "r3cdg twin b", body)

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(main, ["store", "delete", "-c", col, "--title", "r3cdg twin b", "-y"])

    assert "Deleted 0 entries" in result.output, result.output
    assert reader.by_source_uri(uri_a) is not None
    assert reader.by_source_uri(uri_b) is not None
    assert _live_in(client, col, chash)
