# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P-1a (nexus-0wz93): the T2 ``migrate all`` orchestration as a
library callable.

Before this module, the seven-store ladder run + report build + count
verification lived inside ``commands/storage_cmd.migrate_all_cmd`` — CLI
bound, ~300 LOC, with the verification shelling out to ``psql``. RDR-159
needs the orchestration as an importable callable (the guided
``nx upgrade --migrate`` engine and the conexus ``conexus upgrade`` veneer
both drive it), and RDR-152 bars a direct Python PG connection — so the
psql count check is replaced by an injected :class:`CountSource` whose
default counts through the service REST endpoint
(``POST /v1/catalog/verify/relation-counts``).

The CLI is now a thin wrapper over :func:`migrate_all`: it constructs the
sources, supplies a progress callback that ``click.echo``-es, persists the
returned report, and maps the report's gates (``total_failed == 0`` and the
verification verdict) onto exit codes. There is exactly one orchestration
code path.
"""
from __future__ import annotations

import contextlib
import dis
import importlib
import os
import uuid
from typing import Any, Callable, Protocol

import structlog

from nexus.migration.etl_registry import EtlSources, StoreEtl, ordered
from nexus.migration.migration_report import IssueCollector, build_report

_log = structlog.get_logger(__name__)

#: Report ``(store, table)`` → fully-qualified PG relation, for the count
#: verification. Only relations with a 1:1 row mapping appear here; tables
#: whose write counts do not map cleanly to a single relation are left
#: unchecked (an unmapped table is NOT a pass — see :func:`verify_counts`).
#:
#: nexus-d583z (a): the catalog keys map to ``nexus.catalog_documents`` /
#: ``nexus.catalog_links`` (the real schema relations), NOT ``nexus.documents``.
_VERIFY_TABLES: dict[tuple[str, str], str] = {
    ("memory", "memory"): "nexus.memory",
    ("plans", "plans"): "nexus.plans",
    ("taxonomy", "topics"): "nexus.topics",
    ("taxonomy", "topic_assignments"): "nexus.topic_assignments",
    ("taxonomy", "topic_links"): "nexus.topic_links",
    ("telemetry", "hook_failures"): "nexus.hook_failures",
    ("telemetry", "nx_answer_runs"): "nexus.nx_answer_runs",
    ("chash", "chash_index"): "nexus.chash_index",
    ("catalog", "owners"): "nexus.catalog_owners",
    ("catalog", "documents"): "nexus.catalog_documents",
    ("catalog", "collections"): "nexus.catalog_collections",
    ("catalog", "document_chunks"): "nexus.catalog_document_chunks",
    ("catalog", "links"): "nexus.catalog_links",
}

#: Relations whose unique key collapses source rows server-side via
#: ``ON CONFLICT DO UPDATE`` (nexus-d583z c): ``nexus.plans`` is keyed
#: ``UNIQUE (tenant_id, project, query)``, so a landed count BELOW the
#: written (ack) count is convergence-by-design, not data loss.
#:
#: RDR-176 Gap 1a: the three catalog relations added above
#: (owners/collections/document_chunks) are deliberately NOT in this set. Their
#: import is INSERT-or-preserve, never delete: owners/document_chunks use
#: ``DO UPDATE`` on a key that maps 1:1 to a distinct source row (no collapse),
#: and collections uses a conditional ``DO UPDATE`` that only upgrades pre-
#: existing stubs (stubs inflate ``pg_count`` above ``written``, never deflate).
#: ``pg_count >= written`` is therefore structurally guaranteed, so the strict
#: ``pg_count < written`` mismatch check is correct and a real short copy is
#: caught (verified by code-review-expert, 2026-06-29). A future catalog table
#: whose SQLite source can carry duplicates under the PG unique key MUST be
#: added here, or a by-design convergence collapse would read as a false
#: mismatch.
_VERIFY_TABLES_DEDUP: frozenset[str] = frozenset({"nexus.plans"})


class CountSource(Protocol):
    """Source of authoritative post-migration PG row counts.

    Returns ``{relation: count}`` for the requested relations, or ``None``
    when the count cannot be obtained (an unreachable service, missing
    credentials, …). ``None`` resolves to an INDETERMINATE verification —
    a loud warning, never a silent pass (nexus-r0esi).
    """

    def counts(self, relations: list[str]) -> dict[str, int] | None: ...


class ServiceCountSource:
    """Default :class:`CountSource`: counts via the service REST endpoint.

    RDR-152 bars a direct Python PG connection, so verification routes
    through ``HttpCatalogClient.relation_counts`` (tenant-scoped counts
    under the service role) instead of the legacy psql shell-out. Any
    failure is swallowed into ``None`` (→ indeterminate); verification is
    advisory and must never crash the migration or read as a false pass.
    """

    def counts(self, relations: list[str]) -> dict[str, int] | None:
        if not relations:
            return None
        try:
            from nexus.catalog.factory import make_catalog_client_for_migration  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

            # RDR-176 P2 (Gap 3): no-arg → the factory's no-token branch returns
            # a default-resolved catalog client that resolves URL+token config-
            # first via resolve_service_endpoint (env>config>lease), the same
            # unified chain the CLI migrate subcommands use. NOT env-only.
            client = make_catalog_client_for_migration()
            try:
                return client.relation_counts(relations)
            finally:
                with contextlib.suppress(Exception):
                    client.close()
        except Exception as exc:  # noqa: BLE001 — advisory check, never fatal
            _log.warning(
                "migrate_all_verify_count_source_failed", error=str(exc),
            )
            return None


def build_store_etls(sources: EtlSources) -> list[StoreEtl]:
    """The seven RDR-152 ETL adapters registered against the RDR-153 ladder.

    Each runner constructs its HTTP store lazily so a single-store failure
    surfaces inside the orchestrated run, not at registry build time.
    """
    from nexus.migration.etl_registry import StoreEtl as _StoreEtl  # noqa: PLC0415 — deferred per-store ETL import

    def _memory(s: EtlSources, collector: Any) -> dict:
        from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.db.t2.memory_etl import migrate_memory_rows  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        store = HttpMemoryStore()
        try:
            return migrate_memory_rows(s.sqlite_path, store, collector=collector)
        finally:
            store.close()

    def _plans(s: EtlSources, collector: Any) -> dict:
        from nexus.db.t2.http_plan_library import HttpPlanLibrary  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.db.t2.plan_etl import migrate_plan_rows  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        store = HttpPlanLibrary()
        try:
            return migrate_plan_rows(s.sqlite_path, store, collector=collector)
        finally:
            store.close()

    def _telemetry(s: EtlSources, collector: Any) -> dict:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.db.t2.telemetry_etl import migrate_telemetry_rows  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        store = HttpTelemetryStore()
        try:
            return migrate_telemetry_rows(s.sqlite_path, store, collector=collector)
        finally:
            store.close()

    def _taxonomy(s: EtlSources, collector: Any) -> dict:
        from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.db.t2.taxonomy_etl import migrate_taxonomy_rows  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        store = HttpTaxonomyStore()
        try:
            return migrate_taxonomy_rows(s.sqlite_path, store, collector=collector)
        finally:
            store.close()

    def _aspects(s: EtlSources, collector: Any) -> dict:
        # nexus-iy5se: run only document_aspects, highlights, and
        # promotion_log here. The queue import (aspect_extraction_queue) has
        # an FK into catalog_documents and must run AFTER catalog.
        from nexus.db.t2.aspects_etl import migrate_without_queue  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.db.t2.http_document_aspects_store import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
            HttpDocumentAspectsStore,
        )
        from nexus.db.t2.http_document_highlights_store import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
            HttpDocumentHighlightsStore,
        )

        aspects = HttpDocumentAspectsStore()
        highlights = HttpDocumentHighlightsStore()
        try:
            return migrate_without_queue(
                s.sqlite_path, aspects, highlights,
                collector=collector, catalog_db_path=s.catalog_db_path,
            )
        finally:
            for st in (aspects, highlights):
                with contextlib.suppress(Exception):
                    st.close()

    def _aspects_queue(s: EtlSources, collector: Any) -> dict:
        # nexus-iy5se: queue import runs AFTER catalog so catalog_documents
        # is populated and fk_aspect_queue_catalog_doc does not reject valid
        # rows.
        from nexus.db.t2.aspects_etl import migrate_queue  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.db.t2.http_aspect_queue import HttpAspectQueue  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        queue = HttpAspectQueue()
        try:
            return migrate_queue(
                s.sqlite_path, queue,
                collector=collector, catalog_db_path=s.catalog_db_path,
            )
        finally:
            with contextlib.suppress(Exception):
                queue.close()

    def _chash(s: EtlSources, collector: Any) -> dict:
        from nexus.db.t2.chash_etl import migrate_chash_rows  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.db.t2.http_chash_index import HttpChashIndex  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        store = HttpChashIndex()
        try:
            return migrate_chash_rows(s.sqlite_path, store, collector=collector)
        finally:
            with contextlib.suppress(Exception):
                store.close()

    def _catalog(s: EtlSources, collector: Any) -> dict:
        from nexus.catalog.factory import make_catalog_client_for_migration  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        from nexus.db.t2.catalog_etl import migrate_catalog  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        # RDR-176 P2 (Gap 3): no-arg → config-first URL+token resolution (NOT
        # env-only); matches the CLI migrate subcommands and ServiceCountSource.
        client = make_catalog_client_for_migration()
        try:
            return migrate_catalog(
                s.catalog_db_path, client, collector=collector,
            )
        finally:
            client.close()

    # Returned in LADDER_ORDER (nexus-iy5se: aspects_queue trails catalog so
    # its FK into catalog_documents resolves). migrate_all re-imposes order
    # via ordered(), but a direct consumer of this list (the RDR-159 guided
    # engine) must see the correct order too — do not reorder.
    return [
        _StoreEtl("memory", _memory),
        _StoreEtl("plans", _plans),
        _StoreEtl("telemetry", _telemetry),
        _StoreEtl("taxonomy", _taxonomy),
        _StoreEtl("aspects", _aspects),
        _StoreEtl("chash", _chash),
        _StoreEtl("catalog", _catalog),
        _StoreEtl("aspects_queue", _aspects_queue),
    ]


def _written_by_table(report: dict[str, Any]) -> dict[str, int]:
    """Sum the report's written counts per verify-mapped PG relation.

    Only ``(store, table)`` pairs present in :data:`_VERIFY_TABLES` map to a
    relation; everything else is unmapped (and therefore unchecked).
    """
    written: dict[str, int] = {}
    for store in report.get("stores", []):
        for table in store.get("tables", []):
            relation = _VERIFY_TABLES.get((store["store"], table["table"]))
            if relation is not None:
                written[relation] = written.get(relation, 0) + int(table["written"])
    return written


def verify_counts(
    report: dict[str, Any], count_source: CountSource,
) -> tuple[str, list[str], dict[str, int]]:
    """Count-verify the migration against Postgres via *count_source*.

    Returns ``(status, convergence_notes, dest_counts)`` where *status* is one
    of ``"verified"`` | ``"mismatch"`` | ``"indeterminate"`` and *dest_counts*
    is the destination-side (pg) per-relation row counts the source reported
    (``{}`` when nothing was mappable or the source was unreachable). RDR-176
    Gap 5 surfaces *dest_counts* as a first-class observability metric instead
    of discarding it:

    - **indeterminate** — nothing mappable to check, the source returned
      ``None`` (unreachable), or it omitted a requested relation. NEVER a
      pass (nexus-r0esi: an unverifiable migration is a loud warning).
    - **mismatch** — a non-dedup relation landed fewer rows than written,
      or a dedup relation landed zero from a non-zero write / more than
      written.
    - **verified** — every checked relation reconciles (dedup relations
      tolerate convergence collapse, recorded in *convergence_notes*).
    """
    written_by_table = _written_by_table(report)
    if not written_by_table:
        # nothing mappable to verify is NOT a pass
        return "indeterminate", [], {}

    pg_counts = count_source.counts(list(written_by_table))
    if pg_counts is None:
        return "indeterminate", [], {}

    convergence_notes: list[str] = []
    for relation, written in written_by_table.items():
        if relation not in pg_counts:
            # the source could not report this relation → cannot confirm
            return "indeterminate", [], dict(pg_counts)
        pg_count = int(pg_counts[relation])

        if relation in _VERIFY_TABLES_DEDUP:
            # written=0 is a trivial pass (idempotent re-run, nothing new).
            if written > 0 and pg_count == 0:
                _log.error(
                    "migrate_all_verify_mismatch", relation=relation,
                    pg_count=pg_count, report_written=written,
                    note="convergence-aware: 0 rows landed from non-zero write count",
                )
                return "mismatch", [], dict(pg_counts)
            if written > 0 and pg_count > written:
                _log.error(
                    "migrate_all_verify_mismatch", relation=relation,
                    pg_count=pg_count, report_written=written,
                    note="convergence-aware: pg_count exceeds written (impossible under DO UPDATE)",
                )
                return "mismatch", [], dict(pg_counts)
            delta = written - pg_count
            if written > 0 and delta > 0:
                _log.info(
                    "migrate_all_verify_convergence_collapse", relation=relation,
                    pg_count=pg_count, report_written=written, collapsed=delta,
                    note="source-duplicate convergence via DO UPDATE unique key — by design",
                )
                convergence_notes.append(
                    f"{relation}: {pg_count} rows from {written} source rows; "
                    f"{delta} converged onto existing keys by UNIQUE constraint "
                    f"via DO UPDATE — by design"
                )
        else:
            if pg_count < written:
                _log.error(
                    "migrate_all_verify_mismatch", relation=relation,
                    pg_count=pg_count, report_written=written,
                )
                return "mismatch", [], dict(pg_counts)
    return "verified", convergence_notes, dict(pg_counts)


def _etl_import_modules(run: Callable[[EtlSources, Any], dict]) -> tuple[str, ...]:
    """Module names a store's runner imports, read off its own bytecode.

    Every ``build_store_etls`` closure defers its imports (PLC0415) to
    inside the runner body so a single-store failure surfaces there, not at
    registry-build time. This walks the runner's ``IMPORT_NAME`` bytecode
    operands to recover exactly the modules it will import when called — a
    reflection of the ladder's own closures, not a hand-maintained copy that
    could drift from them (nexus-5drgy).
    """
    return tuple(
        instr.argval
        for instr in dis.get_instructions(run.__code__)
        if instr.opname == "IMPORT_NAME"
    )


class EtlPreflightFailed(RuntimeError):
    """Raised before ANY store runs when a ladder step's ETL module cannot
    be imported.

    The 2026-06-30 production migration crashed 6/8 stores mid-run with
    ``ModuleNotFoundError`` because the missing module only surfaced when
    that store's turn came up in the ladder — after earlier stores had
    already written (nexus-5drgy). A migration must be all-runnable or
    not-started, so this check runs before the first store executes and
    aborts the whole run on the first missing module.
    """

    def __init__(self, failures: list[tuple[str, str, str]]) -> None:
        # (store, module, error)
        self.failures = failures
        modules = sorted({module for _, module, _ in failures})
        lines = "\n".join(
            f"  - {store}: {module} — {error}"
            for store, module, error in failures
        )
        super().__init__(
            "migrate-all preflight failed — aborting before any store ran. "
            f"{len(modules)} ETL module(s) could not be imported: "
            f"{', '.join(modules)}. This is almost always a wheel/orchestrator "
            "version skew (the installed package is missing a module the "
            "orchestrator references) — reinstall a consistent build "
            "(e.g. scripts/reinstall-tool.sh) and re-run:\n" + lines
        )


def assert_etls_importable(etls: list[StoreEtl]) -> None:
    """Pre-flight (nexus-5drgy, RDR-178 Gap 1): import every ladder step's
    ETL module BEFORE any store executes.

    Raises :class:`EtlPreflightFailed` naming every unimportable module if
    any is found; imports nothing else and touches no store. Called by
    :func:`migrate_all` ahead of the ladder loop so a version-skewed wheel
    fails loudly up front instead of mid-run.
    """
    failures: list[tuple[str, str, str]] = []
    for etl in etls:
        for module in _etl_import_modules(etl.run):
            try:
                importlib.import_module(module)
            except ImportError as exc:
                failures.append((etl.store, module, str(exc)))
    if failures:
        _log.error(
            "migrate_all_preflight_failed",
            failures=[f"{s}:{m}" for s, m, _ in failures],
        )
        raise EtlPreflightFailed(failures)
    _log.info("migrate_all_preflight_clear", stores=[e.store for e in etls])


def migrate_all(
    sources: EtlSources,
    *,
    count_source: CountSource | None = None,
    on_store: Callable[[str], None] | None = None,
    on_store_failed: Callable[[str, Exception], None] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
    migration_id: str | None = None,
    skip_stores: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Run ALL eight store migrations in RDR-152 ladder order and return ONE
    RDR-153 report dict (with the verification verdict folded in).

    Order: memory → plans → telemetry → taxonomy → aspects → chash →
    catalog → aspects_queue (the last two trail so FK targets exist). One
    shared :class:`IssueCollector` spans the run; a store-level crash is
    recorded and the run continues so the report covers every attempted
    store. The verification verdict is written INTO the report so the
    artifact is self-contained for downstream triage.

    ``on_store(store)`` fires before each store runs; ``on_store_failed(store,
    exc)`` fires when a store crashes (the crash is also recorded in the
    report); ``on_progress(store, written, read)`` fires after each store
    completes SUCCESSFULLY, carrying that store's running written/read counts
    (RDR-176 Gap 5 observability — a long migration is otherwise silent except
    on failure). All three are pure callbacks so the orchestrator never imports
    the CLI's ``click`` — the RDR-159 guided upgrade engine wires its own sink.

    Progress granularity (RDR-176 Gap 5, intentional split): this callback is
    PER-STORE (a rollup fired once the store finishes). PER-BATCH progress is
    NOT a second callback — each ETL already emits per-batch ``*.progress`` INFO
    events to the structlog stream that ``migrate all`` prints, so both
    granularities are observable (per-batch via the log stream, per-store via
    this callback + the report). Threading a per-batch callback through all
    eight ETLs would duplicate the existing per-batch logs.

    ``report["dest_counts"]`` holds the destination-side (pg) per-relation row
    counts the count source reported. It is ``{}`` when verification is
    ``indeterminate`` (nothing mappable, or the count source was unreachable) —
    read it ALONGSIDE ``report["verification"]``: ``{}`` means "unavailable",
    NOT "zero rows landed".

    The report's ``summary.total_failed == 0`` is the hard gate; the
    verification verdict (``verified`` / ``mismatch`` / ``indeterminate``)
    is the advisory count check, with ``report["relations_checked"]`` naming
    how many relations the count source actually reconciled. The caller maps
    both onto its own exit / unlock semantics.

    ``skip_stores`` (RDR-178 Gap 7, nexus-1sx01): store names to skip
    entirely — no ETL call, no read/write counts, not even an
    ``on_store``/``on_progress`` callback. The caller (the guided-upgrade
    already-migrated pre-flight) has independently confirmed these stores
    are already migrated with no newer local writes; re-running them here
    would re-ship data for nothing (the 2026-07-01 incident: 158k catalog
    rows re-sent to patch a 270-row hole). Skipped stores are recorded in
    ``report["skipped_stores"]`` for observability — they carry no read/
    write/failed counts of their own (this is a distinct signal from a
    store that ran and wrote zero rows). Default ``frozenset()`` is fully
    backward compatible — every store always runs.
    """
    etls = ordered(build_store_etls(sources))
    # nexus-5drgy: import every ladder step's ETL module BEFORE any store
    # runs or any report is built — a version-skewed wheel must abort the
    # ENTIRE run up front, never mid-run after earlier stores have written.
    # Skipped stores (Gap 7) never run, so their ETL modules are excluded:
    # an import breakage in an already-migrated store must not block
    # genuinely-pending stores (wave-1 composed review, 2026-07-02).
    assert_etls_importable([e for e in etls if e.store not in skip_stores])

    collector = IssueCollector()
    mig_id = migration_id or str(uuid.uuid4())
    skipped: list[str] = []

    for etl in etls:
        if etl.store in skip_stores:
            skipped.append(etl.store)
            _log.info("migrate_all.store_skipped_already_migrated", store=etl.store)
            continue
        if on_store is not None:
            on_store(etl.store)
        crashed = False
        try:
            etl.run(sources, collector)
        except Exception as exc:  # noqa: BLE001 — recorded, never silent
            crashed = True
            collector.record_event(
                etl.store, etl.store,
                issue_class="unexpected", constraint=etl.store,
                reason=f"store-level ETL crash: {exc}", action="failed",
            )
            if on_store_failed is not None:
                on_store_failed(etl.store, exc)
        # RDR-176 Gap 5: per-store progress signal once the store COMPLETES —
        # the running written/read counts the collector accumulated for it. The
        # ETLs already emit per-batch INFO; this makes the orchestrator emit a
        # per-store rollup (INFO + callback) so `migrate all` is not silent.
        # Suppressed on a crash: on_store_failed is the authoritative signal
        # there, and a "0 written / 0 read" line would misread as "completed
        # empty" rather than "crashed" (code-review-expert, 2026-06-30).
        if not crashed:
            s_written = sum(
                collector.table_counts(etl.store, t)["written"]
                for t in collector.tables_for(etl.store)
            )
            s_read = sum(
                collector.table_counts(etl.store, t)["read"]
                for t in collector.tables_for(etl.store)
            )
            _log.info(
                "migrate_all.store_progress",
                store=etl.store, written=s_written, read=s_read,
            )
            if on_progress is not None:
                on_progress(etl.store, s_written, s_read)

    report = build_report(
        collector,
        source={
            "sqlite": str(sources.sqlite_path),
            "catalog_db": str(sources.catalog_db_path),
        },
        target={"service_url": os.environ.get("NX_SERVICE_URL", "(lease)")},
        migration_id=mig_id,
    )
    verification, convergence_notes, dest_counts = verify_counts(
        report, count_source or ServiceCountSource(),
    )
    report["verification"] = verification
    # RDR-176 Gap 5: surface the destination-side (pg) row counts as a first-
    # class metric so "did rows actually land?" is answerable from the report,
    # not by paginating the read API.
    report["dest_counts"] = dest_counts
    # How many relations the count check actually reconciled (the mappable
    # subset present in this run) — surfaced so the operator artifact and the
    # CLI banner can name coverage instead of a vague "mappable relations".
    report["relations_checked"] = len(_written_by_table(report))
    if convergence_notes:
        report["verification_convergence_notes"] = convergence_notes
    if skipped:
        report["skipped_stores"] = skipped
    return report
