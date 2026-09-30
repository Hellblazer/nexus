# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed ``nexus.chunks`` rows with substrate SQL, never the write routes.

RDR-223 Phase 3 makes ``/v1/vectors/upsert-chunks``, ``/store-put`` and
``/upsert-reference-only`` refuse a write whose chashes have no live manifest
row in the collection. A test that needs a chunk before its owner exists (the
manifest FK ``fk_catalog_chunks_chunk`` demands the chunk first) or that needs
an ORPHAN on purpose (the RDR-192 census, reaper and gc tests) can therefore no
longer go through those routes. This module is the one place they build that
state instead: an ``INSERT INTO nexus.chunks`` run as ``nexus_svc``
(NOSUPERUSER NOBYPASSRLS) with the tenant GUC set, so FORCE RLS applies and a
wrong tenant fails loudly instead of seeding a row nobody can see.

What it deliberately does not do: register a manifest row. The seeded chunk is
ownerless until the test writes one through the catalog writer, exactly the
window the routes used to leave open.

The collection is registered through the same client path the write routes rely
on (:func:`nexus.corpus.ensure_collection_registered`), so the
``catalog_collections`` row carries the content type and model the name implies
rather than a placeholder. Embeddings default to a zero vector (the chunk's
presence is what most callers need); ``embed=True`` asks the engine's
``/v1/vectors/embed`` route for the real vector, and ``embeddings=`` stores
caller-supplied ones verbatim.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tests._engine_substrate import ensure_engine

#: The engine substrate always boots local mode with the bge-768 profile
#: (see ``tests/_catalog_fixture_ops.bypass_fk_seed_chunk``), so a vector this
#: wide is the only one its registered collections accept.
_DEFAULT_DIM = 768
_EMBED_COLUMN = {384: "embedding_384", 768: "embedding_768", 1024: "embedding_1024"}

#: A subprocess that seeds chunks (``tests/_hg2dw_hard_kill_child.py``) must
#: reach the PARENT's Postgres: ``ensure_engine()`` is process-memoized, so
#: calling it in a fresh interpreter would boot a second, unrelated substrate.
#: The parent hands the connection facts over in this variable
#: (:func:`substrate_env`); :func:`_pg_state` prefers it.
_PG_ENV = "NX_CHUNK_SEED_PG"
_PG_KEYS = ("pg_bin", "pg_port", "pg_user", "pg_dbname")


def substrate_env() -> dict[str, str]:
    """Env entries that let a child process seed chunks into THIS substrate."""
    state = ensure_engine()
    return {_PG_ENV: json.dumps({k: str(state[k]) for k in _PG_KEYS})}


def _pg_state() -> dict:
    raw = os.environ.get(_PG_ENV)
    return json.loads(raw) if raw else ensure_engine()


def _lit(value: str) -> str:
    """A SQL string literal. ``standard_conforming_strings`` is on, so only
    the quote itself needs doubling."""
    if "\x00" in value:
        raise ValueError("a NUL byte cannot be stored in a PG text column")
    return "'" + value.replace("'", "''") + "'"


def _vector_lit(vec: Sequence[float]) -> str:
    return "'[" + ",".join(repr(float(x)) for x in vec) + "]'::nexus.vector"


def _psql_superuser(state: dict, sql: str) -> str:
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr}\nSQL: {sql}")
    return proc.stdout.strip()


def ambient_tenant() -> str:
    """The tenant the process's ``NX_SERVICE_TOKEN`` is bound to.

    The engine binds tenant to the bearer, so the token is the only ambient
    signal. Resolved the way ``AuthFilter`` does: SHA-256 hex of the token
    against ``nexus.service_tokens`` (no RLS on that table), which also works
    in a subprocess that never minted the tenant itself.
    """
    token = os.environ["NX_SERVICE_TOKEN"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    tenant = _psql_superuser(
        _pg_state(),
        f"SELECT tenant_id FROM nexus.service_tokens WHERE token_hash = {_lit(digest)}",
    )
    if not tenant:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is not a tenant token this substrate minted; "
            "pass tenant= explicitly"
        )
    return tenant


def seed_chunks_direct(
    collection: str,
    ids: Sequence[str],
    documents: Sequence[str],
    metadatas: Sequence[dict[str, Any]] | None = None,
    *,
    tenant: str | None = None,
    embeddings: Sequence[Sequence[float]] | None = None,
    embed: bool = False,
    dim: int = _DEFAULT_DIM,
) -> None:
    """Insert one ``nexus.chunks`` row per id, with no manifest row.

    *ids* are the full 64-hex chashes (the chunk natural id). The argument
    order mirrors ``HttpVectorClient.upsert_chunks`` so a call site moves by
    swapping the callee. *tenant* must be the tenant the ambient
    ``NX_SERVICE_TOKEN`` is bound to (the collection is registered, and
    vectors embedded, through that token); it defaults to a lookup of that
    tenant, and passing it only saves the lookup. A test that switches
    tokens mid-body must also clear ``nexus.corpus._REGISTERED_COLLECTIONS``,
    as ``tests/conftest.py`` does between tests, or a name registered under
    the first tenant reads as registered under the second.

    A repeat write of an existing ``(tenant, collection, chash)`` updates the
    text and vector and MERGES metadata (stored ``||`` incoming), the same
    conflict behavior as the engine's upsert.
    """
    if not ids:
        return
    if len(documents) != len(ids):
        raise ValueError(f"{len(ids)} ids but {len(documents)} documents")
    metas = list(metadatas) if metadatas is not None else [{} for _ in ids]
    if len(metas) != len(ids):
        raise ValueError(f"{len(ids)} ids but {len(metas)} metadatas")
    if embed and embeddings is not None:
        raise ValueError("pass embed=True or embeddings=, not both")
    for chash in ids:
        if len(chash) != 64 or any(c not in "0123456789abcdef" for c in chash):
            raise ValueError(f"chunk id {chash!r} is not a 64-char lowercase hex chash")
    tenant = tenant if tenant is not None else ambient_tenant()

    from nexus.corpus import ensure_collection_registered

    ensure_collection_registered(collection)

    if embed:
        from nexus.db.http_vector_client import HttpVectorClient

        embeddings = HttpVectorClient().embed_for_collection(collection, list(documents))
    if embeddings is None:
        embeddings = [[0.0] * dim for _ in ids]
    if len(embeddings) != len(ids):
        raise ValueError(f"{len(ids)} ids but {len(embeddings)} embeddings")
    dim = len(embeddings[0])
    if dim not in _EMBED_COLUMN:
        raise ValueError(f"no nexus.chunks embedding column of width {dim}")
    col = _EMBED_COLUMN[dim]

    rows = ",\n".join(
        f"({_lit(tenant)}, {_lit(collection)}, decode({_lit(chash)}, 'hex'), "
        f"{_lit(doc)}, {_vector_lit(vec)}, {_lit(json.dumps(meta))}::jsonb)"
        for chash, doc, vec, meta in zip(ids, documents, embeddings, metas, strict=True)
    )
    script = (
        "BEGIN;\n"
        f"SELECT set_config('nexus.tenant', {_lit(tenant)}, true);\n"
        f"INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, {col}, metadata)\n"
        f"VALUES {rows}\n"
        "ON CONFLICT (tenant_id, collection, chash) DO UPDATE SET\n"
        f"  chunk_text = EXCLUDED.chunk_text, {col} = EXCLUDED.{col},\n"
        "  metadata = COALESCE(nexus.chunks.metadata, '{}'::jsonb) || EXCLUDED.metadata,\n"
        "  last_written_at = now();\n"
        "COMMIT;\n"
    )
    state = _pg_state()
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", "nexus_svc", "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-q", "-f", "-"],
        input=script, capture_output=True, text=True, timeout=130,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"seed_chunks_direct: psql failed ({proc.returncode}) for tenant {tenant!r} "
            f"collection {collection!r}:\n{proc.stderr}"
        )
