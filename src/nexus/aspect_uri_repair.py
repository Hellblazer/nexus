# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Repair planner for ``document_aspects.source_uri``.

``source_uri`` is the key the ``aspect_sql`` operators look a row up by:
``operator_filter`` / ``operator_groupby`` / ``operator_confidence_aggregate``
re-derive it with :func:`nexus.aspect_readers.uri_for` and send the URI list to
the engine, which matches it byte-equal. A row stored WITHOUT one therefore
matches nothing — and the operator reports that miss as ``"does not match"``,
a content verdict rather than an error, so the row is silently absent from
every analytic answer instead of loudly missing.

The rows needing repair were written by ``_build_record_from_entry``, the batch
aspect builder, which omitted the ``source_uri`` kwarg that both single-doc
builders pass. Only its happy path was affected: its schema-failure branch
returns ``_empty_record``, which did mint the URI, so a batch whose entries all
validated produced unattributed rows while a batch that failed validation
produced attributed ones — which is why affected collections are mixed rather
than uniformly empty.

Measured on this box 2026-09-20, before the writer fix: 481 of 658 rows across
the 24 knowledge collections carried an empty ``source_uri``, and a controlled
probe (same collection, same field, criterion ``WSL2``) returned only the rows
that happened to have one, reporting two rows whose ``problem_formulation``
begins "How to reliably keep a WSL2 distro alive" as not matching ``WSL2``.

This module is the PLANNER only — it performs no writes. ``nx enrich
aspects-backfill-uri`` applies the plan. Keeping the decision pure is what lets
the refusal rules be tested without a substrate.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.db.t2.records import AspectRecord

#: The engine's own upsert gate, mirrored so the planner can refuse a row the
#: store would silently drop. ``AspectRepository.MIN_CONFIDENCE`` (0.3) is the
#: authority; a missing or null confidence is coerced to -1.0 there and fails
#: the same comparison. The Java constant's comment still cites a Python
#: ``_MIN_CONFIDENCE`` that no longer exists — it died with the SQLite store —
#: so the engine is the only live definition.
MIN_CONFIDENCE: float = 0.3


@dataclass
class BackfillPlan:
    """What a repair run would do, decided before anything is written.

    The three outcome buckets partition the input exactly:
    ``len(updates) + already_attributed + len(refusals) == total``. A planner
    that dropped a row instead would report a clean small number and leave the
    rest unrepaired with nothing saying so.
    """

    total: int = 0
    already_attributed: int = 0
    updates: list[tuple[AspectRecord, str]] = field(default_factory=list)
    refusals: list[tuple[AspectRecord, str]] = field(default_factory=list)


def plan_source_uri_backfill(rows: list[AspectRecord]) -> BackfillPlan:
    """Decide the repair for every row in *rows*, writing nothing.

    A row is an update when it has no ``source_uri`` and ``uri_for`` mints one
    for its ``(collection, source_path)``. The repaired record is a copy with
    only that field changed, because ``upsert`` is a whole-row overwrite.

    Refusals, each reported by name rather than skipped:

    * ``uri_for`` returned nothing — an empty ``source_path``, or a relative
      one in a file-routed collection with no ``repo_root`` to anchor it.
      ``uri_for`` refuses to guess there (nexus-yg70j) and this inherits the
      refusal; minting something anyway would collapse every such row under
      one key.
    * ``confidence`` below :data:`MIN_CONFIDENCE` (or absent) — the engine's
      upsert drops these and returns -1, so planning them as updates would
      report a repair count the store never performed.
    * blank ``doc_id`` — ``upsert`` raises on it (hygiene-001), so refusing
      here keeps ``--apply`` from dying part-way through a run.
    """
    from nexus.aspect_readers import uri_for  # noqa: PLC0415 — deferred to avoid a circular import (aspect_readers is a leaf)

    plan = BackfillPlan(total=len(rows))
    for row in rows:
        if row.source_uri:
            plan.already_attributed += 1
            continue

        new_uri = uri_for(row.collection, row.source_path)
        if not new_uri:
            plan.refusals.append((
                row,
                "uri_for minted nothing for this source_path "
                f"({row.source_path!r}): empty, or relative with no repo_root",
            ))
            continue

        confidence = row.confidence
        if confidence is None or confidence < MIN_CONFIDENCE:
            plan.refusals.append((
                row,
                f"confidence {confidence!r} is below the engine's upsert gate "
                f"({MIN_CONFIDENCE}); the write would be dropped silently",
            ))
            continue

        if not row.doc_id:
            plan.refusals.append((
                row, "blank doc_id; upsert refuses an unattributable row",
            ))
            continue

        plan.updates.append((dataclasses.replace(row, source_uri=new_uri), new_uri))

    return plan
