#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bbtc2 query-E diagnosis: why one short lexical query is slower on the route than on /hybrid-search.

Analysis harness, not a test. Boots the hermetic session engine (scripts/build-gate-jar.sh first), seeds the
measure_route_lexical.py corpus, then:

1. times D/E/C on hybrid / route / vector (one identical query string, interleaved rounds), recording the
   client-side wall of the route call and of every /hybrid-search call, and the engine's own
   event=search_per_collection[_lexical] log lines for each run;
2. EXPLAIN (ANALYZE, BUFFERS) of the probe's inner SELECT per route arm (one collection each) and per
   hybrid batch, as the probe function's effective role, plus the probe function itself timed per arm and
   per batch in a DO block (no instrumentation overhead);
3. variants for each query over the whole set and over the largest code collection: FTS half alone, ILIKE
   half alone, unescaped ILIKE, word_similarity half.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import measure_route_lexical as m  # noqa: E402

QUERIES = {"D": "advisory lock", "E": "resolve_active_session_id", "C": "serialization"}


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def arr(xs: list[str]) -> str:
    return "ARRAY[" + ",".join(lit(x) for x in xs) + "]::text[]"


def trgm_mode(q: str) -> str:
    t = q.strip()
    if len(t.split()) > 8:
        return "off"
    return "on" if any(ch.isspace() for ch in t) else "substring"


def gate_sql(q: str, variant: str) -> str:
    fts = f"c.chunk_tsv @@ pg_catalog.plainto_tsquery('english', {lit(q)})"
    esc = (r"pg_catalog.replace(pg_catalog.replace(pg_catalog.replace(" + lit(q)
           + r", E'\\', E'\\\\'), '%', E'\\%'), '_', E'\\_')")
    ilike = f"c.chunk_text OPERATOR(pg_catalog.~~*) ('%' || {esc} || '%')"
    ilike_raw = f"c.chunk_text OPERATOR(pg_catalog.~~*) ('%' || {lit(q)} || '%')"
    trgm = f"{lit(q)} OPERATOR(nexus.<%) c.chunk_text"
    return {
        "substring": f"({fts} OR {ilike})",
        "on": f"({fts} OR {trgm})",
        "off": f"({fts})",
        "fts": f"({fts})",
        "ilike": f"({ilike})",
        "ilike_raw": f"({ilike_raw})",
        "trgm": f"({trgm})",
    }[variant]


def probe_sql(tenant: str, cols: list[str], q: str, variant: str) -> str:
    return (
        "SELECT c.chash FROM nexus.chunks c"
        f" WHERE c.embedding_model = {lit(m.MODEL)} AND c.tenant_id = {lit(tenant)}"
        " AND c.tenant_id = NULLIF(pg_catalog.current_setting('nexus.tenant', true), '')"
        f" AND c.collection = ANY({arr(cols)})"
        " AND EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))"
        f" AND {gate_sql(q, variant)} LIMIT 5001"
    )


class Psql:
    def __init__(self, state: dict, tenant: str) -> None:
        self.state, self.tenant = state, tenant
        self.role: str | None = None

    def run(self, sql: str, timeout: int = 1800) -> str:
        st = self.state
        proc = subprocess.run(
            [str(Path(st["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(st["pg_port"]), "-U", st["pg_user"],
             "-d", st["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-A", "-t", "-q", "-1", "-f", "-"],
            input=sql, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"psql: {proc.stderr[:2000]}\nSQL: {sql[:800]}")
        return proc.stdout.strip()

    def prelude(self, mode: str) -> str:
        role = f"SET LOCAL ROLE {self.role};\n" if self.role else ""
        return (f"SET LOCAL nexus.tenant = {lit(self.tenant)};\n"
                "SET LOCAL pg_trgm.word_similarity_threshold = 0.6;\n"
                f"SET LOCAL nexus.lexical_trgm = {lit(mode)};\n" + role)

    def explain(self, cols: list[str], q: str, variant: str, mode: str) -> dict:
        out = self.run(self.prelude(mode) + "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                       + probe_sql(self.tenant, cols, q, variant) + ";")
        return json.loads(out)[0]


def walk(node: dict, acc: dict) -> None:
    nt = node.get("Node Type")
    idx = node.get("Index Name")
    acc["nodes"].append(f"{nt}{'(' + idx + ')' if idx else ''}")
    for k in ("Rows Removed by Filter", "Rows Removed by Index Recheck", "Exact Heap Blocks", "Lossy Heap Blocks"):
        if k in node:
            acc[k] = acc.get(k, 0) + int(node[k]) * int(node.get("Actual Loops", 1) if k.startswith("Rows") else 1)
    if nt in ("Bitmap Index Scan",):
        acc.setdefault("bitmap_rows", {})[idx] = acc.get("bitmap_rows", {}).get(idx, 0) + int(node.get("Actual Rows", 0))
    for ch in node.get("Plans", []) or []:
        walk(ch, acc)


def plan_summary(ex: dict) -> dict:
    acc: dict = {"nodes": []}
    walk(ex["Plan"], acc)
    p = ex["Plan"]
    top_scan = next((n for n in acc["nodes"] if "Scan" in n and "Function" not in n), "?")
    return {
        "exec_ms": ex.get("Execution Time"), "plan_ms": ex.get("Planning Time"),
        "rows": p.get("Actual Rows"), "shared_hit": p.get("Shared Hit Blocks"), "shared_read": p.get("Shared Read Blocks"),
        "top_scan": top_scan, "nodes": acc["nodes"][:12],
        "removed_filter": acc.get("Rows Removed by Filter", 0), "removed_recheck": acc.get("Rows Removed by Index Recheck", 0),
        "heap_exact": acc.get("Exact Heap Blocks", 0), "heap_lossy": acc.get("Lossy Heap Blocks", 0),
        "bitmap_rows": acc.get("bitmap_rows", {}), "jit": "JIT" in ex,
    }


def time_probe_fn(ps: Psql, groups: list[list[str]], q: str, mode: str, reps: int = 3) -> list[dict]:
    """Time nexus.text_gate_probe_768 per group in a DO block (best of reps per group)."""
    body = []
    for gi, g in enumerate(groups):
        body.append(
            f"  FOR r IN 1..{reps} LOOP t0 := clock_timestamp();"
            f" SELECT count(*) INTO n FROM nexus.text_gate_probe_768({lit(q)}, {arr(g)}, NULL, NULL, 5001,"
            f" {lit(m.MODEL)}, {lit(ps.tenant)});"
            f" INSERT INTO _bbtc2_t VALUES ({gi}, r, extract(epoch FROM clock_timestamp() - t0) * 1000, n); END LOOP;")
    sql = (ps.prelude(mode).replace(f"SET LOCAL ROLE {ps.role};\n", "")
           + "CREATE TEMP TABLE _bbtc2_t(g int, r int, ms float8, n int) ON COMMIT DROP;\n"
           + "GRANT ALL ON _bbtc2_t TO PUBLIC;\n"
           + "SET LOCAL ROLE nexus_svc;\n"
           + "DO $$ DECLARE t0 timestamptz; n int; BEGIN\n" + "\n".join(body) + "\nEND $$;\n"
           + "SELECT json_agg(x) FROM (SELECT g, min(ms) AS ms, max(n) AS n FROM _bbtc2_t GROUP BY g ORDER BY g) x;")
    return json.loads(ps.run(sql) or "[]")


class LogTail:
    def __init__(self, path: str) -> None:
        self.path = path
        self.pos = os.path.getsize(path) if os.path.exists(path) else 0

    def new_events(self) -> list[dict]:
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            fh.seek(self.pos)
            text = fh.read()
            self.pos = fh.tell()
        out = []
        for line in text.splitlines():
            if "event=search_per_collection" not in line:
                continue
            ev = {}
            for tok in line[line.index("event="):].split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    ev[k] = v
            out.append(ev)
        return out


def timed_search(client, cols, query, path, tail: LogTail) -> dict:
    from nexus.db import http_vector_client as hvc
    from nexus.search_engine import search_cross_corpus

    calls: list[dict] = []
    real_route, real_hybrid = client.search_per_collection, client.hybrid_search

    def _route(q, names, **kw):
        t0 = time.perf_counter()
        out = real_route(q, names, **kw)
        calls.append({"kind": "route", "n": len(names), "s": time.perf_counter() - t0,
                      "lex": bool(out and out.get("lexical") is True)})
        return out

    def _hybrid(q, names, *a, **kw):
        t0 = time.perf_counter()
        out = real_hybrid(q, names, *a, **kw)
        calls.append({"kind": "hybrid", "n": len(names), "s": time.perf_counter() - t0, "cols": list(names),
                      "t0": t0})
        return out

    client.search_per_collection, client.hybrid_search = _route, _hybrid
    tail.new_events()
    err, rows = None, 0
    try:
        if path == "route":
            os.environ[hvc.ROUTE_LEXICAL_ENV] = "1"
        else:
            os.environ.pop(hvc.ROUTE_LEXICAL_ENV, None)
        t0 = time.perf_counter()
        try:
            rows = len(search_cross_corpus(query, cols, m.N_RESULTS, client, cluster_by=None, rerank=False,
                                           lexical=(path != "vector"), threshold_override=float("inf"),
                                           sensitivity="include"))
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
        wall = time.perf_counter() - t0
    finally:
        client.search_per_collection, client.hybrid_search = real_route, real_hybrid
        os.environ.pop(hvc.ROUTE_LEXICAL_ENV, None)
    time.sleep(0.2)
    return {"wall": wall, "rows": rows, "err": err, "calls": calls, "events": tail.new_events()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collections", type=int, default=120)
    ap.add_argument("--scale", type=float, default=3.0)
    ap.add_argument("--runs", type=int, default=9)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--skip-explain", action="store_true",
                    help="stop after the timed runs and their medians (the remeasure; the EXPLAIN sections are the diagnosis)")
    args = ap.parse_args()

    # A clean client config: the pool user's own config (Voyage models) must not reach the client or the engine.
    os.environ["NEXUS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="bbtc2-cfg-")
    for k in ("VOYAGE_API_KEY", "NX_VOYAGE_API_KEY"):
        os.environ.pop(k, None)

    from tests._engine_substrate import drop_test_tenant, ensure_engine, mint_test_tenant

    state = ensure_engine()
    tenant, token = mint_test_tenant(state)
    os.environ.update({
        "NX_STORAGE_BACKEND": "service", "NX_SERVICE_URL": state["base_url"], "NX_SERVICE_TOKEN": token,
        "NX_LOCAL": "1",
        "NX_ALLOW_PROD_WRITE": "bbtc2 measurement harness writes only to the hermetic session engine it just booted",
        # Both lexical paths compared by nexus-bbtc2 run on the per-collection route: hybrid = route (vectors) plus
        # the /hybrid-search leg, route = route with lexical arms. Without this the client takes the batched path.
        "NX_SEARCH_PER_COLLECTION": "1",
    })
    import httpx

    ver = httpx.get(f"{state['base_url']}/version", timeout=10).json()
    print(f"[engine] {json.dumps(ver)[:300]}", flush=True)
    result: dict = {"version": ver, "args": vars(args) | {"out": str(args.out)}}
    try:
        plan = m.collection_plan(args.collections, args.scale)
        t0 = time.perf_counter()
        counts = m.seed(plan, 7)
        print(f"[seed] done {sum(counts.values())} rows in {time.perf_counter() - t0:.0f}s", flush=True)
        m.analyze(state)
        result["counts"] = counts
        cols = [c for c, _ in plan]

        ps = Psql(state, tenant)
        info = json.loads(ps.run(
            "SELECT json_build_object("
            "'probe', (SELECT json_agg(json_build_object('name', proname, 'secdef', prosecdef, 'owner',"
            " pg_get_userbyid(proowner))) FROM pg_proc WHERE proname = 'text_gate_probe_768'),"
            f"'leaf', (SELECT tableoid::regclass::text FROM nexus.chunks WHERE tenant_id = {lit(tenant)} LIMIT 1),"
            "'settings', (SELECT json_object_agg(name, setting) FROM pg_settings WHERE name IN"
            " ('shared_buffers','work_mem','effective_cache_size','random_page_cost','jit','max_parallel_workers_per_gather',"
            " 'gin_fuzzy_search_limit','server_version','lc_collate','lc_ctype')),"
            "'db_collate', (SELECT datcollate FROM pg_database WHERE datname = current_database()),"
            "'db_ctype', (SELECT datctype FROM pg_database WHERE datname = current_database()))"))
        leaf = info["leaf"]
        info["indexes"] = ps.run(
            f"SELECT string_agg(pg_get_indexdef(indexrelid), E'\\n') FROM pg_index WHERE indrelid = {lit(leaf)}::regclass")
        info["leaf_size"] = ps.run(f"SELECT pg_size_pretty(pg_total_relation_size({lit(leaf)}::regclass))")
        probe = (info["probe"] or [{}])[0]
        ps.role = probe.get("owner") if probe.get("secdef") else "nexus_svc"
        info["explain_role"] = ps.role
        info["arm_permits"] = [ln for ln in Path(state["engine_log"]).read_text(errors="replace").splitlines()
                               if "search_fanout_arm_permits" in ln or "pool_size" in ln][:4]
        result["info"] = info
        print(f"[info] {json.dumps(info)[:3000]}", flush=True)

        from nexus.db.http_vector_client import HttpVectorClient

        client = HttpVectorClient(tenant=tenant)
        tail = LogTail(state["engine_log"])
        runs: list[dict] = []
        for qn, q in QUERIES.items():
            for rnd in range(args.warmup + args.runs):
                order = ["hybrid", "route", "vector"]
                if rnd % 2:
                    order.reverse()
                for path in order:
                    r = timed_search(client, cols, q, path, tail)
                    r.update({"q": qn, "path": path, "round": rnd, "warmup": rnd < args.warmup})
                    runs.append(r)
                    print(f"[run] {qn} {path:6} r{rnd} {r['wall']:.3f}s rows={r['rows']} err={r['err']}"
                          f" calls={[(c['kind'], c['n'], c.get('lex')) for c in r['calls']][:3]}", flush=True)
                    kinds = [(c["kind"], c.get("lex")) for c in r["calls"]]
                    ok = {"route": ("route", True) in kinds,
                          "hybrid": ("route", False) in kinds and any(k == "hybrid" for k, _ in kinds),
                          "vector": ("route", False) in kinds}[path]
                    if not ok and not r["err"]:
                        raise SystemExit(f"path {path} did not take the expected calls: {kinds}")
        result["runs"] = runs
        args.out.write_text(json.dumps(result, indent=1, default=str))

        print("\n== medians (timed runs)")
        for qn in QUERIES:
            line = [qn]
            for path in ("hybrid", "route", "vector"):
                w = [r["wall"] for r in runs if r["q"] == qn and r["path"] == path and not r["warmup"] and not r["err"]]
                line.append(f"{path}={statistics.median(w):.3f}" if w else f"{path}=NA")
            hyb = [r for r in runs if r["q"] == qn and r["path"] == "hybrid" and not r["warmup"]]
            rte = [r for r in runs if r["q"] == qn and r["path"] == "route" and not r["warmup"]]
            hr = [sum(c["s"] for c in r["calls"] if c["kind"] == "route") for r in hyb]
            hl = [max([c["s"] for c in r["calls"] if c["kind"] == "hybrid"] or [0]) for r in hyb]
            nb = [len([c for c in r["calls"] if c["kind"] == "hybrid"]) for r in hyb]
            line.append(f"hybrid: route_call={statistics.median(hr):.3f} slowest_lex_batch={statistics.median(hl):.3f}"
                        f" batches={statistics.median(nb)}")
            lex_sum = [int(e.get("sum_arm_ms", 0)) for r in rte for e in r["events"]
                       if e.get("event") == "search_per_collection_lexical"]
            lex_max = [int(e.get("slowest_arm_ms", 0)) for r in rte for e in r["events"]
                       if e.get("event") == "search_per_collection_lexical"]
            vec_sum = [int(e.get("sum_arm_ms", 0)) for r in rte for e in r["events"]
                       if e.get("event") == "search_per_collection_arm_phases"]
            fan = [int(e.get("fanout_ms", 0)) for r in rte for e in r["events"]
                   if e.get("event") == "search_per_collection"]
            wk = [e.get("workers") for r in rte for e in r["events"] if e.get("event") == "search_per_collection"]
            sp_kind = sorted({e.get("shared_probe", "-") for r in rte for e in r["events"]
                              if e.get("event") == "search_per_collection_lexical"})
            sp_ms = [int(e.get("shared_probe_ms", 0)) for r in rte for e in r["events"]
                     if e.get("event") == "search_per_collection_lexical"]
            if lex_sum:
                line.append(f"route: fanout_ms={statistics.median(fan)} lex_sum_arm_ms={statistics.median(lex_sum)}"
                            f" lex_slowest={statistics.median(lex_max)} vec_sum_arm_ms={statistics.median(vec_sum)}"
                            f" workers={wk[:1]} shared_probe={sp_kind} shared_probe_ms={statistics.median(sp_ms)}")
            hvec = [int(e.get("sum_arm_ms", 0)) for r in hyb for e in r["events"]
                    if e.get("event") == "search_per_collection_arm_phases"]
            hfan = [int(e.get("fanout_ms", 0)) for r in hyb for e in r["events"]
                    if e.get("event") == "search_per_collection"]
            if hfan:
                line.append(f"hybrid-route: fanout_ms={statistics.median(hfan)} vec_sum_arm_ms={statistics.median(hvec)}")
            print("  ".join(line), flush=True)

        if args.skip_explain:
            print(f"[out] {args.out}")
            return 0

        # Hybrid batches as the client made them (first timed hybrid run of E).
        hb = next(r for r in runs if r["q"] == "E" and r["path"] == "hybrid" and not r["warmup"])
        batches = [c["cols"] for c in hb["calls"] if c["kind"] == "hybrid"]
        result["hybrid_batches"] = [len(b) for b in batches]
        print(f"[batches] {len(batches)} hybrid batches, sizes {[len(b) for b in batches]}", flush=True)

        explain: dict = {}
        for qn, q in QUERIES.items():
            mode = trgm_mode(q)
            per_arm = [plan_summary(ps.explain([c], q, mode, mode)) | {"col": c, "rows_in": counts[c]} for c in cols]
            per_batch = [plan_summary(ps.explain(b, q, mode, mode)) | {"ncols": len(b)} for b in batches]
            whole = plan_summary(ps.explain(cols, q, mode, mode))
            fn_arm = time_probe_fn(ps, [[c] for c in cols], q, mode)
            fn_batch = time_probe_fn(ps, batches, q, mode)
            fn_whole = time_probe_fn(ps, [cols], q, mode)
            big = max((c for c in cols if c.startswith("code__")), key=lambda c: counts[c])
            variants = {}
            for v in ("fts", "ilike", "ilike_raw", "trgm"):
                if qn == "D" and v == "trgm":
                    continue
                variants[v] = {"whole": plan_summary(ps.explain(cols, q, v, mode)),
                               "big": plan_summary(ps.explain([big], q, v, mode))}
            scans: dict[str, int] = {}
            for a in per_arm:
                scans[a["top_scan"]] = scans.get(a["top_scan"], 0) + 1
            bscans: dict[str, int] = {}
            for a in per_batch:
                bscans[a["top_scan"]] = bscans.get(a["top_scan"], 0) + 1
            explain[qn] = {"mode": mode, "per_arm": per_arm, "per_batch": per_batch, "whole": whole,
                           "fn_arm": fn_arm, "fn_batch": fn_batch, "fn_whole": fn_whole, "variants": variants,
                           "big": big}
            agg = lambda xs, k: round(sum((x.get(k) or 0) for x in xs), 1)  # noqa: E731
            print(f"\n== EXPLAIN {qn} ({q!r}, mode={mode})")
            print(f"  route arms: n={len(per_arm)} sum_exec_ms={agg(per_arm, 'exec_ms')} sum_plan_ms={agg(per_arm, 'plan_ms')}"
                  f" rows={agg(per_arm, 'rows')} removed_filter={agg(per_arm, 'removed_filter')}"
                  f" removed_recheck={agg(per_arm, 'removed_recheck')} hit={agg(per_arm, 'shared_hit')}"
                  f" read={agg(per_arm, 'shared_read')} scans={scans}")
            print(f"  hybrid batches: n={len(per_batch)} sum_exec_ms={agg(per_batch, 'exec_ms')}"
                  f" sum_plan_ms={agg(per_batch, 'plan_ms')} rows={agg(per_batch, 'rows')}"
                  f" removed_filter={agg(per_batch, 'removed_filter')} removed_recheck={agg(per_batch, 'removed_recheck')}"
                  f" hit={agg(per_batch, 'shared_hit')} read={agg(per_batch, 'shared_read')} scans={bscans}"
                  f" max_exec_ms={max(x['exec_ms'] for x in per_batch):.1f}")
            print(f"  whole set (1 stmt): {json.dumps({k: whole[k] for k in whole if k != 'nodes'})} nodes={whole['nodes']}")
            print(f"  probe fn (no instrumentation, best of 3): arms sum={sum(x['ms'] for x in fn_arm):.1f} ms"
                  f" max={max(x['ms'] for x in fn_arm):.1f}; batches sum={sum(x['ms'] for x in fn_batch):.1f}"
                  f" max={max(x['ms'] for x in fn_batch):.1f}; whole={fn_whole[0]['ms']:.1f} ms n={fn_whole[0]['n']}")
            top = sorted(per_arm, key=lambda a: -(a["exec_ms"] or 0))[:5]
            for a in top:
                print(f"  slowest arm {a['col']} rows_in={a['rows_in']} exec={a['exec_ms']:.1f} scan={a['top_scan']}"
                      f" removed_filter={a['removed_filter']} recheck={a['removed_recheck']} bitmap={a['bitmap_rows']}")
            for v, d in variants.items():
                print(f"  variant {v}: whole exec={d['whole']['exec_ms']:.1f} rows={d['whole']['rows']}"
                      f" scan={d['whole']['top_scan']} bitmap={d['whole']['bitmap_rows']}"
                      f" recheck={d['whole']['removed_recheck']} | big({counts[big]}) exec={d['big']['exec_ms']:.1f}"
                      f" scan={d['big']['top_scan']} filter={d['big']['removed_filter']}")
        result["explain"] = explain
        # Full plan text for E, largest arm and first batch.
        mode = trgm_mode(QUERIES["E"])
        big = explain["E"]["big"]
        result["plan_text"] = {
            "E_big_arm": ps.run(ps.prelude(mode) + "EXPLAIN (ANALYZE, BUFFERS) " + probe_sql(tenant, [big], QUERIES["E"], mode)),
            "E_small_arm": ps.run(ps.prelude(mode) + "EXPLAIN (ANALYZE, BUFFERS) "
                                  + probe_sql(tenant, [min(cols, key=lambda c: counts[c])], QUERIES["E"], mode)),
            "E_batch0": ps.run(ps.prelude(mode) + "EXPLAIN (ANALYZE, BUFFERS) " + probe_sql(tenant, batches[0], QUERIES["E"], mode)),
            "D_big_arm": ps.run(ps.prelude("on") + "EXPLAIN (ANALYZE, BUFFERS) " + probe_sql(tenant, [big], QUERIES["D"], "on")),
            "C_big_arm": ps.run(ps.prelude("substring") + "EXPLAIN (ANALYZE, BUFFERS) "
                                + probe_sql(tenant, [big], QUERIES["C"], "substring")),
        }
        for k, v in result["plan_text"].items():
            print(f"\n== plan {k}\n{v}")
        args.out.write_text(json.dumps(result, indent=1, default=str))
        print(f"[out] {args.out}")
    finally:
        drop_test_tenant(state, tenant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
