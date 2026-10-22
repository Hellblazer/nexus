# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""F3 step 2 client half (bead nexus-ztwp4): HttpTupleStore sends the T1
session TOKEN (``NX_T1_SESSION``, never the identifier ``NX_T1_SESSION_ID``) as
``X-Nexus-T1-Session`` on its own requests, drops it once on a 401 so a stale
token still writes (null stamp), and ``TupleRow`` carries ``sender_session``
with a missing key kept distinct from a null value.

All against a fake transport: the header and 401 logic is client-side and
needs no engine. The stamp itself (engine half, nexus-fetre) is proven by the
engine's own tests.
"""
from __future__ import annotations

import logging

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from nexus.commands.tuple_cmd import _row_dict
from nexus.db.t2._refreshable_client import build_shared_t2_client
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.db.t2.http_tuple_store import HttpTupleStore, _body_to_tuple_row
from nexus.mcp.core import _tuple_row_to_dict

HEADER = "X-Nexus-T1-Session"
TOKEN = "tok-secret-aaaa"
SESSION_ID = "sid-not-the-token"

_ROW = {
    "id": "a" * 64, "subspace": "mailbox/x", "template": "mailbox/<address>",
    "keys": {"to": "x"}, "dims": {"from": "y"}, "body": "hi",
    "claim_state": None, "claimant": None, "lease_until": None,
    "attempts": 0, "consumed_at": None, "consumed_by": None,
    "expires_at": None, "created_at": None,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_SERVICE_TOKEN", "data-token")
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.setenv("NX_T1_SESSION_ID", SESSION_ID)


@pytest.fixture
def info_logs():
    """The suite filters structlog at WARNING, which drops info events before
    capture_logs sees them; lower the filter for the body and put it back."""
    old = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=old)


def _store(handler) -> HttpTupleStore:
    store = HttpTupleStore(base_url="http://mock.test")
    store._client = httpx.Client(transport=httpx.MockTransport(handler))
    reresolves: list[int] = []
    # The mixin's data-token path: counted, and made a no-op so a test sees it fire.
    store._invalidate_and_reresolve = lambda: reresolves.append(1)  # type: ignore[method-assign]
    store.reresolves = reresolves  # type: ignore[attr-defined]
    return store


def _ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/out"):
        return httpx.Response(200, json={"id": "b" * 64})
    return httpx.Response(200, json={"tuples": [_ROW]})


class TestHeader:
    def test_header_carries_the_token_never_the_id(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _ok(request)

        store = _store(handler)
        store.out("mailbox/x", {"to": "x"}, {"from": "y"}, "hi", nonce="n")
        store.rd("mailbox/x", {"to": "x"})
        assert [r.headers.get(HEADER) for r in seen] == [TOKEN, TOKEN]
        assert all(SESSION_ID not in str(r.headers) for r in seen)

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_absent_or_blank_env_sends_no_header(self, monkeypatch, value) -> None:
        if value is not None:
            monkeypatch.setenv("NX_T1_SESSION", value)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _ok(request)

        _store(handler).out("mailbox/x", {"to": "x"}, {"from": "y"}, nonce="n")
        assert HEADER not in seen[0].headers

    def test_token_is_read_per_call_so_a_handoff_swap_is_seen(self, monkeypatch) -> None:
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get(HEADER))
            return _ok(request)

        store = _store(handler)
        monkeypatch.setenv("NX_T1_SESSION", "first")
        store.rd("mailbox/x", {"to": "x"})
        monkeypatch.setenv("NX_T1_SESSION", "second")
        store.rd("mailbox/x", {"to": "x"})
        monkeypatch.delenv("NX_T1_SESSION")
        store.rd("mailbox/x", {"to": "x"})
        assert seen == ["first", "second", None]

    def test_other_stores_and_the_shared_client_never_carry_it(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _ok(request)

        assert HEADER not in build_shared_t2_client().headers
        shared = httpx.Client(transport=httpx.MockTransport(handler))
        tuples = HttpTupleStore(base_url="http://mock.test", client=shared)
        memory = HttpMemoryStore(base_url="http://mock.test", client=shared)
        memory._get("/v1/memory/ping")
        tuples.rd("mailbox/x", {"to": "x"})
        memory._get("/v1/memory/ping")
        assert [HEADER in r.headers for r in seen] == [False, True, False]
        assert HEADER not in shared.headers


class TestStaleTokenFallback:
    @staticmethod
    def _gated(seen: list[httpx.Request]):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if HEADER in request.headers:
                return httpx.Response(401, json={"error": "session_not_minted"})
            return _ok(request)
        return handler

    def test_stale_token_retries_once_without_the_header_and_lands(self, monkeypatch, info_logs) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []
        store = _store(self._gated(seen))
        with capture_logs() as logs:
            tuple_id = store.out("mailbox/x", {"to": "x"}, {"from": "y"}, nonce="n")
        assert tuple_id == "b" * 64
        assert [HEADER in r.headers for r in seen] == [True, False]
        assert [r.url.path for r in seen] == ["/v1/tuples/out"] * 2
        dropped = [e for e in logs if e["event"] == "tuple_session_header_dropped"]
        assert len(dropped) == 1
        assert TOKEN not in repr(logs)

    def test_a_read_falls_back_too(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []
        rows = _store(self._gated(seen)).rd("mailbox/x", {"to": "x"})
        assert len(rows) == 1
        assert [HEADER in r.headers for r in seen] == [True, False]

    def test_the_drop_does_not_stick_to_the_next_call(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []
        store = _store(self._gated(seen))
        store.rd("mailbox/x", {"to": "x"})
        store.rd("mailbox/x", {"to": "x"})
        assert [HEADER in r.headers for r in seen] == [True, False, True, False]

    def test_a_401_that_survives_the_drop_goes_down_the_data_token_path(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(401, json={"error": "bad data token"})

        store = _store(handler)
        with pytest.raises(httpx.HTTPStatusError):
            store.rd("mailbox/x", {"to": "x"})
        # headered, headerless (the drop), then the mixin's re-mint and one more pair.
        assert [HEADER in r.headers for r in seen] == [True, False, True, False]
        assert len(store.reresolves) == 1

    def test_the_drop_runs_before_the_data_token_remint(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        store = _store(self._gated([]))
        assert len(store.rd("mailbox/x", {"to": "x"})) == 1
        assert store.reresolves == []

    def test_a_401_with_no_session_header_sent_is_not_dropped_again(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(401, json={"error": "bad data token"})

        store = _store(handler)
        with pytest.raises(httpx.HTTPStatusError):
            store.rd("mailbox/x", {"to": "x"})
        assert [HEADER in r.headers for r in seen] == [False, False]  # first try + mixin retry
        assert len(store.reresolves) == 1

    def test_a_non_401_error_is_not_retried_without_the_header(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(403, json={"error": "nope"})

        with pytest.raises(httpx.HTTPStatusError):
            _store(handler).rd("mailbox/x", {"to": "x"})
        assert len(seen) == 1


class TestSenderSessionOnTheRow:
    def test_absent_key_is_unknown_and_null_is_known_unstamped(self) -> None:
        absent = _body_to_tuple_row(dict(_ROW))
        null = _body_to_tuple_row({**_ROW, "sender_session": None})
        stamped = _body_to_tuple_row({**_ROW, "sender_session": "S1"})
        assert absent.sender_session is None and absent.sender_session_known is False
        assert null.sender_session is None and null.sender_session_known is True
        assert stamped.sender_session == "S1" and stamped.sender_session_known is True
        assert absent != null

    @pytest.mark.parametrize("render", [_tuple_row_to_dict, _row_dict])
    def test_rendering_keeps_the_three_states_apart(self, render) -> None:
        absent = render(_body_to_tuple_row(dict(_ROW)))
        null = render(_body_to_tuple_row({**_ROW, "sender_session": None}))
        stamped = render(_body_to_tuple_row({**_ROW, "sender_session": "S1"}))
        assert "sender_session" not in absent
        assert null["sender_session"] is None
        assert stamped["sender_session"] == "S1"


def test_the_cli_text_output_prints_it_only_when_the_engine_rendered_the_key(capsys) -> None:
    from nexus.commands import tuple_cmd

    def text(body: dict) -> str:
        tuple_cmd._print_row(_body_to_tuple_row(body))
        return capsys.readouterr().out

    assert "sender_session" not in text(dict(_ROW))
    assert "  sender_session: None" in text({**_ROW, "sender_session": None})
    assert "  sender_session: S1" in text({**_ROW, "sender_session": "S1"})
