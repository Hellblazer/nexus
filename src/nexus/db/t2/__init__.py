# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 SQLite memory bank — seven domain stores behind a composing facade.

The T2 tier is split into seven domain stores, each owning its own
set of tables in a shared SQLite file:

=========================  ==========================  =================================================================
Attribute                  Class                       Responsibility
=========================  ==========================  =================================================================
``db.memory``              ``MemoryStore``             Persistent notes, FTS5 search, access tracking, heat-weighted TTL
``db.plans``               ``PlanLibrary``             Plan templates, plan search, plan TTL
``db.taxonomy``            ``CatalogTaxonomy``         Topic clustering, topic assignment
``db.telemetry``           ``Telemetry``               Relevance log (query/chunk/action), retention-based expiry
``db.chash_index``         ``ChashIndex``              chash → (collection, doc_id) global lookup (RDR-086)
``db.document_aspects``    ``DocumentAspects``         Per-document structured aspects table (RDR-089)
``db.aspect_queue``        ``AspectExtractionQueue``   Async queue feeding the aspect-extraction worker (nexus-qeo8)
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

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite3

import structlog

from nexus.db.t2._tuning import SERVING_BUSY_TIMEOUT_MS

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
    from nexus.db.t2.catalog import CatalogStore
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy
    from nexus.db.t2.chash_index import ChashIndex
    from nexus.db.t2.memory_store import AccessPolicy, MemoryStore
    from nexus.db.t2.plan_library import PlanLibrary
    from nexus.db.t2.telemetry import Telemetry

_log = structlog.get_logger()


# RDR-128 P0a (RF-3): the daemon's startup migration must tolerate another
# process holding memory.db's single WAL writer lock (typically ``nx index
# repo``). ``busy_timeout`` governs how long each statement waits for the
# lock before raising ``database is locked``; the old 5s was tight enough
# that a concurrent indexer could trip a migration step and crash the
# freshly-spawned daemon. 30s matches the intra-host contention window
# already used by aspect_extraction_queue (nexus-v4m7y) and costs nothing
# on a quiet database.
_BOOTSTRAP_BUSY_TIMEOUT_MS: int = 30000

# Bounded Python-level retry around ``apply_pending``, layered on top of the
# busy_timeout. Mirrors aspect_extraction_queue.reclaim_stale: three attempts
# with two inter-attempt sleeps. ``apply_pending`` is idempotent (per-migration
# existence guards) and only records the path as done on success, so a retry
# after a mid-run ``database is locked`` safely re-runs from bootstrap_version.
# Worst case if every attempt blocks the full busy_timeout: 30 + 0.5 + 30 +
# 1.0 + 30 = ~91.5s, after which the final attempt re-raises so a genuinely
# stuck lock still surfaces rather than hanging the daemon forever.
_BOOTSTRAP_RETRY_SLEEPS_BETWEEN: tuple[float, ...] = (0.5, 1.0)


def _rename_dedup_col(conn: "sqlite3.Connection", table: str) -> str:
    """Return the column the rename-cascade collision-defense should dedup on
    for ``table``: the live PRIMARY KEY, which depends on migration state.

    RDR-108 Phase 1c migrates the aspect tables' PK
    ``(collection, source_path) -> (doc_id)`` (and RDR-096 P5.2 then drops
    ``source_path`` from ``document_aspects``), but both steps are deferred
    until a catalog exists — so a DB can be in either shape. We prefer
    ``doc_id`` (the migrated PK) when the column is present, else fall back to
    ``source_path`` (the pre-migration PK). The result is interpolated into a
    DELETE, so it must come from the schema (PRAGMA), never from user input.
    Issue #1057.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "doc_id" in cols:
        return "doc_id"
    if "source_path" in cols:
        return "source_path"
    raise RuntimeError(
        f"rename cascade: {table} has neither doc_id nor source_path to dedup on"
    )


def _apply_pending_with_lock_retry(
    conn: sqlite3.Connection, current_version: str
) -> None:
    """Run the startup migration (``PRAGMA journal_mode=WAL`` + ``apply_pending``)
    with a bounded retry on ``database is locked`` / ``database is busy``.

    ``journal_mode=WAL`` is inside the retry because it is itself a write
    that takes the writer lock when the file is not already in WAL mode —
    leaving it outside would let a held lock crash the daemon before
    ``apply_pending`` ever runs (code-review finding, RDR-128 P0a). Only
    writer-slot contention is retried; any other ``OperationalError``
    (schema corruption, FK violation, ...) propagates on the first attempt.
    The final attempt re-raises so the failure is never silently swallowed.
    """
    import time

    from nexus.db.migrations import apply_pending

    sleeps_between = _BOOTSTRAP_RETRY_SLEEPS_BETWEEN
    max_attempts = len(sleeps_between) + 1
    for attempt in range(1, max_attempts + 1):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            apply_pending(conn, current_version)
            if attempt > 1:
                _log.info(
                    "t2_bootstrap_migration_recovered",
                    attempt=attempt,
                )
            return
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            # Clear any partial transaction left by the failed step so the
            # retry re-runs from a clean connection state.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            if attempt == max_attempts:
                raise
            sleep_seconds = sleeps_between[attempt - 1]
            _log.warning(
                "t2_bootstrap_migration_lock_retry",
                attempt=attempt,
                next_sleep_seconds=sleep_seconds,
                exc=str(exc),
            )
            time.sleep(sleep_seconds)


def _cold_start_is_current_and_wal(path: Path) -> bool:
    """Return True iff *path* is already at ``current_version`` AND already in
    WAL journal mode, using only lock-free reads (RDR-140 P1.2 Gap 4, A3).

    Both probes succeed under a held EXCLUSIVE writer lock, so this never
    contends with a concurrent writer. Any read error, a missing/``0.0.0``
    version row, a non-WAL journal, or a ``"0.0.0"`` current version (the
    importlib fallback) returns False so the caller takes the full
    flock+migration path — a genuine pending migration must always run.
    """
    # A non-existent path is a fresh DB: there is nothing to fast-path, and we
    # must NOT create the file here (this probe is read-only by contract — a
    # plain ``sqlite3.connect`` would materialise a 0-byte DB). Fall through to
    # the full bootstrap, which creates and migrates it. We use a plain
    # read-write connection rather than ``mode=ro`` because read-only mode
    # cannot create the ``-shm`` file a WAL DB needs when no other connection
    # is open (the exact cold-start steady state we optimise), which would
    # defeat the fast path. Reads on a plain connection are lock-free (A3),
    # mirroring ``stored_schema_version``.
    if not path.exists():
        return False
    try:
        from importlib.metadata import version as _pkg_version

        try:
            current_version = _pkg_version("conexus")
        except Exception:  # noqa: BLE001
            return False
        if current_version == "0.0.0":
            return False

        conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            try:
                row = conn.execute(
                    "SELECT value FROM _nexus_version WHERE key='cli_version'"
                ).fetchone()
            except sqlite3.OperationalError:
                return False  # _nexus_version absent — uninitialised DB.
            if not row or row[0] != current_version:
                return False
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            if not mode or str(mode[0]).lower() != "wal":
                return False
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


# Re-export surface for backward compatibility. Resolution happens
# lazily through the module-level ``__getattr__`` below; the eager
# imports were pulling sklearn/scipy/numpy through CatalogTaxonomy on
# every CLI invocation that touched any nexus.db.t2 symbol.
__all__ = [
    "AccessPolicy",
    "AspectExtractionQueue",
    "CatalogStore",
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
        "AccessPolicy":          "nexus.db.t2.memory_store",
        "AspectExtractionQueue": "nexus.db.t2.aspect_extraction_queue",
        "CatalogStore":          "nexus.db.t2.catalog",
        "MemoryStore":           "nexus.db.t2.memory_store",
        "CatalogTaxonomy":       "nexus.db.t2.catalog_taxonomy",
        "ChashIndex":            "nexus.db.t2.chash_index",
        "DocumentAspects":       "nexus.db.t2.document_aspects",
        "PlanLibrary":           "nexus.db.t2.plan_library",
        "Telemetry":             "nexus.db.t2.telemetry",
    }
    if name in _MAP:
        import importlib
        mod = importlib.import_module(_MAP[name])
        value = getattr(mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: RDR-120 P3b: process-wide default for ``T2Database(run_migrations=...)``
#: when the caller leaves the parameter unspecified. Production code keeps
#: this False (daemon owns migrations). The test conftest flips it to True
#: so existing direct-open fixtures keep migrating their fresh tmp DBs.
_DEFAULT_RUN_MIGRATIONS: bool = False

#: Env-var override for :data:`_DEFAULT_RUN_MIGRATIONS`. Set to ``"1"`` to
#: opt every direct-open ``T2Database`` construction into running
#: ``apply_pending`` even when the in-process module global is False.
#: Used by the test conftest to propagate auto-migrate semantics into
#: subprocess children (``subprocess.run`` / ``claude -p`` dispatches /
#: MCP children) which inherit ``os.environ`` but not Python module state.
_RUN_MIGRATIONS_ENV: str = "NX_T2_AUTO_MIGRATE"


def _resolve_default_run_migrations() -> bool:
    """Read the effective default from the env var first, the module
    global second. The env-var path is what propagates into spawned
    subprocesses (RDR-120 P3b review item 1).
    """
    import os as _os

    raw = _os.environ.get(_RUN_MIGRATIONS_ENV, "").strip()
    if raw:
        return raw not in ("0", "false", "False", "no", "")
    return _DEFAULT_RUN_MIGRATIONS


# ── Database facade ───────────────────────────────────────────────────────────


class T2Database:
    """T2 SQLite memory bank with FTS5 full-text search.

    Composition over eight domain stores (``memory``, ``plans``,
    ``taxonomy``, ``telemetry``, ``chash_index``, ``document_aspects``,
    ``aspect_queue``, ``catalog``). Each store owns its own connection,
    lock, schema init, and migration guard; the facade forwards legacy
    public methods to the appropriate store and owns only the
    cross-domain ``expire()`` composition and the context manager.

    The seven domain stores share the single ``nexus.db`` SQLite file
    passed at construction. The eighth — ``catalog`` — is unique in
    that it opens a separate ``.catalog.db`` file under
    ``catalog_path()``. The path split is preserved through P5.A.1 by
    the Hal-approved thin-shim design; collapsing the files is
    explicitly out of scope.

    The ``catalog`` store is constructed lazily on first attribute
    access (RDR-120 P5.A.1) so tests that never touch the catalog do
    not eagerly open ``.catalog.db`` and contend with separately-
    constructed ``CatalogDB`` instances during the P5.A.1 to P5.A.2
    cutover window.
    """

    def __init__(
        self,
        path: Path,
        *,
        run_migrations: bool | None = None,
        catalog_db_path: Path | None = None,
    ) -> None:
        # Lazy-load the seven store classes here rather than at module
        # import time so the CLI cold-start path (which only needs
        # ``_sanitize_fts5``) does not pull sklearn/scipy/numpy through
        # CatalogTaxonomy.
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy
        from nexus.db.t2.chash_index import ChashIndex
        from nexus.db.t2.document_aspects import DocumentAspects
        from nexus.db.t2.document_highlights import DocumentHighlights
        from nexus.db.t2.memory_store import MemoryStore
        from nexus.db.t2.plan_library import PlanLibrary
        from nexus.db.t2.telemetry import Telemetry

        path.parent.mkdir(parents=True, exist_ok=True)
        # Store path for cross-domain operations (e.g. rename_collection_cascade).
        self._path: Path = path

        # RDR-120 P3b: migration ownership transferred to the T2 daemon.
        # ``T2Database.__init__`` no longer auto-runs ``apply_pending``.
        # Callers that need to materialise the schema (daemon startup,
        # ``nx upgrade``, conftest bootstrap) must either pass
        # ``run_migrations=True`` here or call
        # :meth:`T2Database.bootstrap_schema` explicitly. The default is
        # the module-level :data:`_DEFAULT_RUN_MIGRATIONS` (False in
        # production; conftest flips it to True so the test suite stays
        # green without touching 300+ direct-open call sites).
        effective = (
            _resolve_default_run_migrations()
            if run_migrations is None
            else run_migrations
        )
        if effective:
            T2Database.bootstrap_schema(path)

        # ── RDR-138 T1.1 (nexus-tgzvt): process-wide rename coordination lock ──
        #
        # RENAME_LOCK is the OUTERMOST lock in the daemon process. It
        # serializes ``rename_collection_cascade`` against every queue and
        # aspect mutator, closing the rename-cascade vs aspect-worker race
        # (Gaps 1-3 per the RDR).
        #
        # Lock type: ``threading.RLock`` (reentrant), NOT ``threading.Lock``.
        # Rationale: T1.2 will guard ``claim_batch`` AND the inner
        # ``claim_next`` it calls in a loop. A plain Lock would self-deadlock
        # when the outer claim_batch acquire re-enters for each claim_next
        # call. RLock allows the same thread to acquire again without blocking.
        # An alternative shape (unlocked ``_claim_next_locked`` helper) is
        # documented in ``AspectExtractionQueue`` but the RLock approach is
        # chosen for its simplicity.
        #
        # Lock ordering (forward constraint for T1.2 authors):
        #   RENAME_LOCK -> per-store self._lock   (RENAME_LOCK acquired FIRST)
        #   NEVER acquire RENAME_LOCK while already inside a self._lock region.
        #
        # The daemon runs exactly ONE T2Database instance (verified by
        # t2_daemon.py's ``_build_dispatch_table`` receiving a single
        # ``self._t2db``). The lock is instance-held: tests that construct
        # T2Database directly each get their own lock, isolating them from each
        # other. Stand-alone AspectExtractionQueue construction (outside
        # T2Database) falls back to its own RLock via the default parameter.
        #
        # The cascade (rename_collection_cascade) bypasses all per-store
        # self._lock regions by design — it uses its own dedicated connection.
        # It acquires only RENAME_LOCK.
        self.RENAME_LOCK: threading.RLock = threading.RLock()

        # ── Construct domain stores ───────────────────────────────────
        # RDR-152 nexus-gmiaf.4: routing seam.  storage_backend_for("memory")
        # returns StorageBackend.SQLITE by default (env NX_STORAGE_BACKEND_MEMORY
        # or NX_STORAGE_BACKEND unset).  nexus-gmiaf.7 replaces the
        # NotImplementedError branch with HttpMemoryStore(...).
        from nexus.db.storage_mode import StorageBackend, storage_backend_for

        if storage_backend_for("memory") == StorageBackend.SERVICE:
            # RDR-152 nexus-gmiaf.7: thin Python HTTP client over the Java service.
            # Reads NX_SERVICE_HOST / NX_SERVICE_PORT / NX_SERVICE_TOKEN from env.
            from nexus.db.t2.http_memory_store import HttpMemoryStore
            self.memory: MemoryStore = HttpMemoryStore()  # type: ignore[assignment]
        else:
            self.memory: MemoryStore = MemoryStore(path)

        # RDR-152 nexus-gmiaf.11: plans service seam.
        # NX_STORAGE_BACKEND_PLANS=service routes to the Java HTTP plans endpoint.
        if storage_backend_for("plans") == StorageBackend.SERVICE:
            from nexus.db.t2.http_plan_library import HttpPlanLibrary
            self.plans: PlanLibrary = HttpPlanLibrary()  # type: ignore[assignment]
        else:
            self.plans: PlanLibrary = PlanLibrary(path)
        # RDR-152 nexus-gmiaf.14: taxonomy service seam.
        # NX_STORAGE_BACKEND_TAXONOMY=service routes to HttpTaxonomyStore.
        # CatalogTaxonomy takes a MemoryStore reference for the
        # get_topic_docs JOIN (RDR-063 §Cross-Domain Contracts).
        if storage_backend_for("taxonomy") == StorageBackend.SERVICE:
            from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
            self.taxonomy: CatalogTaxonomy = HttpTaxonomyStore()  # type: ignore[assignment]
        else:
            self.taxonomy: CatalogTaxonomy = CatalogTaxonomy(path, self.memory)

        # RDR-152 nexus-gmiaf.12: telemetry service seam.
        # NX_STORAGE_BACKEND_TELEMETRY=service routes to HttpTelemetryStore.
        if storage_backend_for("telemetry") == StorageBackend.SERVICE:
            from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
            self.telemetry: Telemetry = HttpTelemetryStore()  # type: ignore[assignment]
        else:
            self.telemetry: Telemetry = Telemetry(path)
        # RDR-086 Phase 1: global chash → (collection, doc_id) lookup
        # populated by the six indexing write sites via best-effort
        # dual-write after each T3 upsert.
        # RDR-152 nexus-gmiaf.16: chash_index service seam.
        # NX_STORAGE_BACKEND_CHASH_INDEX=service routes to HttpChashIndex.
        if storage_backend_for("chash_index") == StorageBackend.SERVICE:
            from nexus.db.t2.http_chash_index import HttpChashIndex
            self.chash_index: ChashIndex = HttpChashIndex()  # type: ignore[assignment]
        else:
            self.chash_index: ChashIndex = ChashIndex(path)
        # RDR-089 Phase 1: per-document structured aspect table
        # populated by the document-grain hook chain at every CLI
        # ingest site (knowledge__* only in Phase 1).
        # RDR-152 nexus-gmiaf.15: document_aspects service seam.
        if storage_backend_for("document_aspects") == StorageBackend.SERVICE:
            from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
            self.document_aspects: DocumentAspects = HttpDocumentAspectsStore()  # type: ignore[assignment]
        else:
            self.document_aspects: DocumentAspects = DocumentAspects(path)
        # RDR-089 follow-up (nexus-qeo8): durable queue feeding the
        # async aspect-extraction worker. The hook fires fast (just
        # an enqueue); the worker drains in a background thread.
        # RDR-138 T1.1: inject RENAME_LOCK so the queue shares the same
        # lock instance as the cascade. T1.2 will wrap mutator bodies.
        # RDR-152 nexus-gmiaf.15: aspect_queue service seam.
        if storage_backend_for("aspect_queue") == StorageBackend.SERVICE:
            from nexus.db.t2.http_aspect_queue import HttpAspectQueue
            self.aspect_queue: AspectExtractionQueue = HttpAspectQueue(  # type: ignore[assignment]
                rename_lock=self.RENAME_LOCK
            )
        else:
            self.aspect_queue: AspectExtractionQueue = AspectExtractionQueue(
                path, rename_lock=self.RENAME_LOCK
            )
        # RDR-139 Layer E: per-document DEVONthink highlight/mention notes,
        # keyed by tumbler. Dedicated table (NOT document_aspects) so
        # free-text highlights never contend with the aspect worker's
        # whole-row overwrite or its confidence gate.
        # RDR-152 nexus-gmiaf.15: document_highlights service seam.
        if storage_backend_for("document_highlights") == StorageBackend.SERVICE:
            from nexus.db.t2.http_document_highlights_store import HttpDocumentHighlightsStore
            self.document_highlights: DocumentHighlights = HttpDocumentHighlightsStore()  # type: ignore[assignment]
        else:
            self.document_highlights: DocumentHighlights = DocumentHighlights(path)

        # RDR-120 P5.A.1 (nexus-9zmpl): catalog is the eighth domain
        # store. Constructed lazily via the ``catalog`` property so
        # the ``.catalog.db`` file is not opened on every T2Database
        # construction (tests that never touch the catalog stay
        # isolated). Caller may pin ``catalog_db_path`` explicitly to
        # avoid the default resolution through ``nexus.config``.
        self._catalog_db_path_override: Path | None = catalog_db_path
        self._catalog: Any = None

    @property
    def catalog(self) -> "CatalogStore":
        """Lazy-construct the eighth domain store on first access.

        Resolution order for the catalog file path:

        1. Explicit ``catalog_db_path`` argument passed to
           :meth:`T2Database.__init__`.
        2. ``nexus.config.catalog_path()/.catalog.db`` (production default).
        """
        if self._catalog is None:
            from nexus.db.t2.catalog import CatalogStore as _CatalogStore

            if self._catalog_db_path_override is not None:
                db_path = self._catalog_db_path_override
            else:
                from nexus.config import catalog_path as _catalog_path
                db_path = _catalog_path() / ".catalog.db"
            self._catalog = _CatalogStore(db_path)
        return self._catalog

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

    @staticmethod
    def bootstrap_schema(path: Path) -> None:
        """Run ``apply_pending`` against *path*.

        RDR-120 P3b: lifted out of ``__init__`` so the T2 daemon is the
        sole substrate-owner that runs migrations in steady state.
        ``nx upgrade`` and the test conftest also call this directly.

        Idempotent: subsequent calls against the same resolved path
        short-circuit via the ``_upgrade_done`` set in
        :mod:`nexus.db.migrations`.
        """
        from nexus.db.migrations import (
            _upgrade_done,
            _upgrade_lock,
            t2_migration_flock,
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path_key = str(path.resolve())
        except OSError:
            path_key = str(path)

        # Fast path: already migrated in this process. Cheap check under the
        # in-process lock only — no cross-process flock needed.
        with _upgrade_lock:
            if path_key in _upgrade_done:
                return

        # RDR-140 P1.2 (nexus-2p52a) Gap 4: cold-start (cross-process) fast
        # path. When a fresh process meets a DB that is already at
        # current_version AND already in WAL, the existing flock-wait +
        # connection-open + no-op apply_pending below is pure overhead that
        # still takes SQLite's writer lock (PRAGMA journal_mode=WAL is a write
        # when the file is not WAL, and bootstrap_version writes the version
        # table). A3 (T2 rdr/140-research-A3, verified) proved both probes are
        # lock-free: SELECT value FROM _nexus_version (mirrors
        # ``stored_schema_version``) and PRAGMA journal_mode read succeed even
        # under a held EXCLUSIVE writer lock. Short-circuit ONLY when BOTH
        # conditions hold; anything else (missing/0.0.0 row, non-WAL journal,
        # or any read error) falls through to the unchanged flock+migration
        # path so a genuine pending migration always runs (no silent fallback
        # for correctness, mem:feedback_no_silent_fallbacks_for_correctness).
        if _cold_start_is_current_and_wal(path):
            with _upgrade_lock:
                # Double-check (mirrors the full-migration path below): another
                # thread may have finished a migration for this path while we
                # ran the lock-free probe.
                if path_key in _upgrade_done:
                    return
                _upgrade_done.add(path_key)
            return

        # RDR-128 P2: acquire the cross-process migration flock BEFORE the
        # in-process ``_upgrade_lock`` so the lock order matches ``nx upgrade``
        # (flock -> _upgrade_lock, via apply_pending). Consistent ordering
        # means the daemon-startup and upgrade migration paths cannot deadlock
        # even if ever run concurrently in one process (code-review finding).
        # The flock also serializes the two paths cross-process, replacing the
        # old WAL free-for-all.
        with t2_migration_flock(path.parent):
            with _upgrade_lock:
                # Double-check: another thread may have completed the migration
                # while we blocked on the flock.
                if path_key in _upgrade_done:
                    return
                try:
                    from importlib.metadata import version as _pkg_version

                    current_version = _pkg_version("conexus")
                except Exception:
                    current_version = "0.0.0"

                import sys as _sys
                if hasattr(_sys.stderr, "isatty") and _sys.stderr.isatty():
                    print(
                        f"Migrating database {path.name!r} to schema "
                        f"version {current_version} ...",
                        file=_sys.stderr,
                    )

                conn = sqlite3.connect(str(path), check_same_thread=False)
                try:
                    # RDR-128 P0a (RF-3): tolerate a concurrent writer (e.g.
                    # `nx index repo`) holding the WAL writer lock — wait it
                    # out via a 30s busy_timeout plus a bounded retry, rather
                    # than crashing on `database is locked`. busy_timeout is a
                    # connection-local pragma that never blocks; journal_mode
                    # =WAL + apply_pending run inside the retry helper since
                    # both can take the writer lock.
                    conn.execute(f"PRAGMA busy_timeout={_BOOTSTRAP_BUSY_TIMEOUT_MS}")
                    _apply_pending_with_lock_retry(conn, current_version)
                finally:
                    conn.close()
                # Mirror the T2Database-form path_key into _upgrade_done so
                # a second construction with the same Path argument
                # short-circuits without re-opening the connection
                # (nexus-avwe — CI path-resolution edge cases).
                _upgrade_done.add(path_key)

    def __enter__(self) -> "T2Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close all domain connections.

        Each store closes its own connection under its own lock. The
        close order is reverse of construction so the most recently
        opened connection is released first. The ``catalog`` store
        is only closed when it was actually constructed (lazy
        property — never opened means never to close).
        """
        # RDR-120 P5.A.1: close catalog only if it was materialised.
        if self._catalog is not None:
            try:
                self._catalog.close()
            except Exception:  # noqa: BLE001
                pass
            self._catalog = None
        # Reverse-construction order: document_highlights was built after
        # aspect_queue (RDR-139 Layer E), so it closes first.
        self.document_highlights.close()
        self.aspect_queue.close()
        self.document_aspects.close()
        self.chash_index.close()
        self.telemetry.close()
        self.taxonomy.close()
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

        nexus-nhyh / K4: runs all UPDATEs inside a single SQLite
        transaction on a dedicated shared connection, so no partial-update
        window exists. If any UPDATE raises, the entire transaction is
        rolled back before the exception propagates.

        Tables updated atomically:
          - ``chash_index.physical_collection``
          - ``document_aspects.collection`` (with collision-defense DELETE)
          - ``aspect_extraction_queue.collection`` (with collision-defense DELETE)
          - ``topics.collection`` / ``topic_assignments.source_collection`` /
            ``taxonomy_meta.collection``
          - ``search_telemetry.collection``
          - ``hook_failures.collection`` (if table exists)

        Returns a dict with counts per table. Raises on any failure --
        the SQLite transaction is rolled back automatically.

        Callers (``rename_collection_data_plane``) catch and re-raise as
        ClickException with a non-zero exit code.

        ``_conn`` is a private test-seam parameter. Production callers
        omit it; the method opens a fresh dedicated connection. Tests
        pass a wrapper object to inject mid-cascade failures.

        RDR-138 T1.1 (nexus-tgzvt): acquires ``self.RENAME_LOCK`` for the
        ENTIRE method body — including the connection open, BEGIN, all
        UPDATEs, COMMIT, and connection close. This serializes the cascade
        against every queue/aspect mutator that T1.2 will also guard with
        the same lock. Lock is held across the full SQLite transaction to
        close Gaps 1-3 (aspect-worker observes a consistent view).
        Lock ordering: RENAME_LOCK is the outermost lock. The cascade
        bypasses all per-store ``self._lock`` regions by design (own conn).
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
        """Inner implementation — called only while RENAME_LOCK is held."""
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

        # Open a dedicated connection to the shared database file. All
        # UPDATEs run in one BEGIN...COMMIT -- SQLite rolls back the
        # entire transaction automatically if we close without committing.
        owned = _conn is None
        conn: sqlite3.Connection
        if owned:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.execute(f"PRAGMA busy_timeout={SERVING_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
        else:
            conn = _conn  # type: ignore[assignment]

        try:
            conn.execute("BEGIN")

            # chash_index (with collision defense).
            # In service mode, self.chash_index is an HttpChashIndex whose
            # rename_collection() calls the remote service endpoint. The raw
            # SQL UPDATE on the shared SQLite file would diverge from the
            # service's Postgres tables, so we route through the domain-store
            # API when the seam is active (same pattern as taxonomy below).
            from nexus.db.storage_mode import StorageBackend, storage_backend_for
            if storage_backend_for("chash_index") == StorageBackend.SERVICE:
                counts["chash"] = self.chash_index.rename_collection(old=old, new=new)
            else:
                conn.execute(
                    "DELETE FROM chash_index "
                    "WHERE physical_collection = ? "
                    "  AND chash IN ("
                    "    SELECT chash FROM chash_index WHERE physical_collection = ?"
                    "  )",
                    (new, old),
                )
                cur = conn.execute(
                    "UPDATE chash_index SET physical_collection = ? "
                    "WHERE physical_collection = ?",
                    (new, old),
                )
                counts["chash"] = cur.rowcount

            # document_aspects (with collision defense). #1057: dedup on the
            # live PRIMARY KEY, which differs by migration state. RDR-108
            # Phase 1c migrates the PK (collection, source_path) -> (doc_id)
            # and [4.31.0] RDR-096 P5.2 then DROPS source_path — but both are
            # deferred until a catalog exists, so unmigrated DBs still carry
            # source_path as the PK with no doc_id column. The old hardcoded
            # source_path dedup raised "no such column: source_path" on
            # migrated DBs and blocked ALL renames; a hardcoded doc_id would
            # symmetrically break unmigrated DBs. Resolve the column from the
            # live schema so both shapes work and dedup matches the real PK.
            aspects_key = _rename_dedup_col(conn, "document_aspects")
            conn.execute(
                f"DELETE FROM document_aspects "
                f"WHERE collection = ? "
                f"  AND {aspects_key} IN ("
                f"    SELECT {aspects_key} FROM document_aspects WHERE collection = ?"
                f"  )",
                (new, old),
            )
            cur = conn.execute(
                "UPDATE document_aspects SET collection = ? WHERE collection = ?",
                (new, old),
            )
            counts["aspects"] = cur.rowcount

            # aspect_extraction_queue (with collision defense). Same PK
            # migration (RDR-108 Phase 1c) applies; its source_path column is
            # NOT dropped, so the old source_path dedup did not error — but it
            # shared the latent bug of deduping on a non-PK column once the PK
            # moved to doc_id (a same-doc_id/different-source_path pair would
            # hit a PK collision on the UPDATE). Resolve the live PK column.
            queue_key = _rename_dedup_col(conn, "aspect_extraction_queue")
            conn.execute(
                f"DELETE FROM aspect_extraction_queue "
                f"WHERE collection = ? "
                f"  AND {queue_key} IN ("
                f"    SELECT {queue_key} FROM aspect_extraction_queue"
                f"    WHERE collection = ?"
                f"  )",
                (new, old),
            )
            cur = conn.execute(
                "UPDATE aspect_extraction_queue "
                "SET collection = ? WHERE collection = ?",
                (new, old),
            )
            counts["aspect_queue"] = cur.rowcount

            # document_highlights (RDR-139 Layer E). PK is doc_id (tumbler),
            # so the denorm collection column cannot collide on rename — a
            # plain UPDATE suffices (no collision-defense DELETE needed).
            cur = conn.execute(
                "UPDATE document_highlights SET collection = ? WHERE collection = ?",
                (new, old),
            )
            counts["highlights"] = cur.rowcount

            # taxonomy (three sub-tables)
            # In service mode, self.taxonomy is an HttpTaxonomyStore whose
            # rename_collection() calls the remote service endpoint.  The
            # raw SQL UPDATE on the shared SQLite file would diverge from
            # the service's Postgres tables, so we route through the
            # domain-store API when the seam is active.
            from nexus.db.storage_mode import StorageBackend, storage_backend_for
            if storage_backend_for("taxonomy") == StorageBackend.SERVICE:
                tax_counts = self.taxonomy.rename_collection(old, new)
                counts["tax_topics"] = tax_counts.get("topics", 0)
                counts["tax_assignments"] = tax_counts.get("assignments", 0)
                counts["tax_meta"] = tax_counts.get("meta", 0)
            else:
                cur = conn.execute(
                    "UPDATE topics SET collection = ? WHERE collection = ?",
                    (new, old),
                )
                counts["tax_topics"] = cur.rowcount
                cur = conn.execute(
                    "UPDATE topic_assignments SET source_collection = ? "
                    "WHERE source_collection = ?",
                    (new, old),
                )
                counts["tax_assignments"] = cur.rowcount
                cur = conn.execute(
                    "UPDATE taxonomy_meta SET collection = ? WHERE collection = ?",
                    (new, old),
                )
                counts["tax_meta"] = cur.rowcount

            # search_telemetry
            cur = conn.execute(
                "UPDATE search_telemetry SET collection = ? WHERE collection = ?",
                (new, old),
            )
            counts["search_telemetry"] = cur.rowcount

            # hook_failures (optional table -- created by migration)
            table_exists = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='hook_failures'"
            ).fetchone()[0]
            if table_exists:
                cur = conn.execute(
                    "UPDATE hook_failures SET collection = ? WHERE collection = ?",
                    (new, old),
                )
                counts["hook_failures"] = cur.rowcount

            conn.commit()

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            if owned:
                conn.close()

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
        from nexus.db.t2.document_aspects import AspectRecord
        record = AspectRecord(**record_fields)
        with self.RENAME_LOCK:
            upserted = self.document_aspects.upsert(record)
            self.aspect_queue.mark_done(record.collection, record.source_path)
        return upserted
