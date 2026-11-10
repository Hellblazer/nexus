# SPDX-License-Identifier: AGPL-3.0-or-later
"""Name-only collection checks read the routing listing (nexus-mjmjl).

linda's store-get profile: the full ``/v1/vectors/stats`` listing counts every collection's live chunks
(469 ms at 140k rows, growing with the tenant); the routing one reads the catalog (55 ms) and lists the
same collections. A check that reads names, and the content_type or lifecycle_state the routing rows
carry, asks for routing; a caller that prints counts keeps the full listing.
"""

from __future__ import annotations

from typing import Any

import pytest

import nexus.db.http_vector_client as hvc
from nexus.commands import store as store_cmd


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(hvc.HttpVectorClient, "__init__", lambda self: None)
    c = hvc.HttpVectorClient()
    c._tenant = "t"
    return c


def _recording(rows: list[dict]):
    calls: list[dict[str, Any]] = []

    def list_collections(lifecycle_state=None, *, strict=False, routing=False):
        calls.append({"strict": strict, "routing": routing})
        return rows

    return list_collections, calls


def test_get_collection_checks_existence_on_the_routing_listing(client, monkeypatch) -> None:
    listing, calls = _recording([{"name": "c"}])
    monkeypatch.setattr(client, "list_collections", listing)
    client.get_collection("c")
    assert calls == [{"strict": True, "routing": True}]


def test_collection_exists_reads_the_routing_listing(client, monkeypatch) -> None:
    listing, calls = _recording([{"name": "c"}])
    monkeypatch.setattr(client, "list_collections", listing)
    assert client.collection_exists("c") and not client.collection_exists("d")
    assert calls == [{"strict": False, "routing": True}] * 2


def test_the_store_scope_counts_only_for_the_verb_that_prints_them(monkeypatch) -> None:
    rows = [{"name": "knowledge__a__m__v1", "content_type": "knowledge", "lifecycle_state": "live", "count": 3}]
    listing, calls = _recording(rows)

    class _Db:
        list_collections = staticmethod(listing)

    monkeypatch.setattr(store_cmd, "store_read_scope", lambda collection, *, t3, knowledge_collections: knowledge_collections())
    store_cmd._store_scope("knowledge", _Db())
    store_cmd._store_scope("knowledge", _Db(), counts=True)
    assert [c["routing"] for c in calls] == [True, False]
