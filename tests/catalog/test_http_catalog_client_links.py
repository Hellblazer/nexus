# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 7 (nexus-u5m5g.8): the three client methods the linking generators ride on.

``links_upsert_many`` and ``links_same_content`` page at the engine's 1000-per-request cap and sum the pages;
``list_scoped`` is the engine's combined list read and refuses to send an unscoped one. Requests are counted at the
transport against the fake engine in ``_link_engine_fake.py``.
"""
from __future__ import annotations

import pytest

from nexus.catalog.factory import _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import HttpCatalogClient
from tests.catalog._link_engine_fake import State, start_server


@pytest.fixture
def client():
    State.reset()
    server, url = start_server()
    with HttpCatalogClient(base_url=url, tenant="tenant_abc", _token="test_tok") as c:
        yield c
    server.shutdown()


def _edge(i: int, to: str = "1.1.1") -> dict:
    return {"from": {"tumbler": f"1.1.{i}"}, "to": {"tumbler": to}, "link_type": "relates", "created_by": "t"}


class TestLinksUpsertMany:
    def test_pages_at_a_thousand_and_sums_the_pages(self, client) -> None:
        for i in range(1, 2003):
            State.add_document(f"1.1.{i}")
        edges = [_edge(i) for i in range(2, 2002)]  # 2000 edges
        result = client.links_upsert_many(edges)
        assert [len(b["edges"]) for b in State.posts("/links/upsert_many")] == [1000, 1000]
        assert result == {"created": 2000, "skipped": 0, "unresolved": []}

    def test_a_second_send_is_all_skipped(self, client) -> None:
        for i in range(1, 6):
            State.add_document(f"1.1.{i}")
        edges = [_edge(i) for i in range(2, 6)]
        client.links_upsert_many(edges)
        assert client.links_upsert_many(edges) == {"created": 0, "skipped": 4, "unresolved": []}

    def test_an_unresolved_entry_carries_its_index_in_the_whole_list_not_its_page(self, client) -> None:
        for i in range(1, 1102):
            State.add_document(f"1.1.{i}")
        edges = [_edge(i) for i in range(2, 1102)]  # 1100 edges, two pages
        edges[1000]["from"] = {"tumbler": "9.9.9"}          # second page, position 0 of it
        edges[1050]["to"] = {"owner": "1.1", "file_path": "nowhere.py"}  # second page, position 50
        result = client.links_upsert_many(edges)
        assert [(u["index"], u["side"], u["reason"]) for u in result["unresolved"]] == [
            (1000, "from", "dangling"), (1050, "to", "no_path"),
        ]
        assert result["created"] == 1098

    def test_an_empty_list_sends_nothing(self, client) -> None:
        assert client.links_upsert_many([]) == {"created": 0, "skipped": 0, "unresolved": []}
        assert State.requests == []


class TestLinksSameContent:
    def test_pages_at_a_thousand_and_forwards_prune(self, client) -> None:
        client.links_same_content([f"1.1.{i}" for i in range(2500)], prune=True)
        bodies = State.posts("/links/same_content")
        assert [len(b["tumblers"]) for b in bodies] == [1000, 1000, 500]
        assert all(b["prune"] is True for b in bodies)

    def test_prune_defaults_to_false(self, client) -> None:
        client.links_same_content(["1.1.1"])
        assert State.posts("/links/same_content") == [{"tumblers": ["1.1.1"], "prune": False}]

    def test_sums_created_and_retracted_and_keeps_a_refusal(self, client) -> None:
        State.add_document("1.1.1", content_type="pdf", index_content_hash="h")
        State.add_document("1.1.2", content_type="pdf", index_content_hash="h")
        assert client.links_same_content(["1.1.1", "1.1.2"]) == {"created": 1, "retracted": 0}
        # a dedup link exists but no live pdf/paper document does: the engine refuses to prune
        State.documents.clear()
        refused = client.links_same_content(["1.1.2"], prune=True)
        assert refused == {"created": 0, "retracted": 0, "refused": "empty_fetch"}

    def test_an_empty_list_sends_nothing(self, client) -> None:
        assert client.links_same_content([]) == {"created": 0, "retracted": 0}
        assert State.requests == []


class TestListScoped:
    def test_owner_with_types_sends_the_combined_filter_with_repeated_content_type(self, client) -> None:
        State.add_document("1.1.1", content_type="rdr")
        State.add_document("1.1.2", content_type="prose")
        State.add_document("1.1.3", content_type="code")
        State.add_document("1.2.1", content_type="rdr")
        got = client.list_scoped(owner="1.1", content_types=["rdr", "prose"])
        assert [str(e.tumbler) for e in got] == ["1.1.1", "1.1.2"]
        assert State.lists() == [{"owner": ["1.1"], "content_type": ["rdr", "prose"]}]

    def test_a_source_uri_prefix_alone_is_a_scoped_read(self, client) -> None:
        State.add_document("1.1.1", content_type="rdr", source_uri="file:///r/docs/rdr/a.md")
        State.add_document("1.2.1", content_type="rdr", source_uri="file:///other/docs/rdr/a.md")
        got = client.list_scoped(source_uri_prefix="file:///r/docs/rdr/")
        assert [str(e.tumbler) for e in got] == ["1.1.1"]
        assert State.lists() == [{"source_uri_prefix": ["file:///r/docs/rdr/"]}]

    @pytest.mark.parametrize("kwargs", [
        {},
        {"owner": "1.1"},
        {"content_types": ["rdr"]},
        {"owner": "", "content_types": ["rdr"]},
    ])
    def test_an_unscoped_read_raises_and_sends_nothing(self, client, kwargs) -> None:
        """An owner with no type, or a type with no owner, would reach the engine's single-filter or
        tenant-wide branches: refused here."""
        with pytest.raises(ValueError, match="needs an owner with content_types, or a source_uri_prefix"):
            client.list_scoped(**kwargs)
        assert State.requests == []


class TestWriterProxy:
    def test_the_service_writer_exposes_the_two_link_writes_and_not_the_read(self, client) -> None:
        writer = _ServiceCatalogWriter(client)
        assert callable(writer.links_upsert_many) and callable(writer.links_same_content)
        with pytest.raises(AttributeError):
            writer.list_scoped  # noqa: B018 - a read: not on the write-only proxy
