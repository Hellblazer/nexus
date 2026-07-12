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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

import structlog

from nexus.migration.etl_registry import EtlSources, StoreEtl, ordered
from nexus.migration.migration_report import IssueCollector, build_report

if TYPE_CHECKING:
    from nexus.migration.verify_fill import TableVerdict

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


# ═══════════════════════════════════════════════════════════════════════════
# RDR-178 wave-2 P4 (nexus-s3dd4.5): verify-fill CLI wiring.
#
# ``verify_fill.py`` (P2/P3a) built the outer count-diff loop
# (:func:`~nexus.migration.verify_fill.verify_store_counts`) and the inner
# identity-diff + fill primitives (:func:`~nexus.migration.verify_fill.fill_missing`
# / :func:`~nexus.migration.verify_fill.fill_missing_document_chunks`) as
# pure, client-agnostic functions. This section is P4's job: wire REAL
# clients (HttpChashIndex, HttpCatalogClient) as
# :class:`~nexus.migration.verify_fill.IdentitySource` /
# :class:`~nexus.migration.verify_fill.ManifestSource` implementations, and
# orchestrate the outer-verify -> inner-fill decision per store.
#
# Deferred imports of ``nexus.migration.verify_fill`` throughout this
# section mirror verify_fill.py's OWN deferred imports of THIS module's
# constants (see the R2 cycle-guard comment in verify_fill.py) — the two
# modules import each other's symbols, so both sides must defer to runtime.
# ═══════════════════════════════════════════════════════════════════════════


def resolve_target_service_url(explicit: str | None = None) -> str:
    """Best-effort RESOLVED service URL for a migration report's
    ``target.service_url`` field.

    Report-writer fixup (epic te885 comment 2026-07-02 04:19): both
    :func:`migrate_all` and the CLI's per-store ``_emit_store_report`` used
    to write the literal string ``"(lease)"`` when ``NX_SERVICE_URL`` was
    unset — a placeholder that reads as a real hostname and blinds any
    downstream divergence check on the artifact. This resolves the ACTUAL
    endpoint the same way the migration clients do (env, then config.yml,
    then the supervisor lease), falling back to the loud ``"(unresolved)"``
    marker (never a value that could be mistaken for a real endpoint).
    """
    if explicit:
        return explicit.rstrip("/")
    env_url = os.environ.get("NX_SERVICE_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    try:
        from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        url, _token = resolve_service_endpoint()
        return url
    except Exception:  # noqa: BLE001 — best-effort report label, never fatal
        return "(unresolved)"


def clamp_fill_batch_size(batch_size: int) -> int:
    """Clamp *batch_size* to the ``nexus.db.limits`` ``MAX_RECORDS_PER_WRITE``
    ceiling (300).

    R3 review note 2 (2026-07-02): ``fill_missing`` /
    ``fill_missing_document_chunks`` accept a caller-supplied ``batch_size``
    with NO internal clamp — P4 owns enforcing the ceiling here so an
    oversized caller-supplied value cannot trip the ``/v1/*/import`` quota.
    """
    from nexus.db.limits import QUOTAS  # noqa: PLC0415 — deferred; branch-local quota constant

    return max(1, min(int(batch_size), QUOTAS.MAX_RECORDS_PER_WRITE))


def dedup_convergence_notes(
    store: str, verdicts: dict[str, "TableVerdict"],
) -> list[str]:
    """Recompute convergence notes for dedup tables from a
    :func:`~nexus.migration.verify_fill.verify_store_counts` verdict map.

    R2 critic finding (2026-07-02): ``TableVerdict`` intentionally carries
    no ``convergence_notes`` field of its own. Surface WHY a dedup table
    (e.g. ``nexus.plans``) parity'd despite ``target_count < source_count``
    by recomputing the delta from the verdict's OWN ``source_count`` /
    ``target_count`` pair — never re-derive dedup semantics from scratch.
    Wording mirrors :func:`verify_counts`'s ``convergence_notes`` exactly so
    the two surfaces read identically.
    """
    notes: list[str] = []
    for table, verdict in verdicts.items():
        if verdict["status"] != "parity":
            continue
        relation = _VERIFY_TABLES.get((store, table))
        if relation is None or relation not in _VERIFY_TABLES_DEDUP:
            continue
        src, tgt = verdict["source_count"], verdict["target_count"]
        if tgt is None or src <= 0:
            continue
        delta = src - tgt
        if delta > 0:
            notes.append(
                f"{relation}: {tgt} rows from {src} source rows; {delta} "
                f"converged onto existing keys by UNIQUE constraint via DO "
                f"UPDATE — by design"
            )
    return notes


def _try_fill(
    fn: Callable[[], dict[str, Any]],
    *,
    store: str,
    table_name: str,
    collector: Any,
    recovery: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Wrap ONE inner-fill call so a circuit-breaker give-up records partial
    progress in the migration report instead of crashing the whole
    verify-fill run (R3 review note 1: ``fill_missing`` /
    ``fill_missing_document_chunks`` propagate a breaker give-up WITHOUT a
    partial ``FillResult`` — that loss happens INSIDE ``verify_fill.py``,
    which P4 does not own, so recovery here is necessarily a best-effort
    RE-PROBE of the identity surface after the failure, not a reconstruction
    of ``verify_fill.py``'s internal state).

    *recovery* (optional) computes a best-effort partial result after the
    failure (typically re-probing the target's identity surface — every
    batch that succeeded before the breaker gave up already landed,
    idempotent writes, so the re-probe reflects rows genuinely confirmed
    present). When omitted, the failure is still recorded (never silent)
    with ``filled=0``. Never re-raises: the caller continues with the next
    unit of work; the failure is recorded into *collector* so it still
    gates ``summary.total_failed``.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — R3 note 1: a breaker give-up must not crash verify-fill; record + continue
        _log.error("verify_fill.fill_call_failed", table=table_name, error=str(exc))
        partial = (
            recovery()
            if recovery is not None
            else {
                "source_count": 0, "target_count": None, "missing": None,
                "filled": 0, "status": "indeterminate",
            }
        )
        if collector is not None:
            collector.record_event(
                store, table_name,
                issue_class="unexpected", constraint=table_name,
                reason=(
                    f"verify-fill circuit breaker gave up: {exc} — "
                    f"filled={partial.get('filled', 0)} confirmed landed "
                    f"before giving up"
                ),
                action="failed",
            )
        return partial


def _recover_flat_fill(
    identity_source: Any,
    source_rows: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    """Best-effort partial :class:`~nexus.migration.verify_fill.FillResult`
    after a breaker give-up: re-probe *identity_source* and count how many
    of *source_rows* are NOW present. Used as :func:`_try_fill`'s
    ``recovery`` for the flat (single identity-set) fill shapes — chash
    (per collection), catalog owners, catalog collections."""
    try:
        post_present = identity_source.present()
    except Exception:  # noqa: BLE001 — best-effort recovery probe only
        post_present = None
    if post_present is None:
        return {
            "source_count": len(source_rows), "target_count": None,
            "missing": None, "filled": 0, "status": "indeterminate",
        }
    filled = sum(1 for r in source_rows if key_fn(r) in post_present)
    return {
        "source_count": len(source_rows), "target_count": len(post_present),
        "missing": len(source_rows) - filled, "filled": filled,
        "status": "indeterminate",
    }


# ── chash: real IdentitySource + fill orchestration ──────────────────────────


class _ChashCollectionIdentitySource:
    """Real :class:`~nexus.migration.verify_fill.IdentitySource` wiring for
    ``chash_index``, scoped to ONE physical_collection (chash's identity
    surface — ``registered_chashes_for_collection`` — is collection-scoped;
    the same chash value can legitimately appear in multiple collections, so
    the diff must not conflate them)."""

    def __init__(self, http_chash: Any, collection: str) -> None:
        self._http_chash = http_chash
        self._collection = collection

    def present(self) -> set[str] | None:
        try:
            return self._http_chash.registered_chashes_for_collection(self._collection)
        except Exception as exc:  # noqa: BLE001 — unreachable surface -> indeterminate (verify_fill's own documented contract)
            _log.warning(
                "verify_fill.chash_identity_unreachable",
                collection=self._collection, error=str(exc),
            )
            return None


def _read_chash_rows_by_collection(sqlite_path: Path) -> dict[str, list[dict[str, str]]]:
    """Group SQLite ``chash_index`` rows by ``physical_collection`` — chash's
    natural fill scope, mirroring ``chash_etl.migrate_chash_rows``'s own
    read query (never duplicated transform logic, just the SELECT)."""
    import sqlite3  # noqa: PLC0415 — deliberate deferred import: branch-local / startup-cost avoidance

    conn = sqlite3.connect(str(sqlite_path), check_same_thread=False)  # epsilon-allow: ETL source-read; sqlite_path is the migration SOURCE SQLite, never T2Database
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT chash, physical_collection, created_at FROM chash_index"
        ).fetchall()
    finally:
        conn.close()
    by_collection: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        chash = row["chash"] or ""
        collection = row["physical_collection"] or ""
        if not chash or not collection:
            continue
        by_collection.setdefault(collection, []).append({
            "chash": chash,
            "collection": collection,
            "created_at": row["created_at"] or "1970-01-01T00:00:00Z",
        })
    return by_collection


def _chash_import_fn(http_chash: Any) -> Callable[[list[dict[str, Any]]], Any]:
    # nexus-f2qvx.3: previously reached into ``http_chash._client.post(...)``
    # directly — a pre-mixin-adoption wart. HttpChashIndex.import_rows() is
    # the public wrapper (routes through RefreshableHttpStoreMixin's
    # self-healing _post); the raw ``._client`` call would break
    # post-adoption since the mixin's httpx.Client has no baked base_url.
    def _import(batch: list[dict[str, Any]]) -> Any:
        return http_chash.import_rows(batch)
    return _import


def verify_fill_chash(
    sqlite_path: Path,
    http_chash: Any,
    *,
    count_source: "CountSource | None" = None,
    batch_size: int = 200,
    breaker: Any = None,
    collector: Any = None,
) -> dict[str, Any]:
    """RDR-178 P4: verify-fill (delta) for the ``chash`` store.

    Runs the outer count-diff against ``chash_index``'s total row count; on
    parity the fill is skipped entirely (zero HTTP writes — the design's
    "no-op verify = one HTTP call" goal). On divergence OR indeterminacy
    (verify_fill.py's documented contract: an indeterminate outer verdict is
    treated the same as divergent for safety) runs the inner
    :func:`~nexus.migration.verify_fill.fill_missing` PER PHYSICAL
    COLLECTION — chash's identity surface is collection-scoped.

    A per-collection circuit-breaker give-up is caught via :func:`_try_fill`
    and converted into a partial-progress record rather than aborting the
    whole command.
    """
    from nexus.migration.verify_fill import fill_missing, verify_store_counts  # noqa: PLC0415 — R2 import-cycle guard, mirrored from verify_fill.py
    from nexus.retry import EtlCircuitBreaker  # noqa: PLC0415 — deferred to avoid CLI startup cost

    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    batch_size = clamp_fill_batch_size(batch_size)
    cs = count_source or ServiceCountSource()

    by_collection = _read_chash_rows_by_collection(sqlite_path)
    total_rows = sum(len(rows) for rows in by_collection.values())

    outer_verdicts = verify_store_counts(
        "chash", cs, {"chash_index": total_rows},
    )
    outer_status = outer_verdicts.get("chash_index", {}).get("status", "indeterminate")

    fill_by_collection: dict[str, Any] = {}
    total_filled = 0
    if outer_status != "parity":
        for collection, rows in sorted(by_collection.items()):
            key_fn: Callable[[dict[str, Any]], str] = lambda r: (r["chash"] or "")[:32]  # noqa: E731
            identity_source = _ChashCollectionIdentitySource(http_chash, collection)
            result = _try_fill(
                lambda: fill_missing(  # noqa: B023 — invoked immediately by _try_fill, not deferred past this iteration
                    source_rows=rows,
                    key_fn=key_fn,
                    identity_source=identity_source,
                    import_fn=_chash_import_fn(http_chash),
                    batch_size=batch_size,
                    breaker=breaker,
                    table=f"chash_index[{collection}]",
                ),
                store="chash", table_name="chash_index", collector=collector,
                recovery=lambda: _recover_flat_fill(identity_source, rows, key_fn),  # noqa: B023
            )
            fill_by_collection[collection] = result
            total_filled += result["filled"]

    if collector is not None:
        collector.count_read("chash", "chash_index", total_rows)
        collector.count_written("chash", "chash_index", total_filled)

    return {
        "store": "chash",
        "outer": outer_verdicts,
        "fill": fill_by_collection,
        "total_filled": total_filled,
        "convergence_notes": dedup_convergence_notes("chash", outer_verdicts),
    }


# ── catalog: real IdentitySource/ManifestSource + fill orchestration ─────────


class _CatalogFlatIdentitySource:
    """Real :class:`~nexus.migration.verify_fill.IdentitySource` wiring for
    a flat catalog listing endpoint (owners: ``list_owners()``/
    ``tumbler_prefix``; collections: ``list_collections()``/``name``)."""

    def __init__(self, fetch: Callable[[], list[dict[str, Any]]], key: str) -> None:
        self._fetch = fetch
        self._key = key

    def present(self) -> set[str] | None:
        try:
            rows = self._fetch()
        except Exception as exc:  # noqa: BLE001 — unreachable surface -> indeterminate
            _log.warning(
                "verify_fill.catalog_identity_unreachable", key=self._key, error=str(exc),
            )
            return None
        return {r.get(self._key) for r in rows if r.get(self._key)}


class _CatalogChashIdentitySource:
    """Real :class:`~nexus.migration.verify_fill.IdentitySource` wiring for
    ``document_chunks``'s collection-level chash pre-filter
    (``HttpCatalogClient.chashes_for_collection`` returns ``set[str]`` —
    R2 wiring note, accepted verbatim)."""

    def __init__(self, client: Any, collection: str) -> None:
        self._client = client
        self._collection = collection

    def present(self) -> set[str] | None:
        try:
            return self._client.chashes_for_collection(self._collection)
        except Exception as exc:  # noqa: BLE001 — unreachable surface -> indeterminate
            _log.warning(
                "verify_fill.catalog_chash_identity_unreachable",
                collection=self._collection, error=str(exc),
            )
            return None


class _CatalogManifestSource:
    """Real :class:`~nexus.migration.verify_fill.ManifestSource` wiring
    (``HttpCatalogClient.get_manifest`` returns ``list[ManifestRow]``
    dataclasses — ``verify_fill._manifest_key`` already accepts them
    verbatim, fix e7b42d36 — R2 wiring note)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def manifest_for(self, doc_id: str) -> list[Any] | None:
        try:
            return self._client.get_manifest(doc_id)
        except Exception as exc:  # noqa: BLE001 — unreachable surface -> indeterminate
            _log.warning(
                "verify_fill.catalog_manifest_unreachable", doc_id=doc_id, error=str(exc),
            )
            return None


#: catalog tables with NO wired inner-fill surface yet (P3a covers only
#: owners/collections/document_chunks — the incident-recovery slice).
_CATALOG_NO_FILL_SURFACE: tuple[str, ...] = ("documents", "links")


def verify_fill_catalog(
    catalog_db_path: Path,
    client: Any,
    *,
    count_source: "CountSource | None" = None,
    batch_size: int = 300,
    breaker: Any = None,
    collector: Any = None,
) -> dict[str, Any]:
    """RDR-178 P4: verify-fill (delta) for the ``catalog`` store.

    Outer count-diff across the 5 mapped catalog relations. ``documents`` /
    ``links`` have NO wired inner-fill surface (P3a's identity-inventory
    only covers owners/collections/document_chunks); if EITHER is non-parity
    this falls back to the FULL :func:`~nexus.db.t2.catalog_etl.migrate_catalog`
    ETL for the whole store — a partial/incoherent catalog write (chunks
    filled but their parent documents never re-sent) would be worse than a
    full re-send. When documents+links are at parity, owners/collections/
    document_chunks are each delta-filled independently.
    """
    from nexus.db.t2.catalog_etl import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        _fetch_all,
        _open_ro,
        _transform_chunk_row,
        _transform_collection,
        _transform_owner,
        count_source_rows,
        migrate_catalog,
    )
    from nexus.migration.verify_fill import (  # noqa: PLC0415 — R2 import-cycle guard, mirrored from verify_fill.py
        fill_missing,
        fill_missing_document_chunks,
        verify_store_counts,
    )
    from nexus.retry import EtlCircuitBreaker  # noqa: PLC0415 — deferred to avoid CLI startup cost

    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    batch_size = clamp_fill_batch_size(batch_size)
    cs = count_source or ServiceCountSource()

    source_counts = count_source_rows(catalog_db_path)
    # count_source_rows also reports "_meta" (unmapped bookkeeping) —
    # verify_store_counts marks it indeterminate harmlessly; it never
    # participates in the fallback decision below.
    outer_verdicts = verify_store_counts("catalog", cs, source_counts)

    needs_full_fallback = any(
        outer_verdicts.get(t, {}).get("status", "indeterminate") != "parity"
        for t in _CATALOG_NO_FILL_SURFACE
    )
    if needs_full_fallback:
        _log.info(
            "verify_fill.catalog_full_fallback",
            reason="documents/links have no inner-fill surface (P3a scope) "
                   "and are not at parity",
        )
        full_results = migrate_catalog(
            catalog_db_path, client, collector=collector, breaker=breaker,
        )
        total_filled = sum(
            full_results[k]["written"]
            for k in ("owners", "documents", "collections", "document_chunks", "links")
            if k in full_results
        )
        return {
            "store": "catalog", "outer": outer_verdicts, "fallback": "full_etl",
            "full_results": full_results, "total_filled": total_filled,
            "convergence_notes": dedup_convergence_notes("catalog", outer_verdicts),
        }

    conn = _open_ro(catalog_db_path)
    try:
        owners_rows = _fetch_all(conn, "owners")
        collections_rows = _fetch_all(conn, "collections")
        chunk_rows = _fetch_all(conn, "document_chunks")
        doc_rows = _fetch_all(conn, "documents")
    finally:
        conn.close()

    fill_results: dict[str, Any] = {}
    total_filled = 0

    if outer_verdicts.get("owners", {}).get("status") != "parity":
        payload_rows = [_transform_owner(r) for r in owners_rows]
        identity_source = _CatalogFlatIdentitySource(client.list_owners, "tumbler_prefix")
        key_fn: Callable[[dict[str, Any]], str] = lambda r: r["tumbler_prefix"]  # noqa: E731
        result = _try_fill(
            lambda: fill_missing(
                source_rows=payload_rows, key_fn=key_fn,
                identity_source=identity_source,
                import_fn=lambda rows: client._post("/import/owner", {"rows": rows}),
                batch_size=batch_size, breaker=breaker, table="owners",
            ),
            store="catalog", table_name="owners", collector=collector,
            recovery=lambda: _recover_flat_fill(identity_source, payload_rows, key_fn),
        )
        fill_results["owners"] = result
        total_filled += result["filled"]

    if outer_verdicts.get("collections", {}).get("status") != "parity":
        payload_rows = [_transform_collection(r) for r in collections_rows]
        identity_source = _CatalogFlatIdentitySource(client.list_collections, "name")
        key_fn = lambda r: r["name"]  # noqa: E731
        result = _try_fill(
            lambda: fill_missing(
                source_rows=payload_rows, key_fn=key_fn,
                identity_source=identity_source,
                import_fn=lambda rows: client._post("/import/collection", {"rows": rows}),
                batch_size=batch_size, breaker=breaker, table="collections",
            ),
            store="catalog", table_name="collections", collector=collector,
            recovery=lambda: _recover_flat_fill(identity_source, payload_rows, key_fn),
        )
        fill_results["collections"] = result
        total_filled += result["filled"]

    if outer_verdicts.get("document_chunks", {}).get("status") != "parity":
        collection_for_doc = {
            r["tumbler"]: r.get("physical_collection") or "" for r in doc_rows
        }
        payload_rows = [
            {**_transform_chunk_row(r), "doc_id": r["doc_id"]} for r in chunk_rows
        ]
        result = _try_fill(
            lambda: fill_missing_document_chunks(
                source_rows=payload_rows,
                collection_for_doc=collection_for_doc,
                identity_source_factory=lambda coll: _CatalogChashIdentitySource(client, coll),
                manifest_source=_CatalogManifestSource(client),
                import_fn=lambda doc_id, rows: client._post(
                    "/import/chunk", {"doc_id": doc_id, "rows": rows},
                ),
                batch_size=batch_size, breaker=breaker,
            ),
            store="catalog", table_name="document_chunks", collector=collector,
            recovery=lambda: {
                "source_count": len(payload_rows), "missing": None,
                "filled": 0, "indeterminate": len(payload_rows), "status": "indeterminate",
            },
        )
        fill_results["document_chunks"] = result
        total_filled += result.get("filled", 0)

    if collector is not None:
        for table, result in fill_results.items():
            collector.count_read("catalog", table, result.get("source_count", 0))
            collector.count_written("catalog", table, result.get("filled", 0))

    return {
        "store": "catalog", "outer": outer_verdicts, "fill": fill_results,
        "total_filled": total_filled,
        "convergence_notes": dedup_convergence_notes("catalog", outer_verdicts),
    }


# ── telemetry: real IdentitySource (probe_ids) + fill orchestration ──────────
#
# RDR-178 wave-2 P3b (nexus-s3dd4.14): the delta path for the six telemetry
# tables, gated on the wave-3 engine cut (engine-service-v0.1.18+, the
# ``/v1/telemetry/ids/probe`` membership-probe endpoint — P1, nexus-s3dd4.3).

_TELEMETRY_TABLES: tuple[str, ...] = (
    "relevance_log", "search_telemetry", "tier_writes",
    "nx_answer_runs", "hook_failures", "frecency",
)


class _TelemetryProbeIdentitySource:
    """Real :class:`~nexus.migration.verify_fill.IdentitySource` wiring for
    ONE telemetry table via ``HttpTelemetryStore.probe_ids``.

    Unlike chash's/catalog's flat single-column identities, a telemetry
    conflict key is a MULTI-COLUMN tuple (see
    ``telemetry_etl.CONFLICT_KEY_COLUMNS``) — ``fill_missing``'s
    ``str``-typed ``IdentitySource``/``key_fn`` contract is duck-typed to
    tuples here (hashable, comparable, exactly what the diff needs; the
    same widening precedent as ``verify_fill._manifest_key`` accepting two
    real row shapes despite its own narrower declared type).

    A failure here (transport error, 5xx, …) reports the same as chash's/
    catalog's real ``IdentitySource`` wiring: ``present() -> None`` ->
    indeterminate for THIS table only — including a 404, in the unlikely
    event one reaches this far. The MIXED-FLEET 404 case (R1 note 2: a
    pre-v0.1.18 engine 404s ``/v1/telemetry/ids/probe`` entirely) is a
    store-wide capability gap, not a per-table condition, so it is checked
    ONCE up front by :func:`_telemetry_probe_supported` — by the time this
    class's ``present()`` runs, capability has already been confirmed.
    """

    def __init__(self, store: Any, table: str, key_tuples: list[tuple[Any, ...]]) -> None:
        self._store = store
        self._table = table
        self._key_tuples = key_tuples

    def present(self) -> set[tuple[Any, ...]] | None:
        try:
            present = self._store.probe_ids(
                self._table, [list(k) for k in self._key_tuples],
            )
        except Exception as exc:  # noqa: BLE001 — unreachable surface -> indeterminate (verify_fill's own documented contract); the store-wide 404-capability case is pre-screened by _telemetry_probe_supported before this is ever reached
            _log.warning(
                "verify_fill.telemetry_identity_unreachable",
                table=self._table, error=str(exc),
            )
            return None
        return {tuple(k) for k in present}


def _telemetry_import_fn(store: Any, table: str) -> Callable[[list[dict[str, Any]]], Any]:
    def _import(batch: list[dict[str, Any]]) -> Any:
        return store.import_rows_batch(table, batch)
    return _import


def _telemetry_probe_supported(
    store: Any, rows_by_table: dict[str, list[dict[str, Any]]],
) -> bool:
    """One-shot capability probe (R1 note 2, mixed-fleet 404): pick the
    first table (deterministic sort) with >=1 row needing a fill decision
    and probe its FIRST row's conflict key alone.
    ``/v1/telemetry/ids/probe`` is ONE endpoint shared by all six tables —
    a 404 on it means every subsequent probe would also 404, so this is
    checked ONCE, up front, rather than re-discovered per table.

    Returns ``False`` ONLY on a confirmed 404 (old engine, no capability
    at all). Any other outcome — success, or a non-404 failure (which the
    per-table :class:`_TelemetryProbeIdentitySource` will independently
    re-encounter and report as indeterminate for THAT table) — returns
    ``True``: only a genuine store-wide capability gap forces the full-ETL
    fallback; a transient failure must not.
    """
    import httpx  # noqa: PLC0415 — deferred; only needed on this capability-check path
    from nexus.db.t2.telemetry_etl import conflict_key  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    for table in sorted(rows_by_table):
        rows = rows_by_table[table]
        if not rows:
            continue
        probe_key = conflict_key(table, rows[0])
        try:
            store.probe_ids(table, [list(probe_key)])
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return False
        except Exception:  # noqa: BLE001 — any other transport failure: capability presumed present, the per-table probe will independently report indeterminate for that table
            pass
        return True
    return True  # nothing needs filling at all -- vacuously "supported"


def verify_fill_telemetry(
    sqlite_path: Path,
    http_telemetry: Any,
    *,
    count_source: "CountSource | None" = None,
    batch_size: int = 300,
    breaker: Any = None,
    collector: Any = None,
) -> dict[str, Any]:
    """RDR-178 wave-2 P3b (nexus-s3dd4.14): verify-fill (delta) for the
    ``telemetry`` store — all six tables, uniformly.

    Design decision 1 (R1 open question — the 4 unmapped tables): the outer
    count-diff (``_VERIFY_TABLES``) maps only 2/6 tables (``hook_failures``,
    ``nx_answer_runs``) to a PG relation — the other four
    (``relevance_log``, ``search_telemetry``, ``tier_writes``,
    ``frecency``) have NO count-parity fast path and are therefore ALWAYS
    ``indeterminate`` at the outer level. Per ``verify_fill.py``'s own
    contract an indeterminate table is NEVER a silent pass — but here
    "never a pass" does NOT mean "fall back to a full re-send", because
    ``HttpTelemetryStore.probe_ids`` (P1) is a genuine per-table identity
    surface for ALL SIX tables regardless of the count-relation mapping.
    So every table (mapped or not) gets the SAME treatment: a MAPPED,
    PARITY table skips its probe entirely (zero HTTP writes, matching
    chash/catalog); every OTHER table (divergent, or indeterminate because
    unmapped) is probed via ``probe_ids`` and only the rows genuinely
    missing are re-sent.

    Design decision 2 (R1 open question — the DO-NOTHING event-log
    question): is a count-diff + identity fill worth it for append-only
    event-log tables (``relevance_log``/``tier_writes``/``nx_answer_runs``/
    ``hook_failures``), or is a bounded full re-send acceptable? Settled
    here as YES, delta-fill them too — rejected the alternative (full
    re-send for the 4 unmapped tables) because that is EXACTLY the
    2026-07-01-incident shape verify-fill exists to avoid: these are the
    append-mostly tables most likely to grow into the hundreds of
    thousands of rows, ``probe_ids`` makes the cheap per-table diff
    available for them regardless of the PG count-relation gap, and
    ``import_rows_batch`` is idempotent (``DO NOTHING`` / composite PK) so
    there is no correctness reason to prefer a full re-send over a precise
    one. A store-wide full-ETL fallback is reserved for the ONE case where
    NO diff is possible at all — see decision 3.

    Design decision 3 (R1 note 2 — mixed-fleet 404): a pre-v0.1.18 engine
    404s ``/v1/telemetry/ids/probe`` (it does not exist yet). This is
    checked ONCE, up front, via :func:`_telemetry_probe_supported` — NOT
    caught per table, and NOT allowed to propagate and crash
    ``migrate all`` (``HttpTelemetryStore.probe_ids`` intentionally does
    NOT catch transport errors — fail-closed by design, see its own
    docstring). On a confirmed 404 this falls back to
    :func:`~nexus.db.t2.telemetry_etl.migrate_telemetry_rows` (the
    unchanged full ETL) for the ENTIRE telemetry store — never per-table
    indeterminate-fill, never a blind resend passed off as a fill.

    Scope boundary: this function is wired into :func:`migrate_all`'s
    ``verify_fill=True`` ladder loop (mirroring ``chash``/``catalog``).
    The single-store CLI command (``nx storage migrate telemetry
    --verify-fill``) is NOT re-wired here — it continues to call
    :func:`verify_fill_generic_or_full` (the P4 CLI-flag-surface behavior,
    unconditional full ETL on any non-parity table). Out of P3b's scope
    (CLI flag-surface wiring); tracked as residual follow-up on the parent
    bead.
    """
    from nexus.db.t2.telemetry_etl import (  # noqa: PLC0415 — R2-style import-cycle guard, mirrors verify_fill_chash/verify_fill_catalog
        _open_ro,
        conflict_key,
        count_source_rows,
        migrate_telemetry_rows,
        read_rows_for_fill,
    )
    from nexus.migration.verify_fill import fill_missing, verify_store_counts  # noqa: PLC0415 — R2 import-cycle guard, mirrored from verify_fill.py
    from nexus.retry import EtlCircuitBreaker  # noqa: PLC0415 — deferred to avoid CLI startup cost

    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    batch_size = clamp_fill_batch_size(batch_size)
    cs = count_source or ServiceCountSource()

    source_counts = count_source_rows(sqlite_path)
    outer_verdicts = verify_store_counts("telemetry", cs, source_counts)

    conn = _open_ro(sqlite_path)
    try:
        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        for table in _TELEMETRY_TABLES:
            status = outer_verdicts.get(table, {}).get("status", "indeterminate")
            if status == "parity":
                continue  # zero-write skip, matches chash/catalog
            rows_by_table[table] = read_rows_for_fill(conn, table, collector=collector)
    finally:
        conn.close()

    if rows_by_table and not _telemetry_probe_supported(http_telemetry, rows_by_table):
        _log.warning(
            "verify_fill.telemetry_probe_unsupported_full_fallback",
            reason="pre-v0.1.18 engine -- /v1/telemetry/ids/probe 404'd",
        )
        full_results = migrate_telemetry_rows(
            sqlite_path, http_telemetry, collector=collector, breaker=breaker,
        )
        total_filled = sum(v["written"] for v in full_results.values())
        return {
            "store": "telemetry", "outer": outer_verdicts, "fallback": "full_etl",
            "full_results": full_results, "total_filled": total_filled,
            "convergence_notes": dedup_convergence_notes("telemetry", outer_verdicts),
        }

    fill_results: dict[str, Any] = {}
    total_filled = 0
    for table in sorted(rows_by_table):
        rows = rows_by_table[table]
        if not rows:
            fill_results[table] = {
                "source_count": 0, "target_count": None, "missing": 0,
                "filled": 0, "status": "parity",
            }
            continue

        key_tuples = [conflict_key(table, r) for r in rows]

        def _key_fn(r: dict[str, Any], _t: str = table) -> tuple[Any, ...]:
            return conflict_key(_t, r)

        identity_source = _TelemetryProbeIdentitySource(http_telemetry, table, key_tuples)
        result = _try_fill(
            lambda: fill_missing(  # noqa: B023 — invoked immediately by _try_fill, not deferred past this iteration
                source_rows=rows,
                key_fn=_key_fn,
                identity_source=identity_source,
                import_fn=_telemetry_import_fn(http_telemetry, table),
                batch_size=batch_size,
                breaker=breaker,
                table=table,
            ),
            store="telemetry", table_name=table, collector=collector,
            recovery=lambda: _recover_flat_fill(identity_source, rows, _key_fn),  # noqa: B023
        )
        fill_results[table] = result
        total_filled += result["filled"]

    if collector is not None:
        for table, result in fill_results.items():
            collector.count_read("telemetry", table, result.get("source_count", 0))
            collector.count_written("telemetry", table, result.get("filled", 0))

    return {
        "store": "telemetry", "outer": outer_verdicts, "fill": fill_results,
        "total_filled": total_filled,
        "convergence_notes": dedup_convergence_notes("telemetry", outer_verdicts),
    }


# ── generic stores: outer-verify-only, skip-on-parity else full ETL ──────────
#
# memory/plans/taxonomy have NO wired inner-fill surface yet — but the
# outer count-diff loop is store-agnostic and reuses each store's EXISTING
# ``count_source_rows`` (the --dry-run counting function), so these three
# stores still benefit from "skip the full re-send when nothing changed"
# even without a delta fill path. A non-parity table falls back to the
# unchanged full ETL.
#
# telemetry graduated to REAL delta-fill wiring in P3b (nexus-s3dd4.14, see
# ``verify_fill_telemetry`` below) once the wave-3 engine
# (``/v1/telemetry/ids/probe``, engine-service-v0.1.18+) landed — it is
# handled in :func:`migrate_all`'s ladder loop the same way chash/catalog
# are, NOT through ``_GENERIC_VERIFY_FILL_COUNTERS`` below.


def _memory_source_counts(sources: EtlSources) -> dict[str, int]:
    from nexus.db.t2.memory_etl import count_source_rows  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    return {"memory": count_source_rows(sources.sqlite_path)}


def _plans_source_counts(sources: EtlSources) -> dict[str, int]:
    from nexus.db.t2.plan_etl import count_source_rows  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    return {"plans": count_source_rows(sources.sqlite_path)}


def _telemetry_source_counts(sources: EtlSources) -> dict[str, int]:
    """Retained for :func:`verify_fill_generic_or_full`-shaped callers and
    its own regression coverage (``test_telemetry_source_counts_passes_
    all_six_tables_through``). No longer consulted by
    ``_GENERIC_VERIFY_FILL_COUNTERS`` / :func:`migrate_all` since P3b
    (nexus-s3dd4.14) — telemetry now has REAL per-table delta-fill wiring
    (:func:`verify_fill_telemetry`), so a 2/6-table PG mapping no longer
    forces an all-or-nothing store-level decision; each table (mapped or
    not) is diffed and filled independently. See ``verify_fill_telemetry``'s
    docstring for the full design rationale."""
    from nexus.db.t2.telemetry_etl import count_source_rows  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    counts = count_source_rows(sources.sqlite_path)
    return dict(counts)


def _taxonomy_source_counts(sources: EtlSources) -> dict[str, int]:
    from nexus.db.t2.taxonomy_etl import count_source_rows  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    counts = count_source_rows(sources.sqlite_path)
    return {
        "topics": counts.get("topics", 0),
        "topic_assignments": counts.get("assignments", 0),
        "topic_links": counts.get("links", 0),
    }


#: store -> function computing its _VERIFY_TABLES-shaped source counts, for
#: the generic (outer-verify-only) verify-fill path. ``chash``/``catalog``/
#: ``telemetry`` are handled separately (real delta fill — see
#: ``verify_fill_chash`` / ``verify_fill_catalog`` / ``verify_fill_telemetry``
#: above/below); ``aspects`` / ``aspects_queue`` are NOT in this map (no
#: dry-run counting seam wired today) and always run the full ETL under
#: verify-fill, same as before.
_GENERIC_VERIFY_FILL_COUNTERS: dict[str, Callable[[EtlSources], dict[str, int]]] = {
    "memory": _memory_source_counts,
    "plans": _plans_source_counts,
    "taxonomy": _taxonomy_source_counts,
}


def verify_fill_generic_or_full(
    store: str,
    source_counts: dict[str, int],
    run_full: Callable[[], Any],
    *,
    count_source: "CountSource | None" = None,
) -> tuple[dict[str, "TableVerdict"], Any | None, list[str]]:
    """CLI-facing generic verify-fill gate for a store with NO delta-fill
    surface yet (memory/plans/taxonomy — and, still, ``nx storage migrate
    telemetry --verify-fill``'s single-store CLI command, which continues
    to call this generic gate directly rather than
    :func:`verify_fill_telemetry` — see that function's docstring for the
    scope boundary). Runs ONLY the outer count-diff: *run_full* (the
    store's existing unchanged ETL) is SKIPPED entirely when every table is
    at parity; otherwise it is called.

    Returns ``(verdicts, full_result_or_None, convergence_notes)`` — the
    single-store CLI commands use ``full_result is None`` as the "nothing
    to do" signal (mirrors :func:`migrate_all`'s ``skip_stores`` fold-in for
    the multi-store path, at store granularity here since a single-store
    command has nothing else to fold into).
    """
    from nexus.migration.verify_fill import verify_store_counts  # noqa: PLC0415 — R2 import-cycle guard, mirrored from verify_fill.py

    cs = count_source or ServiceCountSource()
    verdicts = verify_store_counts(store, cs, source_counts)
    notes = dedup_convergence_notes(store, verdicts)
    if verdicts and all(v["status"] == "parity" for v in verdicts.values()):
        return verdicts, None, notes
    return verdicts, run_full(), notes


def _open_chash_store() -> Any:
    """No-arg ``HttpChashIndex`` — resolves its endpoint config-first, same
    as :func:`build_store_etls`'s ``_chash`` closure."""
    from nexus.db.t2.http_chash_index import HttpChashIndex  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    return HttpChashIndex()


def _open_catalog_client() -> Any:
    """No-arg catalog client — resolves its endpoint config-first, same as
    :func:`build_store_etls`'s ``_catalog`` closure."""
    from nexus.catalog.factory import make_catalog_client_for_migration  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    return make_catalog_client_for_migration()


def _open_telemetry_store() -> Any:
    """No-arg ``HttpTelemetryStore`` — resolves its endpoint config-first,
    same as :func:`_open_chash_store` / :func:`_open_catalog_client`."""
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    return HttpTelemetryStore()


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
    verify_fill: bool = False,
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

    ``verify_fill`` (RDR-178 wave-2 P4, nexus-s3dd4.5; telemetry P3b,
    nexus-s3dd4.14): when True, run the delta path instead of the
    unconditional full re-send. Per store:

    - ``chash`` / ``catalog`` — real inner-fill wiring
      (:func:`verify_fill_chash` / :func:`verify_fill_catalog`): the outer
      count-diff decides parity (zero writes) vs. divergent/indeterminate
      (send ONLY the missing rows, or — for catalog's documents/links,
      which have no wired fill surface — fall back to the full ETL for the
      whole store).
    - ``telemetry`` — real inner-fill wiring (:func:`verify_fill_telemetry`,
      P3b): PER-TABLE, not store-wide — a table WITH a PG count mapping
      (``hook_failures``/``nx_answer_runs``) skips its probe on parity; the
      other four (no count mapping, always indeterminate) are ALWAYS probed
      and delta-filled via ``HttpTelemetryStore.probe_ids`` (never treated
      as a store-wide "run the full ETL" signal). The ONE case that DOES
      fall back to the whole-store full ETL is a confirmed 404 on
      ``/v1/telemetry/ids/probe`` (pre-v0.1.18 engine, mixed-fleet gate —
      see that function's docstring).
    - ``memory`` / ``plans`` / ``taxonomy`` — outer-verify only (no delta
      fill surface yet): parity SKIPS the store entirely (folded into
      ``skip_stores`` / ``report["skipped_stores"]`` — the same signal an
      already-migrated pre-flight uses); non-parity falls back to the
      unchanged full ETL.
    - ``aspects`` / ``aspects_queue`` — always the full ETL (no counting
      seam wired for these yet); ``verify_fill`` has no effect on them.

    Every store's outer verdicts feed ``report["verify_fill"]`` (present
    only when ``verify_fill=True``) — see :func:`dedup_convergence_notes`
    for why a dedup table can legitimately parity below its source count.
    """
    etls = ordered(build_store_etls(sources))
    cs = count_source or ServiceCountSource()
    verify_fill_outer: dict[str, dict[str, Any]] = {}
    verify_fill_results: dict[str, Any] = {}
    verify_fill_notes: list[str] = []
    skip_stores = set(skip_stores)

    if verify_fill:
        # Generic (outer-verify-only) stores: compute parity BEFORE the
        # ladder loop so an all-parity store folds into the EXISTING
        # skip_stores mechanism (Gap 7) — no new report field needed for
        # "this store needed nothing", the artifact already has one.
        from nexus.migration.verify_fill import verify_store_counts  # noqa: PLC0415 — R2 import-cycle guard, mirrored from verify_fill.py

        for store, counts_fn in _GENERIC_VERIFY_FILL_COUNTERS.items():
            if store in skip_stores:
                continue
            verdicts = verify_store_counts(store, cs, counts_fn(sources))
            verify_fill_outer[store] = verdicts
            verify_fill_notes.extend(dedup_convergence_notes(store, verdicts))
            if verdicts and all(v["status"] == "parity" for v in verdicts.values()):
                skip_stores.add(store)
                _log.info("migrate_all.verify_fill_skip_parity", store=store)

    skip_stores = frozenset(skip_stores)
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
            if verify_fill and etl.store == "chash":
                store_client = _open_chash_store()
                try:
                    result = verify_fill_chash(
                        sources.sqlite_path, store_client, count_source=cs,
                        collector=collector,
                    )
                finally:
                    with contextlib.suppress(Exception):
                        store_client.close()
                verify_fill_outer[etl.store] = result["outer"]
                verify_fill_results[etl.store] = result
                verify_fill_notes.extend(result["convergence_notes"])
            elif verify_fill and etl.store == "catalog":
                catalog_client = _open_catalog_client()
                try:
                    result = verify_fill_catalog(
                        sources.catalog_db_path, catalog_client, count_source=cs,
                        collector=collector,
                    )
                finally:
                    with contextlib.suppress(Exception):
                        catalog_client.close()
                verify_fill_outer[etl.store] = result["outer"]
                verify_fill_results[etl.store] = result
                verify_fill_notes.extend(result["convergence_notes"])
            elif verify_fill and etl.store == "telemetry":
                telemetry_store = _open_telemetry_store()
                try:
                    result = verify_fill_telemetry(
                        sources.sqlite_path, telemetry_store, count_source=cs,
                        collector=collector,
                    )
                finally:
                    with contextlib.suppress(Exception):
                        telemetry_store.close()
                verify_fill_outer[etl.store] = result["outer"]
                verify_fill_results[etl.store] = result
                verify_fill_notes.extend(result["convergence_notes"])
            else:
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
        # RDR-178 P4 report-writer fixup (b): the RESOLVED endpoint, never
        # the misleading "(lease)" placeholder (epic te885 comment
        # 2026-07-02 04:19).
        target={"service_url": resolve_target_service_url()},
        migration_id=mig_id,
    )
    verification, convergence_notes, dest_counts = verify_counts(report, cs)
    report["verification"] = verification
    # RDR-176 Gap 5: surface the destination-side (pg) row counts as a first-
    # class metric so "did rows actually land?" is answerable from the report,
    # not by paginating the read API.
    report["dest_counts"] = dest_counts
    # How many relations the count check actually reconciled (the mappable
    # subset present in this run) — surfaced so the operator artifact and the
    # CLI banner can name coverage instead of a vague "mappable relations".
    report["relations_checked"] = len(_written_by_table(report))
    all_convergence_notes = list(convergence_notes) + verify_fill_notes
    if all_convergence_notes:
        report["verification_convergence_notes"] = all_convergence_notes
    if skipped:
        report["skipped_stores"] = skipped
    if verify_fill:
        # Additive report field (RDR-178 P4): per-table outer verdicts +
        # the chash/catalog delta-fill results, keyed by store. Additive so
        # the RDR-153 per-table key set (locked by
        # tests/migration/test_migration_report.py::test_table_key_set_locked)
        # is never touched.
        report["verify_fill"] = {
            "outer": verify_fill_outer,
            "results": verify_fill_results,
            "total_filled": sum(
                r.get("total_filled", 0) for r in verify_fill_results.values()
            ),
        }
    return report
