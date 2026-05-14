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
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from nexus.db.chroma_quotas import QUOTAS

if TYPE_CHECKING:
    from nexus.catalog.catalog import Catalog
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
    chash: str  #: chunk_text_hash[:32]; falls back to cid[:32]
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
    col = t3_db.get_collection(collection)
    n = col.count()
    by_title: dict[str, list[ChunkRef]] = defaultdict(list)
    offset = 0
    while offset < n:
        batch = col.get(
            limit=_PAGE, offset=offset, include=["metadatas"],
        )
        cids = batch.get("ids") or []
        metas = batch.get("metadatas") or []
        for cid, meta in zip(cids, metas):
            if not isinstance(meta, dict):
                meta = {}
            t = str(meta.get("title") or "").strip()
            t = re.sub(r":[A-Za-z0-9\-]+$", "", t).strip()
            chash = str(meta.get("chunk_text_hash") or cid[:32])
            chunk_index = int(meta.get("chunk_index", 0) or 0)
            by_title[t].append(ChunkRef(
                cid=cid, chash=chash, chunk_index=chunk_index,
            ))
        offset += _PAGE
    return [TitleGroup(title=t, chunks=cs) for t, cs in by_title.items()]


# ── DEVONthink search via osascript ──────────────────────────────────────────


class DTSearchError(RuntimeError):
    """osascript or DEVONthink query failure."""


def _osascript(script: str, *, timeout: int = 30) -> str:
    r = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout,
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


def register_dt_linked(
    catalog: "Catalog",
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
            content_type="pdf",
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
        catalog.write_manifest(str(tumbler), chunks_payload)
        links += len(chunks_payload)
        _log.info(
            "orphan_backfill_dt_linked",
            collection=collection, tumbler=str(tumbler),
            dt_uuid=m.dt_uuid, score=m.score, chunks=len(m.chunks),
        )
    return docs, links


def register_synthetic(
    catalog: "Catalog",
    owner: "Tumbler",
    collection: str,
    groups: list[TitleGroup],
) -> tuple[int, int]:
    """Register one Document per group with synthetic
    ``nx-orphan-backfill://`` URI; write document_chunks rows.

    For groups lacking a title, falls back to chash-grouping: each
    distinct chash gets its own minimal Document so manifest rows can
    still be written. The synthetic URI scheme is intentionally clear
    so search consumers can distinguish residual data from authoritative
    provenance."""
    docs = 0
    links = 0
    for g in groups:
        if g.title:
            uri = f"nx-orphan-backfill://{collection}/{g.title}"
            title = g.title
            # nexus-w5zv: stable sort by chunk_index (same rationale as
            # register_dt_linked above).
            ordered_chunks = sorted(g.chunks, key=lambda c: c.chunk_index)
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
                content_type="pdf",
                file_path="",
                physical_collection=collection,
                chunk_count=len(g.chunks),
                source_uri=uri,
                meta={
                    "backfill_from": "t3_orphan",
                    "backfill_mode": "synthetic",
                },
            )
            docs += 1
            catalog.write_manifest(str(tumbler), chunks_payload)
            links += len(chunks_payload)
        else:
            # No title; bucket each chunk under its chash as a singleton doc.
            for c in g.chunks:
                title = f"orphan-chunk-{c.chash[:12]}"
                uri = f"nx-orphan-backfill://{collection}/chash/{c.chash}"
                tumbler = catalog.register(
                    owner,
                    title=title,
                    content_type="pdf",
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
                }])
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

    with matched_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["title", "dt_uuid", "dt_name", "score", "chunk_count"])
        for m in matched:
            w.writerow([
                m.title, m.dt_uuid, m.dt_name,
                f"{m.score:.3f}", len(m.chunks),
            ])
    with low_path.open("w", newline="") as f:
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
    with unmatched_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["title", "chunk_count", "operator_dt_uuid"])
        for g in unmatched:
            w.writerow([g.title, len(g.chunks), ""])
    return matched_path, low_path, unmatched_path


def apply_csv(
    catalog: "Catalog",
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
    with csv_path.open() as f:
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
                content_type="pdf",
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
            ])
            links += len(chunks)
    return docs, links


# ── Link T3 chunks to EXISTING catalog Documents ─────────────────────────────


def link_by_title(
    catalog: "Catalog",
    collection: str,
    groups: list[TitleGroup],
) -> tuple[int, int, list[TitleGroup]]:
    """For each titled group, find an existing catalog Document with the
    same title in ``collection`` and append manifest rows linking the
    group's chunks to that Document.

    Returns ``(linked_chunks, linked_docs, unlinked_groups)``.
    ``unlinked_groups`` are groups whose title has no catalog match;
    callers route them to synthetic mode.

    Untitled groups are passed through to ``unlinked_groups`` so
    callers can branch them to chash fallback.
    """
    rows = catalog._db.execute(
        "SELECT tumbler, title FROM documents "
        "WHERE physical_collection = ? AND title != ''",
        (collection,),
    ).fetchall()
    by_title: dict[str, str] = {r[1]: r[0] for r in rows}
    linked_chunks = 0
    linked_docs = 0
    unlinked: list[TitleGroup] = []
    for g in groups:
        if not g.title or g.title not in by_title:
            unlinked.append(g)
            continue
        tumbler = by_title[g.title]
        # Read existing manifest to know where to append (preserve order).
        existing = catalog.get_manifest(tumbler)
        start_pos = len(existing)
        # nexus-w5zv: stable sort by chunk_index (see register_dt_linked).
        ordered_chunks = sorted(g.chunks, key=lambda c: c.chunk_index)
        catalog.append_manifest_chunks(tumbler, [
            {
                "chash": c.chash,
                "position": start_pos + pos,
                "line_start": None, "line_end": None,
                "char_start": None, "char_end": None,
            }
            for pos, c in enumerate(ordered_chunks)
        ])
        linked_chunks += len(g.chunks)
        linked_docs += 1
        _log.info(
            "orphan_backfill_linked_by_title",
            collection=collection, tumbler=tumbler,
            title=g.title, chunks=len(g.chunks),
        )
    return linked_chunks, linked_docs, unlinked


def link_by_content_hash(
    catalog: "Catalog",
    t3_db,
    collection: str,
) -> tuple[int, int, int]:
    """For each existing catalog Document with non-empty ``head_hash``,
    find T3 chunks in ``collection`` whose ``content_hash`` matches and
    append manifest rows.

    Returns ``(linked_chunks, linked_docs, unmatched_chunks)``.

    Used when T3 chunks lack ``title`` metadata but have
    ``content_hash`` (PDF-shaped chunks), and the catalog has
    Documents with ``head_hash`` populated. The two hashes are the
    same content-addressed identity so matching is exact.
    """
    rows = catalog._db.execute(
        "SELECT tumbler, head_hash FROM documents "
        "WHERE physical_collection = ? AND head_hash != ''",
        (collection,),
    ).fetchall()
    by_head: dict[str, str] = {r[1]: r[0] for r in rows}
    if not by_head:
        return 0, 0, 0

    col = t3_db.get_collection(collection)
    n = col.count()
    # Group chunks by content_hash so we batch one append_manifest per doc.
    by_hash: dict[str, list[ChunkRef]] = defaultdict(list)
    unmatched = 0
    offset = 0
    while offset < n:
        batch = col.get(limit=_PAGE, offset=offset, include=["metadatas"])
        cids = batch.get("ids") or []
        metas = batch.get("metadatas") or []
        for cid, meta in zip(cids, metas):
            if not isinstance(meta, dict):
                meta = {}
            content_hash = str(meta.get("content_hash") or "")
            chash = str(meta.get("chunk_text_hash") or cid[:32])
            chunk_index = int(meta.get("chunk_index", 0) or 0)
            if content_hash and content_hash in by_head:
                by_hash[content_hash].append(ChunkRef(
                    cid=cid, chash=chash, chunk_index=chunk_index,
                ))
            else:
                unmatched += 1
        offset += _PAGE

    linked_chunks = 0
    linked_docs = 0
    for content_hash, chunks in by_hash.items():
        tumbler = by_head[content_hash]
        existing = catalog.get_manifest(tumbler)
        start_pos = len(existing)
        # nexus-w5zv: stable sort by chunk_index (see register_dt_linked).
        ordered_chunks = sorted(chunks, key=lambda c: c.chunk_index)
        catalog.append_manifest_chunks(tumbler, [
            {
                "chash": c.chash,
                "position": start_pos + pos,
                "line_start": None, "line_end": None,
                "char_start": None, "char_end": None,
            }
            for pos, c in enumerate(ordered_chunks)
        ])
        linked_chunks += len(chunks)
        linked_docs += 1
        _log.info(
            "orphan_backfill_linked_by_content_hash",
            collection=collection, tumbler=tumbler,
            content_hash=content_hash[:16], chunks=len(chunks),
        )
    return linked_chunks, linked_docs, unmatched
