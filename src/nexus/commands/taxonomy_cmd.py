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
    page_size = 250  # Cloud quota: Get limit 300
    while offset < n:
        page = coll.get(
            include=["documents"],
            limit=page_size,
            offset=offset,
        )
        page_ids = page["ids"]
        page_docs = page["documents"] or []
        # Filter out entries with None text (stored without content)
        for pid, pdoc in zip(page_ids, page_docs):
            if pdoc is not None:
                all_ids.append(pid)
                all_texts.append(pdoc)
        offset += len(page_ids)
        if len(page_ids) < page_size:
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
    total_docs = sum(_tree_doc_count(n) for n in tree)
    if total_docs > total_assigned:
        click.echo(f"\nUncategorized: {total_docs - total_assigned} docs")


def _tree_doc_count(node: dict) -> int:
    """Recursively sum doc_count across a tree node and all its children."""
    return node["doc_count"] + sum(
        _tree_doc_count(c) for c in node.get("children", [])
    )


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
def discover_cmd(collection: str, force: bool) -> None:
    """Discover topics from a T3 collection using HDBSCAN clustering."""
    from fnmatch import fnmatch

    from nexus.config import is_local_mode, load_config
    from nexus.db import make_t3

    if is_local_mode():
        cfg = load_config()
        exclude = cfg.get("taxonomy", {}).get("local_exclude_collections", [])
        if any(fnmatch(collection, pat) for pat in exclude):
            click.echo(
                f"Warning: {collection!r} matches taxonomy.local_exclude_collections "
                f"({exclude}). Local MiniLM clusters poorly on code. Proceeding anyway."
            )

    with T2Database(_default_db_path()) as db:
        t3 = make_t3()
        count = discover_for_collection(
            collection, db.taxonomy, t3._client, force=force,
        )
    click.echo(f"Created {count} topics for collection {collection!r}.")


@taxonomy.command("rebuild")
@click.option("--collection", "-c", required=True, help="T3 collection to rebuild taxonomy for")
def rebuild_cmd(collection: str) -> None:
    """Rebuild topic taxonomy from scratch (alias for discover --force)."""
    from nexus.db import make_t3

    with T2Database(_default_db_path()) as db:
        t3 = make_t3()
        count = discover_for_collection(
            collection, db.taxonomy, t3._client, force=True,
        )
    click.echo(f"Rebuilt {count} topics for collection {collection!r}.")


# ── Review command (RDR-070, nexus-lbu) ─────────────────────────────────────


def _resolve_doc_titles(doc_ids: list[str]) -> list[str]:
    """Resolve doc_ids to human-readable titles via catalog, fallback to raw ID."""
    try:
        from nexus.catalog.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return doc_ids
        cat = Catalog(cat_path, cat_path / ".catalog.db")
        titles: list[str] = []
        for doc_id in doc_ids:
            results = cat.search(doc_id)
            if results:
                titles.append(results[0].get("title", doc_id))
            else:
                titles.append(doc_id)
        return titles
    except Exception:
        return doc_ids


def _display_topic(
    topic: dict[str, Any],
    index: int,
    total: int,
    taxonomy: "CatalogTaxonomy",
) -> None:
    """Display a single topic for review."""
    import json

    click.echo(f"\n{'─' * 60}")
    click.echo(f"  [{index}/{total}]  {topic['label']}  ({topic['doc_count']} docs)")

    # c-TF-IDF terms
    if topic.get("terms"):
        try:
            terms = json.loads(topic["terms"])
            click.echo(f"  Terms: {', '.join(terms)}")
        except (json.JSONDecodeError, TypeError):
            pass

    # Representative docs
    doc_ids = taxonomy.get_topic_doc_ids(topic["id"], limit=3)
    if doc_ids:
        titles = _resolve_doc_titles(doc_ids)
        click.echo("  Docs:")
        for title in titles:
            click.echo(f"    - {title}")

    click.echo(f"{'─' * 60}")


def _show_merge_targets(
    current_id: int,
    collection: str,
    taxonomy: "CatalogTaxonomy",
) -> None:
    """Show all other topics in the same collection as merge targets."""
    targets = taxonomy.get_topics_for_collection(collection, exclude_id=current_id)
    if not targets:
        click.echo("  No other topics to merge into.")
        return
    click.echo("  Available merge targets:")
    for t in targets:
        click.echo(f"    [{t['id']}] {t['label']} ({t['doc_count']} docs)")


@taxonomy.command("review")
@click.option("--collection", "-c", default="", help="Filter by collection")
@click.option("--limit", "-n", default=15, type=int, help="Topics per session", show_default=True)
def review_cmd(collection: str, limit: int) -> None:
    """Interactive topic review — accept, rename, merge, delete, or skip."""
    with T2Database(_default_db_path()) as db:
        topics = db.taxonomy.get_unreviewed_topics(collection=collection, limit=limit)
        if not topics:
            click.echo("No unreviewed topics. All done!")
            return

        click.echo(f"Reviewing {len(topics)} topic(s)")
        click.echo("Actions: [a]ccept  [r]ename  [m]erge  [d]elete  [S]kip")

        for i, topic in enumerate(topics, 1):
            _display_topic(topic, i, len(topics), db.taxonomy)

            try:
                action = click.prompt(
                    "Action",
                    type=click.Choice(["a", "r", "m", "d", "S"], case_sensitive=True),
                    default="S",
                )
            except (click.Abort, EOFError):
                click.echo("\n  Aborted.")
                break

            if action == "a":
                db.taxonomy.mark_topic_reviewed(topic["id"], "accepted")
                click.echo(f"  Accepted: {topic['label']}")

            elif action == "r":
                new_label = click.prompt("  New label")
                db.taxonomy.rename_topic(topic["id"], new_label)
                click.echo(f"  Renamed: {topic['label']} -> {new_label}")

            elif action == "m":
                _show_merge_targets(topic["id"], topic["collection"], db.taxonomy)
                target_id = click.prompt("  Merge into topic ID", type=int)
                target = db.taxonomy.get_topic_by_id(target_id)
                if target is None:
                    click.echo(f"  Topic {target_id} not found, skipping.")
                    continue
                db.taxonomy.merge_topics(topic["id"], target_id)
                click.echo(f"  Merged into: {target['label']}")

            elif action == "d":
                db.taxonomy.delete_topic(topic["id"])
                click.echo(f"  Deleted: {topic['label']}")

            elif action == "S":
                click.echo("  Skipped.")

    click.echo("\nReview session complete.")


# ── Manual operations (RDR-070, nexus-c3w) ──────────────────────────────────


@taxonomy.command("assign")
@click.argument("doc_id")
@click.argument("topic_label")
@click.option("--collection", "-c", default="", help="Collection scope for label lookup")
def assign_cmd(doc_id: str, topic_label: str, collection: str) -> None:
    """Assign a document to a topic by label."""
    with T2Database(_default_db_path()) as db:
        topic_id = db.taxonomy.resolve_label(topic_label, collection=collection)
        if topic_id is None:
            click.echo(f"Topic '{topic_label}' not found.")
            return
        db.taxonomy.assign_topic(doc_id, topic_id, assigned_by="manual")
        click.echo(f"Assigned '{doc_id}' to topic '{topic_label}' (id={topic_id}).")


@taxonomy.command("rename")
@click.argument("topic_label")
@click.argument("new_label")
@click.option("--collection", "-c", default="", help="Collection scope for label lookup")
def rename_cmd(topic_label: str, new_label: str, collection: str) -> None:
    """Rename a topic."""
    with T2Database(_default_db_path()) as db:
        topic_id = db.taxonomy.resolve_label(topic_label, collection=collection)
        if topic_id is None:
            click.echo(f"Topic '{topic_label}' not found.")
            return
        db.taxonomy.rename_topic(topic_id, new_label)
        click.echo(f"Renamed '{topic_label}' -> '{new_label}'.")


@taxonomy.command("merge")
@click.argument("source_label")
@click.argument("target_label")
@click.option("--collection", "-c", default="", help="Collection scope for label lookup")
def merge_cmd(source_label: str, target_label: str, collection: str) -> None:
    """Merge source topic into target topic."""
    with T2Database(_default_db_path()) as db:
        source_id = db.taxonomy.resolve_label(source_label, collection=collection)
        if source_id is None:
            click.echo(f"Source topic '{source_label}' not found.")
            return
        target_id = db.taxonomy.resolve_label(target_label, collection=collection)
        if target_id is None:
            click.echo(f"Target topic '{target_label}' not found.")
            return
        db.taxonomy.merge_topics(source_id, target_id)
        click.echo(f"Merged '{source_label}' into '{target_label}'.")


@taxonomy.command("split")
@click.argument("topic_label")
@click.option("--k", "-k", default=2, type=int, help="Number of sub-topics", show_default=True)
@click.option("--collection", "-c", default="", help="Collection scope for label lookup")
def split_cmd(topic_label: str, k: int, collection: str) -> None:
    """Split a topic into k sub-topics via KMeans clustering."""
    from nexus.db import make_t3

    with T2Database(_default_db_path()) as db:
        topic_id = db.taxonomy.resolve_label(topic_label, collection=collection)
        if topic_id is None:
            click.echo(f"Topic '{topic_label}' not found.")
            return
        t3 = make_t3()
        child_count = db.taxonomy.split_topic(topic_id, k=k, chroma_client=t3._client)
        click.echo(f"Split '{topic_label}' into {child_count} sub-topics.")


# ── Topic-aware links (RDR-070, nexus-40f) ──────────────────────────────────


def _try_load_catalog() -> Any:
    """Load the catalog if initialized, else return None."""
    try:
        from nexus.catalog.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if Catalog.is_initialized(cat_path):
            return Catalog(cat_path, cat_path / ".catalog.db")
    except Exception:
        pass
    return None


def compute_topic_links(
    taxonomy: "CatalogTaxonomy",
    catalog: Any,
    *,
    collection: str = "",
    persist: bool = False,
) -> list[dict[str, Any]]:
    """Derive inter-topic relationships from catalog link graph.

    Joins catalog links (tumbler→tumbler) with topic assignments
    (doc_id→topic) via file_path matching. Returns aggregated
    topic-pair counts with link types.

    When ``persist=True``, also writes to the ``topic_links`` T2 table
    for use by ``apply_topic_boost`` at search time.
    """
    from collections import Counter, defaultdict

    # Build doc_id → (topic_label, topic_id) index from T2
    topics = taxonomy.get_topics()
    if collection:
        topics = [t for t in topics if t.get("collection") == collection]

    topic_label_map: dict[int, str] = {t["id"]: t["label"] for t in topics}
    doc_to_topic_label: dict[str, str] = {}
    doc_to_topic_id: dict[str, int] = {}
    for topic in topics:
        doc_ids = taxonomy.get_all_topic_doc_ids(topic["id"])
        for did in doc_ids:
            doc_to_topic_label[did] = topic_label_map[topic["id"]]
            doc_to_topic_id[did] = topic["id"]

    if not doc_to_topic_label:
        return []

    # Build prefix index: file_path → first matching doc_id (O(N) build, O(1) lookup)
    # Sorted doc_ids enable prefix matching via bisect
    from bisect import bisect_left

    sorted_doc_ids = sorted(doc_to_topic_label.keys())

    def _find_by_prefix(prefix: str) -> str | None:
        """Find first doc_id that starts with prefix via binary search."""
        idx = bisect_left(sorted_doc_ids, prefix)
        if idx < len(sorted_doc_ids) and sorted_doc_ids[idx].startswith(prefix):
            return sorted_doc_ids[idx]
        return None

    # Build tumbler → topic via catalog entry resolution
    links = catalog.link_query(limit=0)
    if not links:
        return []

    tumbler_cache: dict[str, tuple[str, int] | None] = {}

    def _resolve_topic(tumbler: Any) -> tuple[str, int] | None:
        key = str(tumbler)
        if key in tumbler_cache:
            return tumbler_cache[key]
        entry = catalog.resolve(tumbler)
        result = None
        if entry and entry.file_path:
            fp = entry.file_path
            if fp in doc_to_topic_label:
                result = (doc_to_topic_label[fp], doc_to_topic_id[fp])
            else:
                match = _find_by_prefix(fp)
                if match:
                    result = (doc_to_topic_label[match], doc_to_topic_id[match])
        tumbler_cache[key] = result
        return result

    # Aggregate links between topics
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_types: dict[tuple[str, str], set[str]] = defaultdict(set)
    # Also track by topic_id for persistence
    id_pair_counts: Counter[tuple[int, int]] = Counter()
    id_pair_types: dict[tuple[int, int], set[str]] = defaultdict(set)

    for link in links:
        from_info = _resolve_topic(link.from_tumbler)
        to_info = _resolve_topic(link.to_tumbler)
        if from_info and to_info and from_info[1] != to_info[1]:
            from_label, from_id = from_info
            to_label, to_id = to_info
            # Canonical ordering
            label_key = (from_label, to_label) if from_label < to_label else (to_label, from_label)
            pair_counts[label_key] += 1
            pair_types[label_key].add(link.link_type)

            id_key = (from_id, to_id) if from_id < to_id else (to_id, from_id)
            id_pair_counts[id_key] += 1
            id_pair_types[id_key].add(link.link_type)

    result = [
        {
            "from_topic": k[0],
            "to_topic": k[1],
            "link_count": v,
            "link_types": sorted(pair_types[k]),
        }
        for k, v in pair_counts.most_common()
    ]

    # Persist to T2 for search-time topic boost
    if persist and id_pair_counts:
        persist_data = [
            {
                "from_topic_id": k[0],
                "to_topic_id": k[1],
                "link_count": v,
                "link_types": sorted(id_pair_types[k]),
            }
            for k, v in id_pair_counts.most_common()
        ]
        taxonomy.upsert_topic_links(persist_data)

    return result


@taxonomy.command("links")
@click.option("--collection", "-c", default="", help="Filter by collection")
def links_cmd(collection: str) -> None:
    """Show inter-topic relationships derived from catalog links."""
    with T2Database(_default_db_path()) as db:
        catalog = _try_load_catalog()
        if catalog is None:
            click.echo("No catalog initialized. Run `nx catalog setup` first.")
            return

        result = compute_topic_links(
            db.taxonomy, catalog, collection=collection, persist=True,
        )
        if not result:
            click.echo("No topic links found.")
            return

        click.echo(f"Topic relationships ({len(result)} pairs):\n")
        for pair in result:
            types_str = ", ".join(pair["link_types"])
            click.echo(
                f"  {pair['from_topic']} <-> {pair['to_topic']}"
                f"  ({pair['link_count']} links: {types_str})"
            )
