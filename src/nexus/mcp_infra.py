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

from nexus.config import default_db_path

# ── Lazy singletons ──────────────────────────────────────────────────────────

_t1_instance = None
_t1_isolated = False
_t1_lock = threading.Lock()

_t3_instance = None
_t3_lock = threading.Lock()

_collections_cache: tuple[list[str], float] = ([], 0.0)
_COLLECTIONS_CACHE_TTL = 60.0

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
    """Return (T1Database, is_isolated), lazy init on first call.

    Post-RDR-105 P4 the FastMCP lifespan owns chroma's lifecycle in
    full (spawn, addr-file publish, ``_t1_state.T1_ADDR``, cleanup);
    no get-t1-side init is needed. By the time any MCP tool fires,
    Claude Code has run the SessionStart hook AND the lifespan has
    completed its `__aenter__`, so the addr file (or env vars) are
    already in place for ``T1Database()`` to read. ``T1Database``'s
    four-branch fail-loud gate raises ``T1ServerNotFoundError`` if
    the lifespan did not run for any reason, which surfaces a clear
    error rather than silently degrading.
    """
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
#: SQLite file mtime captured at the most recent populate. Used by the
#: mtime-guarded refresh in :func:`get_t1_plan_cache` to detect plan
#: library mutation (a re-seeded builtin, an added or deleted row)
#: without requiring an MCP-server restart. nexus-qgjr.
_plan_cache_mtime: float = 0.0


def _plan_library_mtime(library) -> float:
    """Return the SQLite file mtime for *library*, or 0.0 when unknown.

    Falls back to 0.0 when the library does not expose a ``path``
    attribute (in-memory or test-stub libraries) or when the file is
    missing — both produce a stable repopulate-never-runs fallback that
    matches the legacy single-populate contract.
    """
    path = getattr(library, "path", None)
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def get_t1_plan_cache(*, populate_from=None):
    """Return the T1 ``plans__session`` cache, lazy-populated on first call.

    When *populate_from* (a PlanLibrary) is supplied, the cache is
    populated from its rows on first call and **repopulated whenever
    the underlying SQLite file mtime advances** (nexus-qgjr). The mtime
    check mirrors the catalog's ``_last_consistency_mtime`` pattern at
    ``catalog.py:405`` — cheap when nothing changed, rebuilds when a
    write moves the file's stat-time.

    Libraries without a ``path`` attribute fall back to populate-once
    semantics; the mtime tier costs them nothing.

    Returns ``None`` when no T1 client is reachable — the matcher falls
    back to FTS5 in that case. Subsequent calls after an init failure
    return ``None`` immediately without re-entering the lock (see
    sentinel above).
    """
    global _plan_cache_instance, _plan_cache_populated, _plan_cache_mtime
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
    if populate_from is not None:
        current_mtime = _plan_library_mtime(populate_from)
        with _plan_cache_lock:
            stale = (
                not _plan_cache_populated
                or (current_mtime > 0.0 and current_mtime > _plan_cache_mtime)
            )
            if stale:
                try:
                    _plan_cache_instance.populate(populate_from)
                finally:
                    _plan_cache_populated = True
                    _plan_cache_mtime = current_mtime
    return _plan_cache_instance


def reset_plan_cache_for_tests() -> None:
    """Test helper: drop the cache singleton so the next call re-init."""
    global _plan_cache_instance, _plan_cache_populated, _plan_cache_mtime
    with _plan_cache_lock:
        _plan_cache_instance = None
        _plan_cache_populated = False
        _plan_cache_mtime = 0.0


# ── Catalog management ────────────────────────────────────────────────────────


def get_catalog():
    """Return a fresh Catalog or None when not initialised.

    No process-level caching; each call constructs a Catalog at
    ``catalog_path()``. ``Catalog.__init__`` runs ``_ensure_consistent``
    so cross-process JSONL refreshes are picked up automatically.
    Callers that issue many lookups in tight loops should construct
    once and pass the result down (top-down DI) — see also
    ``commands/catalog.py:_get_catalog`` for the established pattern.

    Tests that need a specific Catalog instance under test set
    ``NEXUS_CATALOG_PATH`` (which ``catalog_path()`` reads) so this
    function constructs at the test's location; the autouse
    ``_isolate_catalog`` fixture in ``tests/conftest.py`` provides
    a per-test default.
    """
    from nexus.catalog import Catalog
    from nexus.config import catalog_path
    path = catalog_path()
    if not Catalog.is_initialized(path):
        return None
    return Catalog(path, path / ".catalog.db")


def require_catalog():
    """Return (catalog, None) or (None, error_message)."""
    cat = get_catalog()
    if cat is None:
        return None, "Catalog not initialized — run 'nx catalog setup' to create and populate it"
    return cat, None


def catalog_auto_link(doc_id: str) -> int:
    """Create catalog links from T1 link-context to the just-stored document.

    Returns the number of links actually created (backward-compat int).
    Skip counts are surfaced via structlog: WARNING for invalid tumbler
    skips (recipe-compliance gap), DEBUG for missing endpoint skips
    (legitimate cleanup signal). nexus-a414 made these visible after
    the prior all-DEBUG behaviour silently swallowed every recipe-
    compliant call that produced zero links.
    """
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
    result = auto_link(cat, entry.tumbler, contexts)

    # nexus-a414: surface non-zero outcomes so operators see what's happening.
    # The all-zero case (no contexts) is already gated above. The interesting
    # case is contexts present + zero created — that's the silent failure mode
    # the bead exists for.
    if result.created or result.skipped_invalid_tumbler or result.skipped_missing_endpoint:
        recipe_compliant_zero = (
            result.created == 0
            and result.skipped_invalid_tumbler > 0
        )
        log_method = _log.warning if recipe_compliant_zero else _log.info
        log_method(
            "auto_link_summary",
            doc_id=doc_id,
            created=result.created,
            skipped_invalid_tumbler=result.skipped_invalid_tumbler,
            skipped_missing_endpoint=result.skipped_missing_endpoint,
            recipe_compliant_zero=recipe_compliant_zero,
        )
    return result.created


def resolve_tumbler_mcp(cat, value):
    """Resolve tumbler string OR title/filename. Returns (tumbler, None) or (None, error)."""
    from nexus.catalog import resolve_tumbler
    return resolve_tumbler(cat, value)


# ── Default post-store hook consumers ────────────────────────────────────────
# Pure functions used as default registrations on every HookRegistry the
# CLI / MCP entry points build. The HookRegistry class + its three
# dispatchers (single / batch / document) live in nexus.hook_registry; the
# install_default_hooks(registry) factory there wires the consumers below
# onto every freshly constructed registry. Registration order within the
# batch chain is load-bearing: chash dual-write must precede taxonomy
# assignment so chash rows exist before topic assignment runs (mirrors
# the legacy chash-before-taxonomy invariant).


def taxonomy_assign_batch_hook(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None,
    metadatas: list[dict] | None,
    *,
    catalog_doc_id: str = "",
) -> None:
    """Registered batch hook: assign indexed docs to their nearest topics.

    Called via ``HookRegistry.fire_batch`` from every storage event:
    CLI bulk ingest passes the embeddings already computed by the upsert;
    MCP ``store_put`` passes ``embeddings=None`` and the hook fetches them
    from T3 inline (one ChromaDB get per doc). The fetch falls back to
    local MiniLM embedding when T3 is unavailable, matching the legacy
    single-doc shim behaviour.

    Reads ``embeddings`` and ``contents``; ignores ``metadatas``.
    No-op when centroids don't exist (no discover run yet) or the
    collection is excluded.

    Wired by :func:`nexus.hook_registry.install_default_hooks` onto every
    runtime-constructed registry.
    """
    from fnmatch import fnmatch

    from nexus.config import is_local_mode, load_config

    if not doc_ids:
        return

    if is_local_mode():
        exclude = load_config().get("taxonomy", {}).get("local_exclude_collections", [])
        if any(fnmatch(collection, pat) for pat in exclude):
            return

    if not embeddings:
        embeddings = _fetch_or_embed(doc_ids, collection, contents)
        if not embeddings:
            return

    try:
        with t2_ctx() as db:
            chroma_client = get_t3()._client
            # Same-collection assignment
            db.taxonomy.assign_batch(
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
    except Exception:
        import structlog
        structlog.get_logger().debug("taxonomy_assign_batch_failed", exc_info=True)


def _fetch_or_embed(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
) -> list[list[float]] | None:
    """Fetch existing T3 embeddings for *doc_ids* in *collection*.

    Falls back to local MiniLM embedding of *contents* when T3 is
    unavailable or returns no embedding for a given id. Returns None
    if no embeddings can be produced (callers no-op in that case).
    Used by ``taxonomy_assign_batch_hook`` when called from MCP
    ``store_put`` with ``embeddings=None``.
    """
    import numpy as np

    fetched: list[list[float] | None] = [None] * len(doc_ids)
    try:
        chroma_client = get_t3()._client
        coll = chroma_client.get_collection(collection, embedding_function=None)
        result = coll.get(ids=doc_ids, include=["embeddings"])
        result_ids = result.get("ids", [])
        result_embs = result.get("embeddings")
        if result_embs is not None:
            id_index = {d: i for i, d in enumerate(doc_ids)}
            for j, rid in enumerate(result_ids):
                idx = id_index.get(rid)
                if idx is None:
                    continue
                emb = result_embs[j]
                if emb is not None and len(emb) > 0:
                    fetched[idx] = list(emb)
    except Exception:
        import structlog
        structlog.get_logger().debug(
            "taxonomy_t3_embedding_fetch_failed",
            collection=collection,
            exc_info=True,
        )

    missing = [i for i, e in enumerate(fetched) if e is None]
    if missing and contents:
        try:
            from nexus.db.local_ef import LocalEmbeddingFunction
            ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
            local_inputs = [contents[i] for i in missing if i < len(contents)]
            local_embs = ef(local_inputs)
            for k, i in enumerate(missing):
                if i < len(contents) and k < len(local_embs):
                    fetched[i] = list(np.array(local_embs[k], dtype=np.float32))
        except Exception:
            import structlog
            structlog.get_logger().debug(
                "taxonomy_local_embed_fallback_failed", exc_info=True,
            )

    if any(e is None for e in fetched):
        return None
    return [e for e in fetched if e is not None]


# ── Chash dual-write (RDR-086 Phase 1.2; migrated to batch hook in RDR-095) ──


def chash_dual_write_batch_hook(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None,
    metadatas: list[dict] | None,
    *,
    catalog_doc_id: str = "",
) -> None:
    """Registered batch hook (RDR-095): best-effort dual-write of
    ``chash_index`` rows after a T3 upsert.

    Called via ``HookRegistry.fire_batch`` from every CLI indexing
    path immediately after ``t3.upsert_chunks_with_embeddings(...)``.
    Reads ``metadatas``; ignores ``contents`` and ``embeddings``. Opens a
    fresh T2Database (matching ``taxonomy_assign_batch_hook``'s
    lifecycle), delegates to the store-level
    ``dual_write_chash_index`` helper, and closes. Logs at debug level
    on any outer failure: a T2 failure must never abort the enclosing
    T3 write path.

    Wired by :func:`nexus.hook_registry.install_default_hooks` onto every
    runtime-constructed registry.
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


def manifest_write_batch_hook(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None,
    metadatas: list[dict] | None,
    *,
    catalog_doc_id: str = "",
) -> None:
    """Registered batch hook (nexus-572g OBS-3): UPSERT document_chunks
    manifest rows after every T3 upsert so the catalog manifest stays
    current without manual backfill.

    Calls ``Catalog.append_manifest_chunks`` (UPSERT keyed on
    ``(doc_id, position)``) once per batch. Multi-batch indexing paths
    (streaming PDF pipeline, doc_indexer incremental loop, anything
    that splits a document across multiple ``HookRegistry.fire_batch``
    calls for the same ``catalog_doc_id``) accumulate the manifest
    correctly across batches because UPSERT on the primary key does not
    DELETE prior rows. Re-indexing with fewer chunks than before may
    leave orphan rows at higher positions; the per-document hook
    (fired once at the tail of every indexing call) is responsible for
    final cleanup and can call ``write_manifest`` to replace.
    Best-effort: any failure is logged at debug level and never
    propagates to the caller.

    Reads ``metadatas`` (``chunk_text_hash``, ``line_start``,
    ``line_end``, ``chunk_start_char``, ``chunk_end_char``); ignores
    ``contents`` and ``embeddings``.

    *catalog_doc_id* (RDR-108 Phase 3) — the catalog ``Document.tumbler``
    string for the batch's document. Phase 3 retired ``doc_id`` from
    chunk metadata; the hook now reads it from the outer call context.
    For pre-Phase-3 chunks (still re-fired during legacy reindexes) the
    field may also appear in ``meta.doc_id`` — that legacy fallback path
    preserves correctness during the transition.
    ``int(m.get("chunk_index", i))`` similarly bridges legacy chunks
    (which carry chunk_index in metadata) and Phase 3 chunks (which use
    enumeration index within the batch). The per-batch enumeration is
    safe under multi-batch streaming because callers passing a
    ``chunk_index`` in chunk metadata get global positions; Phase-3
    streaming chunks fall back to local positions which are still
    monotone within a batch — Phase 4 retargeting will pass per-call
    chunk_positions explicitly.
    """
    if not metadatas:
        return
    from collections import defaultdict

    by_doc: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for i, meta in enumerate(metadatas):
        doc_id = catalog_doc_id or meta.get("doc_id", "")
        if doc_id:
            by_doc[doc_id].append((i, meta))
    if not by_doc:
        return
    try:
        cat = get_catalog()
    except Exception:
        import structlog
        structlog.get_logger().debug("manifest_write_hook_no_catalog", exc_info=True)
        return
    if cat is None:
        return
    for doc_id, indexed_metas in by_doc.items():
        chunks = [
            {
                "chash": m.get("chunk_text_hash", ""),
                "position": int(m.get("chunk_index", i)),
                "chunk_index": m.get("chunk_index"),
                "line_start": m.get("line_start") or None,
                "line_end": m.get("line_end") or None,
                "char_start": m.get("chunk_start_char") or None,
                "char_end": m.get("chunk_end_char") or None,
            }
            for i, m in indexed_metas
        ]
        if all(not c["chash"] for c in chunks):
            continue
        try:
            # nexus-zq79 F3 / nexus-lrhg #1: shrink-reindex orphan cleanup.
            # UPSERT keyed on (doc_id, position) leaves orphan rows when a
            # file re-indexes with fewer chunks than before. When the batch
            # contains position 0 (the start of a file's chunks), wrap the
            # DELETE + INSERT + chunk_count UPDATE in one transaction via
            # ``atomic_manifest_replace`` so a partial-failure crash
            # between the purge and the new write cannot leave the catalog
            # with zero chunks for a doc the documents row still claims N.
            # Multi-batch writes never include position 0 in batches other
            # than the first, so the atomic-replace path is safe for the
            # streaming PDF / doc_indexer paths.
            if any(c["position"] == 0 for c in chunks):
                cat.atomic_manifest_replace(doc_id, chunks)
            else:
                cat.append_manifest_chunks(doc_id, chunks)
                # nexus-zq79: documents.chunk_count is a denormalised cache of
                # COUNT(*) document_chunks. The catalog-register hook runs BEFORE
                # per-file indexing (tumbler injection requires it), so chunk_count
                # is initialised to 0; nothing else updates it for code/prose
                # indexers post-Phase-3. Routed via Catalog public API to satisfy
                # the projector-only-writes invariant (RDR-101 Phase 3 ε).
                cat.resync_chunk_count_cache(doc_id)
        except Exception:
            # Post-Phase-3 the manifest hook is load-bearing: a failure
            # leaves the catalog manifest empty and chunk_count=0 for
            # this doc (silent data-correctness bug). The contract still
            # requires non-propagation (best-effort hook) but the log
            # severity is WARNING so failures are discoverable in
            # production log streams without DEBUG enabled. nexus-zq79.
            import structlog
            structlog.get_logger().warning(
                "manifest_write_hook_failed", doc_id=doc_id, exc_info=True
            )


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

    Post-store hook chains are owned by per-invocation ``HookRegistry``
    instances (see ``nexus.hook_registry``); they are no longer
    module-globals on ``mcp_infra`` and therefore not cleared here.
    """
    global _t1_instance, _t1_isolated, _t3_instance, _collections_cache
    _t1_instance = None
    _t1_isolated = False
    _t3_instance = None
    _collections_cache = ([], 0.0)
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
