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
)
from nexus.db.local_ef import LOCAL_EMBEDDING_TOKENS, local_model_token
from tests.conftest import make_vector_test_client


@pytest.fixture(autouse=True)
def _stub_agreeing_embedding_profile(monkeypatch: pytest.MonkeyPatch):
    """RDR-204 Phase 3 item 3 (nexus-ft04v.26): effective_embedding_model_for_writes
    now validates the local intent (this file's own subject) against a
    REAL profile read (nexus.catalog.factory.make_catalog_reader()
    .embedding_profile()). This file's tests are about the local-intent
    DISPATCH, not profile validation, so stub the profile to always
    AGREE with whatever nexus.corpus._write_intent_embedding_model
    computes for the SAME content_type under the test's own active
    config mocks (outcome 3: agree -> the profile's model, which by
    construction equals the intent this fixture just asked for) --
    transparent to every test unless it overrides the stub locally to
    pin outcome 2 or 4 instead. Lazy: embedding_profile() only executes
    when a code path actually calls it, so a test whose config makes
    _write_intent_embedding_model raise (the keyless-voyage credential
    check, content-type-independent) never reaches this fixture's body
    at all -- effective_embedding_model_for_writes raises there first,
    exactly as before this bead.
    """
    import nexus.catalog.factory as factory_mod
    from nexus.corpus import _write_intent_embedding_model

    class _AgreeingReader:
        def embedding_profile(self) -> list[dict]:
            return [
                {"content_type": ct, "embedding_model": _write_intent_embedding_model(ct), "dimension": 1024}
                for ct in ("code", "docs", "rdr", "knowledge")
            ]

    monkeypatch.setattr(factory_mod, "make_catalog_reader", lambda: _AgreeingReader())


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

    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-context-3")
    assert local_embed_model_is_voyage() is True
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "BAAI/bge-base-en-v1.5")
    assert local_embed_model_is_voyage() is False
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: None)
    assert local_embed_model_is_voyage() is False


# ── RDR-204 Phase 3 item 3 (nexus-ft04v.26): effective_embedding_model_for_writes's
# own profile-validation outcomes 2/3/4 (outcome 1, LocalVoyageCredentialMissingError,
# is unchanged and already covered by test_effective_local_voyage_without_key_fails_loud
# above and tests/test_o5x2c_write_chokepoint_repros.py). Each test here overrides
# THIS file's own autouse _stub_agreeing_embedding_profile fixture locally to pin a
# specific (dis)agreement instead of the default "always agree" stub.


def _stub_profile(monkeypatch, rows: dict[str, str]) -> None:
    """Override this file's autouse agreeing-profile stub with an explicit,
    fixed set of {content_type: embedding_model} rows -- for pinning
    outcome 2 (mismatch) and outcome 4 (empty) precisely, rather than the
    autouse fixture's "always agree with intent" default."""
    import nexus.catalog.factory as factory_mod

    class _FixedReader:
        def embedding_profile(self) -> list[dict]:
            return [
                {"content_type": ct, "embedding_model": model, "dimension": 1024}
                for ct, model in rows.items()
            ]

    monkeypatch.setattr(factory_mod, "make_catalog_reader", lambda: _FixedReader())


class TestEffectiveEmbeddingModelForWritesProfileOutcomes:
    def test_outcome_3_intent_and_profile_agree_returns_profile_value(self, monkeypatch) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
        _stub_profile(monkeypatch, {"code": "voyage-code-3"})
        assert effective_embedding_model_for_writes("code") == "voyage-code-3"

    def test_outcome_2_intent_voyage_key_present_profile_says_bge_raises_mismatch(
        self, monkeypatch,
    ) -> None:
        """The canonical repro named in the coordinator's ruling: local
        intent is voyage (key present), but the engine's profile still
        says bge -- the service has not been restarted since the key was
        configured. Must raise EmbeddingProfileMismatchError naming the
        restart, never silently write under either model."""
        from nexus.corpus import EmbeddingProfileMismatchError

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
        _stub_profile(monkeypatch, {"code": "bge-base-en-v15-768"})
        with pytest.raises(EmbeddingProfileMismatchError, match="restart"):
            effective_embedding_model_for_writes("code")

    def test_outcome_4_empty_profile_returns_intent_bootstrap_case(self, monkeypatch) -> None:
        """Engine-verified correction (hand-off report, CatalogRepository
        .upsertCollection): NO profile row for content_type is the
        bootstrap case, not a stale-config case -- returns intent (never
        raises), since intent is exactly what the engine's own
        registration handler would accept and use to seed the row."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
        _stub_profile(monkeypatch, {})  # no row for "code" at all
        assert effective_embedding_model_for_writes("code") == "voyage-code-3"

    def test_outcome_4_empty_profile_local_bge_bootstrap(self, monkeypatch) -> None:
        """Same bootstrap case, plain local mode (no voyage opt-in) --
        intent comes from the service-vector-mode bge branch."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: False)
        monkeypatch.setattr("nexus.db.http_vector_client.is_vector_service_mode", lambda: True)
        _stub_profile(monkeypatch, {})
        assert effective_embedding_model_for_writes("code") == "bge-base-en-v15-768"

    def test_pre_phase_2_engine_route_missing_propagates_uncaught(self, monkeypatch) -> None:
        """Against a pre-Phase-2 engine, EmbeddingProfileRouteMissingError
        propagates uncaught -- never wrapped, never a silent fallback."""
        from nexus.catalog.http_catalog_client import EmbeddingProfileRouteMissingError
        import nexus.catalog.factory as factory_mod

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")

        class _PrePhase2Reader:
            def embedding_profile(self) -> list[dict]:
                raise EmbeddingProfileRouteMissingError("GET /v1/catalog/embedding_profile is not served")

        monkeypatch.setattr(factory_mod, "make_catalog_reader", lambda: _PrePhase2Reader())
        with pytest.raises(EmbeddingProfileRouteMissingError):
            effective_embedding_model_for_writes("code")


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
    local.embed_model=voyage-code-3, NO voyage_api_key, a pre-existing
    ``knowledge__art__bge-base-en-v15-768__v1`` collection. A read
    (search/store list/store get — default for_write=False) must find
    the pre-existing bge collection, never raise."""
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


def test_read_path_prefers_voyage_over_bge_once_voyage_sibling_exists(monkeypatch) -> None:
    """nexus-o5x2c round 4 (substantive-critic Significant): PINS THE
    ACTUAL behavior, which is honestly narrower than
    ``test_read_path_still_finds_bge_when_key_present``'s docstring
    might suggest in isolation. The "key present -> read finds bge" row
    holds ONLY UNTIL a voyage sibling collection exists for the same
    corpus. Once ANY keyed write has created the voyage collection
    (even with the bge collection still present and still holding
    data), ``t3_collection_name``'s FIRST candidate check
    (``t3.collection_exists(promoted)``, where ``promoted`` is already
    the voyage name for a voyage-configured local install) short-
    circuits to the voyage collection immediately — the bge fallback
    probes below it are UNREACHABLE code for this state, by
    construction, not a bug to fix here.

    ``t3_collection_name`` returns ONE physical collection name; genuinely
    searching BOTH bge and voyage collections for one logical corpus is a
    multi-collection concern (RDR-156 / nexus-3l6gz's multi-model corpus
    grouping), out of scope for this single-name resolver. Tracked as an
    explicit caveat on nexus-ddmfg (the pre-existing follow-up for the
    engine's voyage-only-mode-flip), not silently left undocumented —
    see docs/cli-reference.md "Local mode with Voyage" for the caveat."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
    monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
    t3 = _FakeT3ForCollectionName({
        "knowledge__art__bge-base-en-v15-768__v1",
        "knowledge__art__voyage-context-3__v1",
    })
    assert (
        t3_collection_name("art", t3=t3)  # for_write=False (default)
        == "knowledge__art__voyage-context-3__v1"
    )


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


# ── model_version_for_collection_name (nexus-ft04v.27) ────────────────


def test_model_version_reads_the_v_segment() -> None:
    assert (
        model_version_for_collection_name("docs__nexus-1-1__voyage-context-3__v1")
        == "v1"
    )
    assert (
        model_version_for_collection_name("code__nexus-1-1__voyage-code-3__v42")
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
