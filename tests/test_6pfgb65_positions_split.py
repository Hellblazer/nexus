# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 4 Step 0 (nexus-6pfgb.65): the chunkers hand the manifest builder their position
fields as a dict of their own, so the manifest rows no longer depend on the chunk row carrying them.

THE PIN. For each indexing path (code, plain prose, markdown, PDF) a fixture is indexed through the real
chunker and each real route to the owner-row writer, and what the writer is handed is held equal to
``tests/fixtures/6pfgb65_manifest_goldens.json``: the manifest rows (labels, spans, positions) and the
chunk-row metadata dicts. The goldens were captured from the code before the split and verified against
it (the ``row-has-positions`` half of every case passes on the pre-split tree), so equality means
"unchanged".

Each case runs twice. ``row-has-positions`` is the plain run (the factory still emits the position keys
in this commit). ``row-has-no-positions`` makes ``make_chunk_metadata`` drop the POSITION keys from its
output, which is what Step 1 will do; the manifest rows must still equal the goldens, and the only way
they can is by reading the chunker's own position dict. That half fails with the split reverted.

Routes: ``oversize`` (the file goes through the multi-batch combined writer), ``flush`` (the ChunkBatcher
stages it and the flush builds the owner rows), ``legacy`` (the old per-file upsert, where the batch
manifest hook builds the rows), ``doc`` (``nx index md|pdf``'s ``_index_document``), ``stream`` (the
streaming PDF pipeline's buffer and uploader), and ``index_pdf`` / ``incremental`` (``doc_indexer.index_pdf``
with the streaming pipeline off: one request, and ``_index_pdf_incremental``'s batches).

To refresh the goldens (only when a chunker's output is meant to change): ``NX_PIN_CAPTURE=1 uv run pytest
tests/test_6pfgb65_positions_split.py -k capture -s`` prints them.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from functools import partial
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock, patch

import pytest

from nexus.chunk_batcher import ChunkBatcher
from nexus.db.http_vector_client import HttpVectorClient
from nexus.hook_registry import HookRegistry
from nexus.index_context import IndexContext
from nexus.metadata_schema import POSITION_FIELDS
from tests._module_seam import module_time

_DOC = "1.9.65"
_MODEL = "voyage-context-3"
_CCE = "docs__pin65__voyage-context-3__v1"
_CODE = "code__pin65__voyage-code-3__v1"
_NOW = "2026-11-08T00:00:00"
_GOLDENS = Path(__file__).parent / "fixtures" / "6pfgb65_manifest_goldens.json"


class _RecordingCat:
    """A catalog writer that records the manifest rows and chunk payloads each request carries; any
    other method answers with an empty dict."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.chunks: list[dict] = []
        self.requests = 0

    def begin_index_run(self, doc_id, content_hash, run_id, collection, **kw):
        return {"prior_chashes": [], "prior_count": 0}

    def write_manifest_many(self, docs, *, complete=None, sweep=True, chunks=None, **kw):
        self.requests += 1
        self.rows.extend(docs[0][1])
        self.chunks.extend(chunks or [])
        return {"chunks_written": len(chunks or [])}

    def append_manifest_chunks(self, doc_id, rows, *, chunk_payload=None, sweep_chashes=None, **kw):
        self.requests += 1
        self.rows.extend(rows)
        self.chunks.extend(chunk_payload or [])
        return {"chunks_written": len(chunk_payload or []), "chunks_unreferenced": 0}

    def complete_index_run(self, doc_id, content_hash, chunk_count):
        return {"ok": True}

    def fail_index_run(self, doc_id, error):
        pass

    def close(self) -> None:
        pass

    def __getattr__(self, name):
        return lambda *a, **k: {}


class _Reader:
    """The catalog read handle the batch manifest hook gates on and sweeps with."""

    def get_manifests(self, doc_ids):
        return {}

    def __getattr__(self, name):
        return lambda *a, **k: {}


def _install(monkeypatch) -> _RecordingCat:
    import nexus.doc_indexer as di
    import nexus.mcp_infra as mi

    import nexus.db.http_vector_client as hvc

    rec = _RecordingCat()
    # The writer cuts a document into requests of this many rows; the real cap reads the collection's
    # stats from the engine.
    monkeypatch.setattr(hvc, "per_collection_chunk_cap", lambda *a, **k: 500)
    monkeypatch.setattr(mi, "get_catalog_writer", lambda *a, **k: rec)
    monkeypatch.setattr(mi, "get_catalog", lambda *a, **k: _Reader())
    monkeypatch.setattr(di, "_fence_begin", lambda *a, **k: None)
    monkeypatch.setattr(di, "_fence_fail", lambda *a, **k: None)
    module_time(monkeypatch, "nexus.retry").sleep = lambda *_a, **_k: None
    return rec


def _strip_positions(monkeypatch) -> None:
    """Step 1 in miniature: the factory's output carries no POSITION key."""
    import nexus.metadata_schema as ms

    orig = ms.make_chunk_metadata

    def stripped(**kw):
        return {k: v for k, v in orig(**kw).items() if k not in POSITION_FIELDS}

    monkeypatch.setattr(ms, "make_chunk_metadata", stripped)


def _ctx(repo: Path, *, corpus: str, batcher, db, hooks) -> IndexContext:
    return IndexContext(
        col=None, db=db, voyage_key="", voyage_client=None,
        repo_path=repo, corpus=corpus, embedding_model=_MODEL, now_iso=_NOW,
        chunk_lines=6, force=True, doc_id_resolver=lambda p: _DOC, hooks=hooks, batcher=batcher)


# ── fixtures: one deterministic file per path ────────────────────────────────


def _code_src() -> str:
    parts = ['"""Pin module."""\n\n\n']
    for c in ("Alpha", "Beta", "Gamma"):
        parts.append(f"class {c}:\n    \"\"\"{c} docs.\"\"\"\n\n")
        for m in range(4):
            parts.append(
                f"    def {c.lower()}_{m}(self, x: int) -> int:\n"
                f"        # {c} method {m}: unique body text so each chunk hashes differently\n"
                f"        total = x + {m}\n"
                f"        return total * {len(c) + m}\n\n")
        parts.append("\n")
    parts.append("def free_function() -> str:\n    return \"free\"\n")
    return "".join(parts)


def _prose_src() -> str:
    parts: list[str] = []
    for sec in ("Introduction", "Methods", "Results", "Discussion", "Conclusion"):
        parts.append(f"# {sec}\n")
        for i in range(60):
            parts.append(f"{sec} line {i}: the plain prose fixture says something particular here.\n")
        parts.append("\n")
    return "".join(parts)


_CODE_SRC = _code_src()
_PROSE_SRC = _prose_src()

_MD_SRC = (
    "---\ntitle: Pin note\nauthor: Pin Author\n---\n"
    "# Introduction\n\n"
    + "Intro paragraph one is here, with enough words to matter. " * 6 + "\n\n"
    "## Background\n\n"
    + "Background paragraph two goes on and on about the past. " * 6 + "\n\n"
    "# Methods\n\n"
    + "The methods paragraph describes the procedure in detail. " * 6 + "\n\n"
    "# Results\n\n"
    + "Results paragraph reports the findings of the procedure. " * 6 + "\n"
)

_PDF_TEXT = (
    "# Abstract\n\n"
    + "We study the pinning of chunk positions in manifests. " * 30 + "\n\n"
    "# 1 Introduction\n\n"
    + "Introduction text for the pdf fixture, page one and two. " * 30 + "\n\n"
    "# 2 Methods\n\n"
    + "Methods text for the pdf fixture, page two and three. " * 30 + "\n\n"
    "# References\n\n"
    + "Reference entry text that closes the pdf fixture out. " * 12 + "\n"
)


def _pdf_boundaries() -> list[dict]:
    third = len(_PDF_TEXT) // 3
    return [
        {"page_number": 1, "start_char": 0, "page_text_length": third},
        {"page_number": 2, "start_char": third, "page_text_length": third},
        {"page_number": 3, "start_char": 2 * third, "page_text_length": len(_PDF_TEXT) - 2 * third},
    ]


def _pdf_extraction_result():
    result = MagicMock()
    result.text = _PDF_TEXT
    result.metadata = {
        "extraction_method": "mineru", "page_count": 3, "format": "markdown",
        "page_boundaries": _pdf_boundaries(), "pdf_author": "Pin Author",
    }
    return result


# ── runners: index one fixture through the real chunker ──────────────────────
#
# Each takes the batcher, db and hooks of the route being driven.


def _run_code(tmp_path, monkeypatch, *, batcher, db, hooks) -> None:
    from nexus.code_indexer import index_code_file

    p = tmp_path / "mod.py"
    p.write_text(_CODE_SRC, encoding="utf-8")
    index_code_file(_ctx(tmp_path, corpus=_CODE, batcher=batcher, db=db, hooks=hooks), p)


def _run_prose(tmp_path, monkeypatch, *, batcher, db, hooks) -> None:
    from nexus.prose_indexer import index_prose_file

    p = tmp_path / "notes.txt"
    p.write_text(_PROSE_SRC, encoding="utf-8")
    index_prose_file(_ctx(tmp_path, corpus=_CCE, batcher=batcher, db=db, hooks=hooks), p)


def _run_markdown(tmp_path, monkeypatch, *, batcher, db, hooks) -> None:
    from nexus.prose_indexer import index_prose_file

    p = tmp_path / "notes.md"
    p.write_text(_MD_SRC, encoding="utf-8")
    index_prose_file(_ctx(tmp_path, corpus=_CCE, batcher=batcher, db=db, hooks=hooks), p)


def _run_pdf(tmp_path, monkeypatch, *, batcher, db, hooks) -> None:
    from nexus.indexer import _index_pdf_file

    p = tmp_path / "pin.pdf"
    p.write_bytes(b"%PDF-1.4 pin65\n")
    monkeypatch.setattr("nexus.pipeline_stages.record_pdf_extraction_on_document", lambda *a, **k: None)
    with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
        ext_cls.return_value.extract.return_value = _pdf_extraction_result()
        _index_pdf_file(
            p, tmp_path, _CCE, _MODEL, None, db, "", _NOW, 0.0,
            force=True, embed_fn=lambda texts: [[] for _ in texts],
            doc_id_resolver=lambda p_: _DOC, hooks=hooks, batcher=batcher)


_RUNNERS: dict[str, Callable] = {
    "code": _run_code, "prose": _run_prose, "markdown": _run_markdown, "pdf": _run_pdf,
}


def _manifest_hook_registry() -> HookRegistry:
    import nexus.mcp_infra as mi

    hooks = HookRegistry()
    hooks.register_batch(mi.manifest_write_batch_hook)
    return hooks


def _drive_repo_route(path: str, route: str, tmp_path: Path, monkeypatch, rec: _RecordingCat) -> list[dict]:
    """The three `nx index repo` routes. Returns the chunk metadata dicts the route wrote."""
    from nexus.indexer import _build_combined_write_payload

    run = _RUNNERS[path]
    if route == "oversize":
        # The batcher refuses the file; the service-backed handle takes the combined writer.
        run(tmp_path, monkeypatch, batcher=ChunkBatcher(flush=lambda *a: None, max_chunks=1),
            db=MagicMock(spec=HttpVectorClient), hooks=HookRegistry())
        return [c["metadata"] for c in rec.chunks]
    if route == "flush":
        def flush(collection, ids, docs, metas, file_contexts):
            chunks, full_docs, _complete, _orphans = _build_combined_write_payload(
                ids, docs, metas, file_contexts)
            rec.chunks.extend(chunks)
            for _doc, rows in full_docs:
                rec.rows.extend(rows)

        batcher = ChunkBatcher(flush=flush, max_chunks=300)
        run(tmp_path, monkeypatch, batcher=batcher,
            db=MagicMock(spec=HttpVectorClient), hooks=HookRegistry())
        batcher.drain()
        return [c["metadata"] for c in rec.chunks]
    # legacy: no batcher and a handle that is not service-backed, so the file takes the old upsert
    # and the batch manifest hook builds the rows.
    db = MagicMock()
    run(tmp_path, monkeypatch, batcher=None, db=db, hooks=_manifest_hook_registry())
    (call,) = db.upsert_chunks_with_embeddings.call_args_list
    return list(call.kwargs["metadatas"])


def _drive_doc_route(path: str, tmp_path: Path, monkeypatch, rec: _RecordingCat) -> list[dict]:
    """`nx index md|pdf`: ``_index_document`` over the real ``_markdown_chunks`` / ``_pdf_chunks``."""
    from nexus.doc_indexer import _index_document, _markdown_chunks, _pdf_chunks

    class _Col:
        def get(self, **kw):
            return {"ids": [], "metadatas": []}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

    if path == "markdown":
        f = tmp_path / "notes.md"
        f.write_text(_MD_SRC, encoding="utf-8")
        chunk_fn = partial(_markdown_chunks, doc_id=_DOC, collection_name=_CCE)
    else:
        f = tmp_path / "pin.pdf"
        f.write_bytes(b"%PDF-1.4 pin65\n")
        chunk_fn = partial(_pdf_chunks, doc_id=_DOC, collection_name=_CCE)
    with patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value=_DOC), \
            patch("nexus.doc_indexer._doc_id_for_path", return_value=""), \
            patch("nexus.doc_indexer.PDFExtractor") as ext_cls, \
            patch("nexus.catalog.factory.make_catalog_writer", return_value=rec):
        ext_cls.return_value.extract.return_value = _pdf_extraction_result()
        _index_document(
            f, "pin65", chunk_fn, _T3(), collection_name=_CCE,
            embed_fn=lambda texts, model: ([[] for _ in texts], model),
            hooks=HookRegistry(), force=True, doc_id=_DOC)
    return [c["metadata"] for c in rec.chunks]


def _drive_index_pdf_route(route: str, tmp_path: Path, monkeypatch, rec: _RecordingCat) -> list[dict]:
    """`nx index pdf`'s library entry, ``index_pdf``, with the streaming pipeline off: ``index_pdf`` (a
    document of up to ``_INCREMENTAL_THRESHOLD`` chunks goes as one request) and ``incremental`` (a
    document over it goes through ``_index_pdf_incremental``, in batches). The fixture is small, so the
    incremental case lowers the threshold and the batch size; the writer is the real one."""
    import nexus.doc_indexer as di

    if route == "incremental":
        monkeypatch.setattr(di, "_INCREMENTAL_THRESHOLD", 2)
        monkeypatch.setattr(di, "_INCREMENTAL_BATCH_SIZE", 3)
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", tmp_path / "ckpt")
    monkeypatch.setattr("nexus.pdf_stack.require", lambda *a, **k: None)
    monkeypatch.setattr("nexus.pdf_stack.mineru_bounds_problem", lambda *a, **k: None)
    monkeypatch.setattr("nexus.sensitive_write.external_legs_gate", lambda *a, **k: False)
    # The catalog enrichment after the write (title, author, chunk count) is no part of what is pinned.
    monkeypatch.setattr("nexus.pipeline_stages._catalog_pdf_hook", lambda *a, **k: None)

    class _Col:
        def get(self, **kw):
            return {"ids": [], "metadatas": []}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

    f = tmp_path / "pin.pdf"
    f.write_bytes(b"%PDF-1.4 pin65\n")
    with patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value=_DOC), \
            patch("nexus.doc_indexer._doc_id_for_path", return_value=""), \
            patch("nexus.doc_indexer._resolve_write_db", side_effect=lambda t3: t3), \
            patch("nexus.doc_indexer._register_before_read"), \
            patch("nexus.doc_indexer._lookup_existing_doc_id", return_value=_DOC), \
            patch("nexus.catalog.factory.make_catalog_reader", return_value=_Reader()), \
            patch("nexus.doc_indexer._target_model", return_value=(_MODEL, True)), \
            patch("nexus.doc_indexer.PDFExtractor") as ext_cls, \
            patch("nexus.catalog.factory.make_catalog_writer", return_value=rec):
        ext_cls.return_value.extract.return_value = _pdf_extraction_result()
        di.index_pdf(
            f, "pin65", _T3(), collection_name=_CCE,
            embed_fn=lambda texts, model: ([[] for _ in texts], model),
            hooks=HookRegistry(), force=True, streaming="never")
    # Non-vacuity of the route: one request for the single-request path, several for the batched one.
    assert (rec.requests > 1) == (route == "incremental"), rec.requests
    return [c["metadata"] for c in rec.chunks]


def _drive_stream_route(
    tmp_path: Path, monkeypatch, rec: _RecordingCat, *, rewrite_buffer: Callable[[dict], None] | None = None,
) -> list[dict]:
    """The streaming PDF pipeline: ``chunker_loop`` buffers the chunks, ``uploader_loop`` sends them.

    *rewrite_buffer*, when given, is handed the buffer's chunk rows (``(pipeline, index) -> row``) between
    the two, to turn them into a buffer an older client wrote."""
    from nexus.pipeline_stages import chunker_loop, uploader_loop
    from tests._owner_write_double import install_streaming_writer
    from tests.pipeline_fake_engine import make_fake_engine_db

    monkeypatch.setattr("nexus.pipeline_stages._POLL_INTERVAL", 0.01)
    writer = install_streaming_writer(monkeypatch)
    db, _engine = make_fake_engine_db()
    h = "pin65" + "0" * 59
    db.create_pipeline(h, "/pin.pdf", _CCE)
    pages = [_PDF_TEXT[b["start_char"]: b["start_char"] + b["page_text_length"]] for b in _pdf_boundaries()]
    for i, text in enumerate(pages):
        db.write_page(h, i, text, metadata={"page_number": i + 1, "text_length": len(text)})
    db.update_progress(h, total_pages=len(pages), pages_extracted=len(pages))
    extraction_done, chunking_done = threading.Event(), threading.Event()
    extraction_done.set()
    chunker_loop(
        h, db, threading.Event(), embed_fn=lambda texts, model: ([[0.1] * 4 for _ in texts], model),
        extraction_done=extraction_done, chunking_done=chunking_done, pdf_path="/pin.pdf",
        corpus=_CCE, target_model=_MODEL)
    if rewrite_buffer is not None:
        db.flush(h)  # the client batches chunk writes; the engine's rows are the buffer
        rewrite_buffer(_engine.chunks)
    uploader_loop(
        h, db, MagicMock(), _CCE, threading.Event(), chunking_done, catalog_doc_id=_DOC,
        hooks=HookRegistry())
    (w,) = writer.instances
    for rows, chunks in w.batches:
        rec.rows.extend(rows)
        rec.chunks.extend(chunks)
    return [c["metadata"] for c in rec.chunks]


_CASES = (
    [(p, r) for p in sorted(_RUNNERS) for r in ("oversize", "flush", "legacy")]
    + [("markdown", "doc"), ("pdf", "doc"), ("pdf", "stream"), ("pdf", "index_pdf"), ("pdf", "incremental")]
)


def _observe(
    path: str, route: str, tmp_path: Path, monkeypatch, *, strip: bool,
    rewrite_buffer: Callable[[dict], None] | None = None,
) -> dict:
    """What the owner-row writer is handed for *path* indexed by *route*."""
    rec = _install(monkeypatch)
    if strip:
        _strip_positions(monkeypatch)
    if route == "doc":
        metas = _drive_doc_route(path, tmp_path, monkeypatch, rec)
    elif route == "stream":
        metas = _drive_stream_route(tmp_path, monkeypatch, rec, rewrite_buffer=rewrite_buffer)
    elif route in ("index_pdf", "incremental"):
        metas = _drive_index_pdf_route(route, tmp_path, monkeypatch, rec)
    else:
        metas = _drive_repo_route(path, route, tmp_path, monkeypatch, rec)
    # Two routes stamp indexed_at from the wall clock; it is no part of what is pinned.
    return {"rows": rec.rows, "chunks": [{**m, "indexed_at": "T"} for m in metas]}


def _goldens() -> dict:
    return json.loads(_GOLDENS.read_text(encoding="utf-8"))


@pytest.mark.skipif(not os.environ.get("NX_PIN_CAPTURE"), reason="golden capture only")
@pytest.mark.parametrize("path,route", _CASES)
def test_capture(path, route, tmp_path, monkeypatch) -> None:
    obs = _observe(path, route, tmp_path, monkeypatch, strip=False)
    sys.stdout.write("\nGOLDEN " + f"{path}/{route} " + json.dumps(obs, sort_keys=True) + "\n")


@pytest.mark.parametrize("strip", [False, True], ids=["row-has-positions", "row-has-no-positions"])
@pytest.mark.parametrize("path,route", _CASES)
def test_manifest_rows_equal_their_pre_split_values(path, route, strip, tmp_path, monkeypatch) -> None:
    gold = _goldens()[f"{path}/{route}"]
    got = _observe(path, route, tmp_path, monkeypatch, strip=strip)

    assert len(gold["rows"]) > 1, "non-vacuity: the fixture must yield several chunks"
    assert got["rows"] == gold["rows"]
    # The chunk rows are unchanged too (Step 0 changes the data flow, not what is written); in the
    # stripped run they are the goldens less the POSITION keys.
    expected_chunks = [
        {k: v for k, v in m.items() if not (strip and k in POSITION_FIELDS)} for m in gold["chunks"]
    ]
    assert got["chunks"] == expected_chunks


def test_every_path_and_route_has_a_golden() -> None:
    """A case with no golden would KeyError, but a golden with no case would pin nothing: hold the two
    sets equal."""
    assert set(_goldens()) == {f"{p}/{r}" for p, r in _CASES}


def test_position_fields_are_the_tables_position_keys() -> None:
    from nexus.tables.chunk_metadata_keys import keys_of

    assert set(POSITION_FIELDS) == keys_of("POSITION")


def test_the_position_dicts_the_chunkers_return_equal_what_the_row_carries(tmp_path) -> None:
    """Until Step 1 the row still carries the position keys, and the chunker's own dict agrees with
    them for every key it sets (this is what lets Step 1 drop the keys from the row without moving a
    manifest value)."""
    from nexus.doc_indexer import _markdown_chunks
    from nexus.metadata_schema import positions_of_row

    f = tmp_path / "notes.md"
    f.write_text(_MD_SRC, encoding="utf-8")
    prepared = _markdown_chunks(f, "h" * 64, _MODEL, _NOW, "pin65", collection_name=_CCE)
    assert len(prepared) > 1 and all(len(p) == 4 for p in prepared)
    for _cid, _text, meta, positions in prepared:
        assert positions_of_row(meta) == positions


def _carries_positions(golden: dict) -> bool:
    """True when the golden's rows hold at least one label (a section title or type, or a page) and at
    least one span (a line or character bound): a golden regenerated from a tree whose positions were
    lost holds neither, and would pin the loss."""
    rows = golden["rows"]
    labelled = any(r["section_title"] or r["section_type"] or r["page_number"] is not None for r in rows)
    spanned = any(
        r[k] is not None for r in rows for k in ("line_start", "line_end", "char_start", "char_end"))
    return labelled and spanned


@pytest.mark.parametrize("key", sorted(_goldens()))
def test_every_golden_carries_labels_and_spans(key) -> None:
    """The pin proves a route unchanged only against goldens that hold positions. Regenerating one with
    ``NX_PIN_CAPTURE`` on a tree that dropped the position keys yields all-null rows, which every route
    then "matches"; this refuses such a golden."""
    assert _carries_positions(_goldens()[key])


def test_an_all_null_golden_is_refused() -> None:
    nulled = {"rows": [
        {"section_title": "", "section_type": "", "page_number": None,
         "line_start": None, "line_end": None, "char_start": None, "char_end": None}] * 3}
    assert not _carries_positions(nulled)


def _drop_buffered_positions(every: int) -> Callable[[dict], None]:
    """A buffer as a client from before the split wrote it: the position fields live in the chunk row's
    own metadata and there is no ``_positions`` key. Rewrites every *every*-th buffered row so."""
    def rewrite(chunks: dict) -> None:
        dropped = 0
        for (_pid, idx), row in sorted(chunks.items()):
            if idx % every:
                continue
            meta = json.loads(row["metadata_json"])
            assert "_positions" in meta and "chunk_end_char" in meta, "non-vacuity: the buffer row is new-shaped"
            del meta["_positions"]
            row["metadata_json"] = json.dumps(meta)
            dropped += 1
        assert dropped, "non-vacuity: no buffered row was rewritten"
    return rewrite


@pytest.mark.parametrize("every", [1, 2], ids=["old-buffer", "mixed-buffer"])
def test_a_buffer_written_before_the_split_resumes_to_the_same_manifest_rows(every, tmp_path, monkeypatch) -> None:
    """A streaming-PDF run that was interrupted by an upgrade resumes from a buffer whose rows carry no
    ``_positions`` key (all of them, or some). The uploader reads their positions off the row metadata,
    so the manifest rows are the stream golden's (RDR-236 Phase 4 Step 0). Without that fallback the rows
    would carry empty labels and null spans."""
    gold = _goldens()["pdf/stream"]
    got = _observe("pdf", "stream", tmp_path, monkeypatch, strip=False,
                   rewrite_buffer=_drop_buffered_positions(every))
    assert len(gold["rows"]) > 1
    assert got["rows"] == gold["rows"]
    assert got["chunks"] == gold["chunks"]


def test_the_flush_payload_refuses_position_dicts_that_do_not_line_up_with_the_rows(monkeypatch) -> None:
    """The flush pairs each aggregated chunk row with its aggregated position dict. A short list would,
    paired by a bare ``zip``, drop the last chunks from the manifest without a word: the pairing is
    strict, so the mismatch raises."""
    import nexus.indexer as ix

    ids = ["a" * 64, "b" * 64, "c" * 64]
    metas = [{"chunk_text_hash": i} for i in ids]
    fctx = [("a.py", {
        "ids": ids, "documents": ["x", "y", "z"], "metadatas": metas,
        "positions": [{"line_start": 1}, {"line_start": 2}, {"line_start": 3}], "catalog_doc_id": "1.1"})]
    ix._build_combined_write_payload(ids, ["x", "y", "z"], metas, fctx)  # aligned: builds
    real = ix._aggregate_flush_positions
    monkeypatch.setattr(ix, "_aggregate_flush_positions", lambda fc: real(fc)[:-1])
    with pytest.raises(ValueError):
        ix._build_combined_write_payload(ids, ["x", "y", "z"], metas, fctx)


def test_the_manifest_hook_never_propagates_a_position_list_that_does_not_line_up(monkeypatch) -> None:
    """``manifest_write_batch_hook`` is documented never to raise. A position list of the wrong length
    cannot be paired with the rows; the hook writes nothing, records the batch as a manifest drop for the
    run summary, and returns."""
    import nexus.mcp_infra as mi

    class _MustNotWrite:
        def __getattr__(self, name):
            raise AssertionError(f"the hook reached the catalog writer ({name}) with misaligned positions")

    drops: list[tuple] = []
    monkeypatch.setattr(mi, "get_catalog_writer", lambda *a, **k: _MustNotWrite())
    monkeypatch.setattr(mi, "_record_manifest_identity_drop", lambda *a, **k: drops.append(a))
    metas = [{"chunk_text_hash": "a" * 64}, {"chunk_text_hash": "b" * 64}]
    mi.manifest_write_batch_hook(
        ["a" * 64, "b" * 64], _CCE, ["x", "y"], None, metas,
        catalog_doc_id=_DOC, positions=[{"line_start": 1}])
    assert drops == [(_CCE, 2)]
