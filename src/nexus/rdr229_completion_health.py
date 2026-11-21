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
  hit 23514); the engine says how many rows, and the wait clears by itself.
* ``validate_waits``: a table whose stamp move is done and whose CHECK is still NOT VALID because rows violate it: orphans,
  a name with no registry row in its tenant, which the stamp cannot give an id. The engine's own ``what`` text is shown as it
  sent it; the step is ``nexus.rdr229_restamp``, which registers the stamp move again once no orphan remains.
* ``dangling_tombstones``: R7's pre-flight, COUNTS by content type of the registry's tombstones whose ``superseded_by`` names
  no row of their tenant, and of the tombstones the stamp resolved to their own id (retired, no successor). ``/v1/status``
  is unauthenticated, so the engine sends no name; each row carries the read-only query that lists them by name, to run as
  ``nexus_admin``.

None of these is red: every waiting state clears by itself or by a step the row names, and a red row on every install
mid-rollout would hide a real one. Kept out of :mod:`nexus.health` (a file at its size cap); stdlib and
:class:`~nexus.health.HealthResult` only, the status body fetched by the caller as for the other engine-status rows.
"""
from __future__ import annotations

from typing import Any

import structlog

from nexus.health import _ENGINE_STATUS_UNSET, HealthResult

_log = structlog.get_logger(__name__)

#: The ``/v1/status`` key the engine reports under (``BootWorkRegistry``, ``StatusHandler``).
FIELD = "rdr229_completion"

SUMMARY_LABEL = "RDR-229 completion"
DANGLING_LABEL = "RDR-229 dangling tombstones"
SELF_ID_LABEL = "RDR-229 self-id tombstones"
NOT_KNOWN = (
    "not known: the engine has not reported its RDR-229 completion state yet (it has not read it, or it predates the check)"
)

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


def _check_row(constraint: str, what: str) -> HealthResult:
    if constraint == SUPERSEDED_BY_CHECK:
        fix = [
            "Nothing to run. The CHECK is held back on purpose: a tombstone with a blank model_version and no "
            "superseded_by_id would make D2's repair move (repair-catalog-collection-model-version) fail with 23514. "
            "The wait ends when that move completes or when RDR-229's superseded_by stamp fills the id; the next boot "
            "then adds the CHECK. If the repair move itself shows as failed in the Data moves rows, fix that first",
        ]
    else:
        fix = [
            f"{constraint} is part of RDR-229 P3.1's expand (vectors-068-collection-id-expand.xml) and should exist. "
            "If the R5 operator down-step ran and the engine was upgraded again, run the generated re-add "
            "(docs/runbooks/rdr-229-p31-readd.sql, step 7 of docs/runbooks/rdr-229-p31-rollback.md) as nexus_admin; "
            "the engine refuses the id switch until the constraint is back",
        ]
    return HealthResult(label=f"RDR-229 CHECK {constraint}", ok=False, warn=True, detail=what, fix_suggestions=fix)


def _validate_row(table: str, what: str) -> HealthResult:
    return HealthResult(
        label=f"RDR-229 VALIDATE {table}", ok=False, warn=True, detail=what,
        fix_suggestions=[
            "An orphan row names a collection with no registry row in its tenant, so the stamp cannot give it an id and the "
            "CHECK cannot be validated. Registering the collection alone stamps nothing: the table's stamp move is already "
            "complete. Register each missing collection or delete the orphan rows, then, as nexus_admin (the migrating "
            f"role): SELECT nexus.rdr229_restamp('{table}'); It registers the move again (<label>-rerun-N, which "
            "supersedes the completed one); that stamps the rows that can now resolve and validates the CHECK. It answers "
            "'registered: ...' or 'not registered: <reason>' and writes nothing else. The engine counts what its admin role "
            "can see, so a table with row-level security and no owner-read policy can show a lower count than it holds",
        ],
    )


def _dangling_rows(dangling: object) -> list[HealthResult]:
    if not isinstance(dangling, dict):
        return []
    if dangling.get("read") is not True:
        return [HealthResult(
            label=DANGLING_LABEL, ok=True,
            detail="not read: the engine could not read the registry for its dangling-tombstone count")]
    registry = _int(dangling.get("registry_rows"))
    total = _int(dangling.get("total"))
    rows: list[HealthResult] = []
    if total:
        rows.append(HealthResult(
            label=DANGLING_LABEL, ok=True,
            detail=(f"{total} dangling tombstone(s) of {registry} registry rows (superseded_by names no row of the tenant); "
                    f"by content type: {_by_type(dangling.get('by_content_type'))}"),
            fix_suggestions=[f"List them by name, as nexus_admin (read only): {DANGLING_QUERY}"]))
    else:
        rows.append(HealthResult(
            label=DANGLING_LABEL, ok=True, detail=f"no dangling tombstone among {registry} registry rows"))
    own = dangling.get("self_id")
    own_total = _int(own.get("total")) if isinstance(own, dict) else 0
    if own_total:
        rows.append(HealthResult(
            label=SELF_ID_LABEL, ok=True,
            detail=(f"{own_total} tombstone(s) resolved to their own id (retired, no successor); "
                    f"by content type: {_by_type(own.get('by_content_type') if isinstance(own, dict) else None)}"),
            fix_suggestions=[f"List them by name, as nexus_admin (read only): {SELF_ID_QUERY}"]))
    else:
        rows.append(HealthResult(label=SELF_ID_LABEL, ok=True, detail="no self-id tombstone (retired with no successor)"))
    return rows


def check_rdr229_completion(engine_status: object = _ENGINE_STATUS_UNSET) -> list[HealthResult]:
    """The summary row, a warning per missing CHECK and per table whose VALIDATE waits on orphans, and the two count rows.
    Never red.

    *engine_status* is the ``GET /v1/status`` body the doctor already fetched (``None`` when that failed); left unset, this
    fetches its own. Not applicable (ok) when the engine cannot be reached; "not known" (ok, and not "nothing waiting")
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
    rows = [_summary_row(field)]
    for c in _dicts(field.get("checks_missing")):
        constraint = c.get("constraint")
        if isinstance(constraint, str) and constraint:
            rows.append(_check_row(constraint, str(c.get("what") or "")))
    for w in _dicts(field.get("validate_waits")):
        table = w.get("table")
        if isinstance(table, str) and table:
            rows.append(_validate_row(table, str(w.get("what") or "")))
    rows.extend(_dangling_rows(field.get("dangling_tombstones")))
    return rows
