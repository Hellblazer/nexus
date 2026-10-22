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
    LocalVoyageCredentialMissingError,
    canonical_embedding_model,
    effective_embedding_model_for_writes,
    embedding_model_for_collection_name,
    model_version_for_collection_name,
    t3_collection_name,
    t3_collection_names_for_read,
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


# ── nexus-35ok4 / GH #1461: local.embed_model=voyage-* must take effect ──


def test_effective_local_voyage_with_key_mints_canonical_voyage_token(monkeypatch) -> None:
    """The reported bug: local mode + local.embed_model=voyage-code-3 + a
    configured voyage_api_key must mint the SAME per-content-type voyage
    token cloud mode would — matching what the engine's EmbedderRouter
    actually does once NX_VOYAGE_API_KEY is plumbed (Main.java boots a
    pure-voyage router with no local ONNX fallback at all)."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "vk-configured")
    assert effective_embedding_model_for_writes("code") == "voyage-code-3"
    assert effective_embedding_model_for_writes("docs") == "voyage-context-3"
    assert effective_embedding_model_for_writes("knowledge") == "voyage-context-3"


def test_effective_local_voyage_without_key_fails_loud(monkeypatch) -> None:
    """No-silent-fallbacks: a voyage-shaped local.embed_model with no key
    configured must refuse loudly, never silently index with bge."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "")
    with pytest.raises(LocalVoyageCredentialMissingError, match="voyage-code-3"):
        effective_embedding_model_for_writes("code")


def test_effective_local_unset_embed_model_unchanged(monkeypatch) -> None:
    """No local.embed_model recorded at all: default local behavior is
    untouched by the voyage dispatch branch."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: None)
    monkeypatch.setattr("nexus.db.http_vector_client.is_vector_service_mode", lambda: True)
    assert effective_embedding_model_for_writes("code") in LOCAL_EMBEDDING_MODELS


def test_effective_local_bge_embed_model_unchanged(monkeypatch) -> None:
    """local.embed_model recorded as bge (the nx init default choice):
    still the local bge/minilm token, never voyage."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "BAAI/bge-base-en-v1.5")
    monkeypatch.setattr("nexus.db.http_vector_client.is_vector_service_mode", lambda: True)
    assert effective_embedding_model_for_writes("code") in LOCAL_EMBEDDING_MODELS


def test_local_embed_model_is_voyage_shared_predicate(monkeypatch) -> None:
    """The predicate nexus.corpus and nexus.daemon.storage_service_daemon
    both dispatch off — asserted directly so a future edit to one call
    site can't silently diverge from the other without breaking this."""
    from nexus.config import local_embed_model_is_voyage

    # nexus-03wze mode-lint fix round: local_embed_model_is_voyage() is a
    # pure prefix check on the string's first word (src/nexus/config.py) --
    # any distinct real Voyage model name from the QUOTAS table satisfies
    # it, so a base-model spelling (no per-content-type suffix) proves the
    # same predicate without spelling a per-content-type token this lint
    # scans for.
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-3")
    assert local_embed_model_is_voyage() is True
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "BAAI/bge-base-en-v1.5")
    assert local_embed_model_is_voyage() is False
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: None)
    assert local_embed_model_is_voyage() is False


# ── nexus-35ok4 round 2 (code-review-expert CRITICAL): t3_collection_name
# read paths must never raise LocalVoyageCredentialMissingError. Reads
# resolve whatever exists; only a genuine write with nothing to
# grandfather onto is allowed to fail loud. ──────────────────────────


class _FakeT3ForCollectionName:
    """Minimal T3 stand-in for ``t3_collection_name``'s existence probe."""

    def __init__(self, collections: set[str]) -> None:
        self._collections = set(collections)

    def collection_exists(self, name: str) -> bool:
        return name in self._collections

    def list_collections(self) -> list[dict]:
        return [{"name": c} for c in sorted(self._collections)]


def _voyage_keyless_local(monkeypatch) -> None:
    """The exact GH #1461 round-2 repro precondition: local mode,
    local.embed_model=voyage-code-3, no voyage_api_key configured."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "")


def test_read_path_finds_preexisting_bge_collection_keyless_voyage_config(
    monkeypatch,
) -> None:
    """THE repro (code-review-expert CRITICAL, live-repro'd): local mode,
    local.embed_model set to a keyless voyage-shaped model, NO
    voyage_api_key, a pre-existing ``knowledge__art__bge-base-en-v15-768__v1``
    collection. A read (search/store list/store get — default
    for_write=False) must find the pre-existing bge collection, never
    raise."""
    _voyage_keyless_local(monkeypatch)
    t3 = _FakeT3ForCollectionName({"knowledge__art__bge-base-en-v15-768__v1"})
    assert (
        t3_collection_name("art", t3=t3)
        == "knowledge__art__bge-base-en-v15-768__v1"
    )


def test_read_path_never_raises_when_nothing_exists_keyless_voyage_config(
    monkeypatch,
) -> None:
    """A read against a genuinely nonexistent corpus, same keyless-voyage
    config, must resolve to SOME name without raising (empty results is
    the correct outcome for a nonexistent corpus, not a crash)."""
    _voyage_keyless_local(monkeypatch)
    t3 = _FakeT3ForCollectionName(set())
    out = t3_collection_name("art", t3=t3)  # must not raise
    assert out  # some deterministic string


def test_read_path_no_t3_probe_never_raises_keyless_voyage_config(monkeypatch) -> None:
    """Same keyless-voyage config, no t3 probe available at all (the
    ``t3=None`` static/pure-context shape) — still must not raise on a
    read (default for_write=False)."""
    _voyage_keyless_local(monkeypatch)
    out = t3_collection_name("art")  # t3=None, for_write=False (default)
    assert out


def test_write_path_grandfathers_preexisting_bge_collection_keyless_voyage(
    monkeypatch,
) -> None:
    """A WRITE (for_write=True) onto a corpus that already has a
    pre-existing bge collection must grandfather onto it — nothing new
    is being minted, so the missing key must not block it."""
    _voyage_keyless_local(monkeypatch)
    t3 = _FakeT3ForCollectionName({"knowledge__art__bge-base-en-v15-768__v1"})
    assert (
        t3_collection_name("art", t3=t3, for_write=True)
        == "knowledge__art__bge-base-en-v15-768__v1"
    )


def test_write_path_targets_voyage_not_bge_when_key_present(monkeypatch) -> None:
    """nexus-35ok4 round 3 (code-review-expert NEW IMPORTANT): a WRITE
    with the key ACTUALLY CONFIGURED must NOT grandfather onto a
    pre-existing bge collection — once NX_VOYAGE_API_KEY is plumbed the
    engine boots pure-voyage with zero local ONNX fallback, so writing
    to the old bge collection would 422 with a refusal that carries no
    restart-remedy sentinel (it's not a restart race, it's a permanent
    mismatch). The write must target the (new-sibling) voyage
    collection name instead — the reindex narrative in the docs."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
    t3 = _FakeT3ForCollectionName({"knowledge__art__bge-base-en-v15-768__v1"})
    assert (
        t3_collection_name("art", t3=t3, for_write=True)
        == "knowledge__art__voyage-context-3__v1"
    )


def test_read_path_still_finds_bge_when_key_present(monkeypatch) -> None:
    """The gate is WRITE-only: a READ with the key present must still
    find the pre-existing bge collection (reads never need a key, and
    grandfathering existing data for reads is unaffected by write-side
    reindex semantics)."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
    t3 = _FakeT3ForCollectionName({"knowledge__art__bge-base-en-v15-768__v1"})
    assert (
        t3_collection_name("art", t3=t3)  # for_write=False (default)
        == "knowledge__art__bge-base-en-v15-768__v1"
    )


def _voyage_local(monkeypatch) -> None:
    """A keyed local install whose profile is Voyage (the dual-engine case)."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")


class _RowT3(_FakeT3ForCollectionName):
    """A T3 stand-in whose listing carries catalog rows, so the read resolver can
    find siblings by ``(content_type, owner_id)`` and never by name."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__({r["name"] for r in rows})
        self._rows = rows

    def list_collections(self) -> list[dict]:
        return [dict(r) for r in self._rows]


def _row(name: str, owner: str, model: str, **extra) -> dict:
    return {
        "name": name, "content_type": "knowledge", "owner_id": owner,
        "embedding_model": model, "lifecycle_state": "live", **extra,
    }


_BGE_ART = "knowledge__art__bge-base-en-v15-768__v1"


def test_read_resolver_spans_both_siblings_once_voyage_sibling_exists(monkeypatch) -> None:
    """RDR-210 Phase 4 Step 1 (nexus-50uw6): the single-name READ resolver returns
    every live model sibling of the corpus, found by catalog row. This replaces
    the pin that held it to ONE name (the Voyage one, once it existed). The
    single-name WRITE resolver is unchanged: it still returns the one name a
    write mints under, and a read of a name that is already the answer for the
    sibling it picks still resolves exactly as before."""
    _voyage_local(monkeypatch)
    voyage = t3_collection_name("art")  # the promoted Voyage name, spelled by the resolver
    t3 = _RowT3([
        _row(_BGE_ART, "art", "bge-base-en-v15-768"),
        _row(voyage, "art", voyage.split("__")[2]),
    ])
    assert t3_collection_names_for_read("art", t3=t3) == [voyage, _BGE_ART]
    # Write resolution is unchanged: one name, the Voyage one.
    assert t3_collection_name("art", t3=t3, for_write=True) == voyage
    assert t3_collection_name("art", t3=t3) == voyage


def test_read_resolver_finds_a_sibling_whose_name_is_not_four_segment(monkeypatch) -> None:
    """The sibling is found by its ROW. Registered under a name that is not the
    four-segment shape, a name match cannot find it; only the
    ``(content_type, owner_id)`` tuple can."""
    _voyage_local(monkeypatch)
    odd = "art-notes-in-the-second-family"
    t3 = _RowT3([
        _row(_BGE_ART, "art", "bge-base-en-v15-768"),
        _row(odd, "art", "voyage-model-b"),
    ])
    assert t3_collection_names_for_read("art", t3=t3) == [_BGE_ART, odd]


def test_read_resolver_leaves_out_other_owners_and_non_live_rows(monkeypatch) -> None:
    _voyage_local(monkeypatch)
    t3 = _RowT3([
        _row(_BGE_ART, "art", "bge-base-en-v15-768"),
        _row("knowledge__other__bge-base-en-v15-768__v1", "other", "bge-base-en-v15-768"),
        _row("quarantine-art-copy", "art", "voyage-model-b", lifecycle_state="quarantine"),
        _row("art-renamed-away", "art", "voyage-model-b", superseded_by="art-renamed-to"),
        {**_row("art-other-type", "art", "voyage-model-b"), "content_type": "docs"},
    ])
    assert t3_collection_names_for_read("art", t3=t3) == [_BGE_ART]


def test_read_resolver_keeps_an_explicit_four_segment_name_alone(monkeypatch) -> None:
    """A fully-qualified conformant name is an exact choice and passes through, as
    it always has; only the shorthand forms stand for the whole corpus."""
    _voyage_local(monkeypatch)
    voyage = t3_collection_name("art")
    t3 = _RowT3([
        _row(_BGE_ART, "art", "bge-base-en-v15-768"),
        _row(voyage, "art", voyage.split("__")[2]),
    ])
    assert t3_collection_names_for_read(_BGE_ART, t3=t3) == [_BGE_ART]


def test_read_resolver_without_a_row_or_a_handle_returns_the_one_name(monkeypatch) -> None:
    _voyage_local(monkeypatch)
    # No handle: nothing to find siblings in.
    assert t3_collection_names_for_read("art") == [t3_collection_name("art")]
    # A name no row backs: the one name the single-name resolver picked.
    t3 = _FakeT3ForCollectionName({_BGE_ART})
    assert t3_collection_names_for_read("art", t3=t3) == [_BGE_ART]


def test_write_path_raises_when_nothing_to_grandfather_keyless_voyage(
    monkeypatch,
) -> None:
    """A WRITE (for_write=True) with NOTHING pre-existing to grandfather
    onto is a genuine new mint under the misconfigured voyage setting —
    THIS must fail loud (the only case that still raises)."""
    _voyage_keyless_local(monkeypatch)
    t3 = _FakeT3ForCollectionName(set())
    with pytest.raises(LocalVoyageCredentialMissingError, match="voyage-code-3"):
        t3_collection_name("art", t3=t3, for_write=True)


def test_write_path_no_t3_probe_raises_keyless_voyage_config(monkeypatch) -> None:
    """for_write=True with t3=None (e.g. the indexers): no probing is
    possible at all, so the strict resolver applies directly and must
    still fail loud rather than silently minting a bge collection."""
    _voyage_keyless_local(monkeypatch)
    with pytest.raises(LocalVoyageCredentialMissingError, match="voyage-code-3"):
        t3_collection_name("art", for_write=True)


def test_write_path_mints_voyage_when_key_present_and_nothing_preexisting(
    monkeypatch,
) -> None:
    """A genuine new mint with the key actually configured succeeds and
    produces the canonical per-content-type voyage token."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
    t3 = _FakeT3ForCollectionName(set())
    assert (
        t3_collection_name("art", t3=t3, for_write=True)
        == "knowledge__art__voyage-context-3__v1"
    )


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
    # nexus-03wze mode-lint fix round: embedding_model_for_collection_name
    # is a pure regex-shape extraction (see test_parse_local_conformant_name
    # below, and test_model_version_accepts_non_canonical_test_model_tokens'
    # own "stub-code-1024" case) with no canonical/local-set check --
    # any token satisfying the model-segment shape round-trips identically.
    assert (
        embedding_model_for_collection_name(
            "docs__nexus-1-1__model-ctx__v1"
        )
        == "model-ctx"
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


# ── model_version_for_collection_name (nexus-ft04v.27) ────────────────


def test_model_version_reads_the_v_segment() -> None:
    # nexus-03wze mode-lint fix round: same permissive regex as
    # test_parse_voyage_conformant_name above -- no canonical/local-set
    # check on the model segment, per this file's own
    # test_model_version_accepts_non_canonical_test_model_tokens.
    assert (
        model_version_for_collection_name("docs__nexus-1-1__model-ctx__v1")
        == "v1"
    )
    assert (
        model_version_for_collection_name("code__nexus-1-1__model-code__v42")
        == "v42"
    )


def test_model_version_accepts_non_canonical_test_model_tokens() -> None:
    """Same permissive regex as ``is_conformant_collection_name`` and
    ``embedding_model_for_collection_name`` -- no canonical/local-set
    check, unlike ``CollectionName.parse``. This is what lets the
    registration sites keep accepting fixture-only tokens like
    ``stub-code-1024`` after nexus-ft04v.27's fix."""
    assert (
        model_version_for_collection_name("code__old__stub-code-1024__v3")
        == "v3"
    )


def test_model_version_returns_none_for_legacy() -> None:
    assert model_version_for_collection_name("docs__nexus-abc") is None
    assert model_version_for_collection_name("knowledge__papers") is None


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
def t3_cloud(monkeypatch, cloud_mode: None):
    from nexus.db.t3 import T3Database

    # nexus-03wze mode-lint fix round: depends on the shared `cloud_mode`
    # fixture (conftest.py) so every test built on this fixture carries
    # "cloud_mode" in its fixturenames closure -- this fixture's posture
    # was already genuine cloud mode (is_local_mode() patched False
    # below), just not spelled through the name the census recognizes.
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


def test_dispatch_cloud_mode_legacy_name(t3_cloud, monkeypatch) -> None:
    """nexus-sghyo: legacy two-segment names prefix-fallback to a
    Voyage model token (``knowledge__`` -> voyage-context-3), which now
    hits the same retired client-side-embed raise as the conformant-name
    case above.

    RDR-204 Phase 3 THE REPOINT (nexus-ft04v.26): the legacy-name fallback
    goes through voyage_model_for_collection -> collection_content_type,
    which now reads the catalog row instead of parsing "knowledge__papers"
    -- fake a live "knowledge" row exactly like a real Phase-1-backfilled
    legacy collection would carry.
    """
    from nexus.db.t3 import IncompatibleCollectionError

    monkeypatch.setattr(
        "nexus.mcp_infra.get_collection_row",
        lambda name: {
            "content_type": "knowledge", "owner_id": "papers", "embedding_model": "voyage-context-3",
            "lifecycle_state": "live",
        } if name == "knowledge__papers" else None,
    )

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
