# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP core tools: search, store, memory, scratch, collections, plans.

14 registered tools + 3 demoted (plain functions, no @mcp.tool()).
"""
from __future__ import annotations

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
from nexus.config import load_config
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

# Minimum effective timeout for claude -p subagent tools. Planning and
# enrichment agents have been observed passing 180s / 300s overrides
# that re-trigger the class of false-positive timeouts v4.5.3 raised
# the defaults to prevent (see bead nexus-7sbf). The floor clamps
# caller-supplied values upward so agents can raise but not lower
# the effective budget.
_SUBAGENT_TIMEOUT_FLOOR = 300.0


def _clamp_subagent_timeout(requested: float, tool_name: str) -> float:
    """Clamp a caller-supplied subagent timeout to the floor.

    Emits a structured warning when a caller's requested timeout is
    below the floor so the override is visible in logs without
    blocking the call.
    """
    if requested < _SUBAGENT_TIMEOUT_FLOOR:
        import structlog
        structlog.get_logger().warning(
            "subagent_timeout_clamped",
            tool=tool_name,
            requested=requested,
            floor=_SUBAGENT_TIMEOUT_FLOOR,
        )
        return _SUBAGENT_TIMEOUT_FLOOR
    return requested

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
    threshold: float | None = None,
) -> "str | dict":
    """Semantic search across T3 collections. Paged results (``offset=N`` for next page).

    Args:
        query: Search query string
        corpus: Corpus prefixes or collection names, comma-separated. "all" for everything.
        limit: Page size (default 10)
        offset: Skip N results for pagination (default 0)
        where: Metadata filter (KEY=VALUE, comma-separated). Ops: = >= <= > < !=
        cluster_by: "semantic" for topic/Ward clustering (default), empty to disable
        topic: Pre-filter to documents in this topic label (from nx taxonomy discover)
        structured: Return ``{ids, tumblers, distances, collections}`` dict instead
            of human-readable string.  Used by the plan runner so ``$stepN.ids``
            references resolve to actual chunk IDs.
        threshold: Override the per-collection distance threshold uniformly
            (raw cosine distance, lower is stricter). Pass ``float('inf')``
            to disable filtering entirely. ``None`` (default) uses per-corpus
            config thresholds. RDR-087 Phase 1.1 workaround for silent
            threshold-drop on dense-prose collections.
    """
    try:
        from nexus.config import load_config
        from nexus.filters import sanitize_query
        from nexus.search_engine import search_cross_corpus

        cfg = load_config()
        if cfg.get("search", {}).get("query_sanitizer", True):
            query = sanitize_query(query)

        t3 = _get_t3()
        all_names = _get_collection_names()

        if corpus == "all":
            # True "all": every unique prefix that appears in the live
            # collection list. Fixes the gap where the old constant
            # ("knowledge,code,docs,rdr") missed projects whose only
            # collection is e.g. rdr__* or a custom prefix.
            seen_prefixes: list[str] = []
            for n in all_names:
                prefix = n.split("__", 1)[0]
                if prefix and prefix not in seen_prefixes:
                    seen_prefixes.append(prefix)
            corpus = ",".join(seen_prefixes) if seen_prefixes else "knowledge,code,docs,rdr"

        target: list[str] = []
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
        # Always pass taxonomy for topic grouping + topic boost (RDR-070).
        # Wrapped in context manager to avoid connection leak.
        with _t2_ctx() as _t2_db:
            # Note: no ``diagnostics_out`` — MCP does not emit stderr
            # (RDR-087 Phase 1 scope is CLI-only).
            # ``telemetry`` wired for RDR-087 Phase 2.2 hot-path logging;
            # opt-out via ``telemetry.search_enabled=false`` in .nexus.yml.
            results = search_cross_corpus(
                query, target, n_results=fetch_n, t3=t3, where=where_dict,
                cluster_by=cluster_by or None,
                catalog=_get_catalog(),
                link_boost=False,
                taxonomy=_t2_db.taxonomy,
                topic=topic or None,
                threshold_override=threshold,
                telemetry=_t2_db.telemetry,
            )
        # Only sort by distance for flat (non-clustered) results.
        # Clustered results arrive in cluster-grouped order from search_engine.
        if not clustered:
            results.sort(key=lambda r: r.distance)
        if not results:
            if structured:
                return {
                    "ids": [], "tumblers": [], "distances": [],
                    "collections": [], "chunk_collections": [],
                    "chunk_text_hash": [],
                }
            return "No results."

        # Apply pagination
        total = len(results)
        page = results[offset:offset + limit]
        if not page:
            if structured:
                return {
                    "ids": [], "tumblers": [], "distances": [],
                    "collections": [], "chunk_collections": [],
                    "chunk_text_hash": [],
                }
            return f"No results at offset {offset} (total {total})."

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

        # Structured return for plan-runner step output contract.
        # Resolves $stepN.ids / $stepN.collections / $stepN.distances refs.
        # RDR-086 Phase 3.1: chunk_text_hash forwarded per-result so callers
        # can build chash:<hex> citations without a second fetch.
        #
        # Review #7: ``collections`` is dedup'd (plan-runner contract) while
        # ``chunk_collections`` is per-result aligned with ``ids`` so
        # consumers that need per-chunk origin (e.g. ``nx_answer``) get
        # the right collection for every hit, not just the top result.
        if structured:
            return {
                "ids": [r.id for r in page],
                "tumblers": [r.metadata.get("tumbler", "") for r in page],
                "distances": [float(r.distance) for r in page],
                "collections": list({r.collection for r in page}),
                "chunk_collections": [r.collection for r in page],
                "chunk_text_hash": [
                    r.metadata.get("chunk_text_hash", "") for r in page
                ],
            }

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
) -> "str | dict":
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
            all_names = _get_collection_names()

            if corpus == "all":
                seen_prefixes: list[str] = []
                for n in all_names:
                    prefix = n.split("__", 1)[0]
                    if prefix and prefix not in seen_prefixes:
                        seen_prefixes.append(prefix)
                corpus = ",".join(seen_prefixes) if seen_prefixes else "knowledge,code,docs,rdr"

            target: list[str] = []
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
        with _t2_ctx() as _t2_db:
            results = search_cross_corpus(
                question, target, n_results=fetch_n, t3=t3, where=where_dict,
                catalog=_get_catalog(),
                link_boost=True,
                taxonomy=_t2_db.taxonomy,
                telemetry=_t2_db.telemetry,
            )
        results.sort(key=lambda r: r.distance)
        if not results:
            if structured:
                return {
                    "ids": [], "tumblers": [], "distances": [],
                    "collections": [], "chunk_collections": [],
                    "chunk_text_hash": [],
                }
            return "No documents found."

        if structured:
            page = results[:limit]
            return {
                "ids": [r.id for r in page],
                "tumblers": [r.metadata.get("tumbler", "") for r in page],
                "distances": [float(r.distance) for r in page],
                "collections": list({r.collection for r in page}),
                # Review #7: per-result aligned list for consumers
                # that need per-chunk origin (e.g. nx_answer envelope).
                "chunk_collections": [r.collection for r in page],
                # RDR-086 Phase 3.2: chunk_text_hash forwarded for chash
                # citation authoring at the document layer.
                "chunk_text_hash": [
                    r.metadata.get("chunk_text_hash", "") for r in page
                ],
            }

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
        # Register in catalog (same hook the CLI uses). Non-fatal — T3 row is
        # source of truth — but log so #253 orphan shape is observable.
        try:
            from nexus.commands.store import _catalog_store_hook
            _catalog_store_hook(title=title, doc_id=doc_id, collection_name=col_name)
        except Exception:
            import structlog
            structlog.get_logger().warning(
                "catalog_store_hook_failed",
                doc_id=doc_id,
                collection=col_name,
                exc_info=True,
            )
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
        return f"Stored: {doc_id} -> {col_name}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def store_get(doc_id: str, collection: str = "knowledge") -> str:
    """Retrieve the full content and metadata of a T3 knowledge entry by document ID or title.

    Use after store_list or search to read the complete document.

    Args:
        doc_id: Exact 16-char content-hash document ID (from store_list / store_put / search),
                OR an exact title (looked up via metadata).
        collection: Collection name or prefix (default: knowledge)
    """
    try:
        if not doc_id:
            return "Error: doc_id is required"
        col_name = t3_collection_name(collection)
        t3 = _get_t3()
        entry = t3.get_by_id(col_name, doc_id)
        if entry is None:
            # Title fallback: 16 lowercase hex chars looks like a hash;
            # anything else, try treating it as an exact title (matches what
            # store_list / search display, since hashes aren't surfaced there).
            looks_like_hash = len(doc_id) == 16 and all(c in "0123456789abcdef" for c in doc_id)
            if not looks_like_hash:
                ids = t3.find_ids_by_title(col_name, doc_id)
                if len(ids) == 1:
                    entry = t3.get_by_id(col_name, ids[0])
                elif len(ids) > 1:
                    return (
                        f"Multiple documents with title {doc_id!r} in {col_name}: "
                        + ", ".join(ids[:5]) + (" …" if len(ids) > 5 else "")
                        + " — pass a 16-char content-hash to disambiguate."
                    )
        if entry is None:
            return f"Not found: {doc_id!r} in {col_name} (pass a 16-char content-hash from store_list/store_put/search, or an exact title)"
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
def store_get_many(
    ids: str | list,
    collections: str | list = "knowledge",
    *,
    max_chars_per_doc: int = 4000,
    structured: bool = False,
) -> str | dict:
    """Batch-hydrate document content by ID. RDR-079 hydration primitive.

    Args:
        ids: Document IDs to fetch. Accepts a comma-separated string or list.
        collections: Target collection name(s). Accepts a single name,
            comma-separated, or a list aligned 1:1 with ``ids``.
        max_chars_per_doc: Per-document truncation cap (default 4 KB).
        structured: Return ``{contents, missing}`` dict when True;
            human-readable string when False.
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
            return {"contents": [], "missing": [], "error": f"store_get_many failed: {e}"}
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
    """Document-level view: deduplicate chunks by content_hash.

    Per-doc chunk count is derived from the dedup pass — entries written by
    ``store_put`` don't set a ``chunk_count`` metadata field (only the PDF
    indexer does), so reading it from metadata produced ``?`` for everything.
    The page-count column is omitted entirely when no document carries it,
    rather than showing ``?p`` for non-PDF entries.
    """
    seen: dict[str, dict] = {}
    chunks_by_hash: dict[str, int] = {}
    offset = 0
    while offset < total:
        entries = t3.list_store(col_name, limit=300, offset=offset)
        if not entries:
            break
        for e in entries:
            h = e.get("content_hash", e.get("id", ""))
            if h not in seen:
                seen[h] = e
            chunks_by_hash[h] = chunks_by_hash.get(h, 0) + 1
        offset += 300

    if not seen:
        return f"No documents in {col_name}."

    docs = sorted(seen.items(), key=lambda kv: kv[1].get("source_title") or kv[1].get("title") or "")
    show_pages = any(d.get("page_count") for _, d in docs)
    lines = [f"{col_name}  ({len(docs)} documents, {total} chunks)"]
    for i, (h, d) in enumerate(docs, 1):
        # 16-char content-hash prefix surfaces the doc_id store_get expects —
        # without this column the natural list → get flow had no path from
        # title to hash.
        doc_id = (d.get("id") or h)[:16]
        title = (d.get("source_title") or d.get("title") or "untitled")[:50]
        chunks = chunks_by_hash.get(h, "?")
        method = d.get("extraction_method", "")
        indexed = (d.get("indexed_at") or "")[:10]
        if show_pages:
            pages = d.get("page_count", "?")
            lines.append(f"  {i:3d}. {doc_id}  {title:<50}  {chunks:>4} chunks  {pages:>3}p  {method:<8}  {indexed}")
        else:
            lines.append(f"  {i:3d}. {doc_id}  {title:<50}  {chunks:>4} chunks  {method:<8}  {indexed}")
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
                lines.append(f"{prefix}[{r['id'][:8]}] {snippet}")
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
                lines.append(f"{prefix}[{e['id'][:8]}] {snippet}{tags_str}")
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


@mcp.tool()
def plan_save(
    query: str,
    plan_json: str,
    project: str = "",
    outcome: str = "success",
    tags: str = "",
    ttl: int | None = None,
    scope_tags: str = "",
) -> str:
    """Save a query execution plan to the T2 plan library.

    The plan_json should be a JSON string with the execution plan structure.
    Minimal schema: {"steps": [...], "tools_used": [...], "outcome_notes": "..."}

    Args:
        query: The original natural-language question
        plan_json: JSON string of the execution plan (see schema above)
        project: Project namespace for scoping (e.g. "nexus")
        outcome: Plan outcome, "success" or "partial"
        tags: Comma-separated tags (e.g. operation types used)
        ttl: Time-to-live in days. None means permanent (no expiry).
        scope_tags: RDR-091 Phase 2a comma-separated scope-tag string
            (e.g. ``"rdr__arcaneum,code__nexus"``). When empty, inferred
            from plan_json retrieval steps. Normalized at save time:
            trailing 8-char hex suffix and ``*`` globs are stripped.
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
                scope_tags=scope_tags or None,
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
            scope_display = r.get("scope_tags") or "(agnostic)"
            lines.append(
                f"[{r['id']}] {r['query'][:60]}\n"
                f"  outcome={r['outcome']}  tags={r['tags']}\n"
                f"  scope={scope_display}\n"
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


# ── Operator tools ───────────────────────────────────────────────────────────


@mcp.tool()
async def operator_extract(inputs: str, fields: str, timeout: float = 300.0) -> dict:
    """Extract structured fields from each input item using claude -p.

    Args:
        inputs: Items to extract from (plain text or JSON array string).
        fields: Comma-separated field names to extract.
        timeout: Seconds before the subprocess is killed. Default 300s (5 min) — the claude -p substrate handles multi-step analytical workloads; 120s was hitting false timeouts on real input.
    """
    from nexus.operators.dispatch import claude_dispatch

    prompt = (
        f"Extract the following fields from each item: {fields}\n\n"
        f"Items:\n{inputs}"
    )
    schema = {
        "type": "object",
        "required": ["extractions"],
        "properties": {
            "extractions": {
                "type": "array",
                "items": {"type": "object"},
            }
        },
    }
    return await claude_dispatch(prompt, schema, timeout=timeout)


@mcp.tool()
async def operator_rank(items: str, criterion: str, timeout: float = 300.0) -> dict:
    """Rank items by a criterion using claude -p.

    Args:
        items: Items to rank (plain text or JSON array string).
        criterion: Natural-language ranking criterion.
        timeout: Seconds before the subprocess is killed. Default 300s (5 min) — the claude -p substrate handles multi-step analytical workloads; 120s was hitting false timeouts on real input.
    """
    from nexus.operators.dispatch import claude_dispatch

    prompt = (
        f"Rank the following items by {criterion}.\n"
        f"Return them in ranked order, best first.\n\n"
        f"Items:\n{items}"
    )
    schema = {
        "type": "object",
        "required": ["ranked"],
        "properties": {
            "ranked": {"type": "array", "items": {"type": "string"}},
        },
    }
    return await claude_dispatch(prompt, schema, timeout=timeout)


@mcp.tool()
async def operator_compare(
    items: str = "",
    focus: str = "",
    timeout: float = 300.0,
    *,
    items_a: str = "",
    items_b: str = "",
    label_a: str = "A",
    label_b: str = "B",
) -> dict:
    """Compare items and return a structured comparison using claude -p.

    Two modes:

    * **One-sided** (original): pass *items* only. The comparison runs
      across entries within a single set. Keyword-only ``items_a`` /
      ``items_b`` may be omitted or empty.
    * **Two-sided** (nexus-km5i): pass *items_a* and *items_b* together
      for a cross-set compare. The prompt becomes "Compare set {label_a}
      vs set {label_b}" and asks for shared axes, divergent decisions,
      and philosophy differences. Useful for cross-corpus DAGs where a
      plan needs to align extractions from two different collections
      under one synthesis. ``focus`` scopes both modes.

    List / dict values in ``items`` / ``items_a`` / ``items_b`` are
    JSON-serialized before prompt interpolation so the LLM sees clean
    JSON instead of Python ``repr`` output.

    Args:
        items: Items to compare (plain text or JSON array string). Used
            in one-sided mode; ignored when both ``items_a`` and
            ``items_b`` are provided.
        focus: Optional aspect to focus the comparison on.
        timeout: Seconds before the subprocess is killed. Default 300s
            (5 min). The claude -p substrate handles multi-step
            analytical workloads; 120s hit false timeouts on real input.
        items_a: Side A items for two-sided compare.
        items_b: Side B items for two-sided compare.
        label_a: Human-readable label for side A (default "A").
        label_b: Human-readable label for side B (default "B").
    """
    import json as _json

    from nexus.operators.dispatch import claude_dispatch

    def _fmt(v) -> str:
        if isinstance(v, (list, dict)):
            return _json.dumps(v, indent=2, default=str)
        return v if isinstance(v, str) else str(v)

    focus_clause = f" Focus on: {focus}." if focus else ""
    if items_a and items_b:
        a_text = _fmt(items_a)
        b_text = _fmt(items_b)
        prompt = (
            f"Compare two sets of items across corpora.{focus_clause}\n\n"
            f"Set {label_a}:\n{a_text}\n\n"
            f"Set {label_b}:\n{b_text}\n\n"
            "Name:\n"
            f"  * **Shared axes**: concerns both {label_a} and {label_b} "
            "address with comparable intent (even if mechanism differs).\n"
            f"  * **Divergent decisions**: places where {label_a} and {label_b} "
            "take different approaches on the same question; attribute each "
            "choice to its side.\n"
            f"  * **Side-only axes**: concerns that appear in {label_a} or "
            f"{label_b} but not both.\n"
            "  * **Philosophy difference**: one or two sentences on the "
            "underlying stance difference, if one emerges from the evidence."
        )
    else:
        items_text = _fmt(items)
        prompt = (
            f"Compare the following items.{focus_clause}\n\n"
            f"Items:\n{items_text}"
        )
    schema = {
        "type": "object",
        "required": ["comparison"],
        "properties": {
            "comparison": {"type": "string"},
        },
    }
    return await claude_dispatch(prompt, schema, timeout=timeout)


@mcp.tool()
async def operator_summarize(
    content: str,
    cited: bool = False,
    timeout: float = 300.0,
) -> dict:
    """Summarize content using claude -p, optionally with citations.

    Args:
        content: Text to summarize.
        cited: If True, include a citations list in the output.
        timeout: Seconds before the subprocess is killed. Default 300s (5 min) — the claude -p substrate handles multi-step analytical workloads; 120s was hitting false timeouts on real input.
    """
    from nexus.operators.dispatch import claude_dispatch

    cite_clause = " Include citations as a list of source references." if cited else ""
    prompt = f"Summarize the following content concisely.{cite_clause}\n\n{content}"
    schema: dict = {
        "type": "object",
        "required": ["summary"],
        "properties": {
            "summary": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }
    return await claude_dispatch(prompt, schema, timeout=timeout)


@mcp.tool()
async def operator_generate(
    template: str,
    context: str,
    cited: bool = False,
    timeout: float = 300.0,
) -> dict:
    """Generate output from a template and context using claude -p.

    Args:
        template: Named template or description of desired output form.
        context: Source material or context to generate from.
        cited: If True, include a citations list in the output.
        timeout: Seconds before the subprocess is killed. Default 300s (5 min) — the claude -p substrate handles multi-step analytical workloads; 120s was hitting false timeouts on real input.
    """
    from nexus.operators.dispatch import claude_dispatch

    cite_clause = " Include citations as a list of source references." if cited else ""
    prompt = (
        f"Generate a {template}.{cite_clause}\n\n"
        f"Context:\n{context}"
    )
    schema: dict = {
        "type": "object",
        "required": ["output"],
        "properties": {
            "output": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }
    return await claude_dispatch(prompt, schema, timeout=timeout)


#: Shared evidence-item schema for ``operator_check`` (RDR-088 Phase 2).
#: Each entry is a citation-like record grounding the verdict across a
#: multi-item consistency probe. ``role`` is enum-restricted so downstream
#: plan steps can branch on the trichotomy without parsing free text.
_CHECK_EVIDENCE_ITEM_SCHEMA: dict = {
    "type": "object",
    "required": ["item_id", "quote", "role"],
    "properties": {
        "item_id": {"type": "string"},
        "quote": {"type": "string"},
        "role": {
            "type": "string",
            "enum": ["supports", "contradicts", "neutral"],
        },
    },
}


@mcp.tool()
async def operator_filter(
    items: str,
    criterion: str,
    timeout: float = 300.0,
) -> dict:
    """Filter items by a criterion using claude -p, returning a subset with rationale.

    RDR-088 Phase 1. Paper §D.4 Filter operator: given a prior-step's
    output list and a natural-language criterion, return the items that
    satisfy the criterion plus a per-item reason for the keep / reject
    decision. Composable with ``operator_extract``, ``operator_rank``,
    and retrieval tools via ``plan_run``. Distinct from ChromaDB's
    metadata ``where=`` filter which operates at retrieval time over
    structured fields; ``operator_filter`` operates over arbitrary
    prior-step results with natural-language predicates.

    Args:
        items: Items to filter (plain text or JSON array string). Each
            element should carry an ``id`` field when rationale round-
            tripping matters; downstream plan steps key on ``id``.
        criterion: Natural-language predicate describing the keep
            condition (e.g. "peer-reviewed only", "published after 2023").
        timeout: Seconds before the subprocess is killed. Default 300s
            (5 min) — the claude -p substrate handles multi-step
            analytical workloads; 120s was hitting false timeouts on
            real input.
    """
    from nexus.operators.dispatch import claude_dispatch

    prompt = (
        f"Filter the following items by this criterion: {criterion}\n"
        f"Return only the items that satisfy the criterion in the 'items' "
        f"array. Populate 'rationale' with one entry per input item, "
        f"keyed by the item's id, giving the reason each item was kept "
        f"or rejected. The output 'items' array must be a subset of the "
        f"input; never add synthetic items.\n\n"
        f"Items:\n{items}"
    )
    schema: dict = {
        "type": "object",
        "required": ["items", "rationale"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object"},
            },
            "rationale": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "reason"],
                    "properties": {
                        "id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    }
    return await claude_dispatch(prompt, schema, timeout=timeout)


@mcp.tool()
async def operator_check(
    items: str,
    check_instruction: str,
    timeout: float = 300.0,
) -> dict:
    """Check a claim's consistency across peer items using claude -p.

    RDR-088 Phase 2. Paper §D.2 Check operator: validate a claim across
    N peer items (papers, documents, extracted records) and return a
    structured boolean plus grounding evidence. Unlike ``operator_compare``
    which returns free-text, ``operator_check`` returns a composable
    ``{ok: bool, evidence: list[{item_id, quote, role}]}`` payload so
    plan steps can branch deterministically.

    Evidence role is one of ``supports``, ``contradicts``, ``neutral``.
    Populate at least one entry per item unless ``ok=True`` trivially
    (every item agrees with no nuance to report).

    Args:
        items: Items to check for consistency (plain text or JSON array
            string). Each entry should carry an ``id`` field; evidence
            entries key ``item_id`` against these ids.
        check_instruction: Natural-language claim or consistency
            question to evaluate across the items (e.g. "do all papers
            agree on the baseline numbers?").
        timeout: Seconds before the subprocess is killed. Default 300s.
    """
    from nexus.operators.dispatch import claude_dispatch

    prompt = (
        f"Check whether the following items are consistent with this "
        f"claim or question: {check_instruction}\n"
        f"Set ok=true when every item supports the claim, false when at "
        f"least one item contradicts it. Populate 'evidence' with a "
        f"record per item containing a short grounding 'quote' and a "
        f"'role' of 'supports', 'contradicts', or 'neutral'. Keep quotes "
        f"short enough to be verifiable against the source item.\n\n"
        f"Items:\n{items}"
    )
    schema: dict = {
        "type": "object",
        "required": ["ok", "evidence"],
        "properties": {
            "ok": {"type": "boolean"},
            "evidence": {
                "type": "array",
                "items": _CHECK_EVIDENCE_ITEM_SCHEMA,
            },
        },
    }
    return await claude_dispatch(prompt, schema, timeout=timeout)


@mcp.tool()
async def operator_verify(
    claim: str,
    evidence: str,
    timeout: float = 300.0,
) -> dict:
    """Verify a single claim against a single evidence source using claude -p.

    RDR-088 Phase 2. Paper §D.2 Verify operator: targeted single-claim
    variant of ``operator_check``. Returns ``{verified: bool, reason: str,
    citations: list[str]}`` where citations are span anchors or locators
    pulled from the evidence text that ground the verdict.

    Distinct from ``operator_check`` by cardinality: verify is 1-claim to
    1-evidence; check is 1-claim to N-items.

    Args:
        claim: A single assertion to verify (e.g. "the paper reports 2048
            GPU-hours for training").
        evidence: The source material to verify the claim against.
            Typically a section text, extracted passage, or document
            body. Not a collection of items.
        timeout: Seconds before the subprocess is killed. Default 300s.
    """
    from nexus.operators.dispatch import claude_dispatch

    prompt = (
        f"Verify whether the following claim is grounded in the evidence "
        f"provided.\n\n"
        f"Claim: {claim}\n\n"
        f"Evidence:\n{evidence}\n\n"
        f"Set verified=true only when the claim is directly supported by "
        f"the evidence. Provide a concise 'reason' explaining the "
        f"verdict. Populate 'citations' with locators (section, page, "
        f"table, or quoted span snippets) that pinpoint the supporting "
        f"or contradicting passages."
    )
    schema: dict = {
        "type": "object",
        "required": ["verified", "reason", "citations"],
        "properties": {
            "verified": {"type": "boolean"},
            "reason": {"type": "string"},
            "citations": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }
    return await claude_dispatch(prompt, schema, timeout=timeout)


# ── traverse (RDR-078 P3) ─────────────────────────────────────────────────────

#: Depth cap for traverse steps (SC-4).
_TRAVERSE_MAX_DEPTH: int = 3


@mcp.tool()
def traverse(
    seeds: list[str] | str,
    link_types: list[str] | None = None,
    purpose: str = "",
    depth: int = 1,
    direction: str = "both",
) -> dict:
    """Walk the catalog link graph from seed tumblers. RDR-078 P3 (SC-4/SC-5).

    Accepts either explicit ``link_types`` **or** a ``purpose`` name — never
    both (SC-16). Returns the standard retrieval step-output contract so
    downstream plan steps can reference ``$stepN.tumblers``,
    ``$stepN.collections``, or ``$stepN.ids``.

    Args:
        seeds: One or more tumbler strings (e.g. ``["1.1", "1.2"]``).
               Also accepts a single string for convenience.
        link_types: Explicit catalog link types to follow
                    (``"implements"``, ``"cites"``, …).
                    Mutually exclusive with ``purpose``.
        purpose: Named alias for a link-type set (e.g.
                 ``"find-implementations"``).  Resolved via
                 ``nexus.plans.purposes.resolve_purpose``.
                 Mutually exclusive with ``link_types``.
        depth: BFS depth. Capped at 3 (SC-4).
        direction: ``"out"`` | ``"in"`` | ``"both"`` (default).

    Returns:
        ``{"tumblers": [...], "ids": [], "collections": [...]}``
    """
    from nexus.plans.purposes import resolve_purpose

    # SC-16: mutual exclusion.
    if link_types and purpose:
        return {"error": "traverse: specify link_types OR purpose, not both"}

    # Normalise seeds to a list.
    if isinstance(seeds, str):
        seeds = [seeds] if seeds else []

    if not seeds:
        return {"tumblers": [], "ids": [], "collections": []}

    # Resolve link types.
    if purpose:
        resolved = resolve_purpose(purpose)
        if not resolved:
            return {
                "tumblers": [], "ids": [], "collections": [],
                "warning": f"traverse: unknown purpose {purpose!r}",
            }
        effective_types: list[str] = resolved
    elif link_types:
        effective_types = list(link_types)
    else:
        effective_types = []

    depth = min(depth, _TRAVERSE_MAX_DEPTH)

    catalog = _get_catalog()
    if catalog is None:
        return {"error": "traverse: catalog not available"}

    from nexus.catalog.tumbler import Tumbler

    seed_tumblers = []
    for s in seeds:
        try:
            seed_tumblers.append(Tumbler.parse(s))
        except Exception:
            pass  # drop unparseable seeds

    if not seed_tumblers:
        return {"tumblers": [], "ids": [], "collections": []}

    kw = dict(depth=depth, direction=direction, link_types=effective_types or None)
    if len(seed_tumblers) == 1:
        result = catalog.graph(seed_tumblers[0], **kw)
    else:
        result = catalog.graph_many(seed_tumblers, **kw)

    nodes = result.get("nodes") or []
    tumblers = [str(n.tumbler) for n in nodes if hasattr(n, "tumbler")]
    collections = list({
        n.physical_collection
        for n in nodes
        if hasattr(n, "physical_collection") and n.physical_collection
    })

    # Resolve chunk IDs from T3 for nodes that have a file_path.
    chunk_ids: list[str] = []
    candidates = [
        (getattr(n, "file_path", "") or "", getattr(n, "physical_collection", "") or "")
        for n in nodes
        if (getattr(n, "file_path", "") or "") and (getattr(n, "physical_collection", "") or "")
    ]
    if candidates:
        try:
            t3 = _get_t3()
            seen_ids: set[str] = set()
            for fp, pc in candidates:
                try:
                    for cid in t3.ids_for_source(pc, fp):
                        if cid not in seen_ids:
                            seen_ids.add(cid)
                            chunk_ids.append(cid)
                except Exception:
                    pass  # degrade gracefully per node
        except Exception:
            pass  # T3 unavailable — ids stays empty

    return {"tumblers": tumblers, "ids": chunk_ids, "collections": collections}


# ── nx_answer helpers (RDR-080) ───────────────────────────────────────────────

#: Maximum inputs passed to an operator before auto-inserting a rank winnow.
_OPERATOR_MAX_INPUTS: int = 100

#: Minimum confidence for a plan_match result to count as a hit.
_PLAN_MATCH_MIN_CONFIDENCE: float = 0.40

#: JSON schema for the inline plan-miss planner.
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

#: Tool-signature hint text included in the inline-planner prompt so the
#: LLM generates args that match the actual MCP tool contracts.  Without
#: this, the planner typically emits ``operator_extract(corpus=..., query=...)``
#: — tokens the tool's signature doesn't accept — and the step fails with
#: ``missing required argument 'inputs'/'fields'``.
_PLANNER_TOOL_REFERENCE = """\
Use ONLY these tools (bare names; the runner maps them to MCP calls).

=== Retrieval tools ===
Each returns {"ids": [...], "tumblers": [...], "distances": [...], "collections": [...]}.
THEY DO NOT RETURN CONTENT. To get text, chain into store_get_many.

  search(query, corpus="all", limit=10, topic="", where="")
      - `query` is the search string.  `corpus` must be "all", a prefix
        (rdr/knowledge/code), or a full collection name.
      - Output: {ids, tumblers, distances, collections}

  query(question, corpus="all", limit=10, author="", content_type="",
        subtree="", follow_links="", depth=1)
      - Document-level retrieval with catalog-aware routing.
      - Output: {ids, tumblers, distances, collections}
      - Scope filter guidance (bead nexus-sgrg): prefer `corpus=<collection>`
        for project scoping. The `author` filter matches the catalog
        `author` column, which is rarely populated for RDR/docs; setting
        `author=<repo-name>` almost always returns zero rows. Only use
        `author=` when you know the catalog has it (e.g. knowledge docs
        where an explicit author tag was registered). The same caveat
        applies to `content_type=`: it matches exact values like
        `"rdr"`, `"code"`, `"prose"`, `"knowledge"`, not free-form tags.

  traverse(seeds, link_types=[...] OR purpose="<name>", depth=1, direction="both")
      - Walk catalog edges from seed tumblers.
      - `seeds` is a list of tumbler strings. Specify EITHER link_types
        (e.g. ["implements"]) OR purpose ("find-implementations",
        "decision-evolution", "reference-chain", "documentation-for") —
        never both. Depth capped at 3.
      - Output: {tumblers, ids, collections}

=== Content hydration ===
  store_get_many(ids=[...], collections="knowledge")
      - Batch hydration — turn IDs into actual text.
      - `ids` MUST come from a prior retrieval step: ids=$step1.ids
      - `collections` MUST come from a prior retrieval step:
        collections=$step1.collections
      - Output: {contents: [str, ...], missing: [str, ...]}

=== Operators (LLM-backed) ===
Each requires hydrated text as input — NOT ids/tumblers.

  extract(inputs, fields)
      - `inputs` is a JSON array of content strings. Pass $stepN.contents
        where step N was store_get_many.
      - `fields` is a comma-separated string like "topic,decision,year".
      - Output: {extractions: [dict, ...]}

  rank(items, criterion)
      - `items` is a JSON array. `criterion` is a string.
      - Output: {ranked: [...]}

  compare(items, focus="")
      - `items` is a JSON array.  `focus` is an optional axis.
      - Output: {comparison: str or {dict}}

  summarize(content, cited=false)
      - `content` is a SINGLE string (not a list).  Pass one of:
          * $stepN.contents when step N is store_get_many (runner will
            auto-join the list into a single string).
          * A literal string.
      - Output: {summary: str}

  generate(template, context, cited=false)
      - `template` is a natural-language instruction; `context` is a
        string (similar rules as summarize.content).
      - Output: {text: str}

=== Correct chain patterns ===

Pattern A (search → hydrate → operate):
  step1: search(query=..., corpus="all")      → {ids, tumblers, collections}
  step2: store_get_many(ids=$step1.ids,
                        collections=$step1.collections)
                                               → {contents, missing}
  step3: summarize(content=$step2.contents)    → {summary}

Pattern B (operator auto-hydration shortcut):
  step1: search(query=..., corpus="all")
  step2: summarize(ids=$step1.ids,
                   collections=$step1.collections)
    # Runner auto-calls store_get_many for you when an operator step
    # receives `ids` + `collections`.  Skips the explicit hydration step.

=== Step-output reference plumbing ===
  $stepN.<field> — e.g. $step1.ids, $step2.contents.  Never $stepN alone.
  The <field> must be one the tool actually returns (see output contracts
  above).  A mismatch fails with PlanRunStepRefError.

=== Forbidden tools ===
  Do NOT emit mcp__plugin_nx_nexus-catalog__* names — use traverse.
  Do NOT emit Read, Grep, Bash, Write, or web_* — they are not part of
  the plan dispatcher.
"""


def _nx_answer_match_is_hit(
    confidence: float | None,
    threshold: float = _PLAN_MATCH_MIN_CONFIDENCE,
) -> bool:
    """Return True when a plan_match confidence qualifies as a hit.

    ``confidence is None`` (FTS5 sentinel, RF-11) is always a hit.
    Numeric confidence must be >= *threshold*. RDR-092 Phase 2 Option A
    makes the threshold caller-overridable: the default tracks the
    RDR-079 P5 calibration (0.40), and verb skills that have validated
    a stricter floor (0.50 per R9) can pin it per-call.
    """
    if confidence is None:
        return True
    return confidence >= threshold


#: Common English stop-words stripped when synthesizing a grown plan's
#: ``name`` from the question. Kept narrow on purpose; aggressive
#: filtering drops the content words R10 needs for match-text signal.
_GROWN_PLAN_NAME_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "for", "in", "on", "at", "by", "with", "from", "about",
    "and", "or", "but", "so", "as",
    "how", "what", "why", "when", "where", "who", "which",
    "do", "does", "did", "can", "could", "should", "would", "will",
    "this", "that", "these", "those",
    "i", "we", "you", "they", "it", "he", "she",
})


def _infer_grown_plan_verb(
    *,
    caller_dimensions: dict[str, Any] | None,
    plan_json: str,
) -> str:
    """Three-tier verb cascade for a grown plan. RDR-092 Phase 0b.

    Tier 1: caller-supplied ``dimensions["verb"]``.
    Tier 2: operator-shape inference from ``plan_json.steps``:
        compare step → analyze; extract+rank → analyze;
        traverse+search+summarize → research.
    Tier 3: ``"research"`` fallback.
    """
    if caller_dimensions:
        pinned = caller_dimensions.get("verb")
        if isinstance(pinned, str) and pinned.strip():
            return pinned.strip().lower()
    try:
        plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    except (json.JSONDecodeError, TypeError):
        return "research"
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        return "research"
    tools = {
        step.get("tool", "").strip().lower()
        for step in steps if isinstance(step, dict)
    }
    tools.discard("")
    if "compare" in tools:
        return "analyze"
    if {"extract", "rank"}.issubset(tools):
        return "analyze"
    if {"traverse", "search", "summarize"}.issubset(tools):
        return "research"
    return "research"


def _infer_grown_plan_name(
    question: str, *, max_words: int = 5,
) -> str:
    """Kebab-case name from first 3-5 content words of *question*.

    RDR-092 Phase 0b. Drops a narrow set of common English stop-words
    (see :data:`_GROWN_PLAN_NAME_STOP_WORDS`), lowercases the rest, and
    joins up to *max_words* tokens with ``-``. Empty / whitespace-only
    input returns ``"grown-plan"`` so a grown row always has an
    identifier.
    """
    import re

    tokens = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_]*", question.lower())
    content = [t for t in tokens if t not in _GROWN_PLAN_NAME_STOP_WORDS]
    take = content[:max_words] if content else tokens[:max_words]
    return "-".join(take) or "grown-plan"


def _nx_answer_classify_plan(match: Any) -> str:
    """Classify a matched plan: ``"single_query"`` | ``"retrieval_only"`` | ``"needs_operators"``."""
    from nexus.plans.runner import _OPERATOR_TOOL_MAP
    _OPERATOR_TOOLS = frozenset(_OPERATOR_TOOL_MAP.keys())
    try:
        plan = json.loads(match.plan_json)
    except (json.JSONDecodeError, TypeError):
        return "needs_operators"
    steps = plan.get("steps") or []
    if len(steps) == 1 and steps[0].get("tool") == "query":
        return "single_query"
    if any(step.get("tool", "") in _OPERATOR_TOOLS for step in steps):
        return "needs_operators"
    return "retrieval_only"


def _nx_answer_is_single_query(match: Any) -> bool:
    return _nx_answer_classify_plan(match) == "single_query"


def _nx_answer_needs_operators(match: Any) -> bool:
    return _nx_answer_classify_plan(match) == "needs_operators"


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
    """Write one row to ``nx_answer_runs``. Redacts when ``trace=False``."""
    from nexus.db.migrations import migrate_nx_answer_runs
    migrate_nx_answer_runs(conn)
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


def _nx_answer_record_outcome(plan_id: int, *, success: bool) -> None:
    """Bump ``success_count`` or ``failure_count`` for a library-matched plan.

    No-op for ``plan_id == 0`` (synthetic inline-planner Match). Swallows
    library errors — telemetry must never break the user-facing path.
    """
    if not plan_id:
        return
    try:
        with _t2_ctx() as db:
            db.plans.increment_run_outcome(plan_id, success=success)
    except Exception:
        import structlog as _slog
        _slog.get_logger().warning(
            "nx_answer_plan_outcome_increment_failed",
            plan_id=plan_id, success=success, exc_info=True,
        )


async def _nx_answer_plan_miss(
    question: str,
    *,
    scope: str = "",
    max_steps: int = 6,
) -> Any:
    """Decompose *question* into a plan via claude_dispatch, execute it,
    and return a synthetic Match for plan_run.
    """
    from nexus.operators.dispatch import claude_dispatch
    from nexus.plans.match import Match
    from nexus.mcp_infra import get_collection_names

    corpus_hint = f" Focus on the '{scope}' corpus." if scope else ""

    # Give the planner the actual collection names it can search against.
    # Without this, the LLM writes `corpus="knowledge,code,docs"` — generic
    # tokens that may not match any collection in the caller's sandbox.
    try:
        available = get_collection_names()
    except Exception:
        available = []
    corpus_names_hint = ""
    if available:
        corpus_names_hint = (
            f"\n\nAvailable collection names in this environment: "
            f"{', '.join(sorted(available)[:20])}"
            + (f" (and {len(available) - 20} more)" if len(available) > 20 else "")
            + ".  Pass collection names to `search` via `corpus=<name>` — "
            "bare prefixes like 'knowledge' or 'code' will miss if no "
            "collection actually starts with that prefix."
        )

    prompt = (
        f"Decompose this question into a retrieval-and-analysis plan "
        f"with at most {max_steps} steps:{corpus_hint}\n\n"
        f"Question: {question}\n"
        f"{corpus_names_hint}\n\n"
        f"{_PLANNER_TOOL_REFERENCE}\n"
        f"Return the plan as {{\"steps\": [...]}} where each step is "
        f"{{\"tool\": \"<bare name>\", \"args\": {{...}}}}."
    )

    # Inline planner timeout: 300s — decomposing a question into a
    # plan is heavier than a single operator call (multi-step reasoning,
    # tool-choice enumeration). 120s was hitting the timeout on
    # non-trivial questions. Callers of nx_answer see the miss path
    # as a hang when this trips.
    payload = await claude_dispatch(prompt, _PLANNER_SCHEMA, timeout=300.0)
    steps = payload.get("steps", []) if isinstance(payload, dict) else []
    if not steps:
        raise ValueError("planner returned empty plan")

    _ALLOWED_TOOLS = {
        "search", "query", "traverse", "store_get_many",
        "extract", "rank", "compare", "summarize", "generate",
    }
    # The LLM planner emits either the bare operator name ("extract") or
    # the resolved MCP tool name ("operator_extract" / full prefix form).
    # Normalize all three to the bare form the dispatcher's _OPERATOR_TOOL_MAP
    # expects as a key.
    _TOOL_ALIASES = {
        "grep": "search", "read": "search", "bash": "search",
        "find": "search", "glob": "search",
        "web_search": "search", "web_fetch": "search",
        # operator_* → bare op name (the runner dispatcher remaps back)
        "operator_extract": "extract",
        "operator_rank": "rank",
        "operator_compare": "compare",
        "operator_summarize": "summarize",
        "operator_generate": "generate",
    }
    # Catalog tools the LLM might reach for — map to the closest allowed
    # tool (traverse covers link walks; search covers catalog_search use
    # cases).  Prevents silent `planner_step_dropped` when the planner
    # hasn't fully internalised the "no catalog_* in plans" rule.
    _CATALOG_TOOL_REDIRECTS = {
        "link_query": "traverse",
        "links": "traverse",
        "catalog_search": "search",
        "catalog_show": "query",
        "catalog_list": "query",
        "catalog_resolve": "traverse",
        "catalog_stats": None,  # nothing plan-step-worthy to redirect to
    }
    _TOOL_ALIASES.update({
        k: v for k, v in _CATALOG_TOOL_REDIRECTS.items() if v is not None
    })
    import structlog as _slog
    _plog = _slog.get_logger()
    normalized = []
    dropped: list[str] = []
    for step in steps:
        raw_tool = step.get("tool", "")
        bare = raw_tool.rsplit("__", 1)[-1] if raw_tool.startswith("mcp__") else raw_tool
        bare = _TOOL_ALIASES.get(bare.lower(), bare)
        if bare not in _ALLOWED_TOOLS:
            _plog.warning("planner_step_dropped", raw_tool=raw_tool, bare=bare)
            dropped.append(raw_tool or bare or "?")
            continue
        step["tool"] = bare
        normalized.append(step)

    if not normalized:
        # Search review I-5: surface the dropped tools in the error so the
        # caller's "planner failed" message can explain why (e.g. the LLM
        # picked Bash / grep / WebFetch which aren't dispatchable).
        detail = ", ".join(sorted(set(dropped))) if dropped else "(no tools at all)"
        raise ValueError(
            f"planner returned only non-dispatchable tools: {detail}"
        )

    plan_json = json.dumps({"steps": normalized})
    return Match(
        plan_id=0,
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


# ── RDR-084 helpers ───────────────────────────────────────────────────────────


def _load_ad_hoc_ttl() -> int:
    """Return the TTL (days) applied to auto-saved ad-hoc plans.

    Reads ``.nexus.yml#plans.ad_hoc_ttl`` via :func:`nexus.config.load_config`
    with a 30-day fallback. A config load failure also falls back to 30
    — the growth feature is best-effort and must never block ``nx_answer``.
    """
    try:
        config = load_config()
    except Exception:
        return 30
    plans_section = config.get("plans") if isinstance(config, dict) else None
    if not isinstance(plans_section, dict):
        return 30
    value = plans_section.get("ad_hoc_ttl", 30)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 30


# ── RDR-080 orchestration tools ───────────────────────────────────────────────


@mcp.tool()
async def nx_answer(
    question: str,
    scope: str = "",
    context: str = "",
    max_steps: int = 6,
    budget_usd: float = 0.25,
    trace: bool = True,
    dimensions: dict[str, Any] | None = None,
    structured: bool = False,
    min_confidence: float | None = None,
) -> "str | dict":
    """Answer a knowledge question using plan-match-first retrieval. RDR-080 P1.

    Internal flow:

    1. **Plan-match gate**: call ``plan_match(intent=question, dimensions=…)``.
       On hit (confidence >= 0.40 or FTS5 sentinel), execute the matched
       plan.  On miss, dispatch an inline LLM planner via ``claude -p``
       to decompose the question and execute the resulting plan.
    2. **Single-step guard**: if the matched plan has exactly 1 ``query``
       step, reroute to ``query()`` directly.
    3. **Execute plan**: run via ``plan_run``.
    4. **Record**: write run metrics to T2 ``nx_answer_runs``.

    Args:
        question: Natural-language question to answer.
        scope: Catalog subtree or corpus filter (e.g. ``"1.2"`` or ``"knowledge"``).
        context: Supplementary caller-supplied context for the plan matcher.
        max_steps: Cap on plan DAG size (passed to inline planner on miss).
        budget_usd: Per-invocation cost cap (reserved for future enforcement).
        trace: When False, redacts question and final_text in the run log.
        dimensions: Dimensional filter for the plan-match gate.  Pass
            ``{"verb": "research"}`` (etc.) so verb skills narrow the
            match to templates of the appropriate verb.  Unset means
            the matcher considers every active plan.
        structured: RDR-086 Phase 3.3 opt-in. When True, returns an
            envelope dict ``{final_text, chunks, plan_id, step_count}``
            instead of a bare string. Each entry in ``chunks`` carries
            ``id``, ``chash``, ``collection`` (and ``distance``, ``text``
            when available) so callers can build ``chash:<hex>`` citations
            without a second fetch. The single-step guard path produces
            the same envelope shape — the guard logic itself is unchanged.
            On pure-generate plans or retrieval misses, ``chunks`` is ``[]``.
        min_confidence: Per-call plan-match floor override (RDR-092 Phase
            2 Option A). ``None`` (default) uses the global
            :data:`_PLAN_MATCH_MIN_CONFIDENCE` (0.40, per RDR-079 P5).
            Verb skills that have validated a stricter precision-first
            floor (0.50 per R9 against a 5+5 probe corpus) pin the
            tighter value per-call without moving the global knob; the
            global default waits on Phase 5's larger-corpus
            validation. Must be in ``[0.0, 1.0]`` when supplied.

    Returns:
        The final step's output — a string by default, or the envelope
        dict described above when ``structured=True``.
    """
    import time
    import structlog as _slog

    from nexus.mcp_infra import get_t1_plan_cache
    from nexus.plans.matcher import plan_match as _plan_match
    from nexus.plans.runner import plan_run as _plan_run

    _log = _slog.get_logger()
    start = time.monotonic()

    # RDR-086 Phase 3.3: envelope builder. String mode returns the text
    # directly; structured mode wraps it into the documented envelope.
    def _result(text: str, *, plan_id: int = 0, step_count: int = 0,
                chunks: "list | None" = None) -> "str | dict":
        if not structured:
            return text
        return {
            "final_text": text,
            "chunks": chunks if chunks is not None else [],
            "plan_id": plan_id,
            "step_count": step_count,
        }

    # ── Step 1: plan-match gate ──────────────────────────────────────────
    # RDR-092 Phase 2 Option A: effective floor is the caller's override
    # when supplied, otherwise the RDR-079 P5 default (0.40). Bounds-
    # check the override so an agent caller passing a degenerate value
    # fails loudly (code-review S-4) instead of silently admitting
    # every match (negative) or rejecting every cosine match (> 1.0).
    if min_confidence is not None and not (0.0 <= min_confidence <= 1.0):
        return _result(
            f"min_confidence must be in [0.0, 1.0], got {min_confidence!r}"
        )
    effective_min_confidence = (
        min_confidence if min_confidence is not None
        else _PLAN_MATCH_MIN_CONFIDENCE
    )
    try:
        with _t2_ctx() as db:
            cache = get_t1_plan_cache(populate_from=db.plans)
            matches = _plan_match(
                question,
                library=db.plans,
                cache=cache,
                dimensions=dimensions,
                scope_preference=scope,
                context={"user_context": context} if context else None,
                min_confidence=effective_min_confidence,
                n=5,
            )
    except Exception as exc:
        return _result(f"Error during plan match: {exc}")

    if not matches or not _nx_answer_match_is_hit(
        matches[0].confidence, threshold=effective_min_confidence,
    ):
        # Plan miss — inline LLM planner via claude_dispatch.
        _log.info(
            "nx_answer_plan_miss",
            question=question[:100] if trace else "[redacted]",
        )
        try:
            best = await _nx_answer_plan_miss(question, scope=scope, max_steps=max_steps)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _log.warning("nx_answer_planner_failed", error=str(exc))
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.telemetry.conn, question=question, plan_id=None,
                        matched_confidence=matches[0].confidence if matches else None,
                        step_count=0, final_text=f"Planner error: {exc}",
                        cost_usd=0.0, duration_ms=elapsed_ms, trace=trace,
                    )
            except Exception:
                pass
            # Search review I-5: propagate the planner's detail — e.g.
            # "planner returned only non-dispatchable tools: Bash, grep"
            # — so the user isn't left guessing why the inline path failed.
            reason = str(exc) or "unknown error"
            return _result(
                f"No matching plan found and inline planner failed: {reason}. "
                "Try rephrasing, or use search/query directly."
            )
    else:
        best = matches[0]

    if best.plan_id == 0:
        conf_str = "ad-hoc"
    elif best.confidence is None:
        conf_str = "fts5"
    else:
        conf_str = f"{best.confidence:.3f}"

    # nexus-use1: plan execution telemetry. Bump ``use_count`` + stamp
    # ``last_used`` before any execution path (single-step fast path OR
    # _plan_run). Skip plan_id=0 (synthetic inline-planner Match — no
    # library row to update). Downstream paths bump success/failure via
    # ``_nx_answer_record_outcome`` after their try/except completes.
    if best.plan_id:
        try:
            with _t2_ctx() as db:
                db.plans.increment_run_started(best.plan_id)
        except Exception:
            _log.warning(
                "nx_answer_plan_use_increment_failed",
                plan_id=best.plan_id, exc_info=True,
            )

    # ── Step 2: single-step guard ────────────────────────────────────────
    plan_class = _nx_answer_classify_plan(best)

    if plan_class == "single_query":
        _log.info("nx_answer_single_step_guard", plan_id=best.plan_id, confidence=conf_str)
        try:
            plan = json.loads(best.plan_json)
            step_args = plan["steps"][0].get("args", {})
            q = step_args.get("question", question)
            corpus = step_args.get("corpus", "knowledge")
            # RDR-086 review #4: exactly one ``query()`` call — the
            # previous structured path re-ran non-structured for
            # ``result_text`` (doubling the T3 round-trip) even though
            # the structured envelope already contains enough to
            # synthesize a result summary.
            if structured:
                q_struct = query(question=q, corpus=corpus, structured=True)
                chunks: list[dict] = []
                if isinstance(q_struct, dict):
                    ids = q_struct.get("ids", [])
                    colls_list = q_struct.get("chunk_collections") or (
                        q_struct.get("collections") or []
                    )
                    hashes = q_struct.get("chunk_text_hash", [])
                    dists = q_struct.get("distances", [])
                    default_coll = colls_list[0] if colls_list else ""
                    for i, cid in enumerate(ids):
                        chunks.append({
                            "id": cid,
                            "chash": hashes[i] if i < len(hashes) else "",
                            # Per-chunk alignment when chunk_collections is
                            # available (Phase 3 surface fix); otherwise
                            # fall back to the first dedup'd collection.
                            "collection": (
                                colls_list[i]
                                if i < len(colls_list)
                                else default_coll
                            ),
                            "distance": dists[i] if i < len(dists) else None,
                        })
                # Synthesize a compact human-readable summary from the
                # envelope — no second query() required.
                if chunks:
                    lines = [
                        f"Found {len(chunks)} result"
                        f"{'s' if len(chunks) != 1 else ''} for {q!r}:",
                    ]
                    for ch in chunks[:5]:
                        lines.append(
                            f"  - {ch['id']} in {ch['collection']} "
                            f"(distance={ch['distance']:.3f})"
                            if ch["distance"] is not None
                            else f"  - {ch['id']} in {ch['collection']}"
                        )
                    if len(chunks) > 5:
                        lines.append(f"  ... and {len(chunks) - 5} more")
                    result_text = "\n".join(lines)
                else:
                    result_text = "No results."
            else:
                result_text = query(question=q, corpus=corpus)
                chunks = []

            elapsed_ms = int((time.monotonic() - start) * 1000)
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.telemetry.conn, question=question, plan_id=best.plan_id,
                        matched_confidence=best.confidence, step_count=1,
                        final_text=str(result_text)[:2000], cost_usd=0.0,
                        duration_ms=elapsed_ms, trace=trace,
                    )
            except Exception:
                pass
            _nx_answer_record_outcome(best.plan_id, success=True)
            return _result(
                str(result_text),
                plan_id=best.plan_id,
                step_count=1,
                chunks=chunks,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.telemetry.conn, question=question, plan_id=best.plan_id,
                        matched_confidence=best.confidence, step_count=1,
                        final_text=f"Error: {exc}", cost_usd=0.0,
                        duration_ms=elapsed_ms, trace=trace,
                    )
            except Exception:
                pass
            _nx_answer_record_outcome(best.plan_id, success=False)
            return _result(
                f"Error in single-step query: {exc}",
                plan_id=best.plan_id,
                step_count=1,
            )

    # ── Step 3: seed link-context ────────────────────────────────────────
    try:
        scratch(
            action="put",
            content=json.dumps({"question": question, "scope": scope, "plan_id": best.plan_id}),
            tags="link-context",
        )
    except Exception:
        pass

    # ── Step 4: execute plan ─────────────────────────────────────────────
    # nexus-zs1d Phase 1: propagate caller-supplied scope as the
    # ``_nx_scope`` binding so retrieval steps in library-matched plans
    # honour the caller's corpus intent. Plans that pin their own corpus
    # still win; this only fills in the gap when a plan is agnostic.
    run_bindings: dict[str, Any] = {"intent": question}
    if scope:
        run_bindings["_nx_scope"] = scope
    try:
        result = await _plan_run(best, run_bindings)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log.error("nx_answer_plan_run_error", plan_id=best.plan_id, error=str(exc))
        try:
            with _t2_ctx() as db:
                _nx_answer_record_run(
                    db.telemetry.conn, question=question, plan_id=best.plan_id,
                    matched_confidence=best.confidence, step_count=0,
                    final_text=f"Error: {exc}", cost_usd=0.0,
                    duration_ms=elapsed_ms, trace=trace,
                )
        except Exception:
            pass
        _nx_answer_record_outcome(best.plan_id, success=False)
        return _result(
            f"Error during plan execution: {exc}",
            plan_id=best.plan_id,
        )
    _nx_answer_record_outcome(best.plan_id, success=True)

    # ── Step 5: extract final answer ─────────────────────────────────────
    elapsed_ms = int((time.monotonic() - start) * 1000)
    final_step = result.steps[-1] if result.steps else {}
    text_key = next((k for k in ("text", "summary", "answer") if k in final_step), None)
    final_text = str(final_step.get(text_key, "")) if text_key else json.dumps(final_step)

    # RDR-086 Phase 3.3: harvest chunk refs from retrieval-op steps so the
    # envelope's ``chunks`` list carries id+chash+collection for every
    # retrieved chunk, ordered by final-step relevance. Review #7: prefer
    # the per-result ``chunk_collections`` list (Phase 3 fix) so every
    # chunk is tagged with its actual origin — not the first dedup'd
    # collection.
    envelope_chunks: list[dict] = []
    if structured:
        for step_out in result.steps:
            if not isinstance(step_out, dict):
                continue
            ids = step_out.get("ids")
            if not isinstance(ids, list) or not ids:
                continue
            hashes = step_out.get("chunk_text_hash", []) or []
            per_chunk_colls = step_out.get("chunk_collections") or []
            dedup_colls = step_out.get("collections", []) or []
            dists = step_out.get("distances", []) or []
            default_coll = dedup_colls[0] if dedup_colls else ""
            for i, cid in enumerate(ids):
                if i < len(per_chunk_colls):
                    coll = per_chunk_colls[i]
                else:
                    coll = default_coll
                envelope_chunks.append({
                    "id": cid,
                    "chash": hashes[i] if i < len(hashes) else "",
                    "collection": coll,
                    "distance": dists[i] if i < len(dists) else None,
                })

    _log.info(
        "nx_answer_complete",
        plan_id=best.plan_id,
        confidence=conf_str,
        step_count=len(result.steps),
        duration_ms=elapsed_ms,
    )

    # RDR-084: Save successful ad-hoc plans so the plan library compounds
    # with usage. scope=personal keeps growth isolated to the caller (the
    # project/global scopes are reached only via /nx:plan-promote). TTL is
    # config-driven; 30-day default. Best-effort — a save failure never
    # affects the user's answer, and the T1 cache upsert is a separate
    # best-effort step inside the same guard.
    if best.plan_id == 0:
        ttl_days = _load_ad_hoc_ttl()
        if ttl_days > 0:
            try:
                from pathlib import Path as _Path

                project_name = _Path.cwd().name
                # RDR-091 critic follow-up (nexus-dfok): anchor the grown
                # plan to the caller's scope. _infer_scope_tags cannot see
                # the runtime corpus injection from ``_nx_scope`` because
                # it only appears in bindings, not plan_json. Passing
                # scope_tags=scope explicitly captures the retrieval space
                # that produced this plan.
                # RDR-092 Phase 0b: R6 three-tier verb cascade populates
                # verb / name / dimensions so the grown row participates in
                # the dimensional identity index instead of landing as a
                # NULL-dimension legacy ghost.
                from nexus.plans.schema import canonical_dimensions_json

                grown_verb = _infer_grown_plan_verb(
                    caller_dimensions=dimensions,
                    plan_json=best.plan_json,
                )
                grown_name = _infer_grown_plan_name(question)
                grown_dimensions = canonical_dimensions_json({
                    "verb": grown_verb,
                    "scope": "personal",
                    "strategy": grown_name,
                })
                with _t2_ctx() as _save_db:
                    grown_id = _save_db.plans.save_plan(
                        query=question,
                        plan_json=best.plan_json,
                        outcome="success",
                        tags="ad-hoc,grown",
                        project=project_name,
                        ttl=ttl_days,
                        scope="personal",
                        scope_tags=scope or None,
                        verb=grown_verb,
                        name=grown_name,
                        dimensions=grown_dimensions,
                    )
                    # Feed the new plan into the T1 cosine cache so the next
                    # paraphrase can match without a SessionStart re-populate.
                    try:
                        cache = get_t1_plan_cache()
                        if cache is not None:
                            row = _save_db.plans.get_plan(grown_id)
                            if row:
                                cache.upsert(row)
                    except Exception:
                        _log.debug("plan_grow_cache_upsert_failed", exc_info=True)
                _log.info(
                    "plan_grow_saved",
                    plan_id=grown_id,
                    ttl_days=ttl_days,
                    project=project_name,
                )
            except Exception as exc:
                _log.warning("plan_grow_save_failed", error=str(exc))

    # ── Step 6: record run ───────────────────────────────────────────────
    try:
        with _t2_ctx() as db:
            _nx_answer_record_run(
                db.telemetry.conn, question=question, plan_id=best.plan_id,
                matched_confidence=best.confidence, step_count=len(result.steps),
                final_text=final_text[:2000], cost_usd=0.0,
                duration_ms=elapsed_ms, trace=trace,
            )
    except Exception:
        pass

    return _result(
        final_text,
        plan_id=best.plan_id,
        step_count=len(result.steps),
        chunks=envelope_chunks if structured else None,
    )


@mcp.tool()
async def nx_tidy(
    topic: str,
    collection: str = "knowledge",
    timeout: float = 600.0,
) -> str:
    """Consolidate knowledge entries on *topic* via claude -p. RDR-080 P3.

    Replaces the ``knowledge-tidier`` agent. Spawns a ``claude -p``
    subprocess that searches T3 for entries matching *topic*, identifies
    duplicates and contradictions, and returns a consolidated summary.

    Args:
        topic: The knowledge topic to consolidate (e.g. "chromadb quotas").
        collection: T3 collection to search (default: knowledge).
        timeout: Subprocess timeout in seconds. Default 600s (10 min) —
            consolidation on a large corpus does multi-step search +
            cross-reference; 120s was hitting the timeout routinely on
            real workloads. Caller can override lower for small topics.

    Returns:
        Consolidated summary as a human-readable string.
    """
    from nexus.operators.dispatch import claude_dispatch

    schema = {
        "type": "object",
        "required": ["summary", "actions"],
        "properties": {
            "summary": {"type": "string"},
            "actions": {"type": "array", "items": {"type": "object"}},
        },
    }
    prompt = (
        "You are the `tidy` knowledge consolidation operator. You have "
        "access to nx MCP tools (search, query, store_put, store_get). "
        "Search the specified collection for entries matching the topic, "
        "identify duplicates, contradictions, and outdated entries, then "
        "produce a consolidated summary.\n\n"
        f"Consolidate knowledge entries about '{topic}' in collection "
        f"'{collection}'. Search for all related entries, identify duplicates "
        "or contradictions, and produce a consolidated summary."
    )
    payload = await claude_dispatch(prompt, schema, timeout=timeout)

    summary = payload.get("summary", "") if isinstance(payload, dict) else str(payload)
    actions = payload.get("actions", []) if isinstance(payload, dict) else []
    lines = [summary]
    if actions:
        lines.append(f"\n{len(actions)} action(s) suggested.")
    return "\n".join(lines)


@mcp.tool()
async def nx_enrich_beads(
    bead_description: str,
    context: str = "",
    timeout: float = 300.0,
) -> str:
    """Enrich a bead with execution context via claude -p. RDR-080 P3.

    Replaces the ``plan-enricher`` agent. Spawns a ``claude -p``
    subprocess that searches the codebase for relevant file paths,
    code patterns, constraints, and test commands, then returns enriched
    markdown.

    Args:
        bead_description: The bead's title and description to enrich.
        context: Optional additional context (e.g. audit findings).
        timeout: Subprocess timeout in seconds. Default 300s (5 min) —
            codebase exploration with file:line verification is
            multi-step; 120s was a frequent false-timeout on beads
            with broad scope. Requests below the 300s floor are
            clamped upward (see nexus-7sbf) to prevent agent
            overrides from re-introducing false-positive timeouts;
            a structlog warning is emitted when clamping occurs.

    Returns:
        Enriched bead markdown as a human-readable string.
    """
    from nexus.operators.dispatch import claude_dispatch

    timeout = _clamp_subagent_timeout(timeout, "nx_enrich_beads")

    schema = {
        "type": "object",
        "required": ["enriched_description"],
        "properties": {
            "enriched_description": {"type": "string"},
            "key_files": {"type": "array", "items": {"type": "string"}},
            "test_commands": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
        },
    }
    prompt = (
        "You are the `enrich` bead enrichment operator. You have access "
        "to nx MCP tools (search, query) for codebase exploration. "
        "Analyze the bead description, search the codebase for relevant "
        "files, symbols, and patterns, then produce enriched markdown with "
        "key_files, test_commands, and constraints.\n\n"
        f"Enrich this bead with execution context:\n\n{bead_description}"
    )
    if context:
        prompt += f"\n\nAdditional context:\n{context}"

    payload = await claude_dispatch(prompt, schema, timeout=timeout)
    return (
        payload.get("enriched_description", "")
        if isinstance(payload, dict) else str(payload)
    )


@mcp.tool()
async def nx_plan_audit(
    plan_json: str,
    context: str = "",
    timeout: float = 600.0,
) -> str:
    """Audit a plan for correctness and codebase alignment via claude -p. RDR-080 P3.

    Replaces the ``plan-auditor`` agent. Spawns a ``claude -p``
    subprocess that validates the plan's file paths, dependencies,
    and assumptions against the current codebase state.

    Args:
        plan_json: The plan to audit (JSON string or free-text description).
        context: Optional additional context (e.g. RDR reference).
        timeout: Subprocess timeout in seconds. Default 600s (10 min) —
            a real plan audit verifies file:line pointers, cross-
            references research findings, walks dependency graphs;
            120s was hitting the timeout on RDR-086's real plan
            (11 beads, 5 phases). Requests below the 300s floor are
            clamped upward (see nexus-7sbf) to prevent planning
            agents from re-introducing false-positive timeouts via
            low overrides; a structlog warning is emitted when
            clamping occurs.

    Returns:
        Audit verdict as a human-readable string.
    """
    from nexus.operators.dispatch import claude_dispatch

    timeout = _clamp_subagent_timeout(timeout, "nx_plan_audit")

    schema = {
        "type": "object",
        "required": ["verdict", "findings", "summary"],
        "properties": {
            "verdict": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "object"}},
            "summary": {"type": "string"},
        },
    }
    prompt = (
        "You are the `audit` plan validation operator. You have access "
        "to nx MCP tools (search, query) for codebase verification. "
        "Parse the plan, verify file paths exist, check dependency ordering, "
        "identify gaps or incorrect assumptions, then emit a structured verdict.\n\n"
        f"Audit this plan for correctness and codebase alignment:\n\n{plan_json}"
    )
    if context:
        prompt += f"\n\nContext:\n{context}"

    payload = await claude_dispatch(prompt, schema, timeout=timeout)
    if isinstance(payload, dict):
        verdict = payload.get("verdict", "unknown")
        summary = payload.get("summary", "")
        findings = payload.get("findings", [])
        lines = [f"Verdict: {verdict}", summary]
        for f in findings:
            lines.append(f"  [{f.get('severity', '?')}] {f.get('title', '')}")
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
