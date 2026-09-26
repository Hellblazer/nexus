# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-xzeml: a refused token-admin call names the credential and the remedy.

Against the REAL engine substrate: a dead static bearer on
``/v1/service-tokens/issue`` used to surface as httpx's bare "Client error
'401 Unauthorized'" traceback. It must now raise ``TokenAdminAuthError``
(still an ``httpx.HTTPStatusError`` for the T1 session callers) whose message
says which token was sent and where it came from, and the CLI must print that
message with no traceback.
"""
from __future__ import annotations

import hashlib

import httpx
import pytest
from click.testing import CliRunner

from nexus.db.t2.http_token_store import HttpTokenStore, TokenAdminAuthError
from tests._engine_substrate import ensure_engine, mint_test_tenant

_DEAD = "dead-static-token-for-xzeml-000000000000000"


@pytest.fixture
def engine() -> dict:
    return ensure_engine()


def test_dead_static_token_names_itself_and_the_remedy(engine, monkeypatch) -> None:
    monkeypatch.setenv("NX_SERVICE_TOKEN", _DEAD)
    monkeypatch.delenv("NX_MINT_TOKEN", raising=False)
    with HttpTokenStore(base_url=engine["base_url"], tenant="default") as store:
        with pytest.raises(TokenAdminAuthError) as excinfo:
            store.issue_token("default", label="x")
    err = excinfo.value
    assert isinstance(err, httpx.HTTPStatusError), "T1 session callers catch HTTPStatusError"
    assert err.response.status_code == 401
    msg = str(err)
    assert msg.startswith(f"401 Unauthorized from {engine['base_url']}/v1/service-tokens/issue")
    assert "sent: the static token from NX_SERVICE_TOKEN env" in msg
    assert hashlib.sha256(_DEAD.encode()).hexdigest()[:8] in msg
    assert "expired, revoked, or was issued for a different endpoint" in msg
    assert "nx config set service_token" in msg
    assert _DEAD not in msg, "the raw token must never be echoed"
    assert "mint_token" not in msg, "an unarmed box gets no data-token hint"


def test_armed_box_is_told_data_tokens_cannot_administer(engine, monkeypatch) -> None:
    monkeypatch.setenv("NX_SERVICE_TOKEN", _DEAD)
    monkeypatch.setenv("NX_MINT_TOKEN", "some-mint-credential")
    with HttpTokenStore(base_url=engine["base_url"], tenant="default") as store:
        with pytest.raises(TokenAdminAuthError) as excinfo:
            store.list_tokens()
    assert "token-admin surface refuses those" in str(excinfo.value)


def test_cross_tenant_issue_carries_the_engines_403_reason(engine, monkeypatch) -> None:
    tenant, token = mint_test_tenant(engine)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)
    monkeypatch.delenv("NX_MINT_TOKEN", raising=False)
    with HttpTokenStore(base_url=engine["base_url"], tenant=tenant) as store:
        with pytest.raises(TokenAdminAuthError) as excinfo:
            store.issue_token("some-other-tenant")
    msg = str(excinfo.value)
    assert excinfo.value.response.status_code == 403
    assert msg.startswith("403 Forbidden from ")
    assert "endpoint said: " in msg
    assert "acts only on its own tenant" in msg
    assert token not in msg


def test_cli_prints_the_message_not_a_traceback(engine, monkeypatch) -> None:
    from nexus.cli import main

    monkeypatch.setenv("NX_SERVICE_URL", engine["base_url"])
    monkeypatch.setenv("NX_SERVICE_TOKEN", _DEAD)
    monkeypatch.delenv("NX_MINT_TOKEN", raising=False)
    result = CliRunner().invoke(
        main, ["service", "token", "issue", "--tenant", "default", "--label", "x"]
    )
    assert result.exit_code == 1, result.output
    assert "Error: 401 Unauthorized from" in result.output
    assert "sent: the static token from NX_SERVICE_TOKEN env" in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, httpx.HTTPStatusError) or isinstance(
        result.exception, SystemExit
    )


# ── nx doctor row: a dead static service_token is named, not hidden ─────────


def _row(monkeypatch, *, url: str | None, token: str | None, mint: str | None):
    from nexus.health import _check_static_service_token

    for name, value in (("NX_SERVICE_URL", url), ("NX_SERVICE_TOKEN", token),
                        ("NX_MINT_TOKEN", mint)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    [row] = _check_static_service_token()
    return row


def test_doctor_row_is_not_applicable_on_a_virgin_box(monkeypatch) -> None:
    row = _row(monkeypatch, url=None, token=None, mint=None)
    assert row.ok and not row.warn
    assert "not applicable" in row.detail


def test_doctor_row_passes_a_live_token(engine, monkeypatch) -> None:
    _tenant, token = mint_test_tenant(engine)
    row = _row(monkeypatch, url=engine["base_url"], token=token, mint=None)
    assert row.ok, row.detail
    assert "accepted by" in row.detail


def test_doctor_row_warns_on_an_armed_box_with_a_dead_token(engine, monkeypatch) -> None:
    row = _row(monkeypatch, url=engine["base_url"], token=_DEAD, mint="m")
    assert not row.ok and row.warn
    assert "refuses it (HTTP 401" in row.detail
    assert "token-admin commands" in row.detail
    assert _DEAD not in row.detail
    assert row.fix_suggestions


def test_doctor_row_fails_an_unarmed_box_with_a_dead_token(engine, monkeypatch) -> None:
    row = _row(monkeypatch, url=engine["base_url"], token=_DEAD, mint=None)
    assert not row.ok and not row.warn
    assert "Every command that reaches the service" in row.detail


def test_redaction_happens_before_truncation() -> None:
    """A token cut by the 200-char limit must not leak as a prefix."""
    from nexus.db.t2.http_token_store import _endpoint_reason

    token = "T" * 64
    body = "x" * 180 + token
    resp = httpx.Response(401, text=body, request=httpx.Request("POST", "http://h/p"))
    reason = _endpoint_reason(resp, token)
    assert "TTTT" not in reason
    assert len(reason) <= 200


def test_local_supervisor_401_does_not_recommend_config_yml(engine, monkeypatch) -> None:
    """With no service_url, config.yml's service_token is never read, so the
    remedy must not point there."""
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.setenv("NX_SERVICE_TOKEN", _DEAD)
    monkeypatch.delenv("NX_MINT_TOKEN", raising=False)
    with HttpTokenStore(base_url=engine["base_url"], tenant="default") as store:
        with pytest.raises(TokenAdminAuthError) as excinfo:
            store.list_tokens()
    msg = str(excinfo.value)
    assert "nx config set service_token" not in msg
    assert "nx daemon service start" in msg


def test_str_is_one_line_for_structured_logs(engine, monkeypatch) -> None:
    """Session callers log str(exc) into single-line structlog records."""
    monkeypatch.setenv("NX_SERVICE_TOKEN", _DEAD)
    monkeypatch.delenv("NX_MINT_TOKEN", raising=False)
    with HttpTokenStore(base_url=engine["base_url"], tenant="default") as store:
        with pytest.raises(TokenAdminAuthError) as excinfo:
            store.list_tokens()
    assert "\n" not in str(excinfo.value)
    assert "401 Unauthorized" in str(excinfo.value), "mcp/core._is_unauthorized_mint_failure keys on it"
    assert len(excinfo.value.lines) >= 3


def test_non_object_json_body_does_not_crash_the_message() -> None:
    from nexus.db.t2.http_token_store import _endpoint_reason

    req = httpx.Request("POST", "http://h/p")
    for body in ('"unauthorized"', "[1, 2]", "null"):
        resp = httpx.Response(401, content=body.encode(),
                              headers={"content-type": "application/json"}, request=req)
        assert isinstance(_endpoint_reason(resp, "tok"), str)


def test_refused_data_token_gets_a_mint_remedy(monkeypatch) -> None:
    store = HttpTokenStore(base_url="http://h", _token="t")
    store._using_data_token = True  # noqa: SLF001 — the branch under test
    resp = httpx.Response(401, json={"error": "unauthorized"},
                          request=httpx.Request("POST", "http://h/v1/sessions/start"))
    lines = store._auth_error_lines("/v1/sessions/start", resp)  # noqa: SLF001
    text = "\n".join(lines)
    assert "a data token minted from mint_token" in text
    assert "check mint_token" in text
    assert "nx config set service_token" not in text
    store.close()


def test_doctor_row_passes_a_live_mint_scoped_token(engine, monkeypatch) -> None:
    """A 403 on /v1/_whoami is the engine's scope guard for a LIVE
    mint-locked bearer (a dead token is a 401), so the row must not call
    it refused."""
    tenant, _token = mint_test_tenant(engine)
    with HttpTokenStore(base_url=engine["base_url"], _token=engine["bearer"]) as admin:
        mint_locked = admin.issue_token(tenant, label="xzeml-403", scope="mint-locked")["token"]
    row = _row(monkeypatch, url=engine["base_url"], token=mint_locked, mint="m")
    assert row.ok, row.detail
    assert "mint-scoped credential" in row.detail
    assert mint_locked not in row.detail
