# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Recovery bundle: export/import for the catalog link graph and
store_put-origin knowledge content across a reinstall (nexus-xn3fr,
GH #1419 Issue 9).

Design of record: T2 ``nexus/design-xn3fr-recovery-bundle.md`` (Sam-approved
2026-08-31). The two things a reinstall cannot regenerate are the catalog
LINK GRAPH (cross-collection, tumbler-addressed — and tumblers are NOT
stable across reindex) and store_put-origin knowledge content (whose ONLY
copy is its T3 chunks — nexus-b6enc; one per piece of a note ``put_note``
split, RDR-223). This module carries both in one
human-inspectable JSONL bundle:

- line 1: JSON header (``format_version``, counts, source fingerprint);
- then one JSON object per line: ``collection`` records (format 3: one registry row per collection that
  has a note), ``knowledge_doc`` records, then ``link`` records (import is sequential, and links resolve
  against docs the same bundle just imported).

IDENTITY is ``source_uri`` throughout — never tumblers. store_put-origin
docs carry the nexus-sdp0u synthesized ``uri_for(collection, title)``
identity (or none at all for pre-sdp0u legacy rows); ``knowledge_doc``
records therefore ALSO carry ``(collection, title)`` so import re-derives
identity under the TARGET install's collection set. NO EMBEDDINGS are
carried (locked decision): import writes each note through the note writer
(``note_write.put_note``, the writer MCP ``store_put`` uses; RDR-223 P2.8), one
owner-carrying request per note, which re-embeds — model-portable by construction.

The store-put-origin classifier below is a LOCAL reimplementation of the
shape in ``commands/catalog_cmds/reconcile_stale.py`` (and its sibling in
``catalog_cmds/integrity.py``) — this layer must not reach up into
``commands/`` (see ``store_hook.py``'s layering comment), the same
three-implementations-one-contract pattern as the ``file://`` convention.
It DIVERGES deliberately on two audited points (nx_plan_audit rounds 1-2,
recorded on nexus-xn3fr): the note-URI leg carries a
collection-prefix guard (a stray ``chroma://`` URI under a file-routed
prefix is migration residue, never store_put-origin — importing it would
recreate a duplicate document beside what ``nx index repo`` rebuilds,
the nexus-53cae pathology), and the empty-``source_uri`` leg NEVER trusts
the cached ``chunk_count`` column — it ground-truths against the manifest,
because the cache bound exists in ``reconcile_stale.py`` only for an
upstream routing this module does not have, and trusting it here silently
drops exactly the legacy live-chunk rows (the nexus-1uekf population) this
bead exists to carry.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from functools import partial
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import structlog

from nexus.aspect_readers import (
    FILE_ROUTED_PREFIXES,
    NOTE_URI_SCHEME,
    canonical_note_uri,
    is_note_uri,
    uri_for,
)
from nexus.catalog.dchash import check_link_span
from nexus.catalog.types import CatalogEntry
from nexus.errors import NoteUriEngineTooOldError, RecoveryBundleFormatTooNewError
from nexus.ttl import MAX_TTL_DAYS

_log = structlog.get_logger(__name__)

#: Bundle format version written by this implementation; mirrors
#: ``exporter.py``'s FORMAT_VERSION / MAX_SUPPORTED_FORMAT_VERSION pair. Version 2 (nexus-6pfgb.38):
#: a note record's ``tags`` is a list, where version 1 wrote the comma string. The importer accepts
#: both (a string is split once), so a version 1 bundle still reads; an older client reading a
#: version 2 bundle refuses it by this number. Version 3 (RDR-229 P3.9, nexus-p2m1x.29): the bundle carries each
#: collection's registry row (a ``collection`` record) and every note carries its source ``collection_id``;
#: import registers from the row, never from the name, and treats the id as a bundle-local key. A bundle of
#: version 2 or below still imports through the name path (:func:`target_collection_for`).
RECOVERY_BUNDLE_FORMAT_VERSION: int = 3
MAX_SUPPORTED_RECOVERY_FORMAT_VERSION: int = 3

#: Page size for the link_query exhaustion loop (the ``bulk_unlink``
#: dry_run idiom — http_catalog_client.py's own pagination shape).
_LINK_PAGE: int = 200


def classify_store_put_origin(entry: CatalogEntry) -> str | None:
    """Return the store-put-origin leg (``"note_uri"`` /
    ``"knowledge_no_path"``) for *entry*, or ``None`` when it is not
    store_put-origin. See the module docstring for the two audited
    divergences from the ``commands/``-layer classifiers."""
    if entry.file_path:
        return None  # file-backed always wins
    if entry.physical_collection.startswith(FILE_ROUTED_PREFIXES):
        # uri_for's own docstring is the single source of truth: nx-note://
        # identity is minted for everything OUTSIDE the file-routed
        # prefixes; a chroma:// URI under one of them is residue.
        return None
    source_uri = entry.source_uri or ""
    # RDR-231: a note is nx-note://; a row still carrying the legacy chroma://
    # spelling is the same note until the engine rewrites it (a note "in
    # either spelling" is is_note_uri(canonical_note_uri(u))).
    if is_note_uri(canonical_note_uri(source_uri)):
        return "note_uri"
    if not source_uri:
        return "knowledge_no_path"
    return None  # non-empty, non-note URI without a file_path: not ours


def link_identity_uri(entry: CatalogEntry) -> str:
    """The source_uri a LINK record should carry for *entry*.

    For a normal entry this is its recorded ``source_uri``. For a
    pre-sdp0u legacy knowledge doc with NO source_uri, export the
    identity the doc WILL carry after import — the store_put chain mints
    ``uri_for(collection, title)`` at registration — so the link's
    endpoint join key actually resolves on the target. (Plan-gap fix,
    recorded on nexus-xn3fr: exporting the empty string here would make
    every such link unresolvable by construction.)
    """
    if entry.source_uri:
        return entry.source_uri
    if entry.title and not entry.physical_collection.startswith(FILE_ROUTED_PREFIXES):
        return uri_for(entry.physical_collection, entry.title)
    return ""


@dataclass(slots=True)
class ExportSummary:
    docs_exported: int = 0
    links_exported: int = 0
    ghosts_skipped: int = 0
    file_routed_excluded: int = 0
    #: Notes written as several pieces (RDR-223) of which some, not all, are gone from T3: the
    #: record is left out rather than carrying a truncated text, and ``notes_incomplete_notes``
    #: names each as ``(collection, title)``. A note with every piece gone is a ghost.
    notes_incomplete: int = 0
    notes_incomplete_notes: list[tuple[str, str]] = field(default_factory=list)
    unresolvable_link_endpoints: int = 0
    #: Notes whose catalog entry holds none of its own note keys (written before nexus-6pfgb.5, or
    #: stored with none of them before nexus-6pfgb.15) while a chunk row carries a tags, category or
    #: ttl_days value: the record carries none, because the export never reads a note's keys off a row
    #: (nexus-6pfgb.65).
    note_keys_unrecovered: int = 0
    #: ``(collection, title)`` of each of those notes, in export order, so the command can name them
    #: (a title is unique within a collection only).
    note_keys_unrecovered_notes: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class ImportSummary:
    """Per-note outcomes of an import. Each note lands in exactly one of four counts:

    * ``docs_imported``: written and verified complete;
    * ``docs_failed``: definitively not written (``doc_failures`` names each and why);
    * ``docs_uncertain``: may have landed, the run cannot say (``doc_unverified``);
    * ``docs_stamp_refused``: written, but the engine refused to stamp it complete, so the document
      stays ``indexing`` (``doc_unverified``).

    Only ``docs_imported`` is verified; ``docs_unverified`` is the other three together, and the
    ``nx catalog import`` exit status is non-zero when it is not zero.

    ``links_refused_span`` lists each link record not written because its ``from_span`` or ``to_span``
    starts with ``dchash:`` and does not parse (RDR-171): ``{from_uri, to_uri, link_type, span, error}``.
    It does not change the exit status for a link.

    Format 3 (RDR-229 P3.9) adds three lists, each a list of dicts naming the collection by ``label``:

    * ``collections_refused_label_clash``: the collection was not imported and ``error`` says why (the label is
      live on this install with another content_type or owner_id, or the engine refused to register it, e.g. its
      label rule); ``docs_skipped`` counts its notes. ``nx catalog import`` exits non-zero when any is present,
      as for a note that did not land.
    * ``collections_skipped_not_live``: the row is not ``live`` (``lifecycle_state``); never registered, its
      ``docs_skipped`` notes left out. Expected, so the exit status is unchanged.
    * ``collections_changed_model``: registered (or mapped) under another embedding model than the bundle's row
      names (``from_model``, ``to_model``); the notes are re-embedded from text;
    * ``collections_relabelled``: a label of the minted form (``c<digits>``) is the SOURCE install's own, so it is not
      kept: the notes went to the label this install minted for the same attributes (``label``, ``target_label``).
    """

    docs_imported: int = 0
    docs_failed: int = 0
    docs_uncertain: int = 0
    docs_stamp_refused: int = 0
    links_created: int = 0
    links_merged: int = 0
    links_missing_span: int = 0
    unresolvable_links: list[dict] = field(default_factory=list)
    links_refused_span: list[dict] = field(default_factory=list)
    collections_refused_label_clash: list[dict] = field(default_factory=list)
    collections_skipped_not_live: list[dict] = field(default_factory=list)
    collections_changed_model: list[dict] = field(default_factory=list)
    collections_relabelled: list[dict] = field(default_factory=list)
    doc_failures: list[dict] = field(default_factory=list)
    doc_unverified: list[dict] = field(default_factory=list)

    @property
    def docs_unverified(self) -> int:
        """Notes that did not verify: failed, uncertain, or written but not stamped complete."""
        return self.docs_failed + self.docs_uncertain + self.docs_stamp_refused


def enumerate_all_documents(reader: Any) -> list[CatalogEntry]:
    """One full catalog walk (``all_documents`` paginates to exhaustion
    internally). Shared by the store-put enumeration and the link
    tumbler→source_uri map so the export pays exactly one walk."""
    return list(reader.all_documents())


def select_store_put_documents(
    entries: Iterable[CatalogEntry],
    manifests: dict[str, list],
    summary: ExportSummary,
) -> list[tuple[CatalogEntry, str, list]]:
    """Apply the classifier + manifest ground-truthing.

    Returns ``(entry, leg, manifest_rows)`` triples for exportable docs.
    ``manifests`` is the BATCH ``get_manifests`` result — a doc_id absent
    from it is identical to present-with-zero-rows (a true ghost for the
    ``knowledge_no_path`` leg), never "not yet checked". A note may have
    several rows: ``put_note`` writes a long note as pieces (RDR-223), and
    :func:`export_knowledge_doc` joins them (nexus-6pfgb.36).
    """
    out: list[tuple[CatalogEntry, str, list]] = []
    for entry in entries:
        leg = classify_store_put_origin(entry)
        if leg is None:
            if (
                not entry.file_path
                and (entry.source_uri or "").startswith("chroma://")
                and entry.physical_collection.startswith(FILE_ROUTED_PREFIXES)
            ):
                summary.file_routed_excluded += 1
            continue
        rows = manifests.get(str(entry.tumbler), [])
        if len(rows) == 0:
            # True ghost (row without content) — counted, never silent.
            summary.ghosts_skipped += 1
            _log.info(
                "recovery_export_ghost_skipped",
                tumbler=str(entry.tumbler),
                title=entry.title,
                leg=leg,
            )
            continue
        out.append((entry, leg, rows))
    return out


def _positive_ttl(value: Any) -> int | None:
    """A ``ttl_days`` as the note writer takes it: a positive integer up to the 36500-day cap, else
    ``None`` (permanent).

    Catalog meta stores ``0`` as the retraction sentinel and ``put_note`` raises on it, and the chunk
    row spells permanent as ``None``, absent or (a legacy row) ``0``: all of them read as permanent.
    A ``bool`` is not a number of days, and neither is a float or a string. A value above
    ``timedelta.max.days`` is permanent, the reading every reader of a row makes of it
    (:func:`nexus.metadata_schema.chunk_ttl_days`, :func:`nexus.catalog.note_ttl.note_ttl_days`), so a
    row that never lapses does not come back as one that lapses in a century. A value from
    :data:`nexus.ttl.MAX_TTL_DAYS` + 1 up to that range is clamped to the cap, with a warning naming
    the original: ``put_note`` refuses a larger TTL, and ``nx store put --ttl`` never wrote one
    (nexus-njn1a).
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    if value > timedelta.max.days:
        return None
    if value > MAX_TTL_DAYS:
        _log.warning("recovery_bundle_ttl_clamped", ttl_days=value, clamped_to=MAX_TTL_DAYS)
        return MAX_TTL_DAYS
    return value


def _own_note_keys(entry: CatalogEntry) -> dict[str, Any] | None:
    """The note's own ``tags``, ``category`` and ``ttl_days`` from its catalog entry's ``meta``
    (nexus-6pfgb.5), or ``None`` when the entry holds none of the note keys.

    Every put since nexus-6pfgb.5 writes each key it gives a real value and retracts one the entry
    held, so an entry with at least one note key has an authoritative answer for the rest: an absent
    key is unset, never "ask the chunk row". ``put_at`` counts: every put since nexus-6pfgb.15
    writes it, so a note stored with every other key unset still has an answer (all empty, and not
    counted as unrecovered). An entry with none is a legacy one, or a note stored with every key
    unset before nexus-6pfgb.15. :func:`export_knowledge_doc` exports such a note with all three empty
    and no longer asks its chunk row (RDR-236 Phase 4 Step 1, nexus-6pfgb.65): the operator-run backfill
    (``nx catalog backfill-meta``; the upgrade does not run it) fills a keyless note's keys from its rows
    where it can attribute them, and the export notice names it.
    """
    from nexus.catalog.store_hook import NOTE_META_KEYS, split_note_tags  # noqa: PLC0415 — deferred, store_hook has a heavy import graph

    meta = entry.meta or {}
    if not any(key in meta for key in NOTE_META_KEYS):
        return None
    return {
        "tags": split_note_tags(meta.get("tags")),
        "category": str(meta.get("category") or ""),
        "ttl_days": _positive_ttl(meta.get("ttl_days")),
    }


def _own_entity_key(entry: CatalogEntry) -> str:
    """The entity key a put stamped into the entry's ``meta`` (``""`` when none): an explicit
    key, or the title a configured pattern matched in full (nexus-fai28)."""
    value = (entry.meta or {}).get("entity_key")
    return value.strip() if isinstance(value, str) else ""


def export_knowledge_doc(
    entry: CatalogEntry, manifest_rows: list, rows_by_chash: dict[str, dict], summary: ExportSummary,
) -> dict | None:
    """Build one ``knowledge_doc`` record from the note's manifest rows and the chunk rows
    :func:`export_bundle` read for its collection (*rows_by_chash*); ``None`` when the note cannot be
    exported whole, counted on *summary*, never raised.

    The content is every piece of the note joined in manifest position order (nexus-6pfgb.36):
    ``put_note`` writes a long note as several pieces (RDR-223) and ``"".join(pieces) == content``
    by construction (:func:`nexus.catalog.store_hook.note_pieces`). A note with every piece gone from
    T3 is a ghost, and so is every note of a collection the engine has no catalog row for. A note with
    some pieces gone, or whose manifest positions do not run ``0..n-1`` (a lost leading or middle row),
    is incomplete and left out, because importing a shortened text would overwrite the whole note
    wherever it still exists. A lost FINAL manifest row leaves positions that still run ``0..n-2`` and
    is not detected: the manifest carries no piece count, and the span columns are not trusted for it.

    ``tags``, ``category`` and ``ttl_days`` are the NOTE'S OWN, read from its catalog entry's ``meta``
    (nexus-6pfgb.5): two notes with identical text share one chunk row, and the row carries only the
    last writer's. An entry with none of the note keys (written before nexus-6pfgb.5, or stored with
    none of them before nexus-6pfgb.15 made every put write ``put_at``) exports all three empty
    (``ttl_days`` permanent): the export has no row path (RDR-236 Phase 4 Step 1, nexus-6pfgb.65).
    The backfill (nexus-6pfgb.10, operator-run: ``nx catalog backfill-meta``, which the upgrade does not run)
    fills a keyless note whose row it can attribute; a note it has not been run on exports empty. A row is
    never a source here: a TTL a departed co-owner left
    on one would make a permanent note expire after import, and a client no longer writes a note's own
    facts to its rows at all. *summary* counts the note, and names it, when a piece row still holds a
    tags, category or ttl_days value the record does not carry. ``ttl_days`` is a positive integer or
    ``None``.
    """
    ordered = sorted(manifest_rows, key=lambda r: r.position)
    pieces = [rows_by_chash.get(mrow.chash) for mrow in ordered]
    missing = [mrow.chash for mrow, piece in zip(ordered, pieces) if piece is None or not piece.get("content")]
    gapped = [mrow.position for mrow in ordered] != list(range(len(ordered)))
    if missing or gapped:
        _log.warning(
            "recovery_export_note_not_whole",
            tumbler=str(entry.tumbler),
            title=entry.title,
            missing_chashes=missing,
            positions=[mrow.position for mrow in ordered],
        )
        if len(missing) == len(ordered):
            summary.ghosts_skipped += 1
        else:
            summary.notes_incomplete += 1
            summary.notes_incomplete_notes.append((entry.physical_collection, entry.title or ""))
        return None
    rows = [p for p in pieces if p is not None]
    content = "".join(str(p["content"]) for p in rows)
    keys = _own_note_keys(entry)
    if keys is None:
        # The row is never a source (RDR-236 Phase 4 Step 1, nexus-6pfgb.65): `nx catalog backfill-meta` is the
        # operator's way to fill a keyless note first, and the upgrade does not run it. A row's values are only
        # counted, so the operator is told what the record does not carry.
        keys = {"tags": [], "category": "", "ttl_days": None}
        if any(k["tags"] or k["category"] or k["ttl_days"] for k in map(_row_keys, rows)):
            summary.note_keys_unrecovered += 1
            summary.note_keys_unrecovered_notes.append((entry.physical_collection, entry.title or ""))
            _log.info(
                "recovery_export_note_keys_unrecovered",
                tumbler=str(entry.tumbler),
                title=entry.title,
            )
    return {
        "record": "knowledge_doc",
        "source_uri": entry.source_uri or "",
        "collection": entry.physical_collection,
        "title": entry.title,
        "tags": keys["tags"],
        "category": keys["category"],
        "ttl_days": keys["ttl_days"],
        # RDR-147 Step 1 (nexus-fai28): the entity key lives in the catalog entry's meta only and is
        # not a NOTE_META_KEYS member, so it is read here on its own. Without it an import re-puts
        # the note with no key, and that put retracts the one the source held.
        "entity_key": _own_entity_key(entry),
        # RDR-243 (nexus-rfvyy.6.12): the document's flag, from the catalog entry and never from a chunk
        # row. An older bundle has no key and imports as not sensitive, leaving an existing true in place.
        "sensitive": getattr(entry, "sensitive", False) is True,
        "content": content,
    }


def _collection_rows(t3: Any, collection: str, chashes: list[str]) -> dict[str, dict]:
    """``t3.chunk_rows`` for one collection, where a collection the engine has no catalog row for
    holds no chunks: its notes count as ghosts, as they did when each piece was read alone. Any other
    store error propagates and stops the export."""
    from nexus.db.http_vector_client import (  # noqa: PLC0415 — deferred, heavy import graph
        VectorServiceError,
        is_unregistered_collection_error,
    )

    try:
        return t3.chunk_rows(collection, chashes, sensitivity="include")
    except VectorServiceError as exc:
        if not is_unregistered_collection_error(exc):
            raise
        _log.warning("recovery_export_collection_unregistered", collection=collection, pieces=len(chashes))
        return {}


def _row_keys(row: dict) -> dict[str, Any]:
    """A chunk row's ``tags``, ``category`` and ``ttl_days`` in the record's shape: the row keeps a
    comma string, the record carries the array (nexus-6pfgb.38). Read only to count what a keyless
    note's record does not carry (:func:`export_knowledge_doc`), never to fill it."""
    from nexus.catalog.store_hook import split_note_tags  # noqa: PLC0415 — deferred, store_hook has a heavy import graph

    return {
        "tags": split_note_tags(row.get("tags")),
        "category": row.get("category", "") or "",
        "ttl_days": _positive_ttl(row.get("ttl_days")),
    }


def _registry_rows(reader: Any) -> dict[str, dict]:
    """The registry rows by label, every lifecycle state, from ``list_collections`` (the single-row read
    carries no ``lifecycle_state``). A catalog without the route (404) has none."""
    import httpx  # noqa: PLC0415 — deferred, as the module's other lazy imports

    try:
        return {r["name"]: r for r in reader.list_collections()}
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return {}
        raise


def export_collection_rows(reader: Any, docs: list[dict]) -> list[dict]:
    """One ``collection`` record per collection that holds an exported note (format 3, RDR-229 P3.9), and
    stamp each note record with its collection's ``collection_id``.

    The record is the collection's registry row as the catalog lists it (``collection_id``, label,
    ``content_type``, ``owner_id``, ``embedding_model``, ``model_version``, ``lifecycle_state``), never a
    parse of the label. A collection with no usable row (an engine below P3.1, a note left under an
    unregistered name, or a row with an empty content_type or owner_id, which import could not register)
    gets no record and its notes carry ``collection_id: null``; import restores those through the name
    path, as it does a bundle of format 2."""
    registry = _registry_rows(reader) if docs else {}
    records: list[dict] = []
    ids: dict[str, Any] = {}
    for rec in docs:
        label = rec["collection"]
        if label not in ids:
            row = registry.get(label)
            if not row or row.get("collection_id") is None or not row.get("content_type") or not row.get("owner_id"):
                _log.warning("recovery_export_collection_row_missing", collection=label)
                ids[label] = None
            else:
                ids[label] = row["collection_id"]
                records.append({
                    "record": "collection",
                    "collection_id": row["collection_id"],
                    "label": label,
                    "content_type": row["content_type"],
                    "owner_id": row["owner_id"],
                    "embedding_model": row.get("embedding_model", ""),
                    "model_version": row.get("model_version", ""),
                    "lifecycle_state": row.get("lifecycle_state") or "live",
                })
        rec["collection_id"] = ids[label]
    return records


def export_links(
    reader: Any,
    tumbler_to_uri: dict[str, str],
    summary: ExportSummary,
) -> list[dict]:
    """Enumerate ALL catalog links (``link_query`` driven to exhaustion —
    the ``bulk_unlink`` dry_run idiom) and denormalize each to source_uri
    endpoints. A tumbler missing from the map falls back to one
    ``reader.resolve`` round trip; a still-unresolvable endpoint is
    recorded on the summary, never raised."""
    records: list[dict] = []
    cur = 0
    pages = 0
    while True:
        batch = reader.link_query(limit=_LINK_PAGE, offset=cur)
        pages += 1
        for link in batch:
            rec = _link_record(reader, link, tumbler_to_uri, summary)
            if rec is not None:
                records.append(rec)
        if len(batch) < _LINK_PAGE:
            break
        cur += _LINK_PAGE
    if pages == 0:
        raise AssertionError("link_query exhaustion loop made zero requests")
    return records


def _endpoint_uri(
    reader: Any, tumbler: str, tumbler_to_uri: dict[str, str], summary: ExportSummary,
) -> str | None:
    uri = tumbler_to_uri.get(tumbler)
    if uri:
        return uri
    entry = reader.resolve(tumbler)
    if entry is not None:
        derived = link_identity_uri(entry)
        if derived:
            return derived
    summary.unresolvable_link_endpoints += 1
    _log.warning("recovery_export_link_endpoint_unresolvable", tumbler=tumbler)
    return None


def _link_record(
    reader: Any, link: Any, tumbler_to_uri: dict[str, str], summary: ExportSummary,
) -> dict | None:
    from_uri = _endpoint_uri(reader, str(link.from_tumbler), tumbler_to_uri, summary)
    to_uri = _endpoint_uri(reader, str(link.to_tumbler), tumbler_to_uri, summary)
    if from_uri is None or to_uri is None:
        return None
    return {
        "record": "link",
        "from_source_uri": from_uri,
        "to_source_uri": to_uri,
        "link_type": link.link_type,
        "from_span": link.from_span or "",
        "to_span": link.to_span or "",
        "created_by": link.created_by or "",
    }


def write_bundle(
    path: Path,
    knowledge_docs: list[dict],
    links: list[dict],
    summary: ExportSummary,
    *,
    collections: list[dict] | None = None,
    exported_by: dict[str, str] | None = None,
) -> None:
    """JSONL writer: one JSON header line, then collection records (format 3),
    knowledge_doc records, then link records. Plain JSON per line — human-inspectable
    is a locked format decision. ``exported_by`` (RDR-229 P3.9 G2) names the client and engine that wrote the
    bundle; readers ignore it."""
    collections = collections or []
    header = {
        "format": "nexus-recovery-bundle",
        "format_version": RECOVERY_BUNDLE_FORMAT_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # a caller that passes none still records the client; only the engine is unknown here
        "exported_by": exported_by if exported_by is not None else _exported_by(None),
        "collections": len(collections),
        "knowledge_docs": len(knowledge_docs),
        "links": len(links),
        "ghosts_skipped": summary.ghosts_skipped,
        "notes_incomplete": summary.notes_incomplete,
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for rec in collections:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for rec in knowledge_docs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for rec in links:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def export_bundle(reader: Any, t3: Any, path: Path) -> ExportSummary:
    """Full export: one catalog walk, batch manifest fetch, link
    denormalization, JSONL write. Read-only against the store.

    *t3* must offer ``chunk_rows(collection, chashes, *, sensitivity)``
    (:meth:`~nexus.db.http_vector_client.HttpVectorClient.chunk_rows`), which raises when the store
    cannot be read; the error propagates and no bundle is written, since the file is written last."""
    summary = ExportSummary()
    entries = enumerate_all_documents(reader)

    tumbler_to_uri: dict[str, str] = {}
    candidates: list[CatalogEntry] = []
    for entry in entries:
        uri = link_identity_uri(entry)
        if uri:
            tumbler_to_uri[str(entry.tumbler)] = uri
        if classify_store_put_origin(entry) is not None:
            candidates.append(entry)

    manifests = reader.get_manifests([str(e.tumbler) for e in candidates]) if candidates else {}
    docs: list[dict] = []
    selected = select_store_put_documents(entries, manifests, summary)
    # One paged read per collection for every piece of its notes (nexus-6pfgb.36). It raises when the
    # store cannot be read, so a piece the export could not read is never reported as a piece gone.
    chashes_by_collection: dict[str, list[str]] = {}
    for entry, _leg, rows in selected:
        chashes_by_collection.setdefault(entry.physical_collection, []).extend(r.chash for r in rows)
    rows_by_collection = {
        collection: _collection_rows(t3, collection, list(dict.fromkeys(chashes)))
        for collection, chashes in chashes_by_collection.items()
    }
    for entry, _leg, rows in selected:
        rec = export_knowledge_doc(entry, rows, rows_by_collection[entry.physical_collection], summary)
        if rec is not None:
            docs.append(rec)

    collections = export_collection_rows(reader, docs)
    links = export_links(reader, tumbler_to_uri, summary)
    summary.docs_exported = len(docs)
    summary.links_exported = len(links)
    write_bundle(path, docs, links, summary, collections=collections, exported_by=exported_by(reader))
    _log.info(
        "recovery_bundle_exported",
        path=str(path),
        docs=summary.docs_exported,
        links=summary.links_exported,
        ghosts_skipped=summary.ghosts_skipped,
        note_keys_unrecovered=summary.note_keys_unrecovered,
        notes_incomplete=summary.notes_incomplete,
    )
    return summary


def exported_by(reader: Any) -> dict[str, str]:
    """RDR-229 P3.9 G2 (linda's ruling 2026-11-22): what wrote a bundle. ``client`` is the installed conexus version;
    ``engine`` is the serving engine's ``release_version`` when the reader can tell it (``HttpCatalogClient`` reads it
    from its cached ``GET /version``), else ``""``: a local catalog, a tree-built engine, or a read that failed. The
    export never fails on it."""
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415 — cold-start cheap, only on export

    try:
        client = version("conexus")
    except PackageNotFoundError:
        client = ""
    engine = ""
    read = getattr(reader, "engine_release_version", None)
    if callable(read):
        try:
            value = read()
            engine = value if isinstance(value, str) else ""
        except Exception:  # noqa: BLE001 — provenance only; an unreachable engine must not fail the export
            engine = ""
    return {"client": client, "engine": engine}


#: write_bundle's ``exported_by`` parameter shadows the function's name inside it.
_exported_by = exported_by


# ── Import ──────────────────────────────────────────────────────────────────


def read_bundle(path: Path) -> tuple[dict, list[dict]]:
    """Parse a bundle; returns (header, records). Fails loud on a future
    format_version (mirrors ``exporter.py``'s MAX_SUPPORTED contract)."""
    with path.open("r", encoding="utf-8") as f:
        header = _checked_header(path, f.readline())
        records = [json.loads(line) for line in f if line.strip()]
    return header, records


def read_bundle_header(path: Path) -> dict:
    """The header alone, checked as :func:`read_bundle` checks it; the CLI calls this before it connects to anything."""
    with path.open("r", encoding="utf-8") as f:
        return _checked_header(path, f.readline())


def _checked_header(path: Path, header_line: str) -> dict:
    header = json.loads(header_line)
    if header.get("format") != "nexus-recovery-bundle":
        raise ValueError(f"{path} is not a nexus recovery bundle (header: {header})")
    version = int(header.get("format_version", 0))
    if version > MAX_SUPPORTED_RECOVERY_FORMAT_VERSION:
        raise RecoveryBundleFormatTooNewError(path, version, MAX_SUPPORTED_RECOVERY_FORMAT_VERSION)
    return header


def target_collection_for(recorded: str, t3: Any) -> str:
    """Resolve the RECORDED (source-install) collection name to the TARGET
    install's collection (review-fold, the critique's ship-blocker): a
    conformant 4-part name embeds the SOURCE's embedding model, and
    ``t3_collection_name`` passes conformant names through verbatim — so
    importing the recorded name onto a mode-changed install either raises
    ``IncompatibleCollectionError`` or fragments the corpus into a dead
    second collection. Reduce to the mode-independent ``type__owner`` base
    first (the RDR-103 shape's first two segments); the resolver then
    grandfathers an existing legacy collection or promotes to the TARGET's
    active model. A non-conformant recorded name passes through to the
    resolver unchanged, exactly as an operator-typed --collection would."""
    from nexus.corpus import t3_collection_name  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.catalog.rows.legacy_names import (  # noqa: PLC0415 — deferred to avoid circular import
        is_conformant_collection_name,
        parse_conformant_collection_name,
    )

    base = recorded
    if is_conformant_collection_name(recorded):
        # RDR-204 Phase 3 repoint (nexus-ft04v.26): `recorded` names a
        # collection on the SOURCE install a recovery bundle is being
        # restored from -- it may have no catalog row on THIS install at
        # all yet (restoring it is what creates one), so this reads the
        # STRING'S OWN parsed segments (parse_conformant_collection_name,
        # safe here because `recorded` is ALREADY confirmed conformant by
        # the `if` above), never the row-based collection_content_type/
        # collection_owner, which would raise CollectionNotRegisteredError
        # for a name this install has never seen.
        parsed = parse_conformant_collection_name(recorded)
        base = f"{parsed['content_type']}__{parsed['owner_id']}"
    # nexus-0fw11: the recorded collection already exists on the source
    # install, placeholder-named or not (docs__default and
    # knowledge__knowledge are live on the production tenant); restoring it
    # is not a new placeholder mint, so the refusal is lifted here alone.
    return t3_collection_name(base, t3=t3, for_write=True, allow_placeholder=True)


class ImportDocUncertain(RuntimeError):
    """A note's write may have landed, and this run cannot say so either way.

    The request died in flight, or the manifest read that would settle it failed, or the engine
    accepted the write and refused to stamp the document complete (``stamp_refused``; the document
    stays ``indexing`` and nothing is rolled back). The note is NOT verified: the import counts it
    apart from a definitive failure, and a retry is an idempotent re-write either way.
    """

    def __init__(self, message: str, *, stamp_refused: bool = False) -> None:
        super().__init__(message)
        self.stamp_refused = stamp_refused


def _default_import_doc(t3: Any, rec: dict, *, ack_external_embedder: bool = False) -> None:
    """Write one note through the note writer (RDR-223 P2.8, nexus-z0o2p.18).

    The note's pieces and its owner rows go to the engine as ONE ``write_manifest_many`` request
    (:func:`nexus.catalog.note_write.put_note`, the writer MCP ``store_put`` uses), so a chunk of
    the note never lands without its owner and a failed request leaves the previous manifest as it
    was. ``put_note`` also registers the catalog document (the sdp0u identity), begins the index-run
    fence and settles a failure (fail the fence, remove the row this call minted); this function
    words the outcome as the import's three results, from the same message table every note producer
    reads (:func:`~nexus.catalog.note_write.failure_message`):

    * landed and verified: returns, after the post-store hook chains
      (:func:`~nexus.catalog.note_write.fire_note_chains`, the one firing every producer shares) and
      the completion stamp (:func:`~nexus.catalog.note_write.stamp_note`, last: a kill in a chain
      leaves the fence ``indexing`` and the next import redoes the note);
    * not landed, never catalogued, or an outcome this code does not know: raises ``RuntimeError``
      (a definitive failure, never an import);
    * may have landed: raises :class:`ImportDocUncertain`, and fires no hook; landed but not
      stamped (the engine refused the stamp, or it failed after the chains fired): raises
      :class:`ImportDocUncertain` too, the document staying ``indexing``.

    A record with ``sensitive: true`` (RDR-243) passes the flag to ``put_note``, which sets it before any
    chunk is written (born flagged when the put mints the note), after the engine-capability refusal; a flag
    that cannot be set fails the record with nothing written.

    A record the catalog already flags (matched by ``source_uri``, on the recorded or the target identity)
    counts as flagged too, whatever the bundle says: the flag is a one-way property of the document, and an
    unreadable catalog reads as flagged. A flagged record bound for a collection that embeds with Voyage (or
    one whose embedder cannot be told) is refused unless *ack_external_embedder* (Sam's import ruling,
    :func:`nexus.sensitive_write.gate_sensitive_import`): this record's definitive failure, nothing written.

    Deferred imports: this module sits below ``commands/`` and the writer's pieces live in sibling
    modules with heavy import graphs. *t3* is used only to resolve the target collection.
    """
    from nexus.catalog.note_write import (  # noqa: PLC0415 — deferred, sibling with heavy import graph
        UNCERTAIN,
        failure_message,
        fire_note_chains,
        put_note,
        stamp_note,
    )
    from nexus.catalog.store_hook import split_note_tags  # noqa: PLC0415 — deferred, store_hook has a heavy import graph

    col_name = _import_target(rec, t3)
    title = rec.get("title", "")
    sensitive = rec.get("sensitive") is True
    from nexus.sensitive_write import catalog_flags_uri, gate_sensitive_import, is_external_embedder  # noqa: PLC0415 — deferred, leaf module read only here

    if not sensitive and is_external_embedder(col_name):
        sensitive = catalog_flags_uri(rec.get("source_uri", ""), uri_for(col_name, title))
    if sensitive:
        gate_sensitive_import([repr(title)], col_name, ack=ack_external_embedder, what="nx catalog import")
        # Before anything is written: an engine without the column would restore the note unflagged and
        # say nothing. A RuntimeError here is this record's definitive failure, counted and reported.
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred, factory has a heavy import graph
        from nexus.catalog.http_catalog_client import require_catalog_sensitivity  # noqa: PLC0415 — deferred, as above

        require_catalog_sensitivity(make_catalog_reader(), what=f"restoring sensitive note {title!r}")
    outcome = put_note(
        content=rec["content"], collection=col_name, title=rec["title"],
        tags=",".join(split_note_tags(rec.get("tags"))), category=rec.get("category", ""),
        ttl_days=_positive_ttl(rec.get("ttl_days")),
        source_agent="recovery-import",
        # An old bundle has no key: the put stamps the title if a configured pattern matches it in
        # full, else none, as before. A code__ target takes none.
        entity_key=_importable_entity_key(rec, col_name),
        # RDR-243: the note is born flagged, or flagged BEFORE any chunk is written when the put resolves an
        # existing note; a flag that cannot be set raises with nothing written (SensitiveFlagNotSetError, a
        # RuntimeError: this record's definitive failure). Never raised after the write, which would leave the
        # new text in T3 unflagged.
        sensitive=sensitive,
    )
    message = failure_message(outcome, subject=repr(title))
    if message is not None:
        if outcome.status == UNCERTAIN:
            raise ImportDocUncertain(message, stamp_refused=outcome.stamp_refused)
        raise RuntimeError(message)
    fire_note_chains(outcome, rec["content"])
    # The completion stamp, LAST (RDR-223, nexus-z0o2p.34): a kill in a chain above leaves the fence
    # 'indexing', so the next import of the note redoes the write and fires the chains again.
    outcome = stamp_note(outcome)
    message = failure_message(outcome, subject=repr(title))
    if message is not None:
        raise ImportDocUncertain(message, stamp_refused=outcome.stamp_refused)


def _import_target(rec: dict, t3: Any) -> str:
    """The collection a note is written to. A record the importer resolved from a format 3 collection row
    carries it as ``target_collection`` (the label, kept); a record of an older bundle, or one with no
    source id, goes through the name path."""
    resolved = rec.get("target_collection")
    if resolved:
        return resolved
    return target_collection_for(rec["collection"], t3)


def _importable_entity_key(rec: dict, collection: str) -> str:
    """The record's ``entity_key`` as a string a put will take, ``""`` when it has none or the
    target is a ``code__`` collection (nexus-fai28)."""
    from nexus.entity_keys import is_code_collection  # noqa: PLC0415 — deferred, keeps the module's import graph as it was

    value = rec.get("entity_key")
    if not isinstance(value, str) or is_code_collection(collection):
        return ""
    return value


def _resolve_link_endpoint(
    reader: Any, t3: Any, uri: str, *, held_back: frozenset[str] = frozenset(), kept: frozenset[str] = frozenset(),
    remap: dict[str, str] | None = None,
) -> Any:
    """``by_source_uri``, with a note-identity re-derivation fallback
    (review-fold, the critique's mode-change trace): a link exported from
    a source install carries ``nx-note://<SOURCE-collection>/<title>``, but
    on a mode-changed target the imported doc's minted identity uses the
    TARGET collection name — the recorded URI then resolves to nothing.
    Re-derive: parse collection/title out of the note URI, resolve the
    collection under the target (``target_collection_for``), and retry
    with the re-derived identity.

    An old bundle carries ``chroma://`` (RDR-231): *uri* goes through
    :func:`canonical_note_uri` first, so the compare and the parse both see
    the stored ``nx-note://`` form (Test Plan rows 557 and 564).

    Format 3 (RDR-229 P3.9): a note in a collection the import held back (*held_back*: refused or skipped)
    resolves to nothing, because the same label on this install may be another collection's. A note in a collection
    of the bundle's own (*kept*: its label was kept or remapped) is not re-derived by name. A note in a collection whose label this
    install did not keep (*remap*: a minted label, replaced by the one this install minted for the same attributes)
    is looked up under the label it was written to."""
    if not uri or not uri.strip():
        return None  # a blank URI names no document; the engine would read it as "no filter" (nexus-uwggq.1.9)
    uri = canonical_note_uri(uri)
    if held_back and uri.startswith(NOTE_URI_SCHEME) and uri[len(NOTE_URI_SCHEME):].partition("/")[0] in held_back:
        return None
    if remap and uri.startswith(NOTE_URI_SCHEME):
        source_label, sep, title = uri[len(NOTE_URI_SCHEME):].partition("/")
        if sep and title and source_label in remap:
            uri = uri_for(remap[source_label], title)
    entry = reader.by_source_uri(uri)
    if entry is not None or not uri.startswith(NOTE_URI_SCHEME):
        return entry
    rest = uri[len(NOTE_URI_SCHEME):]
    collection, sep, title = rest.partition("/")
    if collection in kept or collection in (remap or {}).values():
        return None
    if not sep or not title:
        return None
    try:
        target_col = target_collection_for(collection, t3)
    except Exception:  # noqa: BLE001 — fallback resolution is best-effort; the caller reports unresolvable
        return None
    rederived = uri_for(target_col, title)
    if rederived and rederived != uri:
        return reader.by_source_uri(rederived)
    return None


def _validated_span(reader: Any, span: str, summary: ImportSummary) -> str:
    """Locked span contract (design record; review-fold — this was
    declared-but-dead): a ``chash:``-anchored span whose chunk no longer
    exists on the target is STRIPPED and counted, and the link still
    imports without it. Non-chash spans and resolvable chashes pass
    through verbatim."""
    if not span.startswith("chash:"):
        return span
    chash = span.split(":", 2)[1]
    try:
        holders = reader.docs_for_chashes([chash])
    except Exception:  # noqa: BLE001 — span validation is advisory; an oracle failure must not block the link
        _log.warning("recovery_import_span_probe_failed", span=span)
        return span
    if holders.get(chash):
        return span
    summary.links_missing_span += 1
    _log.info("recovery_import_span_stripped", span=span)
    return ""


class _CollectionPlan:
    """What a format 3 import decided for each ``collection`` record, keyed by the record's source
    ``collection_id`` (a bundle-local key: the target mints or keeps its own id and never adopts it).

    ``target`` maps a source id to the label its notes are written under: the bundle's label, kept, or for a label
    of the minted form the label the target's engine minted for the same attributes. ``held_back`` maps a source id to
    the summary entry of a refused or skipped collection, whose ``docs_skipped`` the import counts up as it meets that
    collection's notes. ``rows`` holds the target's registry row for each label written to, and ``remap`` the labels
    that differ from the bundle's (bundle label to target label)."""

    __slots__ = ("target", "held_back", "rows", "remap")

    def __init__(self) -> None:
        self.target: dict[int, str] = {}
        self.held_back: dict[int, dict] = {}
        self.rows: dict[str, dict] = {}
        self.remap: dict[str, str] = {}

    @property
    def held_back_labels(self) -> frozenset[str]:
        return frozenset(e["label"] for e in self.held_back.values())


@dataclass(slots=True)
class _Resolved:
    """One ``collection`` record's outcome: the label and row its notes go to, or the refusal (and why), and the
    model change to name when the collection lands under another embedding model than the bundle's row says."""

    label: str = ""
    row: dict | None = None
    refusal: str = ""
    changed_model: dict | None = None


#: The minted form (RDR-229 item 8): ``c`` and digits. The engine renders it and reserves it, so a label of this form
#: in a bundle names the SOURCE install's collection by an id of its own; the same string on this install is
#: another collection's. Such a collection is identified by its attributes, never by its label.
_MINTED_LABEL = re.compile(r"^c[0-9]+$")


def _attrs_text(content_type: str, owner_id: str) -> str:
    return f"content_type {content_type!r}, owner_id {owner_id!r}"


def _plan_collections(reader: Any, writer: Any, records: list[dict], summary: ImportSummary) -> _CollectionPlan:
    """Resolve every ``collection`` record of a format 3 bundle BEFORE any note is written (a note whose
    collection has no registry row is refused by the engine, whatever its position in the file).

    Per record, by the label (RDR-229 item 10; linda's rulings on nexus-p2m1x.29):

    * not ``live`` in the bundle: skipped, counted, named; never registered live;
    * no row with that label on this install: registered from the row's content_type and owner_id under the
      TARGET's active embedding model (the notes are re-embedded from text, so the model of the source need not
      exist here); a change of model is named;
    * a live row with that label, the same content_type and owner_id: this row (an idempotent re-import);
    * a row with that label and another content_type or owner_id, or one that is not live: the collection is
      refused, naming both labels and their attributes. So is a collection the engine refuses to register (its
      label rule, a live tuple held under another label), and one whose model this install's configuration picks
      but the engine's embedding profile does not (the seam's ``EmbeddingProfileMismatchError`` text, before any
      register call);
    * a label of the minted form (``c<digits>``): not kept and never clashing. The engine mints this install's
      label for the row's attributes, or answers the live collection that already has them, and the notes go there.

    A refusal is that collection's alone. A failure to read or register is caught per collection for the same
    reason (the summary names it) and is never raised."""
    plan = _CollectionPlan()
    registry: dict[str, dict] | None = None
    for rec in records:
        if rec.get("record") != "collection":
            continue
        source_id, label = rec.get("collection_id"), rec.get("label") or ""
        entry: dict = {"label": label, "docs_skipped": 0}
        if source_id in plan.target or source_id in plan.held_back:
            summary.collections_refused_label_clash.append(
                {**entry, "error": f"collection_id {source_id} repeats an earlier collection record of the bundle"})
            continue
        state = rec.get("lifecycle_state") or "live"
        if state != "live":
            summary.collections_skipped_not_live.append({**entry, "lifecycle_state": state})
            plan.held_back[source_id] = summary.collections_skipped_not_live[-1]
            continue
        try:
            if registry is None:
                registry = _registry_rows(reader)
            resolved = _resolve_collection(reader, writer, rec, registry)
        except Exception as exc:  # noqa: BLE001 — per-collection fail-loud summary; the import must complete for the rest
            _log.warning("recovery_import_collection_failed", label=label, error=str(exc))
            resolved = _Resolved(refusal=f"could not be registered: {type(exc).__name__}: {exc}")
        if resolved.refusal:
            summary.collections_refused_label_clash.append({**entry, "error": resolved.refusal})
            plan.held_back[source_id] = summary.collections_refused_label_clash[-1]
            continue
        plan.target[source_id] = resolved.label
        plan.rows[resolved.label] = resolved.row or {}
        if resolved.label != label:
            plan.remap[label] = resolved.label
            summary.collections_relabelled.append({"label": label, "target_label": resolved.label})
        if resolved.changed_model is not None:
            summary.collections_changed_model.append({"label": label, **resolved.changed_model})
    return plan


def _resolve_collection(reader: Any, writer: Any, rec: dict, registry: dict[str, dict]) -> _Resolved:
    """One live ``collection`` record, resolved against this install (see :func:`_plan_collections`). Raises what the
    catalog raises; the caller counts that as a refusal."""
    from nexus.corpus import effective_embedding_model_for_writes  # noqa: PLC0415 — deferred, as target_collection_for's imports

    label, content_type, owner_id = rec.get("label") or "", rec.get("content_type") or "", rec.get("owner_id") or ""
    bundle_model = rec.get("embedding_model") or ""
    if not (label and content_type and owner_id):
        return _Resolved(refusal="the bundle's registry row for it is incomplete (label, content_type and owner_id are required)")
    minted = _MINTED_LABEL.match(label) is not None
    existing = None if minted else registry.get(label)
    if existing is not None:
        theirs = _attrs_text(existing.get("content_type", ""), existing.get("owner_id", ""))
        held_state = existing.get("lifecycle_state") or "live"
        if held_state != "live" or existing.get("superseded_by"):
            held_state = held_state if held_state != "live" else f"superseded by {existing['superseded_by']!r}"
            return _Resolved(refusal=(
                f"label {label!r} is {held_state!r} on this install ({theirs}), so the bundle's collection "
                f"({_attrs_text(content_type, owner_id)}) cannot import onto it"))
        if (existing.get("content_type"), existing.get("owner_id")) != (content_type, owner_id):
            return _Resolved(refusal=(
                f"label {label!r} is live on this install with {theirs}; the bundle's label {label!r} "
                f"has {_attrs_text(content_type, owner_id)}"))
        model = existing.get("embedding_model") or ""
        changed = {"from_model": bundle_model, "to_model": model} if model and model != bundle_model else None
        return _Resolved(label=label, row=existing, changed_model=changed)
    model = effective_embedding_model_for_writes(content_type)
    # The registration seam's profile check (ensure_collection_registered), made here because this site registers
    # outside the seam: effective_embedding_model_for_writes reads local.embed_model, not the engine's embedding
    # profile. No row holds this label on the target, so RDR-210 Gap 9's existing-row allowance never applies.
    from nexus.corpus import EmbeddingProfileMismatchError, _profile_model_for_content_type  # noqa: PLC0415 — as above
    profile_model = _profile_model_for_content_type(content_type)
    if profile_model is not None and profile_model != model:
        return _Resolved(refusal=str(EmbeddingProfileMismatchError(content_type, model, profile_model)))
    version = (rec.get("model_version") or "v1") if model == bundle_model else "v1"
    changed = {"from_model": bundle_model, "to_model": model} if model != bundle_model else None
    if minted:
        from nexus.collection_mint import register_by_attributes  # noqa: PLC0415 — deferred, as target_collection_for's imports

        try:
            row = register_by_attributes(
                content_type=content_type, owner_id=owner_id, embedding_model=model, model_version=version)
        except Exception as exc:  # noqa: BLE001 — the engine's refusal names the cause (an engine below the mint, a conflict)
            return _Resolved(refusal=f"the engine refused to register it: {_engine_error(exc)}")
        return _Resolved(
            label=row.label, changed_model=changed,
            row={"content_type": row.content_type, "owner_id": row.owner_id,
                 "embedding_model": row.embedding_model, "lifecycle_state": row.lifecycle_state or "live"})
    try:
        writer.register_collection(
            label, content_type=content_type, owner_id=owner_id, embedding_model=model, model_version=version)
    except Exception as exc:  # noqa: BLE001 — the engine's refusal names the cause (label rule, live tuple held elsewhere)
        return _Resolved(refusal=f"the engine refused to register it: {_engine_error(exc)}")
    row = reader.get_collection(label)
    if row is None:
        return _Resolved(refusal="the engine accepted the registration and holds no row for the label")
    row = {**row, "lifecycle_state": "live"}      # registered live by the client route; the single-row read carries no state
    registry[label] = row                           # so a repeated label in the bundle maps to it
    return _Resolved(label=label, row=row, changed_model=changed)


def _engine_error(exc: Exception) -> str:
    """The engine's own message for a refused registration, else the exception's."""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("error"):
                return f"{response.status_code} {body['error']}"
        except ValueError:
            pass
        return f"{response.status_code} {(response.text or '')[:300]}"
    return f"{type(exc).__name__}: {exc}"


def import_bundle(
    reader: Any,
    writer: Any,
    t3: Any,
    path: Path,
    *,
    import_doc: Callable[[Any, dict], None] | None = None,
    ack_external_embedder: bool = False,
) -> ImportSummary:
    """Import a bundle. Docs first (the bundle writes them first), then
    links resolved by source_uri. FAIL-LOUD SUMMARY, not abort: every
    failure class is enumerated on the returned summary; a partial import
    completes the resolvable records. One exception aborts: a
    NoteUriEngineTooOldError (RDR-231) is raised on the first note, since every
    later note would be refused the same way. Idempotent by construction — the
    store_put chain reconciles per sdp0u, and the engine merges duplicate
    links (``co_discovered_by``)."""
    _header, records = read_bundle(path)
    summary = ImportSummary()
    do_import = import_doc or partial(_default_import_doc, ack_external_embedder=ack_external_embedder)
    plan = _plan_collections(reader, writer, records, summary)
    seeded: set[str] = set()
    if plan.target:
        from nexus.collection_mint import note_registered  # noqa: PLC0415 — deferred: collection_mint reaches back into the catalog

    for rec in records:
        if rec.get("record") != "knowledge_doc":
            continue
        # An old bundle carries chroma:// (RDR-231): the stored form is nx-note://,
        # so no import_doc ever sees the legacy spelling.
        if rec.get("source_uri"):
            rec = {**rec, "source_uri": canonical_note_uri(rec["source_uri"])}
        source_id = rec.get("collection_id")
        if source_id is not None:
            # Format 3: the note goes where its collection's row was resolved, never by its recorded name.
            if source_id in plan.held_back:
                plan.held_back[source_id]["docs_skipped"] += 1
                continue
            if source_id not in plan.target:
                summary.docs_failed += 1
                summary.doc_failures.append({
                    "title": rec.get("title", ""),
                    "error": f"no collection record for collection_id {source_id} ({rec.get('collection', '')!r}) in the bundle"})
                continue
            target = plan.target[source_id]
            if target not in seeded:
                # Seeded now, not when the collection was resolved: the row answers for a few minutes only
                # (collection_mint._ROW_TTL_SECONDS), and a large bundle writes its first note into a late
                # collection long after the plan ran.
                note_registered(target, plan.rows[target])
                seeded.add(target)
            rec = {**rec, "target_collection": target}
            if target != rec.get("collection") and is_note_uri(rec.get("source_uri") or ""):
                # The bundle's URI names the source install's label; the catalog-flag probe must read this install's note.
                rec["source_uri"] = uri_for(target, rec.get("title", ""))
        try:
            do_import(t3, rec)
            summary.docs_imported += 1
        except ImportDocUncertain as exc:
            if exc.stamp_refused:
                summary.docs_stamp_refused += 1
            else:
                summary.docs_uncertain += 1
            summary.doc_unverified.append(
                {"title": rec.get("title", ""), "error": str(exc), "stamp_refused": exc.stamp_refused}
            )
            _log.warning(
                "recovery_import_doc_unverified",
                title=rec.get("title", ""),
                stamp_refused=exc.stamp_refused,
                error=str(exc),
            )
        except NoteUriEngineTooOldError:
            # RDR-231: the engine cannot hold a note's source URI, and that is true of every record that
            # follows. Abort on the first with the one error rather than N per-record failures, each of
            # which would probe the engine again. Records already imported stay (the import is idempotent).
            raise
        except Exception as exc:  # noqa: BLE001 — per-record fail-loud summary; the import must complete for the rest
            summary.docs_failed += 1
            summary.doc_failures.append(
                {"title": rec.get("title", ""), "error": str(exc)}
            )
            _log.warning(
                "recovery_import_doc_failed",
                title=rec.get("title", ""),
                error=str(exc),
            )

    # A collection of the bundle's own kept its label (or was remapped), so an endpoint in it is never re-derived
    # by name; an endpoint in any other collection (a note with no source id, written by the name path) is.
    bundle_labels = frozenset(r.get("label") or "" for r in records if r.get("record") == "collection")
    link_kw = {"held_back": plan.held_back_labels, "remap": plan.remap, "kept": bundle_labels}
    for rec in records:
        if rec.get("record") != "link":
            continue
        from_e = _resolve_link_endpoint(reader, t3, rec["from_source_uri"], **link_kw)
        to_e = _resolve_link_endpoint(reader, t3, rec["to_source_uri"], **link_kw)
        if from_e is None or to_e is None:
            summary.unresolvable_links.append(
                {
                    "from_source_uri": rec["from_source_uri"],
                    "to_source_uri": rec["to_source_uri"],
                    "link_type": rec["link_type"],
                    "missing": ("from" if from_e is None else "to"),
                }
            )
            _log.warning(
                "recovery_import_link_unresolvable",
                from_uri=rec["from_source_uri"],
                to_uri=rec["to_source_uri"],
                link_type=rec["link_type"],
            )
            continue
        # RDR-171: refuse a malformed dchash span on the raw record, before
        # _validated_span probes chash spans and counts a stripped one, so a
        # refused link is never also counted under links_missing_span.
        bad = rec.get("from_span", "")
        try:
            check_link_span(bad)
            bad = rec.get("to_span", "")
            check_link_span(bad)
        except ValueError as exc:
            summary.links_refused_span.append({
                "from_uri": rec["from_source_uri"], "to_uri": rec["to_source_uri"],
                "link_type": rec["link_type"], "span": bad, "error": str(exc),
            })
            _log.warning(
                "recovery_import_link_span_refused",
                from_uri=rec["from_source_uri"], to_uri=rec["to_source_uri"],
                link_type=rec["link_type"], span=bad, error=str(exc),
            )
            continue
        from_span = _validated_span(reader, rec.get("from_span", ""), summary)
        to_span = _validated_span(reader, rec.get("to_span", ""), summary)
        created = writer.link(
            from_e.tumbler,
            to_e.tumbler,
            rec["link_type"],
            rec.get("created_by") or "recovery-import",
            from_span=from_span,
            to_span=to_span,
        )
        if created:
            summary.links_created += 1
        else:
            summary.links_merged += 1

    _log.info(
        "recovery_bundle_imported",
        path=str(path),
        docs_imported=summary.docs_imported,
        docs_failed=summary.docs_failed,
        docs_uncertain=summary.docs_uncertain,
        docs_stamp_refused=summary.docs_stamp_refused,
        links_created=summary.links_created,
        links_merged=summary.links_merged,
        unresolvable=len(summary.unresolvable_links),
        links_refused_span=len(summary.links_refused_span),
        collections_refused=len(summary.collections_refused_label_clash),
        collections_skipped_not_live=len(summary.collections_skipped_not_live),
        collections_changed_model=len(summary.collections_changed_model),
    )
    return summary
