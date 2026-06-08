#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-152 sandbox harness — Python helper for pg_provision and count queries.
# Called by up.sh and status.sh to avoid repeating PG binary discovery in bash.
"""Sandbox helper: provision, count, verify sandbox vs prod."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# Allow running from repo root without explicit PYTHONPATH
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _cmd_provision(args: argparse.Namespace) -> None:
    """Provision (or verify) an isolated Postgres cluster.

    Structlog goes to stderr; the final JSON result goes to stdout.
    This separation lets callers reliably parse stdout as JSON.
    """
    import logging
    import structlog

    # Direct structlog output to stderr so the caller's stdout capture
    # receives only the final JSON line.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )

    from nexus.db.pg_provision import provision

    config_dir = Path(args.config_dir)
    result = provision(config_dir, force_new_port=args.force_new_port)
    data = {
        "port": result.port,
        "cluster_created": result.cluster_created,
        "db_created": result.db_created,
        "admin_role_created": result.admin_role_created,
        "svc_role_created": result.svc_role_created,
        "already_provisioned": result.already_provisioned,
        "credentials_path": str(result.credentials_path),
    }
    # Print ONLY the JSON to stdout so callers can parse it reliably.
    print(json.dumps(data), flush=True)


def _cmd_pg_bin(args: argparse.Namespace) -> None:
    """Print the path to a named postgres binary (e.g. psql, pg_ctl).

    Structlog goes to stderr; the path goes to stdout.
    """
    import logging
    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )

    from nexus.db.pg_provision import discover_pg_binaries

    bins = discover_pg_binaries()
    val = getattr(bins, args.binary, None)
    if val is None:
        print(f"Unknown binary: {args.binary}", file=sys.stderr)
        sys.exit(1)
    print(val, flush=True)


def _cmd_sqlite_counts(args: argparse.Namespace) -> None:
    """Print JSON row counts for T2 tables in a SQLite db."""
    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"error": f"not found: {db_path}"}))
        sys.exit(1)

    tables = [
        "memory", "plans", "chash_index",
        "topics", "topic_assignments", "topic_links", "taxonomy_meta",
        "relevance_log", "search_telemetry", "nx_answer_runs",
        "hook_failures", "tier_writes", "frecency",
    ]
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    counts: dict[str, int] = {}
    with conn:
        for t in tables:
            try:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                counts[t] = -1  # table absent
    conn.close()
    print(json.dumps(counts))


def _cmd_chroma_counts(args: argparse.Namespace) -> None:
    """Print JSON counts from a Chroma sqlite3 file.

    Opens the file in read-only mode (mode=ro URI) to prevent SQLite from
    creating or updating the -shm sidecar on a WAL-mode database.
    Chroma uses 'delete' journal mode rather than WAL so the sidecar risk is
    lower here than for memory.db, but mode=ro is still the correct default
    for any prod-path read.
    """
    sqlite_path = Path(args.chroma_sqlite)
    if not sqlite_path.exists():
        print(json.dumps({"error": f"not found: {sqlite_path}"}))
        sys.exit(1)

    uri = f"file:{sqlite_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    try:
        collections = conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
        embeddings = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        conn.close()
    print(json.dumps({"collections": collections, "embeddings": embeddings}))


def main() -> None:
    p = argparse.ArgumentParser(description="RDR-152 sandbox helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("provision", help="Provision isolated Postgres cluster")
    pp.add_argument("--config-dir", required=True)
    pp.add_argument("--force-new-port", action="store_true", default=False)

    pb = sub.add_parser("pg-bin", help="Print path to a Postgres binary")
    pb.add_argument("binary", choices=["initdb", "pg_ctl", "psql", "createdb", "bin_dir"])

    sc = sub.add_parser("sqlite-counts", help="Row counts for T2 SQLite db")
    sc.add_argument("--db", required=True)

    cc = sub.add_parser("chroma-counts", help="Count Chroma collections and embeddings")
    cc.add_argument("--chroma-sqlite", required=True)

    args = p.parse_args()
    dispatch = {
        "provision": _cmd_provision,
        "pg-bin": _cmd_pg_bin,
        "sqlite-counts": _cmd_sqlite_counts,
        "chroma-counts": _cmd_chroma_counts,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
