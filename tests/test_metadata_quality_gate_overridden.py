# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wi1uv round-2 — ``quality_gate_overridden`` chunk provenance.

Records whether a chunk was indexed under ``--allow-degraded-extraction``
after its document failed the post-extraction quality gate
(``nexus.pdf_extractor.assess_extraction_quality``). Round-2 review
(code-review-expert + substantive-critic Critical, both independently,
2026-08-06) found docs/cli-reference.md's claim that this flag persists to
chunk metadata was FALSE — ``metadata_schema.ALLOWED_TOP_LEVEL`` did not
include it, so a deliberately-degraded document was indistinguishable from
a silent failure one shakedown cycle later. This makes the claim true.

Mirrors the ``extraction_method`` provenance-key pattern
(``tests/test_metadata_extraction_method.py``): the ``False`` default is
dropped by :func:`normalize` so the common healthy-extraction chunk never
spends the extra metadata slot.
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


def test_quality_gate_overridden_is_allowed() -> None:
    assert "quality_gate_overridden" in ALLOWED_TOP_LEVEL


def test_allowed_set_still_within_cap() -> None:
    """Adding the provenance key keeps the schema within the hard cap."""
    assert len(ALLOWED_TOP_LEVEL) <= MAX_SAFE_TOP_LEVEL_KEYS


def test_normalize_drops_false_default() -> None:
    """The common case (healthy extraction) never passes this key True —
    absent == not overridden, never spending a metadata slot."""
    out = normalize({"quality_gate_overridden": False}, content_type="pdf")
    assert "quality_gate_overridden" not in out


def test_normalize_drops_missing() -> None:
    out = normalize({"content_hash": "x"}, content_type="pdf")
    assert "quality_gate_overridden" not in out


def test_normalize_keeps_true_value() -> None:
    out = normalize({"quality_gate_overridden": True}, content_type="pdf")
    assert out["quality_gate_overridden"] is True


def test_make_chunk_metadata_defaults_to_false_and_drops_it() -> None:
    """A normal PDF chunk (gate passed, no override) must not carry the key."""
    meta = make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-08-01T00:00:00Z",
        embedding_model="bge-768",
    )
    assert "quality_gate_overridden" not in meta


def test_make_chunk_metadata_stamps_override() -> None:
    meta = make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-08-01T00:00:00Z",
        embedding_model="bge-768",
        quality_gate_overridden=True,
    )
    assert meta["quality_gate_overridden"] is True
    validate(meta)  # stays writeable / under the key cap


@pytest.mark.parametrize("content_type", ["code", "markdown", "prose"])
def test_non_pdf_content_types_never_carry_it(content_type: str) -> None:
    """The gate is PDF-only; no non-PDF caller ever passes this kwarg, and
    the default keeps it out of the written record regardless."""
    meta = make_chunk_metadata(
        content_type=content_type,
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-08-01T00:00:00Z",
        embedding_model="bge-768",
    )
    assert "quality_gate_overridden" not in meta


def test_where_filter_shape_is_a_bool_primitive() -> None:
    """validate() requires primitive values; True/False must be usable in
    a ``where=quality_gate_overridden=true`` filter without a type coercion
    surprise at the T3 read boundary."""
    meta = make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash="a" * 64,
        content_hash="b" * 64,
        indexed_at="2026-08-01T00:00:00Z",
        embedding_model="bge-768",
        quality_gate_overridden=True,
    )
    assert isinstance(meta["quality_gate_overridden"], bool)
