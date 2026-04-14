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
@click.option("--collection", "-c", default="", help="Show only this collection")
@click.option("--limit", "-n", default=0, type=int, help="Show only top N collections by doc count (0 = all)")
@click.option("--summary", is_flag=True, help="Show only the totals line")
@click.option("--needs-review", is_flag=True, help="Show only collections with pending topics")
def status_cmd(collection: str, limit: int, summary: bool, needs_review: bool) -> None:
    """Show taxonomy health: collections, coverage, review state.

    \b
    Examples:
      nx taxonomy status                              # all collections
      nx taxonomy status --summary                    # totals line only
      nx taxonomy status -c docs__nexus               # one collection
      nx taxonomy status -n 10                        # top 10 by docs
      nx taxonomy status --needs-review               # pending review only
    """
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

        # Compute totals across ALL topics (independent of filters)
        total_topics = sum(r[1] for r in all_topics)
        total_assigned = sum(r[2] for r in all_topics)
        total_pending = sum(r[3] for r in all_topics)

        link_count = db.taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topic_links"
        ).fetchone()[0]

        # Apply filters
        rows = all_topics
        if collection:
            rows = [r for r in rows if r[0] == collection]
            if not rows:
                click.echo(f"No taxonomy data for collection '{collection}'.")
                return
        if needs_review:
            rows = [r for r in rows if r[3] > 0]
        if limit > 0:
            rows = rows[:limit]

        if not summary:
            click.echo("Taxonomy Status\n")
            for coll, n_topics, n_docs, n_pending, n_accepted in rows:
                # Check rebalance
                meta = db.taxonomy.conn.execute(
                    "SELECT last_discover_doc_count, last_discover_at "
                    "FROM taxonomy_meta WHERE collection = ?",
                    (coll,),
                ).fetchone()

                rebal = ""
                if meta:
                    _, last_at = meta
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

            click.echo("")

        click.echo(
            f"Total: {len(all_topics)} collections, {total_topics} topics, "
            f"{total_assigned} docs assigned, {link_count} topic links"
        )
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

        # Cross-collection projection pass (RDR-075 SC-7)
        if total_topics and len(targets) > 1:
            try:
                proj_count = 0
                for col_name in targets:
                    others = [c for c in targets if c != col_name]
                    if others:
                        result = db.taxonomy.project_against(
                            col_name, others, t3._client, threshold=0.85,
                        )
                        assignments = result.get("chunk_assignments", [])
                        if assignments:
                            _persist_assignments(
                                db.taxonomy, assignments, col_name, quiet=True,
                            )
                            proj_count += len(assignments)
                if proj_count:
                    click.echo(f"  Projection: {proj_count} cross-collection assignments")
                    # Co-occurrence topic links (SC-5, SC-7)
                    cooc = db.taxonomy.generate_cooccurrence_links()
                    if cooc:
                        click.echo(f"  Links:      {cooc} co-occurrence topic links")
            except Exception:
                _log.warning("discover_projection_failed", exc_info=True)

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
@click.option(
    "--refresh", is_flag=True,
    help="Recompute catalog-derived links before displaying (requires catalog).",
)
def links_cmd(collection: str, refresh: bool) -> None:
    """Show all inter-topic relationships in topic_links.

    Includes cross-collection projection links from RDR-075 (link_types
    contains 'projection' or 'cooccurrence') AND catalog-derived links
    from compute_topic_links (link_types contains 'cites', 'implements',
    etc.).  Use --refresh to recompute catalog-derived links first.
    """
    with _T2Database(_default_db_path()) as db:
        if refresh:
            catalog = _try_load_catalog()
            if catalog is None:
                click.echo("No catalog initialized — skipping --refresh.")
            else:
                compute_topic_links(
                    db.taxonomy, catalog, collection=collection, persist=True,
                )

        # Display all rows in topic_links, joined with topic labels
        if collection:
            rows = db.taxonomy.conn.execute(
                "SELECT t1.label, t1.collection, t2.label, t2.collection, "
                "       tl.link_count, tl.link_types "
                "FROM topic_links tl "
                "JOIN topics t1 ON tl.from_topic_id = t1.id "
                "JOIN topics t2 ON tl.to_topic_id = t2.id "
                "WHERE t1.collection = ? OR t2.collection = ? "
                "ORDER BY tl.link_count DESC",
                (collection, collection),
            ).fetchall()
        else:
            rows = db.taxonomy.conn.execute(
                "SELECT t1.label, t1.collection, t2.label, t2.collection, "
                "       tl.link_count, tl.link_types "
                "FROM topic_links tl "
                "JOIN topics t1 ON tl.from_topic_id = t1.id "
                "JOIN topics t2 ON tl.to_topic_id = t2.id "
                "ORDER BY tl.link_count DESC"
            ).fetchall()

        if not rows:
            click.echo("No topic links found.")
            return

        click.echo(f"Topic relationships ({len(rows)} pairs):\n")
        for from_label, from_coll, to_label, to_coll, count, types_json in rows:
            try:
                import json as _json
                types_str = ", ".join(_json.loads(types_json))
            except Exception:
                types_str = types_json
            click.echo(
                f"  [{from_coll}] {from_label} <-> [{to_coll}] {to_label}"
                f"  ({count} links: {types_str})"
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


@taxonomy.command("project")
@click.argument("source_collection", default="")
@click.option(
    "--against", "-a", default="",
    help="Comma-separated target collections (omit for all other collections with topics)",
)
@click.option(
    "--threshold", "-t", default=None, type=float,
    help=(
        "Cosine similarity threshold. When omitted, per-corpus-type "
        "defaults apply: code__* → 0.70, knowledge__* → 0.50, "
        "docs__*/rdr__* → 0.55. See docs/taxonomy-projection-tuning.md."
    ),
)
@click.option("--top-k", default=3, type=int, show_default=True, help="Top-k centroids per chunk")
@click.option("--persist", is_flag=True, help="Write projection assignments (assigned_by='projection')")
@click.option("--backfill", is_flag=True, help="Project all collections against each other")
@click.option(
    "--use-icf", "use_icf", is_flag=True,
    help=(
        "Apply ICF (Inverse Collection Frequency) weighting — suppresses "
        "ubiquitous hub topics before threshold + top-k ranking. Stored "
        "similarity remains raw cosine (RDR-077 RF-8)."
    ),
)
def project_cmd(
    source_collection: str,
    against: str,
    threshold: float | None,
    top_k: int,
    persist: bool,
    backfill: bool,
    use_icf: bool,
) -> None:
    """Project source collection chunks against target collection centroids.

    Reports matched topics with chunk counts and average similarity,
    plus novel chunks below the threshold.  Use --persist to write
    projection assignments to topic_assignments.

    \b
    Threshold resolution (RDR-077 Phase 4a):
      explicit --threshold → fallback to prefix default → 0.70
    \b
    Examples:
      nx taxonomy project docs__art-architecture --against knowledge__art
      nx taxonomy project code__nexus --threshold 0.80 --persist
      nx taxonomy project code__nexus --use-icf --persist
      nx taxonomy project --backfill --persist
    """
    from nexus.corpus import default_projection_threshold
    from nexus.db import make_t3

    db = _T2Database(_default_db_path())
    t3 = make_t3()

    # Resolve threshold: explicit flag wins; otherwise per-corpus default
    # (defaults applied at the per-source level inside _run_backfill).
    resolved_threshold = threshold
    if resolved_threshold is None and source_collection:
        resolved_threshold = default_projection_threshold(source_collection)

    try:
        if backfill:
            _run_backfill(
                db.taxonomy, t3._client,
                threshold=threshold, top_k=top_k, persist=persist,
                use_icf=use_icf,
            )
            return

        if not source_collection:
            click.echo("Specify a source collection or use --backfill.")
            return

        # Determine target collections
        if against:
            targets = [c.strip() for c in against.split(",") if c.strip()]
        else:
            # Try sibling collections first (same repo, different prefix)
            from nexus.registry import list_sibling_collections
            targets = list_sibling_collections(source_collection, t3._client)
            if not targets:
                # Fall back to all collections with topics
                targets = [
                    c for c in db.taxonomy.get_distinct_collections()
                    if c != source_collection
                ]
            if not targets:
                click.echo("No other collections have topics. Run 'nx taxonomy discover' first.")
                return

        _progress(
            f"Projecting {source_collection} against {len(targets)} "
            f"collection(s) at threshold {resolved_threshold}"
            + (" with ICF weighting" if use_icf else "")
            + "..."
        )

        icf_map = (
            db.taxonomy.compute_icf_map(use_cache=True) if use_icf else None
        )
        result = db.taxonomy.project_against(
            source_collection, targets, t3._client,
            threshold=resolved_threshold, top_k=top_k,
            icf_map=icf_map,
        )
        # Fall through: display logic uses `threshold` local — rebind
        # to the resolved value so messages reflect what was applied.
        threshold = resolved_threshold

        # Display results
        matched = result["matched_topics"]
        novel = result["novel_chunks"]
        total = result["total_chunks"]

        if matched:
            click.echo(f"\nMatched topics (threshold {threshold}):")
            for m in matched:
                click.echo(
                    f"  [{m['topic_id']}] {m['label']} ({m['collection']}) "
                    f"— {m['chunk_count']} chunks, avg sim {m['avg_similarity']:.2f}"
                )
        else:
            click.echo("\nNo matched topics above threshold.")

        click.echo(f"\nNovel chunks: {len(novel)} (no centroid match >= {threshold})")
        covered = total - len(novel)
        click.echo(f"Total: {len(matched)} matched topics, {covered}/{total} chunks covered")

        if persist and result.get("chunk_assignments"):
            _persist_assignments(
                db.taxonomy, result["chunk_assignments"], source_collection,
            )
        elif matched and not persist:
            click.echo("\nRun with --persist to write assignments to topic_assignments.")

    except ValueError as e:
        click.echo(f"Error: {e}")
    finally:
        db.close()


def _persist_assignments(
    taxonomy: "CatalogTaxonomy",
    chunk_assignments: list[tuple[str, int, float]],
    source_collection: str,
    *,
    quiet: bool = False,
) -> int:
    """Write per-chunk projection assignments from ``project_against`` results.

    Each tuple is ``(doc_id, topic_id, raw_cosine_similarity)`` per RDR-077
    RF-3. *source_collection* identifies the origin of these chunks (used
    later for ICF hub detection).

    Returns the number of assignments written. Set *quiet* to suppress CLI
    output (used when called from pipeline context).
    """
    for doc_id, topic_id, similarity in chunk_assignments:
        taxonomy.assign_topic(
            doc_id,
            topic_id,
            assigned_by="projection",
            similarity=similarity,
            source_collection=source_collection,
        )
    if not quiet:
        click.echo(f"Persisted {len(chunk_assignments)} projection assignment(s).")
    return len(chunk_assignments)


def _run_backfill(
    taxonomy: "CatalogTaxonomy",
    chroma_client: Any,
    *,
    threshold: float | None = None,
    top_k: int = 3,
    persist: bool = False,
    use_icf: bool = False,
) -> None:
    """Project all collections against each other.

    When *threshold* is None, applies the RDR-077 per-corpus-type default
    for each source collection (``default_projection_threshold``). An
    explicit *threshold* short-circuits that and applies uniformly.
    """
    from nexus.corpus import default_projection_threshold

    collections = taxonomy.get_distinct_collections()

    if not collections:
        click.echo("No collections with topics found. Run 'nx taxonomy discover' first.")
        return

    click.echo(f"Backfilling {len(collections)} collection(s)...")

    # ICF map computed once per backfill invocation (per RDR-077 caching).
    icf_map = taxonomy.compute_icf_map(use_cache=True) if use_icf else None

    total_assigned = 0
    total_novel = 0
    for i, src in enumerate(collections, 1):
        targets = [c for c in collections if c != src]
        if not targets:
            continue
        per_src_threshold = (
            threshold if threshold is not None
            else default_projection_threshold(src)
        )
        _progress(
            f"  [{i}/{len(collections)}] {src} → {len(targets)} target(s) "
            f"@ threshold {per_src_threshold}..."
        )
        try:
            result = taxonomy.project_against(
                src, targets, chroma_client,
                threshold=per_src_threshold, top_k=top_k,
                icf_map=icf_map,
            )
            matched = len(result["matched_topics"])
            novel = len(result["novel_chunks"])
            chunks = result["total_chunks"]
            click.echo(
                f"    {matched} matched topics, {novel} novel, "
                f"{chunks} chunks, {len(result.get('chunk_assignments', []))} assignments"
            )
            total_novel += novel

            if persist and result.get("chunk_assignments"):
                _persist_assignments(taxonomy, result["chunk_assignments"], src)
                total_assigned += len(result["chunk_assignments"])
        except Exception as e:
            click.echo(f"    Skipped: {e}")

    click.echo(
        f"Backfill complete: {total_assigned} assignments, {total_novel} novel chunks "
        f"across {len(collections)} collections."
    )


@taxonomy.command("hubs")
@click.option(
    "--min-collections", "-m", default=2, type=int, show_default=True,
    help="Minimum distinct source collections (DF) required to flag a hub.",
)
@click.option(
    "--max-icf", default=None, type=float,
    help=(
        "Only flag topics with ICF at or below this value. Lower ICF "
        "= more ubiquitous = stronger hub signal. Omit to skip ICF filter."
    ),
)
@click.option(
    "--warn-stale", is_flag=True,
    help=(
        "Flag hubs whose latest projection assignment post-dates the newest "
        "`last_discover_at` across contributing source collections (any hub "
        "with a never-discovered source is treated as stale)."
    ),
)
@click.option(
    "--explain", is_flag=True,
    help="Show why each row was flagged: DF, ICF, matched stopword tokens.",
)
def hubs_cmd(
    min_collections: int,
    max_icf: float | None,
    warn_stale: bool,
    explain: bool,
) -> None:
    """List topics that look like cross-collection hubs (RDR-077 Phase 5).

    A hub is a topic whose projection assignments span many source
    collections with low Inverse Collection Frequency — the
    taxonomic analogue of an English stopword. Output sorted by
    `chunks × (1 - ICF)` descending (worst offenders first).

    \b
    Examples:
      nx taxonomy hubs --min-collections 5 --max-icf 1.2
      nx taxonomy hubs --warn-stale --explain

    See docs/taxonomy-projection-tuning.md for guidance on interpreting
    the output and acting on flagged topics.
    """
    db = _T2Database(_default_db_path())
    try:
        rows = db.taxonomy.detect_hubs(
            min_collections=min_collections,
            max_icf=max_icf,
            warn_stale=warn_stale,
        )
        if not rows:
            click.echo("No hubs above the configured thresholds.")
            return

        click.echo(
            "TOPIC                                       DF   CHUNKS   ICF   SCORE"
        )
        click.echo("-" * 76)
        for row in rows:
            label = (row.label or f"topic-{row.topic_id}")[:38]
            click.echo(
                f"[{row.topic_id:>5}] {label:<38}"
                f"{row.distinct_source_collections:>4} {row.total_chunks:>7} "
                f"{row.icf:>5.2f} {row.score:>7.2f}"
            )
            if explain:
                parts: list[str] = [
                    f"DF={row.distinct_source_collections}",
                    f"ICF={row.icf:.3f}",
                ]
                if row.matched_stopwords:
                    parts.append(
                        "stopwords=" + ",".join(row.matched_stopwords)
                    )
                if row.source_collections:
                    parts.append(
                        "sources=" + ",".join(row.source_collections)
                    )
                click.echo("         " + " | ".join(parts))
            if warn_stale and row.is_stale:
                bits: list[str] = []
                if row.max_last_discover_at and row.last_assigned_at and (
                    row.last_assigned_at > row.max_last_discover_at
                ):
                    bits.append(
                        f"last_assigned_at={row.last_assigned_at} > "
                        f"max_last_discover_at={row.max_last_discover_at}"
                    )
                if row.never_discovered_count:
                    bits.append(
                        f"{row.never_discovered_count} never-discovered source(s)"
                    )
                click.echo(
                    "         STALE: " + "; ".join(bits)
                    if bits
                    else "         STALE"
                )
    finally:
        db.close()


@taxonomy.command("audit")
@click.option(
    "--collection", "-c", required=True,
    help="Source collection to audit (e.g. code__nexus).",
)
@click.option(
    "--threshold", "-t", default=None, type=float,
    help=(
        "Count projections whose raw cosine similarity falls below this "
        "value. Defaults to the per-corpus-type value "
        "(code__* 0.70, knowledge__* 0.50, docs__*/rdr__* 0.55). See "
        "docs/taxonomy-projection-tuning.md."
    ),
)
@click.option(
    "--top-n", "-n", default=5, type=int, show_default=True,
    help="Number of receiving hub topics to display.",
)
def audit_cmd(collection: str, threshold: float | None, top_n: int) -> None:
    """Report projection-quality diagnostics for one source collection.

    Output:
      * total projection assignments originating from this collection;
      * p10 / p50 / p90 of raw cosine similarity;
      * count of assignments below threshold (candidates for re-projection);
      * top receiving topics (where this collection's chunks land);
      * pattern-pollution: receiving topics whose labels contain generic
        stopword tokens (`assert`, `class`, `exception`, ...).

    See docs/taxonomy-projection-tuning.md for interpretation guidance.
    """
    db = _T2Database(_default_db_path())
    try:
        report = db.taxonomy.audit_collection(
            collection, threshold=threshold, top_n=top_n,
        )
        click.echo(f"Audit — {report.collection}")
        click.echo("-" * 60)
        if report.total_assignments == 0:
            click.echo("No projection data for this collection yet.")
            click.echo(
                "Run 'nx taxonomy project "
                f"{report.collection} --persist' to populate."
            )
            return

        click.echo(f"Projection assignments: {report.total_assignments}")
        click.echo(
            "Similarity quantiles (raw cosine): "
            f"p10={report.p10:.3f}  p50={report.p50:.3f}  p90={report.p90:.3f}"
        )
        click.echo(
            f"Below threshold {report.threshold}: "
            f"{report.below_threshold_count} assignment(s) — re-projection candidates"
        )

        click.echo("")
        click.echo("Top receiving topics:")
        if not report.top_receiving_hubs:
            click.echo("  (none)")
        for h in report.top_receiving_hubs:
            label = h.label or f"topic-{h.topic_id}"
            click.echo(
                f"  [{h.topic_id}] {label}  "
                f"(chunks={h.chunk_count}, icf={h.icf:.3f})"
            )

        if report.pattern_pollution:
            click.echo("")
            click.echo("Pattern-pollution (hub stopword labels):")
            for h in report.pattern_pollution:
                click.echo(
                    f"  [{h.topic_id}] {h.label} — matched: "
                    + ",".join(h.matched_stopwords)
                )
    finally:
        db.close()
