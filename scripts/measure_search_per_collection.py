#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tu8wp.3: latency, call-count and concurrency measurement of the
per-collection search route against the batched path, on the live cloud.

READS ONLY. Every request is a vector search or a collection listing; nothing
is written, and no config, environment or credential is printed (the client
reads its own config; this script never opens it).

Two modes::

    uv run python scripts/measure_search_per_collection.py latency [--rounds 4] [--warmup 1] [--shapes mcp,cli] [--pause 1.0]
    uv run python scripts/measure_search_per_collection.py concurrency [--ks 1,2,3] [--include-5] [--shape mcp] [--pause 1.0]

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

Shapes (the search itself, ``search_cross_corpus`` over the ``knowledge, code,
docs, rdr`` corpora, no catalog/taxonomy/telemetry: the retrieval step):

- ``mcp``: the MCP ``search`` tool's default fetch, n=30, no rerank;
- ``cli``: ``nx search``'s default, n=10, server rerank on (a multi-collection
  search reranks).

``concurrency``: K concurrent default searches through the route (K in
``--ks``), each on its own fresh client, released together. Prints the wall
time of every search and, from the route's own envelopes and failures, every
``failed_collections`` entry with its ``error_kind`` (``fanout_budget_exhausted``,
``statement_timeout``, ...) and every whole-request failure with its HTTP
status (a 503 from pool or admission exhaustion).

A route run that did not reach the route (a 404, an edge refusal, an engine
that predates it) exits 3 instead of timing the batched fallback as the route.

QUERY STRINGS PER LEG (distinct, so engine log lines are attributable): every
string below ends in a leg marker; the concurrency mode adds ``-k<K>-s<i>`` per
search.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import statistics
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: One base question; each leg appends its own marker (see the module docstring).
_BASE_QUERY = "What does the tuple_renew operation do to the attempts counter?"
LATENCY_MARKERS = {"mcp": "tu8wp3-lat-mcp", "cli": "tu8wp3-lat-cli"}
CONCURRENCY_MARKER = "tu8wp3-conc"
#: The corpora of a default search (the MCP tool's default plus ``rdr``).
DEFAULT_CORPORA = ("knowledge", "code", "docs", "rdr")
#: ``shape -> (n_results, rerank)``.
SHAPES: dict[str, tuple[int, bool]] = {"mcp": (30, False), "cli": (10, True)}
EXIT_ROUTE_NOT_SERVED = 3
DEFAULT_KS = (1, 2, 3)
#: The pause between runs, in seconds, unless ``--pause`` says otherwise.
DEFAULT_PAUSE_S = 1.0


def latency_query(shape: str) -> str:
    return f"{_BASE_QUERY} ({LATENCY_MARKERS[shape]})"


def concurrency_query(k: int, i: int) -> str:
    return f"{_BASE_QUERY} ({CONCURRENCY_MARKER}-k{k}-s{i})"


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
) -> Run:
    """One search on a FRESH client. *path* ``route`` leaves
    ``supports_per_collection_search`` True and must reach the route for every
    group; ``batched`` sets it False and must never touch the route."""
    n, rerank = SHAPES[shape]
    client = make_client(path == "route")
    diag: list = []
    start = utc_now()
    with Recorder(client) as rec:
        t0 = time.perf_counter()
        results = search(
            query, cols, n_results=n, t3=client, cluster_by=None, rerank=rerank,
            diagnostics_out=diag,
        )
        wall = time.perf_counter() - t0
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
    return Run(
        path=path, shape=shape, round=round_no, wall_s=wall, search_calls=rec.search_calls,
        route_calls=rec.route_calls, rows=len(results), failed=failed,
        isolated_errors=list(rec.isolated_errors), route_failures=list(rec.route_failures),
        start_utc=start, end_utc=utc_now(),
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
    cols: list[str],
    make_client: Callable[[bool], Any],
    search: Callable[..., list],
    diag_type: type,
    emit: Callable[[str], None] = print,
    pause_s: float = DEFAULT_PAUSE_S,
    estimate: Callable[[str], tuple[int, int]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, dict]:
    """Interleaved route-vs-batched rounds per shape. Returns, per shape, the
    runs, medians and the UTC window. Runs are sequential with *pause_s*
    between them; *estimate* maps a shape to the requests one route run and
    one batched run send, and the total is printed before the first request."""
    if estimate is not None:
        emit(expected_requests_latency(shapes, rounds, warmup, estimate))
    out: dict[str, dict] = {}
    for shape in shapes:
        query = latency_query(shape)
        runs: list[Run] = []

        def _go(path: str, round_no: int, runs=runs, shape=shape, query=query) -> None:
            if runs:
                sleep(pause_s)
            r = run_one(
                path=path, shape=shape, round_no=round_no, query=query, cols=cols,
                make_client=make_client, search=search, diag_type=diag_type,
            )
            runs.append(r)
            emit(
                f"  {shape:<4} {path:<7} round {round_no:>2}  wall {r.wall_s:>7.2f}s  "
                f"/search {r.search_calls:>3}  route {r.route_calls:>2}  rows {r.rows:>4}  "
                f"failed {len(r.failed)}  {r.start_utc}",
            )

        window_start = utc_now()
        emit(f"\n[{shape}] n={SHAPES[shape][0]} rerank={SHAPES[shape][1]} query {query!r}")
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
        out[shape] = {
            "runs": runs, "medians": medians, "calls": calls,
            "window": (window_start, window_end),
        }
    return out


def measure_concurrency(
    *,
    ks: Sequence[int],
    shape: str,
    cols: list[str],
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
    n, rerank = SHAPES[shape]
    if estimate is not None:
        emit(expected_requests_concurrency(ks, shape, estimate))
    out: dict[int, dict] = {}
    for idx, k in enumerate(ks):
        if idx:
            sleep(pause_s)
        emit(f"\n[concurrency] K={k} shape={shape} (n={n} rerank={rerank})")
        barrier = threading.Barrier(k)
        results: list[dict] = [{} for _ in range(k)]

        def _worker(i: int, k=k, barrier=barrier, results=results) -> None:
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
                        query, cols, n_results=n, t3=client, cluster_by=None, rerank=rerank,
                        diagnostics_out=diag,
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


def make_estimator(cols: list[str]) -> Callable[[str], tuple[int, int]]:
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

        n, rerank = SHAPES[shape]
        groups = _group_collections_by_embedding_model(cols)
        batched = 0
        for g in groups:
            desired = _desired_candidate_count(g, n, deep=rerank)
            cap = QUOTAS.MAX_QUERY_RESULTS
            batched += min(len(g), -(-desired // cap)) if desired > cap and len(g) > 1 else 1
        return len(groups), batched

    return estimate


def _live_world() -> tuple[list[str], Callable[[bool], Any], Callable[..., list], type]:
    """The live client factory, collections and search function (imports here so
    the module imports without the client for the hermetic tests)."""
    from nexus.corpus import resolve_corpus  # noqa: PLC0415
    from nexus.db.http_vector_client import (  # noqa: PLC0415
        PER_COLLECTION_ROUTE_ENV,
        HttpVectorClient,
        per_collection_route_enabled,
    )
    from nexus.logging_setup import configure_logging  # noqa: PLC0415
    from nexus.search_engine import SearchDiagnostics, search_cross_corpus  # noqa: PLC0415

    configure_logging("cli")  # structlog's unconfigured default writes to stdout
    if not per_collection_route_enabled():
        raise SystemExit(
            f"{PER_COLLECTION_ROUTE_ENV} turns the route off in this environment; unset it, "
            "or the route runs would be batched runs",
        )

    def make_client(route: bool) -> HttpVectorClient:
        client = HttpVectorClient()
        client.supports_per_collection_search = route  # type: ignore[misc]
        return client

    probe = make_client(True)
    all_collections = [c["name"] for c in probe.list_collections()]
    cols: list[str] = []
    for spec in DEFAULT_CORPORA:
        for c in resolve_corpus(spec, all_collections):
            if c not in cols:
                cols.append(c)
    if not cols:
        raise SystemExit("no collections resolved for the default corpora")
    return cols, make_client, search_cross_corpus, SearchDiagnostics


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
    args = ap.parse_args(argv)

    ks_arg = [int(x) for x in args.ks.split(",") if x] if args.mode == "concurrency" else []
    if any(k > 3 for k in ks_arg) and not args.include_5:
        raise SystemExit("K above 3 needs --include-5 (load is bounded by default)")
    if any(k < 1 or k > 5 for k in ks_arg):
        raise SystemExit("K must be in 1..5")
    cols, make_client, search, diag_type = _live_world()
    estimate = make_estimator(cols)
    print(f"collections in the default corpora: {len(cols)}")  # noqa: T201
    started = utc_now()
    try:
        if args.mode == "latency":
            shapes = [s for s in args.shapes.split(",") if s]
            bad = [s for s in shapes if s not in SHAPES]
            if bad:
                raise SystemExit(f"unknown shape(s) {bad}; choose from {sorted(SHAPES)}")
            res = measure_latency(
                shapes=shapes, rounds=args.rounds, warmup=args.warmup, cols=cols,
                make_client=make_client, search=search, diag_type=diag_type,
                pause_s=args.pause, estimate=estimate,
            )
            payload: Any = {
                s: {"medians": v["medians"], "calls": v["calls"], "window": v["window"],
                    "runs": [r.__dict__ for r in v["runs"]]} for s, v in res.items()
            }
        else:
            res = measure_concurrency(
                ks=ks_arg, shape=args.shape, cols=cols, make_client=make_client, search=search,
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
