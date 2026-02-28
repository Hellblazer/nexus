# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Callable

import click
import structlog

from nexus.corpus import embedding_model_for_collection, index_model_for_collection
from nexus.db.t3 import T3Database
from nexus.db.t3_stores import (
    STORE_PREFIX_MAP,
    t3_code, t3_code_local,
    t3_docs, t3_docs_local,
    t3_knowledge, t3_knowledge_local,
    t3_rdr, t3_rdr_local,
)

_log = structlog.get_logger(__name__)

# Insertion order is intentional: code → docs → rdr → knowledge matches display order.
# Lambda wrapping is intentional: it provides late name binding so that
# `patch("nexus.commands.collection.t3_code", ...)` intercepts calls in tests.
_STORE_FACTORIES: dict[str, Callable[[], T3Database]] = {
    "code": lambda: t3_code(),
    "docs": lambda: t3_docs(),
    "rdr": lambda: t3_rdr(),
    "knowledge": lambda: t3_knowledge(),
}

_TYPE_CHOICE = click.Choice(list(_STORE_FACTORIES))

# Local (no voyage_api_key) variants used by delete_cmd — deletion is a
# metadata-only operation that never calls the embedding API.
# Lambda wrapping: same rationale as _STORE_FACTORIES above.
_LOCAL_STORE_FACTORIES: dict[str, Callable[[], T3Database]] = {
    "code": lambda: t3_code_local(),
    "docs": lambda: t3_docs_local(),
    "rdr": lambda: t3_rdr_local(),
    "knowledge": lambda: t3_knowledge_local(),
}

# Prefix → store key mapping used by _infer_store_type.
# Imported from t3_stores to keep a single authoritative source.
_PREFIX_TO_STORE: tuple[tuple[str, str], ...] = STORE_PREFIX_MAP


def _infer_store_type(name: str, explicit: str | None) -> str:
    """Return store key for *name*, honouring an explicit ``--type`` if given.

    When no explicit type is provided, the prefix of *name* is used to infer
    the store (``code__`` → code, ``docs__`` → docs, ``rdr__`` → rdr).
    Everything else falls back to the knowledge store.
    """
    if explicit is not None:
        return explicit
    for prefix, key in _PREFIX_TO_STORE:
        if name.startswith(prefix):
            return key
    return "knowledge"


@click.group()
def collection() -> None:
    """Manage ChromaDB collections (list, info, verify, delete)."""


@collection.command("list")
@click.option("--type", "store_type", type=_TYPE_CHOICE, default=None,
              help="Limit to a specific store (code, docs, rdr, knowledge). Default: all 4.")
def list_cmd(store_type: str | None) -> None:
    """List all T3 collections with document counts."""
    if store_type is not None:
        db = _STORE_FACTORIES[store_type]()
        cols = db.list_collections()
    else:
        cols = []
        for _stype, factory in _STORE_FACTORIES.items():
            try:
                cols.extend(factory().list_collections())
            except RuntimeError:
                _log.debug("store not configured, skipping", store_type=_stype)

    if not cols:
        click.echo("No collections found.")
        return
    width = max(len(c["name"]) for c in cols)
    for c in sorted(cols, key=lambda x: x["name"]):
        click.echo(f"{c['name']:<{width}}  {c['count']:>6} docs")


@collection.command("info")
@click.argument("name")
@click.option("--type", "store_type", type=_TYPE_CHOICE, default=None,
              help="Store to query (inferred from collection name prefix when absent).")
def info_cmd(name: str, store_type: str | None) -> None:
    """Show details for a single collection."""
    db = _STORE_FACTORIES[_infer_store_type(name, store_type)]()
    cols = db.list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        raise click.ClickException(f"collection not found: {name!r} — use: nx collection list")

    query_model = embedding_model_for_collection(name)
    idx_model   = index_model_for_collection(name)

    # Use get_collection_raw (read-only) not get_or_create_collection (has side-effect).
    # Cap the fetch at 5000 docs — sufficient for last-indexed heuristic.
    col = db.get_collection_raw(name)
    result = col.get(include=["metadatas"], limit=5000)
    metadatas: list[dict] = result.get("metadatas") or []
    timestamps = [m["indexed_at"] for m in metadatas if m and "indexed_at" in m]
    last_indexed = max(timestamps) if timestamps else "unknown"

    click.echo(f"Collection:  {match['name']}")
    click.echo(f"Documents:   {match['count']}")
    click.echo(f"Index model: {idx_model}")
    click.echo(f"Query model: {query_model}")
    click.echo(f"Indexed:     {last_indexed}")


@collection.command("delete")
@click.argument("name")
@click.option("--yes", "-y", "--confirm", is_flag=True, help="Skip interactive confirmation prompt")
@click.option("--type", "store_type", type=_TYPE_CHOICE, default=None,
              help="Store to target (inferred from collection name prefix when absent).")
def delete_cmd(name: str, yes: bool, store_type: str | None) -> None:
    """Delete a T3 collection (irreversible)."""
    if not yes:
        click.confirm(f"Delete collection '{name}'? This cannot be undone.", abort=True)
    # Use local (no voyage_api_key) variant — deletion never calls the embedding API.
    _LOCAL_STORE_FACTORIES[_infer_store_type(name, store_type)]().delete_collection(name)
    click.echo(f"Deleted: {name}")


@collection.command("verify")
@click.argument("name")
@click.option("--deep", is_flag=True, help="Run embedding probe query to verify index health")
@click.option("--type", "store_type", type=_TYPE_CHOICE, default=None,
              help="Store to query (inferred from collection name prefix when absent).")
def verify_cmd(name: str, deep: bool, store_type: str | None) -> None:
    """Verify a collection exists and report its document count."""
    db = _STORE_FACTORIES[_infer_store_type(name, store_type)]()
    cols = db.list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        raise click.ClickException(f"collection not found: {name!r} — use: nx collection list")

    if not deep:
        click.echo(f"Collection '{name}': {match['count']} documents — OK")
        return

    count = match["count"]
    if count == 0:
        click.echo(f"Warning: collection '{name}' is empty (0 documents) — skipping embedding probe")
        return

    try:
        db.search(query="health check probe", collection_names=[name], n_results=1)
        click.echo(f"Collection '{name}': {count} documents — embedding health OK")
    except Exception as exc:
        click.echo(f"embedding probe failed for '{name}': {exc} — check voyage_api_key with: nx config get voyage_api_key", err=True)
        raise click.exceptions.Exit(1)
