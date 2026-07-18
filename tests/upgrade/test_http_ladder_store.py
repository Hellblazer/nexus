# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpLadderStore (RDR-186 nexus-146xx.12, client half).

httpx.MockTransport idiom (mirrors tests/upgrade/test_remap_client.py): the
CLIENT's wire contract only — request shapes, CompletionLedger shape
adaptation, fail-loud propagation. Server semantics live in
service/src/test/java LadderHandlerTest.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.http_store import HttpLadderStore
from nexus.upgrade_ladder.protocol import CompletionLedger

TOKEN = "fake-ladder-token"

_ROWS = [
    {"rung_name": "engine-install", "verified_at": "2026-07-18T12:00:00Z",
     "package_version": "6.12.0", "detail": ""},
    {"rung_name": "t2-schema", "verified_at": "2026-07-18T12:01:00Z",
     "package_version": "6.12.0", "detail": "ok"},
]


def _store_with_handler(handler) -> HttpLadderStore:
    store = HttpLadderStore(base_url="http://svc", _token=TOKEN)
    store._client = httpx.Client(transport=httpx.MockTransport(handler))
    return store


def test_conforms_to_completion_ledger_protocol():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no HTTP expected for a protocol check")

    assert isinstance(_store_with_handler(handler), CompletionLedger)


def test_record_verified_posts_the_fact():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"recorded": True})

    _store_with_handler(handler).record_verified(
        "t2-schema", package_version="6.12.0", detail="ok"
    )
    assert captured["path"] == "/v1/ladder/record"
    assert captured["json"] == {
        "rung_name": "t2-schema", "package_version": "6.12.0", "detail": "ok",
    }


def test_record_verified_http_error_propagates_loud():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(Exception):
        _store_with_handler(handler).record_verified("x", package_version="1")


def test_verified_rungs_extracts_names():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/ladder/completions"
        return httpx.Response(200, json={"completions": _ROWS})

    result = _store_with_handler(handler).verified_rungs()
    assert result == frozenset({"engine-install", "t2-schema"})
    assert isinstance(result, frozenset)


def test_completions_rebuilds_completion_records():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"completions": _ROWS})

    result = _store_with_handler(handler).completions()
    assert result["t2-schema"] == CompletionRecord(
        rung_name="t2-schema", verified_at="2026-07-18T12:01:00Z",
        package_version="6.12.0", detail="ok",
    )
    assert set(result) == {"engine-install", "t2-schema"}


# ── DeferredLadderLedger: first-use construction (reviewer-146xx-12c gap) ────


def test_deferred_ledger_constructs_nothing_at_init(monkeypatch):
    from nexus.upgrade_ladder import http_store as mod

    def _boom():
        raise AssertionError("HttpLadderStore must not be constructed at __init__")

    monkeypatch.setattr(mod, "HttpLadderStore", _boom)
    ledger = mod.DeferredLadderLedger()  # no raise = nothing constructed
    ledger.close()  # close() no-ops when never constructed


def test_deferred_ledger_constructs_exactly_once_and_delegates(monkeypatch):
    from nexus.upgrade_ladder import http_store as mod

    constructions = {"n": 0}

    class FakeStore:
        def __init__(self):
            constructions["n"] += 1
            self.calls: list[tuple] = []

        def record_verified(self, rung_name, *, package_version, detail=""):
            self.calls.append(("record", rung_name, package_version, detail))

        def verified_rungs(self):
            self.calls.append(("verified",))
            return frozenset({"a"})

        def completions(self):
            self.calls.append(("completions",))
            return {}

        def close(self):
            self.calls.append(("close",))

    monkeypatch.setattr(mod, "HttpLadderStore", FakeStore)
    ledger = mod.DeferredLadderLedger()
    ledger.record_verified("r1", package_version="6.12.0", detail="d")
    assert ledger.verified_rungs() == frozenset({"a"})
    ledger.completions()
    ledger.close()

    assert constructions["n"] == 1, "constructed exactly once across all calls"
    assert ledger._store.calls == [
        ("record", "r1", "6.12.0", "d"), ("verified",), ("completions",), ("close",),
    ]


def test_deferred_ledger_construction_failure_surfaces_from_the_call(monkeypatch):
    """Resolution failure = the CALL raises (the holder treats it as
    backend-down); nothing is cached, so a later call retries construction."""
    from nexus.upgrade_ladder import http_store as mod

    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        raise RuntimeError("endpoint not resolvable")

    monkeypatch.setattr(mod, "HttpLadderStore", _flaky)
    ledger = mod.DeferredLadderLedger()
    with pytest.raises(RuntimeError):
        ledger.verified_rungs()
    with pytest.raises(RuntimeError):
        ledger.record_verified("r", package_version="1")
    assert attempts["n"] == 2, "construction retried per call, never poisoned"
    ledger.close()  # still a no-op — nothing was ever constructed
