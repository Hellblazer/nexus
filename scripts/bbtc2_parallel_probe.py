#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bbtc2: which part of the gather statement keeps it from a parallel plan. Small seed; EXPLAIN only.

Runs each variant with debug_parallel_query = on (a parallel-safe query then shows a Gather even when a serial plan
is cheaper) and the function's zero parallel costs, and prints whether a Gather appears.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import bbtc2_dense_diag as d1  # noqa: E402
import bbtc2_query_e as qe  # noqa: E402
import measure_route_lexical as m  # noqa: E402


def main() -> int:
    os.environ["NEXUS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="bbtc2-cfg-")
    for k in ("VOYAGE_API_KEY", "NX_VOYAGE_API_KEY"):
        os.environ.pop(k, None)
    from tests._engine_substrate import drop_test_tenant, ensure_engine, mint_test_tenant

    state = ensure_engine()
    tenant, token = mint_test_tenant(state)
    os.environ.update({"NX_STORAGE_BACKEND": "service", "NX_SERVICE_URL": state["base_url"],
                       "NX_SERVICE_TOKEN": token, "NX_LOCAL": "1",
                       "NX_ALLOW_PROD_WRITE": "bbtc2 diagnosis writes only to the hermetic engine it booted"})
    try:
        plan = m.collection_plan(8, 1.0)
        m.seed(plan, 7)
        m.analyze(state)
        cols = [c for c, _ in plan]
        ps = qe.Psql(state, tenant)
        owner = ps.run("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname='text_gate_probe_768'")
        leaf = ps.run(f"SELECT tableoid::regclass::text FROM nexus.chunks WHERE tenant_id = {qe.lit(tenant)} LIMIT 1")
        print("[info]", ps.run(
            "SELECT json_build_object('who', current_user, 'iso', current_setting('transaction_isolation'),"
            " 'cur_setting_par', (SELECT json_agg(proparallel) FROM pg_proc WHERE proname='current_setting'),"
            " 'nullif', 1, 'ts', (SELECT json_object_agg(proname, proparallel) FROM pg_proc WHERE proname IN"
            " ('plainto_tsquery','ts_match_vq','texteq')),"
            f" 'leaf_opts', (SELECT reloptions FROM pg_class WHERE oid = {qe.lit(leaf)}::regclass),"
            " 'parent_opts', (SELECT json_agg(json_build_array(relname, reloptions, relrowsecurity, relforcerowsecurity))"
            "   FROM pg_class WHERE relname = 'chunks' AND relnamespace = 'nexus'::regnamespace),"
            " 'settings', (SELECT json_object_agg(name, setting) FROM pg_settings WHERE name LIKE '%parallel%'"
            "   OR name IN ('max_worker_processes')),"
            " 'role_cfg', (SELECT json_agg(json_build_array(setrole::regrole::text, setdatabase, setconfig))"
            "   FROM pg_db_role_setting))"), flush=True)
        q, mode = qe.QUERIES["D"], "on"
        par = ("SET LOCAL debug_parallel_query = on; SET LOCAL parallel_setup_cost = 0;"
               " SET LOCAL parallel_tuple_cost = 0; SET LOCAL min_parallel_table_scan_size = 0;\n")
        arr = "ANY(" + qe.arr(cols) + ")"
        variants = {
            "full_gather": d1.base(tenant, arr, live=False) + " AND " + qe.gate_sql(q, mode),
            "no_gate": d1.base(tenant, arr, live=False),
            "no_tenant_setting": (f" c.embedding_model = {qe.lit(m.MODEL)} AND c.tenant_id = {qe.lit(tenant)}"
                                  f" AND c.collection = {arr} AND {qe.gate_sql(q, mode)}"),
            "only_trgm": (f" c.embedding_model = {qe.lit(m.MODEL)} AND c.tenant_id = {qe.lit(tenant)}"
                          f" AND {qe.gate_sql(q, 'trgm')}"),
            "only_tsv": (f" c.embedding_model = {qe.lit(m.MODEL)} AND c.tenant_id = {qe.lit(tenant)}"
                         f" AND {qe.gate_sql(q, 'fts')}"),
            "bare": " true",
        }
        out = {}
        for role_name, role in (("owner", owner), ("nexus_svc", "nexus_svc")):
            ps.role = role
            for k, where in variants.items():
                for tbl in ("nexus.chunks", leaf):
                    sql = f"SELECT count(*) FROM {tbl} c WHERE {where}"
                    txt = ps.run(ps.prelude(mode) + par + "EXPLAIN (COSTS OFF, VERBOSE) " + sql + ";")
                    g = "Gather" in txt
                    out[f"{role_name}/{k}/{tbl}"] = txt
                    print(f"[{role_name}] {k:20} {tbl[:40]:40} gather={g}", flush=True)
        ps.role = owner
        print("--- full_gather owner parent plan\n" + out["owner/full_gather/nexus.chunks"], flush=True)
        print("--- bare owner parent plan\n" + out["owner/bare/nexus.chunks"], flush=True)
        Path("bbtc2-parallel-probe.json").write_text(json.dumps(out, indent=1))
    finally:
        drop_test_tenant(state, tenant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
