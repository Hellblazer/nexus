#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Positive control for orphan_counts.sql (RDR-229 P3.1 input, P3.2): seeds one orphan row per Expand-list table (a
name naming no registry row of the tenant), or deletes exactly those rows. WRITES: a local store only, never
production, a fork or a shared store. orphan_counts.sql itself stays SELECT-only.

  python3 orphan_control.py --port N [--host 127.0.0.1] [--user postgres] [--tenant nexus] seed|unseed

Seeded rows carry the name 'p32-orphan-control__<table>' and are written with session_replication_role = replica,
so the foreign keys that keep orphans out of the FK tables do not fire (that is how an orphan would have to arrive;
a validated name FK makes the count 0 by constraint). Run it before P3.1's expand (01): a NOT VALID CHECK still checks
new rows, and an orphan's id is NULL by definition.
"""
from __future__ import annotations

import argparse
import json
import subprocess

MARK = "p32-orphan-control__"
VEC = "array_fill(0.1::real, ARRAY[{d}])::nexus.vector"
SHA = "sha256(convert_to('p32-orphan-control', 'UTF8'))"

# (table, name columns, extra values): every other NOT NULL column without a default gets a type-shaped constant
TABLES: list[tuple[str, tuple[str, ...], dict[str, str]]] = [
    ("catalog_collections", ("superseded_by",),
     {"name": f"'{MARK}registry'", "content_type": "'knowledge'", "owner_id": "'p32'",
      "embedding_model": "'voyage-code-3'", "lifecycle_state": "'live'"}),
    ("chunks", ("collection",), {"chash": SHA, "embedding_model": "'voyage-code-3'", "chunk_text": "'p32'",
                                 "embedding_1024": VEC.format(d=1024)}),
    ("taxonomy_centroids", ("collection",), {"topic_id": "0", "label": "'p32'", "embedding_model": "'voyage-code-3'",
                                             "embedding_1024": VEC.format(d=1024)}),
    ("catalog_document_chunks", ("collection",), {"doc_id": "'p32-orphan'", "position": "0", "chash": SHA,
                                                  "chunk_index": "0", "embedding_model": "'voyage-code-3'"}),
    ("chunk_orphaned_at", ("collection",), {"chash": SHA, "embedding_model": "'voyage-code-3'"}),
    ("topic_assignments", ("source_collection",), {"doc_id": SHA, "topic_id": "0", "embedding_model": "'voyage-code-3'"}),
    ("document_aspects", ("collection",), {"source_path": "'/p32/orphan'", "doc_id": "'p32-orphan'"}),
    ("topics", ("collection",), {"label": "'p32'"}),
    ("taxonomy_meta", ("collection",), {}),
    ("document_highlights", ("collection",), {"doc_id": "'p32-orphan'"}),
    ("canary_query_ring", ("collection",), {"embedding_model": "'voyage-code-3'", "embedding_1024": VEC.format(d=1024),
                                            "vector_digest": SHA}),
    ("aspect_extraction_queue", ("collection",), {}),
    ("catalog_documents", ("physical_collection",), {"tumbler": "'p32.orphan.1'", "title": "'p32'"}),
    ("chash_remap", ("source_collection", "target_collection"), {"old_id": "'p32'", "new_chash": SHA}),
    ("pdf_pipeline", ("collection",), {"content_hash": "'p32'", "pdf_path": "'/p32/orphan.pdf'", "pipeline_id": "1"}),
    ("reembed_jobs", ("source_collection", "target_collection"), {"state": "'planned'"}),
    ("relevance_log", ("collection",), {"chunk_id": "encode(" + SHA + ", 'hex')", "query": "'p32'", "action": "'click'"}),
    ("search_telemetry", ("collection",), {"query_hash": "'p32'"}),
    ("hook_failures", ("collection",), {"hook_name": "'p32'"}),
    ("gc_audit", ("collection",), {"operation": "'p32'"}),
]


def filler(t: str) -> str:
    if t.endswith("[]"):
        return "'{}'"
    if t == "text" or t.startswith("character"):
        return "'p32'"
    if t in ("integer", "bigint", "smallint", "real", "double precision") or t.startswith("numeric"):
        return "0"
    if t == "boolean":
        return "false"
    if t in ("jsonb", "json"):
        return "'{}'"
    if t.startswith("timestamp"):
        return "now()"
    if t == "date":
        return "current_date"
    if t == "bytea":
        return SHA
    if t == "uuid":
        return "'00000000-0000-0000-0000-000000000032'"
    raise RuntimeError(f"no filler for type {t}")


def psql(a, sql: str) -> str:
    p = subprocess.run([a.psql, "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-h", a.host, "-p", str(a.port),
                        "-U", a.user, "-d", a.db, "-f", "-"], input=sql, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:])
    return p.stdout


def columns(a, table: str) -> list[dict] | None:
    out = psql(a, f"""SELECT json_agg(json_build_object('name', a.attname, 'type', format_type(a.atttypid, a.atttypmod),
       'notnull', a.attnotnull, 'hasdef', a.atthasdef, 'skip', a.attgenerated <> '' OR a.attidentity <> ''))
  FROM pg_attribute a WHERE a.attrelid = to_regclass('nexus.{table}') AND a.attnum > 0 AND NOT a.attisdropped""").strip()
    return json.loads(out) if out else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("seed", "unseed"))
    ap.add_argument("--port", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--db", default="nexus")
    ap.add_argument("--tenant", default="nexus")
    ap.add_argument("--psql", default="psql")
    a = ap.parse_args()
    stmts = ["BEGIN;", "SET LOCAL session_replication_role = replica;"]
    done = []
    for table, names, extra in TABLES:
        cols = columns(a, table)
        if not cols or not all(any(c["name"] == n for c in cols) for n in names):
            continue
        marker = f"'{MARK}{table}'"
        if a.action == "unseed":
            where = " AND ".join(f"{n} = {marker}" for n in names)
            stmts.append(f"DELETE FROM nexus.{table} WHERE tenant_id = '{a.tenant}' AND {where};")
        else:
            given = {"tenant_id": f"'{a.tenant}'", **{n: marker for n in names}, **extra}
            names_sql, vals = [], []
            for c in cols:
                if c["skip"]:
                    continue
                if c["name"] in given:
                    names_sql.append(f'"{c["name"]}"')
                    vals.append(given[c["name"]])
                elif c["notnull"] and not c["hasdef"]:
                    names_sql.append(f'"{c["name"]}"')
                    vals.append(filler(c["type"]))
            stmts.append(f"INSERT INTO nexus.{table} ({', '.join(names_sql)}) VALUES ({', '.join(vals)});")
        done.append(table)
    stmts.append("COMMIT;")
    psql(a, "\n".join(stmts))
    print(json.dumps({"action": a.action, "tenant": a.tenant, "tables": done}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
