# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 option 2, the client half (nexus-hczdo): in cloud mode the data credential makes, promotes and delegates
seats through the caller's identity token. Tenant-free: an httpx MockTransport stands for the engine.

Wire shape (folio, 2026-11-19): routes and bodies are unchanged; a data-credential call adds ``X-Nexus-Identity-Token``.
Tenant-credential behaviour is unchanged on every route, so the header goes only where the credential is data scope:
an identity token the engine cannot resolve is a 401 on every route it rides.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

import nexus.db.service_endpoint as endpoint
from nexus.commands.identity_cmd import identity_group
from nexus.identity import ops, token_contract
from nexus.identity.bind_client import (
    OLD_ENGINE_SEAT_REFUSAL, SEATS_NEED_ENGINE, SCOPE_DATA, SCOPE_TENANT, IdentityApiError, IdentityClient,
    is_old_engine_seat_refusal,
)
from nexus.identity.ops import CallerFacts, IdentityCommandError, IdentityEnv
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from nexus.operators.delegated_identity import DelegatedIdentity
from tests.identity.fakes import HOST, FakeProcesses

HEADER = token_contract.IDENTITY_TOKEN_HEADER
T1 = "2026-11-17T09:00:00Z"


class Engine:
    """Records every request; answers by route with ``answers[(method, path)]`` or a default 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.answers.get((request.method, request.url.path), (200, {"id": "i7", "generation": 1}))
        return httpx.Response(status, json=body)

    def client(self, scope: str = SCOPE_DATA) -> IdentityClient:
        return IdentityClient("http://engine.test", "bearer", scope=scope, http=httpx.Client(transport=httpx.MockTransport(self)))

    @property
    def headers(self) -> list[str | None]:
        return [r.headers.get(HEADER) for r in self.requests]


@pytest.fixture
def engine() -> Engine:
    return Engine()


def _record(identity: str = "i7", token: str | None = "tok-i7", expires_at: float | None = 9e9) -> TokenFile:
    return TokenFile(identity=identity, generation=1, token=token, expires_at=expires_at, session_id="sid-1", pid=111,
                     started_at=111_000, proc_start=None, host=HOST)


def _env(tmp_path: Path, client: Any, own: TokenFile | None = None) -> IdentityEnv:
    return IdentityEnv(client=client, procs=FakeProcesses(tmp_path / "claude-sessions", host=HOST),
                       identity_dir=tmp_path / "identity", environ={}, caller=lambda: CallerFacts(claude_pid=0, ancestors=()),
                       clock=lambda: 1_000.0, own_token=lambda: own)


# ── the client: each call sends the token it is given ───────────────────────


@pytest.mark.parametrize("call", [
    lambda c, **kw: c.create_seat("tamsin", **kw),
    lambda c, **kw: c.promote("i7", "tamsin", **kw),
    lambda c, **kw: c.create_delegated("i7", display_name=None, evidence={"pid": 1}, **kw),
], ids=["create_seat", "promote", "create_delegated"])
def test_a_seat_call_sends_the_token_it_is_given_and_none_otherwise(engine: Engine, call) -> None:
    call(engine.client(), token="tok-a")
    call(engine.client(), token=_record(token="tok-from-file"))
    call(engine.client())
    assert engine.headers == ["tok-a", "tok-from-file", None]


def test_a_token_file_with_no_live_token_sends_nothing(engine: Engine) -> None:
    engine.client().promote("i7", "tamsin", token=_record(token=None))
    assert engine.headers == [None]


def test_the_scope_is_tenant_unless_the_credential_is_minted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A box with a static service token holds the tenant credential; a box that mints data tokens holds the data one."""
    monkeypatch.setattr(endpoint, "resolve_service_endpoint", lambda: ("http://engine.test", "static"))
    assert IdentityClient.from_config().scope == SCOPE_TENANT
    monkeypatch.setattr(endpoint, "resolve_service_endpoint", lambda: ("http://engine.test", ""))
    assert IdentityClient.from_config().scope == SCOPE_DATA
    assert IdentityClient("http://engine.test", "x").scope == SCOPE_TENANT


# ── nx identity create / promote ────────────────────────────────────────────


def test_promote_in_cloud_mode_sends_the_sessions_own_token(engine: Engine, tmp_path: Path) -> None:
    lines = ops.promote(_env(tmp_path, engine.client(), own=_record()), "i7", "tamsin")
    assert lines[0] == "i7 is now the seat tamsin"
    assert engine.headers == ["tok-i7"] and engine.requests[0].url.path == "/v1/identity/i7/promote"
    assert json.loads(engine.requests[0].content) == {"name": "tamsin"}, "the body is unchanged"


def test_promote_through_the_cli_takes_a_token_file_for_a_session_that_is_not_the_caller(engine: Engine, tmp_path: Path) -> None:
    """The restart script promotes another session's ephemeral: it names that session's token file."""
    path = tmp_path / "target.token"
    write_token_file(path, _record(token="target-tok"))
    result = CliRunner().invoke(identity_group, ["promote", "--token-file", str(path), "--", "i7", "tamsin"],
                                obj={"identity_env": _env(tmp_path, engine.client())})
    assert result.exit_code == 0, result.output
    assert engine.headers == ["target-tok"]


def test_a_token_file_that_holds_no_live_token_is_refused_before_any_call(engine: Engine, tmp_path: Path) -> None:
    path = tmp_path / "old.token"
    write_token_file(path, _record(token="x", expires_at=1.0))
    result = CliRunner().invoke(identity_group, ["promote", "--token-file", str(path), "--", "i7", "tamsin"],
                                obj={"identity_env": _env(tmp_path, engine.client())})
    assert result.exit_code == 1 and "no live identity token" in result.output and engine.requests == []


def test_tenant_credential_calls_are_unchanged(engine: Engine, tmp_path: Path) -> None:
    """A tenant credential never carries the identity token on a seat call, even when the process holds one."""
    env = _env(tmp_path, engine.client(SCOPE_TENANT), own=_record())
    ops.create(env, "tamsin")
    ops.promote(env, "i7", "tamsin")
    path = tmp_path / "t.token"
    write_token_file(path, _record(token="target-tok"))
    ops.promote(env, "i7", "tamsin", token_file=path)
    assert engine.headers == [None, None, None]


def test_create_in_cloud_mode_sends_the_sessions_token_and_refuses_without_one(engine: Engine, tmp_path: Path) -> None:
    assert ops.create(_env(tmp_path, engine.client(), own=_record()), "tamsin", "restart")[0].startswith("created seat tamsin")
    assert engine.headers == ["tok-i7"]
    assert json.loads(engine.requests[0].content) == {"kind": "seat", "name": "tamsin", "created_by": "restart"}
    with pytest.raises(IdentityCommandError, match="no live identity"):
        ops.create(_env(tmp_path, engine.client(), own=None), "tamsin")
    assert len(engine.requests) == 1, "a session with no live identity makes no call"


# ── the engine's refusals, one clear line each ──────────────────────────────


@pytest.mark.parametrize(("status", "body", "want"), [
    (403, {"error": "identity_token_required", "message": "m"}, "no live identity token"),
    (403, {"error": "not_owner", "message": "m", "token_identity": "i3"}, "token names i3"),
    (409, {"error": "generation_superseded", "message": "m"}, "another process holds"),
    (409, {"error": "identity_not_held", "message": "m"}, "no process holds"),
    (409, {"error": "not_ephemeral", "message": "m"}, "not an ephemeral"),
    (404, {"error": "unknown_identity", "message": "m"}, "no identity i7"),
    (403, {"error": "forbidden", "message": "m"}, "may not make, promote or delegate seats"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_each_refusal_code_is_one_clear_line(engine: Engine, tmp_path: Path, status: int, body: dict, want: str) -> None:
    engine.answers[("POST", "/v1/identity/i7/promote")] = (status, body)
    with pytest.raises(IdentityCommandError, match=want) as raised:
        ops.promote(_env(tmp_path, engine.client(), own=_record()), "i7", "tamsin")
    assert "\n" not in str(raised.value)


def test_an_older_engine_is_named_by_its_version(engine: Engine, tmp_path: Path) -> None:
    refusal = {"error": "forbidden", "message": "a 'data' credential may not call PROMOTE on /v1/identity/i7/promote"}
    engine.answers[("POST", "/v1/identity/i7/promote")] = (403, refusal)
    with pytest.raises(IdentityCommandError) as raised:
        ops.promote(_env(tmp_path, engine.client(), own=_record()), "i7", "tamsin")
    assert str(raised.value) == SEATS_NEED_ENGINE == "seats need engine 0.1.197 in cloud mode"


def test_only_a_403_that_starts_with_the_data_credential_wording_is_the_old_engine() -> None:
    assert OLD_ENGINE_SEAT_REFUSAL == "a 'data' credential may not call"
    assert is_old_engine_seat_refusal(IdentityApiError(403, "forbidden", "a 'data' credential may not call CREATE on /v1/identity"))
    assert not is_old_engine_seat_refusal(IdentityApiError(403, "forbidden", "scope is neither tenant nor data"))
    assert not is_old_engine_seat_refusal(IdentityApiError(409, "forbidden", "a 'data' credential may not call X"))


# ── delegated identities ────────────────────────────────────────────────────


def _begin(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str) -> DelegatedIdentity | None:
    procs = FakeProcesses(tmp_path / "claude-sessions", host=HOST)
    procs.procs[os.getpid()] = T1
    parent = tmp_path / "parent.token"
    write_token_file(parent, _record("i2", token="parent-tok"))
    engine.answers[("POST", "/v1/identity/delegated")] = (200, {"id": "i9", "generation": 1, "identity_token": "child-tok",
                                                                "identity_token_expires_in_seconds": 3600})
    return DelegatedIdentity.begin(label="child", environ={"NX_IDENTITY_TOKEN": str(parent)}, client=engine.client(scope),
                                   procs=procs, identity_dir=tmp_path / "identity")


def test_a_delegated_identity_in_cloud_mode_sends_the_parents_token(engine: Engine, tmp_path: Path, monkeypatch) -> None:
    child = _begin(engine, tmp_path, monkeypatch, SCOPE_DATA)
    assert child is not None and child.identity == "i9"
    assert engine.headers == ["parent-tok"]
    assert json.loads(engine.requests[0].content)["parent"] == "i2"


def test_a_delegated_identity_under_a_tenant_credential_sends_no_token(engine: Engine, tmp_path: Path, monkeypatch) -> None:
    assert _begin(engine, tmp_path, monkeypatch, SCOPE_TENANT) is not None
    assert engine.headers == [None]
