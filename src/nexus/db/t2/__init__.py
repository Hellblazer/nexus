# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 memory bank — seven domain stores behind a composing facade.

The T2 tier is split into seven domain stores, all HTTP clients over the
engine's PG tables (their SQLite predecessors died across nexus-i711w
Stage 2; the local CatalogStore and the facade's ``catalog`` property died
with the terminal i711w deletion — the catalog is served by
``nexus.catalog.http_catalog_client`` / ``nexus.catalog.factory``):

=========================  ==========================  =================================================================
Attribute                  Class                       Responsibility
=========================  ==========================  =================================================================
``db.memory``              ``HttpMemoryStore``         Persistent notes, FTS search, access tracking, heat-weighted TTL
``db.plans``               ``HttpPlanLibrary``         Plan templates, plan search, plan TTL
``db.taxonomy``            ``HttpTaxonomyStore``       Topic clustering, topic assignment
``db.telemetry``           ``HttpTelemetryStore``      Relevance log (query/chunk/action), retention-based expiry
``db.chash_index``         ``HttpChashIndex``          chash → (collection, doc_id) global lookup (RDR-086)
``db.document_aspects``    ``HttpDocumentAspectsStore``  Per-document structured aspects table (RDR-089)
``db.aspect_queue``        ``HttpAspectQueue``         Async queue feeding the aspect-extraction worker (nexus-qeo8)
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

Schema is engine-owned (Liquibase) since RDR-158 P4 Stage 4
(nexus-i711w): the client-side SQLite migration chain is deleted, and the
local ``.db`` files are a frozen migration source (RDR-176 Gap 2).

See ``docs/architecture.md`` § T2 Domain Stores for the full picture
and ``docs/contributing.md`` § Adding a T2 Domain Feature for how to
extend the tier.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite3

import structlog

# Cheap import only: ``_sanitize_fts5`` was rehomed to ``records``
# (pure helpers, no substrate) when memory_store died (nexus-i711w
# Stage 2 sub-stage A3). Re-exported here for historical consumers
# (tests import it from ``nexus.db.t2``).
from nexus.db.t2.records import _sanitize_fts5

if TYPE_CHECKING:
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

_log = structlog.get_logger()


# Re-export surface for backward compatibility. The PEP 562
# ``__getattr__`` lazy resolver that used to live here served only the
# ``CatalogStore`` re-export; it died with the local catalog
# (nexus-i711w terminal deletion).
__all__ = [
    "T2Database",
    "_sanitize_fts5",
]


# ── Database facade ───────────────────────────────────────────────────────────


class T2Database:
    """T2 memory bank facade.

    Composition over seven domain stores (``memory``, ``plans``,
    ``taxonomy``, ``telemetry``, ``chash_index``, ``document_aspects``,
    ``aspect_queue``), all HTTP clients over the engine's PG tables.
    The facade forwards legacy public methods to the appropriate store
    and owns only the cross-domain ``expire()`` composition and the
    context manager.

    The eighth domain store the facade used to carry — the local SQLite
    ``catalog`` (RDR-120 P5.A.1) — died with the terminal i711w
    deletion; the catalog is reached via ``nexus.catalog.factory``.
    """

    def __init__(
        self,
        path: Path,
        *,
        run_migrations: bool | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Store path for cross-domain operations (e.g. rename_collection_cascade).
        self._path: Path = path

        # ``run_migrations`` is RETAINED-AND-IGNORED for signature stability
        # (RDR-158 P4 Stage 4, nexus-i711w): the machinery it used to gate —
        # ``bootstrap_schema`` / ``apply_pending`` / the ``NX_T2_AUTO_MIGRATE``
        # conftest default — is deleted with ``nexus/db/migrations.py``. The
        # engine owns its (Postgres) schema via Liquibase in every mode, and
        # the local ``.db`` is a frozen migration source that must never be
        # re-stamped (RDR-176 Gap 2). Callers passing ``run_migrations=True``
        # get exactly the same non-mutating construction as everyone else —
        # pinned by tests/db/test_rdr176_non_mutation.py.
        del run_migrations

        # ── RDR-138 T1.1 (nexus-tgzvt): process-wide rename coordination lock ──
        #
        # RENAME_LOCK is the OUTERMOST lock in the daemon process. It
        # serializes ``rename_collection_cascade`` against every queue and
        # aspect mutator, closing the rename-cascade vs aspect-worker race
        # (Gaps 1-3 per the RDR).
        #
        # Lock type: ``threading.RLock`` (reentrant), NOT ``threading.Lock``.
        # Rationale: T1.2 guarded ``claim_batch`` AND the inner
        # ``claim_next`` it calls in a loop. A plain Lock would self-deadlock
        # when the outer claim_batch acquire re-enters for each claim_next
        # call. RLock allows the same thread to acquire again without blocking.
        #
        # Lock ordering (forward constraint):
        #   RENAME_LOCK -> per-store self._lock   (RENAME_LOCK acquired FIRST)
        #   NEVER acquire RENAME_LOCK while already inside a self._lock region.
        #
        # The lock is instance-held: tests that construct T2Database directly
        # each get their own lock, isolating them from each other. (The T2
        # daemon and the SQLite ``AspectExtractionQueue`` this block was
        # written for died in RDR-158 P4, nexus-i711w; the lock survives for
        # the in-process cascade-vs-mutator ordering.)
        #
        # The cascade (rename_collection_cascade) bypasses all per-store
        # self._lock regions by design — it uses its own dedicated connection.
        # It acquires only RENAME_LOCK.
        self.RENAME_LOCK: threading.RLock = threading.RLock()

        # ── Construct domain stores ───────────────────────────────────
        # RDR-158 P3 (nexus-7bomn): the facade is the T2 entry seam, so it
        # is where the retired =sqlite opt-out fails LOUD. The per-site
        # selectors are collapsed (service is the only backend), which
        # would leave a stranded shell's NX_STORAGE_BACKEND[_<STORE>]=sqlite
        # silently ignored — the resolver raises the stranded-install
        # redirect instead. Validates the facade's own stores only; the
        # ``catalog`` / ``t1`` vars are validated where those tiers route
        # (see storage_mode.T2_FACADE_STORES).
        from nexus.db.storage_mode import T2_FACADE_STORES, storage_backend_for  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores

        for _store in T2_FACADE_STORES:
            storage_backend_for(_store)

        # RDR-152 nexus-gmiaf.4 routing seam, COLLAPSED in nexus-i711w
        # Stage 2 sub-stage A3: HttpMemoryStore is the only memory store —
        # the SQLite MemoryStore it used to select is deleted. Reads
        # NX_SERVICE_HOST / NX_SERVICE_PORT / NX_SERVICE_TOKEN from env.
        from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores
        from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore as _HttpTaxonomyStore  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores

        self.memory: HttpMemoryStore = HttpMemoryStore()

        # RDR-152 nexus-gmiaf.11 seam, COLLAPSED (A3): HttpPlanLibrary is
        # the only plan library — the SQLite PlanLibrary is deleted.
        from nexus.db.t2.http_plan_library import HttpPlanLibrary  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores
        self.plans: HttpPlanLibrary = HttpPlanLibrary()
        # RDR-152 nexus-gmiaf.14 seam, COLLAPSED in nexus-i711w Stage 2
        # sub-stage C (store) and Stage 3 (selector, nexus-7bomn):
        # HttpTaxonomyStore is the only taxonomy store, constructed eagerly
        # and unconditionally — the =sqlite arm that used to defer it is
        # gone with the opt-out. Eager matters: making it lazy changed WHEN
        # the service endpoint is first resolved and broke an unrelated
        # catalog store-hook path in test_memory (see the sub-stage C
        # history in git for the full account).
        self._taxonomy: Any = _HttpTaxonomyStore()

        # RDR-152 nexus-gmiaf.12 seam, COLLAPSED in nexus-i711w Stage 2
        # sub-stage A: HttpTelemetryStore is the only telemetry store — the
        # SQLite Telemetry it used to select is deleted.
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores
        self.telemetry: HttpTelemetryStore = HttpTelemetryStore()
        # RDR-086 Phase 1: global chash → (collection, doc_id) lookup
        # populated by the six indexing write sites via best-effort
        # dual-write after each T3 upsert.
        # RDR-152 nexus-gmiaf.16 seam, COLLAPSED in nexus-i711w Stage 2
        # sub-stage A: HttpChashIndex is the only chash index — the SQLite
        # ChashIndex it used to select is deleted.
        from nexus.db.t2.http_chash_index import HttpChashIndex  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores
        self.chash_index: HttpChashIndex = HttpChashIndex()
        # RDR-089 Phase 1: per-document structured aspect table
        # populated by the document-grain hook chain at every CLI
        # ingest site (knowledge__* only in Phase 1).
        # RDR-152 nexus-gmiaf.15 seam, COLLAPSED (nexus-i711w Stage 2
        # sub-stage A3): HttpDocumentAspectsStore is the only aspects store —
        # the SQLite DocumentAspects it used to select is deleted.
        from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores
        self.document_aspects: HttpDocumentAspectsStore = HttpDocumentAspectsStore()
        # RDR-089 follow-up (nexus-qeo8): durable queue feeding the
        # async aspect-extraction worker. The hook fires fast (just
        # an enqueue); the worker drains in a background thread.
        # RDR-138 T1.1: inject RENAME_LOCK so the queue shares the same
        # lock instance as the cascade. T1.2 will wrap mutator bodies.
        # RDR-152 nexus-gmiaf.15 seam, COLLAPSED in nexus-i711w Stage 2
        # sub-stage A: HttpAspectQueue is the only queue — the SQLite
        # AspectExtractionQueue it used to select is deleted.
        from nexus.db.t2.http_aspect_queue import HttpAspectQueue  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores
        self.aspect_queue: HttpAspectQueue = HttpAspectQueue(
            rename_lock=self.RENAME_LOCK
        )
        # RDR-139 Layer E: per-document DEVONthink highlight/mention notes,
        # keyed by tumbler. Dedicated table (NOT document_aspects) so
        # free-text highlights never contend with the aspect worker's
        # whole-row overwrite or its confidence gate.
        # RDR-152 nexus-gmiaf.15 seam, COLLAPSED in nexus-i711w Stage 2
        # sub-stage A: HttpDocumentHighlightsStore is the only highlights
        # store — the SQLite DocumentHighlights it used to select is deleted.
        from nexus.db.t2.http_document_highlights_store import HttpDocumentHighlightsStore  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores
        self.document_highlights: HttpDocumentHighlightsStore = HttpDocumentHighlightsStore()

    @property
    def taxonomy(self) -> "HttpTaxonomyStore":
        """The service-backed taxonomy store (constructed in ``__init__``).

        Service-only since nexus-i711w Stage 2 sub-stage C; the =sqlite
        lazy/raise arm this property used to carry died with the opt-out
        (RDR-158 P3, nexus-7bomn — the facade constructor now validates
        the env instead). The property survives for the setter below.
        """
        return self._taxonomy

    @taxonomy.setter
    def taxonomy(self, store: Any) -> None:
        """Allow injection, which the plain attribute this replaced supported.

        Every other domain store is a plain assignable attribute, and callers
        (notably the cascade's service-mode spies) swap them to observe routing.
        Making taxonomy lazy must not quietly remove that seam — a read-only
        property would force those callers to reach into ``_taxonomy``, which is
        strictly worse than keeping the public spelling they already use.
        """
        self._taxonomy = store

    def stored_schema_version(self) -> str:
        """Return the ``_nexus_version`` row's ``cli_version`` value.

        RDR-120 P3b: surfaced via the daemon's ``database.hello`` op so
        clients can validate version compatibility on first connect.
        Returns ``"0.0.0"`` when the row is missing (uninitialised DB).
        """
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        try:
            try:
                row = conn.execute(
                    "SELECT value FROM _nexus_version WHERE key='cli_version'"
                ).fetchone()
            except sqlite3.OperationalError:
                return "0.0.0"
            return row[0] if row else "0.0.0"
        finally:
            conn.close()

    def hello(self, client_schema_version: str | None = None) -> dict[str, str]:
        """Connection handshake: report the daemon's stored schema version.

        RDR-120 P3b (nexus-e9x4l): T2Client invokes ``database.hello``
        on first connect with its built-against schema version. The
        daemon echoes the daemon-side version; the client compares and
        raises ``T2SchemaVersionMismatchError`` on disagreement. The
        ``client_schema_version`` argument is accepted but not validated
        daemon-side — the comparison happens on the client because the
        client is the layer that knows what wire shape it expects.
        """
        return {
            "daemon_schema_version": self.stored_schema_version(),
            "client_schema_version": client_schema_version or "",
        }

    # NO bootstrap_schema: deleted in RDR-158 P4 Stage 4 (nexus-i711w) with
    # ``nexus/db/migrations.py``. The engine owns schema via Liquibase; the
    # local ``.db`` is a frozen migration source that must never be migrated
    # or re-stamped (RDR-176 Gap 2, tests/db/test_rdr176_non_mutation.py).

    def __enter__(self) -> "T2Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close all domain connections.

        Each store closes its own connection under its own lock. The
        close order is reverse of construction so the most recently
        opened connection is released first.
        """
        # Reverse-construction order: document_highlights was built after
        # aspect_queue (RDR-139 Layer E), so it closes first.
        self.document_highlights.close()
        self.aspect_queue.close()
        self.document_aspects.close()
        self.chash_index.close()
        self.telemetry.close()
        # Lazy since sub-stage C: close what was BUILT, never force
        # construction. Going through the property here would build (and for a
        # =sqlite caller, raise from) a store this T2Database never used —
        # turning close() into the one call that cannot fail cleanly.
        if self._taxonomy is not None:
            self._taxonomy.close()
            self._taxonomy = None
        self.plans.close()
        self.memory.close()

    # ── Atomic cascade rename (nexus-nhyh / K4) ───────────────────────────────

    def rename_collection_cascade(
        self,
        *,
        old: str,
        new: str,
        _conn: "sqlite3.Connection | None" = None,
    ) -> dict[str, int]:
        """Rename a collection atomically across all T2 collection tables.

        nexus-nhyh / K4 originally ran all UPDATEs inside a single SQLite
        transaction; every leg is now an engine HTTP call (nexus-i711w
        Stage 2), so atomicity is PER STORE, not cross-store — see the
        inner method's docstring.

        Tables updated:
          - ``chash_index.physical_collection``
          - ``document_aspects.collection`` (with collision-defense DELETE)
          - ``aspect_extraction_queue.collection`` (with collision-defense DELETE)
          - ``topics.collection`` / ``topic_assignments.source_collection`` /
            ``taxonomy_meta.collection``
          - ``search_telemetry.collection``
          - ``hook_failures.collection`` (if table exists)

        Returns a dict with counts per table. Raises on any failure.

        Callers (``rename_collection_data_plane``) catch and re-raise as
        ClickException with a non-zero exit code.

        ``_conn`` was the SQLite test-seam parameter; it is retained for
        signature stability and ignored (the scaffolding it fed died with
        the SQLite stores).

        RDR-138 T1.1 (nexus-tgzvt): acquires ``self.RENAME_LOCK`` for the
        ENTIRE method body, serializing the cascade against every
        queue/aspect mutator guarded by the same lock (Gaps 1-3:
        aspect-worker observes a consistent view). Lock ordering:
        RENAME_LOCK is the outermost lock.
        """
        with self.RENAME_LOCK:
            return self._rename_collection_cascade_locked(old=old, new=new, _conn=_conn)

    def _rename_collection_cascade_locked(
        self,
        *,
        old: str,
        new: str,
        _conn: "sqlite3.Connection | None" = None,
    ) -> dict[str, int]:
        """Inner implementation — called only while RENAME_LOCK is held.

        Every leg is an HTTP call to the engine (nexus-i711w Stage 2
        sub-stages A/A3 collapsed all seven store seams), so the old
        dedicated-SQLite-connection BEGIN/COMMIT scaffolding is gone: there
        is no client-side transaction left to wrap. Cross-store atomicity on
        this fan-out path is therefore per-store — a mid-cascade failure
        raises with earlier legs already applied (recorded as a GAP
        candidate on nexus-i711w.1; the engine-side rename endpoint is the
        atomic alternative). ``_conn`` is retained for signature stability
        and ignored.
        """
        counts: dict[str, int] = {
            "chash": 0,
            "aspects": 0,
            "aspect_queue": 0,
            "highlights": 0,
            "tax_topics": 0,
            "tax_assignments": 0,
            "tax_meta": 0,
            "search_telemetry": 0,
            "hook_failures": 0,
        }

        counts["chash"] = self.chash_index.rename_collection(old=old, new=new)

        # document_aspects: collision defense (#1057 dedup-on-live-PK) is
        # engine-side now, with the rest of the leg.
        counts["aspects"] = self.document_aspects.rename_collection(old=old, new=new)

        counts["aspect_queue"] = self.aspect_queue.rename_collection(old=old, new=new)

        counts["highlights"] = self.document_highlights.rename_collection(old=old, new=new)

        # taxonomy (three sub-tables)
        tax_counts = self.taxonomy.rename_collection(old, new)
        counts["tax_topics"] = tax_counts.get("topics", 0)
        counts["tax_assignments"] = tax_counts.get("assignments", 0)
        counts["tax_meta"] = tax_counts.get("meta", 0)

        # search_telemetry + hook_failures — one call, two counts.
        tel_counts = self.telemetry.rename_collection(old=old, new=new)
        counts["search_telemetry"] = tel_counts.get("search_telemetry", 0)
        counts["hook_failures"] = tel_counts.get("hook_failures", 0)

        return counts

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

    def put_or_merge(
        self,
        project: str,
        title: str,
        content: str,
        tags: str = "",
        ttl: int | None = 30,
        agent: str | None = None,
        session: str | None = None,
        min_similarity: float = 0.5,
    ) -> tuple[int, str]:
        return self.memory.put_or_merge(
            project=project,
            title=title,
            content=content,
            tags=tags,
            ttl=ttl,
            agent=agent,
            session=session,
            min_similarity=min_similarity,
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

        RDR-164 P4 (nexus-jcx6w): this memory→taxonomy cascade is
        ORTHOGONAL to the catalog ``fk-001`` document cascade and is NOT
        made redundant by it. ``fk-001`` is rooted at
        ``catalog_documents(tenant_id, tumbler)``; this path deletes a
        ``memory`` row keyed by ``(project, title)`` and purges its
        ``topic_assignments`` — a relationship no FK covers in either
        backend (``topic_assignments.doc_id`` is a chunk content-hash,
        not a tumbler; see fk-001 changeset 1). Do NOT retire
        ``purge_assignments_for_doc`` here on the assumption that fk-001
        covers it — it does not.

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
        # Resolve (project, title) for cascade scoping. Only executes when
        # the caller used --id.
        #
        # nexus-aqbrk: this was an unconditional ``self.memory._lock`` +
        # ``self.memory.conn.execute``, which raises AttributeError against
        # HttpMemoryStore — the id-only path was unreachable in service mode.
        # Latent rather than live (the sole production caller,
        # mcp/core.py::memory_delete, always passes project+title), but the
        # facade is public API and the cascade below is the load-bearing
        # part: without this resolution, ``purge_assignments_for_doc`` never
        # runs and topic_assignments leak silently, which no FK covers.
        # (The raw-SQLite lookup arm died with the stores; the public read's
        # access_count increment is immaterial on a row about to be deleted.)
        if id is not None and (project is None or title is None):
            entry = self.memory.get(id=id)
            if entry is not None:
                project, title = entry["project"], entry["title"]
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

    def trim_hook_failures(self, days: int = 30) -> int:
        """Facade delegate for the hook_failures age reaper (nexus-7365x)."""
        return self.telemetry.trim_hook_failures(days=days)

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def expire(self, relevance_log_days: int | None = None) -> int:
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
        if relevance_log_days is None:
            from nexus.db.t2.records import RELEVANCE_LOG_RETENTION_DAYS  # noqa: PLC0415 — single-source horizon coupling
            relevance_log_days = RELEVANCE_LOG_RETENTION_DAYS
        # Purge relevance_log (RDR-061 E2 telemetry retention).
        log_deleted = 0
        log_error: str | None = None
        try:
            log_deleted = self.expire_relevance_log(days=relevance_log_days)
        except Exception as exc:  # noqa: BLE001 — best-effort relevance-log expiry; logged via log.warning, expiry continues
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

    def complete_aspect(self, record_fields: dict[str, Any]) -> bool:
        """Persist an extracted aspect and clear its queue row in one call.

        nexus-zir76 (RDR-128 follow-up): the aspect worker previously
        upserted ``document_aspects`` and called ``aspect_queue.mark_done``
        via two DIRECT ``memory.db`` writes, competing with the daemon for
        the single WAL writer lock. When the direct ``mark_done`` (or the
        failure path's ``mark_failed``) lost that race, the row was
        orphaned ``in_progress`` until the ``reclaim_stale`` backstop.
        Folding both writes into one daemon-routable method keeps the
        worker off the direct write path and closes that window.

        *record_fields* is ``dataclasses.asdict(AspectRecord)`` — a plain
        JSON-shaped dict, because the daemon wire protocol decodes a
        dataclass argument to its field dict (it does not reconstruct the
        object). The ``AspectRecord`` is rebuilt here, server-side.

        Returns the ``document_aspects.upsert`` result. ``mark_done`` is
        idempotent, so a reclaim-driven re-extraction after a crash
        between the two writes simply re-upserts — no duplicate, no stuck
        row.

        RDR-138 T1.2 (nexus-ra2vj): wraps the ENTIRE call — both the
        ``document_aspects.upsert`` AND the ``aspect_queue.mark_done`` —
        under ONE ``RENAME_LOCK`` acquisition. This closes Gap 3: a
        ``rename_collection_cascade`` cannot interleave between the two
        writes (which would rename the document_aspects row under the OLD
        collection name before mark_done can clear the queue row, leaving
        an orphaned queue row under OLD). The ``mark_done`` call
        re-acquires RENAME_LOCK via the now-guarded mutator; the RLock
        makes the re-entrant acquisition safe.
        """
        from nexus.db.t2.records import AspectRecord  # noqa: PLC0415 — deferred import — circular-dep avoidance between T2 facade and stores
        record = AspectRecord(**record_fields)
        with self.RENAME_LOCK:
            upserted = self.document_aspects.upsert(record)
            self.aspect_queue.mark_done(record.collection, record.source_path)
        return upserted
