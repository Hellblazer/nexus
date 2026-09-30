# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0ntxj: an index run that fails after its chunk upsert must not leave
chunks that live(c) hides.

Shakeout 7.64.1 Surface B F1 (T2 nexus/shakeout-7.64.1-catalog-2026-09-28):
FootPrintRAGVA 1.82.146/147 were stamped ``index_state=failed`` with their
395 chunks physically stored and no manifest rows. Under engine v0.1.137's
live(c) a chunk with no own-collection manifest owner is invisible to every
read, so the content was stored and unreachable until ``nx catalog
reconcile`` ran by hand.

RDR-223 removes the split write that opens this window (chunks commit
before their owner rows). ``_index_document`` (``nx index md`` / ``rdr`` /
DEVONthink markdown) no longer has the window (nexus-z0o2p.13): its chunks
and owner rows are one request. The other paths keep it until their beads
land, so ``doc_indexer._fence_fail`` (the funnel every index failure path and
the exit handler use) still rebuilds the failed document's manifest from the
chunks it did store. The document stays ``failed``, so the next run
re-indexes it; its stored content stays readable meanwhile.

These tests drive the production ``index_markdown`` entry against the real
engine. The failure is injected after the real write, at the post-store hook
chain, and the two tests that need the old hidden-chunk state (a run killed
between its chunk upload and its owner rows) build it by hand, since the
markdown path can no longer produce it.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.db.http_vector_client import HttpVectorClient

# Not integration-marked (nexus-wbfpw.38): the substrate provisions itself,
# and CI's default selection must run this RDR-192 pin.

_COLLECTION = "docs__ntxj-failed-run__bge-base-en-v15-768__v1"

_BODY = "\n\n".join(
    f"## Section {i}\n\n" + " ".join(f"ntxj failed-run paragraph {i} sentence {j}." for j in range(40))
    for i in range(6)
)


def _doc_for(path) -> object:
    from tests._catalog_fixture_ops import ActiveCatalog

    matches = [
        d for d in ActiveCatalog().all_documents()
        if (d.file_path or "").endswith(path.name)
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _seed_killed_split_run(md, client: HttpVectorClient) -> None:
    """A run killed between its chunk upload and its owner rows, built by hand: the split write
    RDR-223 removed from the markdown path. The document is registered, its fence begun with the
    file's hash, its chunks uploaded with no owner row, and its run stamped failed."""
    import uuid
    from datetime import UTC, datetime

    from nexus.catalog.factory import make_catalog_writer
    from nexus.corpus import index_model_for_collection
    from nexus.doc_indexer import _markdown_chunks, _register_or_lookup_doc_id, _sha256

    path = md.resolve()
    doc_id = _register_or_lookup_doc_id(
        path, "ntxj", content_type="prose", physical_collection=_COLLECTION)
    assert doc_id
    content_hash = _sha256(path)
    prepared = _markdown_chunks(
        path, content_hash, index_model_for_collection(_COLLECTION),
        datetime.now(UTC).isoformat(), "ntxj", doc_id=doc_id)
    assert prepared
    cat = make_catalog_writer()
    try:
        cat.begin_index_run(doc_id, content_hash, uuid.uuid4().hex, _COLLECTION)
        client.upsert_chunks_with_embeddings(
            _COLLECTION, [p[0] for p in prepared], [p[1] for p in prepared],
            [[] for _ in prepared], [p[2] for p in prepared])
        cat.fail_index_run(doc_id, "seeded: the run was killed before its owner rows")
    finally:
        close = getattr(cat, "close", None)
        if close is not None:
            close()


def _stored_and_visible(client: HttpVectorClient) -> tuple[set[str], set[str]]:
    col = client.get_collection(_COLLECTION)
    stored = set(col.get(include=[], include_non_live=True)["ids"])
    visible = set(col.get(ids=sorted(stored), include=[])["ids"]) if stored else set()
    return stored, visible


def test_manifest_failure_after_upsert_leaves_the_chunks_readable(t2_service_env, tmp_path):
    from nexus.doc_indexer import index_markdown

    md = tmp_path / "ntxj-failed-run.md"
    md.write_text(f"# ntxj failed run\n\n{_BODY}\n")
    client = HttpVectorClient(tenant=t2_service_env)

    def _hook_chain_dies(self, *args, **kwargs):
        raise RuntimeError("injected: the hook chain failed after the write")

    with patch("nexus.hook_registry.HookRegistry.fire_batch", _hook_chain_dies), \
            pytest.raises(RuntimeError, match="injected"):
        index_markdown(md, corpus="ntxj", t3=client, collection_name=_COLLECTION)

    doc = _doc_for(md)
    assert doc.index_state == "failed", "the run failed, and the document must say so"

    stored, visible = _stored_and_visible(client)
    assert stored, "control: the upsert ran before the injected failure"
    assert visible == stored, (
        f"{len(stored - visible)} of {len(stored)} stored chunk(s) are hidden: "
        "stored with no manifest owner, invisible under live(c)"
    )
    from nexus.catalog.factory import make_catalog_reader

    manifest = make_catalog_reader().get_manifest(str(doc.tumbler))
    assert {r.chash for r in manifest} == stored


def test_the_next_run_completes_the_failed_document(t2_service_env, tmp_path):
    """The heal keeps the document ``failed``, so the next run re-indexes it
    instead of skipping it as current."""
    from nexus.doc_indexer import index_markdown

    md = tmp_path / "ntxj-rerun.md"
    md.write_text(f"# ntxj rerun\n\n{_BODY}\n")
    client = HttpVectorClient(tenant=t2_service_env)

    def _hook_chain_dies(self, *args, **kwargs):
        raise RuntimeError("injected: the hook chain failed after the write")

    with patch("nexus.hook_registry.HookRegistry.fire_batch", _hook_chain_dies), \
            pytest.raises(RuntimeError):
        index_markdown(md, corpus="ntxj", t3=client, collection_name=_COLLECTION)

    count = index_markdown(md, corpus="ntxj", t3=client, collection_name=_COLLECTION)
    assert count > 0, "a failed document is re-indexed, never skipped as current"
    assert _doc_for(md).index_state == "complete"


def test_doctor_names_unhealed_hidden_chunks_and_reconcile_repairs_them(t2_service_env, tmp_path):
    """A run killed outright never reaches the failure-time heal. The doctor
    row must count that document, and the remedy it names, ``nx catalog
    reconcile``, must make the chunks readable. The document failed on its
    first run, so its meta carries no content_hash; reconcile has to use the
    hash the fence begin recorded."""
    from click.testing import CliRunner

    from nexus.cli import main
    from nexus.health import _check_failed_runs_hidden_chunks

    md = tmp_path / "ntxj-killed.md"
    md.write_text(f"# ntxj killed\n\n{_BODY}\n")
    client = HttpVectorClient(tenant=t2_service_env)
    _seed_killed_split_run(md, client)

    stored, visible = _stored_and_visible(client)
    assert stored and not visible, "control: the chunks are stored and hidden"

    [row] = _check_failed_runs_hidden_chunks()
    assert row.warn and not row.ok, row
    assert row.detail.startswith("1 of 1 failed document(s)"), row.detail

    with patch("nexus.db.make_t3", return_value=client):
        result = CliRunner().invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 1 document(s)" in result.output, result.output

    stored, visible = _stored_and_visible(client)
    assert visible == stored
    [row] = _check_failed_runs_hidden_chunks()
    assert row.ok, row


def test_a_failed_reindex_keeps_the_previous_content_readable(t2_service_env, tmp_path):
    """Control for the heal's scope: a document that completed once and then
    fails a re-index keeps its old manifest, so its old content stays
    readable. The heal must not replace it with the failed run's chunks."""
    from nexus.catalog.factory import make_catalog_reader
    from nexus.doc_indexer import index_markdown

    md = tmp_path / "ntxj-reindex.md"
    md.write_text(f"# ntxj reindex v1\n\n{_BODY}\n")
    client = HttpVectorClient(tenant=t2_service_env)
    assert index_markdown(md, corpus="ntxj", t3=client, collection_name=_COLLECTION)
    doc = _doc_for(md)
    reader = make_catalog_reader()
    assert reader is not None
    before = [r.chash for r in sorted(reader.get_manifest(str(doc.tumbler)), key=lambda r: r.position)]
    assert before

    md.write_text(f"# ntxj reindex v2\n\n{_BODY.replace('sentence', 'clause')}\n")

    def _write_dies(self, *args, **kwargs):
        raise RuntimeError("injected: the combined write failed")

    # RDR-223 (nexus-z0o2p.13): the chunks and the manifest are one request, so a failure
    # before the request leaves the previous version whole. (Before, the new chunks were
    # uploaded first and the manifest failure was injected after them.)
    with patch("nexus.catalog.http_catalog_client.HttpCatalogClient.write_manifest_many", _write_dies), \
            pytest.raises(RuntimeError, match="injected"):
        index_markdown(md, corpus="ntxj", t3=client, collection_name=_COLLECTION)

    after = [r.chash for r in sorted(reader.get_manifest(str(doc.tumbler)), key=lambda r: r.position)]
    assert after == before, "the failed re-index must not replace the readable manifest"
    visible = set(client.get_collection(_COLLECTION).get(ids=before, include=[])["ids"])
    assert visible == set(before)
    assert _doc_for(md).index_state == "failed"
