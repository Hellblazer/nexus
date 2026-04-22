# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP catalog tools: search, show, list, register, update, link, resolve, stats.

10 registered tools + 3 demoted (plain functions, no @mcp.tool()).
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from nexus.mcp_infra import (
    get_recent_search_traces as _get_recent_search_traces,
    get_t1 as _get_t1,
    get_t3 as _get_t3,
    require_catalog as _require_catalog,
    resolve_tumbler_mcp as _resolve_tumbler_mcp,
    t2_ctx as _t2_ctx,
)

mcp = FastMCP("nexus-catalog")

_BULK_DELETE_CONFIRM_THRESHOLD = 10


# ── Registered tools ─────────────────────────────────────────────────────────


# Note: core server also registers a "search" tool. No collision — Claude Code
# disambiguates by server prefix (mcp__plugin_nx_nexus-catalog__search vs
# mcp__plugin_nx_nexus__search).
@mcp.tool(name="search")
def catalog_search(
    query: str = "",
    content_type: str = "",
    author: str = "",
    corpus: str = "",
    owner: str = "",
    file_path: str = "",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Find documents by metadata (title, author, corpus, file path).

    Results are paged. When results are truncated, a ``_pagination`` entry appears
    at the end with ``next_offset``. Pass that value as ``offset`` to get the next page.

    Returns catalog entries with tumbler, physical_collection, and metadata — NOT document
    content. Use the ``search`` tool for semantic content search within collections.
    Use catalog_search first to discover WHICH collections to search, then search for content.

    Filters: query (free-text), author, corpus, owner, file_path, content_type (exact match).
    At least one filter required."""
    cat, err = _require_catalog()
    if err:
        return [{"error": err}]
    try:
        from nexus.catalog.tumbler import Tumbler
        import json as _json

        # Structured filters via SQL when provided. content_type alone (no
        # query/author/etc.) routes here too — the FTS5 path below requires
        # a free-text query, but content_type is a perfectly valid sole filter
        # ("show me everything of type prose") and the docstring promises it.
        if owner or corpus or file_path or content_type or (author and not query):
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
                f"FROM documents WHERE {' AND '.join(conditions)} LIMIT ? OFFSET ?"
            )
            params.extend([limit + 1, offset])
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
            has_more = len(entries) > limit
            entries = entries[:limit]
            result = [e.to_dict() for e in entries]
            if has_more:
                result.append({"_pagination": {"next_offset": offset + limit, "limit": limit}})
            return result

        # FTS5 free-text search (append author to query if both provided)
        fts_query = query
        if author and query:
            fts_query = f"{query} {author}"
        if not fts_query.strip():
            return [{"error": "query or at least one filter required"}]
        all_results = cat.find(fts_query, content_type=content_type or None)
        page = all_results[offset:offset + limit]
        result = [e.to_dict() for e in page]
        if offset + limit < len(all_results):
            result.append({"_pagination": {"next_offset": offset + limit, "limit": limit}})
        return result
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool(name="show")
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

        d = entry.to_dict()
        d["links_from"] = [l.to_dict() for l in cat.links_from(entry.tumbler)]
        d["links_to"] = [l.to_dict() for l in cat.links_to(entry.tumbler)]
        return d
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="list")
def catalog_list(
    owner: str = "",
    content_type: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List catalog entries with optional filters.

    Results are paged. When truncated, a ``_pagination`` entry appears at the end
    with ``next_offset``. Pass that as ``offset`` to get the next page."""
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
        page = entries[:limit]
        result = [e.to_dict() for e in page]
        if len(entries) > limit:
            result.append({"_pagination": {"next_offset": offset + limit, "limit": limit}})
        return result
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool(name="register")
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
        from pathlib import Path as _Path

        from nexus.catalog.catalog import make_relative
        from nexus.catalog.tumbler import Tumbler

        # Relativize absolute file_path if it falls under a known repo (RDR-060)
        fp = file_path
        if fp and _Path(fp).is_absolute():
            from nexus.catalog.catalog import _default_registry_path
            from nexus.registry import RepoRegistry

            reg_path = _default_registry_path()
            if reg_path.exists():
                for repo_path_str in RepoRegistry(reg_path).all_info():
                    rel = make_relative(fp, _Path(repo_path_str))
                    if rel != fp:
                        fp = rel
                        break

        tumbler = cat.register(
            Tumbler.parse(owner), title,
            content_type=content_type, file_path=fp,
            corpus=corpus, author=author, year=year,
            physical_collection=physical_collection,
            meta=_json.loads(meta) if meta else None,
        )
        return {"tumbler": str(tumbler), "title": title}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="update")
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


@mcp.tool(name="link")
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
        # RDR-061 E2: log relevance correlation for the most recent search.
        # Filter chunks by collection match to the link target — a coarse
        # but cheap signal that the search likely led to this link.
        try:
            t1, _ = _get_t1()
            session_id = t1.session_id if hasattr(t1, "session_id") else ""
            traces = _get_recent_search_traces(session_id) if session_id else []
            if traces:
                target_entry = cat.resolve(tt)
                target_col = target_entry.physical_collection if target_entry else ""
                if target_col:
                    latest = traces[-1]
                    rows = [
                        (latest["query"], chunk_id, chunk_col, "linked", session_id)
                        for chunk_id, chunk_col in latest["chunks"]
                        if chunk_col == target_col
                    ]
                    if rows:
                        with _t2_ctx() as db:
                            db.log_relevance_batch(rows)
        except Exception:
            import structlog
            structlog.get_logger().debug("relevance_log_link_failed", exc_info=True)
        return {"from": str(ft), "to": str(tt), "type": link_type, "created": created}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="links")
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
            "nodes": [n.to_dict() for n in result["nodes"]],
            "edges": [e.to_dict() for e in result["edges"]],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="link_query")
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

    Results are paged. When truncated, a ``_pagination`` entry appears at the end
    with ``next_offset``. Pass that as ``offset`` to get the next page.

    NOT a retrieval step — use catalog_links (or the `traverse` MCP tool) for graph traversal.
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
            limit=limit + 1, offset=offset,
        )
        has_more = len(links) > limit
        links = links[:limit]
        result = [l.to_dict() for l in links]
        if has_more:
            result.append({"_pagination": {"next_offset": offset + limit, "limit": limit}})
        return result
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool(name="resolve")
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


@mcp.tool(name="stats")
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


# ── Demoted tools (plain functions, no @mcp.tool()) ──────────────────────────


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


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    """Run the catalog MCP server on stdio transport."""
    from nexus.logging_setup import configure_logging
    from nexus.mcp_infra import check_version_compatibility

    configure_logging("mcp")
    check_version_compatibility()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
