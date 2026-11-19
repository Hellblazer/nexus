# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-uwggq.2.7 (RDR-241 Phase 2, gate Significant S2): ``GET /v1/catalog/docs/by-meta`` answers what the
client's own filter answers, against a real engine built from this tree.

For each filter set, the route's documents, paged to completion through ``HttpCatalogClient.documents_by_meta``,
equal ``documents_with_meta`` for the pairs combined with ``meta_at_least`` for the bound, over the same documents
read back from the engine, in the engine's text order of tumbler, and ``count`` is exact. No difference is allowed: a
``miss_count`` written as the float ``1e20`` reads back from jsonb as a plain integer on both sides (``meta_at_least``'s
docstring), so the float-widening case of nexus-uwggq.2.2 cannot arise on stored rows, and the rows are asserted to
read back that way.
"""
from __future__ import annotations

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.meta_filter import documents_with_meta, meta_at_least
from nexus.catalog.tumbler import Tumbler
from tests._catalog_fixture_ops import ActiveCatalog

# Engine-backed: each test mints a tenant on the session engine, so it lives in the integration family, outside the
# python_unit leg's tenant ratchet (tests/AGENTS.md, The tenant is opt-in).
pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

_ABSENT = object()
_MISS = [0, 1, 2, 3, "3", 3.0, 2.9, "2.9", "abc", True, None, _ABSENT, 1e20]
_SOURCE = [_ABSENT, "", "mineru", "file"]
_FLAG = [True, False, _ABSENT]
_TEXT = ["red", "blue", _ABSENT]
_TYPES = ["paper", "knowledge"]


def _fixture() -> list[tuple[str, str, dict]]:
    """``(title, content_type, meta)`` rows covering every value the bead names, crossed so each filter set has
    matches and misses."""
    rows: list[tuple[str, str, dict]] = []
    for i, miss in enumerate(_MISS):
        for j, src in enumerate(_SOURCE):
            meta: dict = {}
            if miss is not _ABSENT:
                meta["miss_count"] = miss
            if src is not _ABSENT:
                meta["extraction_source"] = src
            flag = _FLAG[(i + j) % 3]
            if flag is not _ABSENT:
                meta["quality_gate_overridden"] = flag
            color = _TEXT[(i * 2 + j) % 3]
            if color is not _ABSENT:
                meta["color"] = color
            rows.append((f"meta-eq {i}.{j}", _TYPES[(i + j) % 2], meta))
    return rows


_FILTER_SETS = {
    "one pair": {"meta": [("color", "red")]},
    "two pairs ANDed": {"meta": [("color", "blue"), ("quality_gate_overridden", "true")]},
    "boolean false": {"meta": [("quality_gate_overridden", "false")]},
    "extraction_source default": {"meta": [("extraction_source", "file")]},
    "extraction_source mineru": {"meta": [("extraction_source", "mineru")]},
    "miss_count bound": {"meta_min": [("miss_count", 2)]},
    "pair with content_type": {"meta": [("color", "red")], "content_type": "paper"},
    "bound with pair": {"meta": [("extraction_source", "file")], "meta_min": [("miss_count", 2)]},
}


def _page_all(client: HttpCatalogClient, page: int, **kw) -> tuple[list, int]:
    out, offset, count = [], 0, None
    while True:
        rows, count = client.documents_by_meta(limit=page, offset=offset, **kw)
        out.extend(rows)
        offset += len(rows)
        if not rows or offset >= count:
            return out, count


@pytest.mark.parametrize("name", sorted(_FILTER_SETS))
def test_the_route_answers_what_the_client_filter_answers(t2_service_env, name: str) -> None:
    tenant = t2_service_env
    cat = ActiveCatalog()
    owner = cat.register_owner("meta-eq", "curator")
    titles = {}
    for title, ctype, meta in _fixture():
        t = cat.register(Tumbler.parse(str(owner)), title, content_type=ctype, meta=meta,
                         source_uri=f"nx-note://meta-eq/{len(titles)}")
        titles[str(t)] = meta
    bigs = [t for t, m in titles.items() if isinstance(m.get("miss_count"), float) and m["miss_count"] == 1e20]

    client = HttpCatalogClient(tenant=tenant)
    back = {str(e.tumbler): e for e in client.all_documents(limit=0) if str(e.tumbler) in titles}
    assert len(back) == len(titles), "every fixture row reads back"
    assert bigs and all(back[t].meta["miss_count"] == 10**20 and isinstance(back[t].meta["miss_count"], int)
                        for t in bigs), "a stored 1e20 reads back as a plain integer"

    fs = _FILTER_SETS[name]
    entries = [back[t] for t in sorted(back)]   # the engine's text order of tumbler
    if fs.get("content_type"):
        entries = [e for e in entries if e.content_type == fs["content_type"]]
    oracle = documents_with_meta(entries, fs.get("meta", []))
    for key, n in fs.get("meta_min", []):
        oracle = [e for e in oracle if meta_at_least(e.meta, key, n)]
    expected = [str(e.tumbler) for e in oracle]
    assert expected, f"{name}: the fixture must give the filter set at least one match"

    for page in (1000, 7):
        rows, count = _page_all(client, page, **fs)
        got = [str(e.tumbler) for e in rows]
        assert got == sorted(got), f"{name}: rows come in the engine's text order of tumbler"
        assert count == len(got), f"{name}: count is exact"
        assert got == expected, f"{name}: the route answers exactly what the client filter answers"
