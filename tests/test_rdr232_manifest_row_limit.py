# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 (nexus-jypmj.6): a per-document row limit on ``/manifest/get_many``, so the named-source resolver
can water-fill its row budget across sources again.

``get_manifests`` had no row limit, so nexus-jypmj.1.17 capped the READ by requesting the smallest documents
first; under the cap the largest named source then got no evidence at all (``manifest_not_read``) where the
earlier water-fill sampled every source. With the engine capability ``catalog_manifest_row_limit_supported``
the resolver asks for ``max_rows_per_doc`` = the water-fill level over the documents' recorded chunk counts;
the engine returns a long document as an even sample and reports every document's true row count, so the
read stays inside the budget AND reaches every source. An engine without the capability keeps the
smallest-first read, exactly as before; that path is pinned by ``test_rdr232_primary_evidence.py`` and again
here for the three ways a sampler can be absent.

Tenant-free: ``FakeCatalog`` is the strict four-call fake of the primary-evidence tests, and the sampler is
passed to ``select_primary_evidence`` explicitly (so the fake needs no capability surface). The HTTP half
runs the real ``HttpCatalogClient`` over ``httpx.MockTransport``.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from nexus.plans.evidence import SourceRef
from nexus.plans.primary_evidence import DEFAULT_CAPS, resolve_named_sources, select_primary_evidence
from tests.test_rdr232_primary_evidence import (
    ART_OWNER,
    FakeCatalog,
    FakeT3,
    _doc,
    _pulled,
    _resolved,
    _sectioned,
)

REPO = Path(__file__).resolve().parents[1]


def _engine_indexes(count: int, limit: int) -> list[int]:
    """``ManifestRowSample.indexes`` (Java): the rows of a document the engine returns under a limit."""
    if count <= limit:
        return list(range(count))
    if limit == 1:
        return [0]
    return [i * (count - 1) // (limit - 1) for i in range(limit)]


class FakeSampler:
    """What ``HttpCatalogClient.get_manifests_sampled`` returns against an engine that honours the limit."""

    def __init__(self, cat: FakeCatalog, *, answer: bool = True, fail: Exception | None = None) -> None:
        self._cat = cat
        self._answer = answer
        self._fail = fail
        self.calls: list[tuple[list[str], int]] = []

    def __call__(self, doc_ids: list[str], max_rows_per_doc: int):
        self.calls.append((list(doc_ids), max_rows_per_doc))
        if self._fail is not None:
            raise self._fail
        if not self._answer:
            return None
        manifests, counts = {}, {}
        for d in doc_ids:
            rows = self._cat.manifests.get(d)
            if rows is None:
                continue
            counts[d] = len(rows)
            manifests[d] = [rows[i] for i in _engine_indexes(len(rows), max_rows_per_doc)]
        return manifests, counts


def _three(cat: FakeCatalog, t3: FakeT3, sizes=(40, 5, 30)):
    for i, n in enumerate(sizes):
        _doc(cat, t3, f"1.2.1{i}", f"docs/rdr/rdr-01{i}-doc-{i}.md", _sectioned(n, tag=str(i)))
    return [SourceRef.parse(f"RDR-01{i}") for i in range(len(sizes))]


def _read(cat, t3, refs, caps, sampler=None):
    res = resolve_named_sources(refs, _resolved(ART_OWNER), cat=cat, caps=caps)
    return select_primary_evidence(res, cat=cat, fetch_chunks=t3.fetch, caps=caps, manifest_sampler=sampler)


# ── the new engine ──────────────────────────────────────────────────────────


def test_with_a_row_limit_every_named_source_is_read_where_the_cap_used_to_drop_the_largest() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    refs = _three(cat, t3)
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=30, max_manifest_rows=30)
    sampler = FakeSampler(cat)
    primary = _read(cat, t3, refs, caps, sampler)

    assert [d.status for d in primary.documents] == ["ok", "ok", "ok"], "no source is manifest_not_read"
    (call,) = sampler.calls
    assert sorted(call[0]) == ["1.2.10", "1.2.11", "1.2.12"] and call[1] == 12, "recorded 40/5/30 water-fill a budget of 30 at 12"
    assert _pulled(cat) == [], "the smallest-first path is not used"
    big, small, mid = primary.documents
    assert (big.manifest_rows, small.manifest_rows, mid.manifest_rows) == (40, 5, 30), "the TRUE row counts"
    assert (big.rows_sampled, small.rows_sampled, mid.rows_sampled) == (True, False, True)
    assert "max_manifest_rows_read" in primary.caps_hit, "a sample is a cap hit and is disclosed"
    assert {i.doc_id for i in primary.items} == {"1.2.10", "1.2.11", "1.2.12"}
    assert big.partial is True


def test_the_rows_returned_stay_inside_the_budget() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    refs = _three(cat, t3)
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=30, max_manifest_rows=30)
    sampler = FakeSampler(cat)
    manifests, counts = sampler(["1.2.10", "1.2.11", "1.2.12"], 12)
    assert sum(len(r) for r in manifests.values()) == 12 + 5 + 12 <= caps.max_manifest_rows_read
    assert counts == {"1.2.10": 40, "1.2.11": 5, "1.2.12": 30}
    assert refs


def test_within_the_budget_the_read_is_one_unlimited_call_as_before_even_on_a_new_engine() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    refs = _three(cat, t3, sizes=(10, 5, 8))
    sampler = FakeSampler(cat)
    primary = _read(cat, t3, refs, replace(DEFAULT_CAPS, max_manifest_rows_read=50), sampler)

    assert sampler.calls == [], "recorded counts fit the cap: nothing to sample"
    assert [sorted(c) for c in _pulled(cat)] == [["1.2.10", "1.2.11", "1.2.12"]]
    assert [d.status for d in primary.documents] == ["ok", "ok", "ok"]
    assert "max_manifest_rows_read" not in primary.caps_hit


def test_a_lone_source_larger_than_the_budget_is_sampled_to_the_budget() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.10", "docs/rdr/rdr-010-huge.md", _sectioned(80, tag="huge"))
    sampler = FakeSampler(cat)
    primary = _read(cat, t3, [SourceRef.parse("RDR-010")], replace(DEFAULT_CAPS, max_manifest_rows_read=50, max_manifest_rows=50), sampler)

    assert sampler.calls == [(["1.2.10"], 50)]
    (doc,) = primary.documents
    assert (doc.status, doc.manifest_rows, doc.rows_sampled) == ("ok", 80, True)


# ── the old engine, and an engine that ignores the key ──────────────────────


@pytest.mark.parametrize("how", ["no sampler", "sampler answers None", "sampler raises"])
def test_without_a_working_sampler_the_smallest_documents_are_read_first_exactly_as_before(how: str) -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    refs = _three(cat, t3)
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=30)
    sampler = {
        "no sampler": None,
        "sampler answers None": FakeSampler(cat, answer=False),
        "sampler raises": FakeSampler(cat, fail=RuntimeError("boom")),
    }[how]
    primary = _read(cat, t3, refs, caps, sampler)

    assert _pulled(cat) == [["1.2.11"], ["1.2.12"]], "small then mid; the largest is never requested"
    assert [(d.source, d.status) for d in primary.documents] == [
        ("RDR-010", "manifest_not_read"), ("RDR-011", "ok"), ("RDR-012", "ok"),
    ]
    assert primary.caps_hit == ("max_manifest_rows_read",)


# ── core wiring: the sampler a real catalog gives ───────────────────────────


def test_the_production_sampler_is_the_catalogs_only_when_it_advertises_the_capability() -> None:
    from nexus.mcp.core import _manifest_sampler

    class Real:
        def __init__(self, supported):
            self._supported = supported

        def supports_manifest_row_limit(self):
            return self._supported

        def get_manifests_sampled(self, doc_ids, max_rows_per_doc):
            return {}, {}

    assert _manifest_sampler(Real(True)) is not None
    assert _manifest_sampler(Real(False)) is None
    assert _manifest_sampler(MagicMock()) is None, "a double with no real probe is an old engine"
    assert _manifest_sampler(FakeCatalog()) is None, "a catalog with no capability surface is an old engine"
    assert _manifest_sampler(None) is None


# ── the wire contract ───────────────────────────────────────────────────────


def _client(handler):
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return cat


def _wire_row(doc: str, pos: int) -> dict:
    return {"doc_id": doc, "position": pos, "chash": f"{doc}:{pos}".encode().hex().ljust(64, "0"), "collection": "c"}


def test_get_manifests_sampled_sends_the_limit_and_returns_rows_with_the_true_counts() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={
            "manifests": {"1.2.1": [_wire_row("1.2.1", p) for p in (0, 4, 8)]},
            "count": 1, "max_rows_per_doc": body["max_rows_per_doc"], "row_counts": {"1.2.1": 9},
        })

    got = _client(handler).get_manifests_sampled(["1.2.1"], 3)
    assert seen == [{"doc_ids": ["1.2.1"], "max_rows_per_doc": 3}]
    assert got is not None
    manifests, counts = got
    assert [r.position for r in manifests["1.2.1"]] == [0, 4, 8] and counts == {"1.2.1": 9}


def test_get_manifests_sampled_is_none_when_the_engine_did_not_echo_the_limit() -> None:
    """An engine that predates the limit ignores the key and returns every row: HTTP 200 is no evidence it ran."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"manifests": {"1.2.1": [_wire_row("1.2.1", p) for p in range(9)]}, "count": 1})

    assert _client(handler).get_manifests_sampled(["1.2.1"], 3) is None


def test_get_manifests_sampled_refuses_a_page_whose_count_does_not_reconcile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "manifests": {"1.2.1": [_wire_row("1.2.1", 0)]}, "count": 2, "max_rows_per_doc": 3, "row_counts": {"1.2.1": 1},
        })

    with pytest.raises(RuntimeError, match="truncated"):
        _client(handler).get_manifests_sampled(["1.2.1"], 3)


def test_get_manifests_without_a_limit_is_the_request_it_always_was() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"manifests": {}, "count": 0})

    _client(handler).get_manifests(["1.2.1"])
    assert seen == [{"doc_ids": ["1.2.1"]}]


def test_the_capability_is_read_from_version_strictly() -> None:
    def make(body):
        return _client(lambda request: httpx.Response(200, json=body))

    assert make({"catalog_manifest_row_limit_supported": True}).supports_manifest_row_limit() is True
    assert make({"catalog_manifest_row_limit_supported": "true"}).supports_manifest_row_limit() is False
    assert make({}).supports_manifest_row_limit() is False
    assert _client(lambda request: httpx.Response(500, json={})).supports_manifest_row_limit() is False
    assert make({"catalog_graph_scope_filter_supported": True}).supports_manifest_row_limit() is False, "its own flag"


def test_the_engine_sample_arithmetic_is_the_java_one() -> None:
    java = (REPO / "service/src/main/java/dev/nexus/service/db/ManifestRowSample.java").read_text()
    assert re.search(r"\(int\) \(\(\(long\) i \* \(count - 1\)\) / \(limit - 1\)\)", java), "the formula this test mirrors"
    assert _engine_indexes(9, 4) == [0, 2, 5, 8] and _engine_indexes(10, 2) == [0, 9] and _engine_indexes(7, 1) == [0]
