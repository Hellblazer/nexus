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

import hashlib
import logging
import threading

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from nexus.commands.tuple_cmd import _row_dict
from nexus.db.t2 import _refreshable_client
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


@pytest.fixture
def debug_logs():
    """Like ``info_logs`` but down to DEBUG, for the second-drop-of-one-token event."""
    old = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
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

    @pytest.mark.parametrize(
        "value", ["t\u00f6k", "a\x01b", "a b", "a\tb", "tok\x7f", "tok\u200b", "a\nb"],
    )
    def test_a_token_that_is_not_printable_ascii_without_whitespace_sends_no_header(
        self, monkeypatch, value,
    ) -> None:
        """A non-ASCII value would raise UnicodeEncodeError inside httpx on every
        tuple call; a control or whitespace value is not a header token either.
        Send nothing, so the call works as it did before F3 step 2."""
        monkeypatch.setenv("NX_T1_SESSION", value)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _ok(request)

        store = _store(handler)
        assert store.out("mailbox/x", {"to": "x"}, {"from": "y"}, nonce="n") == "b" * 64
        assert len(store.rd("mailbox/x", {"to": "x"})) == 1
        assert [HEADER in r.headers for r in seen] == [False, False]

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

    def test_the_drop_does_not_stick_past_a_changed_token(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []
        gated = self._gated(seen)

        def handler(request: httpx.Request) -> httpx.Response:
            # TOKEN is stale; "fresh" is live.
            if request.headers.get(HEADER) == "fresh":
                seen.append(request)
                return _ok(request)
            return gated(request)

        store = _store(handler)
        store.rd("mailbox/x", {"to": "x"})
        monkeypatch.setenv("NX_T1_SESSION", "fresh")
        store.rd("mailbox/x", {"to": "x"})
        assert [r.headers.get(HEADER) for r in seen] == [TOKEN, None, "fresh"]

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


class TestStaleTokenMemo:
    """A rejected token is remembered (as a hash) and not sent again until
    NX_T1_SESSION changes, so a stale token costs one 401, not one per call."""

    @staticmethod
    def _gated(seen: list[httpx.Request], live: str | None = None):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            sent = request.headers.get(HEADER)
            if sent is not None and sent != live:
                return httpx.Response(401, json={"error": "session_not_minted"})
            return _ok(request)
        return handler

    def test_a_rejected_token_is_not_sent_again(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []
        store = _store(self._gated(seen))
        for _ in range(3):
            store.rd("mailbox/x", {"to": "x"})
        assert [HEADER in r.headers for r in seen] == [True, False, False, False]

    def test_a_changed_token_is_sent_and_a_live_one_is_not_dropped(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []
        store = _store(self._gated(seen, live="fresh"))
        store.rd("mailbox/x", {"to": "x"})
        monkeypatch.setenv("NX_T1_SESSION", "fresh")
        store.rd("mailbox/x", {"to": "x"})
        store.rd("mailbox/x", {"to": "x"})
        assert [r.headers.get(HEADER) for r in seen] == [TOKEN, None, "fresh", "fresh"]

    def test_a_second_stale_token_is_dropped_and_remembered_in_its_turn(self, monkeypatch) -> None:
        seen: list[httpx.Request] = []
        store = _store(self._gated(seen))
        for tok in ("stale-one", "stale-one", "stale-two", "stale-two"):
            monkeypatch.setenv("NX_T1_SESSION", tok)
            store.rd("mailbox/x", {"to": "x"})
        assert [r.headers.get(HEADER) for r in seen] == [
            "stale-one", None, None, "stale-two", None, None,
        ]

    def test_the_memo_is_per_store_instance(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []
        _store(self._gated(seen)).rd("mailbox/x", {"to": "x"})
        _store(self._gated(seen)).rd("mailbox/x", {"to": "x"})
        assert [HEADER in r.headers for r in seen] == [True, False, True, False]

    def test_the_store_keeps_a_hash_never_the_raw_token(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        store = _store(self._gated([]))
        store.rd("mailbox/x", {"to": "x"})
        digest = hashlib.sha256(TOKEN.encode()).hexdigest()
        assert store._rejected_session_hash == digest  # type: ignore[attr-defined]
        assert TOKEN not in repr(vars(store))

    def test_a_data_token_rotated_between_attempts_sets_no_memo(self, monkeypatch) -> None:
        """A concurrent re-mint between the headered 401 and the headerless retry
        makes the retry succeed on the new bearer. That proves nothing about the
        session token, so it must not be remembered as rejected."""
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        holder: list[HttpTupleStore] = []
        seen: list[bool] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(HEADER in request.headers)
            if HEADER in request.headers:
                holder[0]._token = "rotated-data-token"  # a sibling's re-mint lands now
                return httpx.Response(401, json={"error": "unauthorized"})
            return _ok(request)

        store = _store(handler)
        holder.append(store)
        store.out("mailbox/x", {"to": "x"}, {"from": "y"}, "b", nonce="n")
        assert seen == [True, False]
        assert store._rejected_session_hash is None  # type: ignore[attr-defined]
        store.out("mailbox/x", {"to": "x"}, {"from": "y"}, "b", nonce="n2")
        assert seen[2] is True, "the live session token is sent again"

    def test_a_401_that_is_not_the_sessions_sets_no_memo(self, monkeypatch) -> None:
        """The headerless retry also fails (the data token is bad): the session
        token is not the culprit, so it is not memoised."""
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad data token"})

        store = _store(handler)
        with pytest.raises(httpx.HTTPStatusError):
            store.rd("mailbox/x", {"to": "x"})
        assert store._rejected_session_hash is None  # type: ignore[attr-defined]

    def test_info_log_once_per_token_and_none_for_the_suppressed_calls(
        self, monkeypatch, info_logs,
    ) -> None:
        seen: list[httpx.Request] = []
        store = _store(self._gated(seen))
        with capture_logs() as logs:
            monkeypatch.setenv("NX_T1_SESSION", "stale-one")
            store.rd("mailbox/x", {"to": "x"})
            store.rd("mailbox/x", {"to": "x"})
            monkeypatch.setenv("NX_T1_SESSION", "stale-two")
            store.rd("mailbox/x", {"to": "x"})
        dropped = [e for e in logs if e["event"] == "tuple_session_header_dropped"]
        assert [e["log_level"] for e in dropped] == ["info", "info"]
        assert "stale-one" not in repr(logs) and "stale-two" not in repr(logs)

    def test_a_repeat_drop_of_the_same_token_logs_at_debug(self, monkeypatch, debug_logs) -> None:
        """Two threads both send the stale token before either has memoised it.
        Both get a 401 and drop; the first logs INFO, the second DEBUG."""
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        both_sent = threading.Barrier(2, timeout=5)
        a_done = threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            if HEADER in request.headers:
                both_sent.wait()
                return httpx.Response(401, json={"error": "session_not_minted"})
            if b'"B"' in request.content:
                a_done.wait(5)  # B's headerless retry lands after A has memoised
            return httpx.Response(200, json={"id": "c" * 64})

        store = _store(handler)
        errors: list[BaseException] = []

        def run(who: str) -> None:
            try:
                store.out("mailbox/x", {"to": who}, {"from": "y"}, nonce="n")
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
                errors.append(exc)
            finally:
                if who == "A":
                    a_done.set()

        with capture_logs() as logs:
            threads = [threading.Thread(target=run, args=(w,)) for w in ("A", "B")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        assert errors == []
        dropped = [e for e in logs if e["event"] == "tuple_session_header_dropped"]
        assert sorted(e["log_level"] for e in dropped) == ["debug", "info"]


class TestConcurrentCallers:
    def test_a_call_parked_in_its_headerless_retry_does_not_strip_another_calls_header(
        self, monkeypatch,
    ) -> None:
        """Thread A is inside its headerless retry (parked); thread B makes a fresh
        call on the same store and must still send the header. The drop is per
        call, not per instance, and the memo is set only once A's retry lands."""
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        a_in_retry = threading.Event()
        b_done = threading.Event()
        b_sent: list[bool] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if b'"A"' in request.content:
                if HEADER in request.headers:
                    return httpx.Response(401, json={"error": "session_not_minted"})
                a_in_retry.set()
                assert b_done.wait(5)  # park A's headerless retry
                return httpx.Response(200, json={"id": "a" * 64})
            b_sent.append(HEADER in request.headers)
            b_done.set()
            return httpx.Response(200, json={"id": "b" * 64})

        store = _store(handler)
        a = threading.Thread(
            target=lambda: store.out("mailbox/x", {"to": "A"}, {"from": "y"}, nonce="n"),
        )
        a.start()
        assert a_in_retry.wait(5)
        store.out("mailbox/x", {"to": "B"}, {"from": "y"}, nonce="n")
        a.join(10)
        assert not a.is_alive()
        assert b_sent == [True]

    def test_a_headerless_503_after_the_401_resends_the_header_on_the_gateway_retry(
        self, monkeypatch,
    ) -> None:
        """Pinned design: the memo is set only when the headerless retry SUCCEEDS.
        401 (headered) -> headerless 503 leaves no memo, so the mixin's gateway
        retry sends the header again, is 401'd again, drops again, and only
        then, on success, memoises."""
        monkeypatch.setattr(_refreshable_client, "_GATEWAY_RETRY_SLEEPS", (0.0, 0.0, 0.0))
        monkeypatch.setenv("NX_T1_SESSION", TOKEN)
        seen: list[httpx.Request] = []
        headerless_calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if HEADER in request.headers:
                return httpx.Response(401, json={"error": "session_not_minted"})
            headerless_calls.append(1)
            if len(headerless_calls) == 1:
                return httpx.Response(503, json={"error": "busy"})
            return _ok(request)

        store = _store(handler)
        assert len(store.rd("mailbox/x", {"to": "x"})) == 1
        assert [HEADER in r.headers for r in seen] == [True, False, True, False]
        store.rd("mailbox/x", {"to": "x"})
        assert [HEADER in r.headers for r in seen][4:] == [False]
