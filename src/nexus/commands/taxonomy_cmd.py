# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI command group for topic taxonomy (RDR-061 P3-2, RDR-070 nexus-2dq)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import click
import numpy as np
import structlog

from nexus.commands._helpers import default_db_path as _default_db_path


def _T2Database(path):
    """Lazy T2Database constructor (avoids module-level import poisoning by test mocks)."""
    from nexus.db.t2 import T2Database
    return T2Database(path)

if TYPE_CHECKING:
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

_log = structlog.get_logger(__name__)


def _progress(msg: str) -> None:
    """Print a progress message and flush immediately (works in pipes/redirects)."""
    import sys

    click.echo(msg)
    try:
        sys.stdout.buffer.flush()
    except Exception:
        pass


# ── Shared function (M5 — callable from CLI and index_repo_cmd) ──────────────


def discover_for_collection(
    collection_name: str,
    taxonomy: "CatalogTaxonomy",
    chroma_client: Any,
    *,
    force: bool = False,
) -> int:
    """Fetch texts + embeddings from a T3 collection, run HDBSCAN discovery.

    Uses the existing T3 embeddings (Voyage on cloud, MiniLM on local)
    rather than re-embedding. This preserves the quality of the original
    embedding model — Voyage-code-3 for code, Voyage-context-3 for docs.
    Falls back to local MiniLM re-embedding when T3 embeddings are not
    available (e.g., collection stored without embeddings).

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

    # Fetch doc_ids, documents, and existing embeddings in pages.
    # Uses T3 embeddings (Voyage on cloud) when available.
    all_ids: list[str] = []
    all_texts: list[str] = []
    all_embs: list[list[float]] = []
    has_t3_embeddings = True
    offset = 0
    page_size = 250  # Cloud quota: Get limit 300
    _milestone_step = max(n // 4, 1)
    _next_milestone = _milestone_step
    while offset < n:
        if offset >= _next_milestone and _next_milestone < n:
            _progress(f"    fetching {offset:,}/{n:,} chunks ({100 * offset // n}%)")
            _next_milestone += _milestone_step
        page = coll.get(
            include=["documents", "embeddings"],
            limit=page_size,
            offset=offset,
        )
        page_ids = page["ids"]
        page_docs = page.get("documents") or []
        page_embs = page.get("embeddings")
        if page_embs is None:
            page_embs = [None] * len(page_ids)
            has_t3_embeddings = False

        for i, pid in enumerate(page_ids):
            doc = page_docs[i] if i < len(page_docs) else None
            emb = page_embs[i] if i < len(page_embs) else None
            if doc is not None:
                all_ids.append(pid)
                all_texts.append(doc)
                if emb is not None and len(emb) > 0:
                    all_embs.append(list(emb))
                else:
                    has_t3_embeddings = False

        offset += len(page_ids)
        if len(page_ids) < page_size:
            break

    import time

    _progress(f"    fetched {len(all_ids):,} chunks")

    # Use T3 embeddings if all docs have them; else fall back to MiniLM
    if has_t3_embeddings and len(all_embs) == len(all_ids):
        _progress(f"    embedding: using T3 native ({len(all_embs[0])}d)")
        embeddings = np.array(all_embs, dtype=np.float32)
    else:
        from nexus.db.local_ef import LocalEmbeddingFunction

        _progress(f"    embedding: re-encoding with MiniLM (384d)")
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        embeddings = np.array(ef(all_texts), dtype=np.float32)

    _progress(f"    clustering {len(all_ids):,} x {embeddings.shape[1]}d...")
    t0 = time.monotonic()

    if force:
        result = taxonomy.rebuild_taxonomy(
            collection_name, all_ids, embeddings, all_texts, chroma_client,
        )
    else:
        result = taxonomy.discover_topics(
            collection_name, all_ids, embeddings, all_texts, chroma_client,
        )

    elapsed = time.monotonic() - t0
    _progress(f"    clustered in {elapsed:.1f}s")
    return result


# ── CLI commands ─────────────────────────────────────────────────────────────


@click.group()
def taxonomy() -> None:
    """Topic taxonomy — browsable knowledge hierarchy."""


@taxonomy.command("status")
def status_cmd() -> None:
    """Show taxonomy health: collections, coverage, review state."""
    with _T2Database(_default_db_path()) as db:
        # Get all topics grouped by collection
        all_topics = db.taxonomy.conn.execute(
            "SELECT collection, COUNT(*), SUM(doc_count), "
            "SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN review_status = 'accepted' THEN 1 ELSE 0 END) "
            "FROM topics GROUP BY collection ORDER BY SUM(doc_count) DESC"
        ).fetchall()

        if not all_topics:
            click.echo("No taxonomy data. Run `nx index repo` or `nx taxonomy discover`.")
            return

        total_topics = 0
        total_assigned = 0
        total_pending = 0

        click.echo("Taxonomy Status\n")
        for coll, n_topics, n_docs, n_pending, n_accepted in all_topics:
            total_topics += n_topics
            total_assigned += n_docs
            total_pending += n_pending

            # Check rebalance
            meta = db.taxonomy.conn.execute(
                "SELECT last_discover_doc_count, last_discover_at "
                "FROM taxonomy_meta WHERE collection = ?",
                (coll,),
            ).fetchone()

            rebal = ""
            if meta:
                last_count, last_at = meta
                if last_at:
                    rebal = f"  discovered {last_at[:10]}"

            status_parts = []
            if n_accepted:
                status_parts.append(f"{n_accepted} accepted")
            if n_pending:
                status_parts.append(f"{n_pending} pending")
            status_str = ", ".join(status_parts) if status_parts else "all pending"

            click.echo(f"  {coll}")
            click.echo(f"    {n_topics} topics, {n_docs} docs assigned ({status_str}){rebal}")

        # Topic links
        link_count = db.taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topic_links"
        ).fetchone()[0]

        click.echo(f"\nTotal: {total_topics} topics, {total_assigned} docs assigned, {link_count} topic links")
        if total_pending:
            click.echo(f"Action: {total_pending} topics need review. Run `nx taxonomy review`.")


@taxonomy.command("list")
@click.option("--collection", "-c", default="", help="Filter by collection/project")
@click.option("--depth", "-d", default=2, type=int, help="Tree depth", show_default=True)
def list_cmd(collection: str, depth: int) -> None:
    """Show topic tree."""
    from nexus.taxonomy import get_topic_tree

    depth = min(depth, 4)
    with _T2Database(_default_db_path()) as db:
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

    with _T2Database(_default_db_path()) as db:
        docs = get_topic_docs(db, topic_id, limit=limit)
    if not docs:
        click.echo(f"No documents in topic {topic_id}.")
        return
    click.echo(f"Topic {topic_id}: {len(docs)} documents")
    click.echo("-" * 60)
    for doc in docs:
        click.echo(f"  {doc['doc_id']}")


@taxonomy.command("discover")
@click.option("--collection", "-c", default="", help="T3 collection (omit for --all)")
@click.option("--all", "discover_all", is_flag=True, help="Discover all eligible T3 collections")
@click.option("--force", is_flag=True, help="Delete existing topics before re-discovering")
def discover_cmd(collection: str, discover_all: bool, force: bool) -> None:
    """Discover topics from T3 collections using HDBSCAN clustering.

    Use --collection for a single collection, or --all to discover
    topics for every T3 collection (respects local_exclude_collections).
    """
    from fnmatch import fnmatch

    from nexus.config import is_local_mode, load_config
    from nexus.db import make_t3

    if not collection and not discover_all:
        click.echo("Specify --collection <name> or --all.")
        return

    cfg = load_config()
    exclude = (
        cfg.get("taxonomy", {}).get("local_exclude_collections", [])
        if is_local_mode() else []
    )
    t3 = make_t3()

    if discover_all:
        colls = t3._client.list_collections()
        targets = [
            c.name for c in colls
            if c.count() >= 5
            and not any(fnmatch(c.name, pat) for pat in exclude)
            and not c.name.startswith("taxonomy__")
        ]
        if not targets:
            click.echo("No eligible collections found.")
            return
        click.echo(f"Discovering topics for {len(targets)} collections...")
    else:
        if is_local_mode() and any(fnmatch(collection, pat) for pat in exclude):
            click.echo(
                f"Warning: {collection!r} matches taxonomy.local_exclude_collections "
                f"({exclude}). Local MiniLM clusters poorly on code. Proceeding anyway."
            )
        targets = [collection]

    auto_label = cfg.get("taxonomy", {}).get("auto_label", True)
    can_label = auto_label and _claude_available()

    total_topics = 0
    total_labeled = 0
    with _T2Database(_default_db_path()) as db:
        for i, col_name in enumerate(targets, 1):
            if len(targets) > 1:
                click.echo(f"[{i}/{len(targets)}] {col_name}")
            count = discover_for_collection(
                col_name, db.taxonomy, t3._client, force=force,
            )
            if count:
                click.echo(f"  {col_name}: {count} topics")
                total_topics += count
                # Label immediately after each collection (incremental, crash-safe)
                if can_label:
                    labeled = relabel_topics(
                        db.taxonomy, collection=col_name, only_pending=True,
                    )
                    if labeled:
                        click.echo(f"  {col_name}: labeled {labeled} topics")
                        total_labeled += labeled
            else:
                click.echo(f"  {col_name}: skipped")

        # Refresh L1 context cache after discovery
        if total_topics:
            try:
                from pathlib import Path as _Path
                from nexus.context import generate_context_l1
                generate_context_l1(db.taxonomy, repo_path=_Path.cwd())
            except Exception:
                pass  # Non-fatal

    click.echo(f"\nTotal: {total_topics} topics, {total_labeled} labeled.")


@taxonomy.command("rebuild")
@click.option("--collection", "-c", default="", help="T3 collection to rebuild taxonomy for")
@click.option("--project", "-p", default="", hidden=True, help="Deprecated: use --collection instead")
@click.option("-k", default=None, type=int, hidden=True, help="Deprecated: cluster count is automatic")
def rebuild_cmd(collection: str, project: str, k: int | None) -> None:
    """Rebuild topic taxonomy from scratch (alias for discover --force)."""
    from nexus.db import make_t3

    # Backward compat: old --project flag maps to --collection
    if project and not collection:
        click.echo(
            f"Note: --project is deprecated. Use --collection instead.\n"
            f"  Hint: nx taxonomy rebuild --collection {project}\n"
        )
        collection = project

    if not collection:
        click.echo("Specify --collection <name>. Use `nx taxonomy discover --all` for all collections.")
        return

    if k is not None:
        click.echo("Note: -k is deprecated. Cluster count is now automatic (HDBSCAN).")

    with _T2Database(_default_db_path()) as db:
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
    with _T2Database(_default_db_path()) as db:
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
    with _T2Database(_default_db_path()) as db:
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
    with _T2Database(_default_db_path()) as db:
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
    with _T2Database(_default_db_path()) as db:
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

    with _T2Database(_default_db_path()) as db:
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
    with _T2Database(_default_db_path()) as db:
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


# ── LLM-powered labeling (RDR-070) ──────────────────────────────────────────


def _claude_available() -> bool:
    """Check if claude CLI is on PATH."""
    import shutil

    return shutil.which("claude") is not None


def _generate_labels_batch(
    items: list[tuple[list[str], list[str]]],
) -> list[str | None]:
    """Generate labels for a batch of topics in one claude -p call.

    Each item is (terms, sample_doc_ids). Returns a list of labels
    (same length as items, None for failures). One subprocess call
    for the whole batch instead of one per topic.
    """
    import re
    import subprocess

    if not items:
        return []

    lines = []
    for i, (terms, doc_ids) in enumerate(items, 1):
        doc_names = [d.split("/")[-1].split(":")[0][:25] for d in doc_ids[:3]]
        lines.append(
            f"{i}. terms=[{', '.join(terms[:5])}] docs=[{', '.join(doc_names)}]"
        )

    prompt = (
        "Label each topic in 3-5 words. "
        "Output: numbered labels only, one per line.\n\n"
        + "\n".join(lines)
    )

    results: list[str | None] = [None] * len(items)
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", "haiku",
             "--system-prompt", "You are a topic labeler. Output only numbered labels.",
             "--no-session-persistence"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            return results

        for line in proc.stdout.strip().splitlines():
            line = line.strip()
            m = re.match(r"^(\d+)\.\s*(.+)$", line)
            if m:
                idx = int(m.group(1)) - 1
                label = m.group(2).strip().strip('"').strip("'")
                if 0 <= idx < len(items) and 3 <= len(label) <= 60:
                    results[idx] = label
    except Exception:
        pass

    return results


def relabel_topics(
    taxonomy: "CatalogTaxonomy",
    *,
    collection: str = "",
    only_pending: bool = True,
    batch_size: int = 20,
    workers: int = 4,
) -> int:
    """Relabel topics using batched claude -p calls with parallel workers.

    Sends batches of ``batch_size`` topics per claude -p call (amortizes
    startup + system prompt overhead). Runs ``workers`` batches concurrently.
    Returns number of topics relabeled.
    """
    import json
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if only_pending:
        topics = taxonomy.get_unreviewed_topics(collection=collection, limit=5000)
    else:
        topics = taxonomy.get_topics_for_collection(collection) if collection else taxonomy.get_topics()

    if not topics:
        return 0

    # Prepare work items: (topic_id, terms, doc_ids)
    work: list[tuple[int, str, list[str], list[str]]] = []
    for topic in topics:
        terms = json.loads(topic["terms"]) if topic.get("terms") else []
        if not terms:
            continue
        doc_ids = taxonomy.get_topic_doc_ids(topic["id"], limit=5)
        work.append((topic["id"], topic["label"], terms, doc_ids))

    if not work:
        return 0

    # Split into batches
    batches: list[list[tuple[int, str, list[str], list[str]]]] = []
    for i in range(0, len(work), batch_size):
        batches.append(work[i : i + batch_size])

    _progress(f"    labeling {len(work)} topics ({len(batches)} batches, {workers} workers)")

    count = 0
    batches_done = 0

    def _label_batch(batch: list) -> list[tuple[int, str | None]]:
        items = [(w[2], w[3]) for w in batch]  # (terms, doc_ids)
        labels = _generate_labels_batch(items)
        return [(w[0], lbl) for w, lbl in zip(batch, labels)]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_label_batch, b): b for b in batches}
        for future in as_completed(futures):
            batches_done += 1
            for tid, label in future.result():
                if label:
                    taxonomy.rename_topic(tid, label)
                    count += 1
            _progress(f"    batch {batches_done}/{len(batches)} done ({count} renamed)")

    return count


@taxonomy.command("label")
@click.option("--collection", "-c", default="", help="Filter by collection")
@click.option("--all", "relabel_all", is_flag=True, help="Relabel all topics, not just pending")
def label_cmd(collection: str, relabel_all: bool) -> None:
    """Generate human-readable topic labels using Claude."""
    if not _claude_available():
        click.echo("claude CLI not found. Install Claude Code to use LLM labeling.")
        return

    with _T2Database(_default_db_path()) as db:
        topics = (
            db.taxonomy.get_topics_for_collection(collection) if collection
            else db.taxonomy.get_topics()
        )
        pending = [t for t in topics if t.get("review_status") == "pending"]
        target = topics if relabel_all else pending

        if not target:
            click.echo("No topics to label.")
            return

        click.echo(f"Labeling {len(target)} topics via Claude haiku...")
        count = relabel_topics(
            db.taxonomy,
            collection=collection,
            only_pending=not relabel_all,
        )
        click.echo(f"Relabeled {count}/{len(target)} topics.")
