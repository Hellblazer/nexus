# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s rows for RDR-229 P3.1's boot-time completion check (nexus-p2m1x.17).

After the schema walk the engine reads, on its admin connection and read only, whether each install's stamp moves have
finished and so whether the engine's id readers, the one-row rename and the index builds may switch (three gates, RDR-229
item 6). It reports in its own top-level ``/v1/status`` field, ``rdr229_completion`` (``CompletionStatusJson.java``), not in
``boot_work``: the gates wait by design across releases (gate 3 cannot hold until P3.4 builds its id indexes), and a
``boot_work`` entry would read as work in progress to ``nx upgrade``. The field is ABSENT until the engine's first read (and
on an engine that predates the check); absent is "not known", never ok. This module reads it:

* the gate summary: one ok row. The gates wait by design, so the row informs and never warns.
* ``checks_missing``: a listed CHECK that is missing. A warning. The superseded_by one is held back on purpose by its
  changeset while a registry tombstone with a blank ``model_version`` has no ``superseded_by_id`` (D2's repair UPDATE would
  hit 23514); the wait clears by itself, and the row adds how many of THIS tenant's registry rows hold it back.
* ``validate_waits``: a table whose stamp move is done and whose CHECK is still NOT VALID because rows violate it: orphans,
  a name with no registry row in its tenant, which the stamp cannot give an id, and rows the stamp has not given one yet. An
  orphan row is write-frozen (any UPDATE of it fails 23514). With this tenant's orphan count above 0 the row says to register
  or delete the orphans and then run ``nexus.rdr229_restamp``; with 0 and only unstamped rows left it says the restamp step
  alone finishes it. Without the counts it shows the engine's text, which states both conditions.
* ``gate3_latched`` and ``id_index_invalid`` (P3.4): gate 3 latches the first time it holds (``nexus.rdr229_gate_latch``), and the engine's
  readers go by the latch, so a live gate 3 that turns false later (an id index rebuilding) does not move them. An INVALID id-predicate
  PCI index is reported on its own: a warning once gate 3 has latched (readers depend on that index), information before. The engine
  drops an INVALID index and builds it again on its next reconciler pass; nothing to run.
* ``gate3_latch_pci_off`` (P3.4): the latch row says gate 3 latched while ``NX_SEARCH_PCI`` was off, so no per-collection id index was required
  then. An information row: with PCI on now the engine builds the id indexes first, as if gate 2 held, and until they are VALID a search has
  no per-collection index (the readers are on the id either way).
* ``dangling_tombstones``: R7's pre-flight, COUNTS by content type of this tenant's registry tombstones whose ``superseded_by``
  names no row of their tenant, and of the tombstones the stamp resolved to their own id (retired, no successor). Each row
  carries the read-only query that lists them by name, to run as ``nexus_admin``.

``/v1/status`` is unauthenticated, so the engine's ``rdr229_completion`` field carries NO number that describes tenants (review
N2, linda's ruling). Every number these rows show comes from the authenticated, tenant-scoped
``GET /v1/catalog/rdr229_completion_counts`` (:mod:`nexus.catalog.engine_reads.rdr229_completion_counts`): the caller's own
counts, never another tenant's and never a total. An engine without the route, a timeout or a failed read leaves the counts
unread, and the rows say so; an unread count is never shown as zero.

None of these is red: every waiting state clears by itself or by a step the row names, and a red row on every install
mid-rollout would hide a real one. Kept out of :mod:`nexus.health` (a file at its size cap); stdlib and
:class:`~nexus.health.HealthResult` only, the status body fetched by the caller as for the other engine-status rows.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from nexus.catalog.engine_reads import rdr229_completion_counts
from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable
from nexus.health import _ENGINE_STATUS_UNSET, HealthResult

_log = structlog.get_logger(__name__)

#: The ``/v1/status`` key the engine reports under (``BootWorkRegistry``, ``StatusHandler``).
FIELD = "rdr229_completion"

SUMMARY_LABEL = "RDR-229 completion"
DANGLING_LABEL = "RDR-229 dangling tombstones"
SELF_ID_LABEL = "RDR-229 self-id tombstones"
ID_INDEX_LABEL = "RDR-229 id-predicate index"
LATCH_POSTURE_LABEL = "RDR-229 gate 3 latch posture"
RENAME_LABEL = "RDR-229 collection rename"
NOT_KNOWN = (
    "not known: the engine has not reported its RDR-229 completion state yet (it has not read it, or it predates the check)"
)

#: The rollback runbook and its generated re-add SQL, as published on the repository's main branch: they are not in the
#: installed package, so the doctor row names where to read them (the pattern of ``nexus.health``'s other runbook links).
_DOCS_BLOB = "https://github.com/Hellblazer/nexus/blob/main/docs/runbooks"
ROLLBACK_RUNBOOK_URL = f"{_DOCS_BLOB}/rdr-229-p31-rollback.md"
READD_SQL_URL = f"{_DOCS_BLOB}/rdr-229-p31-readd.sql"

#: The CHECK whose changeset waits on D2's data (``CompletionCheck.SUPERSEDED_BY_CHECK``).
SUPERSEDED_BY_CHECK = "catalog_collections_superseded_by_id_check"

#: The registry's tombstones whose ``superseded_by`` names no row of their tenant: the engine's count
#: (``CompletionCheck.danglingTombstones``) as a listing by name. Pinned against a real registry by
#: ``tests/test_rdr229_completion_queries.py``.
DANGLING_QUERY = (
    "SELECT c.tenant_id, c.content_type, c.name FROM nexus.catalog_collections c "
    "WHERE c.superseded_by <> '' AND NOT EXISTS (SELECT 1 FROM nexus.catalog_collections s "
    "WHERE s.tenant_id = c.tenant_id AND s.name = c.superseded_by) ORDER BY 1, 2, 3;"
)

#: The tombstones the stamp resolved to their own id (retired, no successor): ``superseded_by_id`` equals ``collection_id``.
SELF_ID_QUERY = (
    "SELECT c.tenant_id, c.content_type, c.name FROM nexus.catalog_collections c "
    "WHERE c.superseded_by <> '' AND c.superseded_by_id = c.collection_id ORDER BY 1, 2, 3;"
)


def _not_known(detail: str = NOT_KNOWN) -> list[HealthResult]:
    return [HealthResult(label=SUMMARY_LABEL, ok=True, detail=detail)]


def _dicts(value: object) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _by_type(value: object) -> str:
    """``docs: 3, unknown: 2`` from a content-type count map, largest first; ``none`` when it holds nothing."""
    if not isinstance(value, dict):
        return "none"
    pairs = sorted(((str(k), _int(v)) for k, v in value.items()), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{k}: {n}" for k, n in pairs) or "none"


def _summary_row(field: dict[str, Any]) -> HealthResult:
    summary = str(field.get("summary") or "")
    holding, total = field.get("gates_holding"), field.get("gates_total")
    if isinstance(holding, int) and isinstance(total, int) and not isinstance(holding, bool):
        detail = f"{holding} of {total} gates hold: {summary}"
    else:
        detail = summary
    return HealthResult(label=SUMMARY_LABEL, ok=True, detail=detail)


def _id_index_row(field: dict[str, Any]) -> HealthResult | None:
    """The INVALID id-predicate index condition, or ``None`` when no id index is INVALID."""
    if field.get("id_index_invalid") is not True:
        return None
    latched = field.get("gate3_latched") is True
    detail = (
        "an id-predicate per-collection index is INVALID: a build or REINDEX is running, or one was killed. The engine's reconciler drops an "
        "INVALID index and builds it again on its next pass."
    )
    if latched:
        return HealthResult(
            label=ID_INDEX_LABEL, ok=False, warn=True,
            detail=f"{detail} Gate 3 has latched, so readers select by id: until it is rebuilt, searches of that collection have no index.",
            fix_suggestions=["Nothing to run. If it stays INVALID across reconciler passes, read the engine log for event=pci_build_failed."],
        )
    return HealthResult(label=ID_INDEX_LABEL, ok=True, detail=f"{detail} Gate 3 has not latched yet; readers still select by name.")


def _latch_posture_row(field: dict[str, Any]) -> HealthResult | None:
    """The latch's pci_off posture, or ``None`` when gate 3 did not latch while PCI was off."""
    if field.get("gate3_latch_pci_off") is not True:
        return None
    return HealthResult(
        label=LATCH_POSTURE_LABEL, ok=True,
        detail=(
            "gate 3 latched while NX_SEARCH_PCI was off, so no per-collection id index was required then. With PCI on the engine builds "
            "the id indexes first (as if gate 2 held); until they are VALID a search has no per-collection index, because the readers are "
            "on the id either way."
        ),
    )


#: What a collection rename waits for, by the engine's ``rename_missing`` value (RDR-229 P3.7; the 409 ``rename_refused`` spelling).
RENAME_WAITS: dict[str, str] = {
    "name_key_drop_move": "the name-key drop move (rdr229-name-key-drop), which the engine registers once gate 3 has latched and runs as a data move",
    "p35b_list_missing": "the list of P3.5b changesets this build must carry, which is missing from the build",
    "restart": "an engine restart, which applies the P3.5b changesets the one-row rename waits for",
    "readiness_unreadable": "a readable rename readiness: the engine could not read it on its last check",
    "p35c_readers_absent": "an engine whose readers select by id (P3.5c); this build's do not",
}


def _rename_row(field: dict[str, Any]) -> HealthResult | None:
    """What a collection rename waits for, or ``None`` when the engine says nothing (an older engine) or ``none``."""
    missing = field.get("rename_missing")
    if not isinstance(missing, str) or missing in ("", "none"):
        return None
    what = RENAME_WAITS.get(missing, f"the condition the engine names {missing!r}")
    return HealthResult(label=RENAME_LABEL, ok=True, detail=f"a collection rename is refused (409 rename_refused, missing={missing}) until {what}.")


def _check_row(constraint: str, what: str, counts: dict[str, Any] | None = None) -> HealthResult:
    if constraint == SUPERSEDED_BY_CHECK:
        if counts is not None:
            what = f"{what} For this tenant, {_int(counts.get('superseded_by_waiting'))} registry row(s) hold it back."
        fix = [
            "Nothing to run. The CHECK is held back on purpose: a tombstone with a blank model_version and no "
            "superseded_by_id would make D2's repair move (repair-catalog-collection-model-version) fail with 23514. "
            "The wait ends when that move completes or when RDR-229's superseded_by stamp fills the id; the next boot "
            "then adds the CHECK. If the repair move itself shows as failed in the Data moves rows, fix that first",
        ]
    else:
        fix = [
            f"{constraint} is part of RDR-229 P3.1's expand (vectors-068-collection-id-expand.xml) and should exist. "
            "If the R5 operator down-step ran and the engine was upgraded again, run the generated re-add as nexus_admin "
            f"(the SQL: {READD_SQL_URL}, saved from its Raw view and run with psql -X -v ON_ERROR_STOP=1 "
            f"--single-transaction -f rdr-229-p31-readd.sql; the steps around it: step 7 of {ROLLBACK_RUNBOOK_URL}); "
            "the engine refuses the id switch until the constraint is back",
        ]
    return HealthResult(label=f"RDR-229 CHECK {constraint}", ok=False, warn=True, detail=what, fix_suggestions=fix)


_RESTAMP_ANSWERS = (
    "It registers the move again (<label>-rerun-N, which supersedes the completed one); that stamps the rows that can now resolve and "
    "validates the CHECK. It answers 'registered: ...' or 'not registered: <reason>' and writes nothing else."
)


def _restamp(table: str) -> str:
    return f"as nexus_admin (the migrating role): SELECT nexus.rdr229_restamp('{table}'); {_RESTAMP_ANSWERS}"


def _validate_row(table: str, what: str, counts: dict[str, Any] | None, why_unread: str = "",
                  constraint_names: list[str] | None = None) -> HealthResult:
    """One table whose CHECK waits. With the caller's counts: the orphan shape (orphans above 0) or the restamp-alone shape (none).
    Without them: the engine's text as sent."""
    label = f"RDR-229 VALIDATE {table}"
    if counts is None:
        return HealthResult(
            label=label, ok=False, warn=True, detail=f"{what} (counts not read: {why_unread})",
            fix_suggestions=[
                "An orphan row names a collection with no registry row in its tenant, so the stamp cannot give it an id and the CHECK "
                "cannot be validated; an orphan row is write-frozen. Registering the collection alone stamps nothing: the table's "
                "stamp move is already complete. If there are any orphan rows, register each missing collection or delete the orphan "
                f"rows; then, {_restamp(table)} With no orphan row, that restamp step alone finishes it."])
    mine = next((t for t in _dicts(counts.get("tables")) if t.get("table") == table), {})
    orphans, unstamped = _int(mine.get("orphans")), _int(mine.get("unstamped"))
    constraints = ", ".join(constraint_names or []) or "its CHECK"
    if orphans:
        return HealthResult(
            label=label, ok=False, warn=True,
            detail=(f"waits: {orphans} orphan row(s) and {unstamped} unstamped row(s) of this tenant keep {constraints} on {table} "
                    "NOT VALID. An orphan row is write-frozen: any UPDATE of it fails with SQLSTATE 23514 until its collection is "
                    "registered or the row is deleted."),
            fix_suggestions=[
                "An orphan row names a collection with no registry row in its tenant, so the stamp cannot give it an id and the CHECK "
                "cannot be validated. Registering the collection alone stamps nothing: the table's stamp move is already complete. "
                f"Step 1: register each missing collection or delete the orphan rows. Step 2, {_restamp(table)} The engine counts "
                "what this tenant's session can see; the other tenants' rows are not counted here."])
    return HealthResult(
        label=label, ok=False, warn=True,
        detail=(f"waits: 0 orphan row(s) and {unstamped} unstamped row(s) of this tenant keep {constraints} on {table} NOT VALID."
                if unstamped else
                f"waits: no row of this tenant keeps {constraints} on {table} NOT VALID (0 orphan row(s), 0 unstamped row(s)); "
                "rows of another tenant do."),
        fix_suggestions=[
            f"This tenant has no orphan row, so there is nothing to look for. Run, {_restamp(table)} That restamp step alone "
            "finishes it. If it answers 'not registered:' and names an orphan row, the row is another tenant's: run it again once "
            "that tenant has cleared it."])


def _dangling_rows(counts: dict[str, Any] | None, why_unread: str) -> list[HealthResult]:
    if counts is None:
        return [HealthResult(
            label=DANGLING_LABEL, ok=True,
            detail=f"not read: the dangling-tombstone count is this tenant's own, from GET /v1/catalog{rdr229_completion_counts.ROUTE}; {why_unread}")]
    dangling = counts.get("dangling_tombstones")
    dangling = dangling if isinstance(dangling, dict) else {}
    registry = _int(counts.get("registry_rows"))
    total = _int(dangling.get("total"))
    rows: list[HealthResult] = []
    if total:
        rows.append(HealthResult(
            label=DANGLING_LABEL, ok=True,
            detail=(f"{total} dangling tombstone(s) of {registry} registry rows of this tenant (superseded_by names no row of the "
                    f"tenant); by content type: {_by_type(dangling.get('by_content_type'))}"),
            fix_suggestions=[f"List them by name, as nexus_admin (read only): {DANGLING_QUERY}"]))
    else:
        rows.append(HealthResult(
            label=DANGLING_LABEL, ok=True, detail=f"no dangling tombstone among {registry} registry rows of this tenant"))
    own = dangling.get("self_id")
    own_total = _int(own.get("total")) if isinstance(own, dict) else 0
    if own_total:
        rows.append(HealthResult(
            label=SELF_ID_LABEL, ok=True,
            detail=(f"{own_total} tombstone(s) of this tenant resolved to their own id (retired, no successor); "
                    f"by content type: {_by_type(own.get('by_content_type') if isinstance(own, dict) else None)}"),
            fix_suggestions=[f"List them by name, as nexus_admin (read only): {SELF_ID_QUERY}"]))
    else:
        rows.append(HealthResult(label=SELF_ID_LABEL, ok=True, detail="no self-id tombstone (retired with no successor)"))
    return rows


def _default_counts_reader() -> Callable[[], dict[str, Any]]:
    """The production reader: the catalog reader's client, asked for the route. Built lazily, so a doctor run on a box with no
    catalog costs nothing."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to keep CLI startup fast and avoid a cycle

    reader = make_catalog_reader()
    if reader is None:
        raise LookupError("there is no catalog")
    return lambda: rdr229_completion_counts.read_rdr229_completion_counts(reader)


def _read_counts(counts_reader: Callable[[], dict[str, Any]] | None) -> tuple[dict[str, Any] | None, str]:
    """``(body, "")`` or ``(None, why)``: never raises, and an unread count is never an empty one."""
    try:
        read = counts_reader if counts_reader is not None else _default_counts_reader()
        return read(), ""
    except EngineRouteUnavailable as exc:
        return None, exc.describe()
    except Exception as exc:  # noqa: BLE001 — best-effort: a failed read is "cannot say", never "none", and never crashes `nx doctor`
        _log.debug("doctor_rdr229_completion_counts_failed", error=str(exc))
        return None, "the catalog could not be asked for the counts"


def check_rdr229_completion(
    engine_status: object = _ENGINE_STATUS_UNSET, *, counts_reader: Callable[[], dict[str, Any]] | None = None,
) -> list[HealthResult]:
    """The summary row, a warning per missing CHECK and per table whose VALIDATE waits on orphans, and the two count rows.
    Never red.

    *engine_status* is the ``GET /v1/status`` body the doctor already fetched (``None`` when that failed); left unset, this
    fetches its own. *counts_reader* returns the authenticated route's body for the caller's tenant (it raises
    :class:`~nexus.catalog.engine_reads.route_unavailable.EngineRouteUnavailable` when the engine cannot say); left unset, it asks
    the catalog reader. Not applicable (ok) when the engine cannot be reached; "not known" (ok, and not "nothing waiting")
    when the body has no ``rdr229_completion`` field.
    """
    status: dict[str, Any] | None
    if engine_status is _ENGINE_STATUS_UNSET:
        try:
            from nexus.db.http_engine_status import fetch_engine_status  # noqa: PLC0415 — deferred to keep CLI startup fast

            status = fetch_engine_status()
        except Exception as exc:  # noqa: BLE001 — best-effort: must not crash `nx doctor`
            _log.debug("doctor_rdr229_completion_probe_failed", error=str(exc))
            status = None
    else:
        status = engine_status if isinstance(engine_status, dict) else None
    if status is None:
        return _not_known("not applicable: the engine's status endpoint could not be read")
    field = status.get(FIELD)
    if not isinstance(field, dict):
        return _not_known()
    counts, why_unread = _read_counts(counts_reader)
    rows = [_summary_row(field)]
    id_row = _id_index_row(field)
    if id_row is not None:
        rows.append(id_row)
    posture_row = _latch_posture_row(field)
    if posture_row is not None:
        rows.append(posture_row)
    rename_row = _rename_row(field)
    if rename_row is not None:
        rows.append(rename_row)
    for c in _dicts(field.get("checks_missing")):
        constraint = c.get("constraint")
        if isinstance(constraint, str) and constraint:
            rows.append(_check_row(constraint, str(c.get("what") or ""), counts))
    for w in _dicts(field.get("validate_waits")):
        table = w.get("table")
        if isinstance(table, str) and table:
            names = [c for c in (w.get("constraints") if isinstance(w.get("constraints"), list) else []) if isinstance(c, str)]
            rows.append(_validate_row(table, str(w.get("what") or ""), counts, why_unread, names))
    rows.extend(_dangling_rows(counts, why_unread))
    return rows
