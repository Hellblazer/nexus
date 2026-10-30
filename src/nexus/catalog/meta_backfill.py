# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one-time, fill-only stamp of the new catalog ``meta`` keys from chunk rows (nexus-6pfgb.10, RDR-236 Phase 1).

Documents written before the Phase 1 homes landed (nexus-6pfgb.5 to .9, .38) hold their document-level
facts only on chunk rows, where a chunk two documents share keeps the LAST writer's value. This module
decides, per document, which keys the catalog may take from those rows, and nothing else:

* **Fill-only, by presence.** A key already in the entry's ``meta`` (whatever its value, a retraction
  ``''`` / ``0`` / ``[]`` included) is never touched, and a non-empty catalog column is never replaced.
  A key is sent only with a real value: the catalog merges ``meta`` and an explicit empty overwrites.
* **Notes** (``content_type`` ``knowledge``, Sam's option B): ``ttl_days`` and ``put_at`` come only from a
  row :func:`~nexus.catalog.note_ttl.row_written_by` accepts (stamp AND title), ``put_at`` being that row's
  ``indexed_at``. ``tags``, ``category`` and ``source_agent`` come from that same row or, when there is
  none, from a piece row of a chunk the note solely owns that :func:`~nexus.catalog.note_ttl.row_may_give_tags`
  allows; a note recovered that way has no TTL of its own to give, so it gets ``put_at`` from that row and
  ``ttl_days`` 0, and leaves the keyless count. The rules are the helpers the expiry sweep and the
  recovery-bundle export use; they are not forked here. An entry that already holds a note key is
  authoritative (an absent key is an unset one): only ``put_at`` may still be filled, and only from a row
  whose TTL is the entry's.
* **Indexed documents**: ``tags``, ``category`` and ``source_format`` are NOT backfilled (RDR-236 Phase 3
  makes them GENERATED columns). ``extraction_method``, ``quality_gate_overridden`` (only where the rows say ``True``; a
  missing key is never read as ``False``), ``extraction_source`` (only where a row carries a value: an absent key means "file" and is never stamped
  from absence), ``frecency_score`` (with
  ``frecency_computed_at`` set to the latest ``indexed_at`` of the rows that carry it; a score of 0 is the
  scorer's "none" and is not sent) and the catalog ``author`` (from ``source_author``, where the column is
  empty, and only when the row holds a plain string: the row writer ``str()``-ed the value, so a list repr
  or ``"None"`` is skipped and counted) are copied from chunks the document SOLELY owns.
* **Authorship.** A chunk row is one per ``(collection, chash)``, so a copy needs the document to be the
  chunk's only live owner in its own collection, and the row to be no later than the document
  (``indexed_at`` + :data:`~nexus.catalog.note_ttl.PUT_SKEW`): a row that is later was written by someone
  else. The engine's ``last_written_at`` is not on any 0.1.165 chunk-row read, so the row's own
  ``indexed_at`` stands in for it, and a row with none is not attributable. A document whose usable rows
  disagree on a key is residue. KNOWN GAP: ownership is read from live manifests, so a TOMBSTONED co-owner
  is not counted (the rule asks for it; no client read on 0.1.165 lists a tombstoned document's chunks).
  The ``indexed_at`` guard is what stands against a departed co-owner that wrote last.
* A document whose chunks are all shared is skipped and counted. A failed owner or row lookup withholds
  that document's keys and counts it; it never fails the run. A run that examined no document fails.

Reading chunk rows is this module's job. Its ``row_meta`` reads are the sanctioned legacy reads of the
key table until Phase 4 deletes the keys (``tests/chunk_metadata_read_sites.toml``).
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from nexus.catalog.note_ttl import PUT_SKEW, _parse_iso, note_ttl_days, row_may_give_tags, row_written_by
from nexus.catalog.store_hook import NOTE_META_KEYS, split_note_tags
from nexus.ttl import MAX_TTL_DAYS

_log = structlog.get_logger(__name__)

#: Statuses a key can end in (a residue carries its reason: ``residue:<reason>``).
STAMPED, PRESENT, SHARED = "stamped", "present", "shared"

#: The keys each kind of document is judged on, in report order.
NOTE_KEYS: tuple[str, ...] = ("put_at", "ttl_days", "tags", "category", "source_agent")
DOC_KEYS: tuple[str, ...] = (
    "extraction_method", "quality_gate_overridden", "extraction_source", "frecency_score", "author",
)

#: Single-owner rows read per indexed document. A document's chunks come from one index call, so the
#: values agree across them; reading every row of a 2000-chunk file would move its text for nothing.
#: The sample is spread over the document, so a stale tail still shows.
ROW_SAMPLE = 64

_BATCH = 50


class VacuousRunError(RuntimeError):
    """The run examined no document: a sweep that found nothing to check is a failure, not a pass."""


@dataclass
class Plan:
    """What one document gets: the ``meta`` keys to send, the column to fill, and each key's outcome."""

    meta: dict[str, Any] = field(default_factory=dict)
    author: str = ""
    outcome: dict[str, str] = field(default_factory=dict)

    def has_writes(self) -> bool:
        return bool(self.meta) or bool(self.author)


def _meta(entry: Any) -> dict[str, Any]:
    meta = getattr(entry, "meta", None)
    return meta if isinstance(meta, dict) else {}


def _attributable(entry: Any, row_meta: dict, skew: Any = PUT_SKEW) -> str:
    """``""`` when the row is no later than the document, else the reason it cannot be the document's.

    ``last_written_at`` is not on a chunk-row read, so the row's own ``indexed_at`` is compared with the
    document's (the engine refreshes the document's on every manifest write, so a row that is LATER than
    it plus the skew was written by someone else)."""
    stamped = _parse_iso(row_meta.get("indexed_at"))
    if stamped is None:
        return "no_indexed_at"
    ceiling = _parse_iso(getattr(entry, "indexed_at", ""))
    if ceiling is None:
        return "document_has_no_indexed_at"
    return "" if stamped - ceiling <= skew else "row_newer"


# ── notes ──────────────────────────────────────────────────────────────────────────────────────────

def plan_note(
    entry: Any, pieces: Sequence[tuple[str, dict | None]], sole: Iterable[str], *, skew: Any = PUT_SKEW,
) -> Plan:
    """The note keys the catalog may take for *entry* (a ``knowledge`` document).

    *pieces* is ``(chash, chunk-row metadata or None)`` per manifest row, in position order; *sole* is the
    set of chashes whose only live owner in the note's own collection is this note (empty when the
    caller did not need ownership, see :func:`note_needs_ownership`)."""
    meta = _meta(entry)
    plan = Plan()
    held = [k for k in NOTE_META_KEYS if k in meta]
    if isinstance(meta.get("tags"), str):
        plan.outcome["tags_legacy_string"] = PRESENT  # the .38 rung converts it; this verb never does
    if not held:
        wanted = list(NOTE_KEYS)
    elif "put_at" not in meta:
        wanted = ["put_at"]
    else:
        wanted = []
    for key in NOTE_KEYS:
        if key not in wanted:
            plan.outcome[key] = PRESENT
    if not wanted:
        return plan

    tumbler, title = entry.tumbler, getattr(entry, "title", "") or ""
    sole_set = set(sole)
    written = next((r for _c, r in pieces if r is not None and row_written_by(tumbler, r, owner_title=title)), None)
    chosen, chosen_is_written = written, written is not None
    if chosen is None:
        chosen = next((
            r for c, r in pieces
            if r is not None and c in sole_set and row_may_give_tags(tumbler, r, owner_title=title)
        ), None)
    if chosen is None:
        reason = SHARED if any(r is not None for _c, r in pieces) else "residue:no_row"
        for key in wanted:
            plan.outcome[key] = reason
        return plan
    refusal = _attributable(entry, chosen, skew)
    if refusal:
        for key in wanted:
            plan.outcome[key] = f"residue:{refusal}"
        return plan

    row_days = min(note_ttl_days(chosen), MAX_TTL_DAYS) if chosen_is_written else 0
    if held:  # only put_at is wanted; the entry's TTL is the entry's, so the row must be the same put
        if not chosen_is_written:
            plan.outcome["put_at"] = "residue:no_written_row"
            return plan
        if row_days != note_ttl_days(meta):
            plan.outcome["put_at"] = "residue:row_ttl_differs"
            return plan
    plan.meta["put_at"] = str(chosen.get("indexed_at"))
    plan.outcome["put_at"] = STAMPED
    if "ttl_days" in wanted:
        plan.meta["ttl_days"] = row_days  # 0 for a note with no TTL of its own: it leaves the keyless count
        plan.outcome["ttl_days"] = STAMPED
    given: dict[str, Any] = {
        "tags": split_note_tags(chosen.get("tags")),
        "category": str(chosen.get("category") or ""),
        "source_agent": str(chosen.get("source_agent") or ""),
    }
    for key, value in given.items():
        if key not in wanted:
            continue
        if value:
            plan.meta[key] = value
            plan.outcome[key] = STAMPED
        else:
            plan.outcome[key] = "residue:empty"
    return plan


def note_needs_ownership(entry: Any, pieces: Sequence[tuple[str, dict | None]]) -> list[str]:
    """The chashes whose live owners :func:`plan_note` needs: none when the entry has nothing to fill or
    a row the note itself wrote decides it, else the pieces a sole-owner row could come from."""
    meta = _meta(entry)
    if any(k in meta for k in NOTE_META_KEYS) and "put_at" in meta:
        return []
    tumbler, title = entry.tumbler, getattr(entry, "title", "") or ""
    if any(r is not None and row_written_by(tumbler, r, owner_title=title) for _c, r in pieces):
        return []
    return [c for c, r in pieces if r is not None and row_may_give_tags(tumbler, r, owner_title=title)]


# ── indexed documents ──────────────────────────────────────────────────────────────────────────────

def doc_row_keys_wanted(entry: Any) -> list[str]:
    """The row-sourced keys the entry still lacks."""
    meta = _meta(entry)
    wanted = [k for k in ("extraction_method", "quality_gate_overridden", "extraction_source", "frecency_score") if k not in meta]
    if not (getattr(entry, "author", "") or "").strip():
        wanted.append("author")
    return wanted


_NOT_PLAIN: Any = object()  # repr() of it is unique, so rows that disagree on it still disagree


def _plain_author(value: Any) -> Any:
    """A row's ``source_author`` as a catalog author: the stripped string, ``None`` when empty, and
    :data:`_NOT_PLAIN` for what the row writer's ``str(value)`` leaves of a non-string (``"None"``, a list
    repr starting with ``[``)."""
    text = str(value or "").strip()
    if not text:
        return None
    return _NOT_PLAIN if text == "None" or text.startswith("[") else text


def _real_frecency(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def plan_indexed_rows(entry: Any, rows: Sequence[dict], *, n_chunks: int, skew: Any = PUT_SKEW) -> Plan:
    """The row-sourced keys for *entry* from *rows*, the chunk rows of chunks it solely owns.

    *n_chunks* is the document's manifest size: with no sole-owner row, a document that has chunks has
    only shared ones (:data:`SHARED`), one with none is residue."""
    meta = _meta(entry)
    plan = Plan()
    wanted = doc_row_keys_wanted(entry)
    for key in DOC_KEYS:
        if key not in wanted:
            plan.outcome[key] = PRESENT
    if not wanted:
        return plan
    if not rows:
        for key in wanted:
            plan.outcome[key] = SHARED if n_chunks else "residue:no_chunks"
        return plan
    usable: list[dict] = []
    refusals: Counter[str] = Counter()
    for row in rows:
        why = _attributable(entry, row, skew)
        if why:
            refusals[why] += 1
        else:
            usable.append(row)
    if not usable:
        why = refusals.most_common(1)[0][0]
        for key in wanted:
            plan.outcome[key] = f"residue:{why}"
        return plan

    def agreed(values: list[Any]) -> tuple[bool, Any]:
        distinct = {repr(v) for v in values}
        return len(distinct) == 1, values[0]

    reads = {
        "frecency_score": lambda r: _real_frecency(r.get("frecency_score")),
        "quality_gate_overridden": lambda r: True if r.get("quality_gate_overridden") is True else None,
        "author": lambda r: _plain_author(r.get("source_author")),
        "extraction_method": lambda r: str(r.get("extraction_method") or "") or None,
        "extraction_source": lambda r: str(r.get("extraction_source") or "") or None,
    }
    for key in wanted:
        vals = [reads[key](r) for r in usable]
        same, value = agreed(vals)
        if not same:
            plan.outcome[key] = "residue:disagree"
        elif value is None:
            plan.outcome[key] = "residue:empty"
        elif value is _NOT_PLAIN:
            plan.outcome[key] = "residue:author_not_plain"
        elif key == "author":
            plan.author = value
            plan.outcome[key] = STAMPED
        else:
            plan.meta[key] = value
            plan.outcome[key] = STAMPED
            if key == "frecency_score":
                plan.meta["frecency_computed_at"] = max(str(r.get("indexed_at")) for r in usable)
    return plan


# ── the run ────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class KeyStats:
    examined: int = 0
    present: int = 0
    shared: int = 0
    stamped: int = 0
    residue: Counter = field(default_factory=Counter)

    def add(self, status: str) -> None:
        self.examined += 1
        if status == PRESENT:
            self.present += 1
        elif status == SHARED:
            self.shared += 1
        elif status == STAMPED:
            self.stamped += 1
        else:
            self.residue[status.partition(":")[2] or status] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined, "present": self.present, "skipped_shared": self.shared,
            "stamped": self.stamped, "residue": sum(self.residue.values()), "residue_reasons": dict(self.residue),
        }


@dataclass
class Report:
    documents_examined: int = 0
    documents_notes: int = 0
    documents_complete: int = 0     # nothing left to fill
    documents_all_shared: int = 0   # needed a row, held only shared chunks
    documents_to_stamp: int = 0
    documents_written: int = 0
    lookups_failed: int = 0         # documents whose owner or row read failed: keys withheld
    writes_failed: int = 0
    tags_legacy_strings_left: int = 0
    keys: dict[str, KeyStats] = field(default_factory=dict)
    applied: bool = False

    def stats(self, kind: str, key: str) -> KeyStats:
        return self.keys.setdefault(f"{kind}.{key}", KeyStats())

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "documents_examined": self.documents_examined, "documents_notes": self.documents_notes,
            "documents_complete": self.documents_complete, "documents_all_shared": self.documents_all_shared,
            "documents_to_stamp": self.documents_to_stamp, "documents_written": self.documents_written,
            "lookups_failed": self.lookups_failed, "writes_failed": self.writes_failed,
            "tags_legacy_strings_left_for_the_array_rung": self.tags_legacy_strings_left,
            "keys": {k: v.as_dict() for k, v in sorted(self.keys.items())},
        }


def _spread(items: list[Any], n: int) -> list[Any]:
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _owners_of(reader: Any, chashes: list[str]) -> dict[str, set[str]]:
    """``chash -> live owner tumblers`` (catalog-wide: the caller narrows to the chunk's collection).
    Raises on a failed lookup."""
    if not chashes:
        return {}
    positions = reader.chash_positions(chashes) if hasattr(reader, "chash_positions") else None
    if positions is not None:
        out: dict[str, set[str]] = {}
        for p in positions:
            out.setdefault(p["chash"], set()).add(str(p["doc_id"]))
        return out
    return {c: {str(t) for t in ts} for c, ts in (reader.docs_for_chashes(chashes) or {}).items()}


def _chunk_rows(t3: Any, collection: str, chashes: list[str]) -> dict[str, dict]:
    """``t3.chunk_rows``, where a collection the engine has no catalog row for holds no chunks."""
    from nexus.db.http_vector_client import VectorServiceError, is_unregistered_collection_error  # noqa: PLC0415 — deferred, heavy import graph

    try:
        return t3.chunk_rows(collection, chashes)
    except VectorServiceError as exc:
        if not is_unregistered_collection_error(exc):
            raise
        return {}


def backfill_meta(reader: Any, t3: Any, writer: Any | None, *, apply: bool, batch: int = _BATCH) -> Report:
    """Examine every live document and stamp (``apply``) or count (dry run) what :func:`plan_note` and
    :func:`plan_indexed_rows` allow. Raises :class:`VacuousRunError` when the
    catalog lists no document."""
    entries = [e for e in reader.all_documents() if not getattr(e, "alias_of", "")]
    if not entries:
        raise VacuousRunError("examined 0 documents: an empty catalog is a failed sweep, not a clean one")
    live = {str(e.tumbler): e for e in entries}
    report = Report(applied=apply)
    for start in range(0, len(entries), batch):
        _run_batch(reader, t3, writer, entries[start:start + batch], live, apply, report)
    return report


def _record(report: Report, kind: str, plan: Plan) -> None:
    for key, status in plan.outcome.items():
        if key == "tags_legacy_string":
            report.tags_legacy_strings_left += 1
            continue
        report.stats(kind, key).add(status)


def _run_batch(reader: Any, t3: Any, writer: Any | None, entries: list[Any], live: dict[str, Any], apply: bool, report: Report) -> None:
    notes = [e for e in entries if getattr(e, "content_type", "") == "knowledge"]
    docs = [e for e in entries if getattr(e, "content_type", "") != "knowledge"]
    report.documents_examined += len(entries)
    report.documents_notes += len(notes)

    needs_rows_docs = [e for e in docs if doc_row_keys_wanted(e)]
    needs_rows_notes = [e for e in notes if not ("put_at" in _meta(e))]
    plans: dict[str, tuple[str, Plan]] = {}
    failed: set[str] = set()

    manifests: dict[str, list[Any]] = {}
    want_manifest = [str(e.tumbler) for e in needs_rows_docs + needs_rows_notes]
    if want_manifest:
        try:
            manifests = reader.get_manifests(want_manifest) or {}
        except Exception as exc:  # noqa: BLE001 — a failed read withholds the batch's row-sourced keys and counts it
            _log.warning("meta_backfill_manifest_read_failed", documents=len(want_manifest), error=str(exc))
            failed.update(want_manifest)

    def coll_of(entry: Any, mrow: Any) -> str:
        return getattr(mrow, "collection", None) or entry.physical_collection

    def sole_in_collection(entry: Any, coll: str, owners: set[str]) -> bool:
        others = {t for t in owners if t != str(entry.tumbler) and (t not in live or live[t].physical_collection == coll)}
        return str(entry.tumbler) in owners and not others

    # notes first: rows of every piece, then ownership only for a note no written row decides
    note_pieces: dict[str, list[tuple[str, dict | None]]] = {}
    for e in needs_rows_notes:
        tid = str(e.tumbler)
        if tid in failed:
            continue
        try:
            mrows = manifests.get(tid, [])
            by_coll: dict[str, list[str]] = {}
            for m in mrows:
                by_coll.setdefault(coll_of(e, m), []).append(m.chash)
            fetched = {c: _chunk_rows(t3, c, hs) for c, hs in by_coll.items()}
            note_pieces[tid] = [(m.chash, fetched.get(coll_of(e, m), {}).get(m.chash)) for m in mrows]
        except Exception as exc:  # noqa: BLE001 — withhold this note's keys, count it, go on
            _log.warning("meta_backfill_note_rows_failed", tumbler=tid, error=str(exc))
            failed.add(tid)
    want_owner = {c for e in needs_rows_notes if str(e.tumbler) in note_pieces for c in note_needs_ownership(e, note_pieces[str(e.tumbler)])}
    owners: dict[str, set[str]] = {}
    owners_ok = True
    if want_owner:
        try:
            owners = _owners_of(reader, sorted(want_owner))
        except Exception as exc:  # noqa: BLE001 — the lookup only adds sole-owner keys; its loss is counted per note
            owners_ok = False
            _log.warning("meta_backfill_owner_lookup_failed", chashes=len(want_owner), error=str(exc))
    for e in notes:
        tid = str(e.tumbler)
        if "put_at" in _meta(e):
            plans[tid] = ("note", plan_note(e, [], ()))
        elif tid in note_pieces:
            need = note_needs_ownership(e, note_pieces[tid])
            if need and not owners_ok:
                failed.add(tid)
                continue
            sole = [c for c in need if sole_in_collection(e, e.physical_collection, owners.get(c, set()))]
            plans[tid] = ("note", plan_note(e, note_pieces[tid], sole))

    # indexed documents: ownership first (cheap), then only the sole-owner rows
    if needs_rows_docs:
        want = {m.chash for e in needs_rows_docs if str(e.tumbler) not in failed for m in manifests.get(str(e.tumbler), [])}
        ok = True
        doc_owners: dict[str, set[str]] = {}
        try:
            doc_owners = _owners_of(reader, sorted(want))
        except Exception as exc:  # noqa: BLE001 — withhold the batch's row-sourced keys, count the documents
            ok = False
            _log.warning("meta_backfill_owner_lookup_failed", chashes=len(want), error=str(exc))
        for e in needs_rows_docs:
            tid = str(e.tumbler)
            if tid in failed:
                continue
            if not ok:
                failed.add(tid)
                continue
            mrows = manifests.get(tid, [])
            single = [(coll_of(e, m), m.chash) for m in mrows if sole_in_collection(e, coll_of(e, m), doc_owners.get(m.chash, set()))]
            try:
                rows: list[dict] = []
                by_coll = {}
                for c, h in _spread(single, ROW_SAMPLE):
                    by_coll.setdefault(c, []).append(h)
                for c, hs in by_coll.items():
                    got = _chunk_rows(t3, c, hs)
                    rows.extend(got[h] for h in hs if h in got)
            except Exception as exc:  # noqa: BLE001 — withhold this document's keys, count it, go on
                _log.warning("meta_backfill_rows_failed", tumbler=tid, error=str(exc))
                failed.add(tid)
                continue
            plans[tid] = ("doc_rows", plan_indexed_rows(e, rows, n_chunks=len(mrows)))
    report.lookups_failed += len(failed)

    payloads: list[dict[str, Any]] = []
    note_ids = {str(e.tumbler) for e in notes}
    for e in entries:
        tid = str(e.tumbler)
        kind = "note" if tid in note_ids else "doc"
        parts: list[Plan] = []
        if tid in plans:
            parts.append(plans[tid][1])
        elif kind == "doc" and tid not in failed:
            parts.append(plan_indexed_rows(e, [], n_chunks=0))  # every row key already present: counted as such
        merged = Plan()
        for p in parts:
            _record(report, kind, p)
            merged.meta.update(p.meta)
            merged.author = merged.author or p.author
            merged.outcome.update(p.outcome)
        if tid in plans and any(s == SHARED for s in plans[tid][1].outcome.values()):
            report.documents_all_shared += 1
        if merged.has_writes():
            report.documents_to_stamp += 1
            payload: dict[str, Any] = {"tumbler": tid, "meta": merged.meta}
            if merged.author:
                payload["author"] = merged.author
            payloads.append(payload)
        elif tid not in failed and all(s == PRESENT for s in merged.outcome.values()):
            report.documents_complete += 1

    if apply and payloads:
        counts = writer.update_many(payloads)
        for payload, count in zip(payloads, counts):
            if count:
                report.documents_written += 1
            else:
                report.writes_failed += 1
                _log.warning("meta_backfill_write_failed", tumbler=payload["tumbler"])
