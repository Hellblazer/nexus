"""Chunk list of the ``nx_answer`` structured envelope (GH #1667).

The envelope's ``chunks`` list was every retrieved id of every retrieval step,
repeated per step, with no bound: a six-step plan returned a 96k-character
reply, mostly this list. ``harvest_chunks`` builds it from the step outputs;
``cap_chunks`` deduplicates by id (first occurrence wins, so the earliest
step's rank survives) and caps it at ``MAX_ENVELOPE_CHUNKS``, reporting how
many distinct ids the cap dropped (the envelope's ``chunks_truncated``).
"""
from __future__ import annotations

from typing import Any, Iterable

#: Most chunk refs the structured envelope carries (GH #1667).
MAX_ENVELOPE_CHUNKS: int = 50


def harvest_chunks(steps: Iterable[Any]) -> list[dict]:
    """Chunk refs (id, chash, collection, distance) from every retrieval-op step.

    Steps are taken in order and ids in rank order; nothing is deduplicated or
    capped here (see ``cap_chunks``). A chunk's collection is its own entry of
    the step's ``chunk_collections`` (RDR-086 review #7), else the step's first
    deduplicated collection.
    """
    out: list[dict] = []
    for step_out in steps:
        if not isinstance(step_out, dict):
            continue
        ids = step_out.get("ids")
        if not isinstance(ids, list) or not ids:
            continue
        hashes = step_out.get("chunk_text_hash", []) or []
        per_chunk_colls = step_out.get("chunk_collections") or []
        dedup_colls = step_out.get("collections", []) or []
        dists = step_out.get("distances", []) or []
        default_coll = dedup_colls[0] if dedup_colls else ""
        for i, cid in enumerate(ids):
            out.append({
                "id": cid,
                "chash": hashes[i] if i < len(hashes) else "",
                "collection": per_chunk_colls[i] if i < len(per_chunk_colls) else default_coll,
                "distance": dists[i] if i < len(dists) else None,
            })
    return out


def cap_chunks(
    chunks: "list[dict] | None", limit: int = MAX_ENVELOPE_CHUNKS,
) -> tuple[list[dict], int]:
    """Deduplicate ``chunks`` by ``id`` (first wins, order kept) and cap at ``limit``.

    Returns ``(kept, truncated)``: ``truncated`` counts the distinct ids the cap
    dropped; duplicates removed by the dedupe are not counted. ``None`` and an
    empty list give ``([], 0)``. An entry with no usable id (no ``id`` key, ``None``, an
    unhashable value, or not a dict) is never treated as a duplicate: it is kept, in order,
    and counts toward the cap.
    """
    seen: set[Any] = set()
    distinct: list[dict] = []
    for chunk in chunks or []:
        cid = chunk.get("id") if isinstance(chunk, dict) else None
        try:
            dup = cid is not None and cid in seen
        except TypeError:
            dup, cid = False, None
        if dup:
            continue
        if cid is not None:
            seen.add(cid)
        distinct.append(chunk)
    return distinct[:limit], max(0, len(distinct) - limit)
