# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.24): an unusable MinerU time limit changes nothing, through the REAL engine.

``pdf.mineru_page_timeout_s`` x ``pdf.mineru_page_batch`` above 240 s is refused where a MinerU
extraction starts. That is after the catalog row is written, so ``nx index pdf X --extractor mineru
--force`` over a complete document flipped it to ``failed``, ran the --force orphan cleanup, and with
another ``--collection`` re-pointed ``physical_collection`` to an empty collection. Each entry now asks
before it registers or writes anything.

Driven on the streaming route (the default for a real PDF: its failure handler is what marks the row
``failed``) through ``doc_indexer.index_pdf`` and the real ``nx index pdf`` command against the engine
substrate ``test_rdr223_pdf_journey.py`` boots. The extractor is that journey's fake, plus the one real
thing a fake would hide: before it yields its pages it runs the start of the REAL
``PDFExtractor._extract_with_mineru``, where the limit is checked, so the fake refuses exactly where
the extractor does. A limit that passes ends that real call at its next line (MinerU "not importable",
swallowed here).
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus import pdf_stack
from nexus.cli import main
from nexus.db import http_vector_client as hvc
from nexus.pdf_extractor import MineruBoundsError, PDFExtractor
from tests.integration.test_rdr223_pdf_journey import (
    _COLLECTION,
    _FakeChunker,
    _FakeExtractor,
    _index,
    _lines,
    _manifest,
    _present,
    _reader,
    _register,
    _sha,
    _write_pdf,
)

pytestmark = [pytest.mark.integration]

_OTHER = "docs__tr1rj24-other__bge-base-en-v15-768__v1"


class _BoundsCheckingExtractor(_FakeExtractor):
    def extract(self, pdf_path, **kwargs):
        try:
            PDFExtractor()._extract_with_mineru(pdf_path, on_page=None)
        except ImportError:
            pass  # past the limit check: the real method meets "MinerU is not importable" next
        return super().extract(pdf_path, **kwargs)


@pytest.fixture()
def indexed(tmp_path, monkeypatch):
    """A complete, indexed PDF under the default limits; ``(path, marker, doc_id, lines, extractor)``."""
    marker = "tr1rj24"
    lines = _lines(marker, 12)
    ex = type("Extractor", (_BoundsCheckingExtractor,), {"lines": lines, "calls": []})
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    monkeypatch.setattr("nexus.pdf_extractor._mineru_do_parse", lambda: None)
    pdf_stack.reset_cache()
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("present", ""))
    path = _write_pdf(tmp_path, marker)
    doc_id, _created = _register(path, marker)
    _index(path, marker, streaming="always")
    assert ex.calls, "non-vacuity: the first index ran the extractor"
    return path, marker, doc_id, lines, ex


def _snapshot(doc_id: str, lines: list[str]):
    entry = _reader().resolve(doc_id)
    assert entry is not None
    return entry.index_state, entry.physical_collection, _manifest(doc_id), _present(_COLLECTION, [_sha(x) for x in lines])


def _over_the_limit(monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.get_mineru_page_timeout_s", lambda repo_root=None: 180)
    monkeypatch.setattr("nexus.config.get_mineru_page_batch", lambda repo_root=None: 2)


def test_the_control_a_forced_reindex_under_usable_limits_runs_the_extractor(indexed) -> None:
    path, marker, doc_id, lines, ex = indexed
    before = _snapshot(doc_id, lines)
    assert before[0] == "complete" and before[1] == _COLLECTION and len(before[3]) == len(lines)
    ran = len(ex.calls)
    _index(path, marker, streaming="always", force=True)
    assert len(ex.calls) == ran + 1, "the --force run reached the extractor"
    assert _snapshot(doc_id, lines) == before


def test_a_forced_reindex_over_the_limit_leaves_the_row_and_the_chunks_alone(indexed, monkeypatch) -> None:
    path, marker, doc_id, lines, ex = indexed
    before = _snapshot(doc_id, lines)
    _over_the_limit(monkeypatch)
    ran = len(ex.calls)

    with pytest.raises(MineruBoundsError, match="pdf.mineru_page_batch"):
        _index(path, marker, streaming="always", force=True)

    assert len(ex.calls) == ran, "refused before the extractor was reached"
    assert _snapshot(doc_id, lines) == before, "index_state, physical_collection, manifest and chunks unchanged"


def test_nx_index_pdf_force_to_another_collection_over_the_limit_changes_nothing(indexed, monkeypatch) -> None:
    """The reporter's command: the row stays ``complete``, stays in its collection, and the other
    collection is never created."""
    path, marker, doc_id, lines, ex = indexed
    before = _snapshot(doc_id, lines)
    _over_the_limit(monkeypatch)

    result = CliRunner().invoke(main, [
        "index", "pdf", str(path), "--extractor", "mineru", "--force", "--collection", _OTHER,
        "--streaming", "always",
    ])

    assert result.exit_code == 1, result.output
    assert "pdf.mineru_page_timeout_s (180 s) x pdf.mineru_page_batch (2) = 360 s" in result.output
    assert _snapshot(doc_id, lines) == before
    assert not hvc.HttpVectorClient().collection_exists(_OTHER), "the other collection was never created"
