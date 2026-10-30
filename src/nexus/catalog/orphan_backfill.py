# SPDX-License-Identifier: AGPL-3.0-or-later
"""Orphan T3 chunk backfill: synthesize catalog Documents for chunks
that exist in T3 but have no catalog entry.

Complementary to ``nexus.catalog.manifest_backfill``:

* ``manifest_backfill``: catalog Documents exist; ``document_chunks``
  manifest is missing. Walk catalog docs, query T3 by doc_id, write rows.
* ``orphan_backfill`` (this module): catalog Documents do NOT exist.
  Walk T3 chunks, group by title (or content hash), synthesize Documents,
  write manifest rows.

Three modes:

1. ``DT-link``: search DEVONthink for each title, register Documents
   with ``source_uri='x-devonthink-item://<UUID>'`` for high-precision
   matches (score >= ``DEFAULT_MIN_SCORE``).
2. ``Synthetic``: for chunks without DT match (or for collections
   where DT lookup is impossible), register Documents with
   ``source_uri='nx-orphan-backfill://<collection>/<title_or_chash>'``
   so doctor reports clean without claiming false provenance.
3. ``CSV triage``: dump matched / low-confidence / unmatched titles to
   CSV files for operator review; ``apply_csv`` reads the curated
   output and registers the operator's verified UUID assignments.

Beads: nexus-h2pm (DT-link), nexus-4fw8 (synthetic), nexus-oa9k (CSV).
"""
from __future__ import annotations

import csv
import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from nexus.bounded_subprocess import run_bounded
from nexus.catalog.link_generator import _pdf_content_hash
from nexus.corpus import split_candidate_collection_name
from nexus.db.limits import QUOTAS

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader
    from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger(__name__)

DEFAULT_MIN_SCORE: float = 0.75
LOW_CONF_FLOOR: float = 0.55
DEFAULT_SEARCH_TOP_K: int = 30
_PAGE = QUOTAS.MAX_RECORDS_PER_WRITE  # 300

#: Curator owner-tumbler-prefix per orphan-prone collection.
#: Operators add new collections here as they appear in the orphan
#: census. Owners must already exist in the catalog.
DEFAULT_COLLECTION_OWNER: dict[str, str] = {
    "knowledge__art-papers": "1.9",
    "knowledge__art": "1.9",
    "knowledge__devonthink": "1.9",
    "knowledge__hybridrag": "1.9",
    "knowledge__delos": "1.9",
    "knowledge__knowledge": "1.10",
    "knowledge__test": "1.18",
    "docs__default": "1.20",
}


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class ChunkRef:
    """One T3 chunk awaiting a catalog Document."""

    cid: str  #: Chroma natural ID
    chash: str  #: the FULL chunk_text_hash (RDR-180); falls back to cid (itself the full chash)
    chunk_index: int = 0


@dataclass
class TitleGroup:
    """Chunks sharing one title, candidate for one Document."""

    title: str
    chunks: list[ChunkRef] = field(default_factory=list)


@dataclass
class DTMatch:
    """Outcome of DEVONthink fuzzy match for one TitleGroup."""

    title: str
    dt_uuid: str
    dt_name: str
    score: float
    chunks: list[ChunkRef] = field(default_factory=list)


@dataclass
class BackfillReport:
    """Summary of a backfill run."""

    collection: str
    titles_total: int = 0
    chunks_total: int = 0
    matched: list[DTMatch] = field(default_factory=list)
    low_confidence: list[DTMatch] = field(default_factory=list)
    unmatched: list[TitleGroup] = field(default_factory=list)
    docs_registered: int = 0
    chunks_linked: int = 0


@dataclass
class LinkSkips:
    """Rows ``link-existing`` left alone, and why.

    Filled in by :func:`link_by_title`, :func:`link_by_content_hash` and
    :func:`register_synthetic` (pass one in as ``skips=``, with ``skip_owned=True``)
    so the verb can say what it did not take. "Owned" means a LIVE Document's manifest names the
    chash in the target collection; a chunk only a trashed Document names, or
    one an earlier version of the target left behind, is not owned.
    """

    held_by_target: int = 0  #: the target Document's own manifest already holds the chash (under any collection value)
    owned_elsewhere: int = 0  #: another live Document's manifest holds it, in this collection
    ambiguous_titles: int = 0  #: title groups whose title names more than one live Document
    ambiguous_title_chunks: int = 0  #: chunks in those groups
    synthetic_owned: int = 0  #: chunks the synthetic fallback left alone because a live Document owns them


# ── T3 chunk gathering ───────────────────────────────────────────────────────


def gather_titled_chunks(
    t3_db,
    collection: str,
) -> list[TitleGroup]:
    """Pull all chunks from a T3 collection and group by ``title`` metadata.

    Strips trailing ``:page-N`` / ``:1-794`` suffixes so chunks of the
    same source-document collapse to one group.

    Chunks lacking a title fall under the empty-string key ``''`` and are
    returned as a single group; callers can branch on them (use chash
    grouping or skip).
    """
    # nexus-at2ff: was ``t3_db._client.get_collection`` — production make_t3()
    # returns HttpVectorClient, which has no ``_client`` (only the T3Database
    # test facade does), so these verbs raised AttributeError in service mode.
    # Both handles expose get_collection directly.
    col = t3_db.get_collection(name=collection)
    n = col.count()
    by_title: dict[str, list[ChunkRef]] = defaultdict(list)
    offset = 0
    while offset < n:
        # Stored rows, owned or not: an orphan is by definition a chunk no
        # manifest owns, and a live read hides it (RDR-192 Step 5, nexus-wbfpw.10).
        batch = col.get(
            limit=_PAGE, offset=offset, include=["metadatas"], include_non_live=True,
        )
        cids = batch.get("ids") or []
        metas = batch.get("metadatas") or []
        for cid, meta in zip(cids, metas):
            if not isinstance(meta, dict):
                meta = {}
            t = str(meta.get("title") or "").strip()
            t = re.sub(r":[A-Za-z0-9\-]+$", "", t).strip()
            chash = str(meta.get("chunk_text_hash") or cid)  # RDR-180: cid IS the full chash
            chunk_index = int(meta.get("chunk_index", 0) or 0)
            by_title[t].append(ChunkRef(
                cid=cid, chash=chash, chunk_index=chunk_index,
            ))
        offset += _PAGE
    return [TitleGroup(title=t, chunks=cs) for t, cs in by_title.items()]


def drop_deleted_title_groups(
    catalog: "CatalogReader", collection: str, groups: list[TitleGroup],
) -> tuple[list[TitleGroup], list[str]]:
    """Split off the title groups that belong to a DELETED catalog document.

    The gather above reads every stored chunk, which includes those of a document
    that was deleted but not yet purged, and the registering verbs (``dt-link``,
    ``synthetic``, ``apply-csv``) mint a new live Document per title group. A
    group whose title is a tombstoned document's in *collection*, with no live
    document of that title there, would bring the deleted document back under a
    synthetic or DEVONthink identity (nexus-wbfpw.35 fix round 3). The title is
    this module's own identity for a group (``link_by_title`` matches on it), so
    it is what a tombstone is matched on, exactly.

    Returns ``(kept_groups, dropped_titles)``. Raises
    :class:`~nexus.catalog.tombstones.TombstoneGuardUnavailable` when the engine
    cannot list deleted documents: failing closed, as the other registering verbs
    do.
    """
    from nexus.catalog.tombstones import read_tombstones  # noqa: PLC0415 — deferred: keeps catalog import light

    tombs = read_tombstones(catalog)
    deleted_titles = {
        d.title for d in tombs.docs if d.title and d.physical_collection == collection
    }
    if not deleted_titles:
        return groups, []
    live_titles = {
        e.title for e in catalog.list_by_collection(collection, limit=1_000_000) if e.title
    }
    gone = deleted_titles - live_titles
    dropped = sorted({g.title for g in groups if g.title in gone})
    if not dropped:
        return groups, []
    return [g for g in groups if g.title not in gone], dropped


# ── DEVONthink search via osascript ──────────────────────────────────────────


class DTSearchError(RuntimeError):
    """osascript or DEVONthink query failure."""


def _osascript(script: str, *, timeout: int = 30) -> str:
    r = run_bounded(
        ["osascript", "-e", script],
        timeout=timeout,
    )
    if r.returncode != 0:
        raise DTSearchError(
            f"osascript exit {r.returncode}: {r.stderr.strip()}"
        )
    return r.stdout.strip()


def dt_search(
    query: str, *, top_k: int = DEFAULT_SEARCH_TOP_K, timeout: int = 20,
) -> list[tuple[str, str]]:
    """Single-query DEVONthink search. Returns up to ``top_k`` ``(uuid, name)`` pairs."""
    safe = query.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "DEVONthink"\n'
        f'    set theResults to search "{safe}"\n'
        '    set theOutput to ""\n'
        '    set theCount to count of theResults\n'
        f'    if theCount > {top_k} then set theCount to {top_k}\n'
        '    repeat with i from 1 to theCount\n'
        '        set rec to item i of theResults\n'
        '        set theOutput to theOutput & (uuid of rec) & "\\t" & '
        '(name of rec) & linefeed\n'
        '    end repeat\n'
        '    return theOutput\n'
        'end tell'
    )
    out = _osascript(script, timeout=timeout)
    pairs: list[tuple[str, str]] = []
    for line in out.split("\n"):
        if "\t" in line:
            u, n = line.split("\t", 1)
            pairs.append((u.strip(), n.strip()))
    return pairs


def dt_multi_search(
    title: str, *, top_k: int = DEFAULT_SEARCH_TOP_K,
) -> list[tuple[str, str]]:
    """Issue several query variants per title and merge the candidate
    pool; deduplicates by UUID. Variants explore different word slices
    so DT's substring search returns different candidate sets."""
    queries: list[str] = []
    no_year = re.sub(r"\b(19|20)\d{2}\b", " ", title)
    cleaned = re.sub(r"[^\w\s]", " ", no_year).strip()
    words = cleaned.split()
    if not words:
        return []
    queries.append(" ".join(words[:6]))
    if len(words) >= 3:
        queries.append(" ".join(words[:3]))
        queries.append(" ".join(words[-3:]))
    queries.append(title)
    if len(words) >= 6:
        queries.append(" ".join(words[2:6]))

    seen: set[str] = set()
    merged: list[tuple[str, str]] = []
    for q in queries:
        if not q:
            continue
        try:
            for u, n in dt_search(q, top_k=top_k):
                if u not in seen:
                    seen.add(u)
                    merged.append((u, n))
        except DTSearchError as e:
            _log.debug("dt_multi_search_query_failed", query=q, error=str(e))
            continue
    return merged


def best_match(
    title: str,
    candidates: list[tuple[str, str]],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
) -> tuple[float, str, str] | None:
    """Score candidates by ``difflib.SequenceMatcher`` against ``title``;
    return the top candidate if its score >= ``min_score``."""
    if not candidates:
        return None
    title_lower = title.lower()
    scored = [
        (
            difflib.SequenceMatcher(None, title_lower, n.lower()).ratio(),
            u, n,
        )
        for u, n in candidates
    ]
    scored.sort(reverse=True)
    return scored[0] if scored[0][0] >= min_score else None


# ── Backfill execution ───────────────────────────────────────────────────────


def classify_groups(
    groups: list[TitleGroup],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    low_conf_floor: float = LOW_CONF_FLOOR,
    searcher=dt_multi_search,
) -> tuple[list[DTMatch], list[DTMatch], list[TitleGroup]]:
    """Run DT lookup for each group; partition into
    ``(matched, low_confidence, unmatched)``.

    ``matched`` are at score >= ``min_score`` (high-precision).
    ``low_confidence`` are at ``[low_conf_floor, min_score)``, suitable
    for CSV triage.
    ``unmatched`` had no candidates above ``low_conf_floor``.

    ``searcher`` is injectable for testing without DEVONthink running.
    """
    matched: list[DTMatch] = []
    low_conf: list[DTMatch] = []
    unmatched: list[TitleGroup] = []
    for g in groups:
        if not g.title:
            # No title metadata; cannot DT-search. Fall through to unmatched
            # (caller routes to synthetic mode via chash grouping).
            unmatched.append(g)
            continue
        cands = searcher(g.title)
        # Try at the lower threshold to catch low-conf bucket too.
        best_low = best_match(g.title, cands, min_score=low_conf_floor)
        if best_low is None:
            unmatched.append(g)
            continue
        score, u, name = best_low
        match = DTMatch(
            title=g.title, dt_uuid=u, dt_name=name, score=score,
            chunks=g.chunks,
        )
        if score >= min_score:
            matched.append(match)
        else:
            low_conf.append(match)
    return matched, low_conf, unmatched


def _content_type_for_collection(collection: str) -> str:
    """Infer the catalog content_type from a collection's RDR-103 prefix.

    Conformant collection names are ``<content_type>__<owner>__<embed>__v<n>``
    (RDR-103), so the leading segment IS the content_type (``code`` /
    ``knowledge`` / ``rdr`` / ``docs`` / ...). nexus-s5le: orphan-backfill
    previously hardcoded ``"pdf"`` on every synthesized row regardless of
    corpus, so those rows never matched a ``content_type=`` catalog filter for
    their real corpus (the canonical query types are code/paper/rdr/knowledge/
    docs, not pdf). Falls back to ``"knowledge"`` for a non-conformant name
    with no prefix.
    """
    # RDR-204 Phase 3 (nexus-ft04v.26), class (d): this derives content_type
    # for a SYNTHESIZED (registering) catalog row -- the collection this
    # backfill is registering has, by definition, no row yet. Candidate-
    # string derivation (split_candidate_collection_name), not the
    # row-based collection_content_type/collection_owner -- see
    # http_vector_client.per_collection_chunk_cap's comment for why the
    # naive `split_candidate_collection_name(x)[0] or x` would diverge
    # (it conflates "no '__' at all" with "'__' present but the first
    # segment is empty", e.g. "__weird").
    _ct_probe, _owner_probe = split_candidate_collection_name(collection)
    raw_prefix = collection if _owner_probe == collection else _ct_probe
    prefix = raw_prefix.strip()
    return prefix or "knowledge"


# nexus-jz86a: the manifest rows below carry NO section label keys and no ``labels_written``, on
# purpose. The engine reads that as legacy (NULL labels, ``labels_written`` false), which is the true
# state: the positions are synthesised, so the shared chunk row's label (a former owner's) is not this
# row's own answer, and "" would assert "no heading" for good. nexus-1nrcu's one-time rung copies the
# chunk row's label onto the row where the chash has exactly one manifest row.

def register_dt_linked(
    catalog: "CatalogReader",
    owner: "Tumbler",
    collection: str,
    matches: list[DTMatch],
) -> tuple[int, int]:
    """Register one Document per match with ``x-devonthink-item://`` URI;
    write document_chunks rows. Returns ``(docs_registered, chunks_linked)``."""
    docs = 0
    links = 0
    for m in matches:
        tumbler = catalog.register(
            owner,
            title=m.title,
            content_type=_content_type_for_collection(collection),
            file_path="",
            physical_collection=collection,
            chunk_count=len(m.chunks),
            source_uri=f"x-devonthink-item://{m.dt_uuid}",
            meta={
                "backfill_from": "t3_orphan",
                "backfill_mode": "dt_link",
                "dt_uuid": m.dt_uuid,
                "dt_name": m.dt_name,
                "fuzzy_score": round(m.score, 3),
            },
        )
        docs += 1
        # nexus-w5zv: stable-sort by chunk_index so pre-Phase-3 chunks
        # land in document order; Phase-3 chunks (all chunk_index == 0)
        # preserve chroma insertion order (the only signal we have when
        # the indexer dropped the field).
        ordered_chunks = sorted(m.chunks, key=lambda c: c.chunk_index)
        chunks_payload = [
            {
                "chash": c.chash,
                "position": pos,
                "line_start": None, "line_end": None,
                "char_start": None, "char_end": None,
            }
            for pos, c in enumerate(ordered_chunks)
        ]
        catalog.write_manifest(str(tumbler), chunks_payload, collection=collection)
        links += len(chunks_payload)
        _log.info(
            "orphan_backfill_dt_linked",
            collection=collection, tumbler=str(tumbler),
            dt_uuid=m.dt_uuid, score=m.score, chunks=len(m.chunks),
        )
    return docs, links


def register_synthetic(
    catalog: "CatalogReader",
    owner: "Tumbler",
    collection: str,
    groups: list[TitleGroup],
    *,
    skips: LinkSkips | None = None,
    skip_owned: bool = False,
) -> tuple[int, int]:
    """Register one Document per group with synthetic
    ``nx-orphan-backfill://`` URI; write document_chunks rows.

    For groups lacking a title, falls back to chash-grouping: each
    distinct chash gets its own minimal Document so manifest rows can
    still be written. The synthetic URI scheme is intentionally clear
    so search consumers can distinguish residual data from authoritative
    provenance.

    Every chunk of a group is registered unless ``skip_owned`` is set, which only
    ``link-existing --also-synthetic`` does. The direct ``synthetic`` verb leaves
    it off: it may be run again, the Document's URI is the same, and a manifest
    write REPLACES the Document's rows, so a run that left out the chunks the
    first run registered (now owned by that Document) would drop them.

    With ``skip_owned``, a chunk a live Document of *collection* already names is
    left alone (counted in ``skips.synthetic_owned``): a synthetic Document would
    hold it a second time. A group left with nothing registers no Document. The
    ownership lookup runs before the first registration and raises rather than
    answer partially, so a failure registers nothing. Those groups are the ones
    no live Document of the title matched, so no synthetic Document of the title
    exists for the write to replace."""
    owners: dict[str, list[str]] = {}
    if skip_owned:
        owners = live_owners_in_collection(
            catalog, [c.chash for g in groups for c in g.chunks], collection,
        )
    docs = 0
    links = 0
    for g in groups:
        chunks = [c for c in g.chunks if c.chash not in owners]
        if skips is not None:
            skips.synthetic_owned += len(g.chunks) - len(chunks)
        if not chunks:
            continue
        if g.title:
            uri = f"nx-orphan-backfill://{collection}/{g.title}"
            title = g.title
            # nexus-w5zv: stable sort by chunk_index (same rationale as
            # register_dt_linked above).
            ordered_chunks = sorted(chunks, key=lambda c: c.chunk_index)
            chunks_payload = [
                {
                    "chash": c.chash, "position": pos,
                    "line_start": None, "line_end": None,
                    "char_start": None, "char_end": None,
                }
                for pos, c in enumerate(ordered_chunks)
            ]
            tumbler = catalog.register(
                owner,
                title=title,
                content_type=_content_type_for_collection(collection),
                file_path="",
                physical_collection=collection,
                chunk_count=len(chunks),
                source_uri=uri,
                meta={
                    "backfill_from": "t3_orphan",
                    "backfill_mode": "synthetic",
                },
            )
            docs += 1
            catalog.write_manifest(str(tumbler), chunks_payload, collection=collection)
            links += len(chunks_payload)
        else:
            # No title; bucket each chunk under its chash as a singleton doc.
            for c in chunks:
                title = f"orphan-chunk-{c.chash[:12]}"
                uri = f"nx-orphan-backfill://{collection}/chash/{c.chash}"
                tumbler = catalog.register(
                    owner,
                    title=title,
                    content_type=_content_type_for_collection(collection),
                    file_path="",
                    physical_collection=collection,
                    chunk_count=1,
                    source_uri=uri,
                    meta={
                        "backfill_from": "t3_orphan",
                        "backfill_mode": "synthetic_chash",
                        "chash": c.chash,
                    },
                )
                docs += 1
                catalog.write_manifest(str(tumbler), [{
                    "chash": c.chash, "position": 0,
                    "line_start": None, "line_end": None,
                    "char_start": None, "char_end": None,
                }], collection=collection)
                links += 1
    return docs, links


# ── CSV triage I/O ───────────────────────────────────────────────────────────


def dump_csvs(
    out_dir: Path,
    collection: str,
    matched: list[DTMatch],
    low_conf: list[DTMatch],
    unmatched: list[TitleGroup],
) -> tuple[Path, Path, Path]:
    """Write three CSVs to ``out_dir/<collection>/``:
    ``matched.csv``, ``low_confidence.csv``, ``unmatched.csv``.

    Operators edit ``low_confidence.csv`` and ``unmatched.csv`` then feed
    them back via :func:`apply_csv`.
    """
    target = out_dir / collection
    target.mkdir(parents=True, exist_ok=True)
    matched_path = target / "matched.csv"
    low_path = target / "low_confidence.csv"
    unmatched_path = target / "unmatched.csv"

    with matched_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["title", "dt_uuid", "dt_name", "score", "chunk_count"])
        for m in matched:
            w.writerow([
                m.title, m.dt_uuid, m.dt_name,
                f"{m.score:.3f}", len(m.chunks),
            ])
    with low_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "title", "candidate_dt_uuid", "candidate_dt_name",
            "score", "chunk_count", "operator_decision",
        ])
        for m in low_conf:
            w.writerow([
                m.title, m.dt_uuid, m.dt_name,
                f"{m.score:.3f}", len(m.chunks), "",
            ])
    with unmatched_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["title", "chunk_count", "operator_dt_uuid"])
        for g in unmatched:
            w.writerow([g.title, len(g.chunks), ""])
    return matched_path, low_path, unmatched_path


def apply_csv(
    catalog: "CatalogReader",
    owner: "Tumbler",
    collection: str,
    csv_path: Path,
    *,
    chunk_lookup: dict[str, list[ChunkRef]],
) -> tuple[int, int]:
    """Apply an operator-curated CSV.

    Recognized columns (any of these UUID fields wins, in order):
    ``operator_dt_uuid`` (unmatched.csv), ``operator_decision``
    (low_confidence.csv where the operator types either ``approve`` or
    a UUID), ``dt_uuid`` (matched.csv re-apply).

    Rows where no UUID is provided are skipped. ``chunk_lookup`` maps
    title → ChunkRefs from the original ``gather_titled_chunks`` call;
    callers preserve this between dump and apply phases.
    """
    docs = 0
    links = 0
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = row.get("title", "").strip()
            if not title:
                continue
            uuid_str = (
                row.get("operator_dt_uuid", "").strip()
                or row.get("dt_uuid", "").strip()
            )
            decision = row.get("operator_decision", "").strip().lower()
            if decision and decision != "approve" and len(decision) >= 32:
                # Operator typed a UUID directly into the decision column.
                uuid_str = decision
            elif decision == "approve":
                # Approve the suggested candidate UUID.
                uuid_str = row.get("candidate_dt_uuid", "").strip() or uuid_str
            if not uuid_str:
                continue
            chunks = chunk_lookup.get(title, [])
            if not chunks:
                _log.warning(
                    "apply_csv_title_no_chunks",
                    collection=collection, title=title,
                )
                continue
            tumbler = catalog.register(
                owner,
                title=title,
                content_type=_content_type_for_collection(collection),
                file_path="",
                physical_collection=collection,
                chunk_count=len(chunks),
                source_uri=f"x-devonthink-item://{uuid_str}",
                meta={
                    "backfill_from": "t3_orphan",
                    "backfill_mode": "csv_curated",
                    "dt_uuid": uuid_str,
                    "csv_source": str(csv_path),
                },
            )
            docs += 1
            # nexus-w5zv: stable sort by chunk_index (see register_dt_linked).
            ordered_chunks = sorted(chunks, key=lambda c: c.chunk_index)
            catalog.write_manifest(str(tumbler), [
                {
                    "chash": c.chash, "position": pos,
                    "line_start": None, "line_end": None,
                    "char_start": None, "char_end": None,
                }
                for pos, c in enumerate(ordered_chunks)
            ], collection=collection)
            links += len(chunks)
    return docs, links


# ── Link T3 chunks to EXISTING catalog Documents ─────────────────────────────
#
# A T3 chunk row is one per (collection, chunk text) and is shared by every
# Document holding that text; its ``title`` and ``content_hash`` are the LAST
# writer's. So a title or hash group is not "the Document's chunks": it holds
# chunks the target already owns (a second manifest row for the same chunk) and
# chunks another Document owns, and it misses a chunk the target owns that
# another writer restamped. The only chunks that are the target's to take are
# those no live manifest names (nexus-6pfgb.22, census rows x54wv 13 and 3ajpd 7).


def live_owners_in_collection(
    catalog: "CatalogReader", chashes: list[str], collection: str,
) -> dict[str, list[str]]:
    """``chash -> [doc_id, ...]`` for the LIVE Documents whose manifest names the
    chash in *collection*. A chash nothing names there is absent.

    Reads the engine's chash-positions route (its payload is proportional to the
    hits); where its rows carry their own collection they are filtered on it. An
    engine whose rows carry none (engine-service-v0.1.164 and earlier, the
    released engines) or that has no route falls back to the full manifests of
    the referencing Documents, filtered the same way. A row with no collection
    counts as owned (it cannot be shown to belong elsewhere). Both reads raise
    rather than answer partially, so a caller that asks before it writes links
    nothing on a failure.
    """
    return _lookup_owners(catalog, chashes, collection)[0]


def _lookup_owners(
    catalog: "CatalogReader", chashes: list[str], collection: str,
) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    """``(owners, holders)``: *owners* as :func:`live_owners_in_collection`;
    *holders* is ``chash -> {doc_id, ...}`` over the live manifests under ANY
    collection value, so a target that names a chash under another collection
    value can be told from a Document that does not name it at all."""
    wanted = list(dict.fromkeys(c for c in chashes if c))
    if not wanted:
        return {}, {}
    owners: dict[str, list[str]] = {}
    holders: dict[str, set[str]] = {}

    def _add(chash: str, doc_id: str, row_collection: object) -> None:
        holders.setdefault(chash, set()).add(doc_id)
        if row_collection in (collection, None):
            held = owners.setdefault(chash, [])
            if doc_id not in held:
                held.append(doc_id)

    lookup = getattr(catalog, "chash_positions", None)
    rows = lookup(wanted) if lookup is not None else None
    if rows is not None and all(isinstance(r, dict) and "collection" in r for r in rows):
        for r in rows:
            _add(r["chash"], r["doc_id"], r["collection"])
        return owners, holders
    _by_chash, manifests = catalog.docs_and_manifests_for_chashes(wanted)
    want = set(wanted)
    for doc_id, manifest in manifests.items():
        for m in manifest:
            if m.chash in want:
                _add(m.chash, doc_id, m.collection)
    return owners, holders


def _manifest_less_for(
    tumbler: str,
    chunks: list[ChunkRef],
    owners: dict[str, list[str]],
    holders: dict[str, set[str]],
    skips: LinkSkips,
) -> list[ChunkRef]:
    """The chunks of *chunks* that no live manifest in the collection names,
    deduplicated by chash.

    A chash the target's manifest names, under any collection value, counts under
    ``held_by_target`` (also when another Document holds it too): a second row for
    it would double it. One only another live Document names in the collection
    counts under ``owned_elsewhere``.
    """
    keep: list[ChunkRef] = []
    seen: set[str] = set()
    for c in chunks:
        if c.chash in seen:
            continue
        seen.add(c.chash)
        if tumbler in holders.get(c.chash, ()):
            skips.held_by_target += 1
        elif not owners.get(c.chash):
            keep.append(c)
        else:
            skips.owned_elsewhere += 1
    return keep


def _append_after_existing(
    catalog: "CatalogReader", tumbler: str, collection: str, chunks: list[ChunkRef],
) -> None:
    """Append *chunks* to *tumbler*'s manifest after its current rows, in chunk_index order.

    Positions are not dense (a manifest can keep a gap), and an append upserts BY
    position, so the start is past the highest position held, never the row count.
    """
    start_pos = max((r.position for r in catalog.get_manifest(tumbler)), default=-1) + 1
    # nexus-w5zv: stable sort by chunk_index (see register_dt_linked).
    ordered_chunks = sorted(chunks, key=lambda c: c.chunk_index)
    # legacy rows on purpose: no label keys (nexus-jz86a, see the note above register_dt_linked)
    catalog.append_manifest_chunks(tumbler, [
        {
            "chash": c.chash,
            "position": start_pos + pos,
            "line_start": None, "line_end": None,
            "char_start": None, "char_end": None,
        }
        for pos, c in enumerate(ordered_chunks)
    ], collection=collection)


def link_by_title(
    catalog: "CatalogReader",
    collection: str,
    groups: list[TitleGroup],
    *,
    dry_run: bool = False,
    skips: LinkSkips | None = None,
) -> tuple[int, int, list[TitleGroup]]:
    """For each titled group, find an existing catalog Document with the
    same title in ``collection`` and append manifest rows linking the
    group's MANIFEST-LESS chunks to that Document.

    A chunk some live manifest in the collection already names is not appended:
    one the target holds would become a second row for the same chunk, and one
    another live Document owns is not the target's (the chunk's ``title`` is the
    last writer's). Both are counted in *skips*. A matched group is never
    returned as unlinked, even when every chunk in it was skipped. A title two
    live Documents share matches neither: its group is counted in *skips*
    (``ambiguous_titles``) and returned nowhere.

    Returns ``(linked_chunks, linked_docs, unlinked_groups)``.
    ``unlinked_groups`` are groups whose title has no catalog match;
    callers route them to synthetic mode. With ``dry_run`` nothing is written
    and the counts are what a real run would link.

    Untitled groups are passed through to ``unlinked_groups`` so
    callers can branch them to chash fallback.
    """
    skips = skips if skips is not None else LinkSkips()
    # nexus-2hgxb (nrxs9 final review Critical-2): this was a raw
    # ``catalog._db`` reach — an AttributeError against the service client,
    # the only catalog since the local deletion. The public per-collection
    # listing carries (tumbler, title, index_content_hash, meta); the explicit
    # high limit matters — a server-side default page would silently truncate
    # the backfill's match universe.
    entries = catalog.list_by_collection(collection, limit=1_000_000)
    tumblers_by_title: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        if e.title:
            tumblers_by_title[e.title].append(str(e.tumbler))
    # A title two live Documents share names no single Document: its group is
    # neither linked to an arbitrary one nor passed on to the synthetic fallback
    # (which would register a third Document of that title).
    by_title = {t: ts[0] for t, ts in tumblers_by_title.items() if len(ts) == 1}
    ambiguous = {t: ts for t, ts in tumblers_by_title.items() if len(ts) > 1}
    matched: list[TitleGroup] = []
    unlinked: list[TitleGroup] = []
    for g in groups:
        if g.title and g.title in by_title:
            matched.append(g)
        elif g.title and g.title in ambiguous:
            skips.ambiguous_titles += 1
            skips.ambiguous_title_chunks += len(g.chunks)
            _log.warning(
                "orphan_backfill_title_ambiguous",
                collection=collection, title=g.title, tumblers=ambiguous[g.title],
            )
        else:
            unlinked.append(g)
    if not matched:
        return 0, 0, unlinked
    # One reverse lookup for every candidate chash. It raises rather than answer
    # partially, so a failure links nothing.
    owners, holders = _lookup_owners(
        catalog, [c.chash for g in matched for c in g.chunks], collection,
    )
    linked_chunks = 0
    linked_docs = 0
    for g in matched:
        tumbler = by_title[g.title]
        chunks = _manifest_less_for(tumbler, g.chunks, owners, holders, skips)
        if not chunks:
            continue
        if not dry_run:
            _append_after_existing(catalog, tumbler, collection, chunks)
        linked_chunks += len(chunks)
        linked_docs += 1
        _log.info(
            "orphan_backfill_linked_by_title",
            collection=collection, tumbler=tumbler, title=g.title,
            chunks=len(chunks), dry_run=dry_run,
        )
    return linked_chunks, linked_docs, unlinked


def link_by_content_hash(
    catalog: "CatalogReader",
    t3_db,
    collection: str,
    *,
    dry_run: bool = False,
    skips: LinkSkips | None = None,
) -> tuple[int, int, int]:
    """For each existing catalog Document with a file-content hash, find
    T3 chunks in ``collection`` whose ``content_hash`` matches and append
    manifest rows for the MANIFEST-LESS ones.

    Returns ``(linked_chunks, linked_docs, unmatched_chunks)``.
    ``unmatched_chunks`` are chunks whose ``content_hash`` names no single
    Document. A chunk some live manifest already names is skipped and counted
    in *skips* (see :func:`link_by_title`). With ``dry_run`` nothing is
    written and the counts are what a real run would link.

    Used when T3 chunks lack ``title`` metadata but have
    ``content_hash`` (PDF-shaped chunks). The Document side is
    ``index_content_hash``, else ``meta.content_hash``
    (``link_generator._pdf_content_hash``), the same file-bytes hash the
    chunks carry, so matching is exact. ``head_hash`` is never the key:
    for repo rows it is the indexing commit (nexus-lhmu1, as nexus-utkop).
    A hash two Documents share names no single Document, so its chunks
    stay unmatched rather than join an arbitrary one.
    """
    skips = skips if skips is not None else LinkSkips()
    # nexus-2hgxb: same public-read port as link_by_title above.
    entries = catalog.list_by_collection(collection, limit=1_000_000)
    tumblers_by_hash: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        if content_key := _pdf_content_hash(e):
            tumblers_by_hash[content_key].append(str(e.tumbler))
    if not tumblers_by_hash:
        return 0, 0, 0
    by_content: dict[str, str] = {}
    for content_key, tumblers in tumblers_by_hash.items():
        if len(tumblers) == 1:
            by_content[content_key] = tumblers[0]
        else:
            _log.warning(
                "orphan_backfill_content_hash_ambiguous",
                collection=collection, content_hash=content_key[:16],
                tumblers=tumblers,
            )

    # nexus-at2ff: was ``t3_db._client.get_collection`` — production make_t3()
    # returns HttpVectorClient, which has no ``_client`` (only the T3Database
    # test facade does), so these verbs raised AttributeError in service mode.
    # Both handles expose get_collection directly.
    col = t3_db.get_collection(name=collection)
    n = col.count()
    # Group chunks by content_hash so we batch one append_manifest per doc.
    by_hash: dict[str, list[ChunkRef]] = defaultdict(list)
    unmatched = 0
    offset = 0
    while offset < n:
        batch = col.get(
            limit=_PAGE, offset=offset, include=["metadatas"], include_non_live=True,
        )
        cids = batch.get("ids") or []
        metas = batch.get("metadatas") or []
        for cid, meta in zip(cids, metas):
            if not isinstance(meta, dict):
                meta = {}
            content_hash = str(meta.get("content_hash") or "")
            chash = str(meta.get("chunk_text_hash") or cid)  # RDR-180: cid IS the full chash
            chunk_index = int(meta.get("chunk_index", 0) or 0)
            if content_hash and content_hash in by_content:
                by_hash[content_hash].append(ChunkRef(
                    cid=cid, chash=chash, chunk_index=chunk_index,
                ))
            else:
                unmatched += 1
        offset += _PAGE

    if not by_hash:
        return 0, 0, unmatched
    # One reverse lookup for every candidate chash; it raises rather than answer
    # partially, so a failure links nothing.
    owners, holders = _lookup_owners(
        catalog, [c.chash for chunks in by_hash.values() for c in chunks], collection,
    )
    linked_chunks = 0
    linked_docs = 0
    for content_hash, group in by_hash.items():
        tumbler = by_content[content_hash]
        chunks = _manifest_less_for(tumbler, group, owners, holders, skips)
        if not chunks:
            continue
        if not dry_run:
            _append_after_existing(catalog, tumbler, collection, chunks)
        linked_chunks += len(chunks)
        linked_docs += 1
        _log.info(
            "orphan_backfill_linked_by_content_hash",
            collection=collection, tumbler=tumbler,
            content_hash=content_hash[:16], chunks=len(chunks), dry_run=dry_run,
        )
    return linked_chunks, linked_docs, unmatched
