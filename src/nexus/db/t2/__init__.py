# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 SQLite memory bank — facade over the domain stores.

Phase 1 of RDR-063 extracts the monolithic ``T2Database`` into domain
stores (``MemoryStore``, ``PlanLibrary``, ``CatalogTaxonomy``,
``Telemetry``). This module is the facade: it opens the single
``sqlite3.Connection``, wraps it in a :class:`SharedConnection`, and
instantiates the domain stores around it. All legacy public API calls
(``put``, ``search``, ``save_plan``, ``log_relevance``, ``expire``, …)
are preserved as thin delegating methods so no caller needs to change.

Step 2 (bead ``nexus-vx3c``) moved memory-domain state and methods into
:mod:`nexus.db.t2.memory_store`. Plan, taxonomy, and telemetry code
still lives here and will move in later Phase 1 steps.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import structlog

from nexus.db.t2._connection import SharedConnection
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
    "SharedConnection",
    "T2Database",
    "Telemetry",
    "_sanitize_fts5",
]


# ── Residual schema ──────────────────────────────────────────────────────────
# Memory schema lives in memory_store._MEMORY_SCHEMA_SQL.
# Plans schema lives in plan_library._PLANS_SCHEMA_SQL.
# Taxonomy schema (topics + topic_assignments) lives in
# catalog_taxonomy._TAXONOMY_SCHEMA_SQL.
# Only the WAL pragma + relevance_log (telemetry) remain in the facade
# until ``nexus-yjww`` extracts telemetry.
_RESIDUAL_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;
"""


# ── Per-process migration guard ──────────────────────────────────────────────
# Migrations only need to run once per DB path per process. The MCP server
# opens a fresh T2Database on every tool call; without this guard, each call
# probes all 6 migrations.
#
# This lives at the facade level in Phase 1; RDR-063 §Open Question 3
# (per-domain guards) will split it in a later Phase 1 step. The existing
# regression tests ``test_migration_guard_concurrent_threads`` access this
# module attribute directly.
_migrated_paths: set[str] = set()
_migrated_lock = threading.Lock()


# ── Database facade ───────────────────────────────────────────────────────────


class T2Database:
    """T2 SQLite memory bank with FTS5 full-text search.

    Phase 1 facade: holds a single :class:`SharedConnection` and delegates
    memory-domain calls to :class:`MemoryStore`. Plan, taxonomy, and
    telemetry methods remain inlined until their extraction beads land.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        # Wrap the connection + lock in a SharedConnection for the domain
        # stores. Both self._lock / self.conn and the SharedConnection's
        # fields point at the same objects — Phase 2 will split them.
        self._shared = SharedConnection(conn=self.conn, lock=self._lock)
        self.memory: MemoryStore = MemoryStore(self._shared)
        self.plans: PlanLibrary = PlanLibrary(self._shared)
        # CatalogTaxonomy takes a MemoryStore reference because
        # cluster_and_persist reads memory entries to build word vectors.
        # The cross-domain dependency is intentionally explicit at the
        # constructor signature (RDR-063 §Cross-Domain Contracts).
        self.taxonomy: CatalogTaxonomy = CatalogTaxonomy(self._shared, self.memory)
        self.telemetry: Telemetry = Telemetry(self._shared)
        # Canonicalize path for the migration guard key: /foo/./bar and
        # /foo/bar must hash to the same entry or the guard is bypassed.
        try:
            canonical_key = str(path.resolve())
        except OSError:
            canonical_key = str(path)
        self._init_schema(canonical_key)

    def _init_schema(self, path_key: str) -> None:
        with self._lock:
            # Note: executescript() implicitly COMMITs any open transaction.
            # Safe here because _init_schema runs only during __init__ with
            # no prior transaction. All four domains run their lock-naive
            # init_schema_unlocked helpers; the residual script only carries
            # the WAL pragma now that every domain owns its own DDL.
            self.memory.init_schema_unlocked()
            self.plans.init_schema_unlocked()
            self.taxonomy.init_schema_unlocked()
            self.telemetry.init_schema_unlocked()
            self.conn.executescript(_RESIDUAL_SCHEMA_SQL)
            self.conn.commit()
            result = self.conn.execute("PRAGMA journal_mode").fetchone()
            if result and result[0].lower() != "wal":
                _log.warning("WAL mode not available", actual_mode=result[0])
            # Migration guard: hold _migrated_lock across the full check-run-add
            # sequence so two concurrent T2Database constructors on the same path
            # cannot both enter the migration functions (ALTER TABLE ADD COLUMN
            # is NOT idempotent — double-application raises OperationalError).
            #
            # The relevance_log "migration" was historically a one-shot
            # CREATE-IF-MISSING because the table was added in a later release
            # than memory/plans. Now that telemetry.init_schema_unlocked()
            # creates the table at every construction (idempotent via
            # IF NOT EXISTS), the legacy migration is dead code and has been
            # removed.
            with _migrated_lock:
                if path_key in _migrated_paths:
                    return
                self.memory._migrate_fts_if_needed()
                self.plans._migrate_plans_if_needed()
                self.plans._migrate_plans_ttl_if_needed()
                self.memory._migrate_access_tracking_if_needed()
                self.taxonomy._migrate_topics_if_needed()
                _migrated_paths.add(path_key)

    def __enter__(self) -> "T2Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

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
        Return value counts only memory rows deleted. Log purge count and
        errors are surfaced via structured logs (``expire_complete`` /
        ``expire_relevance_log_failed``).

        The call goes through ``self.expire_relevance_log`` (the facade's own
        delegate), NOT ``self.telemetry.expire_relevance_log`` directly, so
        that ``test_expire_complete_includes_error_when_log_purge_fails``'s
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
