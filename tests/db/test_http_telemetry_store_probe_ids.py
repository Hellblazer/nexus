# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpTelemetryStore.probe_ids (RDR-178 wave-2 P1, nexus-s3dd4.3).

Uses the httpx.MockTransport idiom (mirrors
tests/migration/test_rdr176_p3_batch_conformance.py's ``_counting_client``)
rather than the in-process fake HTTP server in test_http_telemetry_store.py —
this endpoint's contract (request shape, paging, fail-closed propagation) is
best pinned at the httpx-request level, not by re-implementing server logic.

Server-side matching semantics (which rows are "present") are tested in
service/src/test/java/dev/nexus/service/TelemetryRepositoryTest.java
(probeIds_* tests). This file verifies the CLIENT's contract only:
  - correct path + request body shape ({"table", "keys"})
  - transparent paging at QUOTAS.MAX_RECORDS_PER_WRITE (300) candidates/request
  - "present" results from every page are aggregated and returned verbatim
  - empty input short-circuits with no HTTP call
  - FAIL-CLOSED: transport errors and non-2xx responses propagate as
    exceptions — never silently degrade to an empty/partial result (the
    antipattern nexus-te885.6 flags in HttpVectorClient.existing_ids)
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.db.chroma_quotas import QUOTAS
from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

TOKEN = "fake-telemetry-token-probe"


def _store_with_transport(handler) -> HttpTelemetryStore:
    store = HttpTelemetryStore(base_url="http://svc", _token=TOKEN)
    store._client = httpx.Client(
        base_url="http://svc",
        headers=store._headers,
        transport=httpx.MockTransport(handler),
    )
    return store


def test_probe_ids_sends_table_and_keys_to_correct_path():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"present": [["q", "c", "a", "s", "2026-01-01T00:00:00Z"]]})

    store = _store_with_transport(handler)
    keys = [
        ["q", "c", "a", "s", "2026-01-01T00:00:00Z"],
        ["q2", "c2", "a2", "s2", "2026-01-02T00:00:00Z"],
    ]
    present = store.probe_ids("relevance_log", keys)

    assert captured["path"] == "/v1/telemetry/ids/probe"
    assert captured["json"] == {"table": "relevance_log", "keys": keys}
    assert present == [["q", "c", "a", "s", "2026-01-01T00:00:00Z"]]


def test_probe_ids_empty_keys_is_noop_no_http_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"present": []})

    store = _store_with_transport(handler)
    present = store.probe_ids("frecency", [])

    assert present == []
    assert calls == []


def test_probe_ids_pages_transparently_at_quota_cap():
    """> MAX_RECORDS_PER_WRITE candidates must split into multiple requests,
    each carrying <= the cap, with "present" aggregated across pages."""
    cap = QUOTAS.MAX_RECORDS_PER_WRITE
    total = cap + 50
    keys = [[f"chunk-{i}"] for i in range(total)]

    request_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_sizes.append(len(body["keys"]))
        # Echo back every key in this page as "present" for a deterministic total.
        return httpx.Response(200, json={"present": body["keys"]})

    store = _store_with_transport(handler)
    present = store.probe_ids("frecency", keys)

    assert len(request_sizes) == 2, "650 candidates at cap=300 must page into 2 requests"
    assert all(size <= cap for size in request_sizes)
    assert sum(request_sizes) == total
    assert len(present) == total


def test_probe_ids_fail_closed_propagates_transport_error():
    """FAIL-CLOSED (nexus-te885.6): a transport-level failure must raise, not
    silently degrade to an empty/partial result — the opposite polarity of
    HttpVectorClient.existing_ids, which swallows to set()."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    store = _store_with_transport(handler)

    with pytest.raises(httpx.ConnectError):
        store.probe_ids("relevance_log", [["q", "c", "a", "s", "2026-01-01T00:00:00Z"]])


def test_probe_ids_fail_closed_propagates_http_error_status():
    """A non-2xx response (e.g. server-side 400 for an unknown table, or a
    502 blip) must raise via _raise_for_status, never read as "nothing
    present"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "Unknown table: bogus_table"})

    store = _store_with_transport(handler)

    with pytest.raises(httpx.HTTPStatusError):
        store.probe_ids("bogus_table", [["x"]])
