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
from tests.conftest import make_vector_test_client


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


def test_effective_local_service_mode_uses_bge_not_client_fastembed_tier(monkeypatch) -> None:
    # nexus-xq8f9: in local + service-vector mode the service embeds bge-768
    # server-side, so the write token must be bge-768 even when the CLIENT has
    # no fastembed extra (which would otherwise resolve minilm-384 and make the
    # service refuse the write with HTTP 422).
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.db.http_vector_client.is_vector_service_mode", lambda: True)
    # Even if the client falls back to minilm (no fastembed), service path wins.
    monkeypatch.setattr("nexus.db.local_ef._fastembed_available", lambda: False)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "BAAI/bge-base-en-v1.5")
    assert effective_embedding_model_for_writes("code") == "bge-base-en-v15-768"
    assert effective_embedding_model_for_writes("docs") == "bge-base-en-v15-768"


def test_effective_local_nonservice_mode_uses_client_token(monkeypatch) -> None:
    # Raw local (no service vectors): the client's local-EF token is correct.
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.db.http_vector_client.is_vector_service_mode", lambda: False)
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
    from nexus.db.t3 import T3Database

    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=None,
        local_mode=True,
    )


@pytest.fixture
def t3_cloud(monkeypatch):
    from nexus.db.t3 import T3Database

    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    monkeypatch.setenv("CHROMA_API_KEY", "ck")
    # local_mode=False but with EphemeralClient so we don't reach a real
    # CloudClient. We only exercise the _build_embedding_fn path, not
    # actually embed.
    # nexus-sghyo (2026-08-06): voyage_api_key is deleted from
    # T3Database's constructor — client-side Voyage embedding is
    # retired (Hal determination 2026-07-28: "we do no embedding on the
    # client"). A voyage-token collection name now raises
    # IncompatibleCollectionError from this fixture's cloud-mode
    # T3Database (see test_dispatch_cloud_mode_voyage_conformant_name /
    # test_dispatch_cloud_mode_legacy_name).
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=None,
        local_mode=False,
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
    """nexus-sghyo (2026-08-06): cloud + voyage-token name used to
    select a client-constructed Voyage EF (model_name matching the
    parsed token). Client-side Voyage embedding is retired outright
    (Hal determination 2026-07-28) — the same dispatch now raises
    IncompatibleCollectionError instead of constructing one."""
    from nexus.db.t3 import IncompatibleCollectionError

    with pytest.raises(IncompatibleCollectionError, match="voyage-context-3"):
        t3_cloud._build_embedding_fn("docs__owner-1__voyage-context-3__v1")


def test_dispatch_cloud_mode_legacy_name(t3_cloud) -> None:
    """nexus-sghyo: legacy two-segment names prefix-fallback to a
    Voyage model token (``knowledge__`` -> voyage-context-3), which now
    hits the same retired client-side-embed raise as the conformant-name
    case above."""
    from nexus.db.t3 import IncompatibleCollectionError

    with pytest.raises(IncompatibleCollectionError, match="voyage-context-3"):
        t3_cloud._build_embedding_fn("knowledge__papers")


# ── nexus-a4h7b: the token PINS the local model; the active model must not win ──


def test_local_token_pins_named_model_over_active_local_mode(t3_local, monkeypatch) -> None:
    """nexus-a4h7b: active local model is bge (e.g. fastembed installed /
    local.embed_model switched) but the collection NAME says minilm — the EF
    must embed with the NAMED model, else first-write to a stale conformant
    name stores wrong-model vectors under a name claiming otherwise (the
    59vl/GH-667 shape on the intra-local axis)."""
    monkeypatch.setattr(
        "nexus.db.local_ef._resolve_local_model",
        lambda *, warn: "BAAI/bge-base-en-v1.5",
    )
    ef = t3_local._build_embedding_fn("docs__owner-1__minilm-l6-v2-384__v1")
    assert ef.model_name == "all-MiniLM-L6-v2"
    assert ef.dimensions == 384


def test_local_token_pins_named_model_over_active_bge_name(t3_local, monkeypatch) -> None:
    """The converse: name says bge-768, active model resolves minilm (no
    fastembed extra) — the EF must pin bge (and FAIL LOUD at embed time if
    fastembed is genuinely missing, never silently embed 384-dim)."""
    monkeypatch.setattr(
        "nexus.db.local_ef._resolve_local_model",
        lambda *, warn: "all-MiniLM-L6-v2",
    )
    ef = t3_local._build_embedding_fn("docs__owner-1__bge-base-en-v15-768__v1")
    assert ef.model_name == "BAAI/bge-base-en-v1.5"
    assert ef.dimensions == 768


def test_local_token_pins_named_model_in_cloud_mode(t3_cloud, monkeypatch) -> None:
    """Cloud mode reading a legacy local-token collection (the original
    nexus-59vl hazard): must use the NAMED local model, not whatever local
    tier happens to be active on this machine."""
    monkeypatch.setattr(
        "nexus.db.local_ef._resolve_local_model",
        lambda *, warn: "BAAI/bge-base-en-v1.5",
    )
    ef = t3_cloud._build_embedding_fn("code__owner-1__minilm-l6-v2-384__v1")
    assert ef.model_name == "all-MiniLM-L6-v2"
    assert ef.dimensions == 384


def test_legacy_name_without_token_still_uses_active_model(t3_local, monkeypatch) -> None:
    """A pre-RDR-103 name has NO token to pin — the active-model default is
    the only possible choice (documented fallback, unchanged behavior)."""
    monkeypatch.setattr(
        "nexus.db.local_ef._resolve_local_model",
        lambda *, warn: "BAAI/bge-base-en-v1.5",
    )
    ef = t3_local._build_embedding_fn("docs__nexus-abc")
    assert ef.model_name == "BAAI/bge-base-en-v1.5"
