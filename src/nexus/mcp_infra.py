# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP server infrastructure: singletons, caching, test injection.

Separated from tool definitions (mcp_server.py) to isolate concerns.
"""
from __future__ import annotations

import threading
import time
import warnings

from nexus.commands._helpers import default_db_path

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

# ── Search trace cache (RDR-061 E2) ──────────────────────────────────────────
# Session-keyed cache of recent search results. Populated by the search tool,
# consumed by store_put and catalog_link to correlate agent actions with the
# queries that likely led to them.
_search_traces: dict[str, list[dict]] = {}
_search_traces_lock = threading.Lock()
_SEARCH_TRACE_TTL_SECONDS = 600  # 10 minutes
_SEARCH_TRACE_MAX_PER_SESSION = 20


def record_search_trace(
    session_id: str,
    query: str,
    chunks: list[tuple[str, str]],
) -> None:
    """Record a search result set for later correlation (RDR-061 E2).

    chunks: list of (chunk_id, collection) tuples from the search results.
    """
    if not session_id or not chunks:
        return
    trace = {
        "query": query,
        "chunks": chunks,
        "timestamp": time.monotonic(),
    }
    with _search_traces_lock:
        bucket = _search_traces.setdefault(session_id, [])
        bucket.append(trace)
        # Trim old entries (both by age and count)
        now = time.monotonic()
        trimmed = [
            t for t in bucket
            if now - t["timestamp"] < _SEARCH_TRACE_TTL_SECONDS
        ][-_SEARCH_TRACE_MAX_PER_SESSION:]
        if trimmed:
            _search_traces[session_id] = trimmed
        else:
            _search_traces.pop(session_id, None)


def get_recent_search_traces(session_id: str) -> list[dict]:
    """Return non-expired search traces for this session (RDR-061 E2).

    Evicts the session key if all traces have expired.
    """
    if not session_id:
        return []
    with _search_traces_lock:
        bucket = _search_traces.get(session_id, [])
        now = time.monotonic()
        alive = [t for t in bucket if now - t["timestamp"] < _SEARCH_TRACE_TTL_SECONDS]
        if alive:
            if len(alive) != len(bucket):
                _search_traces[session_id] = alive
            return alive
        # All expired — evict the key
        _search_traces.pop(session_id, None)
        return []


def clear_search_traces() -> None:
    """Clear all search traces (test helper)."""
    with _search_traces_lock:
        _search_traces.clear()


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


# ── Post-store hooks (RDR-070, nexus-7h2) ────────────────────────────────────
# Synchronous hooks that fire after every store_put. Exceptions are caught per-
# hook and logged, never propagated — same non-fatal pattern as auto_linker.

_post_store_hooks: list = []


def register_post_store_hook(fn) -> None:
    """Register a callable(doc_id, collection, content) to fire after store_put."""
    _post_store_hooks.append(fn)


def fire_post_store_hooks(doc_id: str, collection: str, content: str) -> None:
    """Invoke all registered post-store hooks. Exceptions logged, never raised."""
    import structlog
    _hook_log = structlog.get_logger()
    for hook in _post_store_hooks:
        try:
            hook(doc_id, collection, content)
        except Exception:
            _hook_log.warning("post_store_hook_failed", hook=getattr(hook, "__name__", "?"), exc_info=True)


def taxonomy_assign_hook(
    doc_id: str,
    collection: str,
    content: str,
    *,
    taxonomy=None,
    chroma_client=None,
) -> None:
    """Assign a newly stored document to its nearest topic via centroid ANN.

    Re-embeds ``content`` with local MiniLM 384d, queries
    ``taxonomy__centroids`` for the nearest cluster, and writes a
    ``topic_assignments`` row with ``assigned_by='centroid'``.

    No-op when centroids don't exist (no discover run yet), or when
    the collection matches ``taxonomy.local_exclude_collections`` and
    running in local mode (MiniLM clusters poorly on code).
    Keyword args ``taxonomy`` and ``chroma_client`` are injection points
    for testing; production path resolves them from singletons.
    """
    from fnmatch import fnmatch

    from nexus.config import is_local_mode, load_config

    if is_local_mode():
        exclude = load_config().get("taxonomy", {}).get("local_exclude_collections", [])
        if any(fnmatch(collection, pat) for pat in exclude):
            return

    if taxonomy is None:
        with t2_ctx() as db:
            taxonomy = db.taxonomy
            _run_taxonomy_assign(doc_id, collection, content, taxonomy, chroma_client)
            return

    _run_taxonomy_assign(doc_id, collection, content, taxonomy, chroma_client)


def _run_taxonomy_assign(doc_id, collection, content, taxonomy, chroma_client):
    """Inner logic for taxonomy_assign_hook (avoids double context-manager).

    Fetches the doc's existing T3 embedding (Voyage on cloud, MiniLM on
    local) rather than re-embedding with MiniLM. Falls back to MiniLM
    if the T3 embedding isn't available (e.g., race condition).
    """
    import numpy as np

    if chroma_client is None:
        chroma_client = get_t3()._client

    # Try to fetch the doc's existing T3 embedding (already stored by store_put)
    embedding = None
    try:
        coll = chroma_client.get_collection(collection, embedding_function=None)
        result = coll.get(ids=[doc_id], include=["embeddings"])
        if result["embeddings"] is not None and len(result["embeddings"]) > 0:
            emb = result["embeddings"][0]
            if emb is not None and len(emb) > 0:
                embedding = np.array(emb, dtype=np.float32)
    except Exception:
        pass  # Fall through to MiniLM

    if embedding is None:
        from nexus.db.local_ef import LocalEmbeddingFunction

        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        embedding = np.array(ef([content])[0], dtype=np.float32)

    topic_id = taxonomy.assign_single(collection, embedding, chroma_client)
    if topic_id is not None:
        taxonomy.assign_topic(doc_id, topic_id, assigned_by="centroid")


# ── Test injection ────────────────────────────────────────────────────────────


def reset_singletons():
    """Reset lazy singletons (for tests only).

    NOTE: _post_store_hooks is intentionally NOT cleared here.  Hooks are
    registered at module-import time (e.g. ``register_post_store_hook`` in
    ``nexus.mcp.core``).  Because Python only executes module-level code
    once, clearing the list here would permanently lose those registrations
    for the remainder of the test session.  Tests that need an empty hook
    list should clear ``_post_store_hooks`` explicitly in their own fixture.
    """
    global _t1_instance, _t1_isolated, _t3_instance, _collections_cache, _catalog_instance, _catalog_mtime
    _t1_instance = None
    _t1_isolated = False
    _t3_instance = None
    _collections_cache = ([], 0.0)
    _catalog_instance = None
    _catalog_mtime = 0.0
    clear_search_traces()


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
