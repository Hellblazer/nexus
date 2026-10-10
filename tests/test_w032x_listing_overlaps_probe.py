# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w032x: a cold cloud-mode search starts the routing listing while the
engine version probe is in flight, instead of after it.

Measured on the managed service (T2 ``nexus/w032x-cli-cold-start-trace-2026-10-10``):
the probe (GET /version) and the listing (GET /v1/vectors/stats) each took
0.24-0.34 s, one after the other, before any search request went out.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import nexus.db.http_vector_client as hvc
from nexus.db.http_vector_client import (
    HttpVectorClient,
    VectorServiceError,
    get_http_vector_client,
    reset_http_vector_client_for_tests,
)
from nexus.db.managed_endpoint import ManagedServiceIncompatible
from nexus.mcp_infra import invalidate_collections_cache

_ROW = {
    "name": "knowledge__notes__voyage-context-3__v1",
    "content_type": "knowledge",
    "owner_id": "notes",
    "embedding_model": "voyage-context-3",
    "lifecycle_state": "live",
}
_CAPS = SimpleNamespace(release_version="0.1.157", embedding_mode="voyage")


@pytest.fixture(autouse=True)
def _fresh_client():
    reset_http_vector_client_for_tests()
    invalidate_collections_cache()
    yield
    reset_http_vector_client_for_tests()
    invalidate_collections_cache()


@pytest.fixture
def stats(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Record each GET /v1/vectors/stats as its ``routing`` flag; answer one row."""
    rec = SimpleNamespace(calls=[], started=threading.Event())

    def fake_stats(self, lifecycle_state=None, *, routing=False):
        rec.calls.append(routing)
        rec.started.set()
        return [dict(_ROW)]

    monkeypatch.setattr(HttpVectorClient, "collection_stats", fake_stats)
    return rec


def _probe_waiting_for(started: threading.Event):
    def probe(*_a, **_k):
        # Serial code would block here forever: the listing starts only after
        # the probe returns. The bounded wait turns that into a failure.
        assert started.wait(5), "the listing did not start while the probe was in flight"
        return _CAPS
    return probe


@pytest.mark.usefixtures("cloud_mode")
def test_listing_runs_during_the_probe_and_is_used_once(monkeypatch, stats) -> None:
    monkeypatch.setattr(
        "nexus.db.managed_endpoint.probe_managed_service",
        _probe_waiting_for(stats.started),
    )
    client = get_http_vector_client(prefetch_routing_listing=True)

    rows = client.list_collections(routing=True)

    assert [r["name"] for r in rows] == [_ROW["name"]]
    assert stats.calls == [True], "the prefetched listing was not reused"
    # A second listing is a fresh request: the prefetch is one-shot.
    client.list_collections(routing=True)
    assert stats.calls == [True, True]


@pytest.mark.usefixtures("cloud_mode")
def test_client_built_before_the_probe_still_gets_the_probe_findings(
    monkeypatch, stats,
) -> None:
    monkeypatch.setattr(
        "nexus.db.managed_endpoint.probe_managed_service",
        _probe_waiting_for(stats.started),
    )
    client = get_http_vector_client(prefetch_routing_listing=True)

    assert client._per_collection_confirmed is True
    assert client._embedding_mode_memo == "voyage"


@pytest.mark.usefixtures("cloud_mode")
def test_failed_probe_raises_and_drops_the_prefetch(monkeypatch, stats) -> None:
    def failing_probe(*_a, **_k):
        assert stats.started.wait(5)
        raise ManagedServiceIncompatible("engine too old")

    monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", failing_probe)

    with pytest.raises(ManagedServiceIncompatible):
        get_http_vector_client(prefetch_routing_listing=True)
    assert hvc._vector_client_instance is not None
    assert hvc._vector_client_instance._routing_prefetch is None


@pytest.mark.usefixtures("cloud_mode")
def test_failed_prefetch_is_repeated_by_the_consuming_call(monkeypatch) -> None:
    calls: list[bool] = []
    started = threading.Event()

    def flaky_stats(self, lifecycle_state=None, *, routing=False):
        calls.append(routing)
        started.set()
        if len(calls) == 1:
            raise VectorServiceError("engine hiccup", code=503)
        return [dict(_ROW)]

    monkeypatch.setattr(HttpVectorClient, "collection_stats", flaky_stats)
    monkeypatch.setattr(
        "nexus.db.managed_endpoint.probe_managed_service", _probe_waiting_for(started),
    )
    client = get_http_vector_client(prefetch_routing_listing=True)

    rows = client.list_collections(routing=True)

    assert [r["name"] for r in rows] == [_ROW["name"]]
    assert calls == [True, True]


def test_local_mode_ignores_the_flag(monkeypatch, stats) -> None:
    # The suite's default posture is local (NX_LOCAL=1): no probe, no prefetch.
    def no_probe(*_a, **_k):
        raise AssertionError("local mode must not probe")

    monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", no_probe)
    client = get_http_vector_client(prefetch_routing_listing=True)

    assert client._routing_prefetch is None
    assert stats.calls == []


@pytest.mark.usefixtures("cloud_mode")
def test_without_the_flag_the_probe_runs_alone(monkeypatch, stats) -> None:
    monkeypatch.setattr(
        "nexus.db.managed_endpoint.probe_managed_service", lambda *_a, **_k: _CAPS,
    )
    client = get_http_vector_client()

    assert client._routing_prefetch is None
    assert stats.calls == []
