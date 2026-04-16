# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP core tools: search, store, memory, scratch, collections, plans.

21 registered tools + 3 demoted (plain functions, no @mcp.tool()).
RDR-078 P1 added ``plan_match`` + ``plan_run`` to the existing
``plan_save`` / ``plan_search`` pair. RDR-080 P1 added ``nx_answer``;
RDR-080 P3 added ``nx_tidy``, ``nx_enrich_beads``, ``nx_plan_audit``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

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
    fire_post_store_hooks as _fire_post_store_hooks,
    get_catalog as _get_catalog,
    get_collection_names as _get_collection_names,
    get_recent_search_traces as _get_recent_search_traces,
    get_t1 as _get_t1,
    get_t3 as _get_t3,
    inject_catalog as _inject_catalog,
    inject_t1 as _inject_t1,
    inject_t3 as _inject_t3,
    record_search_trace as _record_search_trace,
    reset_singletons as _reset_singletons,
    t2_ctx as _t2_ctx,
)
from nexus.ttl import parse_ttl

mcp = FastMCP("nexus")

_DEFAULT_PAGE_SIZE = 10

# ── RDR-079 P2.4: worker-mode tool-surface restriction ────────────────────
#
# When NEXUS_MCP_WORKER_MODE=1 is set at module import, registration of
# the dispatch-surface tools (plan_match, plan_run, operator_*) is
# skipped. The Python functions stay defined and callable from same-
# process code — only the MCP-over-stdio surface is filtered. This
# prevents a pool worker from re-entering the pool via these tools
# (invariant I-2). See RDR-079 §Worker isolation.

import os as _os

_WORKER_MODE: bool = _os.environ.get("NEXUS_MCP_WORKER_MODE", "").strip() == "1"

_WORKER_FORBIDDEN_TOOLS: frozenset[str] = frozenset({
    "plan_match",
    "plan_run",
    "nx_answer",
    "nx_tidy",
    "nx_enrich_beads",
    "nx_plan_audit",
    "operator_extract",
    "operator_rank",
    "operator_compare",
    "operator_summarize",
    "operator_generate",
})


def _mcp_tool():
    """Decorator wrapping ``@mcp.tool()`` with worker-mode filtering.

    In worker mode, functions whose name is in ``_WORKER_FORBIDDEN_TOOLS``
    are returned unchanged — they stay importable/callable from Python
    but do NOT land in FastMCP's registry, so MCP ``tools/list`` excludes
    them. In normal mode, behaves exactly like ``@mcp.tool()``.
    """
    def deco(fn):
        if _WORKER_MODE and fn.__name__ in _WORKER_FORBIDDEN_TOOLS:
            return fn
        return mcp.tool()(fn)
    return deco

# ── Post-store hooks (register once at import) ──────────────────────────────

from nexus.mcp_infra import register_post_store_hook, taxonomy_assign_hook

register_post_store_hook(taxonomy_assign_hook)

# ── Registered tools ─────────────────────────────────────────────────────────


# Note: catalog server also registers a "search" tool. No collision — Claude Code
# disambiguates by server prefix (mcp__plugin_nx_nexus__search vs
# mcp__plugin_nx_nexus-catalog__search).
@mcp.tool()
def search(
    query: str,
    corpus: str = "knowledge,code,docs",
    limit: int = 10,
    offset: int = 0,
    where: str = "",
    cluster_by: str = "",
    topic: str = "",
    structured: bool = False,
) -> str | dict:
    """Semantic search across T3 collections. Paged results (``offset=N`` for next page).

    Args:
        query: Search query string
        corpus: Corpus prefixes or collection names, comma-separated. "all" for everything.
        limit: Page size (default 10)
        offset: Skip N results for pagination (default 0)
        where: Metadata filter (KEY=VALUE, comma-separated). Ops: = >= <= > < !=
        cluster_by: "semantic" for topic/Ward clustering (default), empty to disable
        topic: Pre-filter to documents in this topic label (from nx taxonomy discover)
        structured: RDR-079 P1 runner-contract flag. When True, returns a
            dict ``{ids, tumblers, distances, collections}`` matching the
            RDR-078 §Phase 1 retrieval-step contract so plan steps can
            resolve ``$stepN.ids`` / ``$stepN.tumblers`` / etc. When False
            (default), returns the human-readable string (backward compat).
    """
    try:
        from nexus.config import load_config
        from nexus.filters import sanitize_query
        from nexus.search_engine import search_cross_corpus

        cfg = load_config()
        if cfg.get("search", {}).get("query_sanitizer", True):
            query = sanitize_query(query)

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
            if structured:
                return {
                    "ids": [], "tumblers": [], "distances": [],
                    "collections": [],
                    "error": f"No collections match corpus {corpus!r}",
                }
            return f"No collections match corpus {corpus!r}"

        where_dict = _parse_where_str(where)

        # Fetch enough to fill the requested page
        fetch_n = offset + limit
        clustered = bool(cluster_by)
        # Always pass taxonomy for topic grouping + topic boost (RDR-070).
        # Wrapped in context manager to avoid connection leak.
        with _t2_ctx() as _t2_db:
            results = search_cross_corpus(
                query, target, n_results=fetch_n, t3=t3, where=where_dict,
                cluster_by=cluster_by or None,
                catalog=_get_catalog(),
                link_boost=False,
                taxonomy=_t2_db.taxonomy,
                topic=topic or None,
            )
        # Only sort by distance for flat (non-clustered) results.
        # Clustered results arrive in cluster-grouped order from search_engine.
        if not clustered:
            results.sort(key=lambda r: r.distance)
        if not results:
            if structured:
                return {
                    "ids": [], "tumblers": [], "distances": [],
                    "collections": [],
                }
            return "No results."

        # Apply pagination
        total = len(results)
        page = results[offset:offset + limit]
        if not page:
            if structured:
                return {
                    "ids": [], "tumblers": [], "distances": [],
                    "collections": [],
                }
            return f"No results at offset {offset} (total {total})."

        # RDR-079 P1 runner-contract structured return. Emits the shape
        # plan_run expects for retrieval steps: {ids, tumblers, distances,
        # collections}. Caller passes structured=True via the runner's
        # _default_dispatcher when dispatching a retrieval step.
        if structured:
            ids: list[str] = []
            tumblers: list[str] = []
            distances: list[float] = []
            cols: set[str] = set()
            for r in page:
                ids.append(r.id)
                # Tumbler may live in metadata when the result was catalog-
                # resolved; fall back to empty string so the list aligns 1:1
                # with ids.
                tumblers.append(str(r.metadata.get("tumbler", "")))
                distances.append(float(r.distance))
                if r.collection:
                    cols.add(r.collection)
            return {
                "ids": ids,
                "tumblers": tumblers,
                "distances": distances,
                "collections": sorted(cols),
            }

        # Record search trace for RDR-061 E2 retrieval feedback correlation.
        # Non-fatal — session may be unavailable in test contexts.
        try:
            t1, _ = _get_t1()
            session_id = t1.session_id if hasattr(t1, "session_id") else ""
            if session_id:
                _record_search_trace(
                    session_id,
                    query,
                    [(r.id, r.collection) for r in page],
                )
        except Exception:
            import structlog
            structlog.get_logger().debug("relevance_trace_record_failed", exc_info=True)

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
            flag = " [CONTRADICTS ANOTHER RESULT]" if r.metadata.get("_contradiction_flag") else ""
            lines.append(f"[{dist}] {label}{flag}\n  {snippet}")

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
        if structured:
            return {
                "ids": [], "tumblers": [], "distances": [],
                "collections": [], "error": str(e),
            }
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
    structured: bool = False,
) -> str | dict:
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
        from nexus.config import load_config
        from nexus.filters import sanitize_query
        from nexus.search_engine import search_cross_corpus

        cfg = load_config()
        if cfg.get("search", {}).get("query_sanitizer", True):
            question = sanitize_query(question)

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
                if structured:
                    return {
                        "ids": [], "tumblers": [], "distances": [],
                        "collections": [],
                    }
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
            if structured:
                return {
                    "ids": [], "tumblers": [], "distances": [],
                    "collections": [],
                    "error": f"No collections match corpus {corpus!r}",
                }
            return f"No collections match corpus {corpus!r}"

        where_dict = _parse_where_str(where)

        # Over-fetch chunks to ensure good document coverage
        fetch_n = limit * 10
        with _t2_ctx() as _t2_db:
            results = search_cross_corpus(
                question, target, n_results=fetch_n, t3=t3, where=where_dict,
                catalog=_get_catalog(),
                link_boost=True,
                taxonomy=_t2_db.taxonomy,
            )
        results.sort(key=lambda r: r.distance)
        if not results:
            if structured:
                return {
                    "ids": [], "tumblers": [], "distances": [],
                    "collections": [],
                }
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

        # RDR-079 P1 runner-contract structured return. Document-level query
        # maps to the retrieval-step shape by using chunk-level ids for the
        # hydration path (best-matching chunk per document).
        if structured:
            ids_out: list[str] = []
            tumblers_out: list[str] = []
            distances_out: list[float] = []
            cols_out: set[str] = set()
            for r in results:
                meta = r.metadata
                doc_key = (
                    meta.get("content_hash")
                    or meta.get("source_title")
                    or meta.get("source_path")
                    or r.id
                )
                # Only surface the one best-distance chunk per document so
                # downstream traverse gets distinct seeds, not N duplicates.
                if doc_key in {k for k in docs if docs[k]["distance"] == r.distance}:
                    ids_out.append(r.id)
                    tumblers_out.append(str(meta.get("tumbler", "")))
                    distances_out.append(float(r.distance))
                    if r.collection:
                        cols_out.add(r.collection)
                    if len(ids_out) >= limit:
                        break
            return {
                "ids": ids_out,
                "tumblers": tumblers_out,
                "distances": distances_out,
                "collections": sorted(cols_out),
            }

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
        if structured:
            return {
                "ids": [], "tumblers": [], "distances": [],
                "collections": [], "error": str(e),
            }
        return f"Error: {e}"


@mcp.tool()
def store_put(
    content: str,
    collection: str = "knowledge",
    title: str = "",
    tags: str = "",
    ttl: str = "permanent",
    structured: bool = False,
) -> str | dict:
    """Store content in the T3 permanent knowledge store.

    Args:
        content: Text content to store
        collection: Collection name or prefix (default: knowledge)
        title: Document title (recommended for deduplication)
        tags: Comma-separated tags
        ttl: Time-to-live: Nd (days), Nw (weeks), or "permanent"
        structured: RDR-079 P1. When True, returns a confirmation dict
            ``{stored: bool, doc_id: str, collection: str}`` instead of a
            human-readable string. Default False (backward compat).
    """
    try:
        if not content:
            if structured:
                return {"stored": False, "error": "content is required"}
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
        # RDR-070: post-store hooks (taxonomy assignment, etc.)
        _fire_post_store_hooks(doc_id, col_name, content)
        # RDR-061 E2: log relevance correlation for the most recent search in
        # this session. Only the newest trace is used to minimize noise —
        # older traces are unlikely to have driven this store_put.
        try:
            t1, _ = _get_t1()
            session_id = t1.session_id if hasattr(t1, "session_id") else ""
            traces = _get_recent_search_traces(session_id) if session_id else []
            if traces:
                latest = traces[-1]
                rows = [
                    (latest["query"], chunk_id, chunk_col, "stored", session_id)
                    for chunk_id, chunk_col in latest["chunks"]
                ]
                with _t2_ctx() as db:
                    db.log_relevance_batch(rows)
        except Exception:
            import structlog
            structlog.get_logger().debug("relevance_log_store_failed", exc_info=True)
        if structured:
            return {"stored": True, "doc_id": doc_id, "collection": col_name}
        return f"Stored: {doc_id} -> {col_name}"
    except Exception as e:
        if structured:
            return {"stored": False, "collection": "", "error": str(e)}
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


@_mcp_tool()
def store_get_many(
    ids: str | list,
    collections: str | list = "knowledge",
    *,
    max_chars_per_doc: int = 4000,
    structured: bool = False,
) -> str | dict:
    """Batch-hydrate document content by ID. RDR-079 hydration primitive.

    The plan runner's retrieval tools (search, query) return
    ``{ids, tumblers, distances, collections}`` — identifiers, not
    content. Operator steps (rank/summarize/compare/extract/generate)
    expect actual text. This tool bridges the gap: pass ``ids`` +
    ``collections`` from a prior retrieval step, receive ``contents``
    aligned 1:1 with ``ids``.

    Args:
        ids: Document IDs to fetch. Accepts a comma-separated string
            OR a Python list (the runner's step-ref resolution yields
            a list; direct MCP callers pass a string).
        collections: Target collection name(s). Accepts a single name,
            comma-separated, OR a list the same length as ``ids``
            (pair-wise lookup). Defaults to ``"knowledge"`` if omitted.
        max_chars_per_doc: Per-document truncation cap to keep the
            downstream operator prompt bounded. 4 KB covers most
            chunks while leaving budget for multi-doc fan-out.
        structured: Return a dict ``{contents, missing}`` when True;
            a human-readable string when False. The runner auto-
            promotes retrieval tools, but ``store_get_many`` is
            explicitly NOT in ``_RETRIEVAL_TOOLS`` — plans that feed
            it into an operator must pass ``structured=True``.

    Returns:
        When ``structured=True``: ``{"contents": [<text>, ...],
        "missing": [<id>, ...]}``. ``contents`` is 1:1 with input
        ``ids``; entries for unresolvable IDs land in ``missing`` and
        the corresponding slot in ``contents`` is an empty string.
    """
    try:
        id_list: list[str]
        if isinstance(ids, list):
            id_list = [str(i) for i in ids if i]
        else:
            id_list = [s.strip() for s in str(ids or "").split(",") if s.strip()]

        coll_list: list[str]
        if isinstance(collections, list):
            coll_list = [str(c) for c in collections if c]
        else:
            coll_list = [
                s.strip()
                for s in str(collections or "knowledge").split(",")
                if s.strip()
            ]
        if not coll_list:
            coll_list = ["knowledge"]

        t3 = _get_t3()
        contents: list[str] = []
        missing: list[str] = []

        for idx, doc_id in enumerate(id_list):
            # Pair-wise collection lookup when the caller supplied a
            # collections list aligned 1:1 with ids (typical plan-runner
            # pattern — search returns `collections` alongside `ids`).
            # Otherwise fall back to trying each collection in turn.
            if len(coll_list) == len(id_list):
                candidates = [coll_list[idx]]
            else:
                candidates = coll_list

            entry = None
            for cand in candidates:
                col_name = t3_collection_name(cand)
                try:
                    entry = t3.get_by_id(col_name, doc_id)
                except Exception:
                    entry = None
                if entry is not None:
                    break

            if entry is None:
                missing.append(doc_id)
                contents.append("")
                continue

            body = str(entry.get("content") or "")
            if max_chars_per_doc > 0 and len(body) > max_chars_per_doc:
                body = body[:max_chars_per_doc] + "…"
            contents.append(body)

        if structured:
            return {"contents": contents, "missing": missing}
        lines = [f"Hydrated {len(contents) - len(missing)}/{len(id_list)} docs"]
        if missing:
            lines.append(f"Missing: {', '.join(missing[:10])}")
        return "\n".join(lines)
    except Exception as e:
        if structured:
            return {
                "contents": [], "missing": [],
                "error": f"store_get_many failed: {e}",
            }
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
    structured: bool = False,
) -> str | dict:
    """Store a memory entry in T2 (SQLite). Upserts by (project, title).

    Args:
        content: Text content to store
        project: Project namespace (e.g. "nexus", "nexus_active")
        title: Entry title (unique within project)
        tags: Comma-separated tags
        ttl: Time-to-live in days (default 30, 0 for permanent)
        structured: RDR-079 P1. When True, returns confirmation dict
            ``{stored: bool, project: str, title: str, id: int}``.
    """
    try:
        if not content:
            if structured:
                return {"stored": False, "project": project, "title": title, "error": "content is required"}
            return "Error: content is required"
        with _t2_ctx() as db:
            row_id = db.put(
                project=project,
                title=title,
                content=content,
                tags=tags,
                ttl=ttl if ttl > 0 else None,
            )
        if structured:
            return {"stored": True, "project": project, "title": title, "id": row_id}
        return f"Stored: [{row_id}] {project}/{title}"
    except Exception as e:
        if structured:
            return {"stored": False, "project": project, "title": title, "error": str(e)}
        return f"Error: {e}"


@mcp.tool()
def memory_get(project: str, title: str = "", structured: bool = False) -> str | dict:
    """Retrieve a memory entry by project and title.

    When title is empty, lists all entries for the project (titles only — use
    a second call with the specific title to get content).

    Args:
        project: Project namespace
        title: Entry title. Leave empty to LIST all entries (titles only).
        structured: RDR-079 P1. When True, returns a dict. Single-entry mode:
            ``{project, title, content, tags, timestamp, id}``. List mode:
            ``{project, entries: [{id, title, timestamp}]}``.
    """
    try:
        with _t2_ctx() as db:
            if title:
                entry = db.get(project=project, title=title)
                if entry is None:
                    if structured:
                        return {"project": project, "title": title, "error": "not found"}
                    return f"Not found: {project}/{title}"
                if structured:
                    return {
                        "id": entry.get("id"),
                        "project": entry.get("project", project),
                        "title": entry.get("title", title),
                        "content": entry.get("content", ""),
                        "tags": entry.get("tags", ""),
                        "timestamp": entry.get("timestamp", ""),
                    }
                return (
                    f"[{entry['id']}] {entry['project']}/{entry['title']}\n"
                    f"Tags: {entry.get('tags', '')}\n"
                    f"Updated: {entry.get('timestamp', '')}\n\n"
                    f"{entry['content']}"
                )
            else:
                entries = db.list_entries(project=project)
                if not entries:
                    if structured:
                        return {"project": project, "entries": []}
                    return f"No entries for project {project!r}."
                if structured:
                    return {
                        "project": project,
                        "entries": [
                            {
                                "id": e.get("id"),
                                "title": e.get("title", ""),
                                "timestamp": e.get("timestamp", ""),
                            }
                            for e in entries
                        ],
                    }
                lines: list[str] = [f"{project}  ({len(entries)} entries — titles only, call with title to get content)"]
                for e in entries:
                    lines.append(f"  [{e['id']}] {e['title']}  ({e.get('timestamp', '')[:10]})")
                return "\n".join(lines)
    except Exception as e:
        if structured:
            return {"project": project, "title": title, "error": str(e)}
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
def memory_search(
    query: str, project: str = "", limit: int = 20, offset: int = 0,
    structured: bool = False,
) -> str | dict:
    """Full-text search across T2 memory entries.

    Searches title, content, and tags fields via FTS5.
    Results are paged. Use offset to retrieve subsequent pages.

    Args:
        query: Search query (FTS5 syntax — matches tokens in title, content, and tags)
        project: Optional project filter
        limit: Page size (default 20)
        offset: Skip this many results (default 0). Use for pagination.
        structured: RDR-079 P1. When True, returns
            ``{entries: [{id, project, title, snippet, timestamp}], has_more: bool, offset: int, total: int}``.
    """
    try:
        with _t2_ctx() as db:
            results = db.search(query, project=project or None)
        if not results:
            if structured:
                return {"entries": [], "has_more": False, "offset": offset, "total": 0}
            return "No results."
        total = len(results)
        page = results[offset:offset + limit]
        if not page:
            if structured:
                return {"entries": [], "has_more": False, "offset": offset, "total": total}
            return f"No results at offset {offset} (total {total})."
        if structured:
            return {
                "entries": [
                    {
                        "id": r.get("id"),
                        "project": r.get("project", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("content", "")[:200].replace("\n", " "),
                        "timestamp": r.get("timestamp", ""),
                    }
                    for r in page
                ],
                "has_more": (offset + len(page)) < total,
                "offset": offset,
                "total": total,
            }
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
        if structured:
            return {"entries": [], "has_more": False, "offset": offset, "total": 0, "error": str(e)}
        return f"Error: {e}"


@mcp.tool()
def memory_consolidate(
    action: str,
    project: str,
    min_similarity: float = 0.7,
    idle_days: int = 30,
    keep_id: int = 0,
    delete_ids: str = "",
    merged_content: str = "",
    limit: int = 50,
    dry_run: bool = False,
    confirm_destructive: bool = False,
) -> str:
    """Memory consolidation tools (RDR-061 E6): find overlaps, merge entries, flag stale.

    Args:
        action: One of "find-overlaps", "merge", "flag-stale"
        project: T2 project namespace to operate on
        min_similarity: Jaccard threshold for find-overlaps (default 0.7)
        idle_days: Staleness threshold for flag-stale (default 30)
        keep_id: Entry ID to keep when merging
        delete_ids: Comma-separated IDs to delete during merge
        merged_content: Replacement content for kept entry during merge
        limit: Max results for find-overlaps (default 50)
        dry_run: For merge, return a preview without modifying T2 (default False)
        confirm_destructive: Required when merge would delete >1 entry (default False)
    """
    try:
        if action == "find-overlaps":
            if not project:
                return "Error: project is required for find-overlaps"
            with _t2_ctx() as db:
                pairs = db.find_overlapping_memories(
                    project=project,
                    min_similarity=min_similarity,
                    limit=limit,
                )
            if not pairs:
                return f"No overlapping memories in {project!r} (min_similarity={min_similarity})"
            lines = [f"Found {len(pairs)} overlapping pair(s) in {project!r}:"]
            for a, b in pairs:
                lines.append(f"  [{a['id']}] {a['title']}  ↔  [{b['id']}] {b['title']}")
            return "\n".join(lines)

        elif action == "merge":
            if keep_id <= 0 or not delete_ids or not merged_content:
                return "Error: merge requires keep_id>0, delete_ids, and merged_content"
            try:
                del_ids = [int(x.strip()) for x in delete_ids.split(",") if x.strip()]
            except ValueError:
                return "Error: delete_ids must be comma-separated integers"
            if not del_ids:
                return "Error: delete_ids must contain at least one integer ID"
            if keep_id in del_ids:
                return f"Error: keep_id ({keep_id}) must not appear in delete_ids"
            # Safety gate: merges deleting more than one entry require
            # explicit confirmation. Dry-run returns a preview without
            # modifying T2 (matches catalog_link_bulk's pattern).
            if dry_run:
                with _t2_ctx() as db:
                    keep_entry = db.get(id=keep_id)
                if keep_entry is None:
                    return f"Error: keep_id {keep_id} not found"
                preview = (
                    f"[DRY RUN] Would merge:\n"
                    f"  keep: [{keep_id}] {keep_entry['title']}\n"
                    f"  delete: {del_ids}\n"
                    f"  new content: {merged_content[:200]}"
                )
                return preview
            if len(del_ids) > 1 and not confirm_destructive:
                return (
                    f"Error: would delete {len(del_ids)} entries — set "
                    f"confirm_destructive=True to proceed, or dry_run=True to preview"
                )
            with _t2_ctx() as db:
                db.merge_memories(
                    keep_id=keep_id,
                    delete_ids=del_ids,
                    merged_content=merged_content,
                )
            return f"Merged: kept [{keep_id}], deleted {del_ids}"

        elif action == "flag-stale":
            if not project:
                return "Error: project is required for flag-stale"
            with _t2_ctx() as db:
                stale = db.flag_stale_memories(project=project, idle_days=idle_days)
            if not stale:
                return f"No stale entries in {project!r} (idle > {idle_days} days)"
            lines = [f"Stale entries in {project!r} (idle > {idle_days} days):"]
            for e in stale:
                last = e.get("last_accessed") or e.get("timestamp", "")
                lines.append(f"  [{e['id']}] {e['title']}  last: {last[:10]}")
            return "\n".join(lines)

        else:
            return f"Error: unknown action {action!r}. Use: find-overlaps, merge, flag-stale"
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
                report = t1.promote(entry_id, project=project, title=title, t2=t2)
            return f"{prefix}Promoted: {entry_id} -> {project}/{title} (action={report.action})"

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


def _parse_dim_filter(value: str) -> dict[str, Any]:
    """Parse ``dimensions`` / ``bindings`` arguments for the plan MCP tools.

    Prefers JSON-object form (unambiguous for values containing commas).
    Falls back to legacy ``key=value,key=value`` CSV parsing when the
    input is not JSON. Returns ``{}`` for empty input. Raises ``ValueError``
    with a clear message on malformed input.
    """
    value = value.strip()
    if not value:
        return {}
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON object: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON value must be an object")
        return {str(k): v for k, v in parsed.items()}
    # Legacy CSV — accept only if every segment parses as key=value
    out: dict[str, Any] = {}
    for segment in value.split(","):
        if "=" not in segment:
            raise ValueError(
                f"{segment!r}: expected 'key=value' or a JSON object"
            )
        k, _, v = segment.partition("=")
        out[k.strip()] = v.strip()
    return out


@mcp.tool()
def plan_save(
    query: str,
    plan_json: str,
    project: str = "",
    outcome: str = "success",
    tags: str = "",
    ttl: int | None = None,
    name: str = "",
    verb: str = "",
    scope: str = "",
    dimensions: str = "",
    default_bindings: str = "",
    parent_dims: str = "",
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
        name: Template name for RDR-078 dimensional plans (e.g. "research").
        verb: Verb dimension (e.g. "research", "review"). Populates
            dimensions["verb"] if dimensions is empty.
        scope: Scope dimension ("global" | "rdr" | "project" | "repo").
        dimensions: JSON object of dimensions (e.g. '{"verb":"research",
            "scope":"global"}'). Canonicalized server-side for the UNIQUE
            (project, dimensions) dedup index. If empty but verb/scope
            supplied, they are folded in.
        default_bindings: JSON object of default binding values.
        parent_dims: JSON object naming the parent plan's dimensions
            (for currying / per-RDR specialization).
    """
    try:
        if not query or not plan_json:
            return "Error: query and plan_json are required"
        # Normalize dimensional fields through the canonicalizer so the
        # UNIQUE (project, dimensions) index dedups byte-identical maps.
        from nexus.plans.schema import canonical_dimensions_json
        dims_dict: dict[str, Any] = {}
        if dimensions:
            try:
                parsed = json.loads(dimensions)
                if isinstance(parsed, dict):
                    dims_dict.update(parsed)
            except json.JSONDecodeError:
                return "Error: dimensions must be a JSON object"
        if verb and "verb" not in dims_dict:
            dims_dict["verb"] = verb
        if scope and "scope" not in dims_dict:
            dims_dict["scope"] = scope
        dims_json: str | None = (
            canonical_dimensions_json(dims_dict) if dims_dict else None
        )
        with _t2_ctx() as db:
            # Idempotency: if this (project, dimensions) already has a row,
            # return its id instead of hitting the UNIQUE constraint. The
            # scoped loader uses the same check; honoring it at the MCP
            # boundary closes the P1 write-visibility loop for agents.
            if dims_json is not None:
                existing = db.plans.get_plan_by_dimensions(
                    project=project, dimensions=dims_json,
                )
                if existing is not None:
                    return (
                        f"Plan exists (no-op): [{existing['id']}] "
                        f"{query[:80]} — canonical dims already registered; "
                        f"the new plan_json was NOT saved. To replace the "
                        f"plan body, update via plan_library directly or "
                        f"rotate the dimensional identity."
                    )
            row_id = db.save_plan(
                query=query,
                plan_json=plan_json,
                outcome=outcome,
                tags=tags,
                project=project,
                ttl=ttl,
                name=name or None,
                verb=verb or None,
                scope=scope or None,
                dimensions=dims_json,
                default_bindings=default_bindings or None,
                parent_dims=parent_dims or None,
            )
            # RDR-078 P1 write-visibility: mirror the new plan into the
            # T1 ``plans__session`` cache so in-session ``plan_match``
            # sees it without waiting for the next SessionStart.
            if outcome == "success":
                try:
                    from nexus.mcp_infra import get_t1_plan_cache
                    cache = get_t1_plan_cache(populate_from=db.plans)
                    if cache is not None and cache.is_available:
                        row = db.plans.get_plan(row_id)
                        if row is not None:
                            cache.upsert(row)
                except Exception:
                    pass  # cache-unavailable is non-fatal — T2 is authoritative
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
            # Fetch (offset + limit + 1) rows then slice: the backing
            # search_plans has no OFFSET, so slicing after an over-fetch
            # is the only way to page without a schema change. +1 detects
            # "may have more". Slicing the already-truncated result by
            # offset would be wrong — we must over-fetch past the offset.
            results = db.search_plans(
                query, limit=offset + limit + 1, project=project,
            )
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


@_mcp_tool()
def plan_match(
    intent: str,
    dimensions: str = "",
    scope_preference: str = "",
    min_confidence: float = 0.40,
    n: int = 5,
    project: str = "",
) -> str:
    """Match *intent* against the T2 plan library with cosine + FTS5 fallback.

    RDR-078 P1. Returns ranked plans whose description best matches
    *intent*. Semantic match (T1 ``plans__session`` cosine) is the
    primary path; falls back to T2 FTS5 when the session cache is
    unavailable (the fallback hits carry ``confidence=None`` and are
    returned regardless of *min_confidence*).

    Args:
        intent: Caller's natural-language description of the task.
        dimensions: Optional JSON object of dimension filters
            (e.g. ``'{"verb":"research","scope":"global"}'``). Only plans
            whose stored ``dimensions ⊇ filter`` are returned. Also accepts
            the legacy ``key=value,key=value`` CSV form for values that
            contain no commas.
        scope_preference: Reserved for future scope-cascade weighting
            (PQ-14). Accepted but unused at this version.
        min_confidence: Cosine threshold (0.0–1.0). Default 0.40 per the
            RDR-079 P5 calibration (`docs/rdr/rdr-079-calibration.md`)
            — F1-optimal for the bundled MiniLM T1 embedder. Callers
            that need precision-first behavior override to 0.50 (trades
            recall for precision 0.90). FTS5 fallback matches ignore
            this gate.
        n: Max results to return.
        project: Restrict to plans tagged with this project.
    """
    from nexus.mcp_infra import get_t1_plan_cache
    from nexus.plans.matcher import plan_match as _plan_match

    try:
        dim_filter: dict[str, Any] = {}
        if dimensions:
            dim_filter = _parse_dim_filter(dimensions)
        with _t2_ctx() as db:
            cache = get_t1_plan_cache(populate_from=db.plans)
            matches = _plan_match(
                intent,
                library=db.plans,
                cache=cache,
                dimensions=dim_filter,
                scope_preference=scope_preference,
                min_confidence=min_confidence,
                n=n,
                project=project,
            )
        if not matches:
            return "No matching plans."
        lines: list[str] = []
        for m in matches:
            conf = "fts5" if m.confidence is None else f"{m.confidence:.3f}"
            lines.append(
                f"[{m.plan_id}] {m.description[:80]}\n"
                f"  confidence={conf}  name={m.name or '-'}  "
                f"dimensions={m.dimensions}  tags={m.tags or '-'}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@_mcp_tool()
async def plan_run(plan_id: int, bindings: str = "") -> str:
    """Execute the plan identified by *plan_id* against the MCP tool surface.

    RDR-078 P1. Pure substitution + tool dispatch. No agent spawning,
    no LLM — every transformation is observable.

    Args:
        plan_id: The T2 plans.id to execute.
        bindings: Optional JSON object of placeholder fills
            (e.g. ``'{"intent":"how X works","subtree":"1.2"}'``). Also
            accepts the legacy ``key=value,key=value`` CSV form for
            values that contain no commas. Caller bindings override
            ``match.default_bindings`` on conflict.
    """
    from nexus.plans.match import Match
    from nexus.plans.runner import (
        PlanRunBindingError,
        PlanRunEmbeddingDomainError,
        PlanRunStepRefError,
        PlanRunToolNotFoundError,
        plan_run as _plan_run,
    )

    try:
        with _t2_ctx() as db:
            row = db.plans.get_plan(int(plan_id))
            if row is None:
                return f"Error: no plan with id={plan_id}"
            match = Match.from_plan_row(row)
            db.plans.increment_run_started(match.plan_id)

        caller_bindings = _parse_dim_filter(bindings) if bindings else {}
        try:
            result = await _plan_run(match, caller_bindings)
            success = True
        except (
            PlanRunBindingError,
            PlanRunStepRefError,
            PlanRunEmbeddingDomainError,
            PlanRunToolNotFoundError,
        ) as exc:
            with _t2_ctx() as db:
                db.plans.increment_run_outcome(match.plan_id, success=False)
            return f"Error: {exc}"

        with _t2_ctx() as db:
            db.plans.increment_run_outcome(match.plan_id, success=success)

        lines = [f"Plan {plan_id} ran {len(result.steps)} step(s)."]
        for i, step_out in enumerate(result.steps, start=1):
            text_key = next(
                (k for k in ("text", "summary", "answer") if k in step_out), None,
            )
            preview = (
                str(step_out.get(text_key, ""))[:100] if text_key else str(step_out)[:100]
            )
            lines.append(f"  step{i}: {preview}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def traverse(
    seeds: list,
    depth: int = 1,
    direction: str = "both",
    link_types: list | None = None,
    purpose: str = "",
) -> dict:
    """Walk the catalog link graph from *seeds* (RDR-078 P3).

    Returns a step-output dict ``{tumblers, ids, collections}``:

      * ``tumblers`` — every reachable node's tumbler string.
      * ``ids``     — chunk IDs for the reachable nodes. Always ``[]``
        at this version: chunk-level scoping is tracked by bead
        ``nexus-0m3``. Use ``$step.collections`` (below) for scoping
        downstream retrieval; chunk-level precision arrives with 0m3.
      * ``collections`` — the union of physical collection names
        across reachable nodes; ``$step.collections`` feeds a
        downstream ``search(subtree=...)`` call so the next retrieval
        step is scoped to exactly the collections the traversal
        surfaced (SC-5).

    Either ``link_types`` (explicit list) or ``purpose`` (named alias
    via :func:`nexus.plans.purposes.resolve_purpose`) selects which
    edge labels to follow. Specifying both is a contract violation —
    SC-16, mirrored from the schema validator. Returns
    ``{"error": "..."}`` instead of raising so the MCP boundary stays
    string-typed for callers.

    ``depth`` is capped by ``Catalog._MAX_GRAPH_DEPTH``; traversal
    stops at ``Catalog._MAX_GRAPH_NODES`` merged nodes.
    """
    from nexus.catalog.tumbler import Tumbler
    from nexus.plans.purposes import resolve_purpose

    if link_types and purpose:
        return {
            "error": (
                "traverse: 'link_types' and 'purpose' are mutually exclusive "
                "(SC-16); pass exactly one"
            ),
            "tumblers": [], "ids": [], "collections": [],
        }

    resolved_link_types: list[str] = (
        list(link_types)
        if link_types
        else (resolve_purpose(purpose) if purpose else [])
    )

    catalog = _get_catalog()
    if catalog is None:
        return {
            "error": "traverse: catalog not initialised",
            "tumblers": [], "ids": [], "collections": [],
        }

    parsed_seeds: list[Tumbler] = []
    for s in seeds or []:
        try:
            parsed_seeds.append(Tumbler.parse(str(s)))
        except Exception as exc:
            return {
                "error": f"traverse: invalid seed {s!r}: {exc}",
                "tumblers": [], "ids": [], "collections": [],
            }

    result = catalog.graph_many(
        seeds=parsed_seeds,
        depth=depth,
        link_types=resolved_link_types,
        direction=direction,
    )

    tumblers: list[str] = [n["tumbler"] for n in result["nodes"]]
    collections: list[str] = sorted({
        n.get("physical_collection") for n in result["nodes"]
        if n.get("physical_collection")
    })
    return {
        "tumblers": tumblers,
        "ids": [],
        "collections": collections,
    }


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


# ── RDR-079 P3: operator MCP tools ───────────────────────────────────────────
#
# Five operators (extract/rank/compare/summarize/generate) each dispatch a
# structured turn to a per-operator pool worker. Per Empirical Finding 3,
# workers are spawned with --json-schema set; the model emits a
# StructuredOutput tool_use whose ``input`` is the validated payload. Tools
# intercept the tool_use, validate shape, return dict.
#
# All operator tools are filtered out when NEXUS_MCP_WORKER_MODE=1 via the
# _mcp_tool() decorator — this prevents a pool worker from re-entering the
# pool (invariant I-2).


_OPERATOR_SCHEMA_VERSION = 1


# RDR-079 P3 operator tools MUST be `async def` and `await` the pool
# directly. FastMCP runs its stdio transport on anyio; sync tools are
# called on the running event loop with no thread offload, so
# asyncio.run() from a sync tool raises "cannot be called from a
# running event loop". Converting to native async is the right fix —
# FastMCP's Tool.run supports async callables natively. (Review C-1.)


# Upper bound on operator `inputs` length — imported from runner.py
# (single source of truth, also used by auto-hydration overflow cap).
from nexus.plans.runner import _OPERATOR_MAX_INPUTS


async def _dispatch_with_auth_guard(
    pool, operator: str, *, prompt: str, timeout: float,
) -> dict:
    """Dispatch via the operator pool, converting auth failures to
    :class:`PlanRunOperatorUnavailableError` (SC-10).

    Retrieval tools continue to work when auth is missing — only
    operator-requiring calls surface this typed error so callers can
    branch on it without importing the pool's private exception type.
    """
    from nexus.operators.pool import PoolAuthUnavailableError
    from nexus.plans.runner import PlanRunOperatorUnavailableError
    try:
        return await pool.dispatch_with_rotation(prompt=prompt, timeout=timeout)
    except PoolAuthUnavailableError as exc:
        raise PlanRunOperatorUnavailableError(
            operator=operator, reason=str(exc),
        ) from exc


def _parse_inputs_json(operator: str, inputs: str | list) -> list:
    """Parse the ``inputs`` argument as a JSON array. Raise
    ``PlanRunOperatorOutputError`` with the operator name on failure
    so callers see a clear contract violation instead of an opaque
    model response to malformed prompts. (Review I-5.)

    Accepts both a JSON-array string (from direct MCP callers) and a
    Python list (from the plan runner, where ``$stepN.ids`` /
    ``$stepN.tumblers`` resolves to a list). This lets plan YAML
    reference retrieval fields directly without forcing every author
    to think about JSON serialization at the boundary.
    """
    from nexus.plans.runner import PlanRunOperatorOutputError

    if isinstance(inputs, list):
        if len(inputs) > _OPERATOR_MAX_INPUTS:
            raise PlanRunOperatorOutputError(
                operator=operator,
                reason=(
                    f"`inputs` has {len(inputs)} items; operator budget "
                    f"is {_OPERATOR_MAX_INPUTS}. Winnow via a preceding "
                    f"`rank` step before dispatching a wide fan-out."
                ),
            )
        return inputs

    if not isinstance(inputs, str):
        raise PlanRunOperatorOutputError(
            operator=operator,
            reason=f"`inputs` must be a JSON-array string or list, got {type(inputs).__name__}",
        )

    try:
        parsed = json.loads(inputs)
    except json.JSONDecodeError as exc:
        raise PlanRunOperatorOutputError(
            operator=operator,
            reason=f"`inputs` is not a valid JSON array: {exc}",
        ) from exc
    if not isinstance(parsed, list):
        raise PlanRunOperatorOutputError(
            operator=operator,
            reason=f"`inputs` must decode to a JSON array, got {type(parsed).__name__}",
        )
    if len(parsed) > _OPERATOR_MAX_INPUTS:
        raise PlanRunOperatorOutputError(
            operator=operator,
            reason=(
                f"`inputs` has {len(parsed)} items; operator budget is "
                f"{_OPERATOR_MAX_INPUTS}. Winnow via a preceding `rank` "
                f"step before dispatching a wide fan-out into the model."
            ),
        )
    return parsed


@_mcp_tool()
async def operator_extract(
    inputs: str,
    fields: str,
    schema_version: int = _OPERATOR_SCHEMA_VERSION,
    timeout: float = 60.0,
) -> dict:
    """Extract structured fields from a list of text inputs. RDR-079 P3.1.

    Args:
        inputs: JSON-array string of input texts (e.g. ``'["text1", "text2"]'``).
            Each element becomes one extraction.
        fields: Comma-separated field names to extract (e.g. ``"title,year,author"``).
            The model emits one object per input with these keys.
        schema_version: Operator contract version (pinned at 1 for RDR-079).
            Mismatched versions raise ``PlanRunOperatorSchemaVersionError``.
        timeout: Worker-dispatch timeout in seconds.

    Returns:
        ``{"extractions": [{<field>: <value>, ...}, ...]}`` — one dict per
        input, in the input's order. Missing fields are set to ``null`` by
        the model.

    Raises:
        ``PlanRunOperatorSchemaVersionError``: schema_version != 1.
        ``PlanRunOperatorOutputError``: worker output doesn't match contract.
    """
    from nexus.mcp_infra import get_operator_pool
    from nexus.plans.runner import (
        PlanRunOperatorOutputError,
        PlanRunOperatorSchemaVersionError,
    )

    if schema_version != _OPERATOR_SCHEMA_VERSION:
        raise PlanRunOperatorSchemaVersionError(
            operator="extract",
            received=schema_version,
            expected=_OPERATOR_SCHEMA_VERSION,
        )

    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    if not field_list:
        raise PlanRunOperatorOutputError(
            operator="extract",
            reason="`fields` is empty; at least one field name is required",
        )

    # I-5: validate inputs is a JSON array before sending to the worker.
    _parsed_inputs = _parse_inputs_json("extract", inputs)
    inputs = json.dumps(_parsed_inputs)

    # Per-operator pool. The pool's json_schema is the OUTER {extractions:
    # [dict]} shape — the model uses it as a structural constraint. The
    # per-call field list is encoded in the prompt so the model knows WHICH
    # keys to populate (RDR-079 Empirical Finding 3: CLI --json-schema is
    # per-spawn, not per-turn; caller's field list varies call-to-call,
    # so it lives in the prompt layer, not in the schema layer).
    pool = get_operator_pool(
        "extract",
        operator_role=(
            "You are the `extract` analytical operator. For each user "
            "turn you receive, emit a StructuredOutput tool_use whose "
            "input is {\"extractions\": [<object per input>, ...]}. Every "
            "extraction object must include the caller-requested field "
            "keys (null when the value is not present in the input). "
            "Return no prose."
        ),
        json_schema={
            "type": "object",
            "required": ["extractions"],
            "properties": {
                "extractions": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
        },
    )

    prompt = (
        f"Extract the fields [{', '.join(field_list)}] from each input "
        f"below. Inputs (JSON array): {inputs}"
    )
    payload = await _dispatch_with_auth_guard(
        pool, "extract", prompt=prompt, timeout=timeout,
    )

    # Validate contract.
    if not isinstance(payload, dict) or "extractions" not in payload:
        raise PlanRunOperatorOutputError(
            operator="extract",
            reason=(
                f"missing `extractions` key in worker output; "
                f"got keys={sorted((payload or {}).keys()) if isinstance(payload, dict) else type(payload).__name__}"
            ),
        )
    ext = payload["extractions"]
    if not isinstance(ext, list):
        raise PlanRunOperatorOutputError(
            operator="extract",
            reason=(
                f"`extractions` must be a list, got {type(ext).__name__}"
            ),
        )
    return {"extractions": ext}


@_mcp_tool()
async def operator_rank(
    criterion: str,
    inputs: str,
    schema_version: int = _OPERATOR_SCHEMA_VERSION,
    timeout: float = 60.0,
) -> dict:
    """Rank a list of text inputs by *criterion*. RDR-079 P3.2.

    Args:
        criterion: Natural-language ranking criterion
            (e.g. ``"most relevant to distributed consensus"``).
        inputs: JSON-array string of input texts.
        schema_version: Pinned at 1.
        timeout: Worker-dispatch timeout in seconds.

    Returns:
        ``{"ranked": [{"rank": int, "score": float, "input_index": int,
        "justification": str}, ...]}`` — every input index appears exactly
        once (no gaps, no duplicates). ``rank`` is 1-based, best first.

    Raises:
        ``PlanRunOperatorSchemaVersionError``, ``PlanRunOperatorOutputError``.
    """
    from nexus.mcp_infra import get_operator_pool
    from nexus.plans.runner import (
        PlanRunOperatorOutputError,
        PlanRunOperatorSchemaVersionError,
    )

    if schema_version != _OPERATOR_SCHEMA_VERSION:
        raise PlanRunOperatorSchemaVersionError(
            operator="rank",
            received=schema_version,
            expected=_OPERATOR_SCHEMA_VERSION,
        )

    # I-5: validate inputs is a JSON array, get the expected item count
    # for the coverage check below (I-4).
    input_list = _parse_inputs_json("rank", inputs)
    inputs = json.dumps(input_list)
    expected_indices = set(range(len(input_list)))

    pool = get_operator_pool(
        "rank",
        operator_role=(
            "You are the `rank` analytical operator. For each user turn "
            "you receive a ranking criterion and a JSON array of inputs. "
            "Emit a StructuredOutput tool_use whose input is "
            "{\"ranked\": [<one object per input>]} where each object has "
            "`rank` (1-based, best first), `score` (0.0-1.0), "
            "`input_index` (0-based index into the input array), and "
            "`justification` (1-2 sentences). Every input index must "
            "appear exactly once. Return no prose."
        ),
        json_schema={
            "type": "object",
            "required": ["ranked"],
            "properties": {
                "ranked": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["rank", "score", "input_index", "justification"],
                        "properties": {
                            "rank": {"type": "integer"},
                            "score": {"type": "number"},
                            "input_index": {"type": "integer"},
                            "justification": {"type": "string"},
                        },
                    },
                },
            },
        },
    )

    prompt = (
        f"Ranking criterion: {criterion}\n\n"
        f"Inputs (JSON array): {inputs}\n\n"
        f"Rank every input. Each input index must appear exactly once."
    )
    payload = await _dispatch_with_auth_guard(
        pool, "rank", prompt=prompt, timeout=timeout,
    )

    if not isinstance(payload, dict) or "ranked" not in payload:
        raise PlanRunOperatorOutputError(
            operator="rank",
            reason="missing `ranked` key in worker output",
        )
    ranked = payload["ranked"]
    if not isinstance(ranked, list):
        raise PlanRunOperatorOutputError(
            operator="rank",
            reason=f"`ranked` must be a list, got {type(ranked).__name__}",
        )

    # I-4: enforce the docstring's "every input index appears exactly once"
    # guarantee. Silent dropped or duplicated entries are a correctness bug
    # at the plan-runner level — surface it here with a clear error.
    seen: set[int] = set()
    for item in ranked:
        if not isinstance(item, dict) or "input_index" not in item:
            raise PlanRunOperatorOutputError(
                operator="rank",
                reason=f"ranked item missing `input_index`: {item!r}",
            )
        idx = item["input_index"]
        if idx in seen:
            raise PlanRunOperatorOutputError(
                operator="rank",
                reason=f"duplicate `input_index={idx}` in ranked output",
            )
        seen.add(idx)
    if seen != expected_indices:
        missing = expected_indices - seen
        extra = seen - expected_indices
        raise PlanRunOperatorOutputError(
            operator="rank",
            reason=(
                f"ranked output does not cover all inputs: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            ),
        )
    return {"ranked": ranked}


@_mcp_tool()
async def operator_compare(
    inputs: str,
    criterion: str = "",
    schema_version: int = _OPERATOR_SCHEMA_VERSION,
    timeout: float = 60.0,
) -> dict:
    """Compare a list of text inputs. RDR-079 P3.3.

    Args:
        inputs: JSON-array string of input texts (typically 2+).
        criterion: Optional comparison criterion (e.g.
            ``"methodology"``); empty for general comparison.
        schema_version: Pinned at 1.
        timeout: Worker-dispatch timeout.

    Returns:
        ``{"agreements": [str, ...], "conflicts": [str, ...],
        "gaps": [str, ...]}`` — lists of observations in each category.
    """
    from nexus.mcp_infra import get_operator_pool
    from nexus.plans.runner import (
        PlanRunOperatorOutputError,
        PlanRunOperatorSchemaVersionError,
    )

    if schema_version != _OPERATOR_SCHEMA_VERSION:
        raise PlanRunOperatorSchemaVersionError(
            operator="compare",
            received=schema_version,
            expected=_OPERATOR_SCHEMA_VERSION,
        )

    _parsed_inputs = _parse_inputs_json("compare", inputs)
    inputs = json.dumps(_parsed_inputs)

    pool = get_operator_pool(
        "compare",
        operator_role=(
            "You are the `compare` analytical operator. For each user "
            "turn you receive a list of inputs (and optionally a "
            "criterion). Emit a StructuredOutput tool_use whose input "
            "is {\"agreements\": [str], \"conflicts\": [str], "
            "\"gaps\": [str]}. Each list contains short string "
            "observations. Return no prose."
        ),
        json_schema={
            "type": "object",
            "required": ["agreements", "conflicts", "gaps"],
            "properties": {
                "agreements": {"type": "array", "items": {"type": "string"}},
                "conflicts": {"type": "array", "items": {"type": "string"}},
                "gaps": {"type": "array", "items": {"type": "string"}},
            },
        },
    )

    crit = f"Criterion: {criterion}\n\n" if criterion else ""
    prompt = (
        f"{crit}Compare the inputs below. Identify agreements, conflicts, "
        f"and gaps.\n\nInputs (JSON array): {inputs}"
    )
    payload = await _dispatch_with_auth_guard(
        pool, "compare", prompt=prompt, timeout=timeout,
    )

    if not isinstance(payload, dict):
        raise PlanRunOperatorOutputError(
            operator="compare",
            reason=f"worker output is not a dict, got {type(payload).__name__}",
        )
    for key in ("agreements", "conflicts", "gaps"):
        if key not in payload:
            raise PlanRunOperatorOutputError(
                operator="compare",
                reason=f"missing `{key}` key in worker output",
            )
        if not isinstance(payload[key], list):
            raise PlanRunOperatorOutputError(
                operator="compare",
                reason=f"`{key}` must be a list, got {type(payload[key]).__name__}",
            )
    return {
        "agreements": payload["agreements"],
        "conflicts": payload["conflicts"],
        "gaps": payload["gaps"],
    }


@_mcp_tool()
async def operator_summarize(
    inputs: str,
    mode: str = "short",
    schema_version: int = _OPERATOR_SCHEMA_VERSION,
    timeout: float = 60.0,
) -> dict:
    """Summarize a list of text inputs. RDR-079 P3.4.

    Args:
        inputs: JSON-array string of input texts.
        mode: ``"short"`` (one-paragraph) | ``"detailed"`` (multi-paragraph)
            | ``"evidence"`` (claim → citation per input).
        schema_version: Pinned at 1.
        timeout: Worker-dispatch timeout.

    Returns:
        ``{"text": str, "citations": [{"input_index": int, "span": str}, ...]}``.
    """
    from nexus.mcp_infra import get_operator_pool
    from nexus.plans.runner import (
        PlanRunOperatorOutputError,
        PlanRunOperatorSchemaVersionError,
    )

    if schema_version != _OPERATOR_SCHEMA_VERSION:
        raise PlanRunOperatorSchemaVersionError(
            operator="summarize",
            received=schema_version,
            expected=_OPERATOR_SCHEMA_VERSION,
        )

    _parsed_inputs = _parse_inputs_json("summarize", inputs)
    inputs = json.dumps(_parsed_inputs)

    pool = get_operator_pool(
        "summarize",
        operator_role=(
            "You are the `summarize` analytical operator. For each user "
            "turn you receive a list of inputs and a mode. Emit a "
            "StructuredOutput tool_use whose input is {\"text\": str, "
            "\"citations\": [{\"input_index\": int, \"span\": str}]}. "
            "Mode=short: one paragraph. Mode=detailed: multi-paragraph. "
            "Mode=evidence: every claim paired with an input_index "
            "citation. Return no prose outside the tool_use."
        ),
        json_schema={
            "type": "object",
            "required": ["text", "citations"],
            "properties": {
                "text": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["input_index", "span"],
                        "properties": {
                            "input_index": {"type": "integer"},
                            "span": {"type": "string"},
                        },
                    },
                },
            },
        },
    )

    prompt = (
        f"Mode: {mode}\n\n"
        f"Summarize the inputs below.\n\n"
        f"Inputs (JSON array): {inputs}"
    )
    payload = await _dispatch_with_auth_guard(
        pool, "summarize", prompt=prompt, timeout=timeout,
    )

    if not isinstance(payload, dict):
        raise PlanRunOperatorOutputError(
            operator="summarize",
            reason=f"worker output is not a dict, got {type(payload).__name__}",
        )
    if "text" not in payload or not isinstance(payload["text"], str):
        raise PlanRunOperatorOutputError(
            operator="summarize",
            reason="missing or non-string `text` in worker output",
        )
    citations = payload.get("citations", [])
    if not isinstance(citations, list):
        raise PlanRunOperatorOutputError(
            operator="summarize",
            reason=f"`citations` must be a list, got {type(citations).__name__}",
        )
    return {"text": payload["text"], "citations": citations}


@_mcp_tool()
async def operator_generate(
    outline: str,
    inputs: str,
    with_citations: bool = True,
    schema_version: int = _OPERATOR_SCHEMA_VERSION,
    timeout: float = 90.0,
) -> dict:
    """Generate synthesis text from a list of inputs + an outline. RDR-079 P3.5.

    Distinguished from ``summarize``: generate produces NEW synthesis
    conditioned on a caller-supplied outline; summarize reduces inputs
    to a tighter restatement.

    Args:
        outline: What the generated text should cover (e.g.
            ``"mechanism of consensus in the Delos paper"``).
        inputs: JSON-array string of source inputs.
        with_citations: Whether to require per-claim citations.
        schema_version: Pinned at 1.
        timeout: Worker-dispatch timeout (longer default — generation
            is typically the heaviest operator).

    Returns:
        ``{"text": str, "citations": [{"input_index": int, "span": str}, ...]}``.
    """
    from nexus.mcp_infra import get_operator_pool
    from nexus.plans.runner import (
        PlanRunOperatorOutputError,
        PlanRunOperatorSchemaVersionError,
    )

    if schema_version != _OPERATOR_SCHEMA_VERSION:
        raise PlanRunOperatorSchemaVersionError(
            operator="generate",
            received=schema_version,
            expected=_OPERATOR_SCHEMA_VERSION,
        )

    _parsed_inputs = _parse_inputs_json("generate", inputs)
    inputs = json.dumps(_parsed_inputs)

    citation_req = (
        "Every non-trivial claim must be paired with an input_index "
        "citation (no citation → gap in the outline)."
        if with_citations
        else "Citations are optional; omit `citations` or pass an empty list."
    )
    # I-3: keep `citations` in `required` only when the caller actually
    # wants them. The `with_citations=False` flag must relax both the
    # prose instruction AND the structural schema, otherwise the CLI's
    # StructuredOutput enforcement will always force citations regardless
    # of caller intent.
    generate_schema: dict = {
        "type": "object",
        "required": ["text"] + (["citations"] if with_citations else []),
        "properties": {
            "text": {"type": "string"},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["input_index", "span"],
                    "properties": {
                        "input_index": {"type": "integer"},
                        "span": {"type": "string"},
                    },
                },
            },
        },
    }

    # Per-operator pool must differentiate by with_citations because the
    # schema differs. Cache key includes the flag.
    pool_name = f"generate:{'with-cite' if with_citations else 'no-cite'}"
    pool = get_operator_pool(
        pool_name,
        operator_role=(
            "You are the `generate` analytical operator. For each user "
            "turn you receive an outline and a list of inputs. Produce "
            "NEW prose (not a summary of the inputs) that covers the "
            "outline, grounded in the inputs. Emit a StructuredOutput "
            "tool_use whose input is {\"text\": str, \"citations\": "
            "[{\"input_index\": int, \"span\": str}]}. " + citation_req
        ),
        json_schema=generate_schema,
    )

    prompt = (
        f"Outline: {outline}\n\n"
        f"Generate prose covering the outline, grounded in the inputs "
        f"below.\n\n"
        f"Inputs (JSON array): {inputs}"
    )
    payload = await _dispatch_with_auth_guard(
        pool, "generate", prompt=prompt, timeout=timeout,
    )

    if not isinstance(payload, dict):
        raise PlanRunOperatorOutputError(
            operator="generate",
            reason=f"worker output is not a dict, got {type(payload).__name__}",
        )
    if "text" not in payload or not isinstance(payload["text"], str):
        raise PlanRunOperatorOutputError(
            operator="generate",
            reason="missing or non-string `text` in worker output",
        )
    citations = payload.get("citations", [])
    if not isinstance(citations, list):
        raise PlanRunOperatorOutputError(
            operator="generate",
            reason=f"`citations` must be a list, got {type(citations).__name__}",
        )
    return {"text": payload["text"], "citations": citations}


# ── nx_answer helpers (RDR-080 P1) ────────────────────────────────────────────


def _nx_answer_match_is_hit(confidence: float | None) -> bool:
    """Return True when a plan_match confidence qualifies as a hit.

    ``confidence is None`` (FTS5 sentinel, RF-11) is always a hit.
    Numeric confidence must be >= 0.40 (RDR-079 P5 calibration).
    """
    if confidence is None:
        return True
    return confidence >= 0.40


def _nx_answer_classify_plan(match: Any) -> str:
    """Classify a matched plan: ``"single_query"`` | ``"retrieval_only"`` | ``"needs_operators"``.

    Parses ``match.plan_json`` once (S-4 fix). The caller branches on the
    returned string instead of calling two separate helpers that each parse.
    """
    from nexus.plans.runner import _OPERATOR_TOOL_MAP

    _OPERATOR_TOOLS = frozenset(_OPERATOR_TOOL_MAP.keys())
    try:
        plan = json.loads(match.plan_json)
    except (json.JSONDecodeError, TypeError):
        return "needs_operators"  # Assume operators needed when unparseable.
    steps = plan.get("steps") or []
    if len(steps) == 1 and steps[0].get("tool") == "query":
        return "single_query"
    if any(step.get("tool", "") in _OPERATOR_TOOLS for step in steps):
        return "needs_operators"
    return "retrieval_only"


# Backward-compat shims used by tests.
def _nx_answer_is_single_query(match: Any) -> bool:
    return _nx_answer_classify_plan(match) == "single_query"


def _nx_answer_needs_operators(match: Any) -> bool:
    return _nx_answer_classify_plan(match) == "needs_operators"


def _nx_answer_translate_extract_fields(args: dict[str, Any]) -> dict[str, Any]:
    """RF-13: translate old ``template`` dict to ``fields`` CSV.

    ``analytical-operator`` accepted ``params.template`` (dict), but
    ``operator_extract`` takes ``fields`` (CSV). Translate on the fly.
    """
    if "template" in args and "fields" not in args:
        template = args["template"]
        if isinstance(template, dict):
            args = {k: v for k, v in args.items() if k != "template"}
            args["fields"] = ",".join(template.keys())
    return args


def _nx_answer_record_run(
    conn: Any,
    *,
    question: str,
    plan_id: int | None,
    matched_confidence: float | None,
    step_count: int,
    final_text: str,
    cost_usd: float,
    duration_ms: int,
    trace: bool,
) -> None:
    """Write one row to ``nx_answer_runs``.  Redacts when ``trace=False``.

    Lazily creates the table if missing (I-5: migration 4.5.0 won't run
    until version bump, so ensure the table exists regardless).
    """
    from nexus.db.migrations import migrate_nx_answer_runs

    migrate_nx_answer_runs(conn)  # Idempotent — no-op if table exists.
    q = question if trace else "[redacted]"
    text = final_text if trace else "[redacted]"
    conn.execute(
        """INSERT INTO nx_answer_runs
           (question, plan_id, matched_confidence, step_count,
            final_text, cost_usd, duration_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (q, plan_id, matched_confidence, step_count, text, cost_usd, duration_ms),
    )
    conn.commit()


# ── Plan-miss planner (RDR-080 P1, C-1 critique remediation) ────────────────

_PLANNER_SCHEMA: dict = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["tool", "args"],
                "additionalProperties": False,
                "properties": {
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                },
            },
        },
    },
    "additionalProperties": False,
}


async def _nx_answer_plan_miss(
    question: str,
    *,
    scope: str = "",
    max_steps: int = 6,
) -> Any:
    """Decompose *question* into a plan via the operator pool, execute it,
    and save the plan for future reuse.

    Returns a synthetic :class:`Match` that ``nx_answer`` can execute
    via ``plan_run``.
    """
    from nexus.mcp_infra import get_operator_pool
    from nexus.plans.match import Match

    pool = get_operator_pool(
        "planner",
        operator_role=(
            "You are the `planner` decomposition operator. Given a question, "
            "produce a retrieval-and-analysis plan as a StructuredOutput. "
            "ONLY use these tool names in plan steps (bare names, no prefixes): "
            "search, query, traverse, extract, rank, compare, summarize, generate. "
            "Do NOT use fully-qualified MCP names like mcp__plugin_nx_nexus__search. "
            "Do NOT use tools from other servers (serena, context7, etc.). "
            "Use $intent as a placeholder for the original question. "
            "Use $stepN.ids, $stepN.tumblers, $stepN.text for step references. "
            "Keep plans concise — prefer fewer steps. "
            "Do NOT execute the plan — only produce the JSON structure."
        ),
        json_schema=_PLANNER_SCHEMA,
    )

    corpus_hint = f" Focus on the '{scope}' corpus." if scope else ""
    prompt = (
        f"Decompose this question into a retrieval-and-analysis plan "
        f"with at most {max_steps} steps:{corpus_hint}\n\n{question}"
    )

    payload = await _dispatch_with_auth_guard(
        pool, "planner", prompt=prompt, timeout=120.0,
    )

    steps = payload.get("steps", []) if isinstance(payload, dict) else []
    if not steps:
        raise ValueError("planner returned empty plan")

    # Validate: only nexus-dispatchable tools are allowed in plans.
    _ALLOWED_TOOLS = {
        "search", "query", "traverse", "store_get_many",
        "extract", "rank", "compare", "summarize", "generate",
    }
    for step in steps:
        raw_tool = step.get("tool", "")
        # Strip any MCP prefix for validation.
        bare = raw_tool.rsplit("__", 1)[-1] if raw_tool.startswith("mcp__") else raw_tool
        if bare not in _ALLOWED_TOOLS:
            raise ValueError(
                f"planner generated non-dispatchable tool '{raw_tool}' "
                f"(bare: '{bare}'). Allowed: {sorted(_ALLOWED_TOOLS)}"
            )
        # Normalize to bare name in the plan.
        step["tool"] = bare

    plan_json = json.dumps({"steps": steps})

    # Do NOT save the plan here — save after plan_run succeeds in
    # nx_answer. Saving before execution caches broken plans that
    # cause repeated failures on future plan_match hits (I-6).
    return Match(
        plan_id=0,  # Synthetic — not yet looked up from T2.
        name="ad-hoc",
        description=question,
        confidence=None,
        dimensions={},
        tags="ad-hoc",
        plan_json=plan_json,
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={"intent": question},
        parent_dims=None,
    )


@_mcp_tool()
async def nx_answer(
    question: str,
    scope: str = "",
    context: str = "",
    max_steps: int = 6,
    budget_usd: float = 0.25,
    trace: bool = True,
) -> str:
    """Answer a knowledge question using plan-match-first retrieval. RDR-080 P1.

    Internal flow (all in-process; no subagent spawn):

    1. **Plan-match gate**: call ``plan_match(intent=question)``. On hit
       (confidence >= 0.40 OR confidence is None / FTS5 sentinel), proceed
       to execution. On miss, dispatch an inline LLM planner to decompose
       the question, execute the resulting plan, and ``plan_save(ttl=30)``
       for future reuse.
    2. **Single-step guard**: if the matched plan has exactly 1 step and
       that step is ``query``, reroute to ``query()`` directly.
    3. **Execute plan**: reuse ``plan_run`` from RDR-078/079.
    4. **Record**: write run metrics to T2 ``nx_answer_runs``.

    Args:
        question: Natural-language question to answer.
        scope: Catalog subtree or corpus filter (e.g. ``"1.2"`` or ``"knowledge"``).
        context: Supplementary caller-supplied context for the plan matcher.
        max_steps: Cap on plan DAG size (reserved for P5 enforcement).
        budget_usd: Per-invocation cost cap (reserved for P5 enforcement).
        trace: When False, redacts question and final_text in the run log.

    Returns:
        The final step's output as a human-readable string.
    """
    import time

    import structlog

    from nexus.mcp_infra import get_t1_plan_cache
    from nexus.plans.matcher import plan_match as _plan_match
    from nexus.plans.runner import plan_run as _plan_run

    _log = structlog.get_logger()
    start = time.monotonic()

    # ── Step 1: plan-match gate ──────────────────────────────────────────
    try:
        with _t2_ctx() as db:
            cache = get_t1_plan_cache(populate_from=db.plans)
            matches = _plan_match(
                question,
                library=db.plans,
                cache=cache,
                scope_preference=scope,
                context={"user_context": context} if context else None,
                min_confidence=0.40,
                n=5,
            )
    except Exception as exc:
        return f"Error during plan match: {exc}"

    if not matches or not _nx_answer_match_is_hit(matches[0].confidence):
        # Plan miss — dispatch inline LLM planner to decompose the question.
        # SC-9: check auth before attempting the planner dispatch.
        from nexus.operators.pool import PoolAuthUnavailableError, check_auth

        try:
            check_auth()
        except PoolAuthUnavailableError:
            _log.warning("nx_answer_plan_miss_no_auth")
            return (
                "No matching plan found, and the operator pool is "
                "unavailable (no auth) so the question cannot be "
                "decomposed. Retrieval-only plan-hits still work."
            )

        _log.info(
            "nx_answer_plan_miss",
            question=question[:100] if trace else "[redacted]",
        )
        try:
            best = await _nx_answer_plan_miss(
                question, scope=scope, max_steps=max_steps,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _log.warning("nx_answer_planner_failed", error=str(exc))
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.conn,
                        question=question,
                        plan_id=None,
                        matched_confidence=matches[0].confidence if matches else None,
                        step_count=0,
                        final_text=f"Planner error: {exc}",
                        cost_usd=0.0,
                        duration_ms=elapsed_ms,
                        trace=trace,
                    )
            except Exception:
                pass
            return f"Error: plan-miss planner failed: {exc}"
    else:
        best = matches[0]

    if best.plan_id == 0:
        conf_str = "ad-hoc"
    elif best.confidence is None:
        conf_str = "fts5"
    else:
        conf_str = f"{best.confidence:.3f}"

    # ── Step 2: classify plan (single parse — S-4 fix) ─────────────────
    plan_class = _nx_answer_classify_plan(best)

    if plan_class == "single_query":
        _log.info(
            "nx_answer_single_step_guard",
            plan_id=best.plan_id,
            confidence=conf_str,
        )
        try:
            plan = json.loads(best.plan_json)
            step_args = plan["steps"][0].get("args", {})
            q = step_args.get("question", question)
            corpus = step_args.get("corpus", "knowledge")
            result_text = query(question=q, corpus=corpus)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.conn,
                        question=question,
                        plan_id=best.plan_id,
                        matched_confidence=best.confidence,
                        step_count=1,
                        final_text=str(result_text)[:2000],
                        cost_usd=0.0,
                        duration_ms=elapsed_ms,
                        trace=trace,
                    )
            except Exception:
                pass
            return str(result_text)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.conn,
                        question=question,
                        plan_id=best.plan_id,
                        matched_confidence=best.confidence,
                        step_count=1,
                        final_text=f"Error: {exc}",
                        cost_usd=0.0,
                        duration_ms=elapsed_ms,
                        trace=trace,
                    )
            except Exception:
                pass
            return f"Error in single-step query: {exc}"

    # ── Step 2.5: SC-9 graceful degradation ──────────────────────────────
    if plan_class == "needs_operators":
        from nexus.operators.pool import PoolAuthUnavailableError, check_auth

        try:
            check_auth()
        except PoolAuthUnavailableError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _log.warning(
                "nx_answer_no_auth_operators_required",
                plan_id=best.plan_id,
                duration_ms=elapsed_ms,
            )
            msg = (
                "This question requires operator processing (extract/rank/"
                "compare/summarize/generate), but the operator pool is "
                "unavailable (no auth). Retrieval-only plans still work."
            )
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.conn,
                        question=question,
                        plan_id=best.plan_id,
                        matched_confidence=best.confidence,
                        step_count=0,
                        final_text=msg,
                        cost_usd=0.0,
                        duration_ms=elapsed_ms,
                        trace=trace,
                    )
            except Exception:
                pass
            return msg

    # ── Step 3: seed link-context ────────────────────────────────────────
    try:
        scratch(
            action="put",
            content=json.dumps({
                "question": question,
                "scope": scope,
                "plan_id": best.plan_id,
            }),
            tags="link-context",
        )
    except Exception:
        pass  # Best-effort; auto-linker will work without it.

    # ── Step 4: execute plan ─────────────────────────────────────────────
    try:
        result = await _plan_run(best, {"intent": question})
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log.error(
            "nx_answer_plan_run_error",
            plan_id=best.plan_id,
            error=str(exc),
            duration_ms=elapsed_ms,
        )
        try:
            with _t2_ctx() as db:
                _nx_answer_record_run(
                    db.conn,
                    question=question,
                    plan_id=best.plan_id,
                    matched_confidence=best.confidence,
                    step_count=0,
                    final_text=f"Error: {exc}",
                    cost_usd=0.0,
                    duration_ms=elapsed_ms,
                    trace=trace,
                )
        except Exception:
            pass
        return f"Error during plan execution: {exc}"

    # ── Step 5: extract final answer ─────────────────────────────────────
    elapsed_ms = int((time.monotonic() - start) * 1000)
    final_step = result.steps[-1] if result.steps else {}
    # Prefer text > summary > answer keys for the final output.
    text_key = next(
        (k for k in ("text", "summary", "answer") if k in final_step), None,
    )
    final_text = str(final_step.get(text_key, "")) if text_key else json.dumps(final_step)

    _log.info(
        "nx_answer_complete",
        plan_id=best.plan_id,
        confidence=conf_str,
        step_count=len(result.steps),
        duration_ms=elapsed_ms,
    )

    # ── Step 5.5: save ad-hoc plans on success (I-6 fix) ────────────────
    if best.plan_id == 0:
        try:
            plan_save(
                query=question,
                plan_json=best.plan_json,
                outcome="success",
                ttl=30,
            )
        except Exception:
            pass  # Best-effort; plan is cached for 30 days.

    # ── Step 6: record run ───────────────────────────────────────────────
    # TODO(RDR-080 P5): populate cost_usd from pool worker token counters.
    try:
        with _t2_ctx() as db:
            _nx_answer_record_run(
                db.conn,
                question=question,
                plan_id=best.plan_id,
                matched_confidence=best.confidence,
                step_count=len(result.steps),
                final_text=final_text[:2000],
                cost_usd=0.0,
                duration_ms=elapsed_ms,
                trace=trace,
            )
    except Exception:
        pass  # Best-effort recording.

    return final_text


# ── P3 consolidation tools (RDR-080) ─────────────────────────────────────────


@_mcp_tool()
async def nx_tidy(
    topic: str,
    collection: str = "knowledge",
    timeout: float = 120.0,
) -> str:
    """Consolidate knowledge entries on *topic*. RDR-080 P3.

    Replaces the ``knowledge-tidier`` agent. Dispatches to the operator
    pool: the worker searches T3 for entries matching *topic*, identifies
    duplicates and contradictions, and returns a consolidated summary.

    Args:
        topic: The knowledge topic to consolidate (e.g. "chromadb quotas").
        collection: T3 collection to search (default: knowledge).
        timeout: Worker-dispatch timeout in seconds.

    Returns:
        Consolidated summary as a human-readable string.
    """
    from nexus.mcp_infra import get_operator_pool

    pool = get_operator_pool(
        "tidy",
        operator_role=(
            "You are the `tidy` knowledge consolidation operator. You have "
            "access to nx MCP tools (search, query, store_put, store_get). "
            "For each user turn: (1) search the specified collection for "
            "entries matching the topic, (2) identify duplicates, contradictions, "
            "and outdated entries, (3) emit a StructuredOutput with "
            '{"summary": "<consolidated text>", "actions": [{"action": "...", '
            '"entry_id": "...", "reason": "..."}]}. Return no prose outside '
            "the structured output."
        ),
        json_schema={
            "type": "object",
            "required": ["summary", "actions"],
            "properties": {
                "summary": {"type": "string"},
                "actions": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
        },
    )

    prompt = (
        f"Consolidate knowledge entries about '{topic}' in collection "
        f"'{collection}'. Search for all related entries, identify duplicates "
        f"or contradictions, and produce a consolidated summary."
    )
    payload = await _dispatch_with_auth_guard(
        pool, "tidy", prompt=prompt, timeout=timeout,
    )

    summary = payload.get("summary", "") if isinstance(payload, dict) else str(payload)
    actions = payload.get("actions", []) if isinstance(payload, dict) else []

    lines = [summary]
    if actions:
        lines.append(f"\n{len(actions)} action(s) suggested.")
    return "\n".join(lines)


@_mcp_tool()
async def nx_enrich_beads(
    bead_description: str,
    context: str = "",
    timeout: float = 120.0,
) -> str:
    """Enrich a bead with execution context. RDR-080 P3.

    Replaces the ``plan-enricher`` agent. Dispatches to the operator
    pool: the worker searches the codebase for relevant file paths,
    code patterns, constraints, and test commands, then returns enriched
    markdown.

    Args:
        bead_description: The bead's title and description to enrich.
        context: Optional additional context (e.g. audit findings).
        timeout: Worker-dispatch timeout in seconds.

    Returns:
        Enriched bead markdown as a human-readable string.
    """
    from nexus.mcp_infra import get_operator_pool

    pool = get_operator_pool(
        "enrich",
        operator_role=(
            "You are the `enrich` bead enrichment operator. You have access "
            "to nx MCP tools (search, query) for codebase exploration. "
            "For each user turn: (1) analyze the bead description, (2) search "
            "the codebase for relevant files, symbols, and patterns, (3) emit "
            "a StructuredOutput with "
            '{"enriched_description": "<full enriched markdown>", '
            '"key_files": ["<path>", ...], "test_commands": ["<cmd>", ...], '
            '"constraints": ["<constraint>", ...]}. '
            "Return no prose outside the structured output."
        ),
        json_schema={
            "type": "object",
            "required": ["enriched_description"],
            "properties": {
                "enriched_description": {"type": "string"},
                "key_files": {"type": "array", "items": {"type": "string"}},
                "test_commands": {"type": "array", "items": {"type": "string"}},
                "constraints": {"type": "array", "items": {"type": "string"}},
            },
        },
    )

    prompt = f"Enrich this bead with execution context:\n\n{bead_description}"
    if context:
        prompt += f"\n\nAdditional context:\n{context}"

    payload = await _dispatch_with_auth_guard(
        pool, "enrich", prompt=prompt, timeout=timeout,
    )

    enriched = (
        payload.get("enriched_description", "")
        if isinstance(payload, dict) else str(payload)
    )
    return enriched


@_mcp_tool()
async def nx_plan_audit(
    plan_json: str,
    context: str = "",
    timeout: float = 120.0,
) -> str:
    """Audit a plan for correctness and codebase alignment. RDR-080 P3.

    Replaces the ``plan-auditor`` agent. Dispatches to the operator
    pool: the worker validates the plan's file paths, dependencies,
    and assumptions against the current codebase state.

    Args:
        plan_json: The plan to audit (JSON string or free-text description).
        context: Optional additional context (e.g. RDR reference).
        timeout: Worker-dispatch timeout in seconds.

    Returns:
        Audit verdict as a human-readable string.
    """
    from nexus.mcp_infra import get_operator_pool

    pool = get_operator_pool(
        "audit",
        operator_role=(
            "You are the `audit` plan validation operator. You have access "
            "to nx MCP tools (search, query) for codebase verification. "
            "For each user turn: (1) parse the plan, (2) verify file paths "
            "exist, (3) check dependency ordering, (4) identify gaps or "
            "incorrect assumptions, (5) emit a StructuredOutput with "
            '{"verdict": "pass|fail|warn", "findings": [{"severity": '
            '"critical|important|suggestion", "title": "...", "detail": '
            '"...", "fix": "..."}], "summary": "<one-line verdict>"}. '
            "Return no prose outside the structured output."
        ),
        json_schema={
            "type": "object",
            "required": ["verdict", "findings", "summary"],
            "properties": {
                "verdict": {"type": "string"},
                "findings": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "summary": {"type": "string"},
            },
        },
    )

    prompt = f"Audit this plan for correctness and codebase alignment:\n\n{plan_json}"
    if context:
        prompt += f"\n\nContext:\n{context}"

    payload = await _dispatch_with_auth_guard(
        pool, "audit", prompt=prompt, timeout=timeout,
    )

    if isinstance(payload, dict):
        verdict = payload.get("verdict", "unknown")
        summary = payload.get("summary", "")
        findings = payload.get("findings", [])
        lines = [f"Verdict: {verdict}", summary]
        for f in findings:
            sev = f.get("severity", "?")
            title = f.get("title", "")
            lines.append(f"  [{sev}] {title}")
        return "\n".join(lines)
    return str(payload)


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    """Run the core MCP server on stdio transport."""
    from nexus.logging_setup import configure_logging
    from nexus.mcp_infra import check_version_compatibility

    configure_logging("mcp")
    check_version_compatibility()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
