# SPDX-License-Identifier: AGPL-3.0-or-later
"""The credentials page's token check against a REAL local engine (RDR-228 Phase 2 Step 5,
nexus-sv2wx.18, residual 11 of nexus-9i4d4).

Sam, 2026-10-15: ``/health`` and ``/version`` stay for "the service answers, and which
version", and the token is checked against one authenticated, read-only route under
``/v1`` that refuses a wrong token on every engine. The route is ``GET /v1/_whoami``
(``WhoamiHandler``; the engine's ``AuthFilter`` fronts it): it is the call the doctor's
"Static service_token" row already makes, it is a read, and it answers 401 to a token
the engine does not know and 403 to a live mint-scoped one, which the page then proves
with its tenant by one data-token mint.

This file proves it on the engine substrate (the PG and jar every substrate test uses),
not on a fake: the engine answers ``/health`` and ``/version`` to ANY bearer, and
``/v1/_whoami`` only to a token it minted. It lives outside ``tests/hooks/`` because the
jobs that run that directory carry no engine.
"""
from __future__ import annotations

import secrets

import httpx
import pytest

from nexus.db.t2.http_token_store import HttpTokenStore
from tests._engine_substrate import ensure_engine, minted_test_tenant
from tests.hooks.test_nx_setup import _load_module


@pytest.fixture
def engine() -> dict:
    return ensure_engine()


def test_the_engine_answers_health_and_version_to_any_token_and_refuses_one_on_v1(engine: dict) -> None:
    """The premise of Sam's decision, measured on the pinned engine: neither unauthenticated route can
    carry the token check, and the authenticated route can."""
    wrong = {"Authorization": f"Bearer {secrets.token_urlsafe(24)}"}
    for route in ("/health", "/version"):
        assert httpx.get(engine["base_url"] + route, headers=wrong, timeout=30).status_code == 200, route
    assert httpx.get(engine["base_url"] + "/v1/_whoami", headers=wrong, timeout=30).status_code == 401
    assert httpx.get(engine["base_url"] + "/v1/_whoami", timeout=30).status_code == 401, "no token at all is refused too"


def test_a_wrong_token_fails_the_v1_route_and_a_right_one_passes_on_a_real_engine(engine: dict) -> None:
    mod = _load_module()
    wrong = secrets.token_urlsafe(24)
    with minted_test_tenant(engine) as (_tenant, token):
        ok, text, kind = mod.check_pair(engine["base_url"], token)
        assert ok is True and kind == "static", text
        assert "version" in text.lower() and token not in text
        bad_ok, bad_text, _bad_kind = mod.check_pair(engine["base_url"], wrong)
        assert bad_ok is False and "refus" in bad_text.lower()
        assert wrong not in bad_text and token not in bad_text
        # The same engine, the same address: only the token differs.
        ok_again, _text, _kind = mod.check_pair(engine["base_url"] + "/", token)
        assert ok_again is True


def test_a_live_mint_credential_is_accepted_with_its_own_tenant_and_refused_with_another(engine: dict) -> None:
    """The engine answers 403 to a live mint-locked bearer on ``/v1/_whoami`` (a dead token is a 401). The page then
    proves it with its tenant by one mint (``POST /v1/data-tokens/mint``), which the engine answers 200 for the bound
    tenant and 403 for any other."""
    mod = _load_module()
    with minted_test_tenant(engine) as (tenant, _token):
        with HttpTokenStore(base_url=engine["base_url"], _token=engine["bearer"]) as admin:
            mint_locked = admin.issue_token(tenant, label="sv2wx18-403", scope="mint-locked")["token"]
        assert httpx.get(
            engine["base_url"] + "/v1/_whoami", headers={"Authorization": f"Bearer {mint_locked}"}, timeout=30,
        ).status_code == 403
        ok, text, kind = mod.check_pair(engine["base_url"], mint_locked, tenant)
        assert ok is True and kind == "mint", text
        assert mint_locked not in text
        ok, text, kind = mod.check_pair(engine["base_url"], mint_locked, "not-" + tenant)
        assert ok is False and kind == "" and "another tenant" in text.lower() and mint_locked not in text
        ok, text, _kind = mod.check_pair(engine["base_url"], mint_locked, "")
        assert ok is False and "tenant" in text.lower()
