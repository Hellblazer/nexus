# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.6 (RDR-236 Phase 1): extraction provenance has its DOCUMENT home in catalog meta.

``extraction_method``, ``quality_gate_overridden`` and ``extraction_source`` are facts about ONE
document's extraction. On a chunk row they are the last writer's: when documents A (degraded) and B
(clean) share a chunk, B's clean re-index sends ``delete_keys=["quality_gate_overridden"]`` over the
rows A still owns, and A's override stamps ``True`` on B's rows. Their home is the catalog document's
``meta``; the writer lands here, the readers move in nexus-6pfgb.16.

Driven through the REAL engine substrate (only the extractor and the chunker are fake), through every
register site the bead names:

* the streaming route and the all-at-once route of ``index_pdf`` (``_catalog_pdf_hook`` from both callers);
* ``record_pdf_extraction_on_document`` (``nx index repo``);
* ``index_markdown`` (``_catalog_markdown_hook``), including DEVONthink's ``extraction_source``.

A meta key is sent with a real value; the one exception is ``quality_gate_overridden``, an explicit
boolean on EVERY index, because the engine merges ``meta`` and never retracts a key by omission, so a
clean re-index could not otherwise clear an old ``True``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.pdf_extractor import ExtractionResult
from tests.integration.test_rdr223_pdf_journey import (
    _COLLECTION,
    _FakeChunker,
    _FakeExtractor,
    _index,
    _lines,
    _reader,
    _register,
    _traffic,
    _write_pdf,
)

pytestmark = [pytest.mark.integration]


class _GateExtractor(_FakeExtractor):
    """The fake extractor, reporting the quality-gate override and the method the test scripts."""

    overridden: bool = False
    method: str = "mineru"

    def extract(self, pdf_path, **kw):
        result = super().extract(pdf_path, **kw)
        result.metadata["extraction_method"] = self.method
        if self.overridden:
            result.metadata["quality_gate_overridden"] = True  # the real gate sets it only when it fires
        return ExtractionResult(text=result.text, metadata=result.metadata)


@pytest.fixture()
def gate(monkeypatch):
    """Route both indexers' extraction through the gate extractor; returns its class to script."""
    ex = type("Extractor", (_GateExtractor,), {"lines": [], "calls": [], "overridden": False, "method": "mineru"})
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    return ex


def _meta(path: Path, marker: str) -> dict:
    doc, _created = _register(path, marker)
    entry = _reader().resolve(doc)
    assert entry is not None
    return dict(entry.meta)


_BOTH = pytest.mark.parametrize("streaming", ["always", "never"], ids=["streaming", "all-at-once"])


@_BOTH
def test_a_clean_index_writes_an_explicit_false_and_the_source(tmp_path, gate, streaming) -> None:
    marker = f"6pfgb6-clean-{streaming}"
    gate.lines = _lines(marker, 4)
    path = _write_pdf(tmp_path, marker)

    assert _index(path, marker, streaming=streaming) == 4

    meta = _meta(path, marker)
    assert meta["quality_gate_overridden"] is False, "absent would read as 'unknown', not 'not overridden'"
    assert meta["extraction_method"] == "mineru"
    assert meta["extraction_source"] == "file"


@_BOTH
def test_an_override_writes_true_and_a_clean_re_index_clears_it(tmp_path, gate, streaming) -> None:
    marker = f"6pfgb6-clear-{streaming}"
    gate.lines = _lines(marker, 4)
    gate.overridden, gate.method = True, "pymupdf"
    path = _write_pdf(tmp_path, marker)
    _index(path, marker, streaming=streaming)
    first = _meta(path, marker)
    assert first["quality_gate_overridden"] is True and first["extraction_method"] == "pymupdf"

    gate.overridden, gate.method = False, "mineru"  # the same document, extracted clean this time
    _index(path, marker, streaming=streaming, force=True)

    meta = _meta(path, marker)
    assert meta["quality_gate_overridden"] is False, "the merge never retracts: the clean run overwrites"
    assert meta["extraction_method"] == "mineru"


@_BOTH
def test_a_degraded_document_keeps_its_override_when_a_clean_one_sharing_a_chunk_is_indexed_after_it(
    tmp_path, gate, streaming,
) -> None:
    """Census row 22 at document grain. A is indexed with the override, then B, which shares one chunk
    with A, is indexed clean. B's post-pass used to send ``delete_keys`` over the shared row and
    erase A's marker; A's catalog meta says what A's extraction was, whatever B wrote."""
    shared = "6pfgb6 row 22 shared page, the same text in A and B"
    a_lines = [shared, *_lines(f"6pfgb6-A-{streaming}", 3)]
    b_lines = [shared, *_lines(f"6pfgb6-B-{streaming}", 3)]
    a_path = _write_pdf(tmp_path, f"6pfgb6-A-{streaming}")
    b_path = _write_pdf(tmp_path, f"6pfgb6-B-{streaming}")

    gate.lines, gate.overridden, gate.method = a_lines, True, "pymupdf"
    _index(a_path, f"6pfgb6-A-{streaming}", streaming=streaming)
    gate.lines, gate.overridden, gate.method = b_lines, False, "mineru"
    _index(b_path, f"6pfgb6-B-{streaming}", streaming=streaming)

    a = _meta(a_path, f"6pfgb6-A-{streaming}")
    b = _meta(b_path, f"6pfgb6-B-{streaming}")
    assert a["quality_gate_overridden"] is True and a["extraction_method"] == "pymupdf", a
    assert b["quality_gate_overridden"] is False and b["extraction_method"] == "mineru", b


def test_the_repo_indexer_record_carries_the_override_and_the_source(tmp_path) -> None:
    """``nx index repo`` records through ``record_pdf_extraction_on_document`` onto the document its
    catalog pass registered; the record is ``extraction_record`` of the extraction stats."""
    from nexus.pipeline_stages import extraction_record, record_pdf_extraction_on_document

    name = "6pfgb6-repo-record"
    path = _write_pdf(tmp_path, name)
    doc, _created = _register(path, name)

    record_pdf_extraction_on_document(
        str(doc), path, _COLLECTION,
        extraction_record({"extraction_method": "pymupdf", "quality_gate_overridden": True}))
    meta = _meta(path, name)
    assert meta["quality_gate_overridden"] is True and meta["extraction_method"] == "pymupdf"
    assert meta["extraction_source"] == "file"

    record_pdf_extraction_on_document(
        str(doc), path, _COLLECTION, extraction_record({"extraction_method": "mineru"}))
    assert _meta(path, name)["quality_gate_overridden"] is False


def test_a_dry_run_writes_no_provenance(tmp_path, gate) -> None:
    """The streaming pipeline and ``index_pdf`` never call the hook on a dry run (nexus-uxg4u): the
    engine sees no catalog request, so no document and no meta."""
    from unittest.mock import MagicMock

    from nexus.db import make_t3
    from nexus.db.inmemory_vector_store import InMemoryVectorClient
    from nexus.doc_indexer import index_pdf
    from nexus.hook_registry import HookRegistry

    marker = "6pfgb6-dry"
    gate.lines, gate.overridden = _lines(marker, 4), True
    path = _write_pdf(tmp_path, marker)
    store = make_t3(_client=InMemoryVectorClient(), _ef_override=MagicMock())
    with _traffic() as t:
        index_pdf(
            path, f"z0o2p11-{marker}", t3=store, collection_name=_COLLECTION, streaming="never",
            embed_fn=lambda texts, model: ([[] for _ in texts], model), hooks=HookRegistry(), dry_run=True)
    assert t.catalog == [], "the dry run sent the catalog nothing"


def _register_that_resolves(monkeypatch) -> None:
    """Make ``register`` behave as it does when it resolves onto an existing row: the engine returns that
    row untouched and merges none of the caller's ``meta``, and the hook is told it was not created."""
    import nexus.catalog.path_ambiguity as pa
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    real = HttpCatalogClient.register

    def register(self, *a, **kw):
        kw.pop("meta", None)
        return real(self, *a, **kw)

    monkeypatch.setattr(HttpCatalogClient, "register", register)
    monkeypatch.setattr(pa, "created_from_register_result", lambda _result: False)


# ── markdown ──────────────────────────────────────────────────────────────────


def _md_collection() -> str:
    return _COLLECTION  # a docs__ collection the engine registers


def _index_md(path: Path, corpus: str, **kw) -> int:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_markdown

    return index_markdown(path, corpus=corpus, t3=HttpVectorClient(), collection_name=_md_collection(), **kw)


def _md_meta(path: Path) -> dict:
    from tests._catalog_fixture_ops import documents_by_file_path

    (row,) = documents_by_file_path(str(path.resolve()))
    return dict(row.meta)


def test_a_plain_markdown_file_records_extraction_source_file(tmp_path) -> None:
    md = tmp_path / "6pfgb6-plain.md"
    md.write_text("# Plain\n\nA page of 6pfgb6 plain markdown prose.\n")

    assert _index_md(md, "6pfgb6-plain") > 0

    meta = _md_meta(md)
    assert meta["extraction_source"] == "file"
    assert "quality_gate_overridden" not in meta and "extraction_method" not in meta, "markdown has neither"


def test_a_devonthink_record_records_its_extraction_source(tmp_path) -> None:
    md = tmp_path / "6pfgb6-dt.md"
    md.write_text("# DT\n\nThe 6pfgb6 text DEVONthink extracted from a record.\n")

    assert _index_md(md, "6pfgb6-dt", extraction_source="dt_content") > 0

    assert _md_meta(md)["extraction_source"] == "dt_content"


def test_the_markdown_hook_records_the_source_on_a_fresh_registration_and_a_resolved_one(
    tmp_path, monkeypatch,
) -> None:
    """The hook's other two legs: it mints the row itself when no pre-flight did, and when its
    ``register`` resolved onto an existing row (the engine returns that row without merging ``meta``)
    the record rides an update."""
    from nexus.doc_indexer import _catalog_markdown_hook

    created = tmp_path / "6pfgb6-created.md"
    created.write_text("# Created\n\nBody.\n")
    _catalog_markdown_hook(created, _md_collection(), "prose", "6pfgb6-hook", 1, extraction_source="dt_content")
    assert _md_meta(created)["extraction_source"] == "dt_content"

    _register_that_resolves(monkeypatch)
    resolved = tmp_path / "6pfgb6-resolved.md"
    resolved.write_text("# Resolved\n\nBody.\n")
    _catalog_markdown_hook(resolved, _md_collection(), "prose", "6pfgb6-hook", 1, extraction_source="dt_ocr")
    assert _md_meta(resolved)["extraction_source"] == "dt_ocr"


def test_the_pdf_hook_records_on_a_fresh_registration_and_a_resolved_one(tmp_path, monkeypatch) -> None:
    from nexus.pipeline_stages import _catalog_pdf_hook, extraction_record

    record = extraction_record({"extraction_method": "pymupdf", "quality_gate_overridden": True})
    created = _write_pdf(tmp_path, "6pfgb6-hook-created")
    _catalog_pdf_hook(
        created, _COLLECTION, title="T", corpus="z0o2p11-6pfgb6-hook-created", chunk_count=1, extraction=record)
    meta = _meta(created, "6pfgb6-hook-created")
    assert meta["quality_gate_overridden"] is True and meta["extraction_source"] == "file"

    _register_that_resolves(monkeypatch)
    resolved = _write_pdf(tmp_path, "6pfgb6-hook-resolved")
    _catalog_pdf_hook(
        resolved, _COLLECTION, title="T", corpus="z0o2p11-6pfgb6-hook-resolved", chunk_count=1, extraction=record)
    assert _meta(resolved, "6pfgb6-hook-resolved")["quality_gate_overridden"] is True
