# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP server exposing T1/T2/T3 storage APIs as tools.

Architecture:
- T1 (scratch): lazy singleton — SessionStart hook must fire first
- T2 (memory): per-call context manager — SQLite WAL, microsecond open
- T3 (store/search): lazy singleton — 1-2s ChromaDB Cloud init, reused
- All errors return "Error: {message}" strings, never raise exceptions
- No ANSI escape codes in output
"""
from __future__ import annotations

import re
import threading
import time
import warnings

from mcp.server.fastmcp import FastMCP

from nexus.commands._helpers import default_db_path
from nexus.corpus import (
    embedding_model_for_collection,
    index_model_for_collection,
    resolve_corpus,
    t3_collection_name,
)
from nexus.db.t3 import verify_collection_deep
from nexus.ttl import parse_ttl

mcp = FastMCP("nexus")

# ── Lazy singletons ──────────────────────────────────────────────────────────

_t1_instance = None
_t1_isolated = False
_t1_lock = threading.Lock()

_t3_instance = None
_t3_lock = threading.Lock()

_collections_cache: tuple[list[str], float] = ([], 0.0)
_COLLECTIONS_CACHE_TTL = 60.0  # seconds


def _get_t1():
    """Return (T1Database, is_isolated) — lazy init on first call."""
    global _t1_instance, _t1_isolated
    if _t1_instance is None:
        with _t1_lock:
            if _t1_instance is None:  # double-checked locking
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    from nexus.db.t1 import T1Database
                    _t1_instance = T1Database()
                    _t1_isolated = any("EphemeralClient" in str(w.message) for w in caught)
    return _t1_instance, _t1_isolated


def _get_t3():
    """Return T3Database singleton — lazy init on first call."""
    global _t3_instance
    if _t3_instance is None:
        with _t3_lock:
            if _t3_instance is None:  # double-checked locking
                from nexus.db import make_t3
                _t3_instance = make_t3()
    return _t3_instance


def _get_collection_names() -> list[str]:
    """Return cached T3 collection names, refreshing every _COLLECTIONS_CACHE_TTL seconds."""
    global _collections_cache
    names, ts = _collections_cache
    now = time.monotonic()
    if now - ts > _COLLECTIONS_CACHE_TTL:
        new_names = [c["name"] for c in _get_t3().list_collections()]
        _collections_cache = (new_names, now)  # atomic single-assignment
        return new_names
    return names


def _t2_ctx():
    """Return a T2Database context manager — fresh per call."""
    from nexus.db.t2 import T2Database
    return T2Database(default_db_path())


# ── Where-filter parser (shared with CLI) ────────────────────────────────────

_NUMERIC_FIELDS = frozenset({
    "bib_year", "bib_citation_count", "page_number", "page_count",
    "chunk_index", "chunk_count", "chunk_start_char", "chunk_end_char",
})
_WHERE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(>=|<=|!=|>|<|=)(.*)$")
_OP_MAP: dict[str, str | None] = {
    ">=": "$gte", "<=": "$lte", "!=": "$ne",
    ">": "$gt", "<": "$lt", "=": None,
}


def _parse_where_str(where_str: str) -> dict | None:
    """Parse comma-separated KEY{op}VALUE pairs into a ChromaDB where dict.

    Returns None when where_str is empty.
    """
    if not where_str.strip():
        return None
    parts: list[dict] = []
    for pair in where_str.split(","):
        pair = pair.strip()
        if not pair:
            continue
        m = _WHERE_RE.match(pair)
        if not m:
            raise ValueError(f"Invalid where format: {pair!r}. Use KEY=VALUE or KEY>=VALUE")
        key, op_str, raw_value = m.group(1), m.group(2), m.group(3)
        if not raw_value:
            continue
        # Auto-coerce numeric fields
        value: str | int | float = raw_value
        if key in _NUMERIC_FIELDS:
            try:
                value = int(raw_value)
            except ValueError:
                try:
                    value = float(raw_value)
                except ValueError:
                    pass
        chroma_op = _OP_MAP[op_str]
        if chroma_op is None:
            parts.append({key: value})
        else:
            parts.append({key: {chroma_op: value}})
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    if all(not isinstance(v, dict) for p in parts for v in p.values()):
        merged: dict = {}
        for p in parts:
            merged.update(p)
        return merged
    return {"$and": parts}


# ── Test injection ────────────────────────────────────────────────────────────

_catalog_instance = None
_catalog_lock = threading.Lock()
_catalog_mtime: float = 0.0


def _max_jsonl_mtime(cat) -> float:
    """Return max mtime across all three JSONL files."""
    mtime = 0.0
    for path in cat.jsonl_paths():
        try:
            mtime = max(mtime, path.stat().st_mtime) if path.exists() else mtime
        except OSError:
            pass
    return mtime


def _get_catalog():
    """Return Catalog singleton or None if not initialized.

    Checks JSONL mtime on each access — if files changed externally
    (e.g., git pull from another process), triggers a rebuild.
    """
    global _catalog_instance, _catalog_mtime
    if _catalog_instance is None:
        with _catalog_lock:
            if _catalog_instance is None:
                from nexus.catalog import Catalog
                from nexus.config import catalog_path

                path = catalog_path()
                if Catalog.is_initialized(path):
                    _catalog_instance = Catalog(path, path / ".catalog.db")
                    _catalog_mtime = _max_jsonl_mtime(_catalog_instance)
    elif _catalog_instance is not None:
        # Check for external JSONL changes (git pull, another process)
        try:
            current_mtime = _max_jsonl_mtime(_catalog_instance)
            if current_mtime > _catalog_mtime:
                with _catalog_lock:
                    # Double-check under lock to avoid redundant rebuilds
                    if current_mtime > _catalog_mtime:
                        _catalog_mtime = current_mtime
                        _catalog_instance._ensure_consistent()
        except OSError:
            pass  # stat failure is non-fatal (file not found, permission denied)
    return _catalog_instance


def _require_catalog():
    """Return (catalog, None) or (None, error_message)."""
    cat = _get_catalog()
    if cat is None:
        return None, "Catalog not initialized — run 'nx catalog setup' to create and populate it"
    return cat, None


def _resolve_tumbler_mcp(
    cat: "Catalog", value: str
) -> "tuple[Tumbler | None, str | None]":
    """Resolve tumbler string OR title/filename. Returns (tumbler, None) or (None, error)."""
    from nexus.catalog.tumbler import Tumbler
    try:
        t = Tumbler.parse(value)
        if cat.resolve(t) is not None:
            return t, None
        # Valid tumbler format but document deleted/missing — don't fall through to FTS
        return None, f"Not found: {value!r}"
    except ValueError:
        pass
    results = cat.find(value)
    if results:
        exact = [r for r in results if r.title == value]
        if exact:
            return exact[0].tumbler, None
        if len(results) == 1:
            return results[0].tumbler, None
        return None, f"Ambiguous: {len(results)} documents match {value!r} — use tumbler"
    return None, f"Not found: {value!r}"


def _reset_singletons():
    """Reset lazy singletons (for tests only)."""
    global _t1_instance, _t1_isolated, _t3_instance, _collections_cache, _catalog_instance, _catalog_mtime
    _t1_instance = None
    _t1_isolated = False
    _t3_instance = None
    _collections_cache = ([], 0.0)
    _catalog_instance = None
    _catalog_mtime = 0.0


def _inject_t1(t1, *, isolated: bool = False):
    """Inject a T1Database for testing."""
    global _t1_instance, _t1_isolated
    _t1_instance = t1
    _t1_isolated = isolated


def _inject_t3(t3):
    """Inject a T3Database for testing."""
    global _t3_instance
    _t3_instance = t3


def _inject_catalog(cat):
    """Inject a Catalog for testing."""
    global _catalog_instance
    _catalog_instance = cat


# ── Tools ─────────────────────────────────────────────────────────────────────

_DEFAULT_PAGE_SIZE = 10


@mcp.tool()
def search(
    query: str,
    corpus: str = "knowledge,code,docs",
    limit: int = 10,
    offset: int = 0,
    where: str = "",
) -> str:
    """Semantic search across T3 knowledge collections.

    Results are paged. Response footer shows ``offset=N`` for next page.

    Args:
        query: Search query string
        corpus: Comma-separated corpus prefixes or full collection names.
                Use "all" to search all corpora.
        limit: Page size — results per page (default 10)
        offset: Skip this many results (default 0). Use for pagination.
        where: Metadata filter in KEY=VALUE format, comma-separated.
               Operators: =, >=, <=, >, <, !=
               Numeric fields auto-coerced: bib_year, bib_citation_count, page_count.
               Example: "bib_year>=2023,tags=arch"
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
        results = search_cross_corpus(
            query, target, n_results=fetch_n, t3=t3, where=where_dict,
        )
        results.sort(key=lambda r: r.distance)
        if not results:
            return "No results."

        # Apply pagination
        total = len(results)
        page = results[offset:offset + limit]
        if not page:
            return f"No results at offset {offset} (total {total})."

        lines: list[str] = []
        for r in page:
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
        sorted_docs = sorted(docs.values(), key=lambda d: d["distance"])[:limit]

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
            return "\n".join(lines)

        elif action == "list":
            entries = t1.list_entries()
            if not entries:
                return f"{prefix}No scratch entries."
            lines = []
            for e in entries:
                snippet = e["content"][:80].replace("\n", " ")
                tags_str = f"  [{e.get('tags', '')}]" if e.get("tags") else ""
                lines.append(f"{prefix}[{e['id'][:12]}] {snippet}{tags_str}")
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


@mcp.tool()
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
        if result.probe_doc_id:
            lines.append(f"Probe doc: {result.probe_doc_id}")
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
def plan_search(query: str, project: str = "", limit: int = 5) -> str:
    """Search the T2 plan library for similar query plans.

    Args:
        query: Search query (matched against plan query text and tags)
        project: Optional project filter (e.g. "nexus")
        limit: Maximum results to return
    """
    try:
        with _t2_ctx() as db:
            results = db.search_plans(query, limit=limit, project=project)
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
        return "\n\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# ── Catalog tools ─────────────────────────────────────────────────────────────


def _entry_to_dict(entry) -> dict:
    return entry.to_dict()


def _link_to_dict(link) -> dict:
    return link.to_dict()


@mcp.tool()
def catalog_search(
    query: str = "",
    content_type: str = "",
    author: str = "",
    corpus: str = "",
    owner: str = "",
    file_path: str = "",
    limit: int = 20,
) -> list[dict]:
    """Find documents by metadata (title, author, corpus, file path).

    Returns catalog entries with tumbler, physical_collection, and metadata — NOT document
    content. Use the `search` tool for semantic content search within collections.
    Use catalog_search first to discover WHICH collections to search, then search for content.

    Filters: query (free-text), author, corpus, owner, file_path, content_type (exact match).
    At least one filter required."""
    cat, err = _require_catalog()
    if err:
        return [{"error": err}]
    try:
        from nexus.catalog.tumbler import Tumbler
        import json as _json

        # Structured filters via SQL when provided
        if owner or corpus or file_path or (author and not query):
            conditions = ["1=1"]
            params: list = []
            if owner:
                depth = len(owner.split("."))
                conditions.append("tumbler LIKE ?")
                params.append(owner + ".%")
                conditions.append("(length(tumbler) - length(replace(tumbler, '.', ''))) = ?")
                params.append(depth)
            if corpus:
                conditions.append("corpus = ?")
                params.append(corpus)
            if file_path:
                conditions.append("file_path = ?")
                params.append(file_path)
            if author:
                conditions.append("author LIKE ?")
                params.append(f"%{author}%")
            if content_type:
                conditions.append("content_type = ?")
                params.append(content_type)
            sql = (
                "SELECT tumbler, title, author, year, content_type, file_path, "
                "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
                f"FROM documents WHERE {' AND '.join(conditions)} LIMIT ?"
            )
            params.append(limit)
            rows = cat._db.execute(sql, params).fetchall()
            from nexus.catalog.catalog import CatalogEntry
            entries = [
                CatalogEntry(
                    tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                    content_type=r[4], file_path=r[5], corpus=r[6],
                    physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                    indexed_at=r[10], meta=_json.loads(r[11]) if r[11] else {},
                )
                for r in rows
            ]
            return [_entry_to_dict(e) for e in entries]

        # FTS5 free-text search (append author to query if both provided)
        fts_query = query
        if author and query:
            fts_query = f"{query} {author}"
        if not fts_query.strip():
            return [{"error": "query or at least one filter required"}]
        results = cat.find(fts_query, content_type=content_type or None)[:limit]
        return [_entry_to_dict(e) for e in results]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def catalog_show(
    tumbler: str = "",
    title: str = "",
) -> dict:
    """Show a document's full metadata, physical collection, and all links to/from it.

    Pass tumbler (e.g. "1.2.5") or title. Returns all metadata plus links_from and links_to
    arrays — useful for discovering a document's connections without a separate catalog_links call.
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        from nexus.catalog.tumbler import Tumbler

        entry = None
        if tumbler:
            entry = cat.resolve(Tumbler.parse(tumbler))
        elif title:
            results = cat.find(title)
            entry = results[0] if results else None

        if entry is None:
            return {"error": f"Not found: {tumbler or title}"}

        d = _entry_to_dict(entry)
        d["links_from"] = [_link_to_dict(l) for l in cat.links_from(entry.tumbler)]
        d["links_to"] = [_link_to_dict(l) for l in cat.links_to(entry.tumbler)]
        return d
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def catalog_list(
    owner: str = "",
    content_type: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List catalog entries with optional filters."""
    cat, err = _require_catalog()
    if err:
        return [{"error": err}]
    try:
        from nexus.catalog.tumbler import Tumbler

        if owner:
            entries = cat.by_owner(Tumbler.parse(owner))
            entries = entries[offset:offset + limit]
        else:
            import json as _json

            rows = cat._db.execute(
                "SELECT tumbler, title, author, year, content_type, file_path, "
                "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
                "FROM documents LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            from nexus.catalog.catalog import CatalogEntry

            entries = [
                CatalogEntry(
                    tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                    content_type=r[4], file_path=r[5], corpus=r[6],
                    physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                    indexed_at=r[10], meta=_json.loads(r[11]) if r[11] else {},
                )
                for r in rows
            ]
        if content_type:
            entries = [e for e in entries if e.content_type == content_type]
        return [_entry_to_dict(e) for e in entries[:limit]]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def catalog_register(
    title: str,
    owner: str,
    content_type: str = "paper",
    author: str = "",
    year: int = 0,
    file_path: str = "",
    corpus: str = "",
    physical_collection: str = "",
    meta: str = "",
) -> dict:
    """Register a document. Assigns tumbler. Ghost elements: physical_collection can be empty."""
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        import json as _json

        from nexus.catalog.tumbler import Tumbler

        tumbler = cat.register(
            Tumbler.parse(owner), title,
            content_type=content_type, file_path=file_path,
            corpus=corpus, author=author, year=year,
            physical_collection=physical_collection,
            meta=_json.loads(meta) if meta else None,
        )
        return {"tumbler": str(tumbler), "title": title}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def catalog_update(
    tumbler: str,
    title: str = "",
    author: str = "",
    year: int = 0,
    corpus: str = "",
    physical_collection: str = "",
    meta: str = "",
) -> dict:
    """Update a catalog entry's metadata."""
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        import json as _json

        from nexus.catalog.tumbler import Tumbler

        fields: dict = {}
        if title:
            fields["title"] = title
        if author:
            fields["author"] = author
        if year:
            fields["year"] = year
        if corpus:
            fields["corpus"] = corpus
        if physical_collection:
            fields["physical_collection"] = physical_collection
        if meta:
            fields["meta"] = _json.loads(meta)
        if not fields:
            return {"error": "No fields to update"}
        cat.update(Tumbler.parse(tumbler), **fields)
        return {"tumbler": tumbler, "updated": list(fields.keys())}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def catalog_link(
    from_tumbler: str,
    to_tumbler: str,
    link_type: str,
    created_by: str = "user",
    from_span: str = "",
    to_span: str = "",
) -> dict:
    """Create a relationship between two documents. Accepts tumblers or titles for both endpoints.

    Built-in link types: cites, implements, implements-heuristic, supersedes, relates, quotes, comments.
    Custom types are also accepted. created_by identifies who/what created this link.
    Duplicate links are merged with co_discovered_by tracking. Returns {created: true/false}.
    Raises error if either endpoint doesn't exist (pass allow_dangling via Python API to bypass).
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        ft, err = _resolve_tumbler_mcp(cat, from_tumbler)
        if err:
            return {"error": err}
        tt, err = _resolve_tumbler_mcp(cat, to_tumbler)
        if err:
            return {"error": err}
        created = cat.link(ft, tt, link_type, created_by, from_span=from_span, to_span=to_span)
        return {"from": str(ft), "to": str(tt), "type": link_type, "created": created}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def catalog_links(
    tumbler: str,
    direction: str = "both",
    link_type: str = "",
    depth: int = 1,
) -> dict:
    """Links to/from a catalog entry. Accepts tumbler or title. depth controls BFS depth (default 1 = direct neighbors).

    Returns {"nodes": [CatalogEntry dicts], "edges": [CatalogLink dicts]}.
    Note: only returns links whose endpoints are live documents (deleted nodes excluded).
    Use catalog_link_query to see all links including those to deleted documents.
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        t, err = _resolve_tumbler_mcp(cat, tumbler)
        if err:
            return {"error": err}
        result = cat.graph(t, depth=depth, direction=direction, link_type=link_type)
        return {
            "nodes": [_entry_to_dict(n) for n in result["nodes"]],
            "edges": [_link_to_dict(e) for e in result["edges"]],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def catalog_unlink(
    from_tumbler: str,
    to_tumbler: str,
    link_type: str = "",
) -> dict:
    """Remove a specific link between two documents. Accepts tumblers or titles.

    If link_type is empty, removes ALL link types between the pair. Returns {removed: count}.
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        ft, err = _resolve_tumbler_mcp(cat, from_tumbler)
        if err:
            return {"error": err}
        tt, err = _resolve_tumbler_mcp(cat, to_tumbler)
        if err:
            return {"error": err}
        removed = cat.unlink(ft, tt, link_type)
        return {"removed": removed, "from": str(ft), "to": str(tt)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def catalog_link_audit() -> dict:
    """Audit the link graph for health issues.

    Returns: total, by_type, by_creator, orphaned (+ count), duplicates (+ count),
    stale_spans (+ count, positional spans on re-indexed docs),
    stale_chash (+ count, content-hash spans that no longer resolve in T3).

    Each stale_chash entry includes a ``reason`` field: ``"missing"`` (chunk deleted),
    ``"document_deleted"``, or ``"error"`` (with ``error`` type name).
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        t3 = _get_t3()
        return cat.link_audit(t3=t3._client)
    except Exception as e:
        return {"error": str(e)}


_BULK_DELETE_CONFIRM_THRESHOLD = 10


@mcp.tool()
def catalog_link_bulk(
    from_tumbler: str = "",
    to_tumbler: str = "",
    link_type: str = "",
    created_by: str = "",
    created_at_before: str = "",
    dry_run: bool = False,
    confirm_destructive: bool = False,
) -> dict:
    """Bulk delete links by filter. DESTRUCTIVE — use dry_run=True first.

    dry_run=True returns count without deleting.
    If deletion would remove more than 10 links, confirm_destructive=True is required.
    created_at_before: ISO timestamp string, e.g. "2026-01-01T00:00:00"
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        # Always preview first
        preview = cat.bulk_unlink(
            from_t=from_tumbler, to_t=to_tumbler, link_type=link_type,
            created_by=created_by, created_at_before=created_at_before,
            dry_run=True,
        )
        if dry_run:
            return {"would_remove": preview, "dry_run": True}
        if preview > _BULK_DELETE_CONFIRM_THRESHOLD and not confirm_destructive:
            return {
                "error": f"Would remove {preview} links — set confirm_destructive=True to proceed",
                "would_remove": preview,
            }
        count = cat.bulk_unlink(
            from_t=from_tumbler, to_t=to_tumbler, link_type=link_type,
            created_by=created_by, created_at_before=created_at_before,
        )
        return {"removed": count}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def catalog_link_query(
    from_tumbler: str = "",
    to_tumbler: str = "",
    link_type: str = "",
    created_by: str = "",
    direction: str = "both",
    tumbler: str = "",
    created_at_before: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Query links by any combination of filters. For admin/audit use.

    NOT a query-planner step — use catalog_links for graph traversal.
    Use for: audit by creator, find all links of a type, count generator output.
    created_at_before: ISO timestamp — only links created before this time.
    Note: returns ALL matching links including orphans (links to deleted documents).
    Use catalog_links for live-documents-only graph traversal.
    """
    cat, err = _require_catalog()
    if err:
        return [{"error": err}]
    try:
        links = cat.link_query(
            from_t=from_tumbler, to_t=to_tumbler, link_type=link_type,
            created_by=created_by, direction=direction, tumbler=tumbler,
            created_at_before=created_at_before,
            limit=limit, offset=offset,
        )
        return [_link_to_dict(l) for l in links]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def catalog_resolve(
    tumbler: str = "",
    owner: str = "",
    corpus: str = "",
) -> list[str]:
    """Resolve to physical ChromaDB collection names.
    Returns collection names usable with the `search` tool."""
    cat, err = _require_catalog()
    if err:
        return [f"Error: {err}"]
    try:
        from nexus.catalog.tumbler import Tumbler

        collections: set[str] = set()
        if tumbler:
            entry = cat.resolve(Tumbler.parse(tumbler))
            if entry and entry.physical_collection:
                collections.add(entry.physical_collection)
        if owner:
            entries = cat.by_owner(Tumbler.parse(owner))
            for e in entries:
                if e.physical_collection:
                    collections.add(e.physical_collection)
        if corpus:
            rows = cat._db.execute(
                "SELECT DISTINCT physical_collection FROM documents WHERE corpus = ?",
                (corpus,),
            ).fetchall()
            for r in rows:
                if r[0]:
                    collections.add(r[0])
        return sorted(collections)
    except Exception as e:
        return [f"Error: {e}"]


@mcp.tool()
def catalog_stats() -> dict:
    """Catalog health summary: owner/document/link counts by type."""
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        db = cat._db
        return {
            "owners": db.execute("SELECT count(*) FROM owners").fetchone()[0],
            "documents": db.execute("SELECT count(*) FROM documents").fetchone()[0],
            "links": db.execute("SELECT count(*) FROM links").fetchone()[0],
            "by_type": dict(db.execute(
                "SELECT content_type, count(*) FROM documents GROUP BY content_type"
            ).fetchall()),
            "by_link_type": dict(db.execute(
                "SELECT link_type, count(*) FROM links GROUP BY link_type"
            ).fetchall()),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    """Run the MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
