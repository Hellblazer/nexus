# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-xzeml: a mint-armed box works with NO static service_token.

Measured 2026-09-26 on Sam's box: deleting a revoked ``service_token`` from
config.yml broke every ``nx`` command ("service_url is set but no
service_token is resolvable"), although every data-path client replaces the
static bearer with a minted data token. The resolvers now accept a
``service_url`` with no static token when ``mint_token`` is configured.

Against the REAL engine: a tenant-bound ``mint-locked`` credential, no
``NX_SERVICE_TOKEN``, no supervisor lease; T3 and T2 round trips must succeed
on data tokens alone, and a token-admin verb must fail with the named
"no bearer" message, not the resolver's refusal.
"""
from __future__ import annotations

import hashlib

import pytest

pytestmark = [pytest.mark.integration]

_DIM = 768


def _no_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.db.http_vector_client as hvc
    import nexus.db.service_endpoint as se
    import nexus.db.t2._refreshable_client as rc

    monkeypatch.setattr(se, "discover_lease", lambda: (None, None))
    monkeypatch.setattr(hvc, "_discover_lease", lambda: (None, None))
    monkeypatch.setattr(hvc, "_lease_cache", None)
    monkeypatch.setattr(rc, "discover_lease_with_wait", lambda **_kw: (None, None))


def test_armed_box_runs_on_data_tokens_with_no_static_token(
    t2_service_env: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = t2_service_env
    from tests._engine_substrate import ensure_engine

    state = ensure_engine()
    from nexus.db.t2.http_token_store import HttpTokenStore, TokenAdminAuthError

    with HttpTokenStore(base_url=state["base_url"], _token=state["bearer"]) as admin:
        mint_locked = admin.issue_token(tenant, label="xzeml", scope="mint-locked")["token"]

    # Register while the static tenant token is still valid (see the
    # sibling nexus-wrwb7 e2e test for why registration must precede the
    # switch to self-mint).
    from nexus.corpus import ensure_collection_registered

    collection = f"knowledge__xzeml-{tenant}__bge-base-en-v15-768__v1"
    ensure_collection_registered(collection)

    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("NX_MINT_TOKEN", mint_locked)
    monkeypatch.setenv("NX_MINT_TENANT", tenant)
    _no_lease(monkeypatch)

    from nexus.db.data_token import reset_data_token_manager
    from nexus.db.service_endpoint import resolve_service_endpoint

    reset_data_token_manager()
    try:
        assert resolve_service_endpoint() == (state["base_url"], "")

        import nexus.db.http_vector_client as hvc

        client = hvc.HttpVectorClient(tenant=tenant)
        content = f"nexus-xzeml armed box, no static token ({tenant})"
        chash = hashlib.sha256(content.encode()).hexdigest()
        client.upsert_chunks_with_embeddings(
            collection, ids=[chash], documents=[content], embeddings=[[0.1] * _DIM],
            metadatas=[{"title": "xzeml", "chunk_text_hash": chash}],
        )
        present = client.get_collection(collection).get(ids=[chash], include=[])
        assert chash in (present.get("ids") or [])

        from nexus.db.t2.http_memory_store import HttpMemoryStore

        mem = HttpMemoryStore(tenant=tenant)
        mem.put(project="xzeml", title="armed", content="data tokens only")
        row = mem.get(project="xzeml", title="armed")
        assert row is not None and row["content"] == "data tokens only"

        # Token admin refuses data tokens by design, and there is no static
        # bearer to send: the verb must say so, not fail to resolve.
        with HttpTokenStore(tenant=tenant) as admin_armed:
            with pytest.raises(TokenAdminAuthError) as excinfo:
                admin_armed.list_tokens()
        msg = str(excinfo.value)
        assert "sent: no bearer (no static service_token is configured)" in msg
        assert "token-admin surface refuses those" in msg
        assert mint_locked not in msg
    finally:
        reset_data_token_manager()


def test_unarmed_box_still_refuses_to_resolve_without_a_token(monkeypatch) -> None:
    """The relaxation is scoped to mint-armed boxes: with no mint_token an
    empty bearer is still an error at resolution, as before."""
    from nexus.db.service_endpoint import resolve_service_endpoint

    monkeypatch.setenv("NX_SERVICE_URL", "https://managed.invalid")
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("NX_MINT_TOKEN", raising=False)
    _no_lease(monkeypatch)
    with pytest.raises(RuntimeError, match="no service_token is resolvable"):
        resolve_service_endpoint()
