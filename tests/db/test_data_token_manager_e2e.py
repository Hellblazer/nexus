# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E: DataTokenManager self-minting against the REAL bundled engine
substrate (nexus-wrwb7, RDR-005 2a).

Issues a real ``scope=mint-locked`` credential via the engine's own admin
surface (``POST /v1/service-tokens/issue``, the ``HttpTokenStore`` client
``nx service token issue --scope mint-locked`` also drives), configures
``mint_token``, and drives a real T3 store + read round trip through
``HttpVectorClient`` presenting ONLY the self-minted data token -- the
static ``service_token`` env is deliberately set to a garbage sentinel, so
the round trip can only succeed via the DataTokenManager resolution seam
actually taking effect end to end.

Uses the SESSION-SCOPED engine substrate (``tests/_engine_substrate.py`` via
the ``t2_service_env`` fixture) rather than spinning up a second hermetic
PG+jar (the pattern ``tests/db/test_data_token_mint_e2e.py`` uses for the
ENGINE side's own contract tests) -- this test is about the CLIENT's
resolution seam, not the engine's admin-surface behavior, so reusing the
shared substrate is the cheaper, still-real E2E gate.
"""
from __future__ import annotations

import hashlib
import logging

import pytest
import structlog

pytestmark = [pytest.mark.integration]

#: bge-base-en-v15-768 dispatches to the 768-dim column server-side (matches
#: the naming convention tests/db/test_count_list_collections_coherence.py
#: uses for the same substrate) -- avoids any dependency on a real embedder
#: being configured; embeddings are supplied explicitly.
_DIM = 768


def test_data_token_manager_self_mint_round_trip_against_real_engine(
    t2_service_env: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = t2_service_env

    from tests._engine_substrate import ensure_engine

    state = ensure_engine()

    from nexus.db.t2.http_token_store import HttpTokenStore

    with HttpTokenStore(base_url=state["base_url"], _token=state["bearer"]) as admin:
        issued = admin.issue_token(tenant, label="nexus-wrwb7-e2e", scope="mint-locked")
    mint_locked_credential = issued["token"]
    assert mint_locked_credential

    # Deliberately break the static service_token so the round trip below
    # can ONLY succeed via the self-minted data token actually being
    # presented -- a silent fallback to the static token would 401 loudly,
    # not pass quietly.
    monkeypatch.setenv("NX_SERVICE_TOKEN", "deliberately-invalid-static-sentinel")
    monkeypatch.setenv("NX_MINT_TOKEN", mint_locked_credential)

    from nexus.db.data_token import get_data_token_manager, reset_data_token_manager

    reset_data_token_manager()
    try:
        import nexus.db.http_vector_client as hvc

        client = hvc.HttpVectorClient(tenant=tenant)
        collection = f"knowledge__wrwb7e2e-{tenant}__bge-base-en-v15-768__v1"
        content = f"nexus-wrwb7 self-minted data-token round trip ({tenant})"
        chash = hashlib.sha256(content.encode()).hexdigest()
        embedding = [0.1] * _DIM

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
        with structlog.testing.capture_logs() as logs:
            # Write: exercises the T3 choke point in _request_once.
            client.upsert_chunks_with_embeddings(
                collection, ids=[chash], documents=[content], embeddings=[embedding],
                metadatas=[{"title": "wrwb7-e2e", "chunk_text_hash": chash}],
            )
            # Read: a second call through the SAME manager -- proves cache
            # reuse (residue discipline), not a re-mint per call.
            present = client.get_collection(collection).get(ids=[chash], include=[])

        assert chash in (present.get("ids") or []), (
            "the engine accepted the write authenticated by the self-minted "
            "data token -- a fallback to the broken static service_token "
            "would have 401'd on the first call"
        )

        minted_events = [e for e in logs if e.get("event") == "data_token_minted"]
        failed_events = [e for e in logs if e.get("event") == "data_token_mint_failed"]
        assert len(minted_events) == 1, (
            f"expected exactly one mint across write+read (residue discipline "
            f"nexus-lgiqw), got {len(minted_events)}: {minted_events}"
        )
        assert failed_events == []
        # Never log the credential or the token value.
        rendered = str(logs)
        assert mint_locked_credential not in rendered

        # ONE live token cached for (base_url, tenant) -- the cache-key
        # contract, verified structurally (not just via the log count).
        manager = get_data_token_manager()
        assert len(manager._cache) == 1  # noqa: SLF001 — verifying the private cache's residue invariant is the point of this assertion
    finally:
        reset_data_token_manager()


def test_mint_tenant_tenant_asymmetric_round_trip_succeeds(
    t2_service_env: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-ssqk9 CRITICAL: Hal's real deployed shape -- every Http*Store
    defaults its own ``tenant`` kwarg to ``"default"``, but a real
    ``scope=mint-locked`` credential is bound server-side to WHATEVER
    tenant the operator issued it under (e.g. ``"nexus"``). Issue the
    credential bound to tenant A, configure ``mint_tenant=A`` while the
    store client operates under its usual ``"default"`` convention
    (deliberately NOT passing ``tenant=`` to HttpVectorClient) -- the mint
    must succeed and the round trip must authenticate, because the mint
    BODY carries the configured mint_tenant, not the caller's tenant."""
    tenant_a = t2_service_env

    from tests._engine_substrate import ensure_engine

    state = ensure_engine()

    from nexus.db.t2.http_token_store import HttpTokenStore

    with HttpTokenStore(base_url=state["base_url"], _token=state["bearer"]) as admin:
        issued = admin.issue_token(tenant_a, label="nexus-ssqk9-asym", scope="mint-locked")
    mint_locked_credential = issued["token"]
    assert mint_locked_credential

    monkeypatch.setenv("NX_SERVICE_TOKEN", "deliberately-invalid-static-sentinel")
    monkeypatch.setenv("NX_MINT_TOKEN", mint_locked_credential)
    monkeypatch.setenv("NX_MINT_TENANT", tenant_a)

    from nexus.db.data_token import get_data_token_manager, reset_data_token_manager

    reset_data_token_manager()
    try:
        import nexus.db.http_vector_client as hvc

        # Deliberately NOT passing tenant= -- the client's usual "default"
        # convention, mismatched from tenant_a on purpose. mint_tenant is
        # what makes the mint body carry the CREDENTIAL's real bound
        # tenant instead.
        client = hvc.HttpVectorClient()
        collection = f"knowledge__ssqk9asym-{tenant_a}__bge-base-en-v15-768__v1"
        content = f"nexus-ssqk9 tenant-asymmetric round trip ({tenant_a})"
        chash = hashlib.sha256(content.encode()).hexdigest()
        embedding = [0.1] * _DIM

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
        with structlog.testing.capture_logs() as logs:
            client.upsert_chunks_with_embeddings(
                collection, ids=[chash], documents=[content], embeddings=[embedding],
                metadatas=[{"title": "ssqk9-asym", "chunk_text_hash": chash}],
            )
            present = client.get_collection(collection).get(ids=[chash], include=[])

        assert chash in (present.get("ids") or []), (
            "mint_tenant must have overridden the caller's 'default' "
            "tenant in the mint body, matching the credential's own "
            "bound tenant -- otherwise this write 403s at mint time"
        )
        minted_events = [e for e in logs if e.get("event") == "data_token_minted"]
        assert len(minted_events) == 1
        rendered = str(logs)
        assert mint_locked_credential not in rendered
    finally:
        reset_data_token_manager()


def test_mint_tenant_wrong_tenant_surfaces_typed_loud_403(
    t2_service_env: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-ssqk9 CRITICAL, wrong-tenant arm: configure mint_tenant to a
    tenant the credential is NOT bound to -- the mint must 403, and that
    403 must surface as the typed DataTokenMintError (never a silent
    fallback to the static, deliberately-broken service_token)."""
    tenant_a = t2_service_env
    tenant_b = f"{tenant_a}-wrong"  # deliberately NOT the credential's bound tenant

    from tests._engine_substrate import ensure_engine

    state = ensure_engine()

    from nexus.db.t2.http_token_store import HttpTokenStore

    with HttpTokenStore(base_url=state["base_url"], _token=state["bearer"]) as admin:
        issued = admin.issue_token(tenant_a, label="nexus-ssqk9-wrong", scope="mint-locked")
    mint_locked_credential = issued["token"]
    assert mint_locked_credential

    monkeypatch.setenv("NX_SERVICE_TOKEN", "deliberately-invalid-static-sentinel")
    monkeypatch.setenv("NX_MINT_TOKEN", mint_locked_credential)
    monkeypatch.setenv("NX_MINT_TENANT", tenant_b)

    from nexus.db.data_token import DataTokenMintError, reset_data_token_manager

    reset_data_token_manager()
    try:
        import nexus.db.http_vector_client as hvc

        client = hvc.HttpVectorClient()
        collection = f"knowledge__ssqk9wrong-{tenant_a}__bge-base-en-v15-768__v1"
        content = f"nexus-ssqk9 wrong-tenant round trip ({tenant_a})"
        chash = hashlib.sha256(content.encode()).hexdigest()
        embedding = [0.1] * _DIM

        with pytest.raises(DataTokenMintError) as exc_info:
            client.upsert_chunks_with_embeddings(
                collection, ids=[chash], documents=[content], embeddings=[embedding],
                metadatas=[{"title": "ssqk9-wrong", "chunk_text_hash": chash}],
            )

        message = str(exc_info.value)
        assert "403" in message
        assert tenant_b in message
        assert "nx config set mint_tenant" in message
        assert mint_locked_credential not in message
    finally:
        reset_data_token_manager()
