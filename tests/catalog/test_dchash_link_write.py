# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 4 (nexus-qrg1r.7): a malformed ``dchash:`` span is refused on the client before any request.

Covers ``check_link_span``, ``HttpCatalogClient.link`` / ``link_if_absent`` / ``links_upsert_many`` against the fake
link engine in ``_link_engine_fake.py`` (``State.requests`` records every request, so "refused before any request" is
an empty log), and the link dry run. No tenant is minted: the engine is the in-process fake.
"""
from __future__ import annotations

import pytest

import nexus.catalog.http_catalog_client as hcc
from nexus.catalog.dchash import check_link_span
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.link_dry_run import DryRunLinkWriter
from tests.catalog._link_engine_fake import State, start_server

H = "ab" * 32
BAD = [
    "dchash:abc:1-2",
    f"dchash:{H}:7-3",
    f"dchash:{H}:01-3",
]
GOOD = [
    "",
    "42-57",
    "3:100-250",
    f"chash:{H}",
    f"chash:{H}:1-5",
    f"dchash:{H}:2-9",
]


@pytest.fixture
def client():
    State.reset()
    State.add_document("1.1.1")
    State.add_document("1.1.2")
    server, url = start_server()
    with HttpCatalogClient(base_url=url, tenant="tenant_abc", _token="test_tok") as c:
        yield c
    server.shutdown()


def _edge(i: int = 2, **spans: str) -> dict:
    return {"from": {"tumbler": "1.1.1"}, "to": {"tumbler": f"1.1.{i}"}, "link_type": "relates",
            "created_by": "t", **spans}


class TestCheckLinkSpan:
    @pytest.mark.parametrize("span", BAD)
    def test_a_malformed_dchash_span_raises_naming_it(self, span: str) -> None:
        with pytest.raises(ValueError) as exc:
            check_link_span(span)
        assert repr(span) in str(exc.value)

    @pytest.mark.parametrize("span", GOOD)
    def test_every_other_form_and_a_valid_dchash_span_passes(self, span: str) -> None:
        assert check_link_span(span) is None

    def test_a_chash_span_is_not_revalidated_here(self) -> None:
        assert check_link_span("chash:not-hex") is None


class TestClientRefusal:
    @pytest.mark.parametrize("side", ["from_span", "to_span"])
    @pytest.mark.parametrize("span", BAD)
    def test_link_refuses_before_any_request(self, client, side: str, span: str) -> None:
        with pytest.raises(ValueError, match="malformed dchash span"):
            client.link("1.1.1", "1.1.2", "relates", "t", **{side: span})
        assert State.requests == []

    @pytest.mark.parametrize("side", ["from_span", "to_span"])
    @pytest.mark.parametrize("span", BAD)
    def test_link_if_absent_refuses_before_any_request_not_even_the_read(
        self, client, side: str, span: str,
    ) -> None:
        with pytest.raises(ValueError, match="malformed dchash span"):
            client.link_if_absent("1.1.1", "1.1.2", "relates", "t", **{side: span})
        assert State.requests == []

    @pytest.mark.parametrize("side", ["from_span", "to_span"])
    @pytest.mark.parametrize("span", BAD)
    def test_links_upsert_many_refuses_before_any_request(self, client, side: str, span: str) -> None:
        with pytest.raises(ValueError, match="malformed dchash span"):
            client.links_upsert_many([_edge(**{side: span})])
        assert State.requests == []

    @pytest.mark.parametrize("side", ["from_span", "to_span"])
    def test_a_bad_edge_on_the_second_page_writes_nothing_on_the_first(
        self, client, monkeypatch, side: str,
    ) -> None:
        monkeypatch.setattr(hcc, "_LINKS_BATCH_PAGE", 2)
        edges = [_edge(), _edge(), _edge(**{side: BAD[0]})]  # index 2 is the first edge of page 2
        with pytest.raises(ValueError, match="malformed dchash span"):
            client.links_upsert_many(edges)
        assert State.requests == []

    @pytest.mark.parametrize("span", GOOD)
    def test_the_other_forms_reach_the_engine_as_given(self, client, span: str) -> None:
        client.link("1.1.1", "1.1.2", "relates", "t", from_span=span)
        assert State.posts("/link")[-1]["from_span"] == span
        client.link_if_absent("1.1.1", "1.1.2", "cites", "t", to_span=span)
        assert State.posts("/link")[-1]["to_span"] == span
        client.links_upsert_many([_edge(from_span=span, to_span=span)])
        assert State.posts("/links/upsert_many")[-1]["edges"][0]["from_span"] == span


class TestDryRunRefusal:
    @pytest.mark.parametrize("side", ["from_span", "to_span"])
    @pytest.mark.parametrize("span", BAD)
    def test_link_if_absent_refuses_and_proposes_nothing(self, side: str, span: str) -> None:
        w = DryRunLinkWriter()
        with pytest.raises(ValueError, match="malformed dchash span"):
            w.link_if_absent("1.1.1", "1.1.2", "relates", "t", **{side: span})
        assert w.proposed == []

    @pytest.mark.parametrize("side", ["from_span", "to_span"])
    def test_links_upsert_many_refuses_and_proposes_nothing_even_for_the_edges_before_the_bad_one(
        self, side: str,
    ) -> None:
        w = DryRunLinkWriter()
        with pytest.raises(ValueError, match="malformed dchash span"):
            w.links_upsert_many([_edge(), _edge(**{side: BAD[1]})])
        assert w.proposed == []

    @pytest.mark.parametrize("span", GOOD)
    def test_the_other_forms_are_proposed(self, span: str) -> None:
        w = DryRunLinkWriter()
        assert w.link_if_absent("1.1.1", "1.1.2", "relates", "t", from_span=span) is True
        assert w.links_upsert_many([_edge(i=3, to_span=span)])["created"] == 1
        assert len(w.proposed) == 2
