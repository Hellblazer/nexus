#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-a6mys: the RDR-227 Gap 1 recall check for a collection served by its own HNSW index.

For a named collection this compares, per own-topic query, the engine's served top-k (the index arm) with an
exact top-k, at k of 40, 60, 80, 100 and 120, prints the recall at each k and the minimum, and exits non-zero when
the minimum is under the target (0.99). It is the check the ``nx doctor`` row "Prose collections at the index
threshold" points at.

No engine change is needed. Both arms are plain ``POST /v1/vectors/search`` requests through the client, so the
engine embeds the query text the same way for both:

* index arm: one single-collection search per k. Above ``NX_SEARCH_EXACT_MAX_ROWS`` the router sends it to the
  collection's own index (or to the ``ef_search = 1000`` walk until that index is built); this is the path a user's
  search takes.
* exact arm: the collection's chunk hashes are listed and sent as ``chash_allow`` in slices of at most 3000 (the
  engine's cap). The engine ranks an allow-list exactly, never by an index walk (nexus-jypmj). The slices' pages are
  merged by distance into the collection's exact top-120.

Scoring is the engine canary's (``PciCanary``): a row of the served page is a hit when it is in the exact top-k or
its distance is no greater than the k-th exact distance (a tie at the boundary is not a miss); recall is hits over
``min(k, exact rows)``. A query has a top-1 miss when the exact nearest row is absent and no served row is as near.

NON-VACUITY. The router runs a collection at or below the threshold exact, and then the index arm IS the exact arm
and recall reads 1.0 whatever the index does. The script reads the collection's physical row count and refuses
(exit 3) at or below the threshold, unless ``--allow-exact-route`` says the run is a rehearsal. The threshold is the
engine's ``NX_SEARCH_EXACT_MAX_ROWS``, which a client cannot read: ``--threshold`` defaults to this shell's value,
else 20,000, as ``nx doctor`` does. The client also cannot see which route a search took, nor whether the
collection's index is built; a run before the build finishes measures the ``ef_search = 1000`` walk, which is what
is then served.

COST AND SAFETY. READ ONLY: lists, gets and searches, nothing is written, nothing is printed from a chunk's text.
Each search embeds its query on the engine (a Voyage call on voyage-context-3). The plan, with the request count,
prints first; ``--dry-run`` stops after it. A run is 5 + ceil(rows / 3000) searches per query, plus the listing
(rows / 300 pages). Run it against the engine beside the collection, serially (it is), never against a shared
engine under load without telling the people who use it.

QUERIES. Own-topic queries written for the collection (``--queries-file``, one per line, at most 256 characters;
blank lines and ``#`` lines skipped) are the measure the RDR set. ``--sample N --seed S`` instead takes N chunks'
opening text from the collection itself (sensitive chunks excluded); a chunk used as its own query is the weaker
document-to-document evidence RDR-227 research-12 called weak, so the report says which source ran.

Exit codes: 0 every k at or above the target; 1 a k under it; 2 the check could not run (no engine support for
``chash_allow``, empty or incompletely listed collection, too few queries, or an engine or network error); 3 vacuous
(collection not above the threshold).

    uv run python scripts/prose_index_recall_check.py docs__1-2__voyage-context-3__v1 --queries-file q.txt
    uv run python scripts/prose_index_recall_check.py <collection> --sample 20 --seed 7 --dry-run
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: RDR-227 Gap 1: the page sizes the recall is read at, and the bar.
KS: tuple[int, ...] = (40, 60, 80, 100, 120)
TARGET = 0.99

#: ``chash_allow`` cap per request (the engine's: at most 3000 distinct hashes).
ALLOW_SLICE = 3000
#: Listing page (``QUOTAS.MAX_RECORDS_PER_WRITE`` / the engine's paging cap).
LIST_PAGE = 300
#: ``QUOTAS.MAX_QUERY_STRING_CHARS``.
MAX_QUERY_CHARS = 256
#: The engine default for ``NX_SEARCH_EXACT_MAX_ROWS``; mirrors ``nexus.health.DEFAULT_SEARCH_EXACT_MAX_ROWS``.
DEFAULT_THRESHOLD = 20_000
#: Fewer own-topic queries than this say too little to call a collection green.
MIN_QUERIES = 10
#: Distance slack for a tie at the k-th exact distance (call-to-call embedding noise).
TIE_EPS = 1e-6

EXIT_OK, EXIT_BELOW, EXIT_CANNOT_RUN, EXIT_VACUOUS = 0, 1, 2, 3


class CollectionReader(Protocol):
    """The collection handle ``HttpVectorClient.get_collection`` returns (``_ServiceCollectionStub``): its ``get``."""

    def get(self, ids: list[str] | None = None, where: dict | None = None, include: list[str] | None = None,
            limit: int | None = None, offset: int = 0, *, sensitivity: str = ...) -> dict: ...


class RecallClient(Protocol):
    """The slice of ``HttpVectorClient`` this script reads. A test passes a double with the same signatures, and a
    parity test checks them against the real client (review: an invented ``get`` hid a crash on the real one)."""

    def supports_chash_allow(self) -> bool: ...

    def list_collections(self) -> list[dict]: ...

    def list_store(self, collection: str, limit: int = 200, offset: int = 0) -> list[dict]: ...

    def get_collection(self, name: str) -> CollectionReader: ...

    def search(self, query: str, collection_names: list[str], n_results: int = 10, *,
               chash_allow: Sequence[str] | None = None, sensitivity: str) -> list[dict]: ...


class CannotRun(Exception):
    """The check cannot be made on this engine or collection; carries the reason."""


@dataclass(frozen=True)
class Hit:
    id: str
    distance: float


@dataclass
class KReport:
    k: int
    recalls: list[float] = field(default_factory=list)

    @property
    def minimum(self) -> float:
        return min(self.recalls)

    @property
    def mean(self) -> float:
        return sum(self.recalls) / len(self.recalls)


@dataclass
class Report:
    collection: str
    rows: int
    queries: int
    query_source: str
    per_k: list[KReport]
    top1_misses: int
    worst: list[tuple[str, int, float]]  # (query ordinal, k, recall) of the lowest cells

    @property
    def minimum(self) -> float:
        return min(r.minimum for r in self.per_k)

    @property
    def passed(self) -> bool:
        return self.minimum >= TARGET


# ── scoring ──────────────────────────────────────────────────────────────────────────────────────────────────────


def score_k(page: Sequence[Hit], exact: Sequence[Hit], k: int, *, eps: float = TIE_EPS) -> tuple[float, bool]:
    """Recall of the served *page* against the exact ranking at *k*, and whether it missed the exact nearest row.

    *exact* is the full exact ranking (nearest first); the first ``k`` rows are the exact top-k."""
    top = list(exact[:k])
    if not top:
        raise CannotRun("the exact arm returned no rows")
    ids = {h.id for h in top}
    kth = top[-1].distance
    served = list(page[:k])
    hits = sum(1 for h in served if h.id in ids or h.distance <= kth + eps)
    recall = min(hits, len(top)) / len(top)
    nearest = top[0]
    top1_miss = not any(h.id == nearest.id or h.distance <= nearest.distance + eps for h in served)
    return recall, top1_miss


def build_report(collection: str, rows: int, query_source: str, queries: Sequence[str],
                 pages: Sequence[dict[int, list[Hit]]], exacts: Sequence[list[Hit]], ks: Sequence[int] = KS) -> Report:
    per_k = [KReport(k) for k in ks]
    misses = 0
    cells: list[tuple[str, int, float]] = []
    for qi, (page_by_k, exact) in enumerate(zip(pages, exacts)):
        missed = False
        for kr in per_k:
            recall, top1 = score_k(page_by_k[kr.k], exact, kr.k)
            kr.recalls.append(recall)
            missed = missed or top1
            cells.append((f"q{qi + 1}", kr.k, recall))
        misses += int(missed)
    cells.sort(key=lambda c: (c[2], c[1]))
    return Report(collection, rows, len(queries), query_source, per_k, misses, cells[:3])


# ── the two arms, through the client ─────────────────────────────────────────────────────────────────────────────


def _hits(rows: list[dict]) -> list[Hit]:
    out = []
    for r in rows:
        if not isinstance(r, dict) or "id" not in r or r.get("distance") is None:
            raise CannotRun("a search row carries no id or distance; the engine answered a shape this check does not read")
        out.append(Hit(str(r["id"]), float(r["distance"])))
    return out


def list_chashes(client: RecallClient, collection: str) -> list[str]:
    """Every live chunk hash of the collection, paged."""
    ids: list[str] = []
    offset = 0
    while True:
        page = client.list_store(collection, limit=LIST_PAGE, offset=offset)
        if not page:
            break
        ids.extend(str(row["id"]) for row in page)
        offset += len(page)
        if len(page) < LIST_PAGE:
            break
    # A repeated id (a page boundary under a concurrent write) would double a hit; the allow-list needs distinct anyway.
    return list(dict.fromkeys(ids))


def live_count(client: RecallClient, collection: str) -> int | None:
    """The collection's live chunk count as the engine reports it (``count``), or ``None`` when it reports none."""
    for row in client.list_collections():
        if isinstance(row, dict) and row.get("name") == collection:
            v = row.get("count")
            if isinstance(v, int) and not isinstance(v, bool):
                return v
    return None


def physical_rows(client: RecallClient, collection: str, listed: int) -> int:
    """The router's number: the physical row count of the listing (``stored_count``, else ``count``); else *listed*."""
    for row in client.list_collections():
        if isinstance(row, dict) and row.get("name") == collection:
            for key in ("stored_count", "count"):
                v = row.get(key)
                if isinstance(v, int) and not isinstance(v, bool):
                    return v
    return listed


def index_arm(client: RecallClient, collection: str, query: str, ks: Sequence[int]) -> dict[int, list[Hit]]:
    """The served page at each k: one single-collection search per k, no allow-list, so the router chooses."""
    return {k: _hits(client.search(query, [collection], n_results=k, sensitivity="include")) for k in ks}


def exact_arm(client: RecallClient, collection: str, query: str, chashes: Sequence[str], depth: int) -> list[Hit]:
    """The exact ranking, nearest first, ``depth`` rows: allow-list slices ranked exactly, merged by distance."""
    merged: dict[str, Hit] = {}
    for start in range(0, len(chashes), ALLOW_SLICE):
        rows = client.search(query, [collection], n_results=depth, chash_allow=list(chashes[start:start + ALLOW_SLICE]),
                             sensitivity="include")
        for h in _hits(rows):
            merged.setdefault(h.id, h)
    return sorted(merged.values(), key=lambda h: (h.distance, h.id))[:depth]


# ── queries ──────────────────────────────────────────────────────────────────────────────────────────────────────


def read_queries_file(path: str) -> list[str]:
    queries: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if len(text) > MAX_QUERY_CHARS:
                raise CannotRun(f"{path}:{n}: a query is {len(text)} characters; the engine's cap is {MAX_QUERY_CHARS}")
            queries.append(text)
    return queries


def sample_queries(client: RecallClient, collection: str, chashes: Sequence[str], n: int, seed: int) -> list[str]:
    """``n`` chunks' opening text, seeded; sensitive chunks are excluded by the engine on this read."""
    rng = random.Random(seed)
    queries: list[str] = []
    pool = list(chashes)
    rng.shuffle(pool)
    reader = client.get_collection(collection)
    for start in range(0, len(pool), LIST_PAGE):
        got = reader.get(ids=pool[start:start + LIST_PAGE], include=["documents"], limit=LIST_PAGE, sensitivity="exclude")
        for text in got.get("documents") or []:
            if isinstance(text, str) and text.strip():
                queries.append(" ".join(text.split())[:MAX_QUERY_CHARS])
                if len(queries) == n:
                    return queries
    return queries


# ── the run ──────────────────────────────────────────────────────────────────────────────────────────────────────


def plan(rows: int, queries: int, ks: Sequence[int] = KS) -> dict[str, int]:
    slices = max(1, math.ceil(rows / ALLOW_SLICE))
    listing = max(1, math.ceil(rows / LIST_PAGE))
    return {"listing_requests": listing, "searches_per_query": len(ks) + slices,
            "search_requests": queries * (len(ks) + slices)}


def run_check(client: RecallClient, collection: str, *, queries_file: str | None, sample: int, seed: int,
              threshold: int, allow_exact_route: bool, dry_run: bool, out: Any = None) -> tuple[int, Report | None]:
    out = out or sys.stdout
    if (queries_file is None) == (sample <= 0):
        raise CannotRun("give exactly one of --queries-file or --sample N")
    if not client.supports_chash_allow():
        raise CannotRun("the engine does not report vector_search_chash_allow_supported; the exact arm needs it "
                        "(nexus-jypmj). Upgrade the engine, or measure by direct SQL as RDR-227 research-12 did")
    chashes = list_chashes(client, collection)
    if not chashes:
        raise CannotRun(f"{collection}: no live chunks listed (wrong name, or the collection is empty)")
    live = live_count(client, collection)
    if live is not None and len(chashes) < live:
        # review: a short listing narrows the exact arm, so served rows outside it read as hits and recall reads high
        raise CannotRun(f"{collection}: the listing returned {len(chashes):,} chunks of the {live:,} the engine counts "
                        "live; an incomplete exact arm would overstate recall. Rerun when no write is in flight")
    rows = physical_rows(client, collection, len(chashes))
    if rows <= threshold and not allow_exact_route:
        print(f"VACUOUS: {collection} holds {rows:,} rows, at or below the threshold {threshold:,}; the router runs it "
              "exact, so the index arm would be the exact arm and recall would read 1.0 whatever the index does. "
              "Nothing measured. (--allow-exact-route to rehearse; --threshold if the engine runs another value.)",
              file=out)
        return EXIT_VACUOUS, None
    if queries_file is not None:
        queries, source = read_queries_file(queries_file), f"queries file ({queries_file})"
    else:
        queries = sample_queries(client, collection, chashes, sample, seed)
        source = f"{len(queries)} chunks sampled from the collection (seed {seed}); document-to-document, weaker evidence"
    if len(queries) < MIN_QUERIES:
        raise CannotRun(f"{len(queries)} queries; at least {MIN_QUERIES} are needed to call a collection green")
    p = plan(rows, len(queries))
    print(f"plan: {collection}, {rows:,} rows ({len(chashes):,} live listed), {len(queries)} queries from {source}; "
          f"{p['listing_requests']} listing pages already read, {p['search_requests']} searches "
          f"({p['searches_per_query']} per query: {len(KS)} index arm + {p['searches_per_query'] - len(KS)} exact slices), "
          "each embedding its query on the engine", file=out)
    if dry_run:
        print("dry run: nothing sent beyond the listing", file=out)
        return EXIT_OK, None
    pages, exacts = [], []
    for query in queries:
        pages.append(index_arm(client, collection, query, KS))
        exacts.append(exact_arm(client, collection, query, chashes, max(KS)))
    report = build_report(collection, rows, source, queries, pages, exacts)
    print_report(report, out)
    return (EXIT_OK if report.passed else EXIT_BELOW), report


def print_report(r: Report, out: Any) -> None:
    print(f"recall of {r.collection} ({r.rows:,} rows) against exact, {r.queries} queries ({r.query_source})", file=out)
    for kr in r.per_k:
        print(f"  k={kr.k:<4} min {kr.minimum:.4f}  mean {kr.mean:.4f}", file=out)
    print(f"minimum {r.minimum:.4f} (target {TARGET}); queries with a top-1 miss: {r.top1_misses}", file=out)
    print("lowest cells: " + ", ".join(f"{q} k={k} {rec:.4f}" for q, k, rec in r.worst), file=out)
    print("route taken is not visible to a client: a run before the collection's index is built measures the "
          "ef_search=1000 walk", file=out)
    print("PASS" if r.passed else "FAIL: below target", file=out)


def default_threshold() -> int:
    raw = os.environ.get("NX_SEARCH_EXACT_MAX_ROWS", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_THRESHOLD
    return value if 0 < value <= 1_000_000 else DEFAULT_THRESHOLD


def threshold_source(args_threshold: int | None) -> str:
    """Where the vacuity threshold came from, for the report: the engine's own value cannot be read by a client."""
    if args_threshold is not None:
        return "--threshold"
    if os.environ.get("NX_SEARCH_EXACT_MAX_ROWS", "").strip():
        return "NX_SEARCH_EXACT_MAX_ROWS in this shell"
    return f"the default {DEFAULT_THRESHOLD}"


def main(argv: Sequence[str] | None = None, *, client: RecallClient | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("collection", help="full collection name, e.g. docs__1-2__voyage-context-3__v1")
    ap.add_argument("--queries-file", help="own-topic queries, one per line")
    ap.add_argument("--sample", type=int, default=0, help="instead, N chunks' opening text as queries")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--threshold", type=int, default=None,
                    help="the engine's NX_SEARCH_EXACT_MAX_ROWS (default: this shell's value, else 20000)")
    ap.add_argument("--allow-exact-route", action="store_true", help="run even when the collection is at or below the threshold")
    ap.add_argument("--dry-run", action="store_true", help="list, print the plan, send no search")
    ap.add_argument("--json", action="store_true", help="print the report as JSON instead of text")
    args = ap.parse_args(argv)
    if client is None:
        from nexus.db import make_t3  # noqa: PLC0415 — deferred: the module imports without a configured engine

        client = make_t3()
    threshold = args.threshold if args.threshold is not None else default_threshold()
    source = threshold_source(args.threshold)
    text_out = sys.stderr if args.json else sys.stdout
    print(f"threshold {threshold:,} (from {source}; the engine's own NX_SEARCH_EXACT_MAX_ROWS cannot be read by a "
          "client, so pass --threshold if it runs another value)", file=text_out)  # noqa: T201 — CLI output
    try:
        code, report = run_check(client, args.collection, queries_file=args.queries_file, sample=args.sample,
                                 seed=args.seed, threshold=threshold, allow_exact_route=args.allow_exact_route,
                                 dry_run=args.dry_run, out=text_out)
    except CannotRun as exc:
        print(f"cannot run: {exc}", file=sys.stderr)  # noqa: T201 — CLI output
        return EXIT_CANNOT_RUN
    except Exception as exc:  # noqa: BLE001 — review: an engine or network failure is "cannot run", never "recall below target"
        print(f"cannot run: {type(exc).__name__}: {exc}", file=sys.stderr)  # noqa: T201 — CLI output
        return EXIT_CANNOT_RUN
    if code == EXIT_OK and args.threshold is None:
        print("note: PASS was judged against a threshold this client assumed; if the engine runs a higher "
              "NX_SEARCH_EXACT_MAX_ROWS the collection may have been served exact, and recall then reads 1.0 "
              "whatever the index does", file=text_out)  # noqa: T201 — CLI output
    if args.json and report is not None:
        sys.stdout.write(json.dumps({"collection": report.collection, "rows": report.rows, "queries": report.queries,
                          "minimum": report.minimum, "target": TARGET, "top1_misses": report.top1_misses,
                          "per_k": {str(k.k): {"min": k.minimum, "mean": k.mean} for k in report.per_k},
                          "passed": report.passed}) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
