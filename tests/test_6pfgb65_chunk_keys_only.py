# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 4 Step 1 (nexus-6pfgb.65), the writer half: a client sends CHUNK keys only.

The key table (``tables/chunk-metadata-keys.toml``) names two CHUNK keys, ``chunk_text_hash`` and
``embedding_model``, and four engine ROW keys. Every client write route is driven here against the real
writer and what it hands the store is held to those: no document key (title, tags, bibliography,
provenance, lifecycle), no position key (span, page, section) and no legacy key.

The routes are the Step 0 pin's (code, plain prose, markdown and PDF through the repo indexer's three
landings, ``nx index md|pdf``, the streaming PDF pipeline, ``index_pdf`` single-request and incremental),
plus the note write, ``T3Database.put``, the ``.nxexp`` import, the overlays that went through
``update-metadata`` (the streaming PDF post-pass, the table-page tag, the frecency-only reindex, bib
enrichment) and the collection rewrite verb. The manifest rows each route builds are pinned by
``test_6pfgb65_positions_split.py``; its goldens' chunk-row half is these two keys.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.metadata_schema import (
    ALLOWED_TOP_LEVEL,
    POSITION_FIELDS,
    REWRITE_OWNED_KEYS,
    MetadataSchemaError,
    make_chunk_metadata,
    normalize,
    rewrite_delete_keys,
    validate,
)
from nexus.tables import chunk_metadata_keys as ckm
from tests.test_6pfgb65_positions_split import _CASES, _RecordingCat, _observe

_CHUNK = ckm.keys_of("CHUNK")
_ROW = ckm.keys_of("ROW")
_COLL = "knowledge__subject__bge-base-en-v15-768__v1"
_NOT_CHUNK_KEYS = {r.key for r in ckm.rows() if r.kind == "metadata-key"} - _CHUNK - _ROW


def _every_key_row() -> dict:
    return {k: f"v-{k}" for k in sorted(_NOT_CHUNK_KEYS)} | {"made_up_cargo": 1}


def _assert_chunk_keys_only(rows: list[dict], what: str) -> None:
    assert rows, f"{what}: non-vacuity, the route wrote no chunk row"
    for m in rows:
        extra = set(m) - _CHUNK - _ROW
        assert not extra, f"{what} sent non-CHUNK keys {sorted(extra)}"
        assert m.get("chunk_text_hash"), f"{what}: a row without its chash"


# ── the schema ───────────────────────────────────────────────────────────────


def test_the_allowed_keys_are_the_key_tables_chunk_rows() -> None:
    assert ALLOWED_TOP_LEVEL == _CHUNK == {"chunk_text_hash", "embedding_model"}
    assert REWRITE_OWNED_KEYS == ALLOWED_TOP_LEVEL


def test_the_factory_takes_no_document_or_position_argument() -> None:
    params = set(inspect.signature(make_chunk_metadata).parameters)
    assert params == {"content_type", "chunk_text_hash", "embedding_model"}, params
    assert not (params - {"content_type"}) & (_NOT_CHUNK_KEYS | set(POSITION_FIELDS))  # content_type is validated, not written


def test_the_factory_and_normalize_emit_chunk_keys_only() -> None:
    assert make_chunk_metadata(
        content_type="pdf", chunk_text_hash="a" * 64, embedding_model="m"
    ) == {"chunk_text_hash": "a" * 64, "embedding_model": "m"}
    out = normalize({**_every_key_row(), "chunk_text_hash": "a" * 64, "embedding_model": "m"}, content_type="code")
    assert set(out) == _CHUNK
    assert len(_NOT_CHUNK_KEYS) >= 30, "the key table examined too little to mean anything"


def test_validate_refuses_every_key_that_is_not_a_chunk_key() -> None:
    validate({"chunk_text_hash": "a", "embedding_model": "m"})
    for key in sorted(_NOT_CHUNK_KEYS):
        with pytest.raises(MetadataSchemaError):
            validate({"chunk_text_hash": "a", key: "x"})


def test_a_client_batch_names_no_delete_key() -> None:
    """Both keys ride on every row, so a full-rewrite writer has nothing to strip: the retired keys a
    pre-Step-1 row still holds are the Step 3 sweep's, not one document's re-index (the row is shared)."""
    rows = [make_chunk_metadata(content_type="code", chunk_text_hash=c * 64, embedding_model="m") for c in "ab"]
    assert rewrite_delete_keys(rows) == []
    assert rewrite_delete_keys([{"chunk_text_hash": "a"}]) == ["embedding_model"]


# ── the indexing routes ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path,route", _CASES)
def test_every_indexing_route_sends_chunk_keys_only(path, route, tmp_path, monkeypatch) -> None:
    got = _observe(path, route, tmp_path, monkeypatch)
    assert len(got["chunks"]) > 1, "non-vacuity: the fixture must yield several chunks"
    _assert_chunk_keys_only(got["chunks"], f"{path}/{route}")
    assert len(got["rows"]) == len(got["chunks"]), "the manifest rows were built from the position dicts"


# ── notes: write_note and T3Database.put ─────────────────────────────────────


def test_a_note_write_sends_chunk_keys_only(monkeypatch) -> None:
    from nexus import corpus
    from nexus.catalog.note_write import write_note

    monkeypatch.setattr(corpus, "embedding_model_row_preferred", lambda name: "bge-base-en-v1.5")
    rec = _RecordingCat()
    write_note(
        catalog_doc_id="1.1.1", collection=_COLL, pieces=["a note about chunk keys", "and a second piece"],
        title="A title", tags="t1,t2", category="c", source_agent="the-agent", ttl_days=30, cat=rec,
    )
    assert len(rec.chunks) == 2, rec.chunks
    _assert_chunk_keys_only([c["metadata"] for c in rec.chunks], "write_note")
    assert {c["metadata"]["embedding_model"] for c in rec.chunks} == {"bge-base-en-v1.5"}


def test_t3_put_sends_chunk_keys_only() -> None:
    from nexus.db.t3 import T3Database

    client = MagicMock()
    col = MagicMock()
    client.get_or_create_collection.return_value = col
    db = T3Database(tenant="t", database="d", api_key="k", _client=client, _ef_override=MagicMock(name="ef"))
    db.put(
        collection="knowledge__security", content="security finding text", title="sec.md", tags="security,audit",
        category="security", source_agent="an-agent", ttl_days=30,
    )
    rows = col.upsert.call_args.kwargs["metadatas"]
    _assert_chunk_keys_only(rows, "T3Database.put")


# ── the .nxexp import ────────────────────────────────────────────────────────


def test_an_import_chunk_payload_is_the_chunk_keys_of_the_record() -> None:
    from nexus.exporter import _import_chunk_metadata

    got = _import_chunk_metadata(_every_key_row() | {"chunk_text_hash": "a", "embedding_model": "m"})
    assert set(got) == _CHUNK


def test_an_import_puts_a_documents_own_positions_on_its_manifest_row_not_the_chunk_row() -> None:
    from nexus.catalog.multi_document_write import PageWriteResult
    from nexus.exporter import _OwnerImport, _PageRec
    from tests.test_6pfgb45_import_note_keys import _entry, _ImportWriter, _Reader, _Writer

    seen: dict = {}

    class _Capture(_ImportWriter):
        def write_page(self, rows_by_doc, chunks):
            seen["rows"] = {d: [dict(r) for r in rows] for d, rows in rows_by_doc.items()}
            seen["chunks"] = chunks
            self.landed.extend(rows_by_doc)
            return PageWriteResult(written=list(rows_by_doc), landed=list(rows_by_doc))

    entries = {"1.1.5": _entry("1.1.5", source_uri="file:///x/a.md", collection="docs__x__bge-base-en-v15-768__v1")}
    log: list = []
    imp = _OwnerImport(
        db=None, collection_name="docs__x__bge-base-en-v15-768__v1", hooks=SimpleNamespace(fire_store_chains=lambda *a, **k: None),
        embedding_model="m", file_hash="h", skip_existing=False)
    imp._reader = _Reader(entries)
    imp._writer = _Writer(entries, log)
    imp._import_writer = _Capture(log)
    imp._state["1.1.5"] = "write"

    legacy = {  # a record from before Step 1: the last writer's document keys and positions
        "chunk_text_hash": "c0" * 32, "embedding_model": "m", "title": "T", "tags": "x", "ttl_days": 0,
        "chunk_start_char": 10, "chunk_end_char": 99, "line_start": 2, "line_end": 5,
        "section_title": "Intro", "section_type": "abstract", "page_number": 3,
    }
    keyless = {"chunk_text_hash": "c1" * 32, "embedding_model": "m"}  # written since Step 1: no position key
    group = {"doc_id": "1.1.5", "key": "1.1.5", "source_uri": entries["1.1.5"].source_uri}
    imp.flush([
        _PageRec(rec_id="c0" * 32, doc="t0", meta=legacy, emb=[0.0], group=group, position=0),
        _PageRec(rec_id="c1" * 32, doc="t1", meta=keyless, emb=[0.0], group=group, position=1),
    ])

    for payload in seen["chunks"].values():
        assert set(payload["metadata"]) <= _CHUNK, payload["metadata"]
    by_chash = {r["chash"]: r for r in seen["rows"]["1.1.5"]}
    labelled = by_chash["c0" * 32]
    assert (labelled["line_start"], labelled["line_end"], labelled["char_start"], labelled["char_end"]) == (2, 5, 10, 99)
    assert (labelled["section_title"], labelled["section_type"], labelled["page_number"]) == ("Intro", "abstract", 3)
    bare = by_chash["c1" * 32]
    # a record with no position key sends no label: an empty label would be authoritative
    assert not {"section_title", "section_type", "page_number", "char_start", "line_start"} & set(bare), bare


# ── the overlays that went through update-metadata ───────────────────────────


def test_the_frecency_only_reindex_writes_no_chunk_row(tmp_path) -> None:
    from tests.test_6pfgb9_frecency_from_catalog import _Writer, _frecency_only

    w = _Writer()
    db = _frecency_only(tmp_path, w)

    assert sorted(t for t, _ in w.updates) == ["1.1.1", "1.1.2"], "non-vacuity: the catalog took the scores"
    assert db.method_calls == [], db.method_calls  # no handle was opened, let alone written


def test_the_streaming_pipeline_tail_writes_no_chunk_row_and_leaves_the_title_to_the_catalog(tmp_path) -> None:
    """The post-pass that wrote title, author, extraction method and chunk_type onto each row is gone; the
    same facts reach the catalog document through ``_catalog_pdf_hook``."""
    from pathlib import Path

    from nexus.hook_registry import HookRegistry
    from nexus.pipeline_stages import pipeline_index_pdf
    from tests.pipeline_fake_engine import make_fake_engine_db

    pdf = Path(tmp_path / "x.pdf")
    pdf.write_bytes(b"%PDF-1.4 chunk keys\n")
    db, _engine = make_fake_engine_db()
    t3 = MagicMock()
    extraction = MagicMock()
    extraction.text = "# A heading\n\nbody text of the document."
    extraction.metadata = {
        "table_regions": [{"page": 2, "html": "<table/>"}], "extraction_method": "mineru",
        "pdf_title": "The Real Title", "pdf_author": "An Author", "page_count": 2,
    }
    stats: dict = {}
    with patch("nexus.pipeline_stages.extractor_loop", return_value=extraction), \
            patch("nexus.pipeline_stages.chunker_loop", return_value=None), \
            patch("nexus.pipeline_stages.uploader_loop", return_value=0), \
            patch("nexus.doc_indexer._fence_begin"), patch("nexus.doc_indexer._fence_fail"), \
            patch("nexus.pipeline_stages._catalog_pdf_hook") as hook, \
            patch("nexus.catalog.factory.make_catalog_reader", return_value=MagicMock()):
        pipeline_index_pdf(
            pdf, "e" * 64, "knowledge__x", t3, db=db, embed_fn=lambda texts, model: ([[0.1] * 4] * len(texts), model),
            extraction_stats=stats, doc_id="1.9.9", hooks=HookRegistry(),
        )

    hook.assert_called_once()
    assert hook.call_args.kwargs["title"] == "The Real Title" and hook.call_args.kwargs["author"] == "An Author"
    assert (stats["title"], stats["author"]) == ("The Real Title", "An Author")
    t3.update_chunks.assert_not_called()


def test_the_post_pass_writers_are_gone() -> None:
    import nexus.pipeline_stages as ps

    for name in ("_enrich_metadata_from_extraction", "_update_chunk_metadata", "_own_labels_view"):
        assert not hasattr(ps, name), name


def test_the_streaming_stub_row_is_chunk_keys_only() -> None:
    from nexus.pipeline_stages import _build_chunk_metadata

    chunk = SimpleNamespace(text="t", metadata={"page_number": 2, "chunk_start_char": 3})
    assert set(_build_chunk_metadata(chunk, embedding_model="m")) == _CHUNK


# ── the collection rewrite verb ──────────────────────────────────────────────


def test_the_rewrite_verb_strips_every_non_chunk_key_with_no_exemption() -> None:
    from nexus.db.t3 import _rewrite_collection_metadata

    legacy = {
        "chunk_text_hash": "a" * 64, "embedding_model": "m", "title": "t", "bib_year": 2020, "bib_venue": "V",
        "content_type": "pdf", "page_number": 3, "tags": "x", "doc_id": "1.1.1", "made_up_cargo": 1,
    }
    clean = {"chunk_text_hash": "b" * 64, "embedding_model": "m"}
    col = MagicMock()
    col.get.return_value = {"ids": ["a", "b"], "metadatas": [legacy, clean]}
    t3_db = MagicMock()
    t3_db.get_or_create_collection.return_value = col

    updated, skipped, total = _rewrite_collection_metadata(t3_db, _COLL)

    assert (updated, skipped, total) == (1, 1, 2)
    ((_coll, ids, metas), kw) = t3_db.update_chunks.call_args
    assert ids == ["a"] and metas == [{"chunk_text_hash": "a" * 64, "embedding_model": "m"}]
    assert set(kw["delete_keys"]) == set(legacy) - _CHUNK, "bib_* and content_type are no longer exempt"


# ── the readers that moved off the chunk row ─────────────────────────────────


def test_the_pdf_stub_check_judges_only_a_legacy_placeholder_row() -> None:
    from nexus.health import _is_pdf_stub_metadata

    legacy_stub = {"chunk_text_hash": "c", "tags": "pdf", "category": "paper"}  # the streaming stub of before Step 1: no title
    assert _is_pdf_stub_metadata(legacy_stub, document_has_method=False)
    assert not _is_pdf_stub_metadata(legacy_stub, document_has_method=True)
    assert not _is_pdf_stub_metadata({**legacy_stub, "title": "Real"}, document_has_method=False)
    # a row written since Step 1 holds no document key: its lacking a title is no evidence of a stub
    assert not _is_pdf_stub_metadata({"chunk_text_hash": "c", "embedding_model": "m"}, document_has_method=False)


@pytest.mark.usefixtures("cloud_mode")
def test_collection_info_sample_titles_come_from_the_owning_document() -> None:
    from nexus.mcp.core import collection_info

    chash = "d" * 64
    t3 = MagicMock()
    t3.collection_info.return_value = {"count": 1, "metadata": {}}
    t3.list_store.return_value = [{"id": chash, "sensitive": False}]  # a row with no title key
    owner = SimpleNamespace(title="The Owner's Title", tumbler="1.1.1", meta={}, indexed_at="", sensitive=False)
    from nexus import mcp_infra

    mcp_infra.inject_t3(t3)
    try:
        with patch("nexus.catalog.store_hook.live_owner_entries", return_value={chash: [owner]}):
            out = collection_info("knowledge__test")
    finally:
        mcp_infra.inject_t3(None)
    assert "The Owner's Title" in out and "untitled" not in out, out


def test_index_pdf_summary_reads_the_extraction_not_a_row(tmp_path, monkeypatch) -> None:
    """The ``return_metadata`` summary (pages, title, author) of the single-request and incremental
    routes: the pages come from the position dicts, the title and author from the extraction."""
    from tests.test_6pfgb65_positions_split import _drive_index_pdf_route, _install

    for route in ("index_pdf", "incremental"):
        sub = tmp_path / route
        sub.mkdir()
        rec = _install(monkeypatch)
        _drive_index_pdf_route(route, sub, monkeypatch, rec)
        summary = rec.summary
        assert summary["chunks"] > 1 and summary["pages"] == [1, 2, 3], summary
        assert summary["author"] == "Pin Author" and summary["title"], summary


def test_the_streaming_summary_reads_pages_title_and_author_from_the_catalog() -> None:
    from nexus import doc_indexer as di

    rows = [SimpleNamespace(chash="a" * 64, page_number=1), SimpleNamespace(chash="b" * 64, page_number=None),
            SimpleNamespace(chash="c" * 64, page_number=3)]
    reader = MagicMock()
    reader.get_manifest.return_value = rows
    reader.resolve.return_value = SimpleNamespace(title="Catalog Title", author="Catalog Author")
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=reader):
        got = di._catalog_summary_for_doc_id("1.1.1")
    assert got == {"chunks": 3, "pages": [0, 1, 3], "title": "Catalog Title", "author": "Catalog Author"}
    reader.get_manifest.return_value = []
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=reader):
        assert di._catalog_summary_for_doc_id("1.1.1")["chunks"] == 0  # the caller's loud guard sees this


def test_the_markdown_section_count_reads_the_position_dicts(tmp_path, monkeypatch) -> None:
    from nexus.hook_registry import HookRegistry
    from tests.test_6pfgb65_positions_split import _MD_SRC, _install

    import nexus.doc_indexer as di

    _install(monkeypatch)
    f = tmp_path / "notes.md"
    f.write_text(_MD_SRC, encoding="utf-8")

    class _Col:
        def get(self, **kw):
            return {"ids": [], "metadatas": []}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

    rec = _RecordingCat()
    with patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value="1.9.65"), \
            patch("nexus.doc_indexer._doc_id_for_path", return_value=""), \
            patch("nexus.catalog.factory.make_catalog_writer", return_value=rec), \
            patch("nexus.doc_indexer._catalog_markdown_hook"), patch("nexus.doc_indexer._check_document_fork", return_value=[]):
        got = di.index_markdown(
            f, "pin65", _T3(), collection_name="docs__pin65__voyage-context-3__v1",
            embed_fn=lambda texts, model: ([[] for _ in texts], model), force=True, return_metadata=True,
            hooks=HookRegistry(),
        )
    assert got["chunks"] > 1 and got["sections"] >= 3, got


# ── stored rows with no position keys never become authoritative empty labels ─


def test_a_position_dict_with_no_label_key_yields_a_manifest_row_with_no_label_keys() -> None:
    from nexus.mcp_infra import _manifest_chunk_rows
    from nexus.metadata_schema import positions_of_row

    stored_new = {"chunk_text_hash": "a" * 64, "embedding_model": "m"}  # written since Step 1
    stored_legacy = {"chunk_text_hash": "b" * 64, "page_number": 4, "section_title": "S", "section_type": "t",
                     "chunk_start_char": 0, "chunk_end_char": 9, "line_start": 1, "line_end": 2}
    new_row, legacy_row = _manifest_chunk_rows([
        (0, stored_new, {**positions_of_row(stored_new), "chunk_index": 0}),
        (1, stored_legacy, {**positions_of_row(stored_legacy), "chunk_index": 1}),
    ])
    labels = {"section_title", "section_type", "page_number"}
    assert not labels & set(new_row), new_row
    assert (new_row["char_start"], new_row["line_start"]) == (None, None)
    assert (legacy_row["section_title"], legacy_row["section_type"], legacy_row["page_number"]) == ("S", "t", 4)
    assert legacy_row["char_start"] == 0 and legacy_row["char_end"] == 9


def test_the_batch_manifest_hook_sends_no_labels_for_stored_rows_without_positions(monkeypatch) -> None:
    import nexus.mcp_infra as mi
    from tests.test_6pfgb65_positions_split import _DOC, _install

    rec = _install(monkeypatch)
    metas = [{"chunk_text_hash": c * 64, "embedding_model": "m"} for c in "ab"]
    mi.manifest_write_batch_hook(
        [m["chunk_text_hash"] for m in metas], "docs__pin65__voyage-context-3__v1", ["x", "y"], None, metas,
        catalog_doc_id=_DOC)  # positions=None: a caller whose rows are stored rows
    sent = list(rec.rows)
    assert len(sent) == 2, "non-vacuity: the hook wrote the document's rows"
    assert not {"section_title", "section_type", "page_number"} & {k for r in sent for k in r}, sent


def test_a_reembed_does_not_rebuild_a_manifest_from_the_stored_rows() -> None:
    from nexus.commands.collection import _reembed_collection
    import nexus.mcp_infra as mi
    from nexus.hook_registry import HookRegistry

    model = "bge-base-en-v15-768"
    coll = f"knowledge__reembed65__{model}__v1"
    col = MagicMock()
    page = {"ids": ["a" * 64], "documents": ["ta"], "metadatas": [{"chunk_text_hash": "a" * 64, "doc_id": "1.1.1"}]}
    col.count.return_value = 1
    col.get.side_effect = lambda ids=None, limit=None, offset=0, include=None, **kw: (
        page if ids is None and offset == 0 else
        {"ids": ids or [], "documents": ["ta"] if ids else [], "metadatas": []} if ids else
        {"ids": [], "documents": [], "metadatas": []})
    db = MagicMock()
    db.get_collection.return_value = col
    manifest_calls: list = []

    def spy_manifest_hook(ids, collection, contents, embeddings, metadatas, **kw) -> None:
        manifest_calls.append(ids)

    fired: list = []
    # The hook is best-effort and swallows its own errors, so a spy stands in for it: ``_reembed_collection``
    # resolves the function by name at call time and drops that registration from the registry it fires.
    with patch.object(mi, "manifest_write_batch_hook", spy_manifest_hook):
        reg = HookRegistry()
        reg.register_batch(mi.manifest_write_batch_hook)
        reg.register_batch(lambda ids, collection, contents, embeddings, metadatas, **kw: fired.append(ids))
        processed, _skipped = _reembed_collection(db, coll, model, dry_run=False, hooks=reg)
    assert processed == 1 and fired, "non-vacuity: the other batch hooks still fire"
    assert manifest_calls == [], "a re-embed changes no manifest: the manifest hook must not rebuild one"


def test_the_flush_claims_a_document_complete_by_the_contexts_content_hash_not_a_row() -> None:
    """A chunk row carries no ``content_hash`` since Step 1, so the completion claim the flush rides to the engine
    reads the file's hash from the staged context. A context without one claims nothing."""
    from nexus.indexer import _build_combined_write_payload

    def staged(path: str, doc: str, chash: str, content_hash: str):
        return path, {
            "ids": [chash], "documents": ["t"], "metadatas": [{"chunk_text_hash": chash}],
            "positions": [{"line_start": 1}], "content_hash": content_hash, "catalog_doc_id": doc,
        }

    fctx = [staged("a.py", "1.1", "a" * 64, "f" * 64), staged("b.py", "1.2", "b" * 64, "")]
    ids = [i for _p, c in fctx for i in c["ids"]]
    docs = [d for _p, c in fctx for d in c["documents"]]
    metas = [m for _p, c in fctx for m in c["metadatas"]]
    _chunks, full_docs, complete, _orphans = _build_combined_write_payload(ids, docs, metas, fctx)
    assert sorted(d for d, _ in full_docs) == ["1.1", "1.2"], "non-vacuity: both documents are written"
    assert complete == {"1.1": "f" * 64}
