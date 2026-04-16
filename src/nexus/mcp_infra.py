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


# ── T1 plan session cache (RDR-078) ──────────────────────────────────────────
# Singleton wrapper around PlanSessionCache; shared across MCP tool calls in
# one process.  reset_plan_cache_for_tests() tears it down between test cases.
#
# Three states:
#   None                      — not yet initialised, eligible for init attempt
#   _PLAN_CACHE_UNAVAILABLE   — init failed; callers short-circuit to FTS5
#                               without taking the lock on every call
#   PlanSessionCache instance — healthy, available for matching

_PLAN_CACHE_UNAVAILABLE = object()  # sentinel — distinct from None

_plan_cache_instance = None
_plan_cache_lock = threading.Lock()
_plan_cache_populated: bool = False


def get_t1_plan_cache(*, populate_from=None):
    """Return the T1 ``plans__session`` cache, lazy-populated on first call.

    When *populate_from* (a PlanLibrary) is supplied and the cache has not yet
    been populated this process, the full active plan set is loaded. Subsequent
    calls short-circuit — the ``plan_save`` MCP hook keeps the cache fresh.

    Returns ``None`` when no T1 client is reachable — the matcher falls back
    to FTS5 in that case.  Subsequent calls after an init failure return
    ``None`` immediately without re-entering the lock (see sentinel above).
    """
    global _plan_cache_instance, _plan_cache_populated
    if _plan_cache_instance is _PLAN_CACHE_UNAVAILABLE:
        return None
    if _plan_cache_instance is None:
        with _plan_cache_lock:
            if _plan_cache_instance is None:
                try:
                    t1, _ = get_t1()
                    from nexus.plans.session_cache import PlanSessionCache
                    _plan_cache_instance = PlanSessionCache(
                        client=t1._client, session_id=t1.session_id,
                    )
                except Exception:
                    _plan_cache_instance = _PLAN_CACHE_UNAVAILABLE
    if _plan_cache_instance is _PLAN_CACHE_UNAVAILABLE:
        return None
    if (
        populate_from is not None
        and not _plan_cache_populated
    ):
        with _plan_cache_lock:
            if not _plan_cache_populated:
                try:
                    _plan_cache_instance.populate(populate_from)
                finally:
                    _plan_cache_populated = True
    return _plan_cache_instance


def reset_plan_cache_for_tests() -> None:
    """Test helper: drop the cache singleton so the next call re-init."""
    global _plan_cache_instance, _plan_cache_populated
    with _plan_cache_lock:
        _plan_cache_instance = None
        _plan_cache_populated = False


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
        import structlog
        structlog.get_logger().debug("taxonomy_t3_embedding_fetch_failed", doc_id=doc_id, exc_info=True)

    if embedding is None:
        from nexus.db.local_ef import LocalEmbeddingFunction

        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        embedding = np.array(ef([content])[0], dtype=np.float32)

    # Same-collection assignment (existing behavior).
    same = taxonomy.assign_single(collection, embedding, chroma_client)
    if same is not None:
        taxonomy.assign_topic(doc_id, same.topic_id, assigned_by="centroid")

    # Cross-collection projection (RDR-075 SC-4, SC-6 + RDR-077 RF-3/RF-5).
    cross = taxonomy.assign_single(
        collection, embedding, chroma_client, cross_collection=True,
    )
    if cross is not None and (same is None or cross.topic_id != same.topic_id):
        taxonomy.assign_topic(
            doc_id,
            cross.topic_id,
            assigned_by="projection",
            similarity=cross.similarity,
            source_collection=collection,
        )


def taxonomy_assign_batch(
    doc_ids: list[str],
    collection: str,
    embeddings: list[list[float]],
) -> int:
    """Assign a batch of indexed docs to their nearest topics.

    Called by CLI indexing paths after chunk upsert.  Uses existing T3
    embeddings (already in *embeddings*) — no re-fetch or re-embed needed.

    Returns the number of docs assigned, or 0 when centroids don't exist
    (no discover run yet) or the collection is excluded.
    """
    from fnmatch import fnmatch

    from nexus.config import is_local_mode, load_config

    if not doc_ids or not embeddings:
        return 0

    if is_local_mode():
        exclude = load_config().get("taxonomy", {}).get("local_exclude_collections", [])
        if any(fnmatch(collection, pat) for pat in exclude):
            return 0

    try:
        with t2_ctx() as db:
            chroma_client = get_t3()._client
            # Same-collection assignment
            assigned = db.taxonomy.assign_batch(
                collection, doc_ids, embeddings, chroma_client,
            )
            # Cross-collection projection (RDR-075 SC-6)
            cross_assigned = db.taxonomy.assign_batch(
                collection, doc_ids, embeddings, chroma_client,
                cross_collection=True,
            )
            if cross_assigned:
                import structlog
                structlog.get_logger().debug(
                    "taxonomy_cross_collection_batch",
                    collection=collection,
                    cross_assigned=cross_assigned,
                )
            return assigned
    except Exception:
        import structlog
        structlog.get_logger().debug("taxonomy_assign_batch_failed", exc_info=True)
        return 0


# ── Version compatibility check (RDR-076) ─────────────────────────────────────


def check_version_compatibility() -> None:
    """Synchronous check: warn if CLI version diverges from stored version.

    Called from MCP ``main()`` before ``mcp.run()``.  Never blocks startup.
    """
    import sqlite3

    import structlog

    log = structlog.get_logger()
    try:
        from importlib.metadata import version as _pkg_version

        cli_ver = _pkg_version("conexus")
        db_path = default_db_path()
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT value FROM _nexus_version WHERE key='cli_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return  # table doesn't exist yet
        finally:
            conn.close()
        if row is None:
            return
        stored_ver = row[0]
        from nexus.db.migrations import _parse_version

        cli_t = _parse_version(cli_ver)
        stored_t = _parse_version(stored_ver)
        # Warn on minor or major divergence, not patch.
        # Tuple slicing is safe for short tuples — (4,)[:2] == (4,).
        if cli_t[:2] != stored_t[:2]:
            if cli_t > stored_t:
                hint = "run 'nx upgrade' to apply pending migrations"
            else:
                hint = "DB was upgraded by a newer CLI version"
            log.warning(
                "version_mismatch",
                cli_version=cli_ver,
                stored_version=stored_ver,
                hint=hint,
            )
    except Exception:
        pass  # never block MCP startup


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
