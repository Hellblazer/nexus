# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 SQLite memory bank — six domain stores behind a composing facade.

The T2 tier is split into six domain stores, each owning its own set
of tables in a shared SQLite file:

=========================  ==========================  =================================================================
Attribute                  Class                       Responsibility
=========================  ==========================  =================================================================
``db.memory``              ``MemoryStore``             Persistent notes, FTS5 search, access tracking, heat-weighted TTL
``db.plans``               ``PlanLibrary``             Plan templates, plan search, plan TTL
``db.taxonomy``            ``CatalogTaxonomy``         Topic clustering, topic assignment
``db.telemetry``           ``Telemetry``               Relevance log (query/chunk/action), retention-based expiry
``db.chash_index``         ``ChashIndex``              chash → (collection, doc_id) global lookup (RDR-086)
``db.document_aspects``    ``DocumentAspects``         Per-document structured aspects table (RDR-089)
=========================  ==========================  =================================================================

``T2Database`` is a facade: it constructs the six stores and re-exposes
the memory-domain public methods as thin delegates for backward
compatibility (the chash, taxonomy, and document_aspects domains are
accessed directly via their attributes — no facade delegates exist).
``expire()`` runs the cross-domain sweep that each store registers, and
the context manager / ``close()`` tear the stores down in reverse
construction order. The facade itself holds no database connection.

New code should prefer the domain methods over the facade:

.. code-block:: python

    db = T2Database(path)
    db.memory.search("fts query", project="myproj")   # preferred
    db.search("fts query", project="myproj")          # facade delegate

Concurrency model (RDR-063 Phase 2):

* Each store opens its own ``sqlite3.Connection`` against the shared
  file and guards it with its own ``threading.Lock``. Reads in one
  domain are never blocked by writes in another domain (the Phase 1
  global Python mutex is gone). Concurrent writes across domains
  still serialize at SQLite's single-writer WAL lock — ``busy_timeout``
  absorbs brief contention without raising ``OperationalError``.
* All connections run in WAL mode with a 5-second ``busy_timeout``,
  so cross-domain write coordination happens in SQLite rather than
  Python.
* Telemetry writes from MCP hooks no longer block ``memory.search``.
* ``taxonomy.discover_topics`` holds only ``taxonomy._lock`` for
  INSERTs — never acquires ``memory._lock``.

Schema migrations are per-domain and idempotent: each store runs its
own migration guard the first time it sees a given database path, so
independent stores can initialize concurrently.

See ``docs/architecture.md`` § T2 Domain Stores for the full picture
and ``docs/contributing.md`` § Adding a T2 Domain Feature for how to
extend the tier.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite3

import structlog

# Cheap import only: ``_sanitize_fts5`` is needed by
# ``nexus.catalog.catalog_db`` at module import time, and the
# memory_store module's top-level imports are stdlib + structlog only
# (no sklearn/scipy/numpy). Heavy submodule imports are deferred via
# the module __getattr__ at the bottom of this file.
from nexus.db.t2.memory_store import _sanitize_fts5

if TYPE_CHECKING:
    # Type-only: re-exposed for static type checking. The runtime
    # bindings come from ``__getattr__`` below, which lazy-loads each
    # submodule on first attribute access. The CLI cold-start path
    # (nexus.cli -> nexus.commands.catalog -> nexus.catalog.catalog ->
    # nexus.catalog.catalog_db -> from nexus.db.t2 import _sanitize_fts5)
    # therefore stops here without pulling sklearn -> scipy -> numpy
    # via CatalogTaxonomy.
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy
    from nexus.db.t2.chash_index import ChashIndex
    from nexus.db.t2.memory_store import AccessPolicy, MemoryStore
    from nexus.db.t2.plan_library import PlanLibrary
    from nexus.db.t2.telemetry import Telemetry

_log = structlog.get_logger()

# Re-export surface for backward compatibility. Resolution happens
# lazily through the module-level ``__getattr__`` below; the eager
# imports were pulling sklearn/scipy/numpy through CatalogTaxonomy on
# every CLI invocation that touched any nexus.db.t2 symbol.
__all__ = [
    "AccessPolicy",
    "CatalogTaxonomy",
    "ChashIndex",
    "DocumentAspects",
    "MemoryStore",
    "PlanLibrary",
    "T2Database",
    "Telemetry",
    "_sanitize_fts5",
]


def __getattr__(name: str) -> Any:  # PEP 562
    """Lazy resolver for heavy re-exports.

    Map each public name to its owning submodule and import on demand.
    Only fires the first time a name is accessed (Python caches the
    attribute on the module after a successful resolve).
    """
    _MAP = {
        "AccessPolicy":     "nexus.db.t2.memory_store",
        "MemoryStore":      "nexus.db.t2.memory_store",
        "CatalogTaxonomy":  "nexus.db.t2.catalog_taxonomy",
        "ChashIndex":       "nexus.db.t2.chash_index",
        "DocumentAspects":  "nexus.db.t2.document_aspects",
        "PlanLibrary":      "nexus.db.t2.plan_library",
        "Telemetry":        "nexus.db.t2.telemetry",
    }
    if name in _MAP:
        import importlib
        mod = importlib.import_module(_MAP[name])
        value = getattr(mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── Database facade ───────────────────────────────────────────────────────────


class T2Database:
    """T2 SQLite memory bank with FTS5 full-text search.

    Pure composition over the four domain stores. Each store owns its
    own connection, lock, schema init, and migration guard; the facade
    forwards legacy public methods to the appropriate store and owns
    only the cross-domain ``expire()`` composition and the context
    manager.
    """

    def __init__(self, path: Path) -> None:
        # Lazy-load the four store classes here rather than at module
        # import time so the CLI cold-start path (which only needs
        # ``_sanitize_fts5``) does not pull sklearn/scipy/numpy through
        # CatalogTaxonomy.
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy
        from nexus.db.t2.chash_index import ChashIndex
        from nexus.db.t2.document_aspects import DocumentAspects
        from nexus.db.t2.memory_store import MemoryStore
        from nexus.db.t2.plan_library import PlanLibrary
        from nexus.db.t2.telemetry import Telemetry

        path.parent.mkdir(parents=True, exist_ok=True)

        # ── Transient connection: run pending migrations (RDR-076) ────
        from nexus.db.migrations import _upgrade_done, _upgrade_lock, apply_pending

        try:
            path_key = str(path.resolve())
        except OSError:
            path_key = str(path)

        # Serialise the check-then-migrate to prevent concurrent
        # transient connections racing the WAL write lock.
        with _upgrade_lock:
            if path_key not in _upgrade_done:
                try:
                    from importlib.metadata import version as _pkg_version

                    current_version = _pkg_version("conexus")
                except Exception:
                    current_version = "0.0.0"

                conn = sqlite3.connect(str(path), check_same_thread=False)
                try:
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute("PRAGMA journal_mode=WAL")
                    apply_pending(conn, current_version)
                finally:
                    conn.close()

        # ── Construct domain stores ───────────────────────────────────
        self.memory: MemoryStore = MemoryStore(path)
        self.plans: PlanLibrary = PlanLibrary(path)
        # CatalogTaxonomy takes a MemoryStore reference for the
        # get_topic_docs JOIN (RDR-063 §Cross-Domain Contracts).
        self.taxonomy: CatalogTaxonomy = CatalogTaxonomy(path, self.memory)
        self.telemetry: Telemetry = Telemetry(path)
        # RDR-086 Phase 1: global chash → (collection, doc_id) lookup
        # populated by the six indexing write sites via best-effort
        # dual-write after each T3 upsert.
        self.chash_index: ChashIndex = ChashIndex(path)
        # RDR-089 Phase 1: per-document structured aspect table
        # populated by the document-grain hook chain at every CLI
        # ingest site (knowledge__* only in Phase 1).
        self.document_aspects: DocumentAspects = DocumentAspects(path)

    def __enter__(self) -> "T2Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close all six domain connections.

        Each store closes its own connection under its own lock. The
        close order is reverse of construction so that the most
        recently opened connection (document_aspects) is released
        first.
        """
        self.document_aspects.close()
        self.chash_index.close()
        self.telemetry.close()
        self.taxonomy.close()
        self.plans.close()
        self.memory.close()

    # ── Memory delegation (RDR-063 Phase 1 step 2) ────────────────────────────
    # Every memory-domain method delegates to self.memory. Signatures and
    # behavior are identical to the pre-split monolithic T2Database — these
    # delegates exist solely so callers that hold a T2Database (facade) do
    # not need to change their import or call sites.

    def put(
        self,
        project: str,
        title: str,
        content: str,
        tags: str = "",
        ttl: int | None = 30,
        agent: str | None = None,
        session: str | None = None,
    ) -> int:
        return self.memory.put(
            project=project,
            title=title,
            content=content,
            tags=tags,
            ttl=ttl,
            agent=agent,
            session=session,
        )

    def get(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> dict[str, Any] | None:
        return self.memory.get(project=project, title=title, id=id)

    def resolve_title(
        self,
        project: str,
        title: str,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Resolve an entry by exact title, falling back to unique prefix match.

        Delegates to :meth:`MemoryStore.resolve_title` (nexus-e59o). Exact
        match always wins; prefix fallback fires only when no exact match
        exists. Ambiguous prefix returns ``(None, candidates)`` so the
        caller can surface a clear error listing the matches.
        """
        return self.memory.resolve_title(project=project, title=title)

    def search(
        self,
        query: str,
        project: str | None = None,
        access: AccessPolicy = "track",
    ) -> list[dict[str, Any]]:
        return self.memory.search(query, project=project, access=access)

    def list_entries(
        self,
        project: str | None = None,
        agent: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.memory.list_entries(project=project, agent=agent)

    def get_projects_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        return self.memory.get_projects_with_prefix(prefix)

    def search_glob(self, query: str, project_glob: str) -> list[dict[str, Any]]:
        return self.memory.search_glob(query, project_glob)

    def search_by_tag(self, query: str, tag: str) -> list[dict[str, Any]]:
        return self.memory.search_by_tag(query, tag)

    def get_all(self, project: str) -> list[dict[str, Any]]:
        return self.memory.get_all(project)

    def delete(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> bool:
        """Delete a memory entry and cascade cleanup taxonomy assignments.

        v3.8.1: cross-domain cascade (memory → taxonomy). When a memory
        row is deleted, any ``topic_assignments`` rows referencing it
        by (project, title) are also removed and any topics left empty
        by the deletion are dropped. See
        ``CatalogTaxonomy.purge_assignments_for_doc`` for the
        scoped-by-collection semantics.

        The cascade is the facade's job because it crosses a domain
        boundary — ``MemoryStore`` does not know about taxonomy tables
        and should not. When the delete is by numeric id, we resolve
        the row's project and title first so the cascade can scope
        correctly.

        Lock ordering (storage review I-4): this is the ONLY cross-domain
        cascade in the facade. The order is:

            1. ``memory._lock`` (ID resolution only, released before step 2)
            2. ``memory._lock`` (re-acquired by ``memory.delete``)
            3. ``taxonomy._lock`` (acquired by ``purge_assignments_for_doc``)

        Callers MUST NOT hold ``taxonomy._lock`` when entering this
        method — doing so would invert the ordering and deadlock against
        any concurrent writer that follows the memory-before-taxonomy
        convention established here. No current caller violates this
        rule; the docstring is a contract for future edits.
        """
        # Resolve (project, title) for cascade scoping. Cheap indexed
        # lookup via the memory connection directly to avoid the
        # access_count side-effect of ``memory.get(id=...)`` on a row
        # we're about to delete. Only executes when the caller used --id.
        if id is not None and (project is None or title is None):
            with self.memory._lock:
                row = self.memory.conn.execute(
                    "SELECT project, title FROM memory WHERE id = ?", (id,)
                ).fetchone()
            if row is not None:
                project, title = row[0], row[1]
        deleted = self.memory.delete(project=project, title=title, id=id)
        if deleted and project and title:
            self.taxonomy.purge_assignments_for_doc(project=project, title=title)
        return deleted

    def find_overlapping_memories(
        self,
        project: str,
        min_similarity: float = 0.7,
        limit: int = 50,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return self.memory.find_overlapping_memories(
            project, min_similarity=min_similarity, limit=limit
        )

    def merge_memories(
        self,
        keep_id: int,
        delete_ids: list[int],
        merged_content: str,
    ) -> None:
        return self.memory.merge_memories(keep_id, delete_ids, merged_content)

    def flag_stale_memories(
        self,
        project: str,
        idle_days: int = 30,
    ) -> list[dict[str, Any]]:
        return self.memory.flag_stale_memories(project, idle_days=idle_days)

    # ── Plan Library delegation (RDR-063 Phase 1 step 3) ──────────────────────

    def save_plan(
        self,
        query: str,
        plan_json: str,
        outcome: str = "success",
        tags: str = "",
        project: str = "",
        ttl: int | None = None,
        name: str | None = None,
        verb: str | None = None,
        scope: str | None = None,
        dimensions: str | None = None,
        default_bindings: str | None = None,
        parent_dims: str | None = None,
        scope_tags: str | None = None,
    ) -> int:
        return self.plans.save_plan(
            query=query,
            plan_json=plan_json,
            outcome=outcome,
            tags=tags,
            project=project,
            ttl=ttl,
            name=name,
            verb=verb,
            scope=scope,
            dimensions=dimensions,
            default_bindings=default_bindings,
            parent_dims=parent_dims,
            scope_tags=scope_tags,
        )

    def search_plans(
        self,
        query: str,
        limit: int = 5,
        project: str = "",
    ) -> list[dict[str, Any]]:
        return self.plans.search_plans(query, limit=limit, project=project)

    def list_plans(self, limit: int = 20, project: str = "") -> list[dict[str, Any]]:
        return self.plans.list_plans(limit=limit, project=project)

    def plan_exists(self, query: str, tag: str) -> bool:
        """Return True if any plan with *query* has *tag* among its tags.

        Audit finding F2 / Landmine 1: facade delegate so
        ``commands/catalog.py:_seed_plan_templates`` can replace
        ``db.conn.execute(...)`` with ``db.plan_exists(...)`` and
        survive Phase 2's per-store connection split.
        """
        return self.plans.plan_exists(query, tag)

    # ── Telemetry delegation (RDR-063 Phase 1 step 6) ─────────────────────────
    # These delegates exist for two reasons:
    # 1. Public-API stability — callers that hold a T2Database keep using the
    #    same method names without reaching into self.telemetry.
    # 2. Monkeypatch surface — tests/test_structlog_events.py:68 patches
    #    expire_relevance_log on the T2Database instance and expects expire()
    #    to call the patched version. The facade's expire() therefore calls
    #    self.expire_relevance_log(...) (its own method), NOT
    #    self.telemetry.expire_relevance_log(...) directly. Routing through the
    #    facade method preserves the instance-attribute monkeypatch shape.

    def log_relevance(
        self,
        query: str,
        chunk_id: str,
        action: str,
        session_id: str = "",
        collection: str = "",
    ) -> int:
        return self.telemetry.log_relevance(
            query=query,
            chunk_id=chunk_id,
            action=action,
            session_id=session_id,
            collection=collection,
        )

    def log_relevance_batch(
        self,
        rows: list[tuple[str, str, str, str, str]],
    ) -> int:
        return self.telemetry.log_relevance_batch(rows)

    def get_relevance_log(
        self,
        query: str = "",
        chunk_id: str = "",
        action: str = "",
        session_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.telemetry.get_relevance_log(
            query=query,
            chunk_id=chunk_id,
            action=action,
            session_id=session_id,
            limit=limit,
        )

    def expire_relevance_log(self, days: int = 90) -> int:
        return self.telemetry.expire_relevance_log(days=days)

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def expire(self, relevance_log_days: int = 90) -> int:
        """Delete TTL-expired entries using heat-weighted effective TTL.

        effective_ttl = base_ttl * (1 + log(access_count + 1))
        Highly accessed entries survive longer. Unaccessed entries (access_count=0)
        expire at base rate (log(1) = 0, so multiplier = 1).

        Also purges relevance_log rows older than ``relevance_log_days`` days
        (default 90) to prevent unbounded growth of the telemetry table.
        Return value counts only memory rows deleted.

        Emits the ``expire_complete`` structured log event with fields:
          * ``memory_deleted`` (int) — number of memory rows deleted
          * ``relevance_log_deleted`` (int) — number of relevance_log rows
            purged; 0 when the purge succeeded but had nothing to delete
          * ``relevance_log_error`` (str, optional) — exception class name
            (``type(exc).__name__``, NOT the full message or traceback) —
            present ONLY when the log purge raised. Absent on success.

        The log purge call goes through ``self.expire_relevance_log`` (the
        facade's own delegate), NOT ``self.telemetry.expire_relevance_log``
        directly, so that
        ``test_expire_complete_includes_error_when_log_purge_fails``'s
        instance-attribute monkeypatch still injects faults correctly.
        """
        # Purge relevance_log (RDR-061 E2 telemetry retention).
        log_deleted = 0
        log_error: str | None = None
        try:
            log_deleted = self.expire_relevance_log(days=relevance_log_days)
        except Exception as exc:
            log_error = type(exc).__name__
            _log.warning("expire_relevance_log_failed", exc_info=exc)
        expired_ids = self.memory.expire()
        extra: dict[str, Any] = {}
        if log_error is not None:
            extra["relevance_log_error"] = log_error
        _log.info(
            "expire_complete",
            memory_deleted=len(expired_ids),
            relevance_log_deleted=log_deleted,
            **extra,
        )
        return len(expired_ids)
