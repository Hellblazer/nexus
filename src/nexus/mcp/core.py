# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP core tools: search, store, memory, scratch, collections, plans.

14 registered tools + 3 demoted (plain functions, no @mcp.tool()).
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from nexus.corpus import (
    embedding_model_for_collection,
    index_model_for_collection,
    resolve_corpus,
    t3_collection_name,
)
from nexus.db.t3 import verify_collection_deep
from nexus.filters import parse_where_str as _parse_where_str
from nexus.mcp_infra import (
    catalog_auto_link as _catalog_auto_link,
    get_catalog as _get_catalog,
    get_collection_names as _get_collection_names,
    get_t1 as _get_t1,
    get_t3 as _get_t3,
    inject_catalog as _inject_catalog,
    inject_t1 as _inject_t1,
    inject_t3 as _inject_t3,
    reset_singletons as _reset_singletons,
    t2_ctx as _t2_ctx,
)
from nexus.ttl import parse_ttl

mcp = FastMCP("nexus")

_DEFAULT_PAGE_SIZE = 10


# ── Registered tools ─────────────────────────────────────────────────────────


@mcp.tool()
def search(
    query: str,
    corpus: str = "knowledge,code,docs",
    limit: int = 10,
    offset: int = 0,
    where: str = "",
    cluster_by: str = "",
) -> str:
    """Semantic search across T3 collections. Paged results (``offset=N`` for next page).

    Args:
        query: Search query string
        corpus: Corpus prefixes or collection names, comma-separated. "all" for everything.
        limit: Page size (default 10)
        offset: Skip N results for pagination (default 0)
        where: Metadata filter (KEY=VALUE, comma-separated). Ops: = >= <= > < !=
        cluster_by: "semantic" for Ward hierarchical clustering, empty for flat ranked list
    """
    try:
        from nexus.search_engine import search_cross_corpus
        t3 = _get_t3()

        if corpus == "all":
            corpus = "knowledge,code,docs,rdr"

        target: list[str] = []
        all_names = _get_collection_names()
        for part in corpus.split(","):
            part = part.strip()
            if not part:
                continue
            if "__" in part:
                target.append(part)  # fully qualified — include directly
            else:
                target.extend(resolve_corpus(part, all_names))

        if not target:
            return f"No collections match corpus {corpus!r}"

        where_dict = _parse_where_str(where)

        # Fetch enough to fill the requested page
        fetch_n = offset + limit
        clustered = bool(cluster_by)
        results = search_cross_corpus(
            query, target, n_results=fetch_n, t3=t3, where=where_dict,
            cluster_by=cluster_by or None,
            catalog=_get_catalog(),
            link_boost=False,
        )
        # Only sort by distance for flat (non-clustered) results.
        # Clustered results arrive in cluster-grouped order from search_engine.
        if not clustered:
            results.sort(key=lambda r: r.distance)
        if not results:
            return "No results."

        # Apply pagination
        total = len(results)
        page = results[offset:offset + limit]
        if not page:
            return f"No results at offset {offset} (total {total})."

        lines: list[str] = []
        current_cluster: str | None = None
        for r in page:
            # Emit cluster header when group changes
            cluster_label = r.metadata.get("_cluster_label", "")
            if clustered and cluster_label and cluster_label != current_cluster:
                if current_cluster is not None:
                    lines.append("")  # blank separator between clusters
                lines.append(f"── {cluster_label} ──")
                current_cluster = cluster_label
            title = r.metadata.get("source_title") or r.metadata.get("title", "")
            source = r.metadata.get("source_path", "")
            dist = f"{r.distance:.4f}"
            label = title or source or r.id
            snippet = r.content[:200].replace("\n", " ")
            lines.append(f"[{dist}] {label}\n  {snippet}")

        # Pagination footer
        shown_end = offset + len(page)
        if shown_end < total:
            lines.append(f"\n--- showing {offset + 1}-{shown_end} of {total}. next: offset={shown_end}")
        elif total >= fetch_n:
            lines.append(f"\n--- showing {offset + 1}-{shown_end}. may have more: offset={shown_end}")
        else:
            lines.append(f"\n--- showing {offset + 1}-{shown_end} of {total} (end)")

        return "\n\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def query(
    question: str,
    corpus: str = "knowledge",
    where: str = "",
    limit: int = 10,
    author: str = "",
    content_type: str = "",
    follow_links: str = "",
    depth: int = 1,
    subtree: str = "",
) -> str:
    """Document-level semantic search for analytical questions.

    Results are capped at ``limit``. When more documents match, a footer line shows
    the total count. Increase ``limit`` to see more.

    Unlike ``search`` which returns individual chunks, ``query`` groups results
    by source document and returns the best-matching snippet per document along
    with full metadata (title, year, citations, page count, extraction method).

    Use this for research questions where you need to know WHICH documents match,
    not just which text fragments. The calling agent handles analysis/synthesis.

    Catalog-aware routing (optional — all require an initialized catalog):
        author: Filter to documents by this author (catalog metadata search)
        content_type: Filter to documents of this type (code, paper, rdr, knowledge)
        follow_links: Follow links of this type from catalog results (e.g. "cites", "implements").
            Linked collections are merged (interleaved) with seed collections — results
            are ranked by semantic distance across all collections, not separated by source.
        depth: BFS depth for follow_links traversal (default 1)
        subtree: Tumbler prefix — search only documents in this subtree (e.g. "1.1")

    Args:
        question: Natural-language research question
        corpus: Corpus prefix or full collection name (default: knowledge).
                Use "all" for all corpora.
                Note: when catalog params (author, content_type, subtree) are provided,
                corpus is overridden by the resolved catalog collections.
        where: Metadata filter — KEY=VALUE, comma-separated.
               Example: "bib_year>=2020,tags=arch"
        limit: Maximum documents to return (default 10)
        author: Filter by author (catalog metadata)
        content_type: Filter by content type (catalog metadata)
        follow_links: Follow link type from matched documents (catalog graph)
        depth: BFS depth for follow_links (default 1)
        subtree: Tumbler prefix to scope search to a subtree
    """
    try:
        from nexus.search_engine import search_cross_corpus
        t3 = _get_t3()

        # Catalog-aware routing: derive target collections from catalog metadata
        catalog_collections: set[str] | None = None
        has_catalog_params = author or content_type or follow_links or subtree

        if has_catalog_params:
            from nexus.catalog.tumbler import Tumbler
            cat = _get_catalog()
            if cat is None:
                return "Error: catalog not initialized — catalog params (author, content_type, follow_links, subtree) require 'nx catalog setup'"

            # Resolve seed entries for catalog routing
            seed_entries: list = []
            if subtree:
                # Depth check: document-level (3+ segments) has no descendants in the catalog
                subtree_depth = len(subtree.split("."))
                if subtree_depth >= 3:
                    return f"Error: subtree '{subtree}' is a document-level address — use an owner prefix (e.g., '{'.'.join(subtree.split('.')[:2])}') to search a subtree"
                # Use descendants() directly — NOT catalog_search(owner=) which has depth-equality bug
                desc = cat.descendants(subtree)
                catalog_collections = {d["physical_collection"] for d in desc if d.get("physical_collection")}
                seed_entries = [cat.resolve(Tumbler.parse(d["tumbler"])) for d in desc]
                seed_entries = [e for e in seed_entries if e is not None]
            elif author or content_type:
                if content_type and not author:
                    seed_entries = cat.by_content_type(content_type)
                else:
                    seed_entries = cat.find(author, content_type=content_type or None)
                    seed_entries = [r for r in seed_entries if author.lower() in (r.author or "").lower()]
                catalog_collections = {r.physical_collection for r in seed_entries if r.physical_collection}

            if follow_links and catalog_collections is not None:
                # Expand via link graph from already-resolved seed entries
                linked_collections: set[str] = set()
                for entry in seed_entries:
                    graph = cat.graph(entry.tumbler, depth=depth, link_type=follow_links)
                    for node in graph["nodes"]:
                        if node.physical_collection:
                            linked_collections.add(node.physical_collection)
                catalog_collections |= linked_collections
            elif follow_links:
                # follow_links without other filters: use question as catalog seed
                seed_results = cat.find(question)
                if seed_results:
                    catalog_collections = set()
                    for r in seed_results[:5]:  # limit seed to avoid explosion
                        graph = cat.graph(r.tumbler, depth=depth, link_type=follow_links)
                        for node in graph["nodes"]:
                            if node.physical_collection:
                                catalog_collections.add(node.physical_collection)
                    # No link-enriched collections found — fall through to broad search
                    if not catalog_collections:
                        catalog_collections = None
                # else: no seeds found — catalog_collections stays None, broad search proceeds

            if catalog_collections is not None and not catalog_collections:
                return f"No documents found matching catalog filters (author={author!r}, content_type={content_type!r}, subtree={subtree!r}, follow_links={follow_links!r})"

        routing_note = ""
        # Exactly one branch sets `target` — catalog routing or corpus-based routing
        if catalog_collections is not None:
            target = [c for c in catalog_collections if c]
            parts = []
            if author:
                parts.append(f"author={author!r}")
            if content_type:
                parts.append(f"content_type={content_type!r}")
            if subtree:
                parts.append(f"subtree={subtree!r}")
            if follow_links:
                parts.append(f"follow_links={follow_links!r}")
            routing_note = f"[Catalog routing: {', '.join(parts)} -> {len(target)} collections]"
        else:
            if corpus == "all":
                corpus = "knowledge,code,docs,rdr"

            target: list[str] = []
            all_names = _get_collection_names()
            for part in corpus.split(","):
                part = part.strip()
                if not part:
                    continue
                if "__" in part:
                    target.append(part)
                else:
                    target.extend(resolve_corpus(part, all_names))

        if not target:
            return f"No collections match corpus {corpus!r}"

        where_dict = _parse_where_str(where)

        # Over-fetch chunks to ensure good document coverage
        fetch_n = limit * 10
        results = search_cross_corpus(
            question, target, n_results=fetch_n, t3=t3, where=where_dict,
            catalog=_get_catalog(),
            link_boost=True,
        )
        results.sort(key=lambda r: r.distance)
        if not results:
            return "No documents found."

        # Group by document: use content_hash or source_title as doc key
        docs: dict[str, dict] = {}  # doc_key → {meta, snippets, best_distance}
        for r in results:
            meta = r.metadata
            doc_key = (
                meta.get("content_hash")
                or meta.get("source_title")
                or meta.get("source_path")
                or r.id
            )
            if doc_key not in docs:
                docs[doc_key] = {
                    "title": meta.get("source_title") or meta.get("title") or doc_key[:40],
                    "collection": r.collection,
                    "distance": r.distance,
                    "snippet": r.content[:300].replace("\n", " "),
                    "bib_year": meta.get("bib_year", ""),
                    "bib_authors": meta.get("bib_authors", ""),
                    "bib_citation_count": meta.get("bib_citation_count", ""),
                    "bib_venue": meta.get("bib_venue", ""),
                    "page_count": meta.get("page_count", ""),
                    "chunk_count": meta.get("chunk_count", ""),
                    "extraction_method": meta.get("extraction_method", ""),
                    "has_formulas": meta.get("has_formulas", ""),
                    "source_path": meta.get("source_path", ""),
                }
            elif r.distance < docs[doc_key]["distance"]:
                # Better matching chunk — update snippet
                docs[doc_key]["distance"] = r.distance
                docs[doc_key]["snippet"] = r.content[:300].replace("\n", " ")

        # Sort by best match distance, limit
        all_docs = sorted(docs.values(), key=lambda d: d["distance"])
        sorted_docs = all_docs[:limit]
        total = len(all_docs)

        header = f"Found {len(sorted_docs)} documents (from {len(results)} chunks across {len(target)} collections)"
        lines: list[str] = [f"{routing_note}\n{header}" if routing_note else header]
        lines.append("")
        for i, d in enumerate(sorted_docs, 1):
            dist = f"{d['distance']:.4f}"
            title = d["title"][:70]
            header_parts = [f"[{dist}] {title}"]
            # Bibliographic metadata
            bib_parts: list[str] = []
            if d["bib_year"]:
                bib_parts.append(str(d["bib_year"]))
            if d["bib_authors"]:
                authors = d["bib_authors"][:60]
                bib_parts.append(authors)
            if d["bib_venue"]:
                bib_parts.append(d["bib_venue"][:30])
            if d["bib_citation_count"]:
                bib_parts.append(f"{d['bib_citation_count']} citations")
            # Technical metadata
            tech_parts: list[str] = []
            if d["page_count"]:
                tech_parts.append(f"{d['page_count']}p")
            if d["chunk_count"]:
                tech_parts.append(f"{d['chunk_count']} chunks")
            if d["extraction_method"]:
                tech_parts.append(d["extraction_method"])
            if d["has_formulas"]:
                tech_parts.append("formulas")

            lines.append(f"{i}. {' | '.join(header_parts)}")
            if bib_parts:
                lines.append(f"   {' · '.join(bib_parts)}")
            if tech_parts:
                lines.append(f"   [{' · '.join(tech_parts)}]")
            lines.append(f"   {d['collection']}")
            lines.append(f"   {d['snippet']}")
            lines.append("")

        if total > limit:
            lines.append(f"\n--- showing 1-{len(sorted_docs)} of {total} documents. Results are capped at limit={limit}.")

        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def store_put(
    content: str,
    collection: str = "knowledge",
    title: str = "",
    tags: str = "",
    ttl: str = "permanent",
) -> str:
    """Store content in the T3 permanent knowledge store.

    Args:
        content: Text content to store
        collection: Collection name or prefix (default: knowledge)
        title: Document title (recommended for deduplication)
        tags: Comma-separated tags
        ttl: Time-to-live: Nd (days), Nw (weeks), or "permanent"
    """
    try:
        if not content:
            return "Error: content is required"
        days = parse_ttl(ttl)
        ttl_days = days if days is not None else 0
        col_name = t3_collection_name(collection)
        t3 = _get_t3()
        doc_id = t3.put(
            collection=col_name,
            content=content,
            title=title,
            tags=tags,
            ttl_days=ttl_days,
        )
        # Register in catalog (same hook the CLI uses)
        try:
            from nexus.commands.store import _catalog_store_hook
            _catalog_store_hook(title=title, doc_id=doc_id, collection_name=col_name)
        except Exception:
            pass  # catalog registration is non-fatal
        # Auto-link from T1 scratch link-context
        try:
            n = _catalog_auto_link(doc_id)
            if n:
                import structlog
                structlog.get_logger().debug("store_put_auto_linked", doc_id=doc_id, link_count=n)
        except Exception:
            pass  # auto-linking is non-fatal
        return f"Stored: {doc_id} -> {col_name}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def store_get(doc_id: str, collection: str = "knowledge") -> str:
    """Retrieve the full content and metadata of a T3 knowledge entry by document ID.

    Use after store_list or search to read the complete document.

    Args:
        doc_id: Exact document ID (from store_list or store_put output)
        collection: Collection name or prefix (default: knowledge)
    """
    try:
        if not doc_id:
            return "Error: doc_id is required"
        col_name = t3_collection_name(collection)
        t3 = _get_t3()
        entry = t3.get_by_id(col_name, doc_id)
        if entry is None:
            return f"Not found: {doc_id!r} in {col_name}"
        title = entry.get("source_title") or entry.get("title", "")
        tags = entry.get("tags", "")
        indexed_at = (entry.get("indexed_at") or "")[:10]
        method = entry.get("extraction_method", "")
        lines: list[str] = [f"ID:         {entry['id']}", f"Collection: {col_name}"]
        if title:
            lines.append(f"Title:      {title}")
        if tags:
            lines.append(f"Tags:       {tags}")
        if method:
            lines.append(f"Extractor:  {method}")
        if indexed_at:
            lines.append(f"Indexed:    {indexed_at}")
        lines.append("")
        lines.append(entry.get("content", ""))
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def store_list(
    collection: str = "knowledge",
    limit: int = 20,
    offset: int = 0,
    docs: bool = False,
) -> str:
    """List entries in a T3 knowledge collection.

    Results are paged. Use offset to retrieve subsequent pages.

    Args:
        collection: Collection name or prefix (default: knowledge)
        limit: Page size (default 20)
        offset: Skip this many entries (default 0). Use for pagination.
        docs: If True, show unique documents instead of individual chunks.
              Deduplicates by content_hash, shows title, chunk count, page count,
              and extraction method. Ignores offset/limit (scans full collection).
    """
    try:
        col_name = t3_collection_name(collection)
        t3 = _get_t3()
        try:
            info = t3.collection_info(col_name)
            total = info["count"]
        except KeyError:
            return f"Collection not found: {col_name}"
        if total == 0:
            return f"No entries in {col_name}."

        if docs:
            return _store_list_docs(t3, col_name, total)

        page = t3.list_store(col_name, limit=limit, offset=offset)
        if not page:
            return f"No entries at offset {offset} (total {total})."
        lines: list[str] = [f"{col_name}  (showing {offset + 1}-{offset + len(page)} of {total})"]
        for e in page:
            doc_id = e.get("id", "")[:16]
            title = (e.get("title") or e.get("source_title") or "")[:40]
            tags = e.get("tags") or ""
            ttl_days = e.get("ttl_days", 0)
            expires_at = e.get("expires_at") or ""
            indexed_at = (e.get("indexed_at") or "")[:10]
            if ttl_days and ttl_days > 0 and expires_at:
                ttl_str = f"expires {expires_at[:10]}"
            else:
                ttl_str = "permanent"
            tag_str = f"  [{tags}]" if tags else ""
            lines.append(f"  {doc_id}  {title:<40}  {ttl_str:<24}  {indexed_at}{tag_str}")
        shown_end = offset + len(page)
        if shown_end < total:
            lines.append(f"--- showing {offset + 1}-{shown_end} of {total}. next: offset={shown_end}")
        else:
            lines.append(f"--- showing {offset + 1}-{shown_end} of {total} (end)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _store_list_docs(t3, col_name: str, total: int) -> str:
    """Document-level view: deduplicate chunks by content_hash."""
    seen: dict[str, dict] = {}
    offset = 0
    while offset < total:
        entries = t3.list_store(col_name, limit=300, offset=offset)
        if not entries:
            break
        for e in entries:
            h = e.get("content_hash", e.get("id", ""))
            if h not in seen:
                seen[h] = e
        offset += 300

    if not seen:
        return f"No documents in {col_name}."

    docs = sorted(seen.values(), key=lambda d: d.get("source_title") or d.get("title") or "")
    lines = [f"{col_name}  ({len(docs)} documents, {total} chunks)"]
    for i, d in enumerate(docs, 1):
        title = (d.get("source_title") or d.get("title") or "untitled")[:60]
        chunks = d.get("chunk_count", "?")
        pages = d.get("page_count", "?")
        method = d.get("extraction_method", "")
        indexed = (d.get("indexed_at") or "")[:10]
        lines.append(f"  {i:3d}. {title:<60}  {chunks:>4} chunks  {pages:>3}p  {method:<8}  {indexed}")
    return "\n".join(lines)


@mcp.tool()
def memory_put(
    content: str,
    project: str,
    title: str,
    tags: str = "",
    ttl: int = 30,
) -> str:
    """Store a memory entry in T2 (SQLite). Upserts by (project, title).

    Args:
        content: Text content to store
        project: Project namespace (e.g. "nexus", "nexus_active")
        title: Entry title (unique within project)
        tags: Comma-separated tags
        ttl: Time-to-live in days (default 30, 0 for permanent)
    """
    try:
        if not content:
            return "Error: content is required"
        with _t2_ctx() as db:
            row_id = db.put(
                project=project,
                title=title,
                content=content,
                tags=tags,
                ttl=ttl if ttl > 0 else None,
            )
        return f"Stored: [{row_id}] {project}/{title}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def memory_get(project: str, title: str = "") -> str:
    """Retrieve a memory entry by project and title.

    When title is empty, lists all entries for the project (titles only — use
    a second call with the specific title to get content).

    Args:
        project: Project namespace
        title: Entry title. Leave empty to LIST all entries (titles only).
    """
    try:
        with _t2_ctx() as db:
            if title:
                entry = db.get(project=project, title=title)
                if entry is None:
                    return f"Not found: {project}/{title}"
                return (
                    f"[{entry['id']}] {entry['project']}/{entry['title']}\n"
                    f"Tags: {entry.get('tags', '')}\n"
                    f"Updated: {entry.get('timestamp', '')}\n\n"
                    f"{entry['content']}"
                )
            else:
                entries = db.list_entries(project=project)
                if not entries:
                    return f"No entries for project {project!r}."
                lines: list[str] = [f"{project}  ({len(entries)} entries — titles only, call with title to get content)"]
                for e in entries:
                    lines.append(f"  [{e['id']}] {e['title']}  ({e.get('timestamp', '')[:10]})")
                return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def memory_delete(project: str, title: str) -> str:
    """Delete a T2 memory entry by project and title.

    Args:
        project: Project namespace
        title: Entry title to delete
    """
    try:
        if not project or not title:
            return "Error: project and title are required"
        with _t2_ctx() as db:
            deleted = db.delete(project=project, title=title)
        if deleted:
            return f"Deleted: {project}/{title}"
        return f"Not found: {project}/{title}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def memory_search(query: str, project: str = "", limit: int = 20, offset: int = 0) -> str:
    """Full-text search across T2 memory entries.

    Searches title, content, and tags fields via FTS5.
    Results are paged. Use offset to retrieve subsequent pages.

    Args:
        query: Search query (FTS5 syntax — matches tokens in title, content, and tags)
        project: Optional project filter
        limit: Page size (default 20)
        offset: Skip this many results (default 0). Use for pagination.
    """
    try:
        with _t2_ctx() as db:
            results = db.search(query, project=project or None)
        if not results:
            return "No results."
        total = len(results)
        page = results[offset:offset + limit]
        if not page:
            return f"No results at offset {offset} (total {total})."
        lines: list[str] = []
        for r in page:
            snippet = r["content"][:200].replace("\n", " ")
            lines.append(f"[{r['id']}] {r['project']}/{r['title']}\n  {snippet}")
        shown_end = offset + len(page)
        if shown_end < total:
            lines.append(f"\n--- showing {offset + 1}-{shown_end} of {total}. next: offset={shown_end}")
        else:
            lines.append(f"\n--- showing {offset + 1}-{shown_end} of {total} (end)")
        return "\n\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def scratch(
    action: str,
    content: str = "",
    query: str = "",
    tags: str = "",
    entry_id: str = "",
    limit: int = 10,
) -> str:
    """T1 session scratch pad — ephemeral within-session storage.

    For ``search`` and ``list``, results are capped at ``limit``. A footer
    indicates when more entries exist.

    Args:
        action: One of "put", "search", "list", "get", "delete"
        content: Content to store (for "put")
        query: Search query (for "search")
        tags: Comma-separated tags (for "put")
        entry_id: Entry ID (for "get", "delete")
        limit: Max results for search/list (default 10)
    """
    try:
        t1, isolated = _get_t1()
        prefix = "[T1 isolated] " if isolated else ""

        if action == "put":
            if not content:
                return "Error: content is required for put"
            doc_id = t1.put(content=content, tags=tags)
            return f"{prefix}Stored: {doc_id}"

        elif action == "search":
            if not query:
                return "Error: query is required for search"
            results = t1.search(query, n_results=limit)
            if not results:
                return f"{prefix}No results."
            lines: list[str] = []
            for r in results:
                snippet = r["content"][:200].replace("\n", " ")
                lines.append(f"{prefix}[{r['id'][:12]}] {snippet}")
            if len(results) >= limit:
                lines.append(f"\n--- showing {len(results)} results (limit={limit}). Increase limit to see more.")
            return "\n".join(lines)

        elif action == "list":
            entries = t1.list_entries()
            if not entries:
                return f"{prefix}No scratch entries."
            total = len(entries)
            entries = entries[:limit]
            lines = []
            for e in entries:
                snippet = e["content"][:80].replace("\n", " ")
                tags_str = f"  [{e.get('tags', '')}]" if e.get("tags") else ""
                lines.append(f"{prefix}[{e['id'][:12]}] {snippet}{tags_str}")
            if total > limit:
                lines.append(f"\n--- showing {limit} of {total} entries. Increase limit to see all.")
            return "\n".join(lines)

        elif action == "get":
            if not entry_id:
                return "Error: entry_id is required for get"
            entry = t1.get(entry_id)
            if entry is None:
                return f"{prefix}Not found: {entry_id}"
            return f"{prefix}{entry['content']}"

        elif action == "delete":
            if not entry_id:
                return "Error: entry_id is required for delete"
            deleted = t1.delete(entry_id)
            if deleted:
                return f"{prefix}Deleted: {entry_id}"
            return f"{prefix}Not found or not owned: {entry_id}"

        else:
            return f"Error: unknown action {action!r}. Use: put, search, list, get, delete"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def scratch_manage(
    action: str,
    entry_id: str,
    project: str = "",
    title: str = "",
) -> str:
    """Manage scratch entries: flag for persistence or promote to T2.

    Args:
        action: One of "flag", "promote"
        entry_id: Scratch entry ID
        project: Target project for promote (required for promote)
        title: Target title for promote (required for promote)
    """
    try:
        t1, isolated = _get_t1()
        prefix = "[T1 isolated] " if isolated else ""

        if action == "flag":
            t1.flag(entry_id, project=project, title=title)
            return f"{prefix}Flagged: {entry_id}"

        elif action == "promote":
            if not project or not title:
                return "Error: project and title are required for promote"
            with _t2_ctx() as t2:
                t1.promote(entry_id, project=project, title=title, t2=t2)
            return f"{prefix}Promoted: {entry_id} -> {project}/{title}"

        else:
            return f"Error: unknown action {action!r}. Use: flag, promote"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def collection_list() -> str:
    """List all T3 collections with document counts and embedding models."""
    try:
        cols = _get_t3().list_collections()
        if not cols:
            return "No collections found."
        lines: list[str] = []
        for c in sorted(cols, key=lambda x: x["name"]):
            model = embedding_model_for_collection(c["name"])
            lines.append(f"{c['name']}  {c['count']:>6} docs  ({model})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def plan_save(
    query: str,
    plan_json: str,
    project: str = "",
    outcome: str = "success",
    tags: str = "",
    ttl: int | None = None,
) -> str:
    """Save a query execution plan to the T2 plan library.

    The plan_json should be a JSON string with the execution plan structure.
    Minimal schema: {"steps": [...], "tools_used": [...], "outcome_notes": "..."}

    Args:
        query: The original natural-language question
        plan_json: JSON string of the execution plan (see schema above)
        project: Project namespace for scoping (e.g. "nexus")
        outcome: Plan outcome — "success" or "partial"
        tags: Comma-separated tags (e.g. operation types used)
        ttl: Time-to-live in days. None means permanent (no expiry).
    """
    try:
        if not query or not plan_json:
            return "Error: query and plan_json are required"
        with _t2_ctx() as db:
            row_id = db.save_plan(
                query=query,
                plan_json=plan_json,
                outcome=outcome,
                tags=tags,
                project=project,
                ttl=ttl,
            )
        return f"Saved plan: [{row_id}] {query[:80]}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def plan_search(query: str, project: str = "", limit: int = 5, offset: int = 0) -> str:
    """Search the T2 plan library for similar query plans.

    Results are paged. Response footer shows ``offset=N`` for next page.

    Args:
        query: Search query (matched against plan query text and tags)
        project: Optional project filter (e.g. "nexus")
        limit: Maximum results to return (default 5)
        offset: Skip this many results (default 0). Use for pagination.
    """
    try:
        with _t2_ctx() as db:
            # Over-fetch by 1 to detect if there are more
            results = db.search_plans(query, limit=limit + 1, project=project)
        if offset:
            results = results[offset:]
        has_more = len(results) > limit
        results = results[:limit]
        if not results:
            return "No matching plans."
        lines: list[str] = []
        for r in results:
            plan_preview = r["plan_json"][:100].replace("\n", " ")
            lines.append(
                f"[{r['id']}] {r['query'][:60]}\n"
                f"  outcome={r['outcome']}  tags={r['tags']}\n"
                f"  plan: {plan_preview}..."
            )
        shown_end = offset + len(results)
        if has_more:
            lines.append(f"\n--- showing {offset + 1}-{shown_end}. may have more: offset={shown_end}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# ── Demoted tools (plain functions, no @mcp.tool()) ──────────────────────────


def store_delete(doc_id: str, collection: str = "knowledge") -> str:
    """Delete a T3 knowledge entry by document ID.

    Args:
        doc_id: Document ID to delete (from store_list or store_put output)
        collection: Collection name or prefix (default: knowledge)
    """
    try:
        if not doc_id:
            return "Error: doc_id is required"
        col_name = t3_collection_name(collection)
        t3 = _get_t3()
        deleted = t3.delete_by_id(col_name, doc_id)
        if deleted:
            return f"Deleted: {doc_id} from {col_name}"
        return f"Not found: {doc_id!r} in {col_name}"
    except Exception as e:
        return f"Error: {e}"


def collection_info(name: str) -> str:
    """Get detailed information about a T3 collection, including a sample of entries.

    Args:
        name: Fully-qualified collection name (e.g. "knowledge__notes", "code__myrepo")
    """
    try:
        db = _get_t3()
        try:
            info = db.collection_info(name)
        except KeyError:
            return f"Collection not found: {name!r}"
        qry_model = embedding_model_for_collection(name)
        idx_model = index_model_for_collection(name)
        count = info.get("count", 0)
        lines: list[str] = [
            f"Collection:  {name}",
            f"Documents:   {count}",
            f"Index model: {idx_model}",
            f"Query model: {qry_model}",
        ]
        meta = info.get("metadata", {})
        if meta:
            lines.append(f"Metadata:    {meta}")

        # Peek: show first few entry titles for discoverability
        if count > 0:
            peek = db.list_store(name, limit=5, offset=0)
            if peek:
                lines.append("")
                lines.append("Sample entries:")
                for e in peek:
                    title = (e.get("source_title") or e.get("title") or "untitled")[:60]
                    lines.append(f"  - {title}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def collection_verify(name: str) -> str:
    """Verify a collection's retrieval health via known-document probe.

    Args:
        name: Fully-qualified collection name (e.g. "knowledge__notes")
    """
    try:
        db = _get_t3()
        try:
            result = verify_collection_deep(db, name)
        except KeyError:
            return f"Collection not found: {name!r}"
        lines = [
            f"Collection: {name}",
            f"Status:     {result.status}",
            f"Documents:  {result.doc_count}",
        ]
        if result.distance is not None:
            lines.append(f"Probe distance: {result.distance:.4f} ({result.metric})")
        if result.probe_hit_rate is not None:
            lines.append(f"Probe hit rate: {result.probe_hit_rate:.0%}")
        if result.probe_doc_id:
            lines.append(f"Probe doc: {result.probe_doc_id}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    """Run the core MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
