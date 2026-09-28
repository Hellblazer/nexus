# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1vc0n: the bulk owner-agnostic file_path lookup, against the REAL
engine substrate.

Companion to ``tests/catalog/test_cross_owner_file_path_resolution.py``
(the single-path form's own real-substrate contract). This pins the batch
route (``POST /v1/catalog/list_by_file_paths`` /
``HttpCatalogClient.find_all_by_file_paths``) nexus-1vc0n adds so
``indexer._catalog_hook``'s batched registrar can announce a cross-owner
mint at O(1) extra round trips per run instead of the O(N) a per-document
``announce_cross_owner_mint`` would cost inside its ``register_many`` loop.
"""
from __future__ import annotations

import os

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient


@pytest.fixture(autouse=True)
def _reset_bulk_lookup_404_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bulk route's 404 warning is a one-shot module global; a 404 test
    anywhere earlier in the process would leave it set and silence this
    file's own warning assertions. Reset it before every test so no test
    here depends on suite order (nexus-1vc0n review)."""
    import nexus.catalog.http_catalog_client as hcc

    monkeypatch.setattr(hcc, "_find_all_by_file_paths_404_warned", False)

_PATH_A = "docs/shared/bulk-one.md"
_PATH_B = "docs/shared/bulk-two.md"
_PATH_UNKNOWN = "docs/shared/bulk-no-such-path.md"


def _client() -> HttpCatalogClient:
    return HttpCatalogClient(
        base_url=os.environ["NX_SERVICE_URL"],
        _token=os.environ["NX_SERVICE_TOKEN"],
    )


@pytest.fixture
def two_paths_across_owners(t2_service_env):
    """Path A shared by two owners (the yzij1 steady state); path B solo."""
    client = _client()
    owner_a = client.register_owner(
        "1vc0n-owner-a", owner_type="curator", tumbler_prefix="81.1",
    )
    owner_b = client.register_owner(
        "1vc0n-owner-b", owner_type="curator", tumbler_prefix="81.2",
    )
    tumbler_a1 = client.register(
        owner_a, "bulk-one (A)", content_type="knowledge", file_path=_PATH_A,
        source_uri=f"file:///1vc0n/a/{_PATH_A}",
    )
    tumbler_a2 = client.register(
        owner_b, "bulk-one (B)", content_type="knowledge", file_path=_PATH_A,
        source_uri=f"file:///1vc0n/b/{_PATH_A}",
    )
    tumbler_b = client.register(
        owner_a, "bulk-two (solo)", content_type="knowledge", file_path=_PATH_B,
        source_uri=f"file:///1vc0n/a/{_PATH_B}",
    )
    return client, str(tumbler_a1), str(tumbler_a2), str(tumbler_b)


class TestBulkLookupReturnsAcrossOwners:
    def test_every_requested_path_gets_every_live_document(
        self, two_paths_across_owners,
    ) -> None:
        client, tumbler_a1, tumbler_a2, tumbler_b = two_paths_across_owners

        result = client.find_all_by_file_paths([_PATH_A, _PATH_B])

        assert set(result.keys()) == {_PATH_A, _PATH_B}
        assert {str(e.tumbler) for e in result[_PATH_A]} == {tumbler_a1, tumbler_a2}
        assert {str(e.tumbler) for e in result[_PATH_B]} == {tumbler_b}

    def test_unknown_paths_are_omitted_not_empty_listed(
        self, two_paths_across_owners,
    ) -> None:
        client, _, _, _ = two_paths_across_owners

        result = client.find_all_by_file_paths([_PATH_A, _PATH_UNKNOWN])

        assert _PATH_A in result
        assert _PATH_UNKNOWN not in result

    def test_empty_input_returns_empty_dict_with_no_call(
        self, two_paths_across_owners, monkeypatch,
    ) -> None:
        client, _, _, _ = two_paths_across_owners
        calls = []
        monkeypatch.setattr(
            HttpCatalogClient, "_post",
            lambda self, path, *a, **kw: calls.append(path),
        )

        assert client.find_all_by_file_paths([]) == {}
        assert calls == []


class TestBulkLookupTombstoneParity:
    def test_a_tombstoned_document_never_appears(self, t2_service_env) -> None:
        client = _client()
        owner = client.register_owner(
            "1vc0n-owner-dead", owner_type="curator", tumbler_prefix="81.3",
        )
        path = "docs/shared/bulk-tombstoned.md"
        tumbler = client.register(
            owner, "will be tombstoned", content_type="knowledge",
            file_path=path, source_uri=f"file:///1vc0n/dead/{path}",
        )
        client.delete_document(tumbler)

        result = client.find_all_by_file_paths([path])

        assert path not in result


class TestBulkLookupPaging:
    def test_a_batch_larger_than_one_page_makes_more_than_one_request(
        self, t2_service_env, monkeypatch,
    ) -> None:
        """Pin the paging contract without seeding 300+ real documents:
        shrink the page constant and count the underlying POSTs."""
        import nexus.catalog.http_catalog_client as hcc

        monkeypatch.setattr(hcc, "_FILE_PATHS_LOOKUP_PAGE", 2)
        client = _client()

        posted_bodies: list[dict] = []
        original_post = HttpCatalogClient._post

        def _counting_post(self, path, body=None, **kwargs):
            if path == "/list_by_file_paths":
                posted_bodies.append(body)
            return original_post(self, path, body, **kwargs)

        monkeypatch.setattr(HttpCatalogClient, "_post", _counting_post)

        result = client.find_all_by_file_paths(
            ["p/a.md", "p/b.md", "p/c.md", "p/d.md", "p/e.md"],
        )

        assert len(posted_bodies) == 3, (
            f"5 paths at page size 2 must page as 2+2+1, got "
            f"{len(posted_bodies)} requests: {posted_bodies}"
        )
        assert result == {}, "none of these paths were ever registered"


class TestBulkLookupEngineFloorFallback:
    def test_a_404_degrades_to_one_find_all_by_file_path_call_per_path(
        self, two_paths_across_owners, monkeypatch,
    ) -> None:
        client, tumbler_a1, tumbler_a2, tumbler_b = two_paths_across_owners

        original_post = HttpCatalogClient._post

        def _post_404_on_bulk_route(self, path, *args, **kwargs):
            if path == "/list_by_file_paths":
                request = httpx.Request("POST", "http://test-engine/list_by_file_paths")
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError(
                    "not found", request=request, response=response,
                )
            return original_post(self, path, *args, **kwargs)

        monkeypatch.setattr(HttpCatalogClient, "_post", _post_404_on_bulk_route)

        find_all_calls: list[str] = []
        original_single = HttpCatalogClient.find_all_by_file_path

        def _counting_single(self, file_path):
            find_all_calls.append(file_path)
            return original_single(self, file_path)

        monkeypatch.setattr(
            HttpCatalogClient, "find_all_by_file_path", _counting_single,
        )

        result = client.find_all_by_file_paths([_PATH_A, _PATH_B])

        assert set(result.keys()) == {_PATH_A, _PATH_B}
        assert {str(e.tumbler) for e in result[_PATH_A]} == {tumbler_a1, tumbler_a2}
        assert {str(e.tumbler) for e in result[_PATH_B]} == {tumbler_b}
        assert sorted(find_all_calls) == sorted([_PATH_A, _PATH_B]), (
            "the 404 fallback must degrade to one find_all_by_file_path "
            "call per path in the 404'd page"
        )

    def test_the_404_warning_logs_exactly_once_per_process(
        self, two_paths_across_owners, monkeypatch,
    ) -> None:
        from structlog.testing import capture_logs

        import nexus.catalog.http_catalog_client as hcc

        # The one-shot flag is a module global; reset it so this test does
        # not depend on suite ordering having already tripped it.
        monkeypatch.setattr(hcc, "_find_all_by_file_paths_404_warned", False)
        monkeypatch.setattr(hcc, "_FILE_PATHS_LOOKUP_PAGE", 1)

        client, _, _, _ = two_paths_across_owners
        original_post = HttpCatalogClient._post

        def _post_404_on_bulk_route(self, path, *args, **kwargs):
            if path == "/list_by_file_paths":
                request = httpx.Request("POST", "http://test-engine/list_by_file_paths")
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError(
                    "not found", request=request, response=response,
                )
            return original_post(self, path, *args, **kwargs)

        monkeypatch.setattr(HttpCatalogClient, "_post", _post_404_on_bulk_route)

        with capture_logs() as logs:
            # Two pages (page size 1) both 404 -- the warning must fire once,
            # not once per page.
            client.find_all_by_file_paths([_PATH_A, _PATH_B])

        floor_events = [
            e for e in logs
            if e.get("event") == "catalog_list_by_file_paths_engine_floor"
        ]
        assert len(floor_events) == 1
