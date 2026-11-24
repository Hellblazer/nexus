#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bbtc2: does the gather statement get a parallel plan inside PL/pgSQL? Small seed; auto_explain to the client.

debug_parallel_query = on makes any parallel-safe plan carry a Gather, so a missing Gather in a nested statement
means the plpgsql call site planned it without parallelism allowed. Shapes: plain SQL, DO block SELECT INTO,
DO block v := ARRAY(...), a SECURITY DEFINER function with SELECT INTO, the same with RETURN QUERY, and the
worktree's vectors-060 768 body.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import bbtc2_dense_diag as d1  # noqa: E402
import bbtc2_dense_diag2 as d2  # noqa: E402
import bbtc2_query_e as qe  # noqa: E402
import measure_route_lexical as m  # noqa: E402


FSET = (" SET parallel_setup_cost = 0 SET parallel_tuple_cost = 0 SET min_parallel_table_scan_size = 0"
        " SET cpu_operator_cost = 0.25")


def run_both(state: dict, sql: str) -> str:
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]), "-U", state["pg_user"],
         "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-A", "-t", "-q", "-1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=600)
    return f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"


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
        plan = m.collection_plan(120, 6.0)
        m.seed(plan, 7)
        m.analyze(state)
        cols = [c for c, _ in plan]
        ps = qe.Psql(state, tenant)
        owner = ps.run("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname='text_gate_probe_768'")
        ps.role = owner
        q, mode = qe.QUERIES["D"], "on"
        gather = d2.gather_sql(tenant, cols, q, mode)
        ps.run(d2.new_body() + ";\nGRANT EXECUTE ON FUNCTION nexus.bbtc2_p2_768(text, text[], jsonb, text, int, text,"
               " text) TO nexus_svc;")
        ps.run("CREATE FUNCTION nexus.bbtc2_into(p text[]) RETURNS int LANGUAGE plpgsql STABLE SECURITY DEFINER"
               " SET search_path = pg_catalog, pg_temp" + FSET + " AS $$ DECLARE a text[]; b bytea[]; BEGIN "
               + gather.replace("SELECT array_agg(c.collection), array_agg(c.chash) FROM",
                                "SELECT array_agg(c.collection), array_agg(c.chash) INTO a, b FROM")
               + "; RETURN coalesce(cardinality(a), 0); END $$;")
        ps.run("CREATE FUNCTION nexus.bbtc2_rq(p text[]) RETURNS TABLE(n bigint) LANGUAGE plpgsql STABLE SECURITY"
               " DEFINER SET search_path = pg_catalog, pg_temp" + FSET + " AS $$ BEGIN RETURN QUERY "
               + gather.replace("SELECT array_agg(c.collection), array_agg(c.chash) FROM", "SELECT count(*) FROM")
               + "; END $$;")
        ps.run("CREATE FUNCTION nexus.bbtc2_dyn(p text[]) RETURNS int LANGUAGE plpgsql STABLE SECURITY DEFINER"
               " SET search_path = pg_catalog, pg_temp" + FSET + " AS $$ DECLARE a text[]; b bytea[]; BEGIN EXECUTE "
               + qe.lit(gather) + " INTO a, b; RETURN coalesce(cardinality(a), 0); END $$;")
        ae = ("SET LOCAL parallel_setup_cost = 0; SET LOCAL parallel_tuple_cost = 0;"
              " SET LOCAL min_parallel_table_scan_size = 0; SET LOCAL cpu_operator_cost = 0.25;\n")
        fset = (" SET parallel_setup_cost = 0 SET parallel_tuple_cost = 0 SET min_parallel_table_scan_size = 0"
                " SET cpu_operator_cost = 0.25")
        pre = ps.prelude(mode).replace(f"SET LOCAL ROLE {owner};\n", "")
        shapes = {
            "plain": pre + ae + gather + ";",
            "do_select_into": pre + ae + "DO $d$ DECLARE a text[]; b bytea[]; BEGIN "
                + gather.replace("FROM nexus.chunks", "INTO a, b FROM nexus.chunks", 1) + "; END $d$;",
            "do_array_expr": pre + ae + "DO $d$ DECLARE a text[]; BEGIN a := ARRAY(SELECT c.collection FROM"
                + gather.split(" FROM", 1)[1] + "); END $d$;",
            "definer_select_into": pre + ae + f"SELECT nexus.bbtc2_into({qe.arr(cols)});",
            "definer_return_query": pre + ae + f"SELECT * FROM nexus.bbtc2_rq({qe.arr(cols)});",
            "p2_body_as_svc": pre + "SET LOCAL ROLE nexus_svc;\n" + ae
                + f"SELECT count(*) FROM nexus.bbtc2_p2_768({qe.lit(q)}, {qe.arr(cols)}, NULL, NULL, 5001,"
                  f" {qe.lit(m.MODEL)}, {qe.lit(tenant)});",
        }
        shapes["definer_dynamic_execute_into"] = pre + ae + f"SELECT nexus.bbtc2_dyn({qe.arr(cols)});"
        shapes["do_dynamic_execute_into"] = (pre + ae + "DO $d$ DECLARE a text[]; b bytea[]; BEGIN EXECUTE "
                                             + qe.lit(gather) + " INTO a, b; END $d$;")
        import time
        for k, sql in shapes.items():
            ts = []
            for _ in range(2):
                t = time.perf_counter()
                out = run_both(state, sql)
                ts.append(round((time.perf_counter() - t) * 1000))
            print(f"=== {k}: wall_ms={ts} {out.splitlines()[0]} {out[-300:].strip()[:300] if 'rc=0' not in out else ''}",
                  flush=True)
    finally:
        drop_test_tenant(state, tenant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
