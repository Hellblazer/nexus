# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s three collection-identity rows (RDR-229 P3.12, nexus-p2m1x.32).

* ``Collection ids``: the tenant's rows that carry a collection name and no ``collection_id``, from the SAME authenticated
  ``GET /v1/catalog/rdr229_completion_counts`` body the RDR-229 completion rows read (:mod:`nexus.rdr229_completion_health`),
  so there is no new route. ``tables[]`` holds only the tables whose CHECK is still NOT VALID (``p31-checks.tsv``), and for
  each ``orphans`` (a name no registry row carries, id NULL; write-frozen: any UPDATE of the row fails 23514) and ``unstamped``
  (a name set, id NULL, a registry row exists). A warning while any is left: unstamped rows clear when the stamp move runs,
  orphans need a person (the RDR-229 VALIDATE and completion rows say what). ``catalog_collections`` counts differently, by
  kind SUPERSEDED: its ``unstamped`` is the tombstones whose ``superseded_by_id`` is still NULL, never an orphan, always still
  to stamp, and the row reports it as such. A real failure is a registry row with a NULL ``collection_id``, counted from the
  registry listing the clash row reads (the id is an identity column, so none is expected). The four history tables
  (:data:`HISTORY_TABLES`, vectors-068-17) keep NULL on their old rows by design: they have no CHECK, never appear in
  ``tables[]``, and are named in one static sentence.
* ``Collection label clash``: live labels held by more than one registry row, computed on the client from the registry
  listing with the census's :func:`~nexus.catalog.rdr229.collection_row_census._label_clash` (live = ``superseded_by``
  empty). Bounded: the listing is the caller's tenant. The registry's primary key makes it zero until P4.1; the row is the
  proof, and it names both ids when it is not.
* ``Live-label index``: the engine's ``GET /v1/status`` boolean ``live_label_index_present`` (catalog-053,
  ``UNIQUE (tenant_id, name) WHERE superseded_by = ''``, built with failOnError=false). A warning while it is absent, harmless
  until P4.1 (the primary key holds the rule); P4.1 turns it into a failure.

Every way the engine cannot answer is a not-checked row (ok, skipped, never "none"): an engine without the route or its
``/version`` flag, a 503, a malformed or failed read, no catalog reader, an unreadable listing, a status without the field.
Kept out of :mod:`nexus.health` (a file at its size cap).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable
from nexus.catalog.rdr229.collection_row_census import _label_clash
from nexus.health import _ENGINE_STATUS_UNSET, HealthResult, _not_checked_row
from nexus.rdr229_completion_health import _default_counts_reader, _dicts, _int, _read_counts

_log = structlog.get_logger(__name__)

IDS_LABEL = "Collection ids"
CLASH_LABEL = "Collection label clash"
INDEX_LABEL = "Live-label index"

#: vectors-068-17: audit and telemetry tables whose old rows keep a NULL ``collection_id`` on purpose. They have no CHECK, so
#: they are never in the counts route's ``tables[]``: named here, never counted.
HISTORY_TABLES: tuple[str, ...] = ("relevance_log", "search_telemetry", "hook_failures", "gc_audit")
#: The registry. Its ``unstamped`` is the tombstones still to stamp (kind SUPERSEDED), not a null ``collection_id``.
REGISTRY_TABLE = "catalog_collections"
#: The ``/v1/status`` key (``StatusHandler``).
INDEX_FIELD = "live_label_index_present"
#: A registry listing with a null id is a defect; name at most this many labels so the row stays one readable line.
_NAMED_LABELS_MAX = 20

_HISTORY_SENTENCE = (
    "Unstamped by design and not counted (old rows keep NULL, no CHECK): " + ", ".join(HISTORY_TABLES) + ".")


def _lists_ids(rows: list[dict[str, Any]] | None) -> bool:
    """Whether the engine lists registry ids: the CAPABILITY, read from the key's presence in the uncoerced rows
    (``nexus.catalog.engine_reads.collection_rows``), never inferred from the values. An engine that lists ids sends the key
    on every row; one before P3.5a's read half sends none. So an id-listing engine whose rows are ALL null still lists
    ids, and fails below (linda's pre-review, 2026-11-22)."""
    return bool(rows) and all("collection_id" in r for r in rows)


def _null_id_labels(rows: list[dict[str, Any]] | None) -> list[str]:
    """Labels of the registry rows whose ``collection_id`` is NULL, on an engine that lists ids (see :func:`_lists_ids`)."""
    if not _lists_ids(rows):
        return []
    return sorted(str(r.get("name") or "") for r in rows if r.get("collection_id") is None)


def _named(labels: list[str]) -> str:
    shown = ", ".join(repr(n) for n in labels[:_NAMED_LABELS_MAX])
    return shown + (f" and {len(labels) - _NAMED_LABELS_MAX} more" if len(labels) > _NAMED_LABELS_MAX else "")


def _ids_row(counts: dict[str, Any] | None, why: str, retry: bool, rows: list[dict[str, Any]] | None, rows_why: str) -> HealthResult:
    null_labels = _null_id_labels(rows)
    if counts is None and not null_labels:
        return _not_checked_row(IDS_LABEL, why, retry=retry)
    unstamped: dict[str, int] = {}
    orphans: dict[str, int] = {}
    tombstones = 0
    for t in _dicts(counts.get("tables")) if counts is not None else []:
        table = t.get("table")
        if not isinstance(table, str):
            continue
        if table == REGISTRY_TABLE:
            tombstones += _int(t.get("unstamped"))
        elif _int(t.get("unstamped")):
            unstamped[table] = _int(t.get("unstamped"))
        if _int(t.get("orphans")):
            orphans[table] = _int(t.get("orphans"))
    sentences: list[str] = []
    fix: list[str] = []
    if counts is None:
        sentences.append(f"counts not read: {why}")
    else:
        total = sum(unstamped.values())
        if unstamped:
            sentences.append(
                f"{total} unstamped row(s) on {len(unstamped)} table(s) whose name/id CHECK is not yet validated: "
                + ", ".join(f"{t}: {n}" for t, n in sorted(unstamped.items())))
        if tombstones:
            sentences.append(f"{tombstones} tombstone(s) in {REGISTRY_TABLE} awaiting superseded_by_id")
        if unstamped or tombstones:
            fix.append(
                "These rows name a collection and have no id yet. The stamp move gives them one on its own; nothing is run from this "
                "row. The RDR-229 completion and VALIDATE rows above say where each table's move stands and when a table needs a "
                "step of its own")
        if orphans:
            sentences.append(
                f"{sum(orphans.values())} orphan row(s) (a name no registry row carries; write-frozen) on {len(orphans)} table(s): "
                + ", ".join(f"{t}: {n}" for t, n in sorted(orphans.items())))
            fix.append(
                "An orphan row names a collection with no registry row in this tenant, so the stamp cannot give it an id, and any "
                "UPDATE of it fails 23514. The RDR-229 VALIDATE rows above (and the completion rows, for a table whose stamp move "
                "has not finished) say how to clear it: register the missing collection or delete the rows")
        if not (unstamped or tombstones or orphans):
            sentences.append(
                "0 unstamped and 0 orphan row(s) in this tenant's tables whose name/id CHECK is not yet validated, and no "
                f"{REGISTRY_TABLE} tombstone awaits superseded_by_id")
    if null_labels:
        sentences.insert(0, f"{len(null_labels)} registry row(s) with no collection_id: {_named(null_labels)}")
        fix.insert(0, (
            "The registry's id is an identity column (vectors-068-1), so a row without one means that default was removed or "
            "bypassed. Do not stamp it by hand: read the engine's boot log for changeset vectors-068-1 and report the labels named here"))
    if _lists_ids(rows):
        sentences.append(f"{len(rows)} registry row(s) checked for a null collection_id")
    elif rows is not None:
        sentences.append("registry ids not checked: this engine does not list collection_id")
    else:
        sentences.append(f"registry ids not checked: {rows_why}")
    detail = ". ".join(sentences) + ". " + _HISTORY_SENTENCE
    if null_labels:
        return HealthResult(label=IDS_LABEL, ok=False, detail=detail, fix_suggestions=fix)
    if unstamped or tombstones or orphans:
        return HealthResult(label=IDS_LABEL, ok=False, warn=True, detail=detail, fix_suggestions=fix)
    return HealthResult(label=IDS_LABEL, ok=True, detail=detail)


def _read_ids_counts(counts_reader: Callable[[], dict[str, Any]] | None) -> tuple[dict[str, Any] | None, str, bool]:
    """``(body, "", _)`` or ``(None, why, retry)``: never raises. *retry* is False for an engine that does not answer the route
    (its ``/version`` lacks the flag, or the route is a 404): asking again changes nothing until the engine does."""
    seen: list[str] = []

    def read() -> dict[str, Any]:
        try:
            return (counts_reader if counts_reader is not None else _default_counts_reader())()
        except EngineRouteUnavailable as exc:
            seen.append(exc.reason)
            raise

    body, why = _read_counts(read)
    return body, why, EngineRouteUnavailable.UNSUPPORTED not in seen


def _rows_listing(rows_reader: Callable[[], Any] | None) -> tuple[list[dict[str, Any]] | None, str]:
    """``(rows, "")`` or ``(None, why)``: never raises."""
    try:
        if rows_reader is None:
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to keep CLI startup fast and avoid a cycle

            reader = make_catalog_reader()
            if reader is None:
                return None, "there is no catalog reader"
            # the uncoerced rows of the HTTP route, so an engine that lists no ids leaves the key absent (see _lists_ids)
            if callable(getattr(reader, "_get", None)):
                from nexus.catalog.engine_reads.collection_rows import read_collection_rows  # noqa: PLC0415 — deferred like the reader

                rows = read_collection_rows(reader)
            else:
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
            "A live label belongs to one row. Deleting or superseding the extra row by the id this row names arrives with P3.5a's "
            "write half; no command does it by id yet. This state cannot arise before P4.1: the registry's primary key "
            "(tenant_id, name) keeps a name to one row"])


def _index_row(status: dict[str, Any] | None) -> HealthResult:
    if status is None:
        return _not_checked_row(INDEX_LABEL, "the engine's status endpoint could not be read")
    present = status.get(INDEX_FIELD)
    if not isinstance(present, bool):
        return _not_checked_row(
            INDEX_LABEL, f"this engine predates {INDEX_FIELD} on /v1/status, or its probe could not run")
    if present:
        return HealthResult(label=INDEX_LABEL, ok=True, detail="the engine's unique index on live labels exists (catalog-053)")
    return HealthResult(
        label=INDEX_LABEL, ok=False, warn=True,
        detail=("catalog-053's unique index on live labels is not built (or is not valid). Harmless until P4.1: the registry's "
                "primary key keeps label uniqueness until then; P4.1 turns this row into a failure"),
        fix_suggestions=[
            "catalog-053-1 is built with failOnError=false, so a failed build leaves nothing on screen. It has two causes. A lock "
            "timeout (the build waits 5 s behind an open writer on the registry): the next engine boot retries it by itself. Or the "
            "migration role no longer owns nexus.catalog_collections: a restart does not fix that, the build fails again every "
            "boot, and the table's owner has to be put back. Read the engine's boot log for the Liquibase output of changeset "
            "catalog-053-1 to tell which; nothing breaks before P4.1"])


def check_collection_ids(
    engine_status: object = _ENGINE_STATUS_UNSET, *,
    counts_reader: Callable[[], dict[str, Any]] | None = None,
    rows_reader: Callable[[], Any] | None = None,
) -> list[HealthResult]:
    """The three rows, in order: ``Collection ids``, ``Collection label clash``, ``Live-label index``.

    *engine_status* is the ``GET /v1/status`` body the doctor already fetched (``None`` when that failed); left unset, this
    fetches its own. *counts_reader* returns the ``rdr229_completion_counts`` body (it raises
    :class:`~nexus.catalog.engine_reads.route_unavailable.EngineRouteUnavailable` when the engine cannot say); *rows_reader*
    returns the registry listing, which both the clash row and the null-id count of the ids row read; left unset, each asks
    the catalog reader.
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
    counts, counts_why, counts_retry = _read_ids_counts(counts_reader)
    rows, rows_why = _rows_listing(rows_reader)
    return [_ids_row(counts, counts_why, counts_retry, rows, rows_why), _clash_row(rows, rows_why), _index_row(status)]
