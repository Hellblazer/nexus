# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Aspect-extras → fixed-column promotion (RDR-089 Phase E).

The ``document_aspects.extras`` JSON column is the extensibility
anchor: extractors can stash per-domain fields without a schema
migration. When a field has matured (consistently extracted, queried
often, stable shape across runs), it graduates to its own column.
This module ships the mechanic — the policy (which fields graduate,
who decides, what marks the version bump) is governance, not code,
and is left to the operations RDR.

The promotion mechanic has three phases per field:

1. **Schema add**: ``ALTER TABLE document_aspects ADD COLUMN
   <field> <type>``. Idempotent (no-op when column exists).

2. **Backfill**: ``UPDATE document_aspects SET <field> =
   json_extract(extras, '$.<field>') WHERE <field> IS NULL AND
   json_extract(extras, '$.<field>') IS NOT NULL``. Copies existing
   data out of ``extras`` into the new column, leaving ``extras``
   untouched (so dual-read works across the cutover).

3. **Extras prune** (optional): ``UPDATE document_aspects SET
   extras = json_remove(extras, '$.<field>')``. Removes the now-
   redundant key from extras so future readers always go to the
   typed column. Only run after every reader has been updated to
   consume the typed column. Default is to skip; callers opt in
   with ``prune=True``.

Public API:

    promote_extras_field(
        db: T2Database,
        field_name: str,
        sql_type: str = "TEXT",
        prune: bool = False,
    ) -> PromotionResult

The CLI wrapper ``nx enrich aspects-promote-field <name> [--type
TYPE] [--prune]`` exposes this for operators. Each call is logged
to T2 ``aspect_promotion_log`` with the timestamp + field name +
prune flag, so the field's promotion history is auditable.

Phase 1's reserved-column set covers the five RDR-locked aspect
columns. Promotion of those names is rejected with a clear error
message (they are already typed columns; calling promote on them
is a caller-side bug).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

_log = structlog.get_logger(__name__)


# RDR-locked aspect columns — promotion is a no-op for these because
# they already exist as typed columns. Fail fast rather than emit a
# confusing "column already exists" message.
_RESERVED = frozenset({
    "collection", "source_path",
    "problem_formulation", "proposed_method",
    "experimental_datasets", "experimental_baselines",
    "experimental_results",
    "extras", "confidence", "extracted_at",
    "model_version", "extractor_name",
})


# Allowed SQLite column types for promotion. SQLite is dynamically
# typed but we keep the set narrow so the promoted columns remain
# queryable with predictable semantics.
_ALLOWED_TYPES = frozenset({"TEXT", "INTEGER", "REAL"})


@dataclass(frozen=True)
class PromotionResult:
    """Audit summary of a single promotion run.

    Returned by ``promote_extras_field`` and recorded in T2
    ``aspect_promotion_log`` for replay / triage.
    """

    field_name: str
    sql_type: str
    column_added: bool       # True if ALTER TABLE ran
    rows_backfilled: int     # rows whose typed column was set from extras
    rows_pruned: int         # rows whose extras key was removed (0 unless prune=True)
    pruned: bool             # echoes the caller's prune flag
    promoted_at: str         # ISO-8601 UTC timestamp


def promote_extras_field(
    db,  # T2Database (forward-typed via duck typing to avoid import cycle)
    field_name: str,
    *,
    sql_type: str = "TEXT",
    prune: bool = False,
) -> PromotionResult:
    """Promote ``extras['<field_name>']`` to its own typed column.

    Validates ``field_name`` against the reserved set and against
    SQL identifier safety (alpha-numeric + underscore only — no
    quoting / injection vector). Validates ``sql_type`` against
    the allowed types.

    Idempotent: re-running on a field that has already been
    promoted is a no-op (column already exists, no rows to
    backfill from extras since extras has been pruned or the
    column has been the truth-source). Re-running with
    ``prune=True`` after a non-pruning earlier run does the prune
    pass without touching the column.

    Logs the event to T2 ``aspect_promotion_log``; the promotion
    history of any field is queryable via that table.
    """
    _validate_field_name(field_name)
    sql_type = sql_type.upper()
    if sql_type not in _ALLOWED_TYPES:
        raise ValueError(
            f"sql_type must be one of {sorted(_ALLOWED_TYPES)}; "
            f"got {sql_type!r}",
        )

    now = datetime.now(UTC).isoformat()

    # RDR-112 P0-gate (nexus-mz2c): operations routed through public
    # DocumentAspects methods (alter_add_column_if_missing /
    # backfill_extras_column / prune_extras_key) so daemon-mode (Phase 1)
    # can swap the store without touching this admin path.
    column_added = db.document_aspects.alter_add_column_if_missing(
        field_name=field_name, sql_type=sql_type,
    )
    if column_added:
        _log.info(
            "aspect_promotion_column_added",
            field=field_name, sql_type=sql_type,
        )

    # Backfill from extras → typed column. Only update rows where the
    # typed column is currently NULL AND the extras key is set —
    # avoids overwriting any value an earlier extractor wrote directly.
    rows_backfilled = db.document_aspects.backfill_extras_column(
        field_name=field_name,
    )

    rows_pruned = 0
    if prune:
        rows_pruned = db.document_aspects.prune_extras_key(
            field_name=field_name,
        )

    result = PromotionResult(
        field_name=field_name,
        sql_type=sql_type,
        column_added=column_added,
        rows_backfilled=rows_backfilled,
        rows_pruned=rows_pruned,
        pruned=prune,
        promoted_at=now,
    )

    _record_promotion_audit(db, result)
    return result


def list_promotions(db) -> list[dict]:
    """Return the full ``aspect_promotion_log`` history, oldest first.

    Routed through :meth:`DocumentAspects.list_promotion_audit` per
    RDR-112 P0-gate (nexus-mz2c).
    """
    return db.document_aspects.list_promotion_audit()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _validate_field_name(name: str) -> None:
    if not name:
        raise ValueError("field_name must not be empty")
    if name in _RESERVED:
        raise ValueError(
            f"field_name {name!r} is a reserved aspect column; "
            f"promotion is a no-op for fields that are already typed"
        )
    if not _is_safe_identifier(name):
        raise ValueError(
            f"field_name {name!r} contains characters outside the "
            f"safe identifier set (alphanumeric + underscore, must "
            f"start with a letter or underscore)"
        )


def _is_safe_identifier(name: str) -> bool:
    """SQL identifier safety: alphanumeric + underscore only, must
    start with letter or underscore. We validate rather than quote
    because SQLite identifier quoting (``"name"``) interacts oddly
    with ALTER TABLE in some versions; the strict allowlist is the
    safer surface for an operations CLI."""
    if not name:
        return False
    if not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in name)


def _record_promotion_audit(db, result: PromotionResult) -> None:
    """Insert a row into the audit log. Best-effort: persistence
    failure logs at debug level and is otherwise swallowed."""
    try:
        db.document_aspects.record_promotion_audit(
            field_name=result.field_name,
            sql_type=result.sql_type,
            column_added=result.column_added,
            rows_backfilled=result.rows_backfilled,
            rows_pruned=result.rows_pruned,
            pruned=result.pruned,
            promoted_at=result.promoted_at,
        )
    except Exception:
        _log.debug("aspect_promotion_audit_failed", exc_info=True)
