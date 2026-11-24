# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor``'s collection-tuple row (RDR-229 P3.8, nexus-p2m1x.28).

The engine's partial UNIQUE index ``catalog_collections_live_tuple_uq`` on ``(tenant_id, content_type, owner_id,
embedding_model, model_version) WHERE superseded_by = '' AND lifecycle_state <> 'quarantine'`` is created by the
changeset ``collections-001-2``, which waits (a Liquibase ``onFail="CONTINUE"`` precondition, never a halt) for the
re-own data move to complete and for no duplicate tuple to remain. A duplicate holds the index back at every boot with
nothing on screen; this row says which tuple and which collections.

The rows are the catalog's own collection listing, which is the caller's tenant and authenticated. The engine's
``GET /v1/status`` is unauthenticated and carries no tenant text, so it is not where a tuple and its collection names
belong. The row is a warning while a duplicate exists and is absent otherwise: a duplicate means the index cannot exist
(it is what holds it back), no duplicate means the index exists or waits only for the move, and the data-move rows
already show a move that is open, blocked or failed.

A second warning covers an index that is absent with nothing else naming why. ``GET /v1/status`` carries
``collection_tuple_index_present`` (an ``[additive]`` boolean); when it is ``false``, no duplicate is held by the catalog
listing, and ``boot_work`` has no re-own entry, no ``rdr229-restart`` notice and no other RDR-229 move, the row is still a
warning and says what to do (nexus-p2m1x.48): the engine builds the index at its next start, and the row names the restart
command. That is the window on a fresh install between the moves finishing (the re-own move completes seconds after boot 1,
after the Liquibase walk skipped the changeset) and the first restart, which no ``rdr229-restart`` notice covers because every
P3.5b changeset applied at boot 1. A second suggestion keeps the older diagnosis for an index still absent after a restart:
read the boot log for ``collections-001-2``; a precondition error (for example its duplicate counter erroring under
``onError="CONTINUE"``) is indistinguishable from the client. An absent key is an engine that predates the field: no row. The
status body carries one boolean and no tenant text. A pending ``rdr229-restart`` notice, or another RDR-229 move still open,
explains the wait and shows no row: the one restart that notice names builds the index too, so there is one instruction, not two.

Kept out of :mod:`nexus.health` (a file near its size cap). The candidate set is ``CatalogRepository.collectionForTuple``'s:
a row with no supersession and a lifecycle other than ``quarantine``; a dormant or disputed row still holds its tuple.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from nexus.health import HealthResult

_log = structlog.get_logger(__name__)

LABEL = "Collection tuples"
INDEX_WAIT_LABEL = "Collection tuple index"
#: The re-own move the index waits for (nexus.data_moves.label), named in the hint.
REOWN_MOVE_LABEL = "rdr229-reown-repo-stems-from-curator"
#: The ``boot_work`` entry id of the re-own move (``data-move:<label>``): open, blocked, stalled or failed while it is listed.
REOWN_MOVE_ID = "data-move:" + REOWN_MOVE_LABEL
#: The ``boot_work`` entry id of the RDR-229 restart notice (``CompletionBootWork.RESTART_ENTRY_ID``): every stamp move is
#: complete and one engine restart applies the P3.5b changesets. That restart walks the changelog, so it builds this index too.
RESTART_ENTRY_ID = "rdr229-restart"
#: The id prefix of RDR-229's other data moves (stamp, fkvalidate, quarantine, residue): while one is listed the install is
#: still settling, and the restart notice is not due until every stamp move is complete.
RDR229_MOVE_PREFIX = "data-move:rdr229-"
#: Tuples named in the row; the rest are counted.
_NAMED = 5
_UNSET: Any = object()


@dataclass(frozen=True)
class DuplicateTuple:
    """One live tuple held by more than one registry row."""

    content_type: str
    owner_id: str
    embedding_model: str
    model_version: str
    names: tuple[str, ...]


def _holds_tuple(row: dict[str, Any]) -> bool:
    """The index predicate: not superseded, not quarantine (a missing key reads as live, an older engine's row)."""
    return not row.get("superseded_by") and row.get("lifecycle_state") != "quarantine"


def duplicate_tuples(rows: list[dict[str, Any]]) -> list[DuplicateTuple]:
    """The live tuples more than one of *rows* holds, ordered by tuple, each with its collection names sorted."""
    groups: dict[tuple[str, str, str, str], list[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or not _holds_tuple(row):
            continue
        key = tuple(str(row.get(k) or "") for k in ("content_type", "owner_id", "embedding_model", "model_version"))
        groups.setdefault(key, []).append(str(row.get("name") or ""))  # type: ignore[arg-type]
    return [DuplicateTuple(*key, tuple(sorted(names))) for key, names in sorted(groups.items()) if len(names) > 1]


def _describe(d: DuplicateTuple) -> str:
    version = d.model_version or "(none)"
    return (
        f"content_type={d.content_type} owner={d.owner_id} model={d.embedding_model} version={version}: "
        + ", ".join(d.names)
    )


def _index_waits_unexplained(rows: list[dict[str, Any]], dups: list[DuplicateTuple], engine_status: object) -> bool:
    """True when the engine says the index is absent and nothing the doctor can see explains it.

    Needs all three of: ``collection_tuple_index_present`` is the boolean ``False`` (an absent or non-boolean value is an
    older engine or an unread answer, never false); *rows* was read and holds no duplicate (an unread registry cannot rule
    one out, and the caller passes only a list here); and ``boot_work`` is a list with no entry that explains the wait.

    Three entries explain it, each the engine saying what it is doing (nexus-p2m1x.48). The re-own move itself (working,
    blocked, stalled, paused, queued or failed: the data-move rows report it). The ``rdr229-restart`` notice: one restart
    applies RDR-229's CONTINUE changesets, and the same boot walks ``collections-001-2``. Another RDR-229 move still open: on
    a fresh install the re-own move completes within seconds of the first boot, after the walk skipped the changeset, so the
    index waits for the first restart after the moves finish, and the restart notice is published only once every stamp move
    is complete. The row stays for the case it was written for: none of the three is listed.
    """
    if not isinstance(engine_status, dict) or dups:
        return False
    if engine_status.get("collection_tuple_index_present") is not False:
        return False
    work = engine_status.get("boot_work")
    if not isinstance(work, list):
        return False
    return not any(isinstance(e, dict) and _explains_the_wait(str(e.get("id") or "")) for e in work)


def _explains_the_wait(entry_id: str) -> bool:
    return entry_id in (REOWN_MOVE_ID, RESTART_ENTRY_ID) or entry_id.startswith(RDR229_MOVE_PREFIX)


def check_collection_tuples(rows: object = _UNSET, engine_status: object = None) -> list[HealthResult]:
    """The duplicate warning, and the unexplained-wait warning; no row for either when neither applies.

    The duplicate row names every duplicated live tuple (the first few in full, the rest counted). *rows* is the registry
    listing (``reader.list_collections()``); left unset, this reads it. A registry that cannot be read gives no row: an
    unreachable engine is the storage-service check's red, and a clean row this did not check would be worse than none.

    *engine_status* is the ``GET /v1/status`` body the doctor already fetched (``None``: none, so no wait row). The wait
    row needs the registry listing too, to rule a duplicate out.
    """
    if rows is _UNSET:
        try:
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

            reader = make_catalog_reader()
            rows = reader.list_collections() if reader is not None else None
        except Exception as exc:  # noqa: BLE001 — best-effort: must not crash `nx doctor`
            _log.debug("doctor_collection_tuples_unread", error=str(exc))
            rows = None
    if not isinstance(rows, list):
        return []
    dups = duplicate_tuples(rows)
    if not dups:
        if _index_waits_unexplained(rows, dups, engine_status):
            return [_index_wait_row()]
        return []
    shown = "; ".join(_describe(d) for d in dups[:_NAMED])
    more = f"; and {len(dups) - _NAMED} more" if len(dups) > _NAMED else ""
    return [HealthResult(
        label=LABEL, ok=False, warn=True,
        detail=f"{len(dups)} live tuple(s) held by more than one collection, so the engine's unique index waits: {shown}{more}",
        fix_suggestions=[
            "The engine builds the unique index (changeset collections-001-2) at the first start where the re-own move "
            f"({REOWN_MOVE_LABEL}) has completed and no live tuple is shared. Until then nothing is lost and nothing fails; "
            "the engine already refuses to register a second live collection on a tuple (a mint, a named registration, an "
            "import and a restore all refuse it), so only the duplicate named here is left. Leave one collection per tuple "
            "live: supersede or delete the extra, or let the re-own move give the repository collection its own owner. "
            "Then restart the engine; no other step applies the index",
        ],
    )]


def _index_wait_row() -> HealthResult:
    from nexus.corpus import _SERVICE_RESTART_COMMAND  # noqa: PLC0415 — deferred to avoid circular import

    return HealthResult(
        label=INDEX_WAIT_LABEL, ok=False, warn=True,
        detail=(
            "the engine's live-tuple unique index is absent, no live tuple is shared and the re-own move "
            f"({REOWN_MOVE_LABEL}) is neither open nor failed: the engine builds the index at its next start"
        ),
        fix_suggestions=[
            f"Restart the engine: {_SERVICE_RESTART_COMMAND}. The re-own move has completed, so the changeset "
            "collections-001-2 applies at that start. Nothing is lost and nothing fails while it waits: the engine already "
            "refuses to register a second live collection on a tuple, with or without the index",
            "If the index is still absent after a restart, read the engine's boot log for the Liquibase output of changeset "
            "collections-001-2 (a precondition that errored, for example its duplicate counter, is skipped and retried at "
            "every boot, never halted; the client cannot tell the two apart), and for an index build that was refused. Fix "
            "what the log names and restart the engine; no other step applies the index",
        ],
    )
