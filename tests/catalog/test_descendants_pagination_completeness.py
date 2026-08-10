# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2 nexus/chroma-residue-plan-2026-08-10 §C1: ``HttpCatalogClient.descendants()`` silently truncated large subtrees.

Prior bug (``http_catalog_client.py``): ``descendants(prefix)`` issued ONE
unfiltered ``GET /list?limit=500`` and filtered the result client-side by
tumbler prefix, with no pagination loop. Any subtree whose documents did not
all land inside that single unfiltered 500-row page — i.e. essentially any
subtree in a catalog bigger than one page — was silently truncated with no
error. Reproduced live against the cloud catalog (19,824 documents): 0%
coverage on 11 of the 12 largest subtrees, 28.7% on the twelfth. The
user-facing path is ``query(subtree=...)`` (``src/nexus/mcp/core.py``),
whose own guard steers callers toward exactly the depth that triggered the
bug.

Fix: a dedicated ``GET /v1/catalog/descendants`` engine route
(``CatalogHandler.handleDescendants`` -> ``CatalogRepository.descendants``,
one unbounded SQL query, complete by construction) plus a client-side
paginated-``/list`` fallback for an engine that predates the route (404) —
necessary because ``REQUIRED_ENGINE_VERSION`` may be pinned below the
version that ships the route at any given moment.

This suite drives the REAL engine substrate (``tests/_engine_substrate.py``
via the ``t2_service_env`` fixture) — no mocks for the corpus itself: it
registers a corpus LARGER than the old single-page cap (600 > 500) under one
fresh owner prefix and asserts every document comes back from
``descendants()``, on both the dedicated-route path and (via a targeted
404 simulation that leaves the underlying ``/list`` calls hitting the real
engine) the pagination-fallback path.
"""
from __future__ import annotations

import os

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient

# Larger than the OLD single-shot `/list?limit=500` page. Any regression
# back to the unpaginated bug truncates this to at most 500 documents (and,
# per the live repro, frequently to exactly 0 — /list with no filter has no
# ordering guarantee that favors this prefix's rows landing in page one).
_CORPUS_SIZE = 600


def _client() -> HttpCatalogClient:
    return HttpCatalogClient(
        base_url=os.environ["NX_SERVICE_URL"],
        _token=os.environ["NX_SERVICE_TOKEN"],
    )


def _seed_descendants(client: HttpCatalogClient, prefix: str, n: int) -> set[str]:
    """Register *n* documents under a fresh owner at *prefix*; return their tumblers."""
    owner = client.register_owner(
        f"desc-owner-{prefix}", owner_type="curator", tumbler_prefix=prefix,
    )
    docs = [{"title": f"desc-doc-{i}", "content_type": "knowledge"} for i in range(n)]
    tumblers = client.register_many(owner, docs)
    assert len(tumblers) == n, "seed setup itself must mint exactly n documents"
    return {str(t) for t in tumblers}


class TestDescendantsRoute:
    """The dedicated ``GET /descendants`` route path (engine >= the route)."""

    def test_descendants_returns_every_document_in_a_large_subtree(
        self, t2_service_env,
    ) -> None:
        client = _client()
        expected = _seed_descendants(client, "701", _CORPUS_SIZE)

        got_tumblers = {d["tumbler"] for d in client.descendants("701")}

        missing = expected - got_tumblers
        extra = got_tumblers - expected
        assert got_tumblers == expected, (
            f"descendants() must return every document under the prefix: "
            f"expected {len(expected)}, got {len(got_tumblers)} "
            f"(missing {len(missing)}, extra {len(extra)})"
        )

    def test_descendants_excludes_siblings_and_the_root_itself(
        self, t2_service_env,
    ) -> None:
        client = _client()
        target = _seed_descendants(client, "702", 5)
        sibling = _seed_descendants(client, "703", 5)  # must NOT leak in

        got_tumblers = {d["tumbler"] for d in client.descendants("702")}

        assert got_tumblers == target
        assert not (got_tumblers & sibling), "sibling subtree leaked into descendants()"
        assert "702" not in got_tumblers, "the root itself is not its own descendant"


class TestDescendantsFallbackOn404:
    """The pagination fallback for an engine predating the route."""

    def test_fallback_paginates_to_completeness_when_route_missing(
        self, t2_service_env, monkeypatch,
    ) -> None:
        """Simulates an engine predating the ``/descendants`` route (404) — the
        client must fall back to an EXHAUSTIVE paginated ``/list`` walk, not
        the old truncating single-page behaviour. Only the ``/descendants``
        call is intercepted; every ``/list`` call underneath still hits the
        real engine, so the fallback's completeness is proven against a real
        corpus, not a stub.
        """
        client = _client()
        expected = _seed_descendants(client, "704", _CORPUS_SIZE)

        real_get = client._get

        def get_with_404_on_descendants(path, **params):
            if path == "/descendants":
                req = httpx.Request("GET", "http://fake.invalid/v1/catalog/descendants")
                resp = httpx.Response(404, request=req)
                raise httpx.HTTPStatusError("not found", request=req, response=resp)
            return real_get(path, **params)

        monkeypatch.setattr(client, "_get", get_with_404_on_descendants)

        got_tumblers = {d["tumbler"] for d in client.descendants("704")}
        missing = expected - got_tumblers
        assert got_tumblers == expected, (
            f"the 404 fallback must page /list to exhaustion, never silently "
            f"truncate: expected {len(expected)}, got {len(got_tumblers)} "
            f"(missing {len(missing)})"
        )

    def test_non_404_error_propagates_not_swallowed(
        self, t2_service_env, monkeypatch,
    ) -> None:
        client = _client()

        def get_with_500(path, **params):
            req = httpx.Request("GET", "http://fake.invalid/v1/catalog/descendants")
            resp = httpx.Response(500, request=req)
            raise httpx.HTTPStatusError("boom", request=req, response=resp)

        monkeypatch.setattr(client, "_get", get_with_500)

        with pytest.raises(httpx.HTTPStatusError):
            client.descendants("705")
