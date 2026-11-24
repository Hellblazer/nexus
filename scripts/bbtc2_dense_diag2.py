#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bbtc2 dense-leaf diagnosis, round 2: the two-step parallel by-collection body against the committed one.

Analysis harness, not a test. Seeds the same corpus, creates the worktree's vectors-060 768 definer body under a
scratch name (nexus.bbtc2_p2_768), and for D/C/E compares its rows with the live function's, times both as
nexus_svc, and EXPLAINs the gather step with the function's planner settings. Also EXPLAINs (no ANALYZE) the
by-collection SELECT over 30- and 60-collection groups, to see whether the planner ANDs the pkey bitmap in.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import bbtc2_dense_diag as d1  # noqa: E402
import bbtc2_query_e as qe  # noqa: E402
import measure_route_lexical as m  # noqa: E402

XML = ROOT / "service/src/main/resources/db/changelog/vectors-060-text-gate-probe-by-collection.xml"


def body_as(xml: str, name: str) -> str:
    mt = re.search(r"\$f\$(CREATE FUNCTION nexus\.text_gate_probe_by_collection_768\(.*?\n\$\$)\$f\$;", xml, re.S)
    assert mt and "SECURITY DEFINER" in mt.group(1)
    return mt.group(1).replace("nexus.text_gate_probe_by_collection_768(", f"nexus.{name}(", 1)


def new_body() -> str:
    return body_as(XML.read_text(), "bbtc2_p2_768")


def old_body() -> str:
    import subprocess
    xml = subprocess.run(["git", "-C", str(ROOT), "show", "0db9112f8:" + str(XML.relative_to(ROOT))],
                         capture_output=True, text=True, check=True).stdout
    return body_as(xml, "bbtc2_p1_768")


def gather_sql(tenant: str, cols: list[str], q: str, mode: str) -> str:
    return (f"SELECT array_agg(c.collection), array_agg(c.chash) FROM nexus.chunks c"
            f" WHERE {d1.base(tenant, 'ANY(' + qe.arr(cols) + ')', live=False)} AND {qe.gate_sql(q, mode)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collections", type=int, default=120)
    ap.add_argument("--scale", type=float, default=6.0)
    ap.add_argument("--out", type=Path, required=True)
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
    res: dict = {}

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
        ps.role = ps.run("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname='text_gate_probe_768'")
        for fn, body in (("bbtc2_p2_768", new_body()), ("bbtc2_p1_768", old_body())):
            ps.run(body + f";\nGRANT EXECUTE ON FUNCTION nexus.{fn}(text, text[], jsonb, text, int, text, text)"
                   " TO nexus_svc;")
        print(f"[fn] created nexus.bbtc2_p1_768 (0db9112f8 body) and bbtc2_p2_768 (tree) as {ps.role}", flush=True)

        def svc_rows(fn: str, q: str, mode: str) -> tuple[float, list]:
            pre = ps.prelude(mode).replace(f"SET LOCAL ROLE {ps.role};\n", "") + "SET LOCAL ROLE nexus_svc;\n"
            sql = (f"SELECT coalesce(json_agg(json_build_array(collection, encode(chash, 'hex'))), '[]') FROM"
                   f" nexus.{fn}({qe.lit(q)}, {qe.arr(cols)}, NULL, NULL, {d1.CAP}, {qe.lit(m.MODEL)},"
                   f" {qe.lit(tenant)})")
            t = time.perf_counter()
            out = ps.run(pre + sql + ";")
            return (time.perf_counter() - t) * 1000, json.loads(out)

        for qn in ("D", "C", "E"):
            q = qe.QUERIES[qn]
            mode = qe.trgm_mode(q)
            r: dict = {}
            res[qn] = r
            old_t, new_t, same = [], [], None
            for i in range(3):
                to, ro = svc_rows("bbtc2_p1_768", q, mode)
                tn, rn = svc_rows("bbtc2_p2_768", q, mode)
                old_t.append(round(to))
                new_t.append(round(tn))
                eq = sorted(map(tuple, ro)) == sorted(map(tuple, rn))
                same = eq if same is None else (same and eq)
                if i == 0:
                    r["rows_old"], r["rows_new"] = len(ro), len(rn)
            r.update({"old_ms": old_t, "new_ms": new_t, "same_rows": same})
            print(f"== {qn} {q!r} mode={mode}: committed fn {old_t} ms, two-step fn {new_t} ms,"
                  f" rows {r['rows_old']}/{r['rows_new']} identical={same}", flush=True)
            par = ("SET LOCAL parallel_setup_cost = 0; SET LOCAL parallel_tuple_cost = 0;"
                   " SET LOCAL min_parallel_table_scan_size = 0; SET LOCAL cpu_operator_cost = 0.25;\n")
            r["gather_plan"] = d1_explain(ps, gather_sql(tenant, cols, q, mode), mode, par)
            r["gather_plan_serial"] = d1_explain(ps, gather_sql(tenant, cols, q, mode), mode, "")
            print(f"-- gather plan {qn} (function settings)\n{short(r['gather_plan'])}\n-- serial exec_ms="
                  f"{d1.exec_ms(r['gather_plan_serial'])}", flush=True)
            if qn == "D":
                for g in (30, 60):
                    grp = cols[:g]
                    txt = ps.run(ps.prelude(mode) + "EXPLAIN " + d1.by_coll_sql(tenant, grp, q, mode) + ";")
                    r[f"group{g}_plan"] = txt
                    print(f"-- group {g} plan nodes: {[ln.strip().split('  (')[0] for ln in txt.splitlines() if 'Bitmap' in ln]}",
                          flush=True)
            save()
    finally:
        save()
        drop_test_tenant(state, tenant)
    return 0


def d1_explain(ps: qe.Psql, sql: str, mode: str, extra: str) -> str:
    return ps.run(ps.prelude(mode) + extra + "EXPLAIN (ANALYZE, BUFFERS) " + sql + ";")


def short(plan: str) -> str:
    keep = []
    for ln in plan.splitlines():
        if "Filter:" in ln or "Recheck Cond" in ln or "Index Cond" in ln:
            ln = ln[:160]
        keep.append(ln)
    return "\n".join(keep)


if __name__ == "__main__":
    raise SystemExit(main())
