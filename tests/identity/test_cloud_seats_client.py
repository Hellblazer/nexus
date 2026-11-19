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
from structlog.testing import capture_logs

import nexus.db.service_endpoint as endpoint
import nexus.operators.delegated_identity as delegated
from nexus.commands.identity_cmd import identity_group
from nexus.identity import ops, token_contract, token_header
from nexus.identity.bind_client import (
    OLD_ENGINE_SEAT_REFUSAL, SEATS_NEED_ENGINE, SCOPE_DATA, SCOPE_TENANT, IdentityApiError, IdentityClient,
    is_old_engine_seat_refusal,
)
from nexus.identity.ops import CallerFacts, IdentityCommandError, IdentityEnv
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from nexus.operators.delegated_identity import DelegatedIdentity
from tests.identity.fakes import HOST, FakeClient, FakeProcesses

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
    (403, {"error": "identity_token_required", "message": "m"}, "no identity token was sent"),
    (403, {"error": "not_owner", "message": "m", "token_identity": "i3"}, "token names i3"),
    (409, {"error": "generation_superseded", "message": "m"}, "another process holds"),
    (401, {"error": "identity_token_expired", "message": "m"}, "has expired"),
    (401, {"error": "cross_tenant_identity_token", "message": "m"}, "another tenant"),
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


def _begin(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str,
           refusal: tuple[int, dict[str, Any]] | None = None) -> DelegatedIdentity | None:
    procs = FakeProcesses(tmp_path / "claude-sessions", host=HOST)
    procs.procs[os.getpid()] = T1
    parent = tmp_path / "parent.token"
    write_token_file(parent, _record("i2", token="parent-tok"))
    engine.answers[("POST", "/v1/identity/delegated")] = refusal or (
        200, {"id": "i9", "generation": 1, "identity_token": "child-tok", "identity_token_expires_in_seconds": 3600})
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


# ── review r1 ───────────────────────────────────────────────────────────────

_OLD_ENGINE = (403, {"error": "forbidden", "message": "a 'data' credential may not call DELEGATED on /v1/identity/delegated"})


def _dispatch_refused(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: tuple[int, dict], times: int) -> list[dict]:
    monkeypatch.setattr(delegated, "_SEATS_NEED_ENGINE_LOGGED", False, raising=False)   # once per process: module state
    with capture_logs() as logs:
        for _ in range(times):
            engine.requests.clear()
            assert _begin(engine, tmp_path, monkeypatch, SCOPE_DATA, refusal=refusal) is None
    return [e for e in logs if e["log_level"] == "warning"]


def test_two_dispatches_against_an_old_engine_say_so_once(engine: Engine, tmp_path: Path, monkeypatch) -> None:
    logs = _dispatch_refused(engine, tmp_path, monkeypatch, _OLD_ENGINE, times=2)
    assert [e["error"] for e in logs] == [SEATS_NEED_ENGINE], logs


def test_every_other_delegated_refusal_is_its_one_mapped_line_each_time(engine: Engine, tmp_path: Path, monkeypatch) -> None:
    logs = _dispatch_refused(engine, tmp_path, monkeypatch, (403, {"error": "not_owner", "message": "m", "token_identity": "i3"}), times=2)
    assert len(logs) == 2 and all("token names i3" in e["error"] and "identity engine" not in e["error"] for e in logs), logs


def test_the_delegation_path_does_not_import_the_cli_layer() -> None:
    """The one-line refusal wording lives beside the client, so ``operators`` need not import ``identity.ops``."""
    assert "identity.ops" not in Path(delegated.__file__).read_text(encoding="utf-8")


def test_a_named_token_file_wins_over_the_callers_own_token(engine: Engine, tmp_path: Path) -> None:
    """--token-file names the session whose identity is being promoted: a caller that holds a token of its own (it is a
    session too) must not send that one, or the engine answers not_owner."""
    path = tmp_path / "target.token"
    write_token_file(path, _record(token="target-tok"))
    result = CliRunner().invoke(identity_group, ["promote", "--token-file", str(path), "--", "i7", "tamsin"],
                                obj={"identity_env": _env(tmp_path, engine.client(), own=_record(token="caller-tok"))})
    assert result.exit_code == 0, result.output
    assert engine.headers == ["target-tok"]


# ── the refusal wording says whose token, per caller ────────────────────────


def _promote_refused(engine: Engine, tmp_path: Path, status: int, body: dict, *, token_file: Path | None = None,
                     own: TokenFile | None = None) -> str:
    engine.answers[("POST", "/v1/identity/i7/promote")] = (status, body)
    with pytest.raises(IdentityCommandError) as raised:
        ops.promote(_env(tmp_path, engine.client(), own=own), "i7", "tamsin", token_file=token_file)
    return str(raised.value)


def test_generation_superseded_names_the_token_that_was_sent(engine: Engine, tmp_path: Path) -> None:
    body = {"error": "generation_superseded", "message": "m"}
    path = tmp_path / "target.token"
    write_token_file(path, _record(token="target-tok"))
    assert "this process's token" in _promote_refused(engine, tmp_path, 409, body, own=_record())
    said = _promote_refused(engine, tmp_path, 409, body, token_file=path, own=_record())
    assert f"the token in {path}" in said and "this process's token" not in said


def test_an_expired_token_names_the_token_that_was_sent(engine: Engine, tmp_path: Path) -> None:
    path = tmp_path / "target.token"
    write_token_file(path, _record(token="target-tok"))
    said = _promote_refused(engine, tmp_path, 401, {"error": "identity_token_expired", "message": "m"}, token_file=path)
    assert f"the token in {path} has expired" in said


def test_no_refusal_for_a_script_or_a_token_file_tells_the_caller_to_run_it_inside_the_session(engine: Engine, tmp_path: Path) -> None:
    """The restart script and --token-file callers are not inside the session: their lines say what to do instead."""
    said = _promote_refused(engine, tmp_path, 403, {"error": "identity_token_required", "message": "m"}, own=None)
    assert "--token-file" in said and "inside the session that holds the identity" in said
    with pytest.raises(IdentityCommandError) as made:
        ops.create(_env(tmp_path, engine.client(), own=None), "tamsin")
    assert "run it inside" not in str(made.value) and "no live identity token" in str(made.value)
    engine.answers[("POST", "/v1/identity")] = (403, {"error": "identity_token_required", "message": "m"})
    with pytest.raises(IdentityCommandError) as refused:
        ops.create(_env(tmp_path, engine.client(), own=_record()), "tamsin")
    assert "--token-file" not in str(refused.value), "a create has no --token-file"


def test_the_delegated_no_token_line_is_about_the_dispatcher(engine: Engine, tmp_path: Path, monkeypatch) -> None:
    logs = _dispatch_refused(engine, tmp_path, monkeypatch, (403, {"error": "identity_token_required", "message": "m"}), times=1)
    assert len(logs) == 1 and "dispatch" in logs[0]["error"] and "--token-file" not in logs[0]["error"], logs


def test_a_created_seat_reports_the_created_by_the_engine_stamped(engine: Engine, tmp_path: Path) -> None:
    """In cloud mode the engine stamps ``created_by`` from the token (identity:<id>); the requested value is not what stuck."""
    engine.answers[("POST", "/v1/identity")] = (200, {"id": "i8", "generation": 1, "created_by": "identity:i7"})
    assert ops.create(_env(tmp_path, engine.client(), own=_record()), "tamsin", "restart") == [
        "created seat tamsin (i8), created by identity:i7"]
    engine.answers[("POST", "/v1/identity")] = (200, {"id": "i8", "generation": 1})
    assert ops.create(_env(tmp_path, engine.client(), own=_record()), "tamsin", "restart") == [
        "created seat tamsin (i8), created by restart"], "an engine that stamps nothing back: the requested one"


# ── the own token is the session-verified one ───────────────────────────────


def test_a_token_file_whose_sessions_do_not_include_the_callers_is_refused_for_promote(
        engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Bash child started before a cross-seat /resume holds the old session id: its ancestor's token file now names
    another identity. Seat calls use the same session check the fenced writes do, so nothing is sent."""
    path = tmp_path / "ancestor.token"
    write_token_file(path, TokenFile(identity="i2", generation=1, token="tok-i2", expires_at=9e9, session_id="new-sid", pid=4242,
                                     started_at=4_242_000, proc_start=None, session_ids=("new-sid",)))
    token_header.reset_for_tests()
    row = {"identity": "i1", "generation": 1, "current_generation": 1, "kind": "seat", "state": "live"}
    monkeypatch.setattr(token_header, "token_file_path", lambda: path)
    monkeypatch.setattr(token_header, "_resolver_client", lambda: FakeClient(sessions={"old-sid": row}))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "old-sid")
    env = _env(tmp_path, engine.client(), own=None)
    env.own_token = ops._real_own_token
    with pytest.raises(IdentityCommandError, match="two sources name different identities"):
        ops.promote(env, "i2", "tamsin")
    with pytest.raises(IdentityCommandError, match="two sources name different identities"):
        ops.create(env, "tamsin")
    assert engine.requests == [], "nothing was sent"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "new-sid")      # a session the file lists: sent
    ops.promote(env, "i2", "tamsin")
    assert engine.headers == ["tok-i2"]
    token_header.reset_for_tests()


# ── no token value is ever printed or logged ────────────────────────────────

SECRET = "tok-SECRET-9f3a71"

_REFUSALS = [
    (403, {"error": "identity_token_required", "message": "m"}),
    (403, {"error": "not_owner", "message": "m", "token_identity": "i3"}),
    (409, {"error": "generation_superseded", "message": "m"}),
    (409, {"error": "identity_not_held", "message": "m"}),
    (409, {"error": "not_ephemeral", "message": "m"}),
    (404, {"error": "unknown_identity", "message": "m"}),
    (403, {"error": "forbidden", "message": "m"}),
    (401, {"error": "identity_token_expired", "message": "m"}),
    (401, {"error": "cross_tenant_identity_token", "message": "m"}),
    (403, {"error": "forbidden", "message": "a 'data' credential may not call PROMOTE on /v1/identity/i7/promote"}),
    (500, {"error": "boom", "message": "the engine fell over"}),
]


@pytest.mark.parametrize("via", ["own-token", "token-file"])
@pytest.mark.parametrize(("status", "body"), _REFUSALS, ids=[b["error"] + ("-old" if "data" in b["message"] else "") for _, b in _REFUSALS])
def test_no_token_value_reaches_the_output_the_error_or_a_log_on_a_refusal(
        engine: Engine, tmp_path: Path, caplog: pytest.LogCaptureFixture, via: str, status: int, body: dict) -> None:
    engine.answers[("POST", "/v1/identity/i7/promote")] = (status, body)
    path = tmp_path / "target.token"
    write_token_file(path, _record(token=SECRET))
    own = _record(token=SECRET) if via == "own-token" else None
    args = ["promote", *(["--token-file", str(path)] if via == "token-file" else []), "--", "i7", "tamsin"]
    caplog.set_level("DEBUG")
    with capture_logs() as logs:
        result = CliRunner().invoke(identity_group, args, obj={"identity_env": _env(tmp_path, engine.client(), own=own)})
        with pytest.raises(IdentityCommandError) as raised:
            ops.promote(_env(tmp_path, engine.client(), own=own), "i7", "tamsin", token_file=path if via == "token-file" else None)
    assert engine.headers[0] == SECRET, "the token was sent: that is what makes the check mean something"
    assert result.exit_code == 1
    for where in (result.output, str(raised.value), caplog.text, str(logs)):
        assert SECRET not in where


@pytest.mark.parametrize("state", ["success", "expired-file", "no-file", "garbage-file"])
def test_no_token_value_reaches_the_output_on_the_token_file_path(engine: Engine, tmp_path: Path, state: str) -> None:
    path = tmp_path / "target.token"
    if state == "success":
        write_token_file(path, _record(token=SECRET))
    elif state == "expired-file":
        write_token_file(path, _record(token=SECRET, expires_at=1.0))
    elif state == "garbage-file":
        path.write_text(f'{{"token": "{SECRET}", not json', encoding="utf-8")
    result = CliRunner().invoke(identity_group, ["promote", "--token-file", str(path), "--", "i7", "tamsin"],
                                obj={"identity_env": _env(tmp_path, engine.client())})
    assert result.exit_code == (0 if state == "success" else 1), result.output
    assert SECRET not in result.output and SECRET not in str(result.exception)


def test_no_parent_token_value_reaches_a_delegation_log(engine: Engine, tmp_path: Path, monkeypatch) -> None:
    for refusal in (_OLD_ENGINE, (403, {"error": "not_owner", "message": "m"}), (401, {"error": "identity_token_expired", "message": "m"})):
        assert "parent-tok" not in str(_dispatch_refused(engine, tmp_path, monkeypatch, refusal, times=1))


# ── engine half, reviewed (8f226497f): session_held and bad_created_by ─────


def test_the_delegated_bind_carries_no_session_id(engine: Engine, tmp_path: Path, monkeypatch) -> None:
    """The engine refuses (409 session_held) a data-credential delegated create whose bind names a session id that is
    already indexed; the dispatcher has no session of its own for the child, so it sends none."""
    for scope in (SCOPE_DATA, SCOPE_TENANT):
        engine.requests.clear()
        assert _begin(engine, tmp_path, monkeypatch, scope) is not None
        assert "session_id" not in json.loads(engine.requests[0].content)["bind"]


def test_session_held_is_one_clear_line_for_a_delegation_and_a_cli_call(engine: Engine, tmp_path: Path, monkeypatch) -> None:
    refusal = (409, {"error": "session_held", "message": "m"})
    logs = _dispatch_refused(engine, tmp_path, monkeypatch, refusal, times=1)
    assert len(logs) == 1 and "session" in logs[0]["error"] and "identity engine" not in logs[0]["error"], logs
    engine.answers[("POST", "/v1/identity/i7/promote")] = refusal
    with pytest.raises(IdentityCommandError, match="session") as raised:
        ops.promote(_env(tmp_path, engine.client(), own=_record()), "i7", "tamsin")
    assert "session_held" not in str(raised.value) and "\n" not in str(raised.value)


@pytest.mark.parametrize("scope", [SCOPE_TENANT, SCOPE_DATA])
def test_bad_created_by_is_one_clear_line(engine: Engine, tmp_path: Path, scope: str) -> None:
    """Only a data credential's token may stamp ``identity:<id>`` as the creator; the engine refuses it from a tenant
    credential with 400 bad_created_by, which both scopes say in one line."""
    engine.answers[("POST", "/v1/identity")] = (400, {"error": "bad_created_by", "message": "m"})
    with pytest.raises(IdentityCommandError, match="created_by") as raised:
        ops.create(_env(tmp_path, engine.client(scope), own=_record()), "tamsin", "identity:i7")
    assert "bad_created_by:" not in str(raised.value) and "\n" not in str(raised.value)


def test_a_tenant_credential_never_sends_an_identity_prefixed_created_by(engine: Engine, tmp_path: Path) -> None:
    """ops sends the plain value its caller gave (``sam``, ``restart``); the ``identity:`` stamp is the engine's, from a
    token, and a tenant credential's request for one is refused (400 bad_created_by)."""
    env = _env(tmp_path, engine.client(SCOPE_TENANT), own=_record())
    ops.create(env, "a")
    ops.create(env, "b", "restart")
    ops.create(env, "c", "sam")
    assert [json.loads(r.content)["created_by"] for r in engine.requests] == ["sam", "restart", "sam"]
    assert not any(json.loads(r.content)["created_by"].startswith("identity:") for r in engine.requests)
