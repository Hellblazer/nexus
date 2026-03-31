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
    """Return cached T3 collection names, refreshing every _COLLECTIONS_CACHE_TTL seconds.

    Uses atomic tuple assignment to avoid the two-write race where a concurrent
    reader could see the updated list but the stale timestamp (or vice versa).
    """
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


# ── Test injection ────────────────────────────────────────────────────────────

def _reset_singletons():
    """Reset lazy singletons (for tests only)."""
    global _t1_instance, _t1_isolated, _t3_instance, _collections_cache
    _t1_instance = None
    _t1_isolated = False
    _t3_instance = None
    _collections_cache = ([], 0.0)


def _inject_t1(t1, *, isolated: bool = False):
    """Inject a T1Database for testing."""
    global _t1_instance, _t1_isolated
    _t1_instance = t1
    _t1_isolated = isolated


def _inject_t3(t3):
    """Inject a T3Database for testing."""
    global _t3_instance
    _t3_instance = t3


# ── Tools ─────────────────────────────────────────────────────────────────────

_SEARCH_MAX_RESULTS = 25  # hard cap — prevents agents from requesting n=100
_SEARCH_MAX_CHARS = 16_000  # truncate response to fit comfortably in agent context


@mcp.tool()
def search(query: str, corpus: str = "knowledge,code,docs", n: int = 10) -> str:
    """Semantic search across T3 knowledge collections.

    Args:
        query: Search query string
        corpus: Comma-separated corpus prefixes or full collection names (default: knowledge,code,docs).
                Use "all" to search all corpora (knowledge, code, docs, rdr).
        n: Maximum results to return (capped at 25)
    """
    try:
        from nexus.search_engine import search_cross_corpus
        t3 = _get_t3()

        n = min(n, _SEARCH_MAX_RESULTS)

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
        results = search_cross_corpus(query, target, n_results=n, t3=t3)
        results.sort(key=lambda r: r.distance)
        if not results:
            return "No results."
        lines: list[str] = []
        total_chars = 0
        included = 0
        for r in results:
            title = r.metadata.get("title", "")
            source = r.metadata.get("source_path", "")
            dist = f"{r.distance:.4f}"
            label = title or source or r.id
            snippet = r.content[:200].replace("\n", " ")
            entry = f"[{dist}] {label}\n  {snippet}"
            if total_chars + len(entry) > _SEARCH_MAX_CHARS:
                omitted = len(results) - included
                lines.append(f"\n... {omitted} more results truncated (total {len(results)})")
                break
            lines.append(entry)
            total_chars += len(entry) + 2  # +2 for "\n\n" separator
            included += 1
        return "\n\n".join(lines)
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
        return f"Stored: {doc_id} -> {col_name}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def store_list(collection: str = "knowledge", limit: int = 50) -> str:
    """List entries in a T3 knowledge collection.

    Args:
        collection: Collection name or prefix (default: knowledge)
        limit: Maximum entries to return (default 50)
    """
    try:
        col_name = t3_collection_name(collection)
        entries = _get_t3().list_store(col_name, limit=limit)
        if not entries:
            return f"No entries in {col_name}."
        lines: list[str] = [f"{col_name}  ({len(entries)} entries)"]
        for e in entries:
            doc_id = e.get("id", "")[:16]
            title = (e.get("title") or "")[:40]
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
        return "\n".join(lines)
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
    """Retrieve a memory entry by project and title, or list entries if title is empty.

    Args:
        project: Project namespace
        title: Entry title (empty = list all entries for project)
    """
    try:
        with _t2_ctx() as db:
            if title:
                entry = db.get(project=project, title=title)
                if entry is None:
                    return f"Not found: {project}/{title}"
                content = entry['content']
                truncated = ""
                if len(content) > _SEARCH_MAX_CHARS:
                    content = content[:_SEARCH_MAX_CHARS]
                    truncated = f"\n\n... truncated ({len(entry['content'])} chars total)"
                return (
                    f"[{entry['id']}] {entry['project']}/{entry['title']}\n"
                    f"Tags: {entry.get('tags', '')}\n"
                    f"Updated: {entry.get('timestamp', '')}\n\n"
                    f"{content}{truncated}"
                )
            else:
                entries = db.list_entries(project=project)
                if not entries:
                    return f"No entries for project {project!r}."
                lines: list[str] = [f"{project}  ({len(entries)} entries)"]
                for e in entries:
                    lines.append(f"  [{e['id']}] {e['title']}  ({e.get('timestamp', '')[:10]})")
                return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def memory_search(query: str, project: str = "") -> str:
    """Full-text search across T2 memory entries.

    Args:
        query: Search query (FTS5 syntax)
        project: Optional project filter
    """
    try:
        with _t2_ctx() as db:
            results = db.search(query, project=project or None)
        if not results:
            return "No results."
        lines: list[str] = []
        total_chars = 0
        for r in results:
            snippet = r["content"][:200].replace("\n", " ")
            entry = f"[{r['id']}] {r['project']}/{r['title']}\n  {snippet}"
            if total_chars + len(entry) > _SEARCH_MAX_CHARS:
                lines.append(f"\n... {len(results) - len(lines)} more results truncated")
                break
            lines.append(entry)
            total_chars += len(entry) + 2
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
    n: int = 10,
) -> str:
    """T1 session scratch pad — ephemeral within-session storage.

    Args:
        action: One of "put", "search", "list", "get"
        content: Content to store (for "put")
        query: Search query (for "search")
        tags: Comma-separated tags (for "put")
        entry_id: Entry ID (for "get")
        n: Max results for search
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
            results = t1.search(query, n_results=n)
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

        else:
            return f"Error: unknown action {action!r}. Use: put, search, list, get"
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
    """Get detailed information about a T3 collection.

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
        lines: list[str] = [
            f"Collection:  {name}",
            f"Documents:   {info.get('count', 0)}",
            f"Index model: {idx_model}",
            f"Query model: {qry_model}",
        ]
        meta = info.get("metadata", {})
        if meta:
            lines.append(f"Metadata:    {meta}")
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
) -> str:
    """Save a query execution plan to the T2 plan library.

    Args:
        query: The original natural-language question
        plan_json: JSON string of the execution plan
        project: Project namespace for scoping (e.g. "nexus")
        outcome: Plan outcome — "success" or "partial"
        tags: Comma-separated tags (e.g. operation types used)
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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    """Run the MCP server on stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
