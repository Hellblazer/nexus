# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP catalog tools: search, show, list, register, update, link, resolve, stats.

10 registered tools + 3 demoted (plain functions, no @mcp.tool()).
"""
from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from nexus.mcp_infra import (
    get_catalog_writer as _get_catalog_writer,
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


def _file_path_matches(entry_path: str, wanted: str) -> bool:
    """True when *wanted* names the same document as the stored *entry_path*.

    nexus-fhim9. The catalog stores a document's path RELATIVE to its owner
    root (``tests/test_aspect_worker.py``) while recording the absolute form
    separately in ``source_uri`` (``file:///Users/.../tests/test_aspect_worker.py``).
    An exact ``==`` against the stored value therefore rejects the absolute
    path -- which is the form a caller actually holds. It is what
    ``os.path.abspath`` produces, what ``source_uri`` records, and what any
    tool that just touched the file has in hand.

    Measured before the fix: the same document, in the same catalog, one call
    apart --

        file_path="tests/test_aspect_worker.py"            -> tumbler 1.1.100
        file_path="/Users/.../tests/test_aspect_worker.py" -> empty

    Returning empty for a path the catalog demonstrably holds under a
    different spelling is a false NEGATIVE, and the tool's name invites the
    caller to trust it -- the nexus-yg70j family, where an identity resolves
    from only one vantage point and reports absence rather than refusing.

    Matching is deliberately narrow: exact, or the stored relative path is a
    trailing path-SEGMENT suffix of the absolute one (and vice versa). Segment
    boundaries matter -- ``a/foo.py`` must not match ``a/barfoo.py`` -- so the
    suffix test is anchored on a separator rather than being a bare
    ``endswith``. No globbing, no basename-only matching: a bare
    ``foo.py`` still will not match, because that would trade a false negative
    for a false positive across every same-named file in the tree.
    """
    if not entry_path or not wanted:
        return False
    if entry_path == wanted:
        return True
    a, b = entry_path.rstrip("/"), wanted.rstrip("/")
    long_, short_ = (a, b) if len(a) >= len(b) else (b, a)
    # The shorter side must itself be multi-segment. Without this a bare
    # basename matches ("tests/x.py".endswith("/x.py") is True), which is the
    # false POSITIVE this widening exists to avoid -- it would collide across
    # every same-named file in the tree. Caught by this function's own
    # negative test on first run.
    if "/" not in short_:
        return False
    return long_.endswith("/" + short_)


# Note: core server also registers a "search" tool. No collision — Claude Code
# disambiguates by server prefix (mcp__plugin_conexus_nexus-catalog__search vs
# mcp__plugin_conexus_nexus__search).
@mcp.tool(
    name="search",
    title="Catalog Metadata Search",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def catalog_search(
    query: Annotated[str, Field(description="Free-text query, matched by full-text search.")] = "",
    content_type: Annotated[str, Field(description="Exact content_type filter (e.g. \"code\", \"paper\", \"rdr\").")] = "",
    author: Annotated[str, Field(description="Author substring filter, case-insensitive.")] = "",
    corpus: Annotated[str, Field(description="Exact corpus filter.")] = "",
    owner: Annotated[str, Field(description="Owner tumbler or registered owner name.")] = "",
    file_path: Annotated[str, Field(description="File path filter (absolute or repo-relative; both forms match).")] = "",
    limit: Annotated[int, Field(description="Page size.")] = 20,
    offset: Annotated[int, Field(description="Entries to skip, for pagination.")] = 0,
) -> list[dict]:
    """Find catalog documents by metadata: title, author, corpus, owner, or file path.

    Use this (the catalog server's `search`) to discover WHICH
    documents and collections exist before reading content; use the
    nexus server's `search`/`query` tools for semantic content search
    within those collections.

    Returns catalog entries with tumbler, physical_collection, and
    metadata — never document content. A truncated page appends a
    `_pagination` entry with `next_offset`.

    Constraints:
    - At least one of `query`/`content_type`/`author`/`corpus`/`owner`/
      `file_path` is required.
    """
    cat, err = _require_catalog()
    if err:
        return [{"error": err}]
    try:
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — function-local import avoids catalog import at module load
        import json as _json  # noqa: PLC0415 — deliberate function-local import

        # Structured filters via SQL when there's NO free-text query. The
        # SQL path filters by exact-match structural fields (owner, corpus,
        # file_path, content_type, author). When ``query`` IS provided
        # alongside content_type, the FTS5 path below handles both via
        # ``cat.find(query, content_type=...)``. The previous routing put
        # content_type unconditionally on the SQL side, which silently
        # dropped the ``query`` filter for the (query + content_type)
        # combination — nexus-a414 Part 1.
        if not query.strip() and (
            owner or corpus or file_path or content_type or author
        ):
            # Route through catalog API — no direct ._db access (service-mode compatible).
            # The /list endpoint dispatches on a single filter; fetch via the most specific
            # one available and Python-filter the rest.
            if owner:
                candidates = cat.by_owner(Tumbler.parse(owner))
                if corpus:
                    candidates = [e for e in candidates if e.corpus == corpus]
                if file_path:
                    candidates = [e for e in candidates if _file_path_matches(e.file_path, file_path)]
                if author:
                    candidates = [e for e in candidates if author.lower() in (e.author or "").lower()]
                if content_type:
                    candidates = [e for e in candidates if e.content_type == content_type]
                entries = candidates[offset:offset + limit + 1]
            elif corpus:
                candidates = cat.by_corpus(corpus)
                if file_path:
                    candidates = [e for e in candidates if _file_path_matches(e.file_path, file_path)]
                if author:
                    candidates = [e for e in candidates if author.lower() in (e.author or "").lower()]
                if content_type:
                    candidates = [e for e in candidates if e.content_type == content_type]
                entries = candidates[offset:offset + limit + 1]
            elif content_type:
                candidates = cat.by_content_type(content_type)
                if file_path:
                    candidates = [e for e in candidates if _file_path_matches(e.file_path, file_path)]
                if author:
                    candidates = [e for e in candidates if author.lower() in (e.author or "").lower()]
                entries = candidates[offset:offset + limit + 1]
            else:
                # No major filter — fetch paged chunk and apply remaining Python filters.
                # HttpCatalogClient.all_documents() supports offset; SQLite Catalog does not.
                import inspect as _inspect  # noqa: PLC0415 — branch-local import, only needed on the no-major-filter path
                sig = _inspect.signature(cat.all_documents)
                if "offset" in sig.parameters:
                    batch = cat.all_documents(limit=limit + offset + 1, offset=0)
                else:
                    batch = cat.all_documents(limit + offset + 1)
                if file_path:
                    batch = [e for e in batch if _file_path_matches(e.file_path, file_path)]
                if author:
                    batch = [e for e in batch if author.lower() in (e.author or "").lower()]
                entries = batch[offset:offset + limit + 1]
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
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error-dict so the tool call never crashes the client
        return [{"error": str(e)}]


@mcp.tool(
    name="show",
    title="Show Catalog Entry",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def catalog_show(
    tumbler: Annotated[str, Field(description="Document or owner tumbler (e.g. \"1.2.5\" or \"1.2\").")] = "",
    title: Annotated[str, Field(description="Exact or best-matching document title. Ignored when tumbler is set.")] = "",
) -> dict:
    """Show one catalog entry's full metadata and its links to/from other entries.

    Use `links` instead when you only need the link graph, not the
    entry's own metadata. Returns all metadata plus `links_from` and
    `links_to` arrays in one call, or `{"kind": "owner", ...}` when
    `tumbler` names an owner prefix rather than a document.
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deliberate function-local import

        entry = None
        if tumbler:
            t = Tumbler.parse(tumbler)
            # nexus-v3w9n: catalog-034 grammar makes tumbler depth
            # unambiguous — an owner prefix is EXACTLY 2 segments, a
            # document tumbler is >= 3. cat.resolve() never consults
            # owners, so a depth-2 argument would otherwise render an
            # undifferentiated "Not found" for a perfectly valid owner
            # prefix.
            if t.depth == 2:
                owner = cat.get_owner_by_prefix(str(t))
                if owner is None:
                    return {"error": f"Not found: {tumbler}"}
                # nexus-v3w9n fix round 1 (substantive-critic Significant
                # finding): explicit discriminator rather than relying on
                # callers to infer "owner vs document" from key-shape.
                # Document JSON is unchanged.
                d = {"kind": "owner", **owner}
                d["document_count"] = len(cat.by_owner(t))
                return d
            entry = cat.resolve(t)
        elif title:
            results = cat.find(title)
            entry = results[0] if results else None

        if entry is None:
            return {"error": f"Not found: {tumbler or title}"}

        d = entry.to_dict()
        # links_from/links_to return list[CatalogLink] (SQLite) or list[dict] (service mode).
        d["links_from"] = [l if isinstance(l, dict) else l.to_dict() for l in cat.links_from(entry.tumbler)]
        d["links_to"] = [l if isinstance(l, dict) else l.to_dict() for l in cat.links_to(entry.tumbler)]
        return d
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error-dict so the tool call never crashes the client
        return {"error": str(e)}


@mcp.tool(
    name="list",
    title="List Catalog Entries",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def catalog_list(
    owner: Annotated[str, Field(description="Owner tumbler filter; \"\" lists across every owner.")] = "",
    content_type: Annotated[str, Field(description="Exact content_type filter; \"\" for every type.")] = "",
    limit: Annotated[int, Field(description="Page size.")] = 50,
    offset: Annotated[int, Field(description="Entries to skip, for pagination.")] = 0,
) -> list[dict]:
    """List catalog entries, optionally filtered by owner or content_type.

    Use `search` (this server) instead when you need a free-text or
    author/corpus/file_path filter. Returns a paged list of catalog
    entries; a truncated page appends a `_pagination` entry with
    `next_offset`.
    """
    cat, err = _require_catalog()
    if err:
        return [{"error": err}]
    try:
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deliberate function-local import

        if owner:
            entries = cat.by_owner(Tumbler.parse(owner))
            if content_type:
                entries = [e for e in entries if e.content_type == content_type]
            entries = entries[offset:offset + limit + 1]
        else:
            # Route through catalog API — no direct ._db access (service-mode compatible).
            # nexus-blk2 Part 1: content_type filter must be server-side so pagination is correct.
            if content_type:
                candidates = cat.by_content_type(content_type)
                entries = candidates[offset:offset + limit + 1]
            else:
                # HttpCatalogClient.all_documents() supports offset; SQLite Catalog does not.
                # Detect by signature to stay backward-compatible.
                import inspect as _inspect  # noqa: PLC0415 — branch-local import, only needed when no owner filter is set
                sig = _inspect.signature(cat.all_documents)
                if "offset" in sig.parameters:
                    entries = cat.all_documents(limit=limit + 1, offset=offset)
                else:
                    # SQLite: fetch limit+offset+1, slice in Python
                    all_docs = cat.all_documents(limit + offset + 1)
                    entries = all_docs[offset:offset + limit + 1]
        has_more = len(entries) > limit
        page = entries[:limit]
        result = [e.to_dict() for e in page]
        if has_more:
            result.append({"_pagination": {"next_offset": offset + limit, "limit": limit}})
        return result
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error-dict so the tool call never crashes the client
        return [{"error": str(e)}]


# HISTORY (RDR-096 P3.1): source_uri is the persistent URI identity. Omit
# to auto-derive file://<abspath> from file_path; pass an explicit URI
# (a custom scheme, https://, nx-scratch://) to store verbatim. Malformed
# URIs raise at register-time.
@mcp.tool(
    name="register",
    title="Register Document in Catalog",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
def catalog_register(
    title: Annotated[str, Field(description="Document title.")],
    owner: Annotated[str, Field(description="Owner tumbler this document is registered under.")],
    content_type: Annotated[str, Field(description="Content type (e.g. \"paper\", \"code\", \"rdr\", \"knowledge\").")] = "paper",
    author: Annotated[str, Field(description="Author name.")] = "",
    year: Annotated[int, Field(description="Publication year; 0 = unknown.")] = 0,
    file_path: Annotated[str, Field(
        description="On-disk path (absolute or repo-relative). A known repo root relativizes an absolute path automatically.",
    )] = "",
    source_uri: Annotated[str, Field(
        description="Explicit persistent URI identity. Omit to auto-derive file://<abspath> from file_path.",
    )] = "",
    corpus: Annotated[str, Field(description="Corpus label for grouping and routing.")] = "",
    physical_collection: Annotated[str, Field(
        description="Backing T3 collection name; may be empty for a ghost (catalog-only) entry.",
    )] = "",
    meta: Annotated[str, Field(description="Optional extra metadata as a JSON object string.")] = "",
) -> dict:
    """Register a new document in the catalog, assigning it a tumbler.

    Use `update` instead once a document is already registered and only
    its metadata needs to change. Returns `{"tumbler": ..., "title": ...}`,
    or `{"error": ...}` naming why registration was refused (e.g. an
    ephemeral worktree/tempdir path with no owning repo_root).
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    writer = _get_catalog_writer()
    try:
        import json as _json  # noqa: PLC0415 — deliberate function-local import
        from pathlib import Path as _Path  # noqa: PLC0415 — deliberate function-local import

        from nexus.catalog.types import make_relative  # noqa: PLC0415 — function-local import avoids catalog import at module load
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — function-local import avoids catalog import at module load

        # Relativize absolute file_path if it falls under a known repo
        # (RDR-060). RDR-137 Phase 3.2 (nexus-tts0d.7): use the catalog-
        # backed list_repos_dual reader. Catalog owners contribute
        # canonical repo_root; registry fills in pre-catalog installs.
        fp = file_path
        if fp and _Path(fp).is_absolute():
            from nexus.catalog.types import _default_registry_path  # noqa: PLC0415 — branch-local import, only needed for absolute file_path relativization
            from nexus.repos import list_repos_dual  # noqa: PLC0415 — branch-local import, only needed for absolute file_path relativization

            reg_path = _default_registry_path()
            # RDR-137 followup IMP-19 (nexus-43qgm.19): prefer the
            # LONGEST matching prefix so a nested-repo scenario
            # (parent + child both registered) anchors the path under
            # the child, not the parent. Pre-fix iteration picked the
            # first sorted match (typically parent), producing a
            # longer-than-needed relative path.
            candidates = [
                rp for rp in list_repos_dual(cat=cat, registry_path=reg_path)
                if make_relative(fp, _Path(rp)) != fp
            ]
            if candidates:
                best = max(candidates, key=len)
                fp = make_relative(fp, _Path(best))

        # nexus-u8n4r: this is a single-doc, user-explicit registration
        # (unlike the bulk index hooks), so a clear refusal beats a
        # silent skip — the caller named this path deliberately. Same
        # owner-root exception as the bulk hooks: a throwaway owner
        # explicitly rooted in a worktree/tempdir stays registrable.
        # Review fix C1 (code-review-expert): test the ABSOLUTE
        # registered identity, never the post-relativization ``fp`` —
        # see ``reconstruct_absolute_registered_path``'s docstring for
        # why the naive ``fp or file_path`` was silently inert for a
        # worktree nested inside an already-registered repo.
        from nexus.repo_identity import (  # noqa: PLC0415 — deliberate function-local import
            owner_repo_root_best_effort,
            reconstruct_absolute_registered_path,
            should_skip_ephemeral_registration,
        )

        _owner_repo_root = owner_repo_root_best_effort(cat, owner)
        _registered_path = reconstruct_absolute_registered_path(
            file_path, fp, _owner_repo_root,
        )
        if should_skip_ephemeral_registration(_registered_path, _owner_repo_root):
            return {
                "error": (
                    f"refusing to register {_registered_path!r}: it sits under "
                    f"an agent worktree or system temp dir (nexus-u8n4r) and "
                    f"owner {owner!r}'s repo_root is not itself rooted there — "
                    f"the ephemeral checkout will vanish and leave a permanent "
                    f"orphan. If this is a deliberate throwaway owner rooted "
                    f"in a worktree/tempdir, register the owner with that "
                    f"repo_root first so the exception applies."
                )
            }

        tumbler = writer.register(
            Tumbler.parse(owner), title,
            content_type=content_type, file_path=fp,
            corpus=corpus, author=author, year=year,
            physical_collection=physical_collection,
            meta=_json.loads(meta) if meta else None,
            source_uri=source_uri,
        )
        return {"tumbler": str(tumbler), "title": title}
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error-dict so the tool call never crashes the client
        return {"error": str(e)}
    finally:
        writer.close()


@mcp.tool(
    name="update",
    title="Update Catalog Entry",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
def catalog_update(
    tumbler: Annotated[str, Field(description="Document tumbler to update.")],
    title: Annotated[str, Field(description="New title; omit (\"\") to leave unchanged.")] = "",
    author: Annotated[str, Field(description="New author; omit (\"\") to leave unchanged.")] = "",
    year: Annotated[int, Field(description="New publication year; 0 leaves it unchanged.")] = 0,
    corpus: Annotated[str, Field(description="New corpus label; omit (\"\") to leave unchanged.")] = "",
    physical_collection: Annotated[str, Field(description="New backing T3 collection name; omit (\"\") to leave unchanged.")] = "",
    meta: Annotated[str, Field(description="New extra metadata as a JSON object string; omit (\"\") to leave unchanged.")] = "",
) -> dict:
    """Update one or more fields on an already-registered catalog entry.

    Use `register` instead for a document that has no tumbler yet. Only
    fields passed as non-empty/non-zero are changed. Returns
    `{"tumbler": ..., "updated": [<field names>]}`, or an error if no
    field was given to update.
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    writer = _get_catalog_writer()
    try:
        import json as _json  # noqa: PLC0415 — deliberate function-local import

        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — function-local import avoids catalog import at module load

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
        writer.update(Tumbler.parse(tumbler), **fields)
        return {"tumbler": tumbler, "updated": list(fields.keys())}
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error-dict so the tool call never crashes the client
        return {"error": str(e)}
    finally:
        writer.close()


@mcp.tool(
    name="link",
    title="Create Catalog Link",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
def catalog_link(
    from_tumbler: Annotated[str, Field(description="Source document's tumbler or exact title.")],
    to_tumbler: Annotated[str, Field(description="Target document's tumbler or exact title.")],
    link_type: Annotated[str, Field(
        description="Link type: a built-in (cites, implements, implements-heuristic, supersedes, relates, quotes, comments) or a custom string.",
    )],
    created_by: Annotated[str, Field(description="Who or what created this link.")] = "user",
    from_span: Annotated[str, Field(description="Optional span identifier anchoring the link to a location within the source.")] = "",
    to_span: Annotated[str, Field(description="Optional span identifier anchoring the link to a location within the target.")] = "",
) -> dict:
    """Create a relationship between two catalog documents.

    Use `links` afterward to read the graph back. Returns
    `{"from", "to", "type", "created": bool}` — `created=False` means the
    link already existed and was merged (co-discovery tracking), not an
    error.

    Constraints:
    - Both endpoints must already exist in the catalog, or the call errors.
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    writer = _get_catalog_writer()
    try:
        ft, err = _resolve_tumbler_mcp(cat, from_tumbler)
        if err:
            return {"error": err}
        tt, err = _resolve_tumbler_mcp(cat, to_tumbler)
        if err:
            return {"error": err}
        created = writer.link(ft, tt, link_type, created_by, from_span=from_span, to_span=to_span)
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
        except Exception:  # noqa: BLE001 — best-effort relevance telemetry must not crash the link op; surfaced via log.debug
            import structlog  # noqa: PLC0415 — branch-local import, only needed on the telemetry-failure path
            structlog.get_logger().debug("relevance_log_link_failed", exc_info=True)
        return {"from": str(ft), "to": str(tt), "type": link_type, "created": created}
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error-dict so the tool call never crashes the client
        return {"error": str(e)}
    finally:
        writer.close()


@mcp.tool(
    name="links",
    title="Get Document Links",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def catalog_links(
    tumbler: Annotated[str, Field(description="Document tumbler or exact title to start from.")],
    direction: Annotated[str, Field(description="Traversal direction: \"out\", \"in\", or \"both\".")] = "both",
    link_type: Annotated[str, Field(description="Restrict to this link type; \"\" follows every type.")] = "",
    depth: Annotated[int, Field(description="BFS depth; 1 (default) returns direct neighbors only.")] = 1,
) -> dict:
    """Walk the catalog link graph from one entry, live documents only.

    Use `link_query` instead for an admin/audit view that also includes
    links to deleted (orphaned) documents. Returns
    `{"nodes": [entry dicts], "edges": [link dicts]}`.
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        t, err = _resolve_tumbler_mcp(cat, tumbler)
        if err:
            return {"error": err}
        # nexus-qtj24: normalize to the plural ``link_types`` both backends
        # accept. HttpCatalogClient.graph is keyword-only and has NO singular
        # ``link_type`` param, so the old ``link_type=`` kwarg raised TypeError
        # in service mode (swallowed into {"error": ...}).
        link_types = [link_type] if link_type else None
        result = cat.graph(t, depth=depth, direction=direction, link_types=link_types)
        # nexus-u26b4: nodes/edges are CatalogEntry/CatalogLink objects on both
        # the SQLite path and the service /traverse path (HttpCatalogClient.graph
        # now converts wire dicts to typed objects — the isinstance(n, dict)
        # guard nexus-qtj24 added here is no longer reachable and has been
        # dropped; mirrors catalog_link_query's unconditional .to_dict()).
        return {
            "nodes": [n.to_dict() for n in result["nodes"]],
            "edges": [e.to_dict() for e in result["edges"]],
        }
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error-dict so the tool call never crashes the client
        return {"error": str(e)}


@mcp.tool(
    name="link_query",
    title="Query Link Table",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def catalog_link_query(
    from_tumbler: Annotated[str, Field(description="Filter to links from this tumbler; \"\" for any source.")] = "",
    to_tumbler: Annotated[str, Field(description="Filter to links to this tumbler; \"\" for any target.")] = "",
    link_type: Annotated[str, Field(description="Filter to this link type; \"\" for any type.")] = "",
    created_by: Annotated[str, Field(description="Filter to links created by this identity; \"\" for any creator.")] = "",
    direction: Annotated[str, Field(description="Traversal direction when tumbler is set: \"out\", \"in\", or \"both\".")] = "both",
    tumbler: Annotated[str, Field(description="Filter to links touching this tumbler, in either direction (per `direction`).")] = "",
    created_at_before: Annotated[str, Field(description="ISO timestamp; only links created before this time.")] = "",
    limit: Annotated[int, Field(description="Page size.")] = 50,
    offset: Annotated[int, Field(description="Entries to skip, for pagination.")] = 0,
) -> list[dict]:
    """Query the raw link table by any combination of filters, for admin or audit use.

    Use `links` instead for graph traversal — this tool returns ALL
    matching links, including orphans pointing at deleted documents,
    which `links` deliberately excludes. Returns a paged list of link
    records; a truncated page appends a `_pagination` entry with
    `next_offset`.
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
        # link_query returns list[CatalogLink] (SQLite) or list[dict] (service mode).
        result = [l if isinstance(l, dict) else l.to_dict() for l in links]
        if has_more:
            result.append({"_pagination": {"next_offset": offset + limit, "limit": limit}})
        return result
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error-dict so the tool call never crashes the client
        return [{"error": str(e)}]


@mcp.tool(
    name="resolve",
    title="Resolve Identifier to Entry",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def catalog_resolve(
    tumbler: Annotated[str, Field(description="Document tumbler, dotted form (e.g. \"1.2.3\").")] = "",
    owner: Annotated[str, Field(description="Owner tumbler or registered owner name, dotted form (e.g. \"1.2\").")] = "",
    corpus: Annotated[str, Field(description="Corpus label.")] = "",
) -> list[str]:
    """Resolve a tumbler, owner, or corpus to its physical T3 collection name(s).

    Use `search`/`query` (nexus server) with the returned collection names
    to search their content. Returns a sorted list of distinct physical
    collection names; entries with no physical_collection are skipped.
    """
    cat, err = _require_catalog()
    if err:
        return [f"Error: {err}"]
    try:
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — function-local import avoids catalog import at module load

        # nexus-blk2 Part 2: dotted-tumbler form is required (e.g. "1.2.3"
        # for a document, "1.2" for an owner). The dashed format produced
        # by ``nx doctor`` (e.g. "1-2188", "Luciferase-f2d57dbc") is the
        # physical-collection prefix shape, NOT a tumbler. Tumbler.parse
        # used to leak its int() ValueError to the caller; catch and
        # surface an actionable diagnostic instead.
        def _parse_tumbler_or_raise(raw: str, field: str) -> Tumbler:
            try:
                return Tumbler.parse(raw)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"{field}={raw!r}: not a dotted tumbler (e.g. '1.2.3'). "
                    f"If you have a physical collection prefix like "
                    f"'1-2188' from `nx doctor`, that is NOT a tumbler. "
                    f"underlying: {exc}"
                ) from exc

        collections: set[str] = set()
        if tumbler:
            entry = cat.resolve(_parse_tumbler_or_raise(tumbler, "tumbler"))
            if entry and entry.physical_collection:
                collections.add(entry.physical_collection)
        if owner:
            # GH #1527 (nexus-qiah5): a registered owner NAME is accepted
            # here too, the same way `nx catalog resolve --owner` already
            # does; the owner table maps it to the tumbler.
            from nexus.catalog.owner_scope import OwnerScopeError, resolve_owner_scope  # noqa: PLC0415 — deferred, branch-local
            try:
                owner_tumbler = resolve_owner_scope(cat, owner)
            except OwnerScopeError as exc:
                raise ValueError(f"owner {exc}") from exc
            entries = cat.by_owner(_parse_tumbler_or_raise(owner_tumbler, "owner"))
            for e in entries:
                if e.physical_collection:
                    collections.add(e.physical_collection)
        if corpus:
            # Route through catalog API — no direct ._db access (service-mode compatible).
            for e in cat.by_corpus(corpus):
                if e.physical_collection:
                    collections.add(e.physical_collection)
        return sorted(collections)
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error so the tool call never crashes the client
        return [f"Error: {e}"]


@mcp.tool(
    name="stats",
    title="Catalog Statistics",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def catalog_stats() -> dict:
    """Get catalog-wide health counts: owners, documents, links, collections, chunks.

    Returns `{owners, documents, links, collections, chunks, by_link_type}`.
    """
    cat, err = _require_catalog()
    if err:
        return {"error": err}
    try:
        # Both Catalog (SQLite) and HttpCatalogClient implement .stats() —
        # nexus-qnp5s migrated the SQLite backend to expose stats() on the
        # public API so both backends are uniform. The old _db fallback branch
        # has been removed (it was unreachable once Catalog.stats() was added).
        s = cat.stats()
        # Normalise field names: Java returns doc_count/link_count/owner_count/collection_count.
        # SQLite Catalog returns the same keys (added for parity in nexus-qnp5s).
        return {
            "owners":       s.get("owner_count",      s.get("owners",    0)),
            "documents":    s.get("doc_count",        s.get("documents", 0)),
            "links":        s.get("link_count",       s.get("links",     0)),
            "collections":  s.get("collection_count", s.get("collections", 0)),
            "chunks":       s.get("chunk_count",      s.get("chunks",    0)),  # nexus-aeceu
            "by_link_type": s.get("links_by_type",    s.get("by_link_type", {})),
        }
    except Exception as e:  # noqa: BLE001 — MCP tool handler: catch-and-return-error-dict so the tool call never crashes the client
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
    writer = _get_catalog_writer()
    try:
        ft, err = _resolve_tumbler_mcp(cat, from_tumbler)
        if err:
            return {"error": err}
        tt, err = _resolve_tumbler_mcp(cat, to_tumbler)
        if err:
            return {"error": err}
        removed = writer.unlink(ft, tt, link_type)
        return {"removed": removed, "from": str(ft), "to": str(tt)}
    except Exception as e:  # noqa: BLE001 — demoted tool handler: catch-and-return-error-dict so the call never crashes the caller
        return {"error": str(e)}
    finally:
        writer.close()


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
        # nexus-at2ff: pass the HANDLE, not ``._client``. HttpVectorClient has
        # no such attribute, so this tool returned
        # "'HttpVectorClient' object has no attribute '_client'" in production
        # (swallowed into the error dict by the except below). link_audit's
        # contract now documents the handle explicitly.
        return cat.link_audit(t3=t3)
    except Exception as e:  # noqa: BLE001 — demoted tool handler: catch-and-return-error-dict so the call never crashes the caller
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
    writer = _get_catalog_writer()
    try:
        # Always preview first
        preview = writer.bulk_unlink(
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
        count = writer.bulk_unlink(
            from_t=from_tumbler, to_t=to_tumbler, link_type=link_type,
            created_by=created_by, created_at_before=created_at_before,
        )
        return {"removed": count}
    except Exception as e:  # noqa: BLE001 — demoted tool handler: catch-and-return-error-dict so the call never crashes the caller
        return {"error": str(e)}
    finally:
        writer.close()


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    # nexus-4xgfy (critique 38b7db3d C1): the dominant post-upgrade path is
    # a Claude session spawning THIS process with no bare `nx` invocation in
    # between — the finish trigger must fire here too. Report-safe: the MCP
    # host never kills anything from its own startup; the transition stamp +
    # safe restarts are handled identically to the CLI trigger.
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import
        from nexus.upgrade_finish import check_version_transition  # noqa: PLC0415 — deferred import

        _summary = check_version_transition(nexus_config_dir())
        if _summary:
            import structlog as _sl  # noqa: PLC0415 — deferred import

            _sl.get_logger(__name__).info("upgrade_finish", summary=_summary)
    except Exception:  # noqa: BLE001 — the trigger must never break server startup
        pass

    """Run the catalog MCP server on stdio transport.

    Lifecycle logging: emits ``mcp_server_starting``,
    ``mcp_server_stopping``, and ``mcp_server_crashed`` events to
    ``<config>/logs/mcp.log`` (shared file with the core server; the
    ``server`` field discriminates).
    """
    import os  # noqa: PLC0415 — deferred to entry-point invocation, keeps module import cheap

    import structlog  # noqa: PLC0415 — deferred to entry-point invocation, keeps module import cheap

    from nexus.logging_setup import configure_logging  # noqa: PLC0415 — deferred to entry-point invocation, keeps module import cheap
    from nexus.mcp_infra import check_version_compatibility  # noqa: PLC0415 — deferred to entry-point invocation, keeps module import cheap

    configure_logging("mcp")
    log = structlog.get_logger("nexus.mcp.catalog")
    log.info(
        "mcp_server_starting",
        server="nx-mcp-catalog",
        transport="stdio",
        pid=os.getpid(),
        ppid=os.getppid(),
    )
    # NO first-run daemon install here — retired with the T2 daemon
    # (nexus-i711w Stage 2 sub-stage B); see nexus/mcp/core.py.
    # nexus-gynt2: stranded-install detector — deliberately wired on BOTH
    # MCP servers, in contrast to the embedder advisory's single-channel-
    # on-core decision (RDR-144 P5b below): that one is cosmetic and
    # doubling it is noise; this one is the data-loss-shaped correctness
    # class (unmigrated pre-PG data on a post-deletion release), where a
    # hand-configured catalog-only client must still hear it. Same
    # nexus-4xgfy reasoning as the check_version_transition duplication at
    # the top of this function. Disarmed no-op until the N+1 cut.
    from nexus.mcp._first_run import apply_stranded_notice  # noqa: PLC0415 — deferred to entry-point invocation, keeps module import cheap

    apply_stranded_notice(mcp)
    # nexus-g6vb4 (GH #1414): staleness self-detection — Claude Code users
    # connect to BOTH servers, and this process has its own deferred imports
    # subject to the identical mixed-module-graph failure after an in-place
    # `uv tool upgrade`. Same decorate+warn hook as core.main(); best-effort,
    # never blocks boot.
    # nexus-utpuw.12 / design point 6: one readlink at spawn. A shim-launched
    # process is silent by construction (the shim execs the generation the
    # pointer names); this fires only when something BYPASSED the shim and
    # bound to a generation that is no longer current. Informational —
    # the tree it is running is intact and converges at the next spawn.
    from nexus.upgrade_finish import spawn_tripwire  # noqa: PLC0415 — deferred to entry-point invocation, keeps module import cheap

    spawn_tripwire()
    from nexus.mcp._stale_host import install_stale_host_hook  # noqa: PLC0415 — deferred to entry-point invocation, keeps module import cheap

    install_stale_host_hook(mcp)
    # RDR-144 P5b: the embedder advisory notice is deliberately NOT applied
    # here. It rides core.main()'s server instructions. The .mcpb bundle
    # routes Desktop/Cowork users to nexus.mcp.core only (mcpb/manifest.json),
    # so the target population always gets it via core. Wiring it here too
    # would double the notice for Claude Code users (who connect to BOTH the
    # core and catalog servers). Single channel, on core.
    try:
        check_version_compatibility()
        mcp.run(transport="stdio")
    except (KeyboardInterrupt, SystemExit):
        log.info(
            "mcp_server_stopping", server="nx-mcp-catalog", reason="signal",
        )
        raise
    except BaseException as exc:
        log.exception(
            "mcp_server_crashed",
            server="nx-mcp-catalog",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        log.info("mcp_server_stopping", server="nx-mcp-catalog", reason="exit")


if __name__ == "__main__":
    main()
