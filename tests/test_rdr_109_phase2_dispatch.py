# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-109 Phase 2: bidirectional name-aware EF dispatch + effective
write-model regression tests.

Closes nexus-n3qu.2 acceptance criteria 1-4 + 6:

- 4-cell (mode, embedded-model-token) matrix in ``T3Database._embedding_fn``.
- ``IncompatibleCollectionError`` raised loud on local + voyage-name.
- Legacy voyage-named collections still queryable in cloud mode.
- ``effective_embedding_model_for_writes`` returns the local token in
  local mode and delegates to ``canonical_embedding_model`` in cloud.
- ``embedding_model_for_collection_name`` parses conformant names.
"""
from __future__ import annotations

import pytest

from nexus.corpus import (
    CANONICAL_EMBEDDING_MODELS,
    LOCAL_EMBEDDING_MODELS,
    canonical_embedding_model,
    effective_embedding_model_for_writes,
    embedding_model_for_collection_name,
)
from nexus.db.local_ef import LOCAL_EMBEDDING_TOKENS, local_model_token


# ── Foundations ──────────────────────────────────────────────────────


def test_local_embedding_models_disjoint_from_canonical() -> None:
    assert LOCAL_EMBEDDING_MODELS & CANONICAL_EMBEDDING_MODELS == frozenset()


def test_local_embedding_models_matches_local_ef_tokens() -> None:
    assert LOCAL_EMBEDDING_MODELS == LOCAL_EMBEDDING_TOKENS


def test_local_model_token_returns_known_value() -> None:
    assert local_model_token() in LOCAL_EMBEDDING_MODELS


# ── effective_embedding_model_for_writes ─────────────────────────────


def test_effective_in_local_mode_returns_local_token(monkeypatch) -> None:
    monkeypatch.setenv("NX_LOCAL", "1")
    assert effective_embedding_model_for_writes("docs") in LOCAL_EMBEDDING_MODELS
    assert effective_embedding_model_for_writes("code") in LOCAL_EMBEDDING_MODELS


def test_effective_in_cloud_mode_delegates_to_canonical(cloud_mode) -> None:
    assert (
        effective_embedding_model_for_writes("docs")
        == canonical_embedding_model("docs")
    )
    assert (
        effective_embedding_model_for_writes("code")
        == canonical_embedding_model("code")
    )


# ── embedding_model_for_collection_name ──────────────────────────────


def test_parse_voyage_conformant_name() -> None:
    assert (
        embedding_model_for_collection_name(
            "docs__nexus-1-1__voyage-context-3__v1"
        )
        == "voyage-context-3"
    )


def test_parse_local_conformant_name() -> None:
    assert (
        embedding_model_for_collection_name(
            "code__nexus-1-1__minilm-l6-v2-384__v1"
        )
        == "minilm-l6-v2-384"
    )


def test_parse_returns_none_for_legacy() -> None:
    assert embedding_model_for_collection_name("docs__nexus-abc") is None
    assert embedding_model_for_collection_name("knowledge__papers") is None


# ── Bidirectional EF dispatch ────────────────────────────────────────


@pytest.fixture
def t3_local():
    import chromadb
    from nexus.db.local_ef import LocalEmbeddingFunction
    from nexus.db.t3 import T3Database

    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=None,
        local_mode=True,
        voyage_api_key=None,
    )


@pytest.fixture
def t3_cloud(monkeypatch):
    import chromadb
    from nexus.db.t3 import T3Database

    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    monkeypatch.setenv("CHROMA_API_KEY", "ck")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    # local_mode=False but with EphemeralClient so we don't reach a real
    # CloudClient. We only exercise the _build_embedding_fn path, not
    # actually embed.
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=None,
        local_mode=False,
        voyage_api_key="vk_test",
    )


def test_dispatch_local_mode_local_name(t3_local) -> None:
    from nexus.db.local_ef import LocalEmbeddingFunction

    ef = t3_local._build_embedding_fn("docs__owner-1__minilm-l6-v2-384__v1")
    assert isinstance(ef, LocalEmbeddingFunction)


def test_dispatch_local_mode_legacy_name(t3_local) -> None:
    """Legacy two-segment names have no parsed token; local mode falls
    through to LocalEmbeddingFunction (the only thing it CAN do)."""
    from nexus.db.local_ef import LocalEmbeddingFunction

    ef = t3_local._build_embedding_fn("docs__nexus-abc")
    assert isinstance(ef, LocalEmbeddingFunction)


def test_dispatch_local_mode_voyage_name_raises(t3_local) -> None:
    from nexus.db.t3 import IncompatibleCollectionError

    with pytest.raises(IncompatibleCollectionError, match="voyage-context-3"):
        t3_local._build_embedding_fn("docs__owner-1__voyage-context-3__v1")


def test_dispatch_cloud_mode_local_name_uses_local_ef(t3_cloud) -> None:
    """Legacy local-mode collections after credentials are added (the
    original nexus-59vl + GH #667 hazard). Cloud mode must NOT try to
    re-embed those 384-dim vectors with Voyage's 1024-dim space."""
    from nexus.db.local_ef import LocalEmbeddingFunction

    ef = t3_cloud._build_embedding_fn("code__owner-1__minilm-l6-v2-384__v1")
    assert isinstance(ef, LocalEmbeddingFunction)


def test_dispatch_cloud_mode_voyage_conformant_name(t3_cloud) -> None:
    """Cloud + voyage-token name: the standard cloud path. Verifies the
    EF model_name matches the parsed token (not the prefix default)."""
    ef = t3_cloud._build_embedding_fn("docs__owner-1__voyage-context-3__v1")
    assert ef.model_name == "voyage-context-3"


def test_dispatch_cloud_mode_legacy_name(t3_cloud) -> None:
    """Legacy two-segment names: prefix-based fallback selects Voyage."""
    ef = t3_cloud._build_embedding_fn("knowledge__papers")
    # The Voyage EF stores model_name; both context and code possible per
    # prefix. ``knowledge__`` prefix -> voyage-context-3.
    assert ef.model_name == "voyage-context-3"
