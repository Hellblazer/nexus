# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one-time, fill-only stamp of the new catalog ``meta`` keys from chunk rows (nexus-6pfgb.10, RDR-236 Phase 1).

Documents written before the Phase 1 homes landed (nexus-6pfgb.5 to .9, .38) hold their document-level
facts only on chunk rows, where a chunk two documents share keeps the LAST writer's value. This module
decides, per document, which keys the catalog may take from those rows, and nothing else:

* **Fill-only, by presence.** A key already in the entry's ``meta`` (whatever its value, a retraction
  ``''`` / ``0`` / ``[]`` included) is never touched, and a non-empty catalog column is never replaced.
  A key is sent only with a real value: the catalog merges ``meta`` and an explicit empty overwrites.
  The one exception is an indexed document's ``tags`` and ``category`` (nexus-3ax49, Sam's ruling of
  2026-11-01): they are filled when unset by the re-index's own predicate
  (:func:`~nexus.catalog.indexed_meta.is_unset`: absent, ``None``, ``''``, ``[]``) and kept otherwise, so the
  backfill and a re-index share one definition of empty. Notes and ``source_format`` keep the presence rule.
* **Notes** (``content_type`` ``knowledge``, Sam's option B): ``ttl_days`` and ``put_at`` come only from a
  row :func:`~nexus.catalog.note_ttl.row_written_by` accepts (stamp AND title), ``put_at`` being that row's
  ``indexed_at``. ``tags``, ``category`` and ``source_agent`` come from that same row or, when there is
  none, from a piece row of a chunk the note solely owns that :func:`~nexus.catalog.note_ttl.row_may_give_tags`
  allows; a note recovered that way has no TTL of its own to give, so it gets ``put_at`` from that row and
  ``ttl_days`` 0, and leaves the keyless count. The rules are the helpers the expiry sweep and the display
  fallback use; they are not forked here. The verb is operator-run (``nx catalog backfill-meta``); the
  upgrade ladder does not run it, and the recovery-bundle export reads no chunk row, so a keyless note this
  has not been run on exports with its keys empty. An entry that already holds a note key is
  authoritative (an absent key is an unset one): only ``put_at`` may still be filled, and only from a row
  whose TTL is the entry's. ``put_at`` counts as unset when it is absent, or when the entry carries a TTL and
  the key holds no non-empty string (``""``, null, a number): the table CHECK of nexus-6pfgb.44 reads those as
  absent, so they are filled too. A ``put_at`` of ``""`` on an entry with no TTL is the retraction and stays.
  KNOWN GAP: a TOMBSTONED note is never listed or updated here (the engine refuses to update a tombstone), so
  a tombstoned entry with a TTL and no ``put_at`` is left to the remedies in the tombstoned-violators section of that CHECK's runbook (prose; the backfill does not stamp it).
* **Indexed documents**: ``source_format`` (nexus-6pfgb.23, Sam's option A) is DERIVED from the entry's own
  ``content_type`` and ``file_path`` by :func:`~nexus.catalog.indexed_meta.catalog_source_format`, the copy of
  the engine's ``nexus.catalog_source_format``, so it reads no chunk row and needs no ownership: a pair the
  rule derives nothing for is residue (``no_rule``, or ``no_file_path`` when the entry has no path to read an
  extension from). ``tags`` and ``category`` (nexus-9cstf) are derived the same way, by
  :func:`~nexus.catalog.indexed_meta.backfill_tags_category`, from the content type and path through the
  register-time rules (:func:`~nexus.catalog.indexed_meta.document_register_meta`, and the type-keyed
  :func:`~nexus.catalog.indexed_meta.pdf_register_meta`: a catalog type ``paper`` at a ``.pdf`` path is what
  ``nx index pdf`` registers, ``pdf`` what the repo PDF path registers), each key by its own presence, with
  no chunk row read (each key is open when unset by the predicate above, not only when absent). A key the rule cannot settle is residue with its reason: ``ambiguous_indexer`` (``tags``
  of a prose or rdr file that is not ``.md`` / ``.markdown``: the repo pass tags the extension, ``nx index md``
  tags ``markdown``, and the entry cannot say which ran, except that by policy a document in its own repository's
  ``docs__`` / ``rdr__`` collection at a repo-relative path takes the extension, nexus-6pfgb.43; both keys of a prose or rdr file at a ``.pdf`` path,
  where ``nx index pdf`` would have stamped ``paper``), ``not_pdf_path`` (both keys of a ``paper`` whose path is
  not ``.pdf``), ``no_extension``, ``no_file_path`` (a ``paper`` with no path is an ``nx catalog backfill``
  placeholder), ``no_rule``.
  The engine's chunk-row fallback for a ``where`` on these keys stays until the row keys retire.
  ``extraction_method``, ``quality_gate_overridden`` (only where the rows say ``True``; a
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

import contextlib
import copy
import math
import os
import signal
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from nexus.catalog.indexed_meta import SOURCE_FORMAT_KEY, backfill_tags_category, catalog_source_format, is_unset
from nexus.catalog.note_ttl import (
    PUT_SKEW, expires_at, first_sign_reason, held_by_first_sign_alone, parse_iso, note_ttl_days, row_may_give_tags,
    row_written_by,
)
from nexus.catalog.store_hook import NOTE_META_KEYS, split_note_tags
from nexus.db import request_gate
from nexus.ttl import MAX_TTL_DAYS

_log = structlog.get_logger(__name__)

#: Statuses a key can end in (a residue carries its reason: ``residue:<reason>``).
STAMPED, PRESENT, SHARED = "stamped", "present", "shared"

#: Why a run stopped before its last batch: asked to (Ctrl-C, SIGTERM; or cut by a second one), or too many
#: rounds in a row with a failed lookup or write.
STOP_SIGNAL, STOP_FAILED_ROUNDS = "signal", "failed_rounds"

#: The keys each kind of document is judged on, in report order.
NOTE_KEYS: tuple[str, ...] = ("put_at", "ttl_days", "tags", "category", "source_agent")
DOC_KEYS: tuple[str, ...] = (
    "extraction_method", "quality_gate_overridden", "extraction_source", "frecency_score", "author",
)
#: The indexed-document keys that are derived from the entry itself, never read from a chunk row.
DERIVED_DOC_KEYS: tuple[str, ...] = (SOURCE_FORMAT_KEY, "tags", "category")

#: Single-owner rows read per indexed document. A document's chunks come from one index call, so the
#: values agree across them; reading every row of a 2000-chunk file would move its text for nothing.
#: The sample is spread over the document, so a stale tail still shows.
ROW_SAMPLE = 64

_BATCH = 50


class VacuousRunError(RuntimeError):
    """The run examined no document: a sweep that found nothing to check is a failure, not a pass."""


class ResumePointError(ValueError):
    """``--start-after`` names a tumbler the catalog does not list: there is no place to resume from."""


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
    stamped = parse_iso(row_meta.get("indexed_at"))
    if stamped is None:
        return "no_indexed_at"
    ceiling = parse_iso(getattr(entry, "indexed_at", ""))
    if ceiling is None:
        return "document_has_no_indexed_at"
    return "" if stamped - ceiling <= skew else "row_newer"


# ── notes ──────────────────────────────────────────────────────────────────────────────────────────

def put_at_is_open(meta: dict[str, Any]) -> bool:
    """Whether a note's ``put_at`` may still be filled: the key is absent, or the entry carries a TTL and the
    key holds no stamp (``""``, null, a number, anything but a non-empty string).

    A key's presence is final everywhere else in this module. This is the one exception, because the table
    CHECK on ``nexus.catalog_documents`` (nexus-6pfgb.44) reads a ``put_at`` of ``""`` as absent, so an entry
    with a TTL and ``put_at: ""`` is a violator the backfill must repair or the census never reads zero. An
    entry with no TTL of its own keeps ``""``: that pair is the retraction ``_NOTE_META_UNSET`` writes."""
    if "put_at" not in meta:
        return True
    stamp = meta["put_at"]
    return note_ttl_days(meta) > 0 and not (isinstance(stamp, str) and stamp != "")


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
    elif put_at_is_open(meta):
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
    if any(k in meta for k in NOTE_META_KEYS) and not put_at_is_open(meta):
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


def plan_source_format(entry: Any) -> Plan:
    """The ``source_format`` the catalog may stamp on an indexed *entry* (nexus-6pfgb.23).

    Fill-only by presence: a key the entry's ``meta`` holds (whatever its value) is final. Otherwise the value
    is the engine's own derivation for the entry's ``content_type`` and ``file_path``, so the stamped document
    and a derived one agree by construction. Nothing is read from a chunk row, so a document whose chunks are
    all shared gets it too. A pair the rule derives nothing for is residue: ``no_file_path`` when the entry has
    no path (a type outside the rules cannot be told from the type alone), else ``no_rule``."""
    plan = Plan()
    if SOURCE_FORMAT_KEY in _meta(entry):
        plan.outcome[SOURCE_FORMAT_KEY] = PRESENT
        return plan
    file_path = getattr(entry, "file_path", "") or ""
    derived = catalog_source_format(getattr(entry, "content_type", "") or "", file_path)
    if derived is None:
        plan.outcome[SOURCE_FORMAT_KEY] = "residue:no_rule" if file_path else "residue:no_file_path"
        return plan
    plan.meta[SOURCE_FORMAT_KEY] = derived
    plan.outcome[SOURCE_FORMAT_KEY] = STAMPED
    return plan


def repo_owner_prefixes(reader: Any) -> frozenset[str]:
    """The tumbler prefixes of every repository owner, deactivated ones included (a removed repo's documents
    were still registered by the repo pass). Empty when the reader cannot list owners: no document is then
    taken for repo-owned, which only leaves residue; the report carries the count (``repo_owners``)."""
    lister = getattr(reader, "list_owners", None)
    if lister is None:
        return frozenset()
    owners = lister(include_deactivated=True)  # a reader that cannot take the flag fails loudly, never drops owners
    return frozenset(
        str(o.get("tumbler_prefix") or "") for o in owners or []
        if isinstance(o, dict) and o.get("owner_type") == "repo" and o.get("tumbler_prefix")
    )


#: The collection types ``nx index repo`` mints for prose and RDRs (docs/collections.md).
_REPO_PROSE_COLLECTION_TYPES: frozenset[str] = frozenset({"docs", "rdr"})


def repo_prose_collections(reader: Any) -> dict[str, str]:
    """``{collection name: owner_id}`` for every ``docs`` / ``rdr`` collection row of the catalog. The owner is the
    row's ``owner_id`` column, which for these types the engine sets from the tumbler of the documents in the
    collection (docs/collections.md, nexus-6pbwx), never the name's segment. Empty when the reader cannot list them."""
    lister = getattr(reader, "list_collections", None)
    if lister is None:
        return {}
    out: dict[str, str] = {}
    for kind in sorted(_REPO_PROSE_COLLECTION_TYPES):  # the route's own content_type filter
        for r in lister(content_type=kind) or []:
            if isinstance(r, dict) and r.get("name"):
                out[str(r["name"])] = str(r.get("owner_id") or "")
    return out


def in_own_repo_collection(entry: Any, prefixes: frozenset[str], collections: Mapping[str, str]) -> bool:
    """True when *entry* is under a repository owner AND its collection is a ``docs`` / ``rdr`` collection whose
    catalog ``owner_id`` is that owner's segment (the owner prefix with ``-`` for ``.``)."""
    tumbler = str(getattr(entry, "tumbler", "") or "")
    owner = collections.get(str(getattr(entry, "physical_collection", "") or ""))
    if not owner:
        return False
    return any(tumbler.startswith(p + ".") and owner == p.replace(".", "-") for p in prefixes)


def plan_tags_category(
    entry: Any, repo_prefixes: frozenset[str] = frozenset(), repo_collections: Mapping[str, str] | None = None,
) -> Plan:
    """The ``tags`` and ``category`` the catalog may stamp on an indexed *entry* (nexus-9cstf).

    Derived the way :func:`plan_source_format` derives its key, from the entry's own ``content_type`` and
    ``file_path`` by :func:`~nexus.catalog.indexed_meta.backfill_tags_category` (which also decides, from the
    catalog type and the path's extension, whether ``nx index pdf`` or the repo PDF path wrote a PDF), so it
    reads no chunk row and a
    document whose chunks are all shared gets them too. Fill-only, per key, by the re-index's own definition of
    empty (:func:`~nexus.catalog.indexed_meta.is_unset`, nexus-3ax49): a key that is absent, ``None``, ``''``
    or ``[]`` is open and filled; any other value, a legacy string included, is held, and nothing is sent for
    it. A key the rule cannot settle is residue with its reason, never a guess."""
    meta = _meta(entry)
    plan = Plan()
    open_keys = [k for k in ("tags", "category") if is_unset(meta.get(k))]
    for key in ("tags", "category"):
        if key not in open_keys:
            plan.outcome[key] = PRESENT
    if not open_keys:
        return plan
    values, residue = backfill_tags_category(
        getattr(entry, "content_type", "") or "", getattr(entry, "file_path", "") or "",
        repo_owned=in_own_repo_collection(entry, repo_prefixes, repo_collections or {}),
    )
    for key in open_keys:
        if key in values:
            plan.meta[key] = values[key]
            plan.outcome[key] = STAMPED
        else:
            plan.outcome[key] = f"residue:{residue.get(key, 'no_rule')}"
    return plan


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
    repo_owners: int = 0            # repository owners the run saw: 0 means the repo-collection tags rule found none
    repo_listing_failed: bool = False  # the owner or collection listing failed, so the rule was off for the run
    keys: dict[str, KeyStats] = field(default_factory=dict)
    applied: bool = False
    interrupted: bool = False       # stopped before the last batch; only finished batches are counted
    batches_done: int = 0
    batches_total: int = 0
    stop_reason: str = ""           # STOP_SIGNAL (asked to stop, or a second interrupt) or STOP_FAILED_ROUNDS
    last_tumbler: str = ""          # the last document of the last finished batch (the --start-after, before one): where the run is
    resume_after: str = ""          # the --start-after for a rerun: the last document before the first batch with a failed lookup or
                                    # write, so a resume never skips a document whose keys were withheld (review r2 S1)
    cut_batch: int = 0              # the batch a second interrupt cut (0: none); its writes may have landed
    started_after: str = ""         # the --start-after tumbler this run skipped up to
    # nexus-6pfgb.65 (RDR-236 Phase 4 Step 1): the notes the TTL sweep's guard held by the first sign of
    # note_ttl.catalog_ttl_stale alone until Step 1, listed by a dry run asked to (list_first_sign) for Sam to
    # read. Read-only.
    ttl_first_sign_checked: bool = False    # a dry run looked, because it was asked to; no other run does
    ttl_first_sign_notes: list[dict[str, Any]] = field(default_factory=list)
    ttl_first_sign_unread: int = 0          # keyed notes whose manifest or rows could not be read: unknown, not "none"
    list_only: bool = False                 # the run made no stamping plan: it only listed

    def stats(self, kind: str, key: str) -> KeyStats:
        return self.keys.setdefault(f"{kind}.{key}", KeyStats())

    def as_dict(self) -> dict[str, Any]:
        out = {
            "applied": self.applied,
            "documents_examined": self.documents_examined, "documents_notes": self.documents_notes,
            "documents_complete": self.documents_complete, "documents_all_shared": self.documents_all_shared,
            "documents_to_stamp": self.documents_to_stamp, "documents_written": self.documents_written,
            "lookups_failed": self.lookups_failed, "writes_failed": self.writes_failed,
            "tags_legacy_strings_left_for_the_array_rung": self.tags_legacy_strings_left,
            "repo_owners": self.repo_owners,
            **({"repo_listing_failed": True} if self.repo_listing_failed else {}),
            "keys": {k: v.as_dict() for k, v in sorted(self.keys.items())},
        }
        if self.started_after:
            out["started_after"] = self.started_after
        if self.ttl_first_sign_checked:  # absent from an apply run, so its JSON is what it always was
            out["ttl_first_sign_only"] = {
                "count": len(self.ttl_first_sign_notes), "unread": self.ttl_first_sign_unread,
                "notes": self.ttl_first_sign_notes,
            }
        if self.list_only:  # the counts above carry no stamping plan: say so
            out["list_only"] = True
        if self.interrupted:  # absent from a finished run, so its JSON is what it always was
            out.update(
                interrupted=True, batches_done=self.batches_done, batches_total=self.batches_total,
                stop_reason=self.stop_reason, resume_after=self.resume_after,
            )
            if self.cut_batch:
                out["cut_batch_may_have_written"] = self.cut_batch
        return out


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
        return t3.chunk_rows(collection, chashes, sensitivity="include")
    except VectorServiceError as exc:
        if not is_unregistered_collection_error(exc):
            raise
        return {}


#: A run prints at most about this many progress lines, however many batches it has.
PROGRESS_LINES = 300

#: The "recent" request rate of a progress line is taken over this many finished rounds.
RECENT_ROUNDS = 10


@dataclass(frozen=True)
class Progress:
    """One progress report: where the run is after a finished batch, and how fast it is going.

    *requests* is the number of HTTP requests the process has sent (the request gate's count: every page,
    retry and fallback request, nexus-ldf2s), not a count of calls. The rate is given twice: since the start
    of the run and over the last *recent_rounds* rounds."""

    batch: int
    batches: int
    documents: int
    documents_total: int
    requests: int
    elapsed: float
    recent_requests: int
    recent_elapsed: float
    recent_rounds: int
    lookups_failed: int
    writes_failed: int
    last_tumbler: str = ""

    @staticmethod
    def _rate(n: int, seconds: float) -> float:
        return n / seconds if seconds > 0 else 0.0

    @property
    def rate_since_start(self) -> float:
        return self._rate(self.requests, self.elapsed)

    @property
    def rate_recent(self) -> float:
        return self._rate(self.recent_requests, self.recent_elapsed)

    def line(self) -> str:
        return (
            f"backfill-meta: batch {self.batch}/{self.batches}, {self.documents}/{self.documents_total} documents, "
            f"{self.requests} requests ({self.rate_since_start:.2f}/s since start, {self.rate_recent:.2f}/s over the last "
            f"{self.recent_rounds} round{'s' if self.recent_rounds != 1 else ''}), "
            f"{self.lookups_failed} lookups failed, {self.writes_failed} writes failed, "
            f"{self.elapsed:.1f}s, {self._rate(self.documents, self.elapsed):.1f} docs/s, at {self.last_tumbler or '-'}"
        )


class StopRequest:
    """The "stop asked" test :func:`stop_after_current_batch` yields: call it, and read :attr:`signum` for
    the signal that asked (0: none yet)."""

    def __init__(self) -> None:
        self._asked = threading.Event()
        self.signum = 0

    def __call__(self) -> bool:
        return self._asked.is_set()

    def ask(self, signum: int) -> None:
        if not self._asked.is_set():
            self.signum = signum
            self._asked.set()


@contextlib.contextmanager
def stop_after_current_batch() -> Iterator[StopRequest]:
    """While active, the first SIGINT (Ctrl-C) or SIGTERM only asks the run to stop after the batch it is in;
    a second one takes the previous disposition (for SIGINT a :class:`KeyboardInterrupt`, which
    :func:`backfill_meta` also survives, but mid-batch; for SIGTERM the default, which ends the process).
    Yields the "stop asked" test. Off the main thread no handler can be installed and the test never fires."""
    stop = StopRequest()
    if threading.current_thread() is not threading.main_thread():
        yield stop
        return
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    restore = {
        sig: (prev if prev is not None else (signal.default_int_handler if sig == signal.SIGINT else signal.SIG_DFL))
        for sig, prev in previous.items()
    }

    def _handler(signum: int, _frame: Any) -> None:
        stop.ask(signum)
        for sig, prior in restore.items():
            signal.signal(sig, prior)  # the second signal is the previous disposition's
        os.write(
            2, f"backfill-meta: {signal.Signals(signum).name} received, stopping after the current batch "
               f"(send it again to abort the batch at once).\n".encode(),
        )

    for sig in restore:
        signal.signal(sig, _handler)
    try:
        yield stop
    finally:
        for sig, prior in restore.items():
            signal.signal(sig, prior)


def backfill_meta(
    reader: Any, t3: Any, writer: Any | None, *, apply: bool, batch: int = _BATCH, pause_ms: int = 0,
    progress: Callable[[Progress], None] | None = None, should_stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] | None = None, clock: Callable[[], float] = time.monotonic,
    requests: Callable[[], int] | None = None, stop_after_failed_rounds: int = 0, start_after: str = "",
    list_first_sign: bool = False, list_only: bool = False, now: datetime | None = None,
) -> Report:
    """Examine every live document and stamp (``apply``) or count (dry run) what :func:`plan_note` and
    :func:`plan_indexed_rows` allow. Raises :class:`VacuousRunError` when the
    catalog lists no document.

    *batch* documents are examined per round; a batch reads each collection it needs once (nexus-ldf2s).
    *pause_ms* is slept after a batch's reads and again after its write. *progress* gets a report after
    about every ``batches / PROGRESS_LINES``-th batch and the last; its request figures come from *requests*
    (the request gate's count; default: the installed gate's). The run stops, with ``Report.interrupted``
    set, ``stop_reason`` saying why and only finished batches counted, when *should_stop* says so after a
    batch, a :class:`KeyboardInterrupt` ends one, or *stop_after_failed_rounds* (0: never) batches in a row
    each had a failed lookup or write; it is fill-only, so a rerun carries on. ``Report.last_tumbler`` is
    the last document of the last finished batch.

    *start_after* skips the listing up to and including that tumbler. The listing's order is the catalog's
    (``ORDER BY tumbler`` in the database's collation, so not a numeric order: ``1.10`` can sort before
    ``1.2``), so the skip is by position in that listing, never by comparing tumblers. A tumbler the listing
    does not hold raises :class:`ResumePointError`. The skipped documents still count as owners of their
    chunks.

    *list_first_sign* (a dry run only) also lists the keyed notes whose expiry RDR-236 Phase 4 Step 1 changes
    (:func:`_list_first_sign`), at *now* (default: the clock). It reads the manifests and chunk rows (text
    included) of every note with a catalog TTL, which the plain run does not, so it runs only when asked.
    *list_only* lists and makes no stamping plan (it implies *list_first_sign*)."""
    if (list_first_sign or list_only) and apply:
        raise ValueError("the first-sign listing is a dry run only: it cannot be combined with apply")
    list_first_sign = list_first_sign or list_only
    # The listing's clock, fixed once for the whole run: every batch judges lapsed_now against the same instant.
    # Its own name, so nothing else in this function (the progress block's clock readings) can rebind it.
    listing_now: datetime = now if now is not None else datetime.now(UTC)
    if batch < 1:
        raise ValueError(f"batch must be at least 1, got {batch}")
    if pause_ms < 0:
        raise ValueError(f"pause_ms must be at least 0, got {pause_ms}")
    if stop_after_failed_rounds < 0:
        raise ValueError(f"stop_after_failed_rounds must be at least 0, got {stop_after_failed_rounds}")
    count_requests = requests if requests is not None else request_gate.requests_so_far
    started = clock()       # before the listing: its pages are requests of the run
    base = count_requests()
    history: deque[tuple[float, int]] = deque([(started, base)], maxlen=RECENT_ROUNDS + 1)
    listing = list(reader.all_documents())
    repo_listing_failed = False
    try:
        repo_prefixes = repo_owner_prefixes(reader)
        repo_collections = repo_prose_collections(reader) if repo_prefixes else {}
    except Exception as exc:  # noqa: BLE001 — the listings only enable the repo-collection tags rule; its loss is reported, the run goes on
        _log.warning("repo_owner_listing_failed", error=repr(exc))
        repo_prefixes, repo_collections, repo_listing_failed = frozenset(), {}, True
    live_entries = [e for e in listing if not getattr(e, "alias_of", "")]
    if not live_entries:
        raise VacuousRunError("examined 0 documents: an empty catalog is a failed sweep, not a clean one")
    live = {str(e.tumbler): e for e in live_entries}    # every owner counts, the skipped ones too
    report = Report(
        applied=apply, started_after=start_after, last_tumbler=start_after, resume_after=start_after,
        list_only=list_only,
    )
    report.repo_owners = len(repo_prefixes)
    report.repo_listing_failed = repo_listing_failed
    if start_after:
        at = next((i for i, e in enumerate(listing) if str(e.tumbler) == start_after), None)
        if at is None:
            raise ResumePointError(f"--start-after {start_after}: the catalog lists no such document")
        listing = listing[at + 1:]
    entries = [e for e in listing if not getattr(e, "alias_of", "")]
    batches = math.ceil(len(entries) / batch)
    every = max(1, math.ceil(batches / PROGRESS_LINES))
    report.batches_total = batches
    naps = sleep or time.sleep
    pause: Callable[[], None] = (lambda: naps(pause_ms / 1000)) if pause_ms else (lambda: None)
    failed_streak = 0
    clean_so_far = True     # no batch of this run has had a failed lookup or write yet
    for index, start in enumerate(range(0, len(entries), batch), 1):
        chunk = entries[start:start + batch]
        before = copy.deepcopy(report)
        try:
            _run_batch(
                reader, t3, writer, chunk, live, apply, report, pause, pause_after_write=index < batches,
                list_first_sign=list_first_sign, list_only=list_only, now=listing_now,
                repo_prefixes=repo_prefixes, repo_collections=repo_collections,
            )
        except KeyboardInterrupt:
            report = before  # the interrupted batch counts for nothing; a rerun redoes it
            report.interrupted = True
            report.stop_reason = STOP_SIGNAL
            report.cut_batch = index    # its update_many may have gone through before the interrupt
            _log.warning("meta_backfill_interrupted", batch=index, batches=batches)
            break
        report.batches_done = index
        report.last_tumbler = str(chunk[-1].tumbler)
        # A listing read that failed counts as trouble too, so --stop-after-failed-rounds also stops a listing run
        # against a failing service instead of walking the whole catalog one failing request per batch.
        trouble = (
            (report.lookups_failed - before.lookups_failed) + (report.writes_failed - before.writes_failed)
            + (report.ttl_first_sign_unread - before.ttl_first_sign_unread)
        )
        failed_streak = failed_streak + 1 if trouble else 0
        clean_so_far = clean_so_far and not trouble
        if clean_so_far:
            report.resume_after = report.last_tumbler
        history.append((clock(), count_requests()))
        stop_reason = ""
        if index < batches:
            if stop_after_failed_rounds and failed_streak >= stop_after_failed_rounds:
                stop_reason = STOP_FAILED_ROUNDS
            elif should_stop is not None and should_stop():
                stop_reason = STOP_SIGNAL
        if progress is not None and (index % every == 0 or index == batches or stop_reason):
            at, total = history[-1]
            then, was = history[0]
            snapshot = Progress(
                index, batches, report.documents_examined, len(entries), total - base,
                at - started, total - was, at - then, len(history) - 1,
                report.lookups_failed, report.writes_failed, report.last_tumbler,
            )
            _log.info(
                "meta_backfill_progress", batch=index, batches=batches, documents=snapshot.documents,
                requests=snapshot.requests, lookups_failed=snapshot.lookups_failed,
                writes_failed=snapshot.writes_failed, elapsed=round(snapshot.elapsed, 1),
            )
            progress(snapshot)
        if stop_reason:
            report.interrupted = True
            report.stop_reason = stop_reason
            _log.warning("meta_backfill_stopped", batch=index, batches=batches, reason=stop_reason)
            break
    return report


def _record(report: Report, kind: str, plan: Plan) -> None:
    for key, status in plan.outcome.items():
        if key == "tags_legacy_string":
            report.tags_legacy_strings_left += 1
            continue
        report.stats(kind, key).add(status)


def _fetch_rows(t3: Any, wants: dict[str, list[str]]) -> tuple[dict[str, dict[str, dict]], dict[str, Exception]]:
    """One :func:`_chunk_rows` per collection in *wants* (its chashes, de-duplicated): ``(rows, errors)``,
    each keyed by collection. A collection whose read raised is in ``errors`` and not in ``rows``, so the
    documents that needed it can be withheld one by one."""
    rows: dict[str, dict[str, dict]] = {}
    errors: dict[str, Exception] = {}
    for coll, hashes in wants.items():
        unique = list(dict.fromkeys(hashes))
        try:
            rows[coll] = _chunk_rows(t3, coll, unique)
        except Exception as exc:  # noqa: BLE001 — the documents that needed this collection are withheld and counted
            errors[coll] = exc
            _log.warning("meta_backfill_collection_read_failed", collection=coll, chashes=len(unique), error=str(exc))
    return rows, errors


def _list_first_sign(reader: Any, t3: Any, notes: list[Any], report: Report, now: datetime | None = None) -> None:
    """Add to *report* the notes whose expiry RDR-236 Phase 4 Step 1 (nexus-6pfgb.65) changes: the keyed notes
    ``note_ttl.catalog_ttl_stale`` held by its first sign alone until Step 1, the row's TTL differing from the
    catalog's while the row's ``indexed_at`` is not later than ``put_at`` plus the skew (a pre-``.18`` import's
    permanent row, or a note whose owner changed its TTL with a new-client re-put: two shapes the rows cannot
    tell apart), or the row having no readable ``indexed_at`` (reason ``first sign, no indexed_at``). Step 1
    stops holding them, so Sam reads this list first. Each says whether it has already lapsed at *now* (default:
    the clock at the call) and when it expires: a lapsed note is what the first sweep after Step 1 ships would
    retire. A dry run only. Read-only: it writes nothing, stamps nothing and changes no expiry decision.

    Only a note with a readable ``put_at`` and a catalog TTL can be on the list (the rule's threshold is
    ``put_at`` plus the skew, and a catalog-permanent note never lapses); such a note is complete for
    :func:`plan_note`, so the batch has not read its rows. They are read here, by the note's manifest chashes as
    the sweep's guard does (one manifest read for the batch, one store-get per collection, chunk text included).
    A read that fails counts the note in ``ttl_first_sign_unread`` and never fails the run: its absence from the
    list is then unknown, not a finding."""
    report.ttl_first_sign_checked = True
    keyed = [e for e in notes if parse_iso(_meta(e).get("put_at")) is not None and note_ttl_days(_meta(e)) > 0]
    if not keyed:
        return
    now = now or datetime.now(UTC)
    try:
        manifests = reader.get_manifests([str(e.tumbler) for e in keyed]) or {}
    except Exception as exc:  # noqa: BLE001 — the list only informs; its loss is counted, the run goes on
        _log.warning("meta_backfill_ttl_listing_manifest_read_failed", documents=len(keyed), error=str(exc))
        report.ttl_first_sign_unread += len(keyed)
        return
    by_note: dict[str, dict[str, list[str]]] = {}
    wants: dict[str, list[str]] = {}
    for e in keyed:
        colls: dict[str, list[str]] = {}
        for m in manifests.get(str(e.tumbler), []):
            colls.setdefault(getattr(m, "collection", None) or e.physical_collection, []).append(m.chash)
        by_note[str(e.tumbler)] = colls
        for c, hs in colls.items():
            wants.setdefault(c, []).extend(hs)
    fetched, errors = _fetch_rows(t3, wants)
    for e in keyed:
        colls = by_note[str(e.tumbler)]
        if any(c in errors for c in colls):
            report.ttl_first_sign_unread += 1
            continue
        rows = [r for c, hs in colls.items() for h in hs if (r := fetched.get(c, {}).get(h)) is not None]
        held = held_by_first_sign_alone(
            _meta(e), rows, owner_tumbler=e.tumbler, owner_title=getattr(e, "title", "") or "",
        )
        if not held:
            continue
        meta = _meta(e)
        expiry = expires_at(meta, getattr(e, "indexed_at", "") or "")
        report.ttl_first_sign_notes.append({
            "tumbler": str(e.tumbler), "title": getattr(e, "title", "") or "", "collection": e.physical_collection,
            "catalog_ttl_days": meta.get("ttl_days"), "put_at": meta.get("put_at"),
            "row_ttl_days": held[0].get("ttl_days"), "row_indexed_at": held[0].get("indexed_at"),
            "rows": len(held),
            "reason": first_sign_reason(held),
            "expires_at": expiry.isoformat() if expiry is not None else None,
            "lapsed_now": expiry is not None and now >= expiry,
        })


def _run_batch(
    reader: Any, t3: Any, writer: Any | None, entries: list[Any], live: dict[str, Any], apply: bool, report: Report,
    pause: Callable[[], None], *, pause_after_write: bool = True, list_first_sign: bool = False,
    list_only: bool = False, now: datetime | None = None, repo_prefixes: frozenset[str] = frozenset(),
    repo_collections: Mapping[str, str] | None = None,
) -> None:
    notes = [e for e in entries if getattr(e, "content_type", "") == "knowledge"]
    docs = [e for e in entries if getattr(e, "content_type", "") != "knowledge"]
    report.documents_examined += len(entries)
    report.documents_notes += len(notes)
    if list_first_sign and not apply:
        _list_first_sign(reader, t3, notes, report, now)
    if list_only:
        pause()   # the batch's reads are done
        return    # no stamping plan: no owner lookup, no document row, no payload

    needs_rows_docs = [e for e in docs if doc_row_keys_wanted(e)]
    needs_rows_notes = [e for e in notes if put_at_is_open(_meta(e))]
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

    # Ownership of the indexed documents' chunks first (it needs only the manifests), so that every row this
    # batch reads, a note's and a document's, is known before the first store-get and each collection is
    # read once (nexus-ldf2s). The rows of one document were once read with a store-get per document.
    doc_owners: dict[str, set[str]] = {}
    docs_ok = True
    if needs_rows_docs:
        want = {m.chash for e in needs_rows_docs if str(e.tumbler) not in failed for m in manifests.get(str(e.tumbler), [])}
        try:
            doc_owners = _owners_of(reader, sorted(want))
        except Exception as exc:  # noqa: BLE001 — withhold the batch's row-sourced keys, count the documents
            docs_ok = False
            _log.warning("meta_backfill_owner_lookup_failed", chashes=len(want), error=str(exc))

    wants: dict[str, list[str]] = {}
    note_colls: dict[str, dict[str, list[str]]] = {}    # note -> collection -> its chashes
    for e in needs_rows_notes:
        tid = str(e.tumbler)
        if tid in failed:
            continue
        try:
            by_coll: dict[str, list[str]] = {}
            for m in manifests.get(tid, []):
                by_coll.setdefault(coll_of(e, m), []).append(m.chash)
        except Exception as exc:  # noqa: BLE001 — withhold this note's keys, count it, go on
            _log.warning("meta_backfill_note_rows_failed", tumbler=tid, error=str(exc))
            failed.add(tid)
            continue
        note_colls[tid] = by_coll
        for c, hs in by_coll.items():
            wants.setdefault(c, []).extend(hs)
    doc_colls: dict[str, dict[str, list[str]]] = {}     # document -> collection -> its sampled sole-owner chashes
    if docs_ok:
        for e in needs_rows_docs:
            tid = str(e.tumbler)
            if tid in failed:
                continue
            mrows = manifests.get(tid, [])
            single = [(coll_of(e, m), m.chash) for m in mrows if sole_in_collection(e, coll_of(e, m), doc_owners.get(m.chash, set()))]
            by_coll = {}
            for c, h in _spread(single, ROW_SAMPLE):
                by_coll.setdefault(c, []).append(h)
            doc_colls[tid] = by_coll
            for c, hs in by_coll.items():
                wants.setdefault(c, []).extend(hs)

    fetched, errors = _fetch_rows(t3, wants)

    # notes first: rows of every piece, then ownership only for a note no written row decides
    note_pieces: dict[str, list[tuple[str, dict | None]]] = {}
    for e in needs_rows_notes:
        tid = str(e.tumbler)
        if tid not in note_colls:
            continue
        bad = next((c for c in note_colls[tid] if c in errors), None)
        if bad is not None:
            _log.warning("meta_backfill_note_rows_failed", tumbler=tid, error=str(errors[bad]))
            failed.add(tid)
            continue
        note_pieces[tid] = [(m.chash, fetched.get(coll_of(e, m), {}).get(m.chash)) for m in manifests.get(tid, [])]
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
        if not put_at_is_open(_meta(e)):
            plans[tid] = ("note", plan_note(e, [], ()))
        elif tid in note_pieces:
            need = note_needs_ownership(e, note_pieces[tid])
            if need and not owners_ok:
                failed.add(tid)
                continue
            sole = [c for c in need if sole_in_collection(e, e.physical_collection, owners.get(c, set()))]
            plans[tid] = ("note", plan_note(e, note_pieces[tid], sole))

    # indexed documents: only the sole-owner rows, already read
    if needs_rows_docs:
        for e in needs_rows_docs:
            tid = str(e.tumbler)
            if tid in failed:
                continue
            if not docs_ok:
                failed.add(tid)
                continue
            by_coll = doc_colls[tid]
            bad = next((c for c in by_coll if c in errors), None)
            if bad is not None:
                _log.warning("meta_backfill_rows_failed", tumbler=tid, error=str(errors[bad]))
                failed.add(tid)
                continue
            rows: list[dict] = []
            for c, hs in by_coll.items():
                got = fetched.get(c, {})
                rows.extend(got[h] for h in hs if h in got)
            plans[tid] = ("doc_rows", plan_indexed_rows(e, rows, n_chunks=len(manifests.get(tid, []))))
    report.lookups_failed += len(failed)
    fetched.clear()
    if want_manifest:
        pause()  # the batch's reads are done

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
            parts.append(plan_tags_category(e, repo_prefixes, repo_collections))
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
        if pause_after_write:
            pause()
