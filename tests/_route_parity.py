# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tu8wp.3: the route-parity measurement, separated from its live gate.

``tests/test_search_fanout_recall_parity.py`` runs these against the live
cloud engine. Everything here is plain code over an injected client, so the
parts that decide whether a measurement MEANS anything (the "was the route
actually served" guard, the noise-aware verdict, the per-leg query markers)
are tested hermetically in ``tests/test_route_parity_helpers.py`` against a
fake transport, with no cloud and no credentials.

What is compared. Candidate: ``search_cross_corpus`` over a client that
reaches ``POST /v1/vectors/search-per-collection`` (the route). References:

- the BATCHED path: the same ``search_cross_corpus`` over a client with
  ``supports_per_collection_search = False`` and the deep floor
  (``deep_candidates=True``), which is the pre-abdp2 sizing; measured twice
  per query, so its own run-to-run noise (nexus-e9sux) sits beside it;
- the pre-batching one-call-per-collection fan-out of ``ab837d219``,
  loaded standalone from git (what the route reproduces by construction).

Both pages are compared on what a user reads: the FINAL page after
``apply_ranking_boosts`` and, with rerank, the rerank-score sort and the file
diversity cap, cut to ten; and, with rerank off, the raw top ten by distance.
"""
from __future__ import annotations

import datetime as _dt
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The per-query floor a route page is held to when nothing says the reference
#: itself cannot reach it (nexus-abdp2 / nexus-atylb: 0.9).
JACCARD_FLOOR = 0.9
#: The aggregate slack against the reference's own noise (the same margin
#: ``test_final_order_parity_old_vs_new_floor`` uses).
NOISE_MARGIN = 0.05
PAGE = 10

#: One marker per leg. Appended to every query of that leg so a request in the
#: engine's, the edge's or the gateway's logs can be attributed to a leg (and,
#: for the rerank sweep, to a ``limit``). The marker changes the embedded text,
#: so absolute rows differ from an unmarked run of the same base query; it never
#: changes the pairing, because every side of one comparison carries the same
#: string. ``NX_TU8WP3_NO_MARKERS=1`` drops them for an apples-to-apples read
#: against the unmarked nexus-abdp2 numbers.
LEG_MARKERS: dict[str, str] = {
    "norerank": "tu8wp3-nr",
    "rr1000": "tu8wp3-r1000",
    "rr300": "tu8wp3-r300",
}

#: The route's rerank ``limit`` per sweep leg: ``None`` is the shipped sizing
#: (``_per_collection_request_sizes`` sends 1000 when rerank is on; a hermetic
#: test pins that number so this table cannot drift from the code), an integer
#: patches it.
LEG_RERANK_LIMIT: dict[str, int | None] = {"norerank": None, "rr1000": None, "rr300": 300}


def jaccard(a: set | frozenset, b: set | frozenset) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def mark_queries(
    base: Sequence[tuple[str, str]], marker: str | None,
) -> list[tuple[str, str]]:
    """``[(query + marker, corpus)]``; no marker leaves the queries as they are."""
    if not marker:
        return list(base)
    return [(f"{q} ({marker})", corpus) for q, corpus in base]


def raw_top_ids(results: Sequence[Any], limit: int = PAGE) -> list[str]:
    """The *limit* nearest ids by raw vector distance, nearest first."""
    return [r.id for r in sorted(results, key=lambda r: (r.distance, r.id))[:limit]]


class RouteNotServedError(AssertionError):
    """The candidate side of a route-parity measurement never reached the
    route (or silently fell back to the batched path for some group), or the
    reference side never reached ``/search``: the comparison would be a path
    against itself and pass vacuously. A subclass of ``AssertionError`` so it
    fails a test loudly."""


@dataclass
class CallLog:
    """What one client did during one search: wraps ``search`` and
    ``search_per_collection`` on the instance, restores them on exit."""

    client: Any
    search_calls: int = 0
    route_calls: int = 0
    route_served: int = 0
    route_none: int = 0
    #: ``(per_collection_k, limit, n_collections)`` per route call
    route_shapes: list[tuple[int, int, int]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _orig: dict[str, Any] = field(default_factory=dict, repr=False)

    def __enter__(self) -> CallLog:
        c = self.client
        for name in ("search", "search_per_collection"):
            self._orig[name] = getattr(c, name, None)
        if self._orig["search"] is not None:
            real_search = self._orig["search"]

            def _search(*a, **kw):
                with self._lock:
                    self.search_calls += 1
                return real_search(*a, **kw)

            c.search = _search
        if self._orig["search_per_collection"] is not None:
            real_route = self._orig["search_per_collection"]

            def _route(query, collection_names, **kw):
                with self._lock:
                    self.route_calls += 1
                    self.route_shapes.append((
                        kw.get("per_collection_k", -1), kw.get("limit", -1),
                        len(collection_names),
                    ))
                out = real_route(query, collection_names, **kw)
                with self._lock:
                    if out is None:
                        self.route_none += 1
                    else:
                        self.route_served += 1
                return out

            c.search_per_collection = _route
        return self

    def __exit__(self, *exc) -> None:
        for name, orig in self._orig.items():
            if orig is not None:
                # remove the instance override so the class method is visible again
                try:
                    delattr(self.client, name)
                except AttributeError:
                    pass


def sized_with_rerank_limit(real: Callable, limit: int | None) -> Callable:
    """A replacement for ``search_engine._per_collection_request_sizes`` that
    sends *limit* as the global cut when rerank is on (the sweep), and the real
    sizing otherwise. ``None`` returns *real* itself (the shipped sizing)."""
    if limit is None:
        return real

    def _sized(n_results: int, mult: int, *, rerank: bool) -> tuple[int, int]:
        per_k, lim = real(n_results, mult, rerank=rerank)
        return (per_k, limit) if rerank else (per_k, lim)

    return _sized


def estimate_batched_calls(cols: Sequence[str], n: int, *, deep: bool = True) -> tuple[int, int]:
    """``(route requests, batched /search requests)`` one search over *cols*
    sends, from the client's own planner: one route request per embedding-model
    group; the batched count follows ``_desired_candidate_count`` and the
    service cap. An estimate (a poisoned collection or a split retry changes
    the real count, which the table prints)."""
    from nexus.db.limits import QUOTAS  # noqa: PLC0415
    from nexus.search_engine import (  # noqa: PLC0415
        _desired_candidate_count,
        _group_collections_by_embedding_model,
    )

    groups = _group_collections_by_embedding_model(list(cols))
    batched = 0
    for g in groups:
        desired = _desired_candidate_count(g, n, deep=deep)
        cap = QUOTAS.MAX_QUERY_RESULTS
        batched += min(len(g), -(-desired // cap)) if desired > cap and len(g) > 1 else 1
    return len(groups), batched


def expected_cell_calls(
    queries: Sequence[tuple[str, str]], cols_of: dict[str, list[str]], n: int, *, loader: bool,
) -> str:
    """The line a cell prints before it sends anything: the requests it will
    make, in all. Per query: two batched reference runs, one route run and,
    only when the opt-in loader leg is on, one /search per collection."""
    route_total = batched_total = loader_total = 0
    for _q, corpus in queries:
        cols = cols_of[corpus]
        route_per, batched_per = estimate_batched_calls(cols, n)
        route_total += route_per
        batched_total += 2 * batched_per
        loader_total += len(cols) if loader else 0
    total = route_total + batched_total + loader_total
    return (
        f"EXPECTED REQUESTS (estimate) {total} for {len(queries)} queries: "
        f"{route_total} route + {batched_total} batched /search (reference run twice per query)"
        + (f" + {loader_total} pre-batching per-collection /search (opt-in loader)" if loader
           else " + 0 pre-batching per-collection (loader off; NX_TU8WP3_LOADER=1 turns it on)")
    )


def _utc() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class QueryRow:
    query: str
    corpus: str
    #: final-page Jaccards: route vs first reference run, vs second, and the
    #: reference's own run-to-run noise
    page_route_ref1: float
    page_route_ref2: float
    page_noise: float
    #: raw top-10-by-distance Jaccards (the same three), reported always,
    #: asserted only with rerank off
    raw_route_ref1: float
    raw_route_ref2: float
    raw_noise: float
    #: route vs the ab837d219 loader and that loader vs the batched reference;
    #: ``None`` when the loader leg was skipped
    page_route_loader: float | None
    page_ref_loader: float | None
    route_pool: int
    ref_pool: int
    route_calls: int
    route_shapes: list[tuple[int, int, int]]
    ref_search_calls: int
    loader_search_calls: int | None
    start_utc: str
    end_utc: str

    @property
    def page_route(self) -> float:
        return (self.page_route_ref1 + self.page_route_ref2) / 2

    @property
    def raw_route(self) -> float:
        return (self.raw_route_ref1 + self.raw_route_ref2) / 2


def measure_query(
    *,
    query: str,
    corpus: str,
    cols: list[str],
    n: int,
    threshold: float | None,
    rerank: bool,
    route_client: Any,
    ref_client: Any,
    search: Callable[..., list],
    user_page: Callable[..., list[str]],
    loader: Callable[..., list] | None = None,
    catalog: Any = None,
) -> QueryRow:
    """One query: reference, route, loader, reference again, interleaved so
    cloud drift hits all of them alike. Raises :class:`RouteNotServedError`
    when the route was not served for every model group or the reference never
    called ``/search``.

    *search* is ``search_cross_corpus`` (injected so a hermetic test can use a
    stand-in), *user_page* the final-page pipeline, *loader* the ab837d219
    ``search_cross_corpus`` (or ``None`` to skip that leg).
    """
    start = _utc()
    kw: dict[str, Any] = dict(
        cluster_by=None, threshold_override=threshold, rerank=rerank,
        catalog=catalog, deep_candidates=True,
    )

    def _run(fn, client):
        with CallLog(client) as log:
            out = fn(query, cols, n, client, **kw)
        return out, log

    # Each side is checked the moment it has run, so a run that cannot mean
    # anything stops after one query's worth of traffic, not a cell's.
    ref1, ref1_log = _run(search, ref_client)
    _require_batched_reference("first", query, ref1_log)
    route, route_log = _run(search, route_client)
    _require_route_served(query, route_log)
    loader_out = loader_log = None
    if loader is not None:
        lkw = {k: v for k, v in kw.items() if k not in ("deep_candidates", "catalog")}
        with CallLog(ref_client) as loader_log:
            loader_out = loader(query, cols, n, ref_client, **lkw)
    ref2, ref2_log = _run(search, ref_client)
    _require_batched_reference("second", query, ref2_log)

    p_ref1 = user_page(ref1, rerank=rerank)
    p_route = user_page(route, rerank=rerank)
    p_ref2 = user_page(ref2, rerank=rerank)
    r_ref1, r_route, r_ref2 = raw_top_ids(ref1), raw_top_ids(route), raw_top_ids(ref2)
    p_loader = None if loader_out is None else user_page(loader_out, rerank=rerank)
    return QueryRow(
        query=query, corpus=corpus,
        page_route_ref1=jaccard(set(p_route), set(p_ref1)),
        page_route_ref2=jaccard(set(p_route), set(p_ref2)),
        page_noise=jaccard(set(p_ref2), set(p_ref1)),
        raw_route_ref1=jaccard(set(r_route), set(r_ref1)),
        raw_route_ref2=jaccard(set(r_route), set(r_ref2)),
        raw_noise=jaccard(set(r_ref2), set(r_ref1)),
        page_route_loader=None if p_loader is None else jaccard(set(p_route), set(p_loader)),
        page_ref_loader=None if p_loader is None else jaccard(set(p_ref1), set(p_loader)),
        route_pool=len(route), ref_pool=len(ref1),
        route_calls=route_log.route_calls, route_shapes=list(route_log.route_shapes),
        ref_search_calls=ref1_log.search_calls,
        loader_search_calls=None if loader_log is None else loader_log.search_calls,
        start_utc=start, end_utc=_utc(),
    )


def _require_route_served(query: str, route_log: CallLog) -> None:
    if route_log.route_served < 1:
        raise RouteNotServedError(
            f"route NOT served for {query!r}: search_per_collection was called "
            f"{route_log.route_calls} time(s) and returned None {route_log.route_none} "
            "time(s); the candidate ran the batched fallback, so this comparison would "
            "be the batched path against itself (a 404, an edge refusal, or an engine "
            "that predates engine-service-v0.1.147)",
        )
    if route_log.route_none > 0 or route_log.search_calls > 0:
        raise RouteNotServedError(
            f"route only partly served for {query!r}: {route_log.route_served} group(s) "
            f"answered, {route_log.route_none} returned None, and the candidate also made "
            f"{route_log.search_calls} /search call(s): part of the candidate page came "
            "from the batched fallback",
        )


def _require_batched_reference(label: str, query: str, log: CallLog) -> None:
    if log.search_calls < 1:
        raise RouteNotServedError(
            f"the {label} reference run made no /search call for {query!r}: it did "
            "not exercise the batched path it stands for",
        )
    if log.route_calls:
        raise RouteNotServedError(
            f"the {label} reference run called the per-collection route "
            f"{log.route_calls} time(s) for {query!r}: it is not a batched reference",
        )


def query_holds(route_j: float, noise_j: float, floor: float = JACCARD_FLOOR) -> bool:
    """A route page holds when it reaches *floor*, or when it is as close to
    the reference as the reference is to itself: the reference scores 0.818
    against itself on two queries (nexus-e9sux), and a route cannot be held
    above what its reference achieves. A route page below both is a loss."""
    return route_j >= floor or route_j >= noise_j - 1e-9


def cell_failures(rows: Sequence[QueryRow], *, rerank: bool, assert_rerank: bool) -> list[str]:
    """The reasons a cell fails, empty when it holds.

    Rerank off: every query's final page AND raw top-10 must hold
    (:func:`query_holds`), and so must the mean, against the mean noise.
    Rerank on: reported, not asserted (the reranked pool legitimately differs:
    the route reranks the merged top-limit of each model group once, the
    batched path reranked each batch), unless *assert_rerank*.
    """
    problems: list[str] = []
    if not rows:
        return ["vacuous cell: no query was measured"]
    if rerank and not assert_rerank:
        return problems
    metrics = [("page", lambda r: r.page_route, lambda r: r.page_noise)]
    if not rerank:
        metrics.append(("raw", lambda r: r.raw_route, lambda r: r.raw_noise))
    for name, route_of, noise_of in metrics:
        for r in rows:
            if not query_holds(route_of(r), noise_of(r)):
                problems.append(
                    f"{name} page of {r.query!r}: route-vs-reference {route_of(r):.3f} is below "
                    f"both {JACCARD_FLOOR} and the reference's own noise {noise_of(r):.3f}",
                )
        mean_route = sum(route_of(r) for r in rows) / len(rows)
        mean_noise = sum(noise_of(r) for r in rows) / len(rows)
        if mean_route < min(JACCARD_FLOOR, mean_noise) - NOISE_MARGIN:
            problems.append(
                f"mean {name} Jaccard {mean_route:.3f} is more than {NOISE_MARGIN} under "
                f"min({JACCARD_FLOOR}, reference noise {mean_noise:.3f})",
            )
    return problems


def _mean(values: Sequence[float | None]) -> float:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else math.nan


def format_cell(label: str, rows: Sequence[QueryRow]) -> str:
    """The cell's table: one line per query and a MEAN line, plus the UTC
    window of the cell so engine log lines can be attributed to it."""

    def _f(v: float | None) -> str:
        return "   n/a" if v is None else f"{v:>6.3f}"

    lines = [
        f"\nnexus-tu8wp.3 route parity, {label}",
        f"window utc {rows[0].start_utc if rows else '-'} .. {rows[-1].end_utc if rows else '-'}",
        f"{'corpus':<10} {'pg route':>8} {'pg noise':>8} {'raw route':>9} {'raw noise':>9} "
        f"{'route/loader':>12} {'ref/loader':>10} {'pool r/b':>9} {'rt calls':>8} {'ref /search':>11}  query",
    ]
    for r in rows:
        lines.append(
            f"{r.corpus:<10} {_f(r.page_route):>8} {_f(r.page_noise):>8} {_f(r.raw_route):>9} "
            f"{_f(r.raw_noise):>9} {_f(r.page_route_loader):>12} {_f(r.page_ref_loader):>10} "
            f"{r.route_pool:>4}/{r.ref_pool:<4} {r.route_calls:>8} {r.ref_search_calls:>11}  {r.query!r}",
        )
    lines.append(
        f"MEAN {label}: page route={_mean([r.page_route for r in rows]):.3f} "
        f"noise={_mean([r.page_noise for r in rows]):.3f} | raw route={_mean([r.raw_route for r in rows]):.3f} "
        f"noise={_mean([r.raw_noise for r in rows]):.3f} | route/loader={_mean([r.page_route_loader for r in rows]):.3f} "
        f"ref/loader={_mean([r.page_ref_loader for r in rows]):.3f}",
    )
    if rows:
        lines.append(f"WINDOW utc {rows[0].start_utc} .. {rows[-1].end_utc}  ({label})")
    return "\n".join(lines)
