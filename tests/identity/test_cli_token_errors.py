# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLI HTTP layers' identity token (RDR-253 "The resolver", Step 3 item 10), against a stdlib HTTP server on
port 0 that answers a fixed script. Tenant-free: the engine's answers are the Step 2 contract written down in
``nexus.identity.token_contract`` (401 on an expired token, ``409 generation_superseded`` on a superseded one), so
these tests run before Step 2 lands. Covers: the header is sent only when the token file holds a live token; on 401
the file is re-read ONCE; on 409 ``generation_superseded`` the call stops and names the holder; no request that
carried a token is ever retried without it."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from nexus.identity import token_contract, token_header
from nexus.identity.token_file import TokenFile, write_token_file

HEADER = token_contract.IDENTITY_TOKEN_HEADER


class _Server:
    def __init__(self, script: list[tuple[int, dict[str, Any]]], on_request: Callable[[dict[str, str]], None] | None = None) -> None:
        self.script = list(script)
        self.seen: list[dict[str, str]] = []
        self._on_request = on_request
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                headers = {k: v for k, v in self.headers.items()}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.seen.append(headers)
                if outer._on_request:
                    outer._on_request(headers)
                status, body = outer.script.pop(0) if outer.script else (200, {})
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_DELETE = _answer

            def log_message(self, *args: Any) -> None:  # noqa: D401 - silence
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def tokens_sent(self) -> list[str | None]:
        lowered = [{k.lower(): v for k, v in h.items()} for h in self.seen]
        return [h.get(HEADER.lower()) for h in lowered]


@pytest.fixture
def serve() -> Iterator[Callable[..., _Server]]:
    started: list[_Server] = []

    def make(script: list[tuple[int, dict[str, Any]]], on_request: Callable[[dict[str, str]], None] | None = None) -> _Server:
        server = _Server(script, on_request)
        server.thread.start()
        started.append(server)
        return server

    yield make
    for server in started:
        server.httpd.shutdown()
        server.httpd.server_close()


@pytest.fixture
def token_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This process's token file, in tmp: the Claude-ancestor discovery is pointed at it."""
    path = tmp_path / "identity" / "4242.4242000.token"
    monkeypatch.setattr(token_header, "_discover", lambda: path)
    token_header.reset_for_tests()
    yield path
    token_header.reset_for_tests()


def _write(path: Path, token: str | None, expires_at: float | None = 4_000_000_000.0, generation: int = 3) -> None:
    write_token_file(path, TokenFile(identity="i5", generation=generation, token=token, expires_at=expires_at, session_id="s",
                                     pid=4242, started_at=4_242_000, proc_start=None))


def _store(url: str) -> RefreshableHttpStoreMixin:
    return RefreshableHttpStoreMixin(base_url=url, _token="bearer", tenant="t")


def test_the_token_is_sent_only_when_the_file_holds_a_live_one(serve, token_path: Path) -> None:
    server = serve([(200, {"ok": True})] * 4)
    store = _store(server.url)
    store._post("/v1/memory/put", {})                                      # no file at all: no header, as before
    _write(token_path, "tok-a")
    store._post("/v1/memory/put", {})
    _write(token_path, "tok-expired", expires_at=1.0)             # the file records it as expired: not sent
    store._post("/v1/memory/put", {})
    _write(token_path, None, expires_at=None)                     # a Step 1 engine issued none
    store._post("/v1/memory/put", {})
    assert server.tokens_sent() == [None, "tok-a", None, None]


def test_only_a_request_the_engine_fences_carries_the_token(serve, token_path: Path) -> None:
    """An identity token the engine cannot resolve is a 401 on every route it rides: an unfenced read or write must not
    carry one. Fenced: memory writes and mailbox claims; not fenced: reads, a send (out), expire, search."""
    _write(token_path, "tok-a")
    server = serve([(200, {"ok": True})] * 8)
    store = _store(server.url)
    store._post("/v1/tuples/in", {})                              # a claim: fenced
    store._get("/v1/memory/all")                                  # a read
    store._post("/v1/tuples/out", {})                             # a send is open to anyone
    store._post("/v1/memory/expire", {})                          # a tenant-wide sweep
    store._post("/v1/memory/search", {}, mutates=False)           # a search
    store._delete("/v1/memory/delete", {"project": "p"})          # a write: fenced
    assert server.tokens_sent() == ["tok-a", None, None, None, None, "tok-a"]


def test_a_process_with_no_claude_ancestor_walks_the_process_table_once_not_per_request(
        serve, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ancestor (and a miss) is cached for the life of the process."""
    import nexus.session as session

    walks: list[int] = []
    monkeypatch.setattr(session, "find_immediate_claude_pid", lambda *a, **k: walks.append(1) or 0)
    token_header.reset_for_tests()
    server = serve([(200, {"ok": True})] * 5)
    store = _store(server.url)
    for _ in range(5):
        store._post("/v1/memory/put", {})
    assert server.tokens_sent() == [None] * 5 and len(walks) == 1
    token_header.reset_for_tests()


def test_a_bash_child_whose_session_belongs_to_another_identity_is_refused_before_anything_is_sent(
        serve, token_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The token file names i2 (the process switched seats) and lists only its sessions; a child with the old session id
    asks the resolver, which maps it to i1: the write fails with the resolver's message and nothing is sent. A session
    the file lists, or one the engine maps to i2, is sent."""
    from nexus.identity.resolver import IdentityConflictError
    from tests.identity.fakes import FakeClient

    write_token_file(token_path, TokenFile(identity="i2", generation=1, token="tok-i2", expires_at=4_000_000_000.0,
                                           session_id="new-sid", pid=4242, started_at=4_242_000, proc_start=None,
                                           session_ids=("new-sid",)))
    row = lambda ident: {"identity": ident, "generation": 1, "current_generation": 1, "kind": "seat", "state": "live"}  # noqa: E731
    fake = FakeClient(sessions={"old-sid": row("i1"), "branch-sid": row("i2")})
    monkeypatch.setattr(token_header, "_resolver_client", lambda: fake)
    server = serve([(200, {"ok": True})] * 3)
    store = _store(server.url)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "old-sid")
    with pytest.raises(IdentityConflictError) as err:
        store._post("/v1/memory/put", {})
    assert "i1" in str(err.value) and "i2" in str(err.value) and server.seen == []

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "new-sid")       # listed: no lookup at all
    fake.calls.clear()
    store._post("/v1/memory/put", {})
    assert fake.calls == []
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "branch-sid")    # unlisted, the engine maps it to the same identity
    store._post("/v1/memory/put", {})
    store._post("/v1/memory/put", {})
    assert server.tokens_sent() == ["tok-i2"] * 3 and [c for c in fake.calls if c[0] == "session"] == [("session", "branch-sid")]


def test_a_401_re_reads_the_file_once_and_retries_with_what_it_now_holds(serve, token_path: Path) -> None:
    """The token expired in flight; the MCP server refreshed the file meanwhile. One re-read, one retry, new token."""
    _write(token_path, "tok-old")

    def refresh_when_the_old_token_is_seen(headers: dict[str, str]) -> None:
        if headers.get(HEADER) == "tok-old":
            _write(token_path, "tok-new")

    server = serve([(401, {"error": "unauthorized"}), (200, {"ok": True})], on_request=refresh_when_the_old_token_is_seen)
    assert _store(server.url)._post("/v1/memory/put", {}) == {"ok": True}
    assert server.tokens_sent() == ["tok-old", "tok-new"]


def test_a_401_with_an_unchanged_file_is_never_retried_without_the_token(serve, token_path: Path) -> None:
    """The file still holds the rejected token: nothing new to try. Whatever the layers below retry carries the
    token too: a fenced write is refused, never downgraded to a tokenless write."""
    _write(token_path, "tok-same")
    server = serve([(401, {"error": "unauthorized"})] * 4)
    with pytest.raises(Exception):
        _store(server.url)._post("/v1/memory/put", {})
    sent = server.tokens_sent()
    assert sent and set(sent) == {"tok-same"}, f"every attempt carried the token: {sent}"


def test_a_superseded_generation_stops_the_call_and_names_the_holder(serve, token_path: Path) -> None:
    _write(token_path, "tok-gen3", generation=3)
    body = {"error": "generation_superseded", "message": "i5 is at generation 4; this token is for 3", "identity": "i5",
            "token_generation": 3, "current_generation": 4, "state": "live", "holder": {"host": "mini", "pid": 777}}
    server = serve([(409, body), (200, {"ok": True})])
    with pytest.raises(token_header.GenerationSupersededError) as err:
        _store(server.url)._post("/v1/memory/put", {})
    assert len(server.seen) == 1, "a superseded write is not retried"
    text = str(err.value)
    assert "i5" in text and "pid 777" in text and "mini" in text and "generation 4" in text
    assert (err.value.identity, err.value.current_generation, err.value.holder) == ("i5", 4, {"host": "mini", "pid": 777})


def test_an_identity_nobody_holds_is_a_typed_error_naming_the_last_holder(serve, token_path: Path) -> None:
    _write(token_path, "tok")
    server = serve([(409, {"error": "identity_not_held", "identity": "i5", "state": "ended", "holder": {"host": "mini", "pid": 9}})])
    with pytest.raises(token_header.IdentityNotHeldError) as err:
        _store(server.url)._post("/v1/memory/put", {})
    assert (err.value.identity, err.value.state) == ("i5", "ended") and "pid 9" in str(err.value) and len(server.seen) == 1


def test_another_409_is_not_mistaken_for_a_superseded_generation(serve, token_path: Path) -> None:
    """The engine's other 409s (a held bind, a conflict) keep their own errors."""
    _write(token_path, "tok")
    server = serve([(409, {"error": "generation_conflict", "message": "stale"})])
    with pytest.raises(Exception) as err:
        _store(server.url)._post("/v1/memory/put", {})
    assert not isinstance(err.value, token_header.GenerationSupersededError)


def test_a_delegated_child_sends_the_token_its_environment_names(serve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    delegated = tmp_path / "delegated" / "i9.token"
    write_token_file(delegated, TokenFile(identity="i9", generation=1, token="child-token", expires_at=4_000_000_000.0,
                                          session_id="c", pid=1, started_at=1, proc_start=None))
    monkeypatch.setenv("NX_IDENTITY_TOKEN", str(delegated))
    token_header.reset_for_tests()
    server = serve([(200, {})])
    _store(server.url)._post("/v1/memory/put", {})
    assert server.tokens_sent() == ["child-token"]
    token_header.reset_for_tests()
