# SPDX-License-Identifier: AGPL-3.0-or-later
"""``HttpScratchStore(send_session_token=False)``, the identity scope's store (RDR-253 Step 6 part 2, nexus-hczdo).

The engine admits an identity-scope call with no ``X-Nexus-T1-Session`` header (reads open, writes fenced on the identity
token), so the store sends none and owns no session. Tenant-free: the engine is an ``httpx.MockTransport``. What the real
engine does with a header-less identity call is the journey row in tests/identity/test_work_engine_rows.py.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.db import http_scratch_store as hss
from nexus.db.http_scratch_store import (
    IDENTITY_UNAUTHORIZED_MARKER,
    SESSION_UNAUTHORIZED_MARKER,
    HttpScratchStore,
)

HEADER = hss._HEADER_T1_SESSION


class Manager:
    """A configured data-token manager whose first bearer the engine refuses; ``invalidate`` rotates it."""

    def __init__(self) -> None:
        self.token = "v1"
        self.invalidated = 0
        self.configured = True

    def bearer_for(self, base_url: str, tenant: str) -> str:
        return self.token

    def is_configured(self) -> bool:
        return self.configured

    def invalidate(self, base_url: str, tenant: str) -> None:
        self.invalidated += 1
        self.token = "v2"


def _wire(store: HttpScratchStore, handler) -> list[httpx.Request]:
    """Route *store* (and every client it rebuilds) through a MockTransport; return the requests it sees."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def build() -> httpx.Client:
        return httpx.Client(base_url=store._base_url, headers=store._headers, transport=httpx.MockTransport(record))

    store._build_client = build                                        # type: ignore[method-assign]
    store._client.close()
    store._client = build()
    return seen


@pytest.fixture()
def unpinned(monkeypatch) -> Manager:
    """A bearer the store resolves fresh from a manager on every request (the self-minting posture)."""
    import nexus.db.data_token as data_token

    manager = Manager()
    monkeypatch.setenv("NX_SERVICE_TOKEN", "unused-static")
    monkeypatch.setattr(data_token, "get_data_token_manager", lambda: manager)
    return manager


def _identity_store() -> HttpScratchStore:
    return HttpScratchStore(base_url="http://127.0.0.1:1", tenant="t", session_id="identity:i7",
                            _token=None, send_session_token=False, export_adopted_token=False)


class TestTheFlagIsForTheIdentityScope:
    @pytest.mark.parametrize("session_id", ["sess-123", "s", "identity", "Identity:i7"])
    def test_a_header_less_store_for_an_ordinary_session_is_refused(self, session_id) -> None:
        """The engine takes a header-less call's body session id unchecked outside the identity scope, so the flag would
        drop the only per-session check there is (critique S1)."""
        with pytest.raises(ValueError, match="identity scope"):
            HttpScratchStore(base_url="http://127.0.0.1:1", session_id=session_id, _token="x", send_session_token=False)

    def test_an_identity_scope_store_and_every_session_store_still_build(self) -> None:
        store = HttpScratchStore(base_url="http://127.0.0.1:1", session_id="identity:i7", _token="x", send_session_token=False)
        session = HttpScratchStore(base_url="http://127.0.0.1:1", session_id="sess-123", _token="x", _session_token="tok")
        try:
            assert HEADER not in store._headers and session._headers[HEADER] == "tok"
        finally:
            store.close()
            session.close()


class TestTheUnauthorizedLadderWithTheHeaderOff:
    def test_a_401_then_200_remints_the_bearer_and_neither_attempt_carries_a_session_header(self, unpinned) -> None:
        store = _identity_store()
        assert store._token_pinned is False and store._headers["Authorization"] == "Bearer v1"
        seen = _wire(store, lambda r: httpx.Response(200, json={"entries": [{"id": "e1"}]}) if r.headers["Authorization"] == "Bearer v2"
                     else httpx.Response(401, json={"error": "unauthorized"}))
        try:
            assert store.list_entries() == [{"id": "e1"}]
        finally:
            store.close()
        assert [r.headers["Authorization"] for r in seen] == ["Bearer v1", "Bearer v2"], "refused, re-minted, retried once"
        assert unpinned.invalidated == 1, "the bearer re-mint ran"
        assert all(HEADER not in r.headers for r in seen), "no X-Nexus-T1-Session on either attempt"

    def test_a_401_that_stands_names_the_identity_scope_not_a_session(self, unpinned) -> None:
        """The session advice (reconnect the MCP, check the session lease) names a cause this store cannot have."""
        unpinned.configured = False                                              # no re-mint: the 401 stands
        store = _identity_store()
        seen = _wire(store, lambda r: httpx.Response(401, json={"error": "unauthorized"}))
        try:
            for call in (store.list_entries, lambda: store.get("abcd")):          # _post and _post_raw
                with pytest.raises(RuntimeError) as err:
                    call()
                assert IDENTITY_UNAUTHORIZED_MARKER in str(err.value), str(err.value)
                assert SESSION_UNAUTHORIZED_MARKER not in str(err.value), str(err.value)
        finally:
            store.close()
        assert len(seen) == 2 and all(HEADER not in r.headers for r in seen)

    def test_a_session_store_keeps_the_session_text(self, unpinned) -> None:
        unpinned.configured = False
        store = HttpScratchStore(base_url="http://127.0.0.1:1", session_id="sess-123", _token=None, _session_token="tok")
        _wire(store, lambda r: httpx.Response(401, json={"error": "unauthorized"}))
        try:
            with pytest.raises(RuntimeError, match="unauthorized T1 session"):
                store.list_entries()
        finally:
            store.close()
