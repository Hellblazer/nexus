# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI command group for topic taxonomy (RDR-061 P3-2, RDR-070 nexus-2dq)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import click
import numpy as np
import structlog

from nexus.commands._helpers import default_db_path as _default_db_path
from nexus.db.t2 import T2Database

if TYPE_CHECKING:
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

_log = structlog.get_logger(__name__)


# ── Shared function (M5 — callable from CLI and index_repo_cmd) ──────────────


def discover_for_collection(
    collection_name: str,
    taxonomy: "CatalogTaxonomy",
    chroma_client: Any,
    *,
    force: bool = False,
    min_cluster_size: int | None = None,
) -> int:
    """Fetch texts from a T3 collection, embed with MiniLM, run HDBSCAN discovery.

    Shared entry point for the CLI ``nx taxonomy discover`` and
    programmatic callers (``index_repo_cmd``, ``post_store_hook``).

    Parameters
    ----------
    collection_name:
        ChromaDB collection to discover topics for.
    taxonomy:
        :class:`CatalogTaxonomy` instance (owns T2 topic tables).
    chroma_client:
        Raw ``chromadb.ClientAPI`` (not ``T3Database``).
    force:
        If True, delete existing topics for this collection before
        re-discovering (calls ``rebuild_taxonomy``).
    min_cluster_size:
        Override adaptive ``max(5, N//15)`` formula. Passed through
        to HDBSCAN if set (not yet wired — future ``nexus-7m8``).

    Returns
    -------
    int
        Number of topics created.
    """
    from nexus.db.local_ef import LocalEmbeddingFunction

    try:
        coll = chroma_client.get_collection(
            collection_name, embedding_function=None,
        )
    except Exception:
        _log.warning("collection_not_found", collection=collection_name)
        return 0

    n = coll.count()
    if n < 5:
        _log.info("too_few_docs", collection=collection_name, n=n)
        return 0

    # Fetch all doc_ids + documents in pages (ChromaDB default limit is 10k)
    all_ids: list[str] = []
    all_texts: list[str] = []
    offset = 0
    page_size = 5000
    while offset < n:
        page = coll.get(
            include=["documents"],
            limit=page_size,
            offset=offset,
        )
        all_ids.extend(page["ids"])
        all_texts.extend(page["documents"] or [])
        offset += len(page["ids"])
        if len(page["ids"]) < page_size:
            break

    # Re-embed with local MiniLM 384d — T3 may use Voyage (1024d)
    ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    _log.info("embedding_docs", collection=collection_name, n=len(all_texts))
    embeddings = np.array(ef(all_texts), dtype=np.float32)

    if force:
        return taxonomy.rebuild_taxonomy(
            collection_name, all_ids, embeddings, all_texts, chroma_client,
        )
    return taxonomy.discover_topics(
        collection_name, all_ids, embeddings, all_texts, chroma_client,
    )


# ── CLI commands ─────────────────────────────────────────────────────────────


@click.group()
def taxonomy() -> None:
    """Topic taxonomy — browsable knowledge hierarchy."""


@taxonomy.command("list")
@click.option("--collection", "-c", default="", help="Filter by collection/project")
@click.option("--depth", "-d", default=2, type=int, help="Tree depth", show_default=True)
def list_cmd(collection: str, depth: int) -> None:
    """Show topic tree."""
    from nexus.taxonomy import get_topic_tree

    depth = min(depth, 4)
    with T2Database(_default_db_path()) as db:
        tree = get_topic_tree(db, collection, max_depth=depth)
        # Count docs with no topic assignment (noise / uncategorized)
        total_assigned = db.taxonomy.conn.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM topic_assignments"
            + (" WHERE topic_id IN (SELECT id FROM topics WHERE collection = ?)" if collection else ""),
            (collection,) if collection else (),
        ).fetchone()[0]
    if not tree:
        click.echo("No topics found. Run `nx taxonomy discover --collection <name>` first.")
        return
    for node in tree:
        _print_tree(node, indent=0)
    total_docs = sum(n["doc_count"] for n in tree)
    if total_docs > total_assigned:
        click.echo(f"\nUncategorized: {total_docs - total_assigned} docs")


def _print_tree(node: dict, indent: int = 0) -> None:
    prefix = "  " * indent + ("├── " if indent > 0 else "")
    click.echo(f"{prefix}{node['label']} ({node['doc_count']} docs)")
    for child in node.get("children", []):
        _print_tree(child, indent + 1)


@taxonomy.command("show")
@click.argument("topic_id", type=int)
@click.option("--limit", "-n", default=20, help="Max docs to show", show_default=True)
def show_cmd(topic_id: int, limit: int) -> None:
    """Show documents assigned to a topic."""
    from nexus.taxonomy import get_topic_docs

    with T2Database(_default_db_path()) as db:
        docs = get_topic_docs(db, topic_id, limit=limit)
    if not docs:
        click.echo(f"No documents in topic {topic_id}.")
        return
    click.echo(f"Topic {topic_id}: {len(docs)} documents")
    click.echo("-" * 60)
    for doc in docs:
        click.echo(f"  {doc['doc_id']}")


@taxonomy.command("discover")
@click.option("--collection", "-c", required=True, help="T3 collection to discover topics for")
@click.option("--force", is_flag=True, help="Delete existing topics before re-discovering")
@click.option("--min-cluster-size", type=int, default=None, help="Override adaptive cluster size")
def discover_cmd(collection: str, force: bool, min_cluster_size: int | None) -> None:
    """Discover topics from a T3 collection using HDBSCAN clustering."""
    from nexus.db import make_t3

    with T2Database(_default_db_path()) as db:
        t3 = make_t3()
        count = discover_for_collection(
            collection,
            db.taxonomy,
            t3._client,
            force=force,
            min_cluster_size=min_cluster_size,
        )
    click.echo(f"Created {count} topics for collection {collection!r}.")


@taxonomy.command("rebuild")
@click.option("--collection", "-c", required=True, help="T3 collection to rebuild taxonomy for")
@click.option("--min-cluster-size", type=int, default=None, help="Override adaptive cluster size")
def rebuild_cmd(collection: str, min_cluster_size: int | None) -> None:
    """Rebuild topic taxonomy from scratch (alias for discover --force)."""
    from nexus.db import make_t3

    with T2Database(_default_db_path()) as db:
        t3 = make_t3()
        count = discover_for_collection(
            collection,
            db.taxonomy,
            t3._client,
            force=True,
            min_cluster_size=min_cluster_size,
        )
    click.echo(f"Rebuilt {count} topics for collection {collection!r}.")
