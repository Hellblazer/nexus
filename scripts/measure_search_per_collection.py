#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tu8wp.3: latency, call-count and concurrency measurement of the
per-collection search route against the batched path, on the live cloud.

READS ONLY. Every request is a vector search or a collection listing; nothing
is written, and no config, environment or credential is printed (the client
reads its own config; this script never opens it).

Three modes::

    uv run python scripts/measure_search_per_collection.py latency [--rounds 4] [--warmup 1] [--shapes mcp,cli] [--pause 1.0]
    uv run python scripts/measure_search_per_collection.py concurrency [--ks 1,2,3] [--include-5] [--shape mcp] [--pause 1.0]
    uv run python scripts/measure_search_per_collection.py lexical [--rounds 4] [--warmup 1] [--ks 3] [--pause 1.0]

``lexical`` (nexus-pq3wr): a default ``--lexical`` search (the MCP shape, n=30, no
rerank) through the route, interleaving rounds WITH the route's ``lexical`` field
(nexus-xd2rc: the lexical arms ride the request) and WITHOUT it (the field dropped,
so the engine echoes none and the client runs the leg on ``/hybrid-search``, the
path before xd2rc). Per run: wall time, route and ``/hybrid-search`` requests, and
every arm's ``lexical_error_kind`` (a ``statement_timeout`` or
``fanout_budget_exhausted`` arm is retried on ``/hybrid-search``). Then K concurrent
searches with the field (``--ks``, default 3). Engine-side timings (``fanout_ms``,
``sum_arm_ms``) are read from the engine's ``event=search_per_collection`` lines in
the printed windows.

LOAD IS BOUNDED (the nexus-abdp2 measurement runs, ~10k per-collection searches
in three hours, pushed the live engine's plain_search mean from ~230 ms to
516 ms and into the 30 s statement timeout): runs are sequential, with a pause
(``--pause``, default 1 s) between them; K defaults to 1, 2, 3 and 5 needs the
explicit ``--include-5``; the batched baseline is the only reference (no
per-collection fan-out); and the total expected request count is printed, with
the run's UTC window at the end, before anything is sent.

``latency``: for each shape, interleaved rounds of one default search through
the route (a client with ``supports_per_collection_search`` left True) and
through the batched path (False; develop's fallback, the nexus-abdp2 floors).
Every run gets a FRESH client, so the route's 404 memo and single-flight probe
start empty, and the order of the two paths alternates round by round so cloud
drift hits both alike. Untimed warm-up runs come first. Prints, per run, the
wall time, the number of ``/v1/vectors/search`` and route requests, and the row
count; per path the median wall time; and the UTC start and end of each shape's
window, to match against the engine's ``event=search_per_collection`` lines
(``fanout_ms``, ``slowest_arm_ms``, ``sum_arm_ms``, ``budget_exhausted``).

Shapes (the search itself, ``search_cross_corpus``, no catalog/taxonomy/telemetry: the
retrieval step), each over its surface's own DEFAULT corpora, read from where the surface
defines them (``search_page_parity.shape_corpora``):

- ``mcp``: the MCP ``search`` tool's default fetch, n=30, no rerank, over the default of its
  ``corpus`` parameter (``knowledge,code,docs``);
- ``cli``: ``nx search``'s default, n=10, server rerank on (a multi-collection
  search reranks), over the default of its ``--corpus`` option (``knowledge, code, docs``).

EARLIER RUNS SEARCHED FOUR CORPORA. Before this change every shape searched
``knowledge, code, docs, rdr`` (the 151 collections of the 2026-11-09 cloud leg), so those
runs included the ``rdr`` collections neither surface searches by default; they are not
comparable with a run of this version. The ``concurrency`` mode takes its shape's corpora
too. The ``lexical`` mode still searches the four.

Result parity (nexus-tu8wp.3, the row-gap finding): a route run caps each collection at
``per_collection_k`` (60 for code, 120 for the others), a batched run takes one flat LIMIT
per batch, so raw row counts differ (138 vs 165 on the cloud) while the page can match.
For each round's pair the latency leg also prints and records, from
``search_page_parity.py``: the rows each path KEPT per collection (``SearchDiagnostics``);
the overlap of the two pages, top-1 and top-3 agreement, and the route-only and batched-only
ids; and, for each batched-only page row, its rank inside its own collection on the batched
path against that collection's route k. A batched-only row ranked past k is one the route can
never return (the boost window is wider than the code k): any count above 0 is the recall
signal. The JSON summary per shape carries ``parity_summary`` (mean overlap, top-1 rate,
runs with a past-k row, the max) and ``parity`` (per pair, with each run's failed-collection
count).

THE PAGE. ``mcp`` is the MCP ``search`` tool's first page at its default ``limit=10``: the
search fetches 30 rows (``limit`` plus two lookahead pages), then ``apply_ranking_boosts``
(window ``boost_window_size(10)`` = 30, the config's ``hybrid_default``, ``get_tuning_config()``
and the search's own ``model_of_out``), ``apply_file_diversity_cap`` over the same window, and
the first 10. The tool inlines those calls, so ``boosted_page`` repeats them in order; the
page therefore depends on the operator's config and tuning, as the tool's does. ``cli`` reranks
on the server, and ``nx search`` orders by ``rerank_score`` with held-slot restoration: this
script does NOT reproduce that, so the ``cli`` parity lines say ``pre-rerank pool order`` (the
boosted, capped pool order, not the page a CLI user sees) and a pair where a rerank degraded in
either run is SKIPPED. A pair where either run had failed collections is EXCLUDED from the
summary (``N of M pairs excluded``); it still prints, and its counts stay in the JSON.

COLLECTION SET AND BYTES. The collections are ``resolve_corpus`` over the tenant's listing, not the
tool's ``_resolve_corpus_target`` (which also drops non-live collections and applies the thin-sibling
floor), and ``content_chars`` is not sent, so the absolute row counts and bytes can differ from the
tool's. The route-vs-batched comparison stays valid: both paths take the same collections and
parameters.

ROUTE k. The route k used is the one this script sends for the shape. The ``mcp`` shape passes
the MCP tool's own ``per_collection_k_cap`` (``nexus.mcp.core._MCP_PER_COLLECTION_K``, 40: route
k 40 for every collection at n=30; the batched path ignores it); ``cli`` stays uncapped (20 /
40 at n=10). MEASUREMENTS BEFORE THIS CHANGE (the 2026-11-09 cloud latency leg, the 138 vs 165
row counts) RAN THE ``mcp`` SHAPE UNCAPPED, with route k 60 / 120, so they are not comparable
with a run of this version. The ``concurrency`` mode takes the same cap for its shape, so ITS
route k changed too (it is resolved once, before the barrier). The ``lexical`` mode still sends
no cap.

``concurrency``: K concurrent default searches through the route (K in
``--ks``), each on its own fresh client, released together. Prints the wall
time of every search and, from the route's own envelopes and failures, every
``failed_collections`` entry with its ``error_kind`` (``fanout_budget_exhausted``,
``statement_timeout``, ...) and every whole-request failure with its HTTP
status (a 503 from pool or admission exhaustion).

A route run that did not reach the route (a 404, an edge refusal, an engine
that predates it) exits 3 instead of timing the batched fallback as the route.

The client's route is opt-in (``NX_SEARCH_PER_COLLECTION=1``, nexus-tu8wp.3). This script sets it
for exactly the duration of a route run (and the concurrency and lexical modes' searches) and
clears it for a batched run, then restores the caller's value; the caller's own setting never
decides which path a leg measures.

QUERY STRINGS PER LEG (distinct, so engine log lines are attributable): every
string below ends in a leg marker; the concurrency mode adds ``-k<K>-s<i>`` per
search.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import statistics
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import search_page_parity as parity
from search_page_parity import boosted_page, cols_for, make_route_k, shape_corpora, shape_k_cap

#: One base question; each leg appends its own marker (see the module docstring).
_BASE_QUERY = "What does the tuple_renew operation do to the attempts counter?"
LATENCY_MARKERS = {"mcp": "tu8wp3-lat-mcp", "cli": "tu8wp3-lat-cli"}
CONCURRENCY_MARKER = "tu8wp3-conc"
#: nexus-pq3wr's lexical-mode marker.
LEXICAL_MARKER = "pq3wr-lex"
#: The corpora of a default search (the MCP tool's default plus ``rdr``).
DEFAULT_CORPORA = ("knowledge", "code", "docs", "rdr")
#: ``shape -> (n_results, rerank)``.
#: ``shape -> (fetch n, rerank, page limit)``. ``mcp``: the MCP ``search`` tool's default call, limit 10,
#: so it fetches ``limit + 2 lookahead pages`` = 30 rows and shows the first 10 after the boosts and the
#: file-diversity cap. ``cli``: ``nx search``'s default, n=10 with server rerank.
SHAPES: dict[str, tuple[int, bool, int]] = {"mcp": (30, False, 10), "cli": (10, True, 10)}
EXIT_ROUTE_NOT_SERVED = 3
#: The client's route opt-in (``nexus.db.http_vector_client.PER_COLLECTION_ROUTE_ENV``; a test pins
#: that the two agree, so this module still imports without the client). The route is off by default
#: since nexus-tu8wp.3, so this script sets it for exactly the duration of a route run and clears it
#: for a batched one, whatever the caller's environment says.
ROUTE_ENV = "NX_SEARCH_PER_COLLECTION"
DEFAULT_KS = (1, 2, 3)
#: The pause between runs, in seconds, unless ``--pause`` says otherwise.
DEFAULT_PAUSE_S = 1.0


@contextlib.contextmanager
def route_env(on: bool):
    """Set ``NX_SEARCH_PER_COLLECTION=1`` (*on*) or remove it (off) for the duration of the block,
    then restore the caller's value. The env is process-wide, so concurrent searches share one
    ``route_env`` block around all of them; never nest one per thread."""
    prior = os.environ.get(ROUTE_ENV)
    if on:
        os.environ[ROUTE_ENV] = "1"
    else:
        os.environ.pop(ROUTE_ENV, None)
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(ROUTE_ENV, None)
        else:
            os.environ[ROUTE_ENV] = prior


def latency_query(shape: str) -> str:
    return f"{_BASE_QUERY} ({LATENCY_MARKERS[shape]})"


def concurrency_query(k: int, i: int) -> str:
    return f"{_BASE_QUERY} ({CONCURRENCY_MARKER}-k{k}-s{i})"


def lexical_query(path: str, *, k: int = 0, i: int = 0) -> str:
    """The lexical mode's marked query: per path, and per concurrent search."""
    tail = f"-k{k}-s{i}" if path == "conc" else ""
    return f"{_BASE_QUERY} {LEXICAL_MARKER}-{path}{tail}"


def utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


class RouteNotServedError(RuntimeError):
    """A route run never reached the route, or fell back to the batched path
    for some group: its timing would be the batched path's."""


@dataclass
class Recorder:
    """Counts the requests one search made and what the route answered.

    Wraps ``search`` and ``search_per_collection`` on the instance."""

    client: Any
    search_calls: int = 0
    route_calls: int = 0
    route_served: int = 0
    route_none: int = 0
    #: ``(collection, error_kind)`` for every per-collection entry the route
    #: reported as failed
    isolated_errors: list[tuple[str, str]] = field(default_factory=list)
    #: ``(http status or None, message)`` for every route request that raised
    route_failures: list[tuple[int | None, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _orig: dict[str, Any] = field(default_factory=dict, repr=False)

    def __enter__(self) -> Recorder:
        c = self.client
        real_search = c.search

        def _search(*a, **kw):
            with self._lock:
                self.search_calls += 1
            return real_search(*a, **kw)

        c.search = _search
        real_route = getattr(c, "search_per_collection", None)
        if real_route is not None:

            def _route(query, names, **kw):
                with self._lock:
                    self.route_calls += 1
                try:
                    out = real_route(query, names, **kw)
                except Exception as exc:  # noqa: BLE001 — recorded and re-raised, never swallowed
                    with self._lock:
                        self.route_failures.append((getattr(exc, "code", None), str(exc)[:200]))
                    raise
                with self._lock:
                    if out is None:
                        self.route_none += 1
                    else:
                        self.route_served += 1
                        for e in out.get("per_collection", []):
                            if e.get("error") is not None or e.get("error_kind") is not None:
                                self.isolated_errors.append(
                                    (str(e.get("collection")), str(e.get("error_kind"))),
                                )
                return out

            c.search_per_collection = _route
        return self

    def __exit__(self, *exc) -> None:
        for name in ("search", "search_per_collection"):
            vars(self.client).pop(name, None)


@dataclass
class Run:
    path: str  # "route" | "batched"
    shape: str
    round: int  # 0-based; -1 for a warm-up
    wall_s: float
    search_calls: int
    route_calls: int
    rows: int
    failed: dict[str, str]
    isolated_errors: list[tuple[str, str]]
    route_failures: list[tuple[int | None, str]]
    start_utc: str
    end_utc: str
    #: rows kept per collection (``SearchDiagnostics``) and the top-n page after the boosts:
    #: ``{"key", "collection", "rank"}`` with the rank inside the collection's own rows of this pool
    kept: dict[str, int] = field(default_factory=dict)
    page: list[dict] = field(default_factory=list)
    #: why the page could not be built (``page_of`` raised); the timing is kept
    page_error: str | None = None
    #: a server rerank degraded in some collection (the pool order is then not the rerank order)
    rerank_degraded: bool = False


def run_one(
    *,
    path: str,
    shape: str,
    round_no: int,
    query: str,
    cols: list[str],
    make_client: Callable[[bool], Any],
    search: Callable[..., list],
    diag_type: type,
    page_of: Callable[..., list] = boosted_page,
) -> Run:
    """One search on a FRESH client. *path* ``route`` leaves
    ``supports_per_collection_search`` True and must reach the route for every
    group; ``batched`` sets it False and must never touch the route.

    The page (:func:`~search_page_parity.boosted_page`) is cut AFTER the Recorder is restored and
    the wall time taken, so it can touch neither the timing nor the request counts; a *page_of*
    that raises leaves an empty page and ``page_error`` set, and the run carries on. Building the page
    may still list collections through the process's shared client (the boosts look each collection's
    catalog row up), outside the Recorder and the timed window."""
    n, rerank, limit = SHAPES[shape]
    k_cap = shape_k_cap(shape)  # resolved before the clock starts (a first call imports nexus.mcp.core)
    client = make_client(path == "route")
    diag: list = []
    model_of_out: list = []
    rerank_meta: dict = {}
    start = utc_now()
    with route_env(path == "route"), Recorder(client) as rec:
        t0 = time.perf_counter()
        results = search(
            query, cols, n_results=n, t3=client, cluster_by=None, rerank=rerank,
            diagnostics_out=diag, sensitivity="include", per_collection_k_cap=k_cap,
            model_of_out=model_of_out, **({"rerank_meta_out": rerank_meta} if rerank else {}),
        )
        wall = time.perf_counter() - t0
    end = utc_now()  # the search's window ends here, before the page is built
    failed: dict[str, str] = {}
    for d in diag:
        if isinstance(d, diag_type):
            failed.update(d.failed_collections)
    if path == "route":
        if rec.route_served < 1 or rec.route_none > 0 or rec.search_calls > 0:
            raise RouteNotServedError(
                f"{shape} round {round_no}: the route run reached the route {rec.route_calls} time(s), "
                f"was served {rec.route_served}, got None {rec.route_none}, and made {rec.search_calls} "
                "/search call(s): this is not a route measurement (an engine before the route, an edge "
                "refusal, or a group that fell back to the batched path)",
            )
    elif rec.search_calls < 1 or rec.route_calls:
        raise RouteNotServedError(
            f"{shape} round {round_no}: the batched run made {rec.search_calls} /search call(s) and "
            f"{rec.route_calls} route call(s): it is not a batched baseline",
        )
    page_rows: list[dict] = []
    page_error: str | None = None
    try:
        page_rows = [asdict(r) for r in parity.page_rows(
            page_of(list(results), limit, model_of=model_of_out[-1] if model_of_out else None), results,
        )]
    except Exception as exc:  # noqa: BLE001 — a page that cannot be built must not cost the timing
        page_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    return Run(
        path=path, shape=shape, round=round_no, wall_s=wall, search_calls=rec.search_calls,
        route_calls=rec.route_calls, rows=len(results), failed=failed,
        isolated_errors=list(rec.isolated_errors), route_failures=list(rec.route_failures),
        start_utc=start, end_utc=end,
        kept=parity.kept_counts(d for d in diag if isinstance(d, diag_type)),
        page=page_rows, page_error=page_error,
        rerank_degraded=any(bool(m.get("degraded")) for m in rerank_meta.values()),
    )


def expected_requests_latency(
    shapes: Sequence[str], rounds: int, warmup: int,
    estimate: Callable[[str], tuple[int, int]],
) -> str:
    """The line printed before a latency run sends anything: how many requests
    it will make, per shape and in all (an estimate from the client's own
    planner; the per-run counts print as they happen)."""
    parts = []
    total = 0
    for shape in shapes:
        route_per, batched_per = estimate(shape)
        runs = warmup + rounds
        n = runs * (route_per + batched_per)
        total += n
        parts.append(
            f"{shape}: {runs} route runs x {route_per} + {runs} batched runs x {batched_per} = {n}",
        )
    return f"EXPECTED REQUESTS (estimate) {total} total  [" + "; ".join(parts) + "]"


def expected_requests_concurrency(
    ks: Sequence[int], shape: str, estimate: Callable[[str], tuple[int, int]],
) -> str:
    route_per, _ = estimate(shape)
    total = sum(ks) * route_per
    return (
        f"EXPECTED REQUESTS (estimate) {total} total route requests  "
        f"[K in {list(ks)}: {sum(ks)} searches x {route_per}]"
    )


def measure_latency(
    *,
    shapes: Sequence[str],
    rounds: int,
    warmup: int,
    cols: list[str] | dict[str, list[str]],
    make_client: Callable[[bool], Any],
    search: Callable[..., list],
    diag_type: type,
    emit: Callable[[str], None] = print,
    pause_s: float = DEFAULT_PAUSE_S,
    estimate: Callable[[str], tuple[int, int]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    page_of: Callable[..., list] = boosted_page,
    route_k_of: Callable[[str], Callable[[str], int]] | None = None,
) -> dict[str, dict]:
    """Interleaved route-vs-batched rounds per shape. Returns, per shape, the
    runs, medians and the UTC window. Runs are sequential with *pause_s*
    between them; *estimate* maps a shape to the requests one route run and
    one batched run send, and the total is printed before the first request.

    Result parity (module docstring): per round, the kept counts, the page overlap and the
    past-k rows of the route/batched pair. *page_of* cuts the boosted page (injectable for
    offline tests); *route_k_of* maps a shape to ``collection -> route k`` and defaults to
    the client's own sizing (:func:`make_route_k`, which needs the collection names)."""
    if route_k_of is None:
        route_k_of = make_route_k(cols, SHAPES)
    if estimate is not None:
        emit(expected_requests_latency(shapes, rounds, warmup, estimate))
    out: dict[str, dict] = {}
    for shape in shapes:
        query = latency_query(shape)
        runs: list[Run] = []
        pending: dict[int, dict[str, Run]] = {}
        records: list[parity.PairRecord] = []
        shape_cols = cols_for(cols, shape)
        limit = SHAPES[shape][2]
        reranks = SHAPES[shape][1]
        route_k = route_k_of(shape)

        def _pair(round_no: int, pair: dict[str, Run], records=records, limit=limit, reranks=reranks,
                  shape=shape, route_k=route_k) -> None:
            rr, bb = pair["route"], pair["batched"]
            nf, bf = len(rr.failed), len(bb.failed)
            skipped = (
                "page error" if (rr.page_error or bb.page_error)
                else "rerank degraded" if (rr.rerank_degraded or bb.rerank_degraded) else None
            )
            if skipped:
                records.append(parity.PairRecord(round_no, None, nf, bf, skipped))
                emit(f"  {shape:<4} parity  round {round_no:>2}  SKIPPED ({skipped}; route "
                     f"{rr.page_error or 'ok'}, batched {bb.page_error or 'ok'})")
            else:
                p = parity.compare_pages(
                    [parity.PageRow(**d) for d in rr.page], [parity.PageRow(**d) for d in bb.page],
                    limit, route_k,
                )
                rec = parity.PairRecord(round_no, p, nf, bf)
                records.append(rec)
                notes = []
                if reranks:  # the page is the pool's boosted order, not the order the CLI shows
                    notes.append("pre-rerank pool order")
                if rec.excluded:
                    notes.append(f"EXCLUDED, failed collections: route {nf} batched {bf}")
                emit(parity.format_parity_line(shape, round_no, p, "; ".join(notes)))
            emit(parity.format_kept_line(shape, round_no, rr.kept, bb.kept))

        def _go(path: str, round_no: int, runs=runs, shape=shape, query=query, pending=pending,
                shape_cols=shape_cols) -> None:
            if runs:
                sleep(pause_s)
            r = run_one(
                path=path, shape=shape, round_no=round_no, query=query, cols=shape_cols,
                make_client=make_client, search=search, diag_type=diag_type, page_of=page_of,
            )
            runs.append(r)
            emit(
                f"  {shape:<4} {path:<7} round {round_no:>2}  wall {r.wall_s:>7.2f}s  "
                f"/search {r.search_calls:>3}  route {r.route_calls:>2}  rows {r.rows:>4}  "
                f"failed {len(r.failed)}  {r.start_utc}",
            )
            pair = pending.setdefault(round_no, {})
            pair[path] = r
            if len(pair) == 2:
                _pair(round_no, pending.pop(round_no))

        window_start = utc_now()
        emit(f"\n[{shape}] fetch n={SHAPES[shape][0]} page {SHAPES[shape][2]} rerank={SHAPES[shape][1]} query {query!r}")
        for w in range(warmup):
            for path in ("route", "batched"):
                _go(path, -1 - w)
        for rnd in range(rounds):
            order = ("route", "batched") if rnd % 2 == 0 else ("batched", "route")
            for path in order:
                _go(path, rnd)
        window_end = utc_now()
        timed = [r for r in runs if r.round >= 0]
        medians = {
            p: statistics.median([r.wall_s for r in timed if r.path == p]) for p in ("route", "batched")
        }
        calls = {p: statistics.median([r.search_calls + r.route_calls for r in timed if r.path == p])
                 for p in ("route", "batched")}
        emit(
            f"[{shape}] MEDIAN wall: route {medians['route']:.2f}s  batched {medians['batched']:.2f}s  "
            f"(route/batched {medians['route'] / medians['batched']:.2f})  "
            f"median requests: route {calls['route']:.0f}  batched {calls['batched']:.0f}  "
            f"failed_collections route {sum(len(r.failed) for r in timed if r.path == 'route')} "
            f"batched {sum(len(r.failed) for r in timed if r.path == 'batched')}",
        )
        emit(f"[{shape}] WINDOW utc {window_start} .. {window_end}")
        timed_records = [r for r in records if r.round >= 0]
        summary = parity.summarize_pairs(timed_records)
        emit(parity.format_summary_line(shape, summary, "rerank degraded or page error"))
        out[shape] = {
            "runs": runs, "medians": medians, "calls": calls,
            "window": (window_start, window_end),
            "parity": [r.as_dict() for r in records],
            "parity_summary": summary,
        }
    return out


def measure_concurrency(
    *,
    ks: Sequence[int],
    shape: str,
    cols: list[str] | dict[str, list[str]],
    make_client: Callable[[bool], Any],
    search: Callable[..., list],
    diag_type: type,
    emit: Callable[[str], None] = print,
    pause_s: float = DEFAULT_PAUSE_S,
    estimate: Callable[[str], tuple[int, int]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[int, dict]:
    """K concurrent route searches per K, released together, each on its own
    fresh client and its own marked query. Records every wall time, every
    ``failed_collections`` entry with its ``error_kind``, and whole-request
    failures with their status. The K groups run one after another with
    *pause_s* between them."""
    n, rerank, _limit = SHAPES[shape]
    k_cap = shape_k_cap(shape)  # resolved once, before the barrier: never inside a timed worker
    shape_cols = cols_for(cols, shape)
    if estimate is not None:
        emit(expected_requests_concurrency(ks, shape, estimate))
    out: dict[int, dict] = {}
    for idx, k in enumerate(ks):
        if idx:
            sleep(pause_s)
        emit(f"\n[concurrency] K={k} shape={shape} (n={n} rerank={rerank})")
        barrier = threading.Barrier(k)
        results: list[dict] = [{} for _ in range(k)]

        def _worker(i: int, k=k, barrier=barrier, results=results, shape_cols=shape_cols) -> None:
            query = concurrency_query(k, i)
            client = make_client(True)
            diag: list = []
            with Recorder(client) as rec:
                barrier.wait()
                t0 = time.perf_counter()
                err: str | None = None
                rows = 0
                try:
                    res = search(
                        query, shape_cols, n_results=n, t3=client, cluster_by=None, rerank=rerank,
                        diagnostics_out=diag, sensitivity="include",
                        per_collection_k_cap=k_cap,
                    )
                    rows = len(res)
                except Exception as exc:  # noqa: BLE001 — the search's own failure is a measurement
                    err = f"{type(exc).__name__}: {str(exc)[:200]}"
                wall = time.perf_counter() - t0
            failed: dict[str, str] = {}
            for d in diag:
                if isinstance(d, diag_type):
                    failed.update(d.failed_collections)
            results[i] = {
                "query": query, "wall_s": wall, "rows": rows, "error": err,
                "route_served": rec.route_served, "route_none": rec.route_none,
                "search_calls": rec.search_calls, "failed": failed,
                "isolated": list(rec.isolated_errors), "route_failures": list(rec.route_failures),
            }

        start = utc_now()
        threads = [threading.Thread(target=_worker, args=(i,), name=f"conc-{k}-{i}") for i in range(k)]
        with route_env(True):  # one block around every worker: the env is process-wide
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        end = utc_now()
        unserved = [r for r in results if r["route_served"] < 1 or r["search_calls"] > 0]
        for i, r in enumerate(results):
            kinds: dict[str, int] = {}
            for _col, kind in r["isolated"]:
                kinds[kind] = kinds.get(kind, 0) + 1
            emit(
                f"  K={k} search {i}: wall {r['wall_s']:>7.2f}s rows {r['rows']:>4} "
                f"failed_collections {len(r['failed'])} error_kinds {kinds or '-'} "
                f"route_failures {r['route_failures'] or '-'} error {r['error'] or '-'}  {r['query']!r}",
            )
        walls = [r["wall_s"] for r in results]
        emit(
            f"[concurrency] K={k} wall median {statistics.median(walls):.2f}s max {max(walls):.2f}s  "
            f"searches with failed_collections {sum(1 for r in results if r['failed'])}/{k}  "
            f"whole-request failures {sum(1 for r in results if r['error'])}/{k}",
        )
        emit(f"[concurrency] K={k} WINDOW utc {start} .. {end}")
        out[k] = {"results": results, "window": (start, end), "unserved": len(unserved)}
        if unserved:
            raise RouteNotServedError(
                f"K={k}: {len(unserved)} of {k} concurrent searches never reached the route (or fell "
                "back to /search): this is not a route measurement",
            )
    return out


def _lexical_search_once(
    *, path: str, query: str, cols: list[str], make_client: Callable[[bool], Any],
    search: Callable[..., list], diag_type: type,
) -> dict:
    """One ``--lexical`` search on a fresh route client. *path* ``nofield`` drops the
    route's ``lexical`` keyword, so the leg runs on ``/hybrid-search`` as before xd2rc."""
    n, rerank, _limit = SHAPES["mcp"]
    client = make_client(True)
    counts = {"hybrid": 0, "served": 0}
    kinds: dict[str, int] = {}
    real_route = client.search_per_collection
    real_hybrid = client.hybrid_search

    def _route(query_, names, **kw):
        if path == "nofield":
            kw.pop("lexical", None)
        out = real_route(query_, names, **kw)
        if out is not None and out.get("lexical") is True:
            counts["served"] += 1
            for e in out.get("per_collection", []):
                kind = e.get("lexical_error_kind")
                if kind is not None:
                    kinds[str(kind)] = kinds.get(str(kind), 0) + 1
        return out

    def _hybrid(*a, **kw):
        counts["hybrid"] += 1
        return real_hybrid(*a, **kw)

    client.search_per_collection = _route
    client.hybrid_search = _hybrid
    diag: list = []
    start = utc_now()
    t0 = time.perf_counter()
    err: str | None = None
    rows = 0
    try:
        rows = len(search(query, cols, n_results=n, t3=client, cluster_by=None, rerank=rerank,
                          lexical=True, diagnostics_out=diag, sensitivity="include"))
    except Exception as exc:  # noqa: BLE001 — the search's own failure is a measurement
        err = f"{type(exc).__name__}: {str(exc)[:200]}"
    wall = time.perf_counter() - t0
    failed: dict[str, str] = {}
    for d in diag:
        if isinstance(d, diag_type):
            failed.update(d.failed_collections)
    return {
        "path": path, "query": query, "wall_s": wall, "rows": rows, "error": err,
        "lexical_served": counts["served"] > 0, "hybrid_calls": counts["hybrid"],
        "lexical_kinds": kinds, "failed": failed, "start_utc": start, "end_utc": utc_now(),
    }


def _opt_in_route_lexical() -> None:
    """The lexical mode measures the route's ``lexical`` field, which the client sends only on
    ``NX_SEARCH_ROUTE_LEXICAL`` (nexus-n51v1); the ``nofield`` path drops it again."""
    import os  # noqa: PLC0415

    from nexus.db.http_vector_client import ROUTE_LEXICAL_ENV  # noqa: PLC0415

    os.environ[ROUTE_LEXICAL_ENV] = "1"


def measure_lexical(
    *,
    rounds: int,
    warmup: int,
    ks: Sequence[int],
    cols: list[str],
    make_client: Callable[[bool], Any],
    search: Callable[..., list],
    diag_type: type,
    emit: Callable[[str], None] = print,
    pause_s: float = DEFAULT_PAUSE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """nexus-pq3wr: interleaved field / no-field rounds of a default ``--lexical``
    search, then K concurrent searches with the field. Sequential runs with *pause_s*
    between them."""
    _opt_in_route_lexical()
    runs: list[dict] = []

    def _go(path: str, round_no: int) -> None:
        if runs:
            sleep(pause_s)
        with route_env(True):
            r = _lexical_search_once(path=path, query=lexical_query(path), cols=cols,
                                     make_client=make_client, search=search, diag_type=diag_type)
        r["round"] = round_no
        runs.append(r)
        emit(
            f"  lexical {path:<7} round {round_no:>2}  wall {r['wall_s']:>7.2f}s  served {r['lexical_served']!s:<5} "
            f"/hybrid-search {r['hybrid_calls']:>3}  arm kinds {r['lexical_kinds'] or '-'}  rows {r['rows']:>4}  "
            f"failed {len(r['failed'])}  error {r['error'] or '-'}  {r['start_utc']}",
        )

    window_start = utc_now()
    emit(f"\n[lexical] n={SHAPES['mcp'][0]} rerank={SHAPES['mcp'][1]} collections {len(cols)}")
    for w in range(warmup):
        for path in ("field", "nofield"):
            _go(path, -1 - w)
    for rnd in range(rounds):
        for path in (("field", "nofield") if rnd % 2 == 0 else ("nofield", "field")):
            _go(path, rnd)
    window_end = utc_now()
    timed = [r for r in runs if r["round"] >= 0]
    med = {p: statistics.median([r["wall_s"] for r in timed if r["path"] == p]) for p in ("field", "nofield")}
    hyb = {p: statistics.median([r["hybrid_calls"] for r in timed if r["path"] == p]) for p in ("field", "nofield")}
    emit(
        f"[lexical] MEDIAN wall: field {med['field']:.2f}s  nofield {med['nofield']:.2f}s  "
        f"(field/nofield {med['field'] / med['nofield']:.2f})  median /hybrid-search: field {hyb['field']:.0f}  "
        f"nofield {hyb['nofield']:.0f}",
    )
    emit(f"[lexical] WINDOW utc {window_start} .. {window_end}")
    conc: dict[int, dict] = {}
    for k in ks:
        sleep(pause_s)
        barrier = threading.Barrier(k)
        results: list[dict] = [{} for _ in range(k)]

        def _worker(i: int, k=k, barrier=barrier, results=results) -> None:
            barrier.wait()
            results[i] = _lexical_search_once(path="field", query=lexical_query("conc", k=k, i=i), cols=cols,
                                              make_client=make_client, search=search, diag_type=diag_type)

        start = utc_now()
        threads = [threading.Thread(target=_worker, args=(i,), name=f"lex-{k}-{i}") for i in range(k)]
        with route_env(True):
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        end = utc_now()
        for i, r in enumerate(results):
            emit(
                f"  lexical K={k} search {i}: wall {r['wall_s']:>7.2f}s  /hybrid-search {r['hybrid_calls']:>3}  "
                f"arm kinds {r['lexical_kinds'] or '-'}  failed {len(r['failed'])}  error {r['error'] or '-'}",
            )
        emit(f"[lexical] K={k} WINDOW utc {start} .. {end}")
        conc[k] = {"results": results, "window": (start, end)}
    return {"runs": runs, "medians": med, "hybrid": hyb, "window": (window_start, window_end), "concurrency": conc}


def make_estimator(cols: list[str] | dict[str, list[str]]) -> Callable[[str], tuple[int, int]]:
    """``shape -> (route requests, batched requests)`` one search sends, from
    the client's own planner (one route request per embedding-model group; the
    batched count follows ``_desired_candidate_count``). An estimate: a poisoned
    collection or a split retry changes the real count, which prints per run."""

    def estimate(shape: str) -> tuple[int, int]:
        from nexus.db.limits import QUOTAS  # noqa: PLC0415
        from nexus.search_engine import (  # noqa: PLC0415
            _desired_candidate_count,
            _group_collections_by_embedding_model,
        )

        n, rerank, _limit = SHAPES[shape]
        groups = _group_collections_by_embedding_model(cols_for(cols, shape))
        batched = 0
        for g in groups:
            desired = _desired_candidate_count(g, n, deep=rerank)
            cap = QUOTAS.MAX_QUERY_RESULTS
            batched += min(len(g), -(-desired // cap)) if desired > cap and len(g) > 1 else 1
        return len(groups), batched

    return estimate


def _live_world() -> tuple[list[str], dict[str, list[str]], Callable[[bool], Any], Callable[..., list], type]:
    """The live client factory, collections and search function (imports here so
    the module imports without the client for the hermetic tests)."""
    from nexus.corpus import resolve_corpus  # noqa: PLC0415
    from nexus.db.http_vector_client import HttpVectorClient  # noqa: PLC0415
    from nexus.logging_setup import configure_logging  # noqa: PLC0415
    from nexus.search_engine import SearchDiagnostics, search_cross_corpus  # noqa: PLC0415

    configure_logging("cli")  # structlog's unconfigured default writes to stdout
    def make_client(route: bool) -> HttpVectorClient:
        client = HttpVectorClient()
        client.supports_per_collection_search = route  # type: ignore[misc]
        # nexus-pq3wr: an unconfirmed client sends its route request under the
        # process-wide single-flight probe lock, so K fresh clients in K threads ran
        # one at a time. The process singleton is confirmed at construction from the
        # cloud version probe (_seed_from_probe); do the same here.
        client._per_collection_confirmed = route
        return client

    probe = make_client(True)
    all_collections = [c["name"] for c in probe.list_collections()]

    def resolve(specs: Sequence[str]) -> list[str]:
        out: list[str] = []
        for spec in specs:
            for c in resolve_corpus(spec, all_collections):
                if c not in out:
                    out.append(c)
        return out

    cols = resolve(DEFAULT_CORPORA)  # the lexical mode's four corpora
    if not cols:
        raise SystemExit("no collections resolved for the default corpora")
    by_shape = {shape: resolve(shape_corpora(shape)) for shape in SHAPES}
    empty = [s for s, c in by_shape.items() if not c]
    if empty:
        raise SystemExit(f"no collections resolved for the default corpora of shape(s) {empty}")
    return cols, by_shape, make_client, search_cross_corpus, SearchDiagnostics


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    lat = sub.add_parser("latency", help="interleaved route-vs-batched rounds")
    lat.add_argument("--rounds", type=int, default=4)
    lat.add_argument("--warmup", type=int, default=1)
    lat.add_argument("--shapes", default="mcp,cli")
    lat.add_argument("--pause", type=float, default=DEFAULT_PAUSE_S,
                     help="seconds between runs (default %(default)s)")
    conc = sub.add_parser("concurrency", help="K concurrent default searches through the route")
    conc.add_argument("--ks", default=",".join(str(k) for k in DEFAULT_KS))
    conc.add_argument("--include-5", action="store_true",
                      help="allow K=5 (the engine's arm gate has 5 permits; K=5 is the heaviest load)")
    conc.add_argument("--shape", default="mcp", choices=sorted(SHAPES))
    conc.add_argument("--pause", type=float, default=DEFAULT_PAUSE_S,
                      help="seconds between the K groups (default %(default)s)")
    lex = sub.add_parser("lexical", help="--lexical with and without the route's lexical field (nexus-pq3wr)")
    lex.add_argument("--rounds", type=int, default=4)
    lex.add_argument("--warmup", type=int, default=1)
    lex.add_argument("--ks", default="3", help="concurrent searches with the field after the rounds; empty for none")
    lex.add_argument("--pause", type=float, default=DEFAULT_PAUSE_S,
                     help="seconds between runs (default %(default)s)")
    args = ap.parse_args(argv)

    ks_arg = [int(x) for x in args.ks.split(",") if x] if args.mode in ("concurrency", "lexical") else []
    if any(k > 3 for k in ks_arg) and not args.include_5:
        raise SystemExit("K above 3 needs --include-5 (load is bounded by default)")
    if any(k < 1 or k > 5 for k in ks_arg):
        raise SystemExit("K must be in 1..5")
    cols, cols_by_shape, make_client, search, diag_type = _live_world()
    estimate = make_estimator(cols_by_shape)
    print(f"collections in the four default corpora (lexical): {len(cols)}")  # noqa: T201
    for shape_name, shape_cols in cols_by_shape.items():
        print(  # noqa: T201
            f"collections for shape {shape_name} ({','.join(shape_corpora(shape_name))}): {len(shape_cols)}",
        )
    started = utc_now()
    try:
        if args.mode == "latency":
            shapes = [s for s in args.shapes.split(",") if s]
            bad = [s for s in shapes if s not in SHAPES]
            if bad:
                raise SystemExit(f"unknown shape(s) {bad}; choose from {sorted(SHAPES)}")
            res = measure_latency(
                shapes=shapes, rounds=args.rounds, warmup=args.warmup, cols=cols_by_shape,
                make_client=make_client, search=search, diag_type=diag_type,
                pause_s=args.pause, estimate=estimate,
            )
            payload: Any = {
                s: {"medians": v["medians"], "calls": v["calls"], "window": v["window"],
                    "parity": v["parity"], "parity_summary": v["parity_summary"],
                    "runs": [r.__dict__ for r in v["runs"]]} for s, v in res.items()
            }
        elif args.mode == "lexical":
            res = measure_lexical(
                rounds=args.rounds, warmup=args.warmup, ks=ks_arg, cols=cols, make_client=make_client,
                search=search, diag_type=diag_type, pause_s=args.pause,
            )
            payload = res
        else:
            res = measure_concurrency(
                ks=ks_arg, shape=args.shape, cols=cols_by_shape, make_client=make_client, search=search,
                diag_type=diag_type, pause_s=args.pause, estimate=estimate,
            )
            payload = {str(k): {"window": v["window"], "results": v["results"]} for k, v in res.items()}
    except RouteNotServedError as exc:
        print(f"ROUTE NOT SERVED: {exc}", file=sys.stderr)  # noqa: T201
        return EXIT_ROUTE_NOT_SERVED
    print(f"\nrun window utc {started} .. {utc_now()}")  # noqa: T201
    print("TU8WP3_MEASURE_JSON " + json.dumps({"mode": args.mode, "data": payload}, default=str))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
