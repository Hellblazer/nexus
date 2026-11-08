#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bbtc2: does the per-collection route's ``lexical`` field cost no more than ``/hybrid-search``?

A LOCAL-SUBSTRATE harness (it boots the session engine of ``tests/_engine_substrate.py``: the bundled
PG17 + the shaded service JAR, so run ``scripts/build-gate-jar.sh`` first). It mints its own tenant,
seeds synthetic collections with repo text, and drives ``search_cross_corpus(lexical=True)`` the way a
``--lexical`` search does, on three paths with ONE IDENTICAL query string:

- ``hybrid``: ``NX_SEARCH_ROUTE_LEXICAL`` unset (the shipped default): the route without the field,
  then the lexical leg on ``/hybrid-search``;
- ``route``: ``NX_SEARCH_ROUTE_LEXICAL=1``: the route with ``lexical: true``, one lexical arm per
  collection engine-side;
- ``vector``: ``lexical=False``, the reference with no lexical work at all.

Each (collection count, query, path) gets ``--warmup`` discarded runs then ``--runs`` timed ones, the
paths interleaved round by round so drift hits all alike. Prints the per-cell medians and p95, then
the per-size medians over the whole query set, and writes the raw runs to ``--out`` as JSON.

NOT a pytest test and not collected: it mints a tenant only when run by hand, so the default path
mints none (``scripts/tenant_delta.py`` stays +0).

    scripts/build-gate-jar.sh
    uv run python scripts/measure_route_lexical.py --sizes 5,30,120 --runs 7 --warmup 2 --out out.json

Run it on a lab host through ``lab-remote --host qwent -c lab-4c8g``, never on chas.

Corpus: ``--max-collections`` collections named ``<kind>__bbtc2cN__bge-base-en-v15-768__v1`` with kind
cycling code, docs, knowledge, rdr; row counts cycle through ``ROW_PROFILE`` times ``--scale``; chunk
text is the repo's own files cut to ~1500 characters (code kinds from source files, the others from
markdown), each collection reading its own window of the pool; vectors are seeded and rounded. A search
over ``N`` collections reads the first ``N``, so every prefix has the same size mix. ``ANALYZE`` runs
after seeding, as a live tenant has statistics. Every chunk gets a live owner (a manifest row), or the
engine's reads would not see it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

MODEL = "bge-base-en-v15-768"
KINDS = ("code", "docs", "knowledge", "rdr")
#: Rows per collection, cycled. Mean 601: small and medium collections dominate, as in the live corpus
#: (88 of 118 collections held ~114k chunks), with a heavier tail than a tenant's median.
ROW_PROFILE = (50, 100, 160, 250, 400, 650, 1000, 2200)
CHUNK_CHARS = 1500
DIM = 768
INSERT_BATCH = 400
OWNER_BATCH = 1000
N_RESULTS = 30  # the MCP default fetch, as nexus-pq3wr measured it

#: Same strings on every path. Short ones have a handful of lexemes; long ones are sentences, which is
#: where the trigram GIN stops pruning (analysis-deep-route-lexical-arm-cost-2026-10-22).
QUERIES: dict[str, str] = {
    "short-1": "advisory lock",
    "short-2": "manifest search",
    "short-3": "tenant token",
    "long-1": "the engine runs the text gate once per collection arm and the trigram leg decides most of the cost",
    "long-2": "a chunk with no live owner in its own collection is invisible to every content read of the service",
    "long-3": "how does the client fall back to the batched search path when the per collection route is absent",
}


def pool_texts(kind: str, need: int) -> list[str]:
    """Chunks of ~CHUNK_CHARS from the repo's own files: source for ``code``, markdown for the rest."""
    exts = {".py", ".java", ".sql", ".xml"} if kind == "code" else {".md"}
    skip = {".git", ".venv", "target", "node_modules", "archive", "data", "web", "papers", "__pycache__"}
    files: list[Path] = []
    for p in sorted(REPO.rglob("*")):
        if p.suffix in exts and p.is_file() and not (set(p.relative_to(REPO).parts) & skip):
            files.append(p)
    out: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore").replace("\x00", "")
        except OSError:
            continue
        for i in range(0, len(text), CHUNK_CHARS):
            piece = text[i:i + CHUNK_CHARS].strip()
            if len(piece) > 200:
                out.append(piece)
        if len(out) >= need:
            break
    if not out:
        raise SystemExit(f"no {kind} text under {REPO}")
    return out


def collection_plan(n: int, scale: float) -> list[tuple[str, int]]:
    return [
        (f"{KINDS[i % len(KINDS)]}__bbtc2c{i}__{MODEL}__v1",
         max(10, int(ROW_PROFILE[i % len(ROW_PROFILE)] * scale)))
        for i in range(n)
    ]


def percentile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    k = max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))
    return s[k]


def seed(plan: list[tuple[str, int]], seed_value: int) -> dict[str, int]:
    """Register, fill and give a live owner to every planned collection; return rows per collection."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from tests._catalog_fixture_ops import give_chunks_a_live_owner
    from tests._chunk_seed import seed_chunks_direct

    rng = random.Random(seed_value)
    vec_pool = [[round(rng.uniform(-1, 1), 3) for _ in range(DIM)] for _ in range(64)]
    pools = {"code": pool_texts("code", 60_000), "docs": pool_texts("docs", 60_000)}
    counts: dict[str, int] = {}
    t0 = time.perf_counter()
    for idx, (name, rows) in enumerate(plan):
        pool = pools["code" if name.startswith("code__") else "docs"]
        start = rng.randrange(len(pool))
        texts = [pool[(start + j) % len(pool)] for j in range(rows)]
        # chash = sha256 of the text, as the engine keys it; repeated pool text in one collection
        # collapses (first wins), so de-duplicate here and count what landed.
        seen: dict[str, str] = {}
        for t in texts:
            seen.setdefault(hashlib.sha256(t.encode()).hexdigest(), t)
        ids = list(seen)
        docs = [seen[i] for i in ids]
        for b in range(0, len(ids), INSERT_BATCH):
            sl = slice(b, b + INSERT_BATCH)
            seed_chunks_direct(
                name, ids=ids[sl], documents=docs[sl],
                metadatas=[{"chunk_text_hash": c, "title": f"bbtc2-{idx}-{b + k}.txt:1-9"}
                           for k, c in enumerate(ids[sl])],
                embeddings=[vec_pool[(b + k + idx) % len(vec_pool)] for k in range(len(ids[sl]))],
            )
        ctype = "code" if name.startswith("code__") else "prose"
        for b in range(0, len(ids), OWNER_BATCH):
            give_chunks_a_live_owner(name, ids[b:b + OWNER_BATCH], content_type=ctype,
                                     owner_name=f"bbtc2-owner-{idx}")
        counts[name] = len(ids)
        if idx % 10 == 9 or idx == len(plan) - 1:
            print(f"[seed] {idx + 1}/{len(plan)} collections, {sum(counts.values())} rows, "
                  f"{time.perf_counter() - t0:.0f}s", flush=True)
    del HttpCatalogClient
    return counts


def analyze(state: dict) -> None:
    from tests._chunk_seed import _psql_superuser

    _psql_superuser(state, "ANALYZE nexus.chunks;")


def make_search(client, cols: list[str], query: str, path: str):
    """One timed search; returns (wall_s, rows, hybrid_calls, route_lexical_served, error)."""
    from nexus.db import http_vector_client as hvc
    from nexus.search_engine import search_cross_corpus

    counts = {"hybrid": 0, "route_lex": 0}
    real_route, real_hybrid = client.search_per_collection, client.hybrid_search

    def _route(q, names, **kw):
        out = real_route(q, names, **kw)
        if out is not None and out.get("lexical") is True:
            counts["route_lex"] += 1
        return out

    def _hybrid(*a, **kw):
        counts["hybrid"] += 1
        return real_hybrid(*a, **kw)

    client.search_per_collection, client.hybrid_search = _route, _hybrid
    try:
        if path == "route":
            os.environ[hvc.ROUTE_LEXICAL_ENV] = "1"
        else:
            os.environ.pop(hvc.ROUTE_LEXICAL_ENV, None)
        err = None
        rows = 0
        t0 = time.perf_counter()
        try:
            rows = len(search_cross_corpus(
                query, cols, N_RESULTS, client, cluster_by=None, rerank=False,
                lexical=(path != "vector"), threshold_override=float("inf"), sensitivity="include",
            ))
        except Exception as exc:  # noqa: BLE001 - the search's own failure is a measurement
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
        wall = time.perf_counter() - t0
    finally:
        client.search_per_collection, client.hybrid_search = real_route, real_hybrid
        os.environ.pop(hvc.ROUTE_LEXICAL_ENV, None)
    return wall, rows, counts["hybrid"], counts["route_lex"], err


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1], formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--sizes", default="5,30,120", help="collection counts to search over (default 5,30,120)")
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--scale", type=float, default=1.0, help="multiply ROW_PROFILE (default 1.0)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--queries", default=",".join(QUERIES), help="subset of: " + ",".join(QUERIES))
    ap.add_argument("--out", type=Path, default=None, help="write the raw runs here as JSON")
    args = ap.parse_args(argv)

    sizes = sorted({int(s) for s in args.sizes.split(",")})
    qnames = [q for q in args.queries.split(",") if q]
    for q in qnames:
        if q not in QUERIES:
            raise SystemExit(f"unknown query {q!r}")
    plan = collection_plan(max(sizes), args.scale)

    from tests._engine_substrate import drop_test_tenant, ensure_engine, mint_test_tenant

    state = ensure_engine()
    tenant, token = mint_test_tenant(state)
    os.environ.update({
        "NX_STORAGE_BACKEND": "service", "NX_SERVICE_URL": state["base_url"],
        "NX_SERVICE_TOKEN": token, "NX_LOCAL": "1",
        # The write guard presumes a dev-checkout write hits the operator's real store. This one hits the
        # hermetic engine this very process booted on a random localhost port (ensure_engine above).
        "NX_ALLOW_PROD_WRITE": "bbtc2 measurement harness writes only to the hermetic session engine it just booted",
    })
    try:
        import httpx

        ver = httpx.get(f"{state['base_url']}/version", timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        ver = {"error": str(exc)}
    print(f"[engine] /version {json.dumps(ver)[:400]}", flush=True)
    try:
        counts = seed(plan, args.seed)
        analyze(state)
        from nexus.db.http_vector_client import HttpVectorClient

        client = HttpVectorClient(tenant=tenant)
        raw: list[dict] = []
        for n in sizes:
            cols = [c for c, _ in plan[:n]]
            rows_in = sum(counts[c] for c in cols)
            print(f"[size {n}] {len(cols)} collections, {rows_in} rows", flush=True)
            for qn in qnames:
                q = QUERIES[qn]
                for rnd in range(args.warmup + args.runs):
                    order = ["hybrid", "route", "vector"]
                    if rnd % 2:
                        order.reverse()
                    for path in order:
                        wall, rows, hyb, rlex, err = make_search(client, cols, q, path)
                        raw.append({
                            "size": n, "rows_in_corpus": rows_in, "query": qn, "path": path,
                            "round": rnd, "warmup": rnd < args.warmup, "wall_s": wall, "rows": rows,
                            "hybrid_calls": hyb, "route_lexical_served": rlex, "error": err,
                        })
        summarize(raw, sizes, qnames, ver)
        if args.out:
            args.out.write_text(json.dumps({"version": ver, "plan_rows": counts, "runs": raw}, indent=1))
            print(f"[out] {args.out}")
    finally:
        drop_test_tenant(state, tenant)
    return 0


def summarize(raw: list[dict], sizes: list[int], qnames: list[str], ver: dict) -> None:
    timed = [r for r in raw if not r["warmup"]]
    errs = [r for r in timed if r["error"]]
    print(f"\n== per cell (timed runs only; {len(errs)} errored runs of {len(timed)})")
    print(f"{'size':>4} {'query':8} {'path':7} {'n':>2} {'median_s':>9} {'p95_s':>8} {'rows':>5} {'hyb':>4} {'rte-lex':>7}")
    for n in sizes:
        for qn in qnames:
            for path in ("hybrid", "route", "vector"):
                cell = [r for r in timed if r["size"] == n and r["query"] == qn and r["path"] == path and not r["error"]]
                if not cell:
                    continue
                w = [r["wall_s"] for r in cell]
                print(f"{n:>4} {qn:8} {path:7} {len(w):>2} {statistics.median(w):>9.3f} {percentile(w, 0.95):>8.3f} "
                      f"{statistics.median(r['rows'] for r in cell):>5.0f} "
                      f"{statistics.median(r['hybrid_calls'] for r in cell):>4.0f} "
                      f"{statistics.median(r['route_lexical_served'] for r in cell):>7.0f}")
    print("\n== per size, all queries pooled; ratio = route / hybrid")
    print(f"{'size':>4} {'group':6} {'hybrid_med':>10} {'route_med':>9} {'vector_med':>10} {'ratio':>6} "
          f"{'hybrid_p95':>10} {'route_p95':>9}")
    for n in sizes:
        for group, names in (("short", [q for q in qnames if q.startswith("short")]),
                             ("long", [q for q in qnames if q.startswith("long")]),
                             ("all", qnames)):
            if not names:
                continue
            by = {p: [r["wall_s"] for r in timed if r["size"] == n and r["query"] in names and r["path"] == p
                      and not r["error"]] for p in ("hybrid", "route", "vector")}
            if not all(by.values()):
                continue
            h, r_, v = (statistics.median(by[p]) for p in ("hybrid", "route", "vector"))
            print(f"{n:>4} {group:6} {h:>10.3f} {r_:>9.3f} {v:>10.3f} {r_ / h:>6.2f} "
                  f"{percentile(by['hybrid'], 0.95):>10.3f} {percentile(by['route'], 0.95):>9.3f}")
    print(f"\nengine: {ver.get('release_version')} build_ref={ver.get('build_ref')}")


if __name__ == "__main__":
    raise SystemExit(main())
