# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1oguj — ``extraction_method`` chunk provenance.

Records which PDF-extraction backend produced a chunk's text
(``docling`` | ``mineru`` | ``pymupdf_normalized``, or the honest
degraded-aggregate ``mineru+docling-degraded`` when RDR-148 Gap 5's
per-page OOM-degrade fired). Before this fix the value was computed at
extraction time (asserted in ``test_pdf_subsystem.py`` /
``test_mineru_extractor.py``) and then discarded before storage — no
chunk or catalog document said which extractor produced it, so extractor
quality (e.g. nexus-gtltb's mineru-corruption blast radius) could not be
scoped from the data.

Mirrors the RDR-139 ``extraction_source`` provenance-key pattern
(``tests/test_metadata_extraction_source.py``): empty default is dropped
by :func:`normalize` so non-PDF chunks (markdown/code/prose) never spend
the extra metadata slot.
"""
from __future__ import annotations

import pytest

from nexus.metadata_schema import (
    ALLOWED_TOP_LEVEL,
    MAX_SAFE_TOP_LEVEL_KEYS,
    make_chunk_metadata,
    normalize,
    validate,
)


def test_extraction_method_is_allowed() -> None:
    assert "extraction_method" in ALLOWED_TOP_LEVEL


def test_allowed_set_still_within_cap() -> None:
    """Adding the provenance key keeps the schema within the hard cap."""
    assert len(ALLOWED_TOP_LEVEL) <= MAX_SAFE_TOP_LEVEL_KEYS


def test_normalize_drops_empty_default() -> None:
    """Non-PDF chunks never pass ``extraction_method`` — absent == unknown."""
    out = normalize({"extraction_method": ""}, content_type="markdown")
    assert "extraction_method" not in out


def test_normalize_drops_missing() -> None:
    out = normalize({"content_hash": "x"}, content_type="code")
    assert "extraction_method" not in out


@pytest.mark.parametrize(
    "method",
    ["docling", "mineru", "pymupdf_normalized", "mineru+docling-degraded"],
)
def test_normalize_keeps_real_extractor_value(method: str) -> None:
    out = normalize({"extraction_method": method}, content_type="pdf")
    assert out["extraction_method"] == method


def test_make_chunk_metadata_defaults_to_empty_and_drops_it() -> None:
    """Markdown/code/prose callers never pass extraction_method — the
    empty default must not survive into the written record."""
    meta = make_chunk_metadata(
        content_type="code",
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-08-01T00:00:00Z",
        embedding_model="voyage-code-3",
    )
    assert "extraction_method" not in meta


def test_make_chunk_metadata_stamps_extractor_identity() -> None:
    meta = make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-08-01T00:00:00Z",
        embedding_model="voyage-context-3",
        extraction_method="mineru",
    )
    assert meta["extraction_method"] == "mineru"
    validate(meta)  # stays writeable / under the key cap


def test_make_chunk_metadata_stamps_degraded_aggregate() -> None:
    """The honest mixed-extractor value survives the factory + normalize
    round-trip unchanged — no truncation/rewriting of the compound name."""
    meta = make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-08-01T00:00:00Z",
        embedding_model="voyage-context-3",
        extraction_method="mineru+docling-degraded",
    )
    assert meta["extraction_method"] == "mineru+docling-degraded"
    validate(meta)
