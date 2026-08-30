# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-maf9l: the T1 session mint on an ARMED pass-through box, against the
real engine substrate.

The incident (2026-08-30, the first armed box, RDR-005 step (d)): the box's
persisted ``service_token`` is the scope=mint-locked credential — it can call
``POST /v1/data-tokens/mint`` and nothing else — and ``HttpTokenStore``
presented it to ``/v1/sessions/start``, so every T1 session mint 401'd the
moment the pre-armed leases expired, and T1 scratch died box-wide while
T2/T3 (which ride ``DataTokenManager``) kept working.

Three pins, all against a real engine and a real mint-locked credential
issued by the substrate's root bearer:

1. THE INCIDENT: ``/v1/sessions/start`` with the mint-locked bearer is
   rejected (the AuthFilter's nexus-868dq carve-out — mint scopes are
   rejected outside the mint surface).
2. THE FIX: ``HttpTokenStore(prefer_data_token=True)`` under the armed env
   shape (static token = mint-locked, ``NX_MINT_TOKEN`` configured) mints a
   DATA token and starts the session.
3. THE TTL CONTRACT: a data-scoped session mint is defaulted to the
   data-token ceiling and never exceeds it (SessionTokenHandler,
   nexus-t8abd: "a data-scoped bearer may not mint a session that outlives
   its own window"), and ``close_session`` works on the same path.
"""
from __future__ import annotations

import os

import httpx
import pytest

from nexus.db.t2.http_token_store import HttpTokenStore

# Same battery as the direct precedent (tests/db/test_data_token_manager_e2e.py):
# a real-engine token-lifecycle E2E belongs to the pre-tag integration battery,
# not every `pytest -n auto` push (R1 review finding).
pytestmark = [pytest.mark.integration]


@pytest.fixture()
def armed_tenant(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    """A fresh tenant with a REAL mint-locked credential, and the process
    environment shaped exactly like the armed box: the static service token
    IS the mint-locked credential, and minting is configured from it.

    Returns ``(base_url, tenant, mint_locked_token)``.
    """
    from tests._engine_substrate import ensure_engine, mint_test_tenant

    state = ensure_engine()
    tenant, tenant_token = mint_test_tenant(state)
    base_url = state["base_url"]

    # Root issues the tenant's mint-locked credential (operator-only route).
    with HttpTokenStore(base_url=base_url, tenant=tenant, _token=state["bearer"]) as admin:
        issued = admin.issue_token(tenant, label="armed-test", scope="mint-locked")
    mint_locked = issued["token"]

    # The armed box's env shape: the resolvable static token is mint-locked;
    # minting is configured from the same credential for this tenant.
    monkeypatch.setenv("NX_SERVICE_URL", base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", mint_locked)
    monkeypatch.setenv("NX_MINT_TOKEN", mint_locked)
    monkeypatch.setenv("NX_MINT_TENANT", tenant)

    from nexus.db.data_token import reset_data_token_manager

    reset_data_token_manager()
    yield base_url, tenant, mint_locked
    reset_data_token_manager()


def test_the_incident_a_mint_locked_bearer_cannot_start_a_session(
    armed_tenant: tuple[str, str, str],
) -> None:
    """Pin the failure exactly as measured on the armed box: the static
    (mint-locked) token on /v1/sessions/start is rejected, not honoured."""
    base_url, tenant, mint_locked = armed_tenant
    with (
        HttpTokenStore(base_url=base_url, tenant=tenant, _token=mint_locked) as store,
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        store.start_session("armed-incident-session")
    assert exc_info.value.response.status_code in (401, 403), (
        exc_info.value.response.status_code,
        exc_info.value.response.text,
    )


def test_the_fix_prefer_data_token_mints_the_session(
    armed_tenant: tuple[str, str, str],
) -> None:
    """The fix end to end on the real engine: bearer_for mints a DATA token
    with the mint-locked credential, the session starts, its TTL respects
    the data ceiling, and close works on the same path."""
    base_url, tenant, _ = armed_tenant

    with HttpTokenStore(tenant=tenant, prefer_data_token=True) as store:
        minted = store.start_session("armed-fix-session")
        assert minted["session_token"]
        assert minted["session_id"] == "armed-fix-session"
        # nexus-t8abd: a data-scoped mint defaults to min(24h, data ceiling)
        # — the session must not outlive the bearer's own window. Compute
        # the expectation from the SAME env knob the engine reads
        # (DataTokenHandler.ttlCeilingFromEnv, default 3600) so a substrate
        # configured with a different ceiling stays green (R1 review
        # finding: a magic-number exclusion flaked on ceiling >= 24h).
        ceiling = int(os.environ.get("NX_DATA_TOKEN_TTL_CEILING_SECONDS", "3600"))
        assert int(minted["expires_in_seconds"]) == min(86_400, ceiling), (
            minted,
            f"expected the data-scoped default min(86400, {ceiling})",
        )
        closed = store.close_session("armed-fix-session")
        assert closed["closed"] >= 1


def test_mint_t1_session_token_heals_on_the_armed_shape(
    armed_tenant: tuple[str, str, str],
) -> None:
    """The actual broken entry point from the incident: db.t1's mint wrapper
    (the stale-lease recovery path) succeeds on an armed box."""
    from nexus.db.t1 import mint_t1_session_token

    minted = mint_t1_session_token("armed-wrapper-session", context="nexus-maf9l armed-shape test")
    assert minted["session_token"]
    ceiling = int(os.environ.get("NX_DATA_TOKEN_TTL_CEILING_SECONDS", "3600"))
    assert 0 < int(minted["expires_in_seconds"]) <= min(86_400, ceiling)
