# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 section 3 (nexus-jypmj.1.13): the engine pre-cap graph-scope filter, client half.

The engine cuts a walk at 500 nodes ordered by ``(min_depth, tumbler)`` before any client sees it, so
out-of-scope documents the client later drops had already taken the slots in-scope evidence needed. With
the ``catalog_graph_scope_filter_supported`` capability the client sends the scope as a predicate
(``scope_filter``: an allowed set of collections, or a tumbler subtree), the engine applies it BEFORE the
cut, and answers with a ``scope_filter`` marker. The client requires the marker: a 200 from an engine that
ignored the key is no evidence the predicate ran.

An engine without the capability (or a reply without the marker) gives a DISCLOSED incomplete traversal and
no secondary graph evidence when the walk may have been cut: never a containment claim. A walk that was
not cut is complete, and the client's own scope filter over it is exact, so it is kept as it always was.

The tests run ``traverse`` against a fake catalog that behaves like the engine on both sides of the
capability (``_Engine`` honours or ignores the predicate), the pattern ``test_rdr232_traversal_policy.py``
uses; the HTTP half runs the real ``HttpCatalogClient`` over ``httpx.MockTransport``.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
RDR = "rdr__1-2__bge-base-en-v15-768__v1"
CODE = "code__1-2__bge-base-en-v15-768__v1"
SEED = "1.2.1"
CAP = 500


def _node(tumbler: str, collection: str):
    return SimpleNamespace(
        tumbler=tumbler, physical_collection=collection,
        content_type={"rdr": "rdr", "code": "code"}[collection.split("__")[0]],
    )


def _edge(frm: str, to: str, link_type: str = "relates"):
    return SimpleNamespace(from_tumbler=frm, to_tumbler=to, link_type=link_type)


class _Engine:
    """A catalog that walks like the Java engine: nodes ordered by tumbler, the first 500 kept.

    ``honour`` True: the engine knows ``scope_filter``, applies it before the cut (seeds exempt) and returns
    the marker. ``honour`` False with ``advertise`` True models an edge or engine that advertises the flag
    but drops the key (no marker). ``advertise`` False is an engine that predates the capability.
    """

    def __init__(self, nodes, edges, *, advertise=True, honour=True, registry=None, registry_error=None):
        self._nodes = nodes
        self._edges = edges
        self._advertise = advertise
        self._honour = honour
        self._registry = registry if registry is not None else sorted({n.physical_collection for n in nodes})
        self._registry_error = registry_error
        self.graph_kwargs: list[dict] = []
        self.registry_calls = 0
        self.registry_lifecycle_states: list[str | None] = []
        self.quarantined: list[str] = []

    def supports_graph_scope_filter(self) -> bool:
        return self._advertise

    def list_collections(self, lifecycle_state=None, content_type=None):
        self.registry_calls += 1
        self.registry_lifecycle_states.append(lifecycle_state)
        if self._registry_error is not None:
            raise self._registry_error
        rows = [{"name": name, "lifecycle_state": "live"} for name in self._registry]
        rows += [{"name": name, "lifecycle_state": "quarantine"} for name in self.quarantined]
        return [r for r in rows if lifecycle_state is None or r["lifecycle_state"] == lifecycle_state]

    def _walk(self, seeds, **kw):
        self.graph_kwargs.append(dict(kw))
        nodes = list(self._nodes)
        reply: dict = {}
        flt = kw.get("scope_filter")
        if flt is not None and self._honour:
            seed_ids = {str(s) for s in seeds}
            if "collections" in flt:
                allowed = set(flt["collections"])
                keep = lambda n: str(n.tumbler) in seed_ids or n.physical_collection in allowed  # noqa: E731
            else:
                sub = flt["subtree"]
                keep = lambda n: str(n.tumbler) in seed_ids or str(n.tumbler) == sub or str(n.tumbler).startswith(sub + ".")  # noqa: E731
            kept = [n for n in nodes if keep(n)]
            reply["scope_filter"] = {"applied": True, "dropped_out_of_scope": len(nodes) - len(kept)}
            nodes = kept
        nodes = sorted(nodes, key=lambda n: [int(p) for p in str(n.tumbler).split(".")])[:CAP]
        keep_ids = {str(n.tumbler) for n in nodes}
        reply["nodes"] = nodes
        reply["edges"] = [e for e in self._edges if e.from_tumbler in keep_ids and e.to_tumbler in keep_ids]
        return reply

    def graph(self, seed, **kw):
        return self._walk([seed], **kw)

    def graph_many(self, seeds, **kw):
        return self._walk(seeds, **kw)

    def get_manifests(self, tumblers):
        return {t: [SimpleNamespace(chash=f"{t}:{j}".encode().hex().ljust(64, "0")) for j in range(2)] for t in tumblers}


def _run(cat, **kw):
    from nexus.mcp.core import traverse

    with patch("nexus.mcp.core._get_catalog", return_value=cat):
        return traverse(**kw)


def _policy(scope: str = "rdr", **over) -> dict:
    return {"allowed_collections": scope, **over}


def _crowded():
    """A seed whose walk reaches 520 code documents that sort BEFORE 12 rdr documents (1.2.10.x < 1.2.20.x)."""
    nodes = [_node(SEED, RDR)]
    nodes += [_node(f"1.2.10.{i}", CODE) for i in range(520)]
    nodes += [_node(f"1.2.20.{i}", RDR) for i in range(12)]
    edges = [_edge(n.tumbler, SEED) for n in nodes[1:]]
    return nodes, edges


# ── the new engine ──────────────────────────────────────────────────────────


def test_a_scoped_traverse_sends_the_predicate_and_the_in_scope_evidence_survives_the_cut() -> None:
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges, registry=[RDR, CODE, "rdrx__1-2__bge-base-en-v15-768__v1"])
    out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))

    (kw,) = cat.graph_kwargs
    assert kw["scope_filter"] == {"collections": [RDR]}, "the registered collections the scope admits; rdrx is not rdr"
    in_scope = {f"1.2.20.{i}" for i in range(12)}
    assert in_scope <= set(out["tumblers"]), "the engine spent its 500 slots on in-scope nodes"
    rep = out["traversal"]
    assert rep["scope_prefilter"] == "applied"
    assert rep["engine_dropped_out_of_scope"] == 520
    assert rep["secondary_omitted"] is False
    assert rep["possibly_truncated"] is False and rep["contained"] is True, "13 nodes came back: a complete in-scope walk"


def test_the_registry_is_read_through_the_live_view_so_a_quarantine_row_never_reaches_the_predicate() -> None:
    """nexus-bc7ps: routing reads lifecycle_state=live; a quarantine-<name> row the scope would otherwise admit
    by prefix is not sent to the engine."""
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges, registry=[RDR])
    cat.quarantined = ["rdr__1-9__bge-base-en-v15-768__v1"]
    _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    assert cat.registry_lifecycle_states == ["live"]
    (kw,) = cat.graph_kwargs
    assert kw["scope_filter"] == {"collections": [RDR]}


def test_without_the_predicate_the_same_walk_loses_its_in_scope_evidence_to_the_cut() -> None:
    """The control: the engine of today cuts 500 code nodes first and the 12 rdr documents are gone."""
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges, advertise=False)
    out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))

    assert not any(t.startswith("1.2.20.") for t in out["tumblers"])


def test_a_collection_scope_sends_the_one_name_without_asking_the_registry() -> None:
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges)
    _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy(RDR))

    (kw,) = cat.graph_kwargs
    assert kw["scope_filter"] == {"collections": [RDR]}
    assert cat.registry_calls == 0


def test_a_subtree_scope_sends_the_tumbler_prefix() -> None:
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges)
    out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("1.2.20"))

    (kw,) = cat.graph_kwargs
    assert kw["scope_filter"] == {"subtree": "1.2.20"}
    assert cat.registry_calls == 0
    assert out["traversal"]["scope_prefilter"] == "applied"


@pytest.mark.parametrize("policy", [None, _policy(""), _policy("all")])
def test_no_scope_sends_no_predicate(policy) -> None:
    nodes, edges = _crowded()
    cat = _Engine(nodes[:20], edges[:19])
    kw = {"policy": policy} if policy is not None else {}
    out = _run(cat, seeds=[SEED], link_types=["relates"], **kw)

    assert "scope_filter" not in cat.graph_kwargs[0]
    assert cat.registry_calls == 0
    if policy is not None:
        assert out["traversal"]["scope_prefilter"] == "not_scoped"


def test_a_walk_still_at_the_cap_inside_the_scope_is_truncated_not_contained() -> None:
    nodes = [_node(SEED, RDR)] + [_node(f"1.2.10.{i}", RDR) for i in range(600)]
    edges = [_edge(n.tumbler, SEED) for n in nodes[1:]]
    out = _run(_Engine(nodes, edges), seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))

    rep = out["traversal"]
    assert rep["scope_prefilter"] == "applied"
    assert rep["possibly_truncated"] is True and rep["contained"] is False
    assert rep["secondary_omitted"] is False, "the engine filtered before it cut: what came back is in scope"
    assert len(out["tumblers"]) > 1


# ── the old engine, and a reply without the marker ──────────────────────────


def test_an_old_engine_gives_a_disclosed_incomplete_traversal_and_no_secondary_evidence() -> None:
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges, advertise=False)
    out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))

    assert "scope_filter" not in cat.graph_kwargs[0], "an engine that did not advertise the flag is never sent the key"
    rep = out["traversal"]
    assert rep["scope_prefilter"] == "unavailable"
    assert rep["possibly_truncated"] is True and rep["contained"] is False
    assert rep["secondary_omitted"] is True
    assert out["tumblers"] == [SEED], "only the seed: it is the document the walk starts from"
    assert "secondary graph evidence omitted" in out["warning"]


def test_an_engine_that_advertises_the_flag_but_returns_no_marker_is_treated_as_old() -> None:
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges, advertise=True, honour=False)
    out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))

    assert "scope_filter" in cat.graph_kwargs[0], "the client did send it"
    rep = out["traversal"]
    assert rep["scope_prefilter"] == "unavailable", "a 200 without the marker is no evidence the predicate ran"
    assert rep["secondary_omitted"] is True and rep["contained"] is False
    assert out["tumblers"] == [SEED]


def test_an_old_engine_and_an_uncut_walk_keep_the_secondary_evidence_it_always_had() -> None:
    nodes = [_node(SEED, RDR)] + [_node(f"1.2.20.{i}", RDR) for i in range(5)] + [_node(f"1.2.10.{i}", CODE) for i in range(5)]
    edges = [_edge(n.tumbler, SEED) for n in nodes[1:]]
    out = _run(_Engine(nodes, edges, advertise=False), seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))

    rep = out["traversal"]
    assert rep["scope_prefilter"] == "unavailable"
    assert rep["possibly_truncated"] is False and rep["contained"] is True, "a complete walk, exactly filtered by the client"
    assert rep["secondary_omitted"] is False
    assert {f"1.2.20.{i}" for i in range(5)} <= set(out["tumblers"])
    assert not any(t.startswith("1.2.10.") for t in out["tumblers"])


def test_a_registry_that_cannot_be_read_falls_back_to_the_old_engine_path() -> None:
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges, registry_error=RuntimeError("catalog down"))
    out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))

    assert "scope_filter" not in cat.graph_kwargs[0], "no predicate we could not build is sent"
    rep = out["traversal"]
    assert rep["scope_prefilter"] == "unavailable" and rep["secondary_omitted"] is True and rep["contained"] is False


def _clock(start: float = 1000.0):
    """A hand-set monotonic clock for the registry cache: ``clock.now`` is read, ``clock.now += n`` advances."""
    return SimpleNamespace(now=start)


def _with_clock(clock):
    return patch("nexus.mcp.core._scope_registry_clock", lambda: clock.now)


def test_a_multi_step_run_lists_the_registry_once() -> None:
    """nexus-jypmj.19: every corpus-prefix scoped traverse step used to list every collection. Steps of one run
    share one listing, across prefixes too, since the cached view is the live registry and not one scope's slice."""
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges, registry=[RDR, CODE])
    clock = _clock()
    with _with_clock(clock):
        for scope in ("rdr", "rdr", "code", "rdr"):
            out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy(scope))
            assert out["traversal"]["scope_prefilter"] == "applied"
    assert cat.registry_calls == 1, "four scoped steps, one registry read"
    assert [kw["scope_filter"] for kw in cat.graph_kwargs] == [
        {"collections": [RDR]}, {"collections": [RDR]}, {"collections": [CODE]}, {"collections": [RDR]},
    ]


def test_the_cached_registry_is_not_served_past_its_bound() -> None:
    from nexus.mcp.core import _SCOPE_REGISTRY_TTL_S  # noqa: PLC0415 — deferred, like this file's other imports of nexus.mcp.core

    nodes, edges = _crowded()
    cat = _Engine(nodes, edges, registry=[RDR])
    clock = _clock()
    new_rdr = "rdr__1-9__bge-base-en-v15-768__v1"
    with _with_clock(clock):
        _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
        cat._registry = [RDR, new_rdr]  # a collection registered while the cache is warm
        clock.now += _SCOPE_REGISTRY_TTL_S - 1
        _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
        assert cat.registry_calls == 1 and cat.graph_kwargs[-1]["scope_filter"] == {"collections": [RDR]}
        clock.now += 1  # the bound is reached
        _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    assert cat.registry_calls == 2, "the entry expired at the TTL and the registry was read again"
    assert cat.graph_kwargs[-1]["scope_filter"] == {"collections": sorted([RDR, new_rdr])}


def test_a_failed_registry_read_is_not_cached() -> None:
    nodes, edges = _crowded()
    cat = _Engine(nodes, edges, registry=[RDR], registry_error=RuntimeError("catalog down"))
    clock = _clock()
    with _with_clock(clock):
        _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
        assert "scope_filter" not in cat.graph_kwargs[0]
        cat._registry_error = None
        out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    assert cat.registry_calls == 2, "the failure left nothing behind to serve"
    assert cat.graph_kwargs[1]["scope_filter"] == {"collections": [RDR]}
    assert out["traversal"]["scope_prefilter"] == "applied"


def test_two_catalog_handles_do_not_share_a_cached_registry() -> None:
    nodes, edges = _crowded()
    a = _Engine(nodes, edges, registry=[RDR])
    b = _Engine(nodes, edges, registry=[RDR, "rdr__1-9__bge-base-en-v15-768__v1"])
    clock = _clock()
    with _with_clock(clock):
        _run(a, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
        _run(b, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    assert (a.registry_calls, b.registry_calls) == (1, 1)
    assert b.graph_kwargs[0]["scope_filter"] == {"collections": sorted(b._registry)}


def test_a_scope_naming_more_collections_than_the_engine_accepts_is_not_sent() -> None:
    from nexus.plans.traversal_policy import MAX_SCOPE_FILTER_COLLECTIONS

    nodes, edges = _crowded()
    registry = [f"rdr__1-{i}__bge-base-en-v15-768__v1" for i in range(MAX_SCOPE_FILTER_COLLECTIONS + 1)]
    cat = _Engine(nodes, edges, registry=registry)
    out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))

    assert "scope_filter" not in cat.graph_kwargs[0]
    assert out["traversal"]["scope_prefilter"] == "unavailable"


def test_a_catalog_with_no_capability_probe_is_an_old_engine() -> None:
    """The older catalog handles (and MagicMock-based test doubles) have no probe: never sent the key."""
    nodes, edges = _crowded()
    cat = MagicMock()
    cat.graph.return_value = {"nodes": nodes[:3], "edges": edges[:2]}
    cat.get_manifests.return_value = {}
    out = _run(cat, seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))

    assert "scope_filter" not in cat.graph.call_args.kwargs
    assert out["traversal"]["scope_prefilter"] == "unavailable"


# ── the wire contract ───────────────────────────────────────────────────────


def test_the_client_collection_bound_equals_the_engine_constant() -> None:
    from nexus.plans.traversal_policy import MAX_SCOPE_FILTER_COLLECTIONS

    java = (REPO / "service/src/main/java/dev/nexus/service/db/GraphScopeFilter.java").read_text()
    (declared,) = re.findall(r"public static final int MAX_COLLECTIONS = (\d+);", java)
    assert MAX_SCOPE_FILTER_COLLECTIONS == int(declared)


def test_the_diagnostics_summary_carries_the_prefilter_state() -> None:
    from nexus.plans.evidence_diagnostics import _traversal_summary

    summary = _traversal_summary(2, {"scope_prefilter": "unavailable", "secondary_omitted": True, "engine_dropped_out_of_scope": 0})
    assert summary["scope_prefilter"] == "unavailable" and summary["secondary_omitted"] is True


# ── HttpCatalogClient: the HTTP half ────────────────────────────────────────


def _client(handler):
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return cat


_EMPTY = {"nodes": [], "edges": []}


def test_graph_sends_the_predicate_and_keeps_the_marker() -> None:
    import json

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={**_EMPTY, "scope_filter": {"applied": True, "dropped_out_of_scope": 3}})

    cat = _client(handler)
    got = cat.graph_many(["1.2.1", "1.2.2"], scope_filter={"collections": [RDR]})
    assert seen[0]["scope_filter"] == {"collections": [RDR]}
    assert got["scope_filter"] == {"applied": True, "dropped_out_of_scope": 3}
    got = cat.graph("1.2.1", scope_filter={"subtree": "1.2"})
    assert seen[1]["scope_filter"] == {"subtree": "1.2"}


def test_graph_without_a_predicate_is_the_request_it_always_was() -> None:
    import json

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_EMPTY)

    cat = _client(handler)
    got = cat.graph("1.2.1", depth=2)
    assert "scope_filter" not in seen[0]
    assert "scope_filter" not in got, "no marker is invented for a request that carried no predicate"


def test_the_capability_is_read_from_version_strictly_and_cached() -> None:
    bodies = iter([
        {"catalog_graph_scope_filter_supported": True},
        {"catalog_graph_scope_filter_supported": "true"},
        {},
    ])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=next(bodies))

    cat = _client(handler)
    assert cat.supports_graph_scope_filter() is True
    assert cat.supports_graph_scope_filter() is True
    assert calls == ["/version"], "one probe serves repeated reads"

    now = [0.0]
    cat2 = _client(handler)
    cat2._capability_clock = lambda: now[0]
    assert cat2.supports_graph_scope_filter() is False, "a non-boolean true is not true"
    now[0] += cat2._CAPABILITY_TTL_S + 1
    assert cat2.supports_graph_scope_filter() is False, "re-read after the TTL: the flag is gone"
    assert calls == ["/version", "/version", "/version"]


def test_an_unreachable_version_probe_reads_unsupported_and_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    assert _client(handler).supports_graph_scope_filter() is False
