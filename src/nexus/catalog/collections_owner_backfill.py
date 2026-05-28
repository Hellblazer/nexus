# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 Phase 1.5a: backfill ``collections.owner_id`` (nexus-tts0d.1).

The ``collections`` projection table (RDR-103) carries an ``owner_id``
column that any catalog-backed reader joins against
``owners.tumbler_prefix`` to resolve repo → collection. Legacy rows
synthesised by the RDR-108 auto-backfill at ``CatalogStore.__init__``
land with ``owner_id=''``, making the join return nothing — the root
of the RDR-137 phantom-collection symptom.

This module exposes :func:`backfill_owner_id`, a self-contained pass
that populates owner_id from two complementary sources:

1. **Conformant collection name** (auto, idempotent, always safe).
   RDR-103 names have shape ``<content_type>__<owner_id>__<model>__v<n>``;
   the 2nd segment IS the owner_id. Parsing failures fall through.

2. **Documents-table fallback** (opt-in, CLI-only). For rows whose
   name cannot be parsed (legacy 2-segment names like
   ``knowledge__delos``), look up documents registered against the
   collection and derive owner_id from the document tumblers via
   :func:`nexus.catalog.collection_name.owner_segment_for_tumbler`
   (the canonical hyphen form ``1.7.42 -> 1-7`` — matches the format
   queried by ``Catalog.collection_for``). Multiple distinct owners
   → ambiguous, row left empty for operator review.

The auto-migration in ``CatalogStore.__init__`` invokes this with
``include_documents_fallback=False`` so the safe path runs on every
DB open. The CLI verb ``nx catalog backfill-owner-id`` calls it with
the fallback enabled so legacy names also get covered after operator
review.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import structlog

from nexus.catalog.collection_name import owner_segment_for_tumbler

_log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """Counts emitted by :func:`backfill_owner_id`.

    Sum of ``updated_from_name + updated_from_documents +
    skipped_ambiguous + skipped_unresolvable`` equals
    ``total_empty`` — every row with ``owner_id=''`` at entry is
    accounted for.
    """

    total_empty: int
    updated_from_name: int
    updated_from_documents: int
    skipped_ambiguous: int
    skipped_unresolvable: int


def backfill_owner_id(
    conn: sqlite3.Connection,
    *,
    include_documents_fallback: bool = False,
    dry_run: bool = False,
) -> BackfillResult:
    """Populate ``collections.owner_id`` for empty rows.

    Iterates ``collections WHERE owner_id = ''`` and updates each
    row's owner_id via the conformant-name path. When
    ``include_documents_fallback`` is true, rows that the name path
    cannot resolve get a second attempt via the documents-table
    inference.

    Idempotent: rows with non-empty owner_id are not re-examined.
    Re-running on a fully-backfilled DB returns zero counts.

    ``dry_run`` suppresses the UPDATE statements but still emits the
    result counters so an operator can preview the change set.
    """
    from nexus.corpus import parse_conformant_collection_name  # noqa: PLC0415

    rows: list[tuple[str]] = conn.execute(
        "SELECT name FROM collections WHERE owner_id = ''"
    ).fetchall()
    total = len(rows)
    updated_from_name = 0
    updated_from_documents = 0
    skipped_ambiguous = 0
    skipped_unresolvable = 0

    remaining: list[str] = []

    # Pass 1 — conformant name.
    for (name,) in rows:
        try:
            parsed = parse_conformant_collection_name(name)
        except (ValueError, KeyError):
            remaining.append(name)
            continue
        owner_id = parsed.get("owner_id", "")
        if not owner_id:
            remaining.append(name)
            continue
        if not dry_run:
            conn.execute(
                "UPDATE collections SET owner_id = ? WHERE name = ?",
                (owner_id, name),
            )
        updated_from_name += 1
        _log.info(
            "collections_owner_backfill_from_name",
            name=name, owner_id=owner_id, dry_run=dry_run,
        )

    # Pass 2 — documents-table fallback (opt-in).
    if include_documents_fallback:
        for name in remaining:
            owners = {
                owner_segment_for_tumbler(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT tumbler FROM documents "
                    "WHERE physical_collection = ?",
                    (name,),
                ).fetchall()
            }
            # Strip empty owner prefixes (malformed tumblers); they
            # cannot identify an owner and should not be counted as
            # candidates.
            owners.discard("")
            if not owners:
                skipped_unresolvable += 1
                _log.warning(
                    "collections_owner_backfill_no_documents",
                    name=name,
                )
                continue
            if len(owners) > 1:
                skipped_ambiguous += 1
                _log.warning(
                    "collections_owner_backfill_ambiguous_multi_owner",
                    name=name, candidates=sorted(owners),
                )
                continue
            owner_id = next(iter(owners))
            if not dry_run:
                conn.execute(
                    "UPDATE collections SET owner_id = ? WHERE name = ?",
                    (owner_id, name),
                )
            updated_from_documents += 1
            _log.info(
                "collections_owner_backfill_from_documents",
                name=name, owner_id=owner_id, dry_run=dry_run,
            )
    else:
        skipped_unresolvable += len(remaining)

    return BackfillResult(
        total_empty=total,
        updated_from_name=updated_from_name,
        updated_from_documents=updated_from_documents,
        skipped_ambiguous=skipped_ambiguous,
        skipped_unresolvable=skipped_unresolvable,
    )
