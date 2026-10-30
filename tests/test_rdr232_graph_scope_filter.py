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

    def __init__(self, nodes, edges, *, advertise=True, honour=True, registry=None, registry_error=None, walk=False):
        self._walk_fields = walk  # nexus-jypmj.18: advertises ``catalog_graph_node_walk_supported`` and sends ``node_walk``
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

    def supports_graph_node_walk(self) -> bool:
        return self._walk_fields

    def _node_walk(self, seeds, direction, keep_ids) -> dict:
        """Depth and best role of every returned node, over EVERY edge (the removed relays included): the
        engine's ``node_walk``, by the rule ``traversal_policy._distance_and_role`` applies to a complete edge set."""
        from nexus.plans.traversal_policy import _ROLE_RANK, link_role

        depth = {str(x): 0 for x in seeds}
        role: dict[str, str] = {}
        frontier = list(depth)
        while frontier:
            found: dict[str, str] = {}
            for e in self._edges:
                for here, there in ((e.from_tumbler, e.to_tumbler), (e.to_tumbler, e.from_tumbler)):
                    if (here, there) == (e.to_tumbler, e.from_tumbler) and direction == "out":
                        continue
                    if (here, there) == (e.from_tumbler, e.to_tumbler) and direction == "in":
                        continue
                    if here in frontier and there not in depth:
                        r = link_role(e.link_type)
                        if there not in found or _ROLE_RANK[r] < _ROLE_RANK[found[there]]:
                            found[there] = r
            for there, r in found.items():
                depth[there] = depth[frontier[0]] + 1
                role[there] = r
            frontier = list(found)
        return {
            t: ({"depth": d} if t not in role else {"depth": d, "role": role[t]})
            for t, d in depth.items() if t in keep_ids
        }

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
        if flt is not None and self._honour and self._walk_fields:
            reply["node_walk"] = self._node_walk(seeds, kw.get("direction", "both"), keep_ids)
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


@pytest.fixture()
def real_handle_factory(monkeypatch):
    """The production path: ``_get_catalog()`` -> ``make_catalog_reader()`` builds a NEW ``_SharedServiceCatalogHandle``
    on every call, over one shared client. The slot is the only seam replaced, by one that holds the test's engine."""
    from nexus.catalog import factory
    from nexus.service_handles import SharedClientSlot

    monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
    holder: dict = {}
    slot = SharedClientSlot(lambda: holder["engine"], lambda _client: None)
    monkeypatch.setattr(factory, "_default_catalog_slot", slot)

    def install(engine):
        holder["engine"] = engine
        return engine

    return install


def _run_real(**kw):
    """``traverse`` with nothing patched: the handle comes from the real factory path."""
    from nexus.mcp.core import traverse

    return traverse(**kw)


def test_the_real_factory_hands_a_new_handle_per_call(real_handle_factory) -> None:
    """The premise of the run-scoped listing: a cache keyed on the catalog handle can never hit in production."""
    from nexus.mcp_infra import get_catalog

    real_handle_factory(_Engine(*_crowded()))
    assert get_catalog() is not get_catalog()


def test_a_multi_step_run_lists_the_registry_once(real_handle_factory) -> None:
    """nexus-jypmj.19: every corpus-prefix scoped traverse step used to list every collection. Steps of one run
    share one listing, across prefixes too, since the shared view is the live registry and not one scope's slice."""
    from nexus.plans.traversal_policy import registry_listing_scope

    cat = real_handle_factory(_Engine(*_crowded(), registry=[RDR, CODE]))
    with registry_listing_scope():
        for scope in ("rdr", "rdr", "code", "rdr"):
            out = _run_real(seeds=[SEED], link_types=["relates"], policy=_policy(scope))
            assert out["traversal"]["scope_prefilter"] == "applied"
    assert cat.registry_calls == 1, "four scoped steps, one registry read"
    assert [kw["scope_filter"] for kw in cat.graph_kwargs] == [
        {"collections": [RDR]}, {"collections": [RDR]}, {"collections": [CODE]}, {"collections": [RDR]},
    ]


def test_a_second_run_lists_again_and_sees_a_collection_registered_between_the_runs(real_handle_factory) -> None:
    from nexus.plans.traversal_policy import registry_listing_scope

    new_rdr = "rdr__1-9__bge-base-en-v15-768__v1"
    cat = real_handle_factory(_Engine(*_crowded(), registry=[RDR]))
    with registry_listing_scope():
        _run_real(seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    cat._registry = [RDR, new_rdr]  # an index or store_put registers it after the first run
    with registry_listing_scope():
        _run_real(seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    assert cat.registry_calls == 2
    assert cat.graph_kwargs[0]["scope_filter"] == {"collections": [RDR]}
    assert cat.graph_kwargs[1]["scope_filter"] == {"collections": sorted([RDR, new_rdr])}, "no staleness window"


def test_a_traverse_outside_a_run_reads_the_registry_each_time(real_handle_factory) -> None:
    cat = real_handle_factory(_Engine(*_crowded(), registry=[RDR]))
    for _ in range(2):
        _run_real(seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    assert cat.registry_calls == 2, "nothing is kept past a call that belongs to no run"


def test_a_failed_registry_read_is_not_kept_within_a_run(real_handle_factory) -> None:
    from nexus.plans.traversal_policy import registry_listing_scope

    cat = real_handle_factory(_Engine(*_crowded(), registry=[RDR], registry_error=RuntimeError("catalog down")))
    with registry_listing_scope():
        _run_real(seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
        assert "scope_filter" not in cat.graph_kwargs[0]
        cat._registry_error = None
        out = _run_real(seeds=[SEED], link_types=["relates"], policy=_policy("rdr"))
    assert cat.registry_calls == 2, "the failure left nothing behind to serve"
    assert cat.graph_kwargs[1]["scope_filter"] == {"collections": [RDR]}
    assert out["traversal"]["scope_prefilter"] == "applied"


def test_plan_run_opens_one_registry_listing_per_run() -> None:
    """The seam: ``plan_run`` opens the scope, so every step of a run sees the same listing and a second run a new one."""
    import asyncio
    import json

    from nexus.plans.match import Match
    from nexus.plans.runner import plan_run
    from nexus.plans.traversal_policy import active_registry_listing

    seen: list = []

    async def dispatcher(tool: str, args: dict) -> dict:
        seen.append(active_registry_listing())
        return {"ids": [], "tumblers": [], "collections": []}

    plan = {"steps": [{"tool": "traverse", "args": {"seeds": []}}, {"tool": "traverse", "args": {"seeds": []}}]}
    match = Match(
        plan_id=7, name="fixture", description="fixture", confidence=0.9, dimensions={"verb": "test"}, tags="",
        plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[], default_bindings={}, parent_dims=None,
    )
    for _ in range(2):
        asyncio.run(plan_run(match, {}, dispatcher=dispatcher, bundle_operators=False))
    assert len(seen) == 4 and all(listing is not None for listing in seen)
    assert seen[0] is seen[1] and seen[2] is seen[3], "the steps of one run share a listing"
    assert seen[0] is not seen[2], "a second run opens a new one"
    assert active_registry_listing() is None, "nothing leaks out of the run"


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


# ── nexus-jypmj.18: a node behind a removed relay keeps its depth and role ──────────────────────────────


def _relay_walk():
    """S (rdr, the seed) -implements-> R (code, out of scope) -implements-> B (rdr), and A (rdr) -implements-> S.

    The engine's predicate removes R and with it the R -> B edge, so B arrives with no edge at all; A keeps
    its edge. Under a single-role policy the client scores B by what is left.
    """
    nodes = [_node(SEED, RDR), _node("1.2.2", RDR), _node("1.2.3", RDR), _node("1.1.9", CODE)]
    edges = [_edge("1.2.2", SEED, "implements"), _edge(SEED, "1.1.9", "implements"), _edge("1.1.9", "1.2.3", "implements")]
    return nodes, edges


def _relay_run(cat, role: str):
    return _run(cat, seeds=[SEED], link_types=["implements"], policy=_policy("rdr", require_link_role=role, max_documents=50))


def test_a_single_role_policy_keeps_a_node_behind_a_removed_relay_when_the_engine_sends_node_walk() -> None:
    """The new engine: the predicate is sent, the relay is removed, B has no edge, and B survives on node_walk."""
    nodes, edges = _relay_walk()
    cat = _Engine(nodes, edges, walk=True)
    out = _relay_run(cat, "implementation")

    assert "scope_filter" in cat.graph_kwargs[0], "an engine that carries node_walk is sent the predicate"
    assert out["traversal"]["scope_prefilter"] == "applied"
    assert out["tumblers"] == [SEED, "1.2.2", "1.2.3"], "B (1.2.3) kept; A (distance 1) ranks before B (distance 2)"
    assert out["traversal"]["dropped_role"] == 0
    assert out["traversal"]["engine_dropped_out_of_scope"] == 1


def test_a_single_role_policy_on_an_engine_without_node_walk_walks_unfiltered_and_loses_nothing() -> None:
    """The stopgap, an engine of 0.1.168 or 0.1.169: it has the predicate but not node_walk."""
    nodes, edges = _relay_walk()
    cat = _Engine(nodes, edges, walk=False)
    out = _relay_run(cat, "implementation")

    assert "scope_filter" not in cat.graph_kwargs[0], "no predicate for a role-filtering policy without node_walk"
    assert out["traversal"]["scope_prefilter"] == "unavailable"
    assert out["tumblers"] == [SEED, "1.2.2", "1.2.3"], "narrowed client-side, over the relay's edges"
    assert out["traversal"]["dropped_role"] == 0
    assert out["traversal"]["dropped_out_of_scope"] == 1, "R is dropped by the client's own scope filter"


@pytest.mark.parametrize("role", ["factual", None])
def test_a_role_agnostic_policy_sends_the_predicate_whether_or_not_the_engine_sends_node_walk(role) -> None:
    nodes, edges = _relay_walk()
    for walk in (False, True):
        cat = _Engine(nodes, edges, walk=walk)
        over = {} if role is None else {"require_link_role": role}
        out = _run(cat, seeds=[SEED], link_types=["implements"], policy=_policy("rdr", max_documents=50, **over))
        assert "scope_filter" in cat.graph_kwargs[0], f"walk={walk}"
        assert out["traversal"]["scope_prefilter"] == "applied"
        assert "1.2.3" in out["tumblers"], "role 'factual' admits B whatever its role, ranked last"


def test_without_node_walk_in_the_reply_a_node_behind_a_removed_relay_is_dropped_as_before() -> None:
    """The control that pins the defect: the same new-shape request answered WITHOUT node_walk loses B."""
    nodes, edges = _relay_walk()
    cat = _Engine(nodes, edges, walk=True)
    cat._node_walk = lambda *a, **k: {}  # the predicate is sent (it advertises node_walk) but the reply carries nothing
    out = _relay_run(cat, "implementation")

    assert "1.2.3" not in out["tumblers"]
    assert out["traversal"]["dropped_role"] == 1


def test_node_walk_wins_over_the_edges_and_a_malformed_entry_is_ignored() -> None:
    from nexus.plans.traversal_policy import _distance_and_role

    edges = [_edge("a", "b", "relates"), _edge("b", "c", "relates")]
    dist, role = _distance_and_role(["a"], edges, "out")
    assert dist == {"a": 0, "b": 1, "c": 2} and role == {"b": "related", "c": "related"}

    walk = {
        "b": {"depth": 1, "role": "citation"},    # the engine saw a better role than the edges show
        "c": {"depth": True, "role": "citation"},  # a bool is not a depth
        "d": {"depth": 2, "role": "implementation"},  # no edge reaches d
        "e": {"depth": 3, "role": "sideways"},    # an unknown role
        "f": {"depth": 0, "role": "citation"},    # depth 0 is a seed
        "a": {"depth": 4, "role": "citation"},    # a seed is never rescored
        "g": "nonsense",
    }
    dist, role = _distance_and_role(["a"], edges, "out", walk)
    assert dist == {"a": 0, "b": 1, "c": 2, "d": 2}
    assert role == {"b": "citation", "c": "related", "d": "implementation"}


def test_the_client_link_role_table_equals_the_engines() -> None:
    from nexus.plans.traversal_policy import _ROLE_OF_LINK_TYPE

    java = (REPO / "service/src/main/java/dev/nexus/service/db/CatalogRepository.java").read_text()
    body = java[java.index("public static String linkRole("):]
    body = body[: body.index("private static int roleRank")]
    engine: dict[str, str] = {}
    for labels, role in re.findall(r"case ((?:\"[a-z-]+\"(?:, )?)+) -> \"(\w+)\"", body):
        for label in re.findall(r"\"([a-z-]+)\"", labels):
            engine[label] = role
    assert engine == _ROLE_OF_LINK_TYPE, "CatalogRepository.linkRole and traversal_policy._ROLE_OF_LINK_TYPE drifted"


def test_the_client_reads_the_node_walk_capability_strictly() -> None:
    bodies = iter([{"catalog_graph_node_walk_supported": True}, {"catalog_graph_node_walk_supported": "true"}])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(bodies))

    assert _client(handler).supports_graph_node_walk() is True
    assert _client(handler).supports_graph_node_walk() is False, "a non-boolean true is not true"


def test_graph_keeps_the_node_walk_of_the_reply_and_invents_none() -> None:
    walk = {"1.2.3": {"depth": 2, "role": "implementation"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**_EMPTY, "scope_filter": {"applied": True}, "node_walk": walk})

    assert _client(handler).graph("1.2.1", scope_filter={"collections": [RDR]})["node_walk"] == walk

    def bare(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_EMPTY)

    assert "node_walk" not in _client(bare).graph("1.2.1")
