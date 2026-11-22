# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s three collection-identity rows (RDR-229 P3.12, nexus-p2m1x.32).

* ``collection ids``: the tenant's rows that carry a collection name and no ``collection_id``, from the SAME authenticated
  ``GET /v1/catalog/rdr229_completion_counts`` body the RDR-229 completion rows read (:mod:`nexus.rdr229_completion_health`),
  so there is no new route. ``tables[].unstamped`` is that count per table whose CHECK is still NOT VALID. A warning while
  any is left, pointing at ``nexus.rdr229_restamp``. The four history tables (:data:`HISTORY_TABLES`, vectors-068-17) keep NULL
  on their old rows by design: they are named, never counted. ``catalog_collections`` takes its id from its own DDL (an
  identity column), so an unstamped row there is a failure, not a wait.
* ``collection label clash``: live labels held by more than one registry row, computed on the client from the registry
  listing with the census's :func:`~nexus.catalog.rdr229.collection_row_census._label_clash` (live = ``superseded_by``
  empty). Bounded: the listing is the caller's tenant. The registry's primary key makes it zero until P4.1; the row is the
  proof, and it names both ids when it is not.
* ``live-label index``: the engine's ``GET /v1/status`` boolean ``live_label_index_present`` (catalog-053,
  ``UNIQUE (tenant_id, name) WHERE superseded_by = ''``, built with failOnError=false). A warning while it is absent; P4.1
  turns that into a failure.

Every way the engine cannot answer is a not-checked row (ok, skipped, never "none"): an engine without the route or its
``/version`` flag, a 503, a malformed or failed read, no catalog reader, an unreadable listing, a status without the field.
Kept out of :mod:`nexus.health` (a file at its size cap).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from nexus.catalog.rdr229.collection_row_census import _label_clash
from nexus.health import _ENGINE_STATUS_UNSET, HealthResult, _not_checked_row
from nexus.rdr229_completion_health import _dicts, _int, _read_counts

_log = structlog.get_logger(__name__)

IDS_LABEL = "collection ids"
CLASH_LABEL = "collection label clash"
INDEX_LABEL = "live-label index"

#: vectors-068-17: audit and telemetry tables whose old rows keep a NULL ``collection_id`` on purpose.
HISTORY_TABLES: tuple[str, ...] = ("relevance_log", "search_telemetry", "hook_failures", "gc_audit")
#: The registry: identity column, filled at its DDL (vectors-068-1).
REGISTRY_TABLE = "catalog_collections"
#: The ``/v1/status`` key (``StatusHandler``).
INDEX_FIELD = "live_label_index_present"


def _history_sentence(by_table: dict[str, int]) -> str:
    named = ", ".join(f"{t} ({by_table[t]})" if by_table.get(t) else t for t in HISTORY_TABLES)
    return f"Unstamped by design and not counted (old rows keep NULL): {named}."


def _ids_row(counts: dict[str, Any] | None, why: str) -> HealthResult:
    if counts is None:
        return _not_checked_row(IDS_LABEL, why)
    by_table = {str(t["table"]): _int(t.get("unstamped")) for t in _dicts(counts.get("tables")) if isinstance(t.get("table"), str)}
    counted = {t: n for t, n in sorted(by_table.items()) if n > 0 and t not in HISTORY_TABLES}
    total = sum(counted.values())
    history = _history_sentence(by_table)
    if not total:
        return HealthResult(
            label=IDS_LABEL, ok=True,
            detail=(f"0 unstamped row(s) in this tenant's tables whose name/id CHECK is not yet validated; {REGISTRY_TABLE} is identity-filled (its id comes from its "
                    f"DDL, so no row there is unstamped). {history}"))
    parts = ", ".join(f"{t}: {n}" for t, n in counted.items())
    registry = counted.get(REGISTRY_TABLE, 0)
    if registry:
        return HealthResult(
            label=IDS_LABEL, ok=False,
            detail=(f"{total} unstamped row(s) on {len(counted)} table(s) whose name/id CHECK is not yet validated: {parts}. {REGISTRY_TABLE} is identity-filled at its "
                    f"DDL, so a row there with no id is a failure, not a wait. {history}"),
            fix_suggestions=[
                f"A registry row with a null collection_id means the identity column is gone or was bypassed (vectors-068-1). "
                f"Do not stamp it by hand: read the engine log for the changeset, and restore the column's identity default "
                f"before running the restamp for the other tables ({_restamp_hint(counted)})"])
    return HealthResult(
        label=IDS_LABEL, ok=False, warn=True,
        detail=f"{total} unstamped row(s) on {len(counted)} table(s) whose name/id CHECK is not yet validated: {parts}. {history}",
        fix_suggestions=[
            f"Each of these rows names a collection and has no collection_id yet. {_restamp_hint(counted)}"])


def _restamp_hint(counted: dict[str, int]) -> str:
    tables = [t for t in counted if t != REGISTRY_TABLE]
    steps = "; ".join(f"SELECT nexus.rdr229_restamp('{t}')" for t in tables) or "SELECT nexus.rdr229_restamp('<table>')"
    return (f"As nexus_admin (the migrating role) run {steps}. It registers the stamp move again, which stamps the rows that can "
            "now resolve; the RDR-229 VALIDATE rows say whether an orphan row (a name no registry row carries) holds a table back")


def _rows_listing(rows_reader: Callable[[], Any] | None) -> tuple[list[dict[str, Any]] | None, str]:
    """``(rows, "")`` or ``(None, why)``: never raises."""
    try:
        if rows_reader is None:
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to keep CLI startup fast and avoid a cycle

            reader = make_catalog_reader()
            if reader is None:
                return None, "there is no catalog reader"
            rows = reader.list_collections()
        else:
            rows = rows_reader()
    except Exception as exc:  # noqa: BLE001 — best-effort: an unread listing is "cannot say", never "none", and never crashes `nx doctor`
        _log.debug("doctor_collection_ids_listing_failed", error=str(exc))
        return None, "the registry listing could not be read"
    if not isinstance(rows, list):
        return None, "the registry listing is not a list"
    return [r for r in rows if isinstance(r, dict)], ""


def _id_text(cid: object) -> str:
    return "no id" if cid is None else str(cid)


def _clash_row(rows: list[dict[str, Any]] | None, why: str) -> HealthResult:
    if rows is None:
        return _not_checked_row(CLASH_LABEL, why)
    live = [r for r in rows if isinstance(r.get("name"), str) and r["name"] and not (r.get("superseded_by") or "")]
    census = _label_clash(live)
    if not census["clashing_names"]:
        return HealthResult(label=CLASH_LABEL, ok=True, detail=f"none ({census['live_names']} live label(s) checked)")
    lines = []
    for c in census["clashes"]:
        ids = sorted((r.get("collection_id") for r in live if r["name"] == c["name"]),
                     key=lambda v: (v is None, v if isinstance(v, int) else 0))
        lines.append(f"{c['name']!r} is held by {c['rows']} live rows (ids {', '.join(_id_text(i) for i in ids)})")
    return HealthResult(
        label=CLASH_LABEL, ok=False,
        detail=f"{census['clashing_names']} live label(s) held by more than one row: " + "; ".join(lines),
        fix_suggestions=[
            "A live label belongs to one row. Supersede or delete the extra row, by the id this row names, so one live row "
            "keeps the label; the engine's partial unique index on live labels cannot build while two share one"])


def _index_row(status: dict[str, Any] | None) -> HealthResult:
    if status is None:
        return _not_checked_row(INDEX_LABEL, "the engine's status endpoint could not be read")
    present = status.get(INDEX_FIELD)
    if not isinstance(present, bool):
        return _not_checked_row(INDEX_LABEL, f"this engine does not report {INDEX_FIELD} on /v1/status", retry=False)
    if present:
        return HealthResult(label=INDEX_LABEL, ok=True, detail="the engine's unique index on live labels exists (catalog-053)")
    return HealthResult(
        label=INDEX_LABEL, ok=False, warn=True,
        detail="catalog-053 has not built; the next boot retries; label uniqueness rests on the PK until P4.1",
        fix_suggestions=[
            "Restart the engine to retry the build. Read its boot log for the Liquibase output of changeset catalog-053-1 "
            "(it is built with failOnError=false, so a failure leaves nothing on screen). The collection label clash row above "
            "says whether a duplicate live label is what holds it back"])


def check_collection_ids(
    engine_status: object = _ENGINE_STATUS_UNSET, *,
    counts_reader: Callable[[], dict[str, Any]] | None = None,
    rows_reader: Callable[[], Any] | None = None,
) -> list[HealthResult]:
    """The three rows, in order: ``collection ids``, ``collection label clash``, ``live-label index``.

    *engine_status* is the ``GET /v1/status`` body the doctor already fetched (``None`` when that failed); left unset, this
    fetches its own. *counts_reader* returns the ``rdr229_completion_counts`` body (it raises
    :class:`~nexus.catalog.engine_reads.route_unavailable.EngineRouteUnavailable` when the engine cannot say); *rows_reader*
    returns the registry listing; left unset, each asks the catalog reader.
    """
    status: dict[str, Any] | None
    if engine_status is _ENGINE_STATUS_UNSET:
        try:
            from nexus.db.http_engine_status import fetch_engine_status  # noqa: PLC0415 — deferred to keep CLI startup fast

            status = fetch_engine_status()
        except Exception as exc:  # noqa: BLE001 — best-effort: must not crash `nx doctor`
            _log.debug("doctor_collection_ids_probe_failed", error=str(exc))
            status = None
    else:
        status = engine_status if isinstance(engine_status, dict) else None
    counts, counts_why = _read_counts(counts_reader)
    rows, rows_why = _rows_listing(rows_reader)
    return [_ids_row(counts, counts_why), _clash_row(rows, rows_why), _index_row(status)]
