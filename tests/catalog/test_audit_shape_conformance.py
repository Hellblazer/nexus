# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime return-SHAPE pins for the nexus-h8rf6.3 catalog-client audit fixes.

Wave-review HIGH (substantive-critic): the 94-method HttpCatalogClient audit
in 8a0500f9 fixed three return-shape bugs beyond docs_for_chashes — but only
docs_for_chashes got a dedicated runtime-shape conformance test. The
annotation-comparison check in test_catalog_protocol_fidelity.py cannot
catch this class (an impl can return a flat list at runtime regardless of
its annotation). These tests pin the actual runtime VALUES the real client
produces from the real Java wire shapes:

  - unlink                    -> int rowcount (was bool)
  - set_owner_head_hash       -> int rowcount (was None)
  - lookup_doc_id_by_collection_and_path -> "" on no-match (never None)

The full-surface mechanized tripwire is tracked separately (see the bead
referenced in the wave-review epic comment); these pin the three methods
the audit actually evidenced.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient


@pytest.fixture
def client() -> HttpCatalogClient:
    return HttpCatalogClient(base_url="http://fake.invalid", _token="test-token")


def test_unlink_returns_int_rowcount(client, monkeypatch):
    # Java wire shape: CatalogHandler.handleUnlink -> {"deleted": N}
    monkeypatch.setattr(client, "_post", lambda path, body=None: {"deleted": 2})
    out = client.unlink("1.1", "1.2", "cites")
    assert out == 2
    assert type(out) is int  # not bool (bool is an int subclass — pin exactly)


def test_unlink_zero_when_body_empty(client, monkeypatch):
    monkeypatch.setattr(client, "_post", lambda path, body=None: {})
    assert client.unlink("1.1", "1.2", "cites") == 0


def test_set_owner_head_hash_returns_int_rowcount(client, monkeypatch):
    # Java wire shape: CatalogHandler.handleOwnerHeadHash -> {"updated": N}.
    # Pre-fix the client returned None, so indexer.py's rowcount==0
    # concurrent-owner-deletion detector could never fire in service mode.
    monkeypatch.setattr(client, "_post", lambda path, body=None: {"updated": 1})
    out = client.set_owner_head_hash("1.1", "a" * 40)
    assert out == 1
    assert type(out) is int


def test_set_owner_head_hash_zero_rowcount_visible(client, monkeypatch):
    monkeypatch.setattr(client, "_post", lambda path, body=None: {"updated": 0})
    assert client.set_owner_head_hash("1.1", "a" * 40) == 0


def test_lookup_doc_id_no_match_returns_empty_string_not_none(client, monkeypatch):
    # Local Catalog's documented contract: "" (never None) on no-match.
    monkeypatch.setattr(client, "_get", lambda path, **kw: {"documents": []})
    out = client.lookup_doc_id_by_collection_and_path("code__x__y__v1", "src/a.py")
    assert out == ""
    assert out is not None


def test_lookup_doc_id_404_returns_empty_string(client, monkeypatch):
    def raise_404(path, **kw):
        resp = httpx.Response(404, request=httpx.Request("GET", "http://fake.invalid/resolve"))
        raise httpx.HTTPStatusError("not found", request=resp.request, response=resp)

    monkeypatch.setattr(client, "_get", raise_404)
    assert client.lookup_doc_id_by_collection_and_path("c", "p") == ""


def test_lookup_doc_id_match_returns_tumbler_string(client, monkeypatch):
    monkeypatch.setattr(
        client, "_get",
        lambda path, **kw: {"documents": [{"tumbler": "1.2.5"}]},
    )
    out = client.lookup_doc_id_by_collection_and_path("c", "p")
    assert out == "1.2.5"
