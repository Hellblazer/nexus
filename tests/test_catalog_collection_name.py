# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-103 Phase 1: CollectionName tuple type + CANONICAL_EMBEDDING_MODELS.

Tests pin the canonical-name contract:
    <content_type>__<owner_id>__<embedding_model>__v<n>

CollectionName is the in-process value object; the catalog renders it
into a physical T3 collection name. CollectionName.parse is strict: it
rejects legacy 2-segment names (RDR-101 grandfathering reads them as
strings, not as CollectionName instances) and unknown embedding models.
"""
from __future__ import annotations

import pytest

from nexus.catalog.collection_name import CollectionName
from nexus.corpus import (
    CANONICAL_EMBEDDING_MODELS,
    is_conformant_collection_name,
)


# ── Constant: CANONICAL_EMBEDDING_MODELS ────────────────────────────────────

def test_canonical_embedding_models_is_frozenset() -> None:
    assert isinstance(CANONICAL_EMBEDDING_MODELS, frozenset)


def test_canonical_embedding_models_string_members() -> None:
    for m in CANONICAL_EMBEDDING_MODELS:
        assert isinstance(m, str)


def test_canonical_embedding_models_includes_voyage_context_3() -> None:
    assert "voyage-context-3" in CANONICAL_EMBEDDING_MODELS


def test_canonical_embedding_models_includes_voyage_code_3() -> None:
    assert "voyage-code-3" in CANONICAL_EMBEDDING_MODELS


def test_canonical_embedding_models_excludes_legacy_voyage_3() -> None:
    """Pre-canonical-set ``voyage-3`` must not be treated as canonical.

    Pinned decision #1: the migration uses the indexer's CURRENT canonical
    model rather than the model parsed out of legacy collection names.
    Including ``voyage-3`` here would let legacy names round-trip through
    CollectionName.parse and re-emerge as conformant, defeating the
    migration's invariant.
    """
    assert "voyage-3" not in CANONICAL_EMBEDDING_MODELS


# ── CollectionName: shape ─────────────────────────────────────────────────

def test_collection_name_is_frozen_dataclass() -> None:
    """Frozen so CollectionName is hashable and safe to use as a dict key."""
    name = CollectionName(
        content_type="code",
        owner_id="nexus-abc12345",
        embedding_model="voyage-code-3",
        model_version=1,
    )
    with pytest.raises(Exception):
        name.content_type = "docs"  # type: ignore[misc]


def test_collection_name_is_hashable() -> None:
    a = CollectionName("code", "nexus-abc12345", "voyage-code-3", 1)
    b = CollectionName("code", "nexus-abc12345", "voyage-code-3", 1)
    assert hash(a) == hash(b)
    assert {a, b} == {a}


def test_collection_name_equality_by_value() -> None:
    a = CollectionName("code", "nexus-abc12345", "voyage-code-3", 1)
    b = CollectionName("code", "nexus-abc12345", "voyage-code-3", 1)
    c = CollectionName("code", "nexus-abc12345", "voyage-code-3", 2)
    assert a == b
    assert a != c


# ── render() ─────────────────────────────────────────────────────────────

def test_render_produces_four_segment_name() -> None:
    name = CollectionName("code", "nexus-abc12345", "voyage-code-3", 1)
    assert name.render() == "code__nexus-abc12345__voyage-code-3__v1"


def test_render_higher_version() -> None:
    name = CollectionName("docs", "nexus-abc12345", "voyage-context-3", 7)
    assert name.render() == "docs__nexus-abc12345__voyage-context-3__v7"


def test_render_for_each_content_type() -> None:
    for ct in ("code", "docs", "rdr", "knowledge"):
        name = CollectionName(ct, "owner1", "voyage-code-3", 1)
        assert name.render() == f"{ct}__owner1__voyage-code-3__v1"


def test_render_for_tumbler_owner_id() -> None:
    """Tumbler-style owner IDs (e.g. ``1.1``) must arrive with dots
    replaced by hyphens; render() does not transform the segment.
    """
    name = CollectionName("knowledge", "1-1", "voyage-context-3", 1)
    assert name.render() == "knowledge__1-1__voyage-context-3__v1"


# ── parse() ──────────────────────────────────────────────────────────────

def test_parse_round_trip_each_content_type() -> None:
    for ct in ("code", "docs", "rdr", "knowledge"):
        original = CollectionName(ct, "nexus-abc12345", "voyage-code-3", 1)
        parsed = CollectionName.parse(original.render())
        assert parsed == original


def test_parse_round_trip_voyage_context_3() -> None:
    original = CollectionName("docs", "nexus-abc12345", "voyage-context-3", 3)
    parsed = CollectionName.parse(original.render())
    assert parsed == original


def test_parse_returns_int_model_version() -> None:
    parsed = CollectionName.parse("code__nexus-abc12345__voyage-code-3__v1")
    assert parsed.model_version == 1
    assert isinstance(parsed.model_version, int)


def test_parse_higher_version() -> None:
    parsed = CollectionName.parse("docs__owner1__voyage-context-3__v42")
    assert parsed.model_version == 42


def test_parse_rejects_legacy_two_segment_name() -> None:
    """Pinned decision #4: parse raises on legacy/unknown shapes.

    Generic callers must gate with ``is_conformant_collection_name`` first.
    """
    with pytest.raises(ValueError):
        CollectionName.parse("docs__nexus-571b8edd")


def test_parse_rejects_fallback_default_name() -> None:
    with pytest.raises(ValueError):
        CollectionName.parse("docs__default")


def test_parse_rejects_knowledge_fallback() -> None:
    with pytest.raises(ValueError):
        CollectionName.parse("knowledge__knowledge")


def test_parse_rejects_unknown_embedding_model() -> None:
    """A name that matches the regex shape but uses a non-canonical model
    must NOT be treated as a CollectionName. Pinned decision #1.
    """
    with pytest.raises(ValueError, match="embedding_model"):
        CollectionName.parse("code__owner1__voyage-3__v1")


def test_parse_rejects_invented_model_name() -> None:
    with pytest.raises(ValueError, match="embedding_model"):
        CollectionName.parse("docs__owner1__voyage-future-7__v1")


def test_parse_rejects_invalid_content_type() -> None:
    """``other__owner__voyage-code-3__v1`` is not a known content type;
    the underlying regex already restricts content_type to the closed set.
    """
    with pytest.raises(ValueError):
        CollectionName.parse("other__owner1__voyage-code-3__v1")


def test_parse_rejects_missing_version() -> None:
    with pytest.raises(ValueError):
        CollectionName.parse("code__owner1__voyage-code-3")


def test_parse_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        CollectionName.parse("")


# ── Regex contract lock (Phase 6 regression guard) ─────────────────────────

def test_render_output_passes_is_conformant_predicate() -> None:
    """Lock the regex contract against the new type. If Phase 6 changes
    the regex without updating CollectionName, this guards the contract.
    """
    for ct in ("code", "docs", "rdr", "knowledge"):
        for model in CANONICAL_EMBEDDING_MODELS:
            name = CollectionName(ct, "nexus-abc12345", model, 1)
            assert is_conformant_collection_name(name.render())


def test_legacy_name_is_not_conformant_and_does_not_parse() -> None:
    """Joint invariant: any name the regex predicate rejects must also
    raise in CollectionName.parse. Anything the predicate accepts that
    uses a canonical model must round-trip.
    """
    legacy = "docs__nexus-571b8edd"
    assert not is_conformant_collection_name(legacy)
    with pytest.raises(ValueError):
        CollectionName.parse(legacy)
