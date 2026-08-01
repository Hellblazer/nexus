# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Aspect-extras → fixed-column promotion: the audit-log read path.

The ``document_aspects.extras`` JSON column is the extensibility
anchor: extractors can stash per-domain fields without a schema
migration. When a field has matured (consistently extracted, queried
often, stable shape across runs), it graduates to its own column.

**The runtime promotion verb is RETIRED (Hal decision 2026-07-25,
bead nexus-70x7y).** RDR-089 Phase E shipped ``promote_extras_field()``
as a runtime add-column + backfill against SQLite. That premise —
operator-driven schema evolution without a migration — does not survive
the move to Postgres: ALL DDL goes through Liquibase (directive of
record, T2 ``nexus/directive-no-sqlite-pg-everywhere``). Runtime schema
DDL in either substrate is now a review Critical, so the verb has no
valid implementation to port. It is deleted rather than
reimplemented as a service endpoint, because the changeset that must
exist anyway can already do every phase inline.

Promoting a field is now ONE Liquibase changeset with three statements:

.. code-block:: xml

    <changeSet id="aspects-0NN-1" author="<bead-id>">
      <comment>Promote extras.venue to a typed column</comment>
      <sql splitStatements="true">
    ALTER TABLE nexus.document_aspects ADD COLUMN venue TEXT;

    UPDATE nexus.document_aspects
       SET venue = extras->>'venue'
     WHERE venue IS NULL
       AND extras ? 'venue';

    INSERT INTO nexus.aspect_promotion_log
           (tenant_id, field_name, sql_type, column_added,
            rows_backfilled, rows_pruned, pruned, promoted_at)
    SELECT tenant_id, 'venue', 'TEXT', 1, count(*), 0, 0, now()
      FROM nexus.document_aspects
     WHERE venue IS NOT NULL
     GROUP BY tenant_id;
      </sql>
    </changeSet>

The optional extras prune (``UPDATE ... SET extras = extras - 'venue'``)
is a SEPARATE, later changeset: run it only once every reader consumes
the typed column, so the dual-read cutover the original mechanic
supported is preserved. Record it with ``pruned = 1``.

The audit log itself is NOT retired. ``nexus.aspect_promotion_log`` is
the queryable history of which fields graduated when, and the changeset
above is what appends to it. This module keeps the read path:

    list_promotions(db: T2Database) -> list[dict]

exposed to operators as ``nx enrich aspects-promote-field --history``.
It is backend-agnostic: the SQLite store reads its local table and the
service store reads ``GET /v1/aspects/promotion/list``. Neither creates
the table on read — the sanctioned creators are
``migrations.migrate_aspect_promotion_log_table`` (SQLite) and
``aspects-001-baseline.xml`` (PG), so an install that never promoted a
field simply has an empty log.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.db.t2 import T2Database


#: Message shown when an operator invokes the retired promotion verb.
#: Kept here (not in the CLI) so the substrate owns the recipe and the
#: CLI stays a thin echo of it.
PROMOTION_RETIRED = (
    "Runtime extras->column promotion is retired (nexus-70x7y). Schema "
    "changes go through Liquibase in every mode, so there is no runtime "
    "schema statement to run.\n"
    "\n"
    "Promote {field!r} with ONE changeset under "
    "service/src/main/resources/db/changelog/ that does all three phases:\n"
    "  1. ALTER TABLE nexus.document_aspects ADD COLUMN {field} <TYPE>;\n"
    "  2. UPDATE nexus.document_aspects SET {field} = extras->>'{field}'\n"
    "      WHERE {field} IS NULL AND extras ? '{field}';\n"
    "  3. INSERT the promotion event into nexus.aspect_promotion_log\n"
    "      so --history still shows it.\n"
    "\n"
    "Prune extras in a SEPARATE later changeset, once every reader "
    "consumes the typed column. See src/nexus/aspect_promotion.py for the "
    "full changeset template."
)


def list_promotions(db: T2Database) -> list[dict]:
    """Return the ``aspect_promotion_log`` history, oldest first.

    Each entry is a dict with keys ``field_name``, ``sql_type``,
    ``column_added``, ``rows_backfilled``, ``rows_pruned``, ``pruned``,
    ``promoted_at``. Used by ``nx enrich aspects-promote-field --history``
    and by anyone auditing the aspect schema's evolution.

    Works on both substrates — the call is delegated to whichever
    ``document_aspects`` store is configured, so no caller reaches into a
    raw connection. Returns ``[]`` when nothing has ever been promoted.
    """
    return db.document_aspects.list_promotions()
