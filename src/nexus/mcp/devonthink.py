# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx-mcp-devonthink`` — agent-surface MCP server for DEVONthink (RDR-139 Layer A').

A nexus-owned MCP *server* (third sibling to ``nx-mcp`` / ``nx-mcp-catalog``)
that is simultaneously an MCP *client* to DEVONthink's built-in server via the
Layer A core. It is the answer to "how does a Claude Code agent reach DT" that
declaring DT's own binary cannot give: because this wrapper is nexus code that
ships with the package, it **always spawns successfully** on every consumer
(DT present or not) and gates internally.

**Optionality is the internal gate, not ``.mcp.json``.** On startup
:func:`main` probes :func:`nexus.mcp_client.devonthink.available`; the curated
DT toolset is registered only when DT is reachable. DT absent → only the
always-present ``devonthink_status`` stub is advertised, the process still
exits 0, and no spawn error reaches a DT-less consumer. The ``.mcp.json``
``alwaysLoad: false`` is purely a tool-search startup-cost optimisation; the
wrapper is equally optionality-correct with ``alwaysLoad: true`` (asserted by
``test_layer_a_prime_wrapper``).

**Async bridging.** MCP tool handlers run inside the server's event loop, so
they must NOT call the Layer A sync helpers (``dt_call`` → ``asyncio.run``,
which raises inside a running loop — the very hazard Layer A's running-loop
guard defends). Pure proxies therefore call DT through the async core
(:func:`_dt_proxy`); the ``dt_incorporate`` composite runs the sync Layer B/F
functions via :func:`asyncio.to_thread` (a worker thread has no running loop,
so their internal ``asyncio.run`` is safe).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from nexus.mcp_client.core import MCPEndpoint, call_tool, open_session
from nexus.mcp_client.devonthink import dt_mcp_url

#: Whether the curated DT surface was registered at startup. Set by
#: :func:`build_server`. The availability gate is probed ONCE at startup, so a
#: server that started before DEVONthink launched advertises only the stub for
#: its lifetime; ``devonthink_status`` surfaces a restart hint in that case.
_STARTED_WITH_DT: bool = False


async def _dt_proxy(tool: str, args: dict[str, Any]) -> str:
    """Forward one call to DEVONthink's built-in MCP server, async-native.

    Returns the result as a JSON string, or a structured error string when DT
    is unreachable / the call fails. Never raises (fail-soft, like Layer A).
    """
    endpoint = MCPEndpoint(url=dt_mcp_url())
    try:
        async with open_session(endpoint) as session:
            result = await call_tool(session, tool, args)
    except Exception as exc:  # transport / connect failure
        return json.dumps({"error": f"DEVONthink call failed: {type(exc).__name__}: {exc}", "tool": tool})
    if result is None:
        return json.dumps({"error": "DEVONthink returned no result (unavailable or excluded)", "tool": tool})
    return json.dumps(result)


def _incorporate_sync(uuid: str) -> dict[str, Any]:
    """Layer B + F composite for one already-indexed record (sync; runs off-loop).

    Resolves the record's tumbler (it must already be indexed in nexus), then
    generates DT-derived ``relates`` edges (Layer B) and stamps the nexus
    identity back onto the DT record (Layer F). Returns a structured summary.
    """
    from nexus.catalog.catalog import Catalog  # noqa: PLC0415
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415
    from nexus.catalog.dt_link_generator import generate_dt_links  # noqa: PLC0415
    from nexus.config import catalog_path  # noqa: PLC0415
    from nexus.dt_writeback import writeback_record  # noqa: PLC0415

    cp = catalog_path()
    if not Catalog.is_initialized(cp):
        return {"error": "nexus catalog is not initialized"}
    cat = None
    try:
        cat = make_catalog_reader()
        entry = cat.by_source_uri(f"x-devonthink-item://{uuid}")
        if entry is None:
            return {
                "error": "record is not indexed in nexus; run "
                "`nx dt index --uuid <uuid>` (or capture) first",
                "uuid": uuid,
            }
        tumbler = entry.tumbler
        links = generate_dt_links(cat, tumbler, uuid)
        writeback = writeback_record(uuid, str(tumbler))
        return {"tumbler": str(tumbler), "links": links, "writeback": writeback}
    finally:
        if cat is not None:
            cat._db.close()


# ── Tool handlers (registered conditionally by build_server) ─────────────────

async def devonthink_status() -> str:
    """Report whether DEVONthink is reachable and which databases are open.

    The one always-advertised tool: present even when DT is absent so a DT-less
    agent gets a clean status answer instead of a missing server.
    """
    endpoint = MCPEndpoint(url=dt_mcp_url())
    try:
        async with open_session(endpoint) as session:
            running = await call_tool(session, "is_running", {})
            dbs = await call_tool(session, "get_databases", {})
    except Exception as exc:
        return json.dumps({"available": False, "reason": f"{type(exc).__name__}: {exc}"})
    available = bool(running) and bool(running.get("running"))
    dbs_payload = dbs.get("result", dbs) if isinstance(dbs, dict) else dbs
    n_dbs = len(dbs_payload) if isinstance(dbs_payload, list) else 0
    status: dict[str, Any] = {"available": available, "databases": n_dbs}
    # Startup-only gate: if DT is reachable NOW but the curated surface was not
    # registered at startup (server started before DT launched), the DT tools
    # are absent until restart. Surface that so the agent isn't confused.
    if available and not _STARTED_WITH_DT:
        status["restart_required"] = True
        status["note"] = (
            "DEVONthink is reachable but this server started before it was "
            "available, so the DT tools are not registered. Restart "
            "nx-mcp-devonthink to advertise the full surface."
        )
    return json.dumps(status)


async def get_record_text(uuid: str) -> str:
    """Get a record's plain-text body (content read; UUID comes from a nexus search)."""
    return await _dt_proxy("get_record_text", {"uuid": uuid})


async def get_record_annotation(uuid: str) -> str:
    """Get the UUID of a record's annotation note (read with get_record_text)."""
    return await _dt_proxy("get_record_annotation", {"uuid": uuid})


async def find_similar_records(uuid: str, limit: int = 25) -> str:
    """Find records DEVONthink's AI considers similar to this one ('See Also')."""
    return await _dt_proxy("find_similar_records", {"mode": "record", "uuid": uuid, "limit": limit})


async def classify_record(uuid: str) -> str:
    """Get DEVONthink AI's suggested groups for a record (organizational hint)."""
    return await _dt_proxy("classify_record", {"uuid": uuid})


async def extract_record_content(uuid: str) -> str:
    """Get a record's AI-optimised text content (for non-file-backed records)."""
    return await _dt_proxy("extract_record_content", {"uuid": uuid})


async def extract_record_highlights(uuid: str) -> str:
    """Get a markdown summary of a record's highlights / annotations."""
    return await _dt_proxy("extract_record_highlights", {"uuid": uuid})


async def extract_record_mentions(uuid: str) -> str:
    """Get a markdown summary of a record's mentions."""
    return await _dt_proxy("extract_record_mentions", {"uuid": uuid})


async def resolve_doi_metadata(doi: str) -> str:
    """Resolve a DOI to CrossRef bibliographic metadata."""
    return await _dt_proxy("resolve_doi_metadata", {"doi": doi})


async def search_crossref(query: str, limit: int = 20) -> str:
    """Search CrossRef for scholarly works by free-text query (discovery only)."""
    return await _dt_proxy("search_crossref", {"query": query, "limit": limit})


async def resolve_google_books_metadata(isbn: str = "", query: str = "") -> str:
    """Resolve an ISBN (or title query) to Google Books bibliographic metadata."""
    args = {k: v for k, v in {"isbn": isbn, "query": query}.items() if v}
    return await _dt_proxy("resolve_google_books_metadata", args)


async def capture_web_page(url: str, capture_type: str = "webarchive") -> str:
    """Capture a URL into DEVONthink (html/webarchive/markdown/pdf). Returns the new record."""
    return await _dt_proxy("capture_web_page", {"url": url, "type": capture_type})


async def download_pdf_from_doi(doi: str, contact_email: str = "") -> str:
    """Resolve a DOI and download its open-access PDF into DEVONthink (Unpaywall)."""
    args: dict[str, Any] = {"doi": doi}
    if contact_email:
        args["contact_email"] = contact_email
    return await _dt_proxy("download_pdf_from_doi", args)


async def import_file(path: str, mode: str = "import") -> str:
    """Import (copy in) or index (reference in place) a loose file into DEVONthink."""
    return await _dt_proxy("import_file", {"path": path, "mode": mode})


async def get_databases() -> str:
    """List the open DEVONthink databases."""
    return await _dt_proxy("get_databases", {})


async def get_record_links(uuid: str) -> str:
    """Get a record's deliberate item links (both directions)."""
    return await _dt_proxy("get_record_links", {"uuid": uuid, "direction": "both", "kind": "item"})


async def dt_incorporate(uuid: str) -> str:
    """Incorporate an already-indexed DT record into the nexus graph (Layer B + F).

    Composite: resolves the record's tumbler, creates 'relates' edges to its
    DEVONthink similarity + explicit-link neighbours that are also indexed in
    nexus (Layer B), and stamps the nexus identity back onto the DT record
    (Layer F: nx-indexed / nx-tumbler tags + tumbler backlink annotation). The
    record must already be indexed (run ``nx dt index`` / capture first).
    """
    result = await asyncio.to_thread(_incorporate_sync, uuid)
    return json.dumps(result)


#: The curated DT toolset advertised only when DEVONthink is reachable.
#: ``devonthink_status`` is excluded here — it is registered unconditionally.
#:
#: Curation respects the RDR's "Explicitly out of scope" boundary: DT's own
#: SELECTORS / CRUD (search_records, lookup_records, get_record_properties,
#: get_selected_records, get_current_record, open_record, group/parent walks,
#: versions) and file-management verbs (move/trash/duplicate/merge/convert/
#: export) stay on osascript and are NOT exposed here. The agent's DT entry
#: point is a record UUID obtained from a NEXUS search (the indexed corpus),
#: after which it uses these AI / content / bib / capture / link tools and the
#: dt_incorporate composite. (Code review found the selectors had slipped in;
#: removed.)
_DT_TOOLS = (
    get_record_text, get_record_annotation, find_similar_records, classify_record,
    extract_record_content, extract_record_highlights, extract_record_mentions,
    resolve_doi_metadata, search_crossref, resolve_google_books_metadata,
    capture_web_page, download_pdf_from_doi, import_file, get_databases,
    get_record_links, dt_incorporate,
)


def build_server(*, available: bool) -> FastMCP:
    """Construct the FastMCP server, registering tools per the DT-availability gate.

    ``available=False`` -> only ``devonthink_status`` (the harmless always-present
    stub). ``available=True`` -> ``devonthink_status`` + the full curated surface
    (:data:`_DT_TOOLS`). This factory is the load-bearing optionality mechanism;
    the registered tool count is asserted by the Phase-3 alwaysLoad-independence
    test, independent of any ``.mcp.json`` value.
    """
    global _STARTED_WITH_DT
    _STARTED_WITH_DT = available
    mcp = FastMCP("nexus-devonthink")
    mcp.add_tool(devonthink_status)
    if available:
        for fn in _DT_TOOLS:
            mcp.add_tool(fn)
    return mcp


def main() -> None:
    """Run the DEVONthink agent-surface MCP server on stdio (RDR-139 Layer A')."""
    import os  # noqa: PLC0415

    import structlog  # noqa: PLC0415

    from nexus.logging_setup import configure_logging  # noqa: PLC0415
    from nexus.mcp._first_run import ensure_installed_and_running  # noqa: PLC0415
    from nexus.mcp_client.devonthink import available  # noqa: PLC0415

    configure_logging("mcp")
    log = structlog.get_logger("nexus.mcp.devonthink")
    # Probe the gate in main() — no running loop here, so the sync available()
    # (asyncio.run-backed) is safe; the result decides the advertised surface.
    dt_available = available()
    log.info(
        "mcp_server_starting",
        server="nx-mcp-devonthink",
        transport="stdio",
        devonthink_available=dt_available,
        pid=os.getpid(),
        ppid=os.getppid(),
    )
    ensure_installed_and_running()
    mcp = build_server(available=dt_available)
    try:
        mcp.run(transport="stdio")
    except (KeyboardInterrupt, SystemExit):
        log.info("mcp_server_stopping", server="nx-mcp-devonthink", reason="signal")
        raise
    except BaseException as exc:
        log.exception(
            "mcp_server_crashed",
            server="nx-mcp-devonthink",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        log.info("mcp_server_stopping", server="nx-mcp-devonthink", reason="exit")


if __name__ == "__main__":
    main()
