# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direct-SQL seeding against the self-provisioned engine substrate, shared by the
RDR-192 upgrade-ladder tests (``test_rdr192_manifest_backfill_substrate.py``,
``test_rdr192_empty_tenant_parity.py``).

Seeds ``nexus.catalog_collections`` and ``nexus.chunks`` with ``psql`` against the
substrate's own Postgres, so a chunk can exist ownerless (the write routes refuse that
state since RDR-223 Phase 3). The catalog documents and manifest rows a test also needs
go through the catalog API, as in production.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

MODEL = "bge-base-en-v15-768"


def lit(value: str) -> str:
    """A SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def psql(sql: str) -> str:
    """Run one statement against the substrate's Postgres and return its stdout."""
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — substrate boots lazily

    state = ensure_engine()
    proc = subprocess.run(
        [
            str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
            "-U", state["pg_user"], "-d", state["pg_dbname"],
            "-v", "ON_ERROR_STOP=1", "-At", "-c", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}\nSQL: {sql}"
    return proc.stdout


def register_collection(tenant: str, collection: str, lifecycle_state: str = "live") -> None:
    psql(
        "INSERT INTO nexus.catalog_collections "
        "(tenant_id, name, content_type, owner_id, embedding_model, lifecycle_state) "
        f"VALUES ({lit(tenant)}, {lit(collection)}, 'knowledge', 'test-seed', {lit(MODEL)}, "
        f"{lit(lifecycle_state)}) "
        "ON CONFLICT DO NOTHING"
    )


def seed_chunk(tenant: str, collection: str, text: str, metadata: dict) -> str:
    """Insert one ``nexus.chunks`` row by direct SQL and return its chash.

    Registers the collection live first when it is not registered yet (a collection a
    caller registered under another state is left as it is).
    """
    chash = hashlib.sha256(text.encode()).hexdigest()
    register_collection(tenant, collection)
    vec = "[" + ",".join(["0"] * 768) + "]"
    psql(
        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768, metadata) "
        f"VALUES ({lit(tenant)}, {lit(collection)}, decode({lit(chash)}, 'hex'), {lit(text)}, "
        f"{lit(vec)}::nexus.vector, {lit(json.dumps({'chunk_text_hash': chash, **metadata}))}::jsonb) "
        "ON CONFLICT DO NOTHING"
    )
    return chash
