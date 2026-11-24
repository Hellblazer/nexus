#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bbtc2 dense-leaf diagnosis: where text_gate_probe_by_collection_768 spends its time for D/E/C.

Analysis harness, not a test. Boots the hermetic engine, seeds the bbtc2_query_e corpus (same plan and seed),
then for each query runs EXPLAIN (ANALYZE, BUFFERS) of the by-collection function's inner SELECT and of the
single capped probe, per-collection gate counts, the gate halves alone, candidate bound shapes, and the function
itself timed as nexus_svc (alone and split into concurrent groups).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bbtc2_query_e as qe  # noqa: E402
import measure_route_lexical as m  # noqa: E402

CAP = 5001


def base(tenant: str, cols_expr: str, live: bool = True) -> str:
    s = (f" c.embedding_model = {qe.lit(m.MODEL)} AND c.tenant_id = {qe.lit(tenant)}"
         " AND c.tenant_id = NULLIF(pg_catalog.current_setting('nexus.tenant', true), '')"
         f" AND c.collection = {cols_expr}")
    if live:
        s += " AND EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))"
    return s


def by_coll_sql(tenant: str, cols: list[str], q: str, mode: str) -> str:
    return (
        "WITH g AS (SELECT c.collection AS coll, c.chash AS ch, row_number() OVER (PARTITION BY c.collection) AS rn"
        f" FROM nexus.chunks c WHERE {base(tenant, 'ANY(' + qe.arr(cols) + ')')} AND {qe.gate_sql(q, mode)}),"
        f" capped AS (SELECT g.coll, g.ch, g.rn, count(*) OVER (PARTITION BY g.coll) AS n FROM g WHERE g.rn <= {CAP})"
        f" SELECT x.coll, CASE WHEN x.n < {CAP} THEN x.ch END FROM capped x WHERE x.n < {CAP} OR x.rn = {CAP}")


def lateral_sql(tenant: str, cols: list[str], q: str, mode: str) -> str:
    return (
        f"SELECT u.coll, x.ch FROM unnest({qe.arr(cols)}) u(coll) CROSS JOIN LATERAL ("
        f"SELECT c.chash AS ch FROM nexus.chunks c WHERE {base(tenant, 'u.coll')} AND {qe.gate_sql(q, mode)}"
        f" LIMIT {CAP}) x")


def counts_sql(tenant: str, cols: list[str], q: str, mode: str) -> str:
    fts = qe.gate_sql(q, "fts")
    half = qe.gate_sql(q, "ilike" if mode == "substring" else "trgm") if mode != "off" else "false"
    return (
        "SELECT json_agg(x) FROM (SELECT c.collection AS coll, count(*) AS n,"
        f" count(*) FILTER (WHERE {fts}) AS n_tsv, count(*) FILTER (WHERE {half}) AS n_half,"
        f" count(*) FILTER (WHERE {qe.gate_sql(q, mode)}) AS n_gate"
        f" FROM nexus.chunks c WHERE {base(tenant, 'ANY(' + qe.arr(cols) + ')')} GROUP BY 1) x")


def fn_sql(tenant: str, cols: list[str], q: str) -> str:
    return (f"SELECT count(*) FROM nexus.text_gate_probe_by_collection_768({qe.lit(q)}, {qe.arr(cols)}, NULL, NULL,"
            f" {CAP}, {qe.lit(m.MODEL)}, {qe.lit(tenant)})")


def exec_ms(plan_text: str) -> float | None:
    mm = re.search(r"Execution Time: ([0-9.]+) ms", plan_text)
    return float(mm.group(1)) if mm else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collections", type=int, default=120)
    ap.add_argument("--scale", type=float, default=6.0)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--queries", default="D,C,E")
    args = ap.parse_args()
    os.environ["NEXUS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="bbtc2-cfg-")
    for k in ("VOYAGE_API_KEY", "NX_VOYAGE_API_KEY"):
        os.environ.pop(k, None)
    from tests._engine_substrate import drop_test_tenant, ensure_engine, mint_test_tenant

    state = ensure_engine()
    tenant, token = mint_test_tenant(state)
    os.environ.update({"NX_STORAGE_BACKEND": "service", "NX_SERVICE_URL": state["base_url"],
                       "NX_SERVICE_TOKEN": token, "NX_LOCAL": "1",
                       "NX_ALLOW_PROD_WRITE": "bbtc2 diagnosis writes only to the hermetic engine it booted"})
    res: dict = {"args": vars(args) | {"out": str(args.out)}}

    def save() -> None:
        args.out.write_text(json.dumps(res, indent=1, default=str))

    try:
        plan = m.collection_plan(args.collections, args.scale)
        t0 = time.perf_counter()
        counts = m.seed(plan, 7)
        print(f"[seed] {sum(counts.values())} rows in {time.perf_counter() - t0:.0f}s", flush=True)
        m.analyze(state)
        cols = [c for c, _ in plan]
        ps = qe.Psql(state, tenant)
        owner = json.loads(ps.run("SELECT json_build_object('o', (SELECT pg_get_userbyid(proowner) FROM pg_proc"
                                  " WHERE proname='text_gate_probe_768'), 'par', (SELECT json_object_agg(proname,"
                                  " proparallel) FROM pg_proc WHERE proname IN ('chunk_live_owners',"
                                  " 'text_gate_probe_by_collection_768','word_similarity_commutator_op',"
                                  " 'word_similarity_op')), 'set', (SELECT json_object_agg(name, setting) FROM"
                                  " pg_settings WHERE name IN ('max_parallel_workers','max_parallel_workers_per_gather',"
                                  " 'work_mem','shared_buffers','jit','parallel_setup_cost','min_parallel_table_scan_size')))"))
        ps.role = owner["o"]
        res["info"] = owner
        print(f"[info] {json.dumps(owner)}", flush=True)

        def explain(sql: str, mode: str, extra: str = "") -> str:
            return ps.run(ps.prelude(mode) + extra + "EXPLAIN (ANALYZE, BUFFERS, SETTINGS) " + sql + ";")

        def svc(sql: str, mode: str) -> float:
            pre = ps.prelude(mode).replace(f"SET LOCAL ROLE {ps.role};\n", "") + "SET LOCAL ROLE nexus_svc;\n"
            t = time.perf_counter()
            ps.run(pre + sql + ";")
            return (time.perf_counter() - t) * 1000

        groups_by_n: dict[int, list[list[str]]] = {}
        for g in (2, 4):
            buckets: list[list[str]] = [[] for _ in range(g)]
            load = [0] * g
            for c in sorted(cols, key=lambda c: -counts[c]):
                i = load.index(min(load))
                buckets[i].append(c)
                load[i] += counts[c]
            groups_by_n[g] = buckets

        for qn in args.queries.split(","):
            q = qe.QUERIES[qn]
            mode = qe.trgm_mode(q)
            r: dict = {"q": q, "mode": mode}
            res[qn] = r
            cnt = json.loads(ps.run(ps.prelude(mode) + counts_sql(tenant, cols, q, mode) + ";") or "[]")
            r["counts"] = cnt
            dense = [x for x in cnt if x["n_gate"] >= CAP]
            dense_tsv = [x for x in cnt if x["n_tsv"] >= CAP]
            print(f"\n== {qn} {q!r} mode={mode}: rows={sum(x['n'] for x in cnt)} gated={sum(x['n_gate'] for x in cnt)}"
                  f" tsv={sum(x['n_tsv'] for x in cnt)} half={sum(x['n_half'] for x in cnt)}"
                  f" dense={len(dense)} (rows in dense colls {sum(x['n'] for x in dense)}, gated there"
                  f" {sum(x['n_gate'] for x in dense)}) dense_by_tsv_alone={len(dense_tsv)}", flush=True)
            top = sorted(cnt, key=lambda x: -x["n_gate"])[:8]
            print("  top gated: " + ", ".join(f"{x['coll'].split('__')[1]} n={x['n']} gate={x['n_gate']}"
                                             f" tsv={x['n_tsv']}" for x in top), flush=True)
            plans = {}
            plans["by_collection"] = explain(by_coll_sql(tenant, cols, q, mode), mode)
            plans["by_collection_jit_off"] = explain(by_coll_sql(tenant, cols, q, mode), mode, "SET LOCAL jit = off;\n")
            plans["single_capped"] = explain(qe.probe_sql(tenant, cols, q, mode), mode)
            plans["gate_count_no_exists"] = explain(
                f"SELECT count(*) FROM nexus.chunks c WHERE {base(tenant, 'ANY(' + qe.arr(cols) + ')', live=False)}"
                f" AND {qe.gate_sql(q, mode)}", mode)
            plans["gate_count"] = explain(
                f"SELECT count(*) FROM nexus.chunks c WHERE {base(tenant, 'ANY(' + qe.arr(cols) + ')')}"
                f" AND {qe.gate_sql(q, mode)}", mode)
            plans["tsv_count"] = explain(
                f"SELECT count(*) FROM nexus.chunks c WHERE {base(tenant, 'ANY(' + qe.arr(cols) + ')')}"
                f" AND {qe.gate_sql(q, 'fts')}", mode)
            if mode != "off":
                half = "ilike" if mode == "substring" else "trgm"
                plans["half_count"] = explain(
                    f"SELECT count(*) FROM nexus.chunks c WHERE {base(tenant, 'ANY(' + qe.arr(cols) + ')')}"
                    f" AND {qe.gate_sql(q, half)}", mode)
                plans["gate_seqscan_count"] = explain(
                    f"SELECT count(*) FROM nexus.chunks c WHERE {base(tenant, 'ANY(' + qe.arr(cols) + ')', live=False)}"
                    f" AND {qe.gate_sql(q, mode)}", mode,
                    "SET LOCAL enable_bitmapscan = off; SET LOCAL enable_indexscan = off;"
                    " SET LOCAL max_parallel_workers_per_gather = 0;\n")
                plans["total_rows_seqscan"] = explain(
                    f"SELECT count(*) FROM nexus.chunks c WHERE {base(tenant, 'ANY(' + qe.arr(cols) + ')', live=False)}",
                    mode, "SET LOCAL enable_bitmapscan = off; SET LOCAL enable_indexscan = off;"
                          " SET LOCAL max_parallel_workers_per_gather = 0;\n")
            plans["lateral_limit"] = explain(lateral_sql(tenant, cols, q, mode), mode)
            r["plans"] = plans
            for k, v in plans.items():
                print(f"  {k}: exec_ms={exec_ms(v)}", flush=True)
            print(f"\n-- plan by_collection {qn}\n{plans['by_collection']}\n-- plan single_capped {qn}\n"
                  f"{plans['single_capped']}\n-- plan half_count {qn}\n{plans.get('half_count')}", flush=True)
            fn = [svc(fn_sql(tenant, cols, q), mode) for _ in range(3)]
            r["fn_ms"] = fn
            print(f"  fn alone as nexus_svc (wall incl psql): {[round(x) for x in fn]}", flush=True)
            for g, buckets in groups_by_n.items():
                walls = []
                for _ in range(2):
                    out: list[float] = [0.0] * g
                    def one(i: int) -> None:
                        out[i] = svc(fn_sql(tenant, buckets[i], q), mode)
                    ths = [threading.Thread(target=one, args=(i,)) for i in range(g)]
                    t = time.perf_counter()
                    for th in ths:
                        th.start()
                    for th in ths:
                        th.join()
                    walls.append({"wall": (time.perf_counter() - t) * 1000, "each": out})
                r[f"fn_split{g}"] = walls
                print(f"  fn split {g} concurrent: " + "; ".join(
                    f"wall={w['wall']:.0f} each={[round(x) for x in w['each']]}" for w in walls), flush=True)
            save()
    finally:
        save()
        drop_test_tenant(state, tenant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
