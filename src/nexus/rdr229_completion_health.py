# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s rows for RDR-229 P3.1's boot-time completion check (nexus-p2m1x.17).

After the schema walk the engine reads, on its admin connection and read only, whether each install's stamp moves have
finished and so whether the engine's id readers, the one-row rename and the index builds may switch (three gates, RDR-229
item 6). It reports in ``/v1/status`` ``boot_work`` (``CompletionBootWork.java``); this module reads those entries:

* ``rdr229-completion``: where the three gates stand. One ok row. The gates wait by design across releases (gate 3 cannot
  hold until P3.4 builds its id indexes), so the row informs and never warns. The engine sends it only while a gate waits
  (``boot_work`` is work in progress), so no entry at all means nothing waits, or the engine predates the check.
* ``rdr229-check:<constraint>``: a listed CHECK that is missing. A warning. The superseded_by one is held back on purpose by
  its changeset while a registry tombstone with a blank ``model_version`` has no ``superseded_by_id`` (D2's repair UPDATE
  would hit 23514); the engine says how many rows, and the wait clears by itself.
* ``rdr229-validate:<table>``: the table's stamp move is done and its CHECK is still NOT VALID because rows violate it:
  orphans, a name with no registry row in its tenant, which the stamp cannot give an id. The VALIDATE waits instead of
  failing a boot; the row names the table and the counts and says what the operator does.

None of these is red: every waiting state clears by itself or by a step the row names, and a red row on every install
mid-rollout would hide a real one. ``/v1/status`` is unauthenticated, so an entry carries counts, a table and a constraint
name and no collection or tenant. Kept out of :mod:`nexus.health` (a file at its size cap); stdlib and
:class:`~nexus.health.HealthResult` only, the status body fetched by the caller as for the other engine-status rows.
"""
from __future__ import annotations

import re
from typing import Any

import structlog

from nexus.health import _ENGINE_STATUS_UNSET, HealthResult

_log = structlog.get_logger(__name__)

#: The engine's ids (``CompletionBootWork.SUMMARY_ID``, ``CHECK_PREFIX``, ``VALIDATE_PREFIX``); a test reads the Java source.
SUMMARY_ID = "rdr229-completion"
CHECK_PREFIX = "rdr229-check:"
VALIDATE_PREFIX = "rdr229-validate:"

SUMMARY_LABEL = "RDR-229 completion"
NOT_APPLICABLE_NOT_REPORTED = (
    "nothing waiting: the engine reports no RDR-229 completion state (every gate holds, or it predates the check)"
)

#: The CHECK whose changeset waits on D2's data (``CompletionCheck.SUPERSEDED_BY_CHECK``).
SUPERSEDED_BY_CHECK = "catalog_collections_superseded_by_id_check"

_KEEPS = re.compile(r"\bkeep (\w+) on (\w+) NOT VALID\b")


def _entries(status: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The RDR-229 entries of ``status['boot_work']``; ``None`` when the engine predates ``boot_work``."""
    work = status.get("boot_work")
    if not isinstance(work, list):
        return None
    return [
        e for e in work
        if isinstance(e, dict) and isinstance(e.get("id"), str)
        and (e["id"] == SUMMARY_ID or e["id"].startswith((CHECK_PREFIX, VALIDATE_PREFIX)))
    ]


def _not_applicable(detail: str) -> list[HealthResult]:
    return [HealthResult(label=SUMMARY_LABEL, ok=True, detail=detail)]


def _summary_row(entry: dict[str, Any]) -> HealthResult:
    what = str(entry.get("what") or "")
    done, total = entry.get("done"), entry.get("total")
    if isinstance(done, int) and isinstance(total, int) and not isinstance(done, bool):
        detail = f"{done} of {total} gates hold: {what}"
    else:
        detail = what
    return HealthResult(label=SUMMARY_LABEL, ok=True, detail=detail)


def _check_row(constraint: str, entry: dict[str, Any]) -> HealthResult:
    what = str(entry.get("what") or "")
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
            "If the R5 operator down-step ran and the engine was upgraded again, re-add the constraint NOT VALID "
            "from that changeset's text as nexus_admin; the engine refuses the id switch until it is back",
        ]
    return HealthResult(label=f"RDR-229 CHECK {constraint}", ok=False, warn=True, detail=what, fix_suggestions=fix)


def _validate_row(table: str, entry: dict[str, Any]) -> HealthResult:
    what = str(entry.get("what") or "")
    m = _KEEPS.search(what)
    constraint = m.group(1) if m else "<the CHECK named in the detail>"
    return HealthResult(
        label=f"RDR-229 VALIDATE {table}", ok=False, warn=True, detail=what,
        fix_suggestions=[
            "An orphan row names a collection that has no registry row in its tenant, so the stamp cannot give it an id and "
            "the CHECK cannot be validated. Register that collection, or remove the rows, then validate the CHECK as "
            f"nexus_admin: ALTER TABLE nexus.{table} VALIDATE CONSTRAINT {constraint}; (a read of every row, no row is "
            "rewritten). The engine counts what its admin role can see, so a table with row-level security and no "
            "owner-read policy can show a lower count than it holds. Until then the engine keeps readers on the name path",
        ],
    )


def check_rdr229_completion(engine_status: object = _ENGINE_STATUS_UNSET) -> list[HealthResult]:
    """The summary row, a warning per missing CHECK and per table whose VALIDATE waits on orphans. Never red.

    *engine_status* is the ``GET /v1/status`` body the doctor already fetched (``None`` when that failed); left unset, this
    fetches its own. Not applicable (ok) when the engine cannot be reached or predates ``boot_work``; "nothing waiting" (ok) when it reports none of these entries.
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
        return _not_applicable("not applicable: the engine's status endpoint could not be read")
    entries = _entries(status)
    if entries is None:
        return _not_applicable("not applicable: this engine does not report its work (boot_work)")
    if not entries:
        return _not_applicable(NOT_APPLICABLE_NOT_REPORTED)
    rows: list[HealthResult] = []
    for entry in entries:
        entry_id = entry["id"]
        if entry_id == SUMMARY_ID:
            rows.append(_summary_row(entry))
        elif entry_id.startswith(CHECK_PREFIX):
            rows.append(_check_row(entry_id[len(CHECK_PREFIX):], entry))
        elif entry_id.startswith(VALIDATE_PREFIX):
            rows.append(_validate_row(entry_id[len(VALIDATE_PREFIX):], entry))
    return rows
