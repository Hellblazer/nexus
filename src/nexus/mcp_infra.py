# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP server infrastructure: singletons, caching, test injection.

Separated from tool definitions (mcp_server.py) to isolate concerns.
"""
from __future__ import annotations

import threading
import time
import warnings

from mcp.server.fastmcp import FastMCP

from nexus.commands._helpers import default_db_path

mcp = FastMCP("nexus")

# ── Lazy singletons ──────────────────────────────────────────────────────────

_t1_instance = None
_t1_isolated = False
_t1_lock = threading.Lock()

_t3_instance = None
_t3_lock = threading.Lock()

_collections_cache: tuple[list[str], float] = ([], 0.0)
_COLLECTIONS_CACHE_TTL = 60.0

_catalog_instance = None
_catalog_lock = threading.Lock()
_catalog_mtime: float = 0.0


def get_t1():
    """Return (T1Database, is_isolated) — lazy init on first call."""
    global _t1_instance, _t1_isolated
    if _t1_instance is None:
        with _t1_lock:
            if _t1_instance is None:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    from nexus.db.t1 import T1Database
                    _t1_instance = T1Database()
                    _t1_isolated = any("EphemeralClient" in str(w.message) for w in caught)
    return _t1_instance, _t1_isolated


def get_t3():
    """Return T3Database singleton — lazy init on first call."""
    global _t3_instance
    if _t3_instance is None:
        with _t3_lock:
            if _t3_instance is None:
                from nexus.db import make_t3
                _t3_instance = make_t3()
    return _t3_instance


def get_collection_names() -> list[str]:
    """Return cached T3 collection names, refreshing every _COLLECTIONS_CACHE_TTL seconds."""
    global _collections_cache
    names, ts = _collections_cache
    now = time.monotonic()
    if now - ts > _COLLECTIONS_CACHE_TTL:
        new_names = [c["name"] for c in get_t3().list_collections()]
        _collections_cache = (new_names, now)
        return new_names
    return names


def t2_ctx():
    """Return a T2Database context manager — fresh per call."""
    from nexus.db.t2 import T2Database
    return T2Database(default_db_path())


# ── Catalog management ────────────────────────────────────────────────────────


def _max_jsonl_mtime(cat) -> float:
    """Return max mtime across all three JSONL files."""
    mtime = 0.0
    for path in cat.jsonl_paths():
        try:
            mtime = max(mtime, path.stat().st_mtime) if path.exists() else mtime
        except OSError:
            pass
    return mtime


def get_catalog():
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
        try:
            current_mtime = _max_jsonl_mtime(_catalog_instance)
            if current_mtime > _catalog_mtime:
                with _catalog_lock:
                    if current_mtime > _catalog_mtime:
                        _catalog_mtime = current_mtime
                        _catalog_instance._ensure_consistent()
        except OSError:
            pass
    return _catalog_instance


def require_catalog():
    """Return (catalog, None) or (None, error_message)."""
    cat = get_catalog()
    if cat is None:
        return None, "Catalog not initialized — run 'nx catalog setup' to create and populate it"
    return cat, None


def catalog_auto_link(doc_id: str) -> int:
    """Create catalog links from T1 link-context to the just-stored document."""
    import structlog
    _log = structlog.get_logger()

    cat = get_catalog()
    if cat is None:
        return 0
    t1, _ = get_t1()
    entries = t1.list_entries()
    link_entries = [
        e for e in entries
        if "link-context" in {t.strip() for t in (e.get("tags") or "").split(",")}
    ]
    if not link_entries:
        return 0
    entry = cat.by_doc_id(doc_id)
    if entry is None:
        _log.debug("auto_link_skip_doc_not_in_catalog", doc_id=doc_id)
        return 0
    from nexus.catalog.auto_linker import auto_link, read_link_contexts
    contexts = read_link_contexts(link_entries)
    return auto_link(cat, entry.tumbler, contexts)


def resolve_tumbler_mcp(cat, value):
    """Resolve tumbler string OR title/filename. Returns (tumbler, None) or (None, error)."""
    from nexus.catalog import resolve_tumbler
    return resolve_tumbler(cat, value)


# ── Test injection ────────────────────────────────────────────────────────────


def reset_singletons():
    """Reset lazy singletons (for tests only)."""
    global _t1_instance, _t1_isolated, _t3_instance, _collections_cache, _catalog_instance, _catalog_mtime
    _t1_instance = None
    _t1_isolated = False
    _t3_instance = None
    _collections_cache = ([], 0.0)
    _catalog_instance = None
    _catalog_mtime = 0.0


def inject_t1(t1, *, isolated: bool = False):
    """Inject a T1Database for testing."""
    global _t1_instance, _t1_isolated
    _t1_instance = t1
    _t1_isolated = isolated


def inject_t3(t3):
    """Inject a T3Database for testing."""
    global _t3_instance
    _t3_instance = t3


def inject_catalog(cat):
    """Inject a Catalog for testing."""
    global _catalog_instance
    _catalog_instance = cat
