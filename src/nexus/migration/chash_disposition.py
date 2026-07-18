# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-180 Item8 (nexus-jxizy.4): null/orphaned ``chunk_text`` disposition.

The classification half of the Item6 full-digest rekey: every chunk row
gets exactly ONE of three dispositions, in priority order, keyed off the
union old→new content map built from ALL content-bearing rows across every
dim/collection:

1. **Rehashable** — non-empty ``chunk_text``: new key =
   ``sha256(chunk_text)`` (full digest). 100% of the current corpus (A-1).
2. **Reference-only, recoverable** — null/empty text BUT the ``old_chash``
   appears with content on some other row: remapped to the sibling's new
   key. Never dropped, never synthesized — genuine cross-collection
   references are preserved.
3. **Orphaned** — null/empty text AND no content-bearing source anywhere:
   the per-run ``orphan_policy`` decides — ``drop`` (default) or
   ``synthesize`` (deterministic surrogate, honestly flagged).

Defined REGARDLESS of the current zero residue, precisely so a
text-sparse tenant does not surface it as unplanned work mid-migration
(RDR-180 Scope Verification).

Pure policy by design: the Item6 ETL writer (nexus-jxizy.6) executes the
results — including the ``drop`` pointer-cascade (manifest + chash_index
rows die in the same transaction, else dangling pointers) and the
``metadata->>'chash_origin' = 'synthetic'`` stamp on synthesized rows
(the honest signal; the bytes are indistinguishable from a content
digest by construction).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

import structlog

_log = structlog.get_logger(__name__)

#: Version-pinned domain-separation prefix for synthesized surrogate keys.
SYNTHETIC_CHASH_PREFIX = "nexus:synthetic-chash:v1|"


class OrphanPolicy(StrEnum):
    """Per-run ETL flag for disposition 3 (default ``drop``)."""

    DROP = "drop"
    SYNTHESIZE = "synthesize"


class Disposition(StrEnum):
    REHASHED = "rehashed"
    REMAPPED = "remapped"
    DROPPED = "dropped"
    SYNTHESIZED = "synthesized"


@dataclass(frozen=True)
class ChunkRecord:
    """The minimal per-row shape the classifier needs."""

    old_chash: str
    chunk_text: str | None
    tenant_id: str
    collection: str


@dataclass(frozen=True)
class DispositionResult:
    """One row's verdict. ``new_chash_hex`` is the FULL 64-hex digest
    (interchange form; the writer converts to storage bytes at the
    boundary) — ``None`` only for :attr:`Disposition.DROPPED`, whose
    writer must ALSO cascade the row's manifest/chash_index pointers."""

    old_chash: str
    tenant_id: str
    collection: str
    disposition: Disposition
    new_chash_hex: str | None
    synthetic: bool = False


@dataclass(frozen=True)
class DispositionCounts:
    """The auditable per-run tally — logged, never silent."""

    rehashed: int = 0
    remapped: int = 0
    dropped: int = 0
    synthesized: int = 0


def full_digest_hex(text: str) -> str:
    """The canonical new key for *text*: full SHA-256, 64-hex interchange."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def synthetic_chash_hex(tenant_id: str, collection: str, old_chash: str) -> str:
    """Deterministic 32-byte surrogate for an orphan whose pointer must
    survive: ``sha256("nexus:synthetic-chash:v1|" + tenant + "|" +
    collection + "|" + old_chash)``. Uniqueness holds by construction; the
    honest not-a-content-address signal is the writer's metadata flag."""
    seed = f"{SYNTHETIC_CHASH_PREFIX}{tenant_id}|{collection}|{old_chash}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def build_content_map(rows: list[ChunkRecord]) -> dict[str, str]:
    """The union old→new map over every content-bearing row.

    Raises ``ValueError`` when two different texts claim the same
    ``old_chash`` — that is corpus corruption (or a realized 128-bit
    collision, the class this RDR eliminates); never pick one silently
    (GH #1390: correct addresses only).
    """
    cmap: dict[str, str] = {}
    for row in rows:
        if not row.chunk_text:
            continue
        new_hex = full_digest_hex(row.chunk_text)
        prior = cmap.get(row.old_chash)
        if prior is not None and prior != new_hex:
            raise ValueError(
                f"conflicting content for old_chash {row.old_chash!r}: two "
                "distinct texts claim one legacy id — corpus corruption or a "
                "realized 128-bit collision; refusing to pick one silently"
            )
        cmap[row.old_chash] = new_hex
    return cmap


def classify(
    rows: list[ChunkRecord],
    content_map: dict[str, str],
    *,
    orphan_policy: OrphanPolicy = OrphanPolicy.DROP,
) -> tuple[list[DispositionResult], DispositionCounts]:
    """Apply the three-way policy to *rows* against *content_map*.

    Returns one result per input row (order preserved) plus the counts.
    The counts are the audit trail — the caller logs them per tenant so a
    run against a text-sparse tenant is auditable, not silently lossy.
    """
    results: list[DispositionResult] = []
    rehashed = remapped = dropped = synthesized = 0
    for row in rows:
        if row.chunk_text:
            rehashed += 1
            results.append(DispositionResult(
                old_chash=row.old_chash,
                tenant_id=row.tenant_id,
                collection=row.collection,
                disposition=Disposition.REHASHED,
                new_chash_hex=full_digest_hex(row.chunk_text),
            ))
        elif (sibling := content_map.get(row.old_chash)) is not None:
            remapped += 1
            results.append(DispositionResult(
                old_chash=row.old_chash,
                tenant_id=row.tenant_id,
                collection=row.collection,
                disposition=Disposition.REMAPPED,
                new_chash_hex=sibling,
            ))
        elif orphan_policy is OrphanPolicy.SYNTHESIZE:
            synthesized += 1
            results.append(DispositionResult(
                old_chash=row.old_chash,
                tenant_id=row.tenant_id,
                collection=row.collection,
                disposition=Disposition.SYNTHESIZED,
                new_chash_hex=synthetic_chash_hex(
                    row.tenant_id, row.collection, row.old_chash
                ),
                synthetic=True,
            ))
        else:
            dropped += 1
            results.append(DispositionResult(
                old_chash=row.old_chash,
                tenant_id=row.tenant_id,
                collection=row.collection,
                disposition=Disposition.DROPPED,
                new_chash_hex=None,
            ))
    counts = DispositionCounts(
        rehashed=rehashed, remapped=remapped,
        dropped=dropped, synthesized=synthesized,
    )
    _log.info(
        "chash_disposition_classified",
        rows=len(rows),
        rehashed=counts.rehashed,
        remapped=counts.remapped,
        dropped=counts.dropped,
        synthesized=counts.synthesized,
        orphan_policy=str(orphan_policy),
    )
    return results, counts
