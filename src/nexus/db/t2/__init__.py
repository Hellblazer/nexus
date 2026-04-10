# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 SQLite memory bank — four domain stores behind a composing facade.

The T2 tier is split into four domain stores, each owning its own set
of tables in a shared SQLite file:

====================  ==========================  ============================
Attribute             Class                       Responsibility
====================  ==========================  ============================
``db.memory``         ``MemoryStore``             Persistent notes, project context, FTS5 search
``db.plans``          ``PlanLibrary``             Plan templates, plan search, plan TTL
``db.taxonomy``       ``CatalogTaxonomy``         Topic clustering, topic assignment
``db.telemetry``      ``Telemetry``               Relevance logs, access tracking
====================  ==========================  ============================

``T2Database`` is a facade: it constructs the four stores and re-exposes
their public methods as thin delegates for backward compatibility.
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
* ``taxonomy.cluster_and_persist`` no longer freezes interactive
  memory access while rebuilding clusters.

Schema migrations are per-domain and idempotent: each store runs its
own migration guard the first time it sees a given database path, so
independent stores can initialize concurrently.

See ``docs/architecture.md`` § T2 Domain Stores for the full picture
and ``docs/contributing.md`` § Adding a T2 Domain Feature for how to
extend the tier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy
from nexus.db.t2.memory_store import (
    AccessPolicy,
    MemoryStore,
    _sanitize_fts5,  # re-exported for nexus.catalog.catalog_db
)
from nexus.db.t2.plan_library import PlanLibrary
from nexus.db.t2.telemetry import Telemetry

_log = structlog.get_logger()

# Re-export for backward compatibility — ``catalog/catalog_db.py`` and
# ``tests/test_t2.py`` still ``from nexus.db.t2 import _sanitize_fts5``.
__all__ = [
    "AccessPolicy",
    "CatalogTaxonomy",
    "MemoryStore",
    "PlanLibrary",
    "T2Database",
    "Telemetry",
    "_sanitize_fts5",
]


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
        path.parent.mkdir(parents=True, exist_ok=True)
        # Order matters for WAL setup: MemoryStore runs first and
        # transitions the SQLite file into WAL mode. Subsequent stores
        # re-issue ``PRAGMA journal_mode=WAL`` on their own connections,
        # which is a no-op once the file is already in WAL. The
        # per-store ``busy_timeout=5000`` absorbs the brief write-lock
        # contention that can occur while four connections race through
        # their initial CREATE TABLE IF NOT EXISTS scripts.
        self.memory: MemoryStore = MemoryStore(path)
        self.plans: PlanLibrary = PlanLibrary(path)
        # CatalogTaxonomy takes a MemoryStore reference because
        # cluster_and_persist reads memory entries to build word vectors.
        # The cross-domain dependency is intentionally explicit at the
        # constructor signature (RDR-063 §Cross-Domain Contracts).
        self.taxonomy: CatalogTaxonomy = CatalogTaxonomy(path, self.memory)
        self.telemetry: Telemetry = Telemetry(path)

    def __enter__(self) -> "T2Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close all four domain connections.

        Each store closes its own connection under its own lock. The
        close order is reverse of construction so that the most
        recently opened connection (telemetry) is released first.
        """
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
        return self.memory.delete(project=project, title=title, id=id)

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
    ) -> int:
        return self.plans.save_plan(
            query=query,
            plan_json=plan_json,
            outcome=outcome,
            tags=tags,
            project=project,
            ttl=ttl,
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
