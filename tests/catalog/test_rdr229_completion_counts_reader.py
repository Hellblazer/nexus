# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 (nexus-p2m1x.17), review N2: the client reader of ``GET /v1/catalog/rdr229_completion_counts``.

The route is authenticated and tenant-scoped: it answers the CALLER'S tenant's registry rows, live collections, dangling
tombstones (by content type) and self-id rows, and the orphan and unstamped rows of each table whose CHECK is NOT VALID. The
unauthenticated ``/v1/status`` carries none of it. The reader checks the flag first, sends one request, and refuses a body
that is not the contract (``EngineRouteUnavailable.MALFORMED``), as the other RDR-241 readers do.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.catalog.engine_reads import rdr229_completion_counts as rc
from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable

GOOD = {
    "registry_rows": 12, "live_collections": 9, "superseded_by_waiting": 0,
    "dangling_tombstones": {"total": 2, "by_content_type": {"docs": 2}, "self_id": {"total": 1, "by_content_type": {"docs": 1}}},
    "tables": [{"table": "catalog_documents", "orphans": 1, "unstamped": 3}],
}


class _Client:
    def __init__(self, *, flag: bool = True, body=GOOD, status: int | None = None, probe_failed: bool = False):
        self._flag, self._body, self._status, self._version_probe_failed = flag, body, status, probe_failed
        self.requests: list[tuple[str, dict]] = []

    def _version_flags(self) -> dict:
        return {rc.FLAG: True} if self._flag else {}

    def _get(self, route: str, *, idempotent: bool, timeout: float, **params):
        self.requests.append((route, params))
        assert idempotent is False, "a statement the engine cut at its bound is not retried"
        if self._status is not None:
            request = httpx.Request("GET", "http://engine" + route)
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(self._status, request=request))
        return self._body


def test_the_route_and_flag_are_the_ones_the_engine_serves():
    assert rc.ROUTE == "/rdr229_completion_counts"
    assert rc.FLAG == "catalog_rdr229_completion_counts_supported"


def test_a_good_body_is_returned_after_one_request():
    client = _Client()
    assert rc.read_rdr229_completion_counts(client) == GOOD
    assert client.requests == [(rc.ROUTE, {})]


def test_an_engine_that_does_not_advertise_the_flag_is_asked_nothing():
    client = _Client(flag=False)
    with pytest.raises(EngineRouteUnavailable) as exc:
        rc.read_rdr229_completion_counts(client)
    assert exc.value.reason == EngineRouteUnavailable.UNSUPPORTED and client.requests == []


@pytest.mark.parametrize("status,reason", [(404, EngineRouteUnavailable.UNSUPPORTED), (503, EngineRouteUnavailable.TIMEOUT),
                                           (500, EngineRouteUnavailable.FAILED)])
def test_a_refusal_is_cannot_say_never_an_empty_answer(status, reason):
    with pytest.raises(EngineRouteUnavailable) as exc:
        rc.read_rdr229_completion_counts(_Client(status=status))
    assert exc.value.reason == reason


@pytest.mark.parametrize("bad", [
    None, [], {}, {**GOOD, "registry_rows": -1}, {**GOOD, "registry_rows": True}, {**GOOD, "tables": "nope"},
    {**GOOD, "tables": [{"table": "t", "orphans": -1, "unstamped": 0}]}, {**GOOD, "tables": [{"orphans": 1, "unstamped": 0}]},
    {**GOOD, "dangling_tombstones": []}, {**GOOD, "dangling_tombstones": {"total": 1}},
    {k: v for k, v in GOOD.items() if k != "live_collections"},
])
def test_a_body_that_is_not_the_contract_is_malformed(bad):
    with pytest.raises(EngineRouteUnavailable) as exc:
        rc.read_rdr229_completion_counts(_Client(body=bad))
    assert exc.value.reason == EngineRouteUnavailable.MALFORMED
