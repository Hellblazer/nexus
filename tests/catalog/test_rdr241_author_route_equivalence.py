# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-uwggq.2.6 (RDR-241 Phase 2, gate Significant S4): ``GET /v1/catalog/docs/by-author`` never leaves out a row the
client's final filter keeps, including for non-ASCII names, against a real engine built from this tree.

The rule of the client's filter (``_keep`` in ``mcp/catalog.py``): ``needle.lower() in (author or "").lower()``, Python's
Unicode default lowercase (the full mapping), no normalization, ``%`` and ``_`` literal, an empty or absent author never
matching. The engine folds with the simple mapping, reads the final sigma as the medial one and drops U+0307, so its answer
is a superset of that rule (Sam 2026-11-19, T2 nexus/sam-ruling-rdr241-author-fold-2026-11-19): for each needle, the route's
documents paged to completion contain every oracle document, its ``count`` is the number of route rows (an upper bound
on the oracle's), and after the client's filter the documents equal the oracle in the engine's text order of tumbler.
Needles and authors are written as ``\\u`` escapes so an editor cannot normalize them.
"""
from __future__ import annotations

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.tumbler import Tumbler
from tests._catalog_fixture_ops import ActiveCatalog

AUTHORS: list[str | None] = [
    "Leslie Lamport",
    "G\u00f6del, Kurt",            # precomposed o with diaeresis
    "Go\u0308del, Kurt",           # o plus combining diaeresis
    "G\u00d6DEL",
    "Paul Erd\u0151s",
    "Anders \u00c5ngstr\u00f6m",
    "Jan \u0141ukasiewicz",
    "Stra\u00dfe",
    "\u0130smet \u0130n\u00f6n\u00fc",
    "\u03a3\u03af\u03c3\u03c5\u03c6\u03bf\u03c2",
    "\u03a3\u038a\u03a3\u03a5\u03a6\u039f\u03a3",   # the same name in capitals, ending in a capital sigma
    "\u041f\u0443\u0448\u043a\u0438\u043d",
    "\u6e6f\u5ddd\u79c0\u6a39",
    "100%_done",
    "",
    None,
]

NEEDLES = [
    "lamport", "hild", "g\u00f6del", "G\u00d6DEL", "\u00f6del", "erd\u0151s", "ERD\u0150S", "\u00e5ngstr\u00f6m",
    "\u0142ukasiewicz", "stra\u00dfe", "\u0130n\u00f6n\u00fc", "in\u00f6n\u00fc", "\u03c3\u03af\u03c3\u03c5\u03c6\u03bf\u03c2",
    "\u03a3\u038a\u03a3\u03a5\u03a6\u039f\u03a3", "\u041f\u0423\u0428\u041a\u0418\u041d", "\u6e6f\u5ddd", "%", "_", "100%_",
]


def _keep(needle: str, author: str | None) -> bool:
    return needle.lower() in (author or "").lower()


def test_the_route_is_a_superset_and_equals_the_client_filter_after_it(t2_service_env) -> None:
    tenant = t2_service_env
    cat = ActiveCatalog()
    owner = cat.register_owner("author-eq", "curator")
    by_tumbler: dict[str, str | None] = {}
    for i, author in enumerate(AUTHORS):
        kw = {} if author is None else {"author": author}
        t = cat.register(Tumbler.parse(str(owner)), f"author-eq {i}", content_type="paper",
                         source_uri=f"nx-note://author-eq/{i}", **kw)
        by_tumbler[str(t)] = author

    client = HttpCatalogClient(tenant=tenant)
    back = {str(e.tumbler): e.author for e in client.all_documents(limit=0) if str(e.tumbler) in by_tumbler}
    assert len(back) == len(by_tumbler), "every fixture row reads back"

    # no normalization: the precomposed and the decomposed Gödel stay two authors, and "gödel" (precomposed) selects
    # exactly one of them, so an editor that normalized the fixture would fail here rather than pass vacuously
    assert AUTHORS[1] != AUTHORS[2] and len(AUTHORS[1]) == len(AUTHORS[2]) - 1
    assert sum(_keep("g\u00f6del", a) for a in AUTHORS) == 2, "precomposed G\u00f6del and G\u00d6DEL, not the decomposed one"
    selecting = 0
    for needle in NEEDLES:
        expected = [t for t in sorted(by_tumbler) if _keep(needle, by_tumbler[t])]
        selecting += bool(expected)
        for page in (1000, 2):
            got, offset = [], 0
            while True:
                rows, count = client.documents_by_author(needle, limit=page, offset=offset)
                got.extend(rows)
                offset += len(rows)
                if not rows or offset >= count:
                    break
            tumblers = [str(e.tumbler) for e in got]
            assert tumblers == sorted(tumblers), f"{needle!r}: rows come in the engine's text order of tumbler"
            assert count == len(tumblers) >= len(expected), f"{needle!r}: count is the route's, an upper bound"
            missing = [t for t in expected if t not in tumblers]
            assert missing == [], f"{needle!r}: the route left out rows the client keeps: {missing}"
            kept = [str(e.tumbler) for e in got if _keep(needle, e.author)]
            assert kept == expected, f"{needle!r}: after the client's filter the route equals the oracle"
    assert selecting >= 15, "non-vacuity: most needles select at least one author"
