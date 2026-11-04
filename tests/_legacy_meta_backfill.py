# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-document chunk-row reader of ``nx catalog backfill-meta`` as it stood before nexus-ldf2s, kept verbatim.

It is the reference ``tests/test_ldf2s_backfill_throttle.py`` runs next to the grouped reader to prove the two
plan the same writes and report the same counts, and to count the requests the old reader made. Only the two
function names are changed (``legacy_`` prefix); the bodies are the old ones. Delete with that test once the
grouped reader has been in a release.
"""
from __future__ import annotations

from typing import Any

import structlog

from nexus.catalog.meta_backfill import (
    _BATCH, PRESENT, ROW_SAMPLE, SHARED, Plan, Report, VacuousRunError, _chunk_rows, _meta, _owners_of, _record, _spread,
    doc_row_keys_wanted, note_needs_ownership, plan_indexed_rows, plan_note, plan_source_format, plan_tags_category,
)

_log = structlog.get_logger(__name__)


def legacy_backfill_meta(reader: Any, t3: Any, writer: Any | None, *, apply: bool, batch: int = _BATCH) -> Report:
    """Examine every live document and stamp (``apply``) or count (dry run) what :func:`plan_note` and
    :func:`plan_indexed_rows` allow. Raises :class:`VacuousRunError` when the
    catalog lists no document."""
    entries = [e for e in reader.all_documents() if not getattr(e, "alias_of", "")]
    if not entries:
        raise VacuousRunError("examined 0 documents: an empty catalog is a failed sweep, not a clean one")
    live = {str(e.tumbler): e for e in entries}
    report = Report(applied=apply)
    for start in range(0, len(entries), batch):
        legacy_run_batch(reader, t3, writer, entries[start:start + batch], live, apply, report)
    return report


def legacy_run_batch(reader: Any, t3: Any, writer: Any | None, entries: list[Any], live: dict[str, Any], apply: bool, report: Report) -> None:
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
        if kind == "doc":
            # derived from the entry: no lookup, so a failed one does not withhold them
            parts.append(plan_source_format(e))
            parts.append(plan_tags_category(e))
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
