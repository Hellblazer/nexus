# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP server infrastructure: singletons, caching, test injection.

Separated from tool definitions (mcp_server.py) to isolate concerns.
"""
from __future__ import annotations

import os
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
    """Invoke all registered post-store hooks. Exceptions logged, never raised.

    Failures are additionally persisted to T2 ``hook_failures`` (GH #251) so
    ``nx taxonomy status`` can surface them with an Action line. The persist
    path is itself guarded — if the T2 write fails, the store_put still
    succeeds and structlog still carries the original warning.
    """
    import structlog
    _hook_log = structlog.get_logger()
    for hook in _post_store_hooks:
        try:
            hook(doc_id, collection, content)
        except Exception as exc:
            hook_name = getattr(hook, "__name__", "?")
            _hook_log.warning(
                "post_store_hook_failed",
                hook=hook_name,
                exc_info=True,
            )
            _record_hook_failure(
                doc_id=doc_id,
                collection=collection,
                hook_name=hook_name,
                error=str(exc),
            )


def _record_hook_failure(
    *,
    doc_id: str,
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Persist a post-store hook failure to T2 ``hook_failures`` (GH #251).

    Secondary best-effort path: if the T2 write itself fails, swallow the
    second failure (the primary warning already reached structlog) rather
    than mask the original hook exception.
    """
    try:
        with t2_ctx() as t2:
            # ``hook_failures`` is cross-cutting — any domain's connection
            # points at the same SQLite file. Use the taxonomy conn because
            # the status line that surfaces these rows already reads there.
            conn = t2.taxonomy.conn
            with t2.taxonomy._lock:
                conn.execute(
                    "INSERT INTO hook_failures "
                    "(doc_id, collection, hook_name, error) VALUES (?, ?, ?, ?)",
                    (doc_id, collection, hook_name, error[:2000]),
                )
                conn.commit()
    except Exception:
        import structlog
        structlog.get_logger().debug(
            "hook_failure_persist_failed",
            hook=hook_name,
            collection=collection,
            exc_info=True,
        )


# ── Post-store batch hooks (RDR-095, nexus-wxcb) ─────────────────────────────
# Parallel batch-shape contract for bulk-ingest enrichment. Single-document
# hooks above fire from MCP store_put; batch hooks fire from CLI indexing
# paths where N docs land in one operation and consumers benefit from
# batched dependency calls (e.g. one ChromaDB query for N centroids vs N
# sequential queries). Same per-hook failure-isolation pattern: exceptions
# captured, persisted to T2 hook_failures, never propagated.

_post_store_batch_hooks: list = []


def register_post_store_batch_hook(fn) -> None:
    """Register a callable(doc_ids, collection, contents, embeddings, metadatas)
    to fire after batch CLI ingest."""
    _post_store_batch_hooks.append(fn)


def fire_post_store_batch_hooks(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None = None,
    metadatas: list[dict] | None = None,
) -> None:
    """Invoke all registered batch hooks. Per-hook failures captured and
    persisted to T2 hook_failures, never raised.

    Empty doc_ids returns early — no hooks fire on empty batches.

    Different consumers read different payload fields:
    chash_dual_write_batch_hook reads metadatas, taxonomy_assign_batch_hook
    reads embeddings. Hooks ignore parameters they don't need.
    """
    if not doc_ids:
        return
    import structlog
    _hook_log = structlog.get_logger()
    for hook in _post_store_batch_hooks:
        try:
            hook(doc_ids, collection, contents, embeddings, metadatas)
        except Exception as exc:
            hook_name = getattr(hook, "__name__", "?")
            _hook_log.warning(
                "post_store_batch_hook_failed",
                hook=hook_name,
                exc_info=True,
            )
            _record_batch_hook_failure(
                doc_ids=doc_ids,
                collection=collection,
                hook_name=hook_name,
                error=str(exc),
            )


def _record_batch_hook_failure(
    *,
    doc_ids: list[str],
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Persist a batch-shape post-store hook failure to T2 ``hook_failures``.

    Writes the JSON-encoded doc_id list to ``batch_doc_ids`` and sets
    ``is_batch=1``; stores a representative scalar (first doc_id) in the
    legacy ``doc_id`` column so existing scalar readers continue to render
    something meaningful (RDR-095 schema migration adds the two new columns
    in 4.14.1).

    Falls back to scalar-only insert if the new columns don't exist yet
    (P1.1 merged before P1.2 migration ran). Same secondary best-effort
    semantics as ``_record_hook_failure``: persistence failure cannot
    break ingest.
    """
    import json
    import sqlite3
    representative = doc_ids[0] if doc_ids else ""
    payload = json.dumps(doc_ids)
    truncated = error[:2000]
    try:
        with t2_ctx() as t2:
            conn = t2.taxonomy.conn
            with t2.taxonomy._lock:
                try:
                    conn.execute(
                        "INSERT INTO hook_failures "
                        "(doc_id, collection, hook_name, error, "
                        " batch_doc_ids, is_batch) VALUES (?, ?, ?, ?, ?, 1)",
                        (representative, collection, hook_name, truncated, payload),
                    )
                except sqlite3.OperationalError:
                    # Pre-4.14.1 schema: new columns not yet migrated.
                    conn.execute(
                        "INSERT INTO hook_failures "
                        "(doc_id, collection, hook_name, error) VALUES (?, ?, ?, ?)",
                        (representative, collection, hook_name, truncated),
                    )
                conn.commit()
    except Exception:
        import structlog
        structlog.get_logger().debug(
            "batch_hook_failure_persist_failed",
            hook=hook_name,
            collection=collection,
            exc_info=True,
        )


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


# ── Chash dual-write (RDR-086 Phase 1.2) ──────────────────────────────────────


def chash_dual_write_batch(
    doc_ids: list[str],
    collection: str,
    metadatas: list[dict],
) -> None:
    """Best-effort dual-write of ``chash_index`` rows after a T3 upsert.

    Called from each of the six indexing write sites immediately after
    ``t3.upsert_chunks_with_embeddings(...)``. Opens a fresh T2Database
    (matching ``taxonomy_assign_batch``'s lifecycle), delegates to the
    store-level ``dual_write_chash_index`` helper, and closes. Logs at
    debug level on any outer failure — a T2 failure must never abort
    the enclosing T3 write path.
    """
    if not doc_ids or not metadatas:
        return
    try:
        from nexus.db.t2.chash_index import dual_write_chash_index

        with t2_ctx() as db:
            dual_write_chash_index(db.chash_index, collection, doc_ids, metadatas)
    except Exception:
        import structlog
        structlog.get_logger().debug("chash_dual_write_batch_failed", exc_info=True)


# ── Version compatibility check (RDR-076) ─────────────────────────────────────


def check_version_compatibility() -> None:
    """Synchronous startup check for two version-drift cases.

    Called from each MCP server's ``main()`` before ``mcp.run()`` — the
    natural single binding point between plugin and CLI (the MCP server
    binaries ``nx-mcp`` / ``nx-mcp-catalog`` are conexus entry points;
    plugin/CLI coupling runs entirely through this surface).

    Two warnings, both non-fatal:

    1. **CLI ↔ T2 schema drift** — current ``conexus`` package version
       differs (minor or major) from ``_nexus_version.cli_version``
       stored in T2. Suggests ``nx upgrade``. Catches the case where
       the user upgraded conexus but hasn't run any migration-applying
       command yet.

    2. **Plugin ↔ CLI version drift** — installed Claude Code plugin's
       declared version (read from ``${CLAUDE_PLUGIN_ROOT}/.claude-plugin/
       plugin.json``) differs (minor or major) from the running CLI.
       Suggests ``/plugin update nx@nexus-plugins`` or
       ``uv tool upgrade conexus`` depending on which side is older.
       The plugin and CLI ship from the same repo at the same version
       (CI enforces marketplace.json parity); drift means one update
       command was run without the other.

    Never blocks startup — both checks log warnings only.
    """
    import json
    import sqlite3
    from pathlib import Path

    import structlog

    log = structlog.get_logger()
    try:
        from importlib.metadata import version as _pkg_version
        from nexus.db.migrations import _parse_version

        cli_ver = _pkg_version("conexus")

        # ── (1) CLI ↔ T2 schema drift ────────────────────────────────────
        db_path = default_db_path()
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT value FROM _nexus_version WHERE key='cli_version'"
                ).fetchone()
            except sqlite3.OperationalError:
                row = None  # table doesn't exist yet — fresh install
            finally:
                conn.close()
            if row is not None:
                stored_ver = row[0]
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

        # ── (2) Plugin ↔ CLI version drift ──────────────────────────────
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
        if plugin_root:
            manifest_path = Path(plugin_root) / ".claude-plugin" / "plugin.json"
            try:
                manifest = json.loads(manifest_path.read_text())
                plugin_ver = manifest.get("version")
            except (OSError, json.JSONDecodeError):
                plugin_ver = None
            if plugin_ver:
                cli_t = _parse_version(cli_ver)
                plugin_t = _parse_version(plugin_ver)
                if cli_t[:2] != plugin_t[:2]:
                    # Choose the actionable update for the lagging side.
                    if cli_t > plugin_t:
                        hint = "plugin is older — run '/plugin update nx@nexus-plugins' in Claude Code"
                    else:
                        hint = "CLI is older — run 'uv tool upgrade conexus'"
                    log.warning(
                        "plugin_cli_version_mismatch",
                        cli_version=cli_ver,
                        plugin_version=plugin_ver,
                        hint=hint,
                    )
    except Exception:
        pass  # never block MCP startup


# ── Test injection ────────────────────────────────────────────────────────────


def reset_singletons():
    """Reset lazy singletons (for tests only).

    Search review I-2: also resets the T1 plan-match cache. Previously
    the plan cache survived ``reset_singletons()`` calls — tests that
    injected a fresh T1 but kept the populated plan cache saw stale
    embeddings against the injected client and produced nondeterministic
    matches.

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
    reset_plan_cache_for_tests()


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
