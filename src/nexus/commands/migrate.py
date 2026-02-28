# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx migrate — data migration commands."""
from __future__ import annotations

import click

from nexus.db.t3 import T3Database


_PREFIX_TO_STORE = {
    "code__": "code",
    "docs__": "docs",
    "rdr__": "rdr",
    "knowledge__": "knowledge",
}


def _dest_store_for(col_name: str) -> str:
    """Return the destination store key for a collection name."""
    for prefix, store in _PREFIX_TO_STORE.items():
        if col_name.startswith(prefix):
            return store
    return "knowledge"


def _open_source_db() -> T3Database:
    """Open the source T3 store for migration.

    Requires ``chromadb.path`` to be set in config (legacy single-store path).
    Post-migration there is no legacy path, so this raises ``ClickException``
    rather than silently falling back to CloudClient.
    """
    from pathlib import Path as _Path
    from nexus.config import load_config
    cfg = load_config()
    legacy_path = cfg.get("chromadb", {}).get("path", "")
    if not legacy_path:
        raise click.ClickException(
            "chromadb.path not configured — nothing to migrate "
            "(set chromadb.path to the legacy single-store directory)"
        )
    import chromadb
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    client = chromadb.PersistentClient(path=str(_Path(legacy_path).expanduser()))
    return T3Database(_client=client, _ef_override=DefaultEmbeddingFunction())


def _open_dest_db(path_key: str) -> T3Database:
    """Open a destination store for migration (no voyage_api_key required).

    Embeddings are copied verbatim — no re-embedding occurs.
    """
    from pathlib import Path as _Path
    import chromadb
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    from nexus.config import load_config
    cfg = load_config()
    raw_path = cfg.get("chromadb", {}).get(path_key, "")
    if not raw_path:
        raise click.ClickException(
            f"chromadb.{path_key} not configured — run: nx config init"
        )
    path = str(_Path(raw_path).expanduser())
    client = chromadb.PersistentClient(path=path)
    return T3Database(_client=client, _ef_override=DefaultEmbeddingFunction())


@click.group()
def migrate() -> None:
    """Data migration utilities."""


@migrate.command("t3")
def migrate_t3_cmd() -> None:
    """Migrate T3 data from legacy single store to four-store layout.

    Opens the source store (legacy chromadb.path), then copies each collection
    to the appropriate destination store based on prefix: code__* → code store,
    docs__* → docs store, rdr__* → rdr store, everything else → knowledge store.

    Migration is idempotent: if destination count equals source count, the
    collection is skipped.  Embeddings are copied verbatim — no re-embedding.
    Note: idempotency is count-based — if destination has a different count
    than source (partial migration), the full collection is re-upserted.

    Does NOT delete the source store.  Verify the migration and then remove
    the source manually.
    """
    _dest_path_keys = {
        "code": "code_path",
        "docs": "docs_path",
        "rdr": "rdr_path",
        "knowledge": "knowledge_path",
    }

    source_db = _open_source_db()
    dest_stores: dict[str, T3Database] = {}  # opened lazily as needed

    # Per-type counters for the migration report
    counts: dict[str, dict[str, int]] = {
        k: {"migrated": 0, "skipped": 0, "total": 0}
        for k in _dest_path_keys
    }

    collections = source_db.list_collections()
    if not collections:
        click.echo("Source store is empty — nothing to migrate.")
        return

    for col_info in collections:
        col_name = col_info["name"]
        store_key = _dest_store_for(col_name)
        if store_key not in dest_stores:
            dest_stores[store_key] = _open_dest_db(_dest_path_keys[store_key])
        dest_db = dest_stores[store_key]
        counts[store_key]["total"] += 1

        source_col = source_db._client.get_collection(col_name)
        dest_col = dest_db.get_or_create_collection(col_name)

        src_count = source_col.count()
        dst_count = dest_col.count()

        # Idempotency: skip if counts match and collection is non-empty.
        # Note: count-based — re-upserts if counts differ (partial migration).
        if src_count == dst_count and src_count > 0:
            counts[store_key]["skipped"] += 1
            click.echo(f"  {col_name}: skipped ({src_count} docs already present)")
            continue

        # Paginate with limit=5000 to avoid OOM on large collections.
        _PAGE_SIZE = 5000
        all_ids: list = []
        all_docs: list = []
        all_embs: list = []
        all_metas: list = []
        offset = 0
        while True:
            page = source_col.get(
                include=["documents", "embeddings", "metadatas", "ids"],
                limit=_PAGE_SIZE,
                offset=offset,
            )
            if not page["ids"]:
                break
            all_ids.extend(page["ids"])
            all_docs.extend(page["documents"])
            all_embs.extend(page["embeddings"])
            all_metas.extend(page["metadatas"])
            offset += len(page["ids"])
            if len(page["ids"]) < _PAGE_SIZE:
                break
        if all_ids:
            dest_col.upsert(
                ids=all_ids,
                documents=all_docs,
                embeddings=all_embs,
                metadatas=all_metas,
            )
        counts[store_key]["migrated"] += 1
        click.echo(f"  {col_name} → {store_key}: {len(all_ids)} docs")

    click.echo("\nMigration complete:")
    for store_key, c in counts.items():
        if c["total"]:
            click.echo(
                f"  {store_key}: {c['migrated']} migrated, "
                f"{c['skipped']} skipped ({c['total']} collections)"
            )
    click.echo("Source store NOT deleted — verify and remove manually.")
