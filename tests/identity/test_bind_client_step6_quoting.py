# SPDX-License-Identifier: AGPL-3.0-or-later
"""Step 6's identity-client routes put the id and the group into the path as one quoted segment each, as ``memberships``
does (nexus-hczdo.1.10 r1 item 7): a ``/``, space or ``?`` must not change which route is called."""
from __future__ import annotations

import httpx
import pytest

from nexus.identity.bind_client import IdentityClient


@pytest.mark.parametrize(("call", "want"), [
    (lambda c: c.put_work("a/b c", "wait", "s"), "/v1/identity/a%2Fb%20c/work"),
    (lambda c: c.remove_work("a/b c", "wait", "s"), "/v1/identity/a%2Fb%20c/work"),
    (lambda c: c.work_of("a/b c"), "/v1/identity/a%2Fb%20c/work"),
    (lambda c: c.roster("g/1?x"), "/v1/identity/groups/g%2F1%3Fx"),
])
def test_the_path_segments_are_quoted(call, want: str) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"work": [], "members": [], "removed": 0})

    call(IdentityClient("http://engine.test", "tok", http=httpx.Client(transport=httpx.MockTransport(handler))))
    assert seen[0].url.raw_path.decode().split("?")[0] == want


@pytest.mark.parametrize(("call", "want"), [
    (lambda c: c.join_group("a/b", "g h"), "/v1/identity/a%2Fb/memberships"),
    (lambda c: c.leave_group("a/b", "g/h"), "/v1/identity/a%2Fb/memberships/g%2Fh"),
])
def test_the_fenced_membership_paths_are_quoted(call, want: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.identity.token_header.identity_headers", lambda: {})
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"removed": 0})

    call(IdentityClient("http://engine.test", "tok", http=httpx.Client(transport=httpx.MockTransport(handler))))
    assert seen[0].url.raw_path.decode().split("?")[0] == want
