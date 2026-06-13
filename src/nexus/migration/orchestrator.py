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
    ("catalog", "documents"): "nexus.catalog_documents",
    ("catalog", "links"): "nexus.catalog_links",
}

#: Relations whose unique key collapses source rows server-side via
#: ``ON CONFLICT DO UPDATE`` (nexus-d583z c): ``nexus.plans`` is keyed
#: ``UNIQUE (tenant_id, project, query)``, so a landed count BELOW the
#: written (ack) count is convergence-by-design, not data loss.
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
            from nexus.catalog.factory import make_catalog_client_for_migration

            token = os.environ.get("NX_SERVICE_TOKEN", "")
            client = make_catalog_client_for_migration(base_url=None, token=token)
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
    from nexus.migration.etl_registry import StoreEtl as _StoreEtl

    def _memory(s: EtlSources, collector: Any) -> dict:
        from nexus.db.t2.http_memory_store import HttpMemoryStore
        from nexus.db.t2.memory_etl import migrate_memory_rows

        store = HttpMemoryStore()
        try:
            return migrate_memory_rows(s.sqlite_path, store, collector=collector)
        finally:
            store.close()

    def _plans(s: EtlSources, collector: Any) -> dict:
        from nexus.db.t2.http_plan_library import HttpPlanLibrary
        from nexus.db.t2.plan_etl import migrate_plan_rows

        store = HttpPlanLibrary()
        try:
            return migrate_plan_rows(s.sqlite_path, store, collector=collector)
        finally:
            store.close()

    def _telemetry(s: EtlSources, collector: Any) -> dict:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
        from nexus.db.t2.telemetry_etl import migrate_telemetry_rows

        store = HttpTelemetryStore()
        try:
            return migrate_telemetry_rows(s.sqlite_path, store, collector=collector)
        finally:
            store.close()

    def _taxonomy(s: EtlSources, collector: Any) -> dict:
        from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
        from nexus.db.t2.taxonomy_etl import migrate_taxonomy_rows

        store = HttpTaxonomyStore()
        try:
            return migrate_taxonomy_rows(s.sqlite_path, store, collector=collector)
        finally:
            store.close()

    def _aspects(s: EtlSources, collector: Any) -> dict:
        # nexus-iy5se: run only document_aspects, highlights, and
        # promotion_log here. The queue import (aspect_extraction_queue) has
        # an FK into catalog_documents and must run AFTER catalog.
        from nexus.db.t2.aspects_etl import migrate_without_queue
        from nexus.db.t2.http_document_aspects_store import (
            HttpDocumentAspectsStore,
        )
        from nexus.db.t2.http_document_highlights_store import (
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
        from nexus.db.t2.aspects_etl import migrate_queue
        from nexus.db.t2.http_aspect_queue import HttpAspectQueue

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
        from nexus.db.t2.chash_etl import migrate_chash_rows
        from nexus.db.t2.http_chash_index import HttpChashIndex

        store = HttpChashIndex()
        try:
            return migrate_chash_rows(s.sqlite_path, store, collector=collector)
        finally:
            with contextlib.suppress(Exception):
                store.close()

    def _catalog(s: EtlSources, collector: Any) -> dict:
        from nexus.catalog.factory import make_catalog_client_for_migration
        from nexus.db.t2.catalog_etl import migrate_catalog

        token = os.environ.get("NX_SERVICE_TOKEN", "")
        client = make_catalog_client_for_migration(base_url=None, token=token)
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
) -> tuple[str, list[str]]:
    """Count-verify the migration against Postgres via *count_source*.

    Returns ``(status, convergence_notes)`` where *status* is one of
    ``"verified"`` | ``"mismatch"`` | ``"indeterminate"``:

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
        return "indeterminate", []

    pg_counts = count_source.counts(list(written_by_table))
    if pg_counts is None:
        return "indeterminate", []

    convergence_notes: list[str] = []
    for relation, written in written_by_table.items():
        if relation not in pg_counts:
            # the source could not report this relation → cannot confirm
            return "indeterminate", []
        pg_count = int(pg_counts[relation])

        if relation in _VERIFY_TABLES_DEDUP:
            # written=0 is a trivial pass (idempotent re-run, nothing new).
            if written > 0 and pg_count == 0:
                _log.error(
                    "migrate_all_verify_mismatch", relation=relation,
                    pg_count=pg_count, report_written=written,
                    note="convergence-aware: 0 rows landed from non-zero write count",
                )
                return "mismatch", []
            if written > 0 and pg_count > written:
                _log.error(
                    "migrate_all_verify_mismatch", relation=relation,
                    pg_count=pg_count, report_written=written,
                    note="convergence-aware: pg_count exceeds written (impossible under DO UPDATE)",
                )
                return "mismatch", []
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
                return "mismatch", []
    return "verified", convergence_notes


def migrate_all(
    sources: EtlSources,
    *,
    count_source: CountSource | None = None,
    on_store: Callable[[str], None] | None = None,
    on_store_failed: Callable[[str, Exception], None] | None = None,
    migration_id: str | None = None,
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
    report). Both are pure callbacks so the orchestrator never imports the
    CLI's ``click`` — the RDR-159 guided upgrade engine wires its own sink.

    The report's ``summary.total_failed == 0`` is the hard gate; the
    verification verdict (``verified`` / ``mismatch`` / ``indeterminate``)
    is the advisory count check, with ``report["relations_checked"]`` naming
    how many relations the count source actually reconciled. The caller maps
    both onto its own exit / unlock semantics.
    """
    collector = IssueCollector()
    mig_id = migration_id or str(uuid.uuid4())

    for etl in ordered(build_store_etls(sources)):
        if on_store is not None:
            on_store(etl.store)
        try:
            etl.run(sources, collector)
        except Exception as exc:  # noqa: BLE001 — recorded, never silent
            collector.record_event(
                etl.store, etl.store,
                issue_class="unexpected", constraint=etl.store,
                reason=f"store-level ETL crash: {exc}", action="failed",
            )
            if on_store_failed is not None:
                on_store_failed(etl.store, exc)

    report = build_report(
        collector,
        source={
            "sqlite": str(sources.sqlite_path),
            "catalog_db": str(sources.catalog_db_path),
        },
        target={"service_url": os.environ.get("NX_SERVICE_URL", "(lease)")},
        migration_id=mig_id,
    )
    verification, convergence_notes = verify_counts(
        report, count_source or ServiceCountSource(),
    )
    report["verification"] = verification
    # How many relations the count check actually reconciled (the mappable
    # subset present in this run) — surfaced so the operator artifact and the
    # CLI banner can name coverage instead of a vague "mappable relations".
    report["relations_checked"] = len(_written_by_table(report))
    if convergence_notes:
        report["verification_convergence_notes"] = convergence_notes
    return report
