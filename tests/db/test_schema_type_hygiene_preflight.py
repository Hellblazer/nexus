# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-vacuous type-state check for the schema type-hygiene arc (nexus-cefa1,
P0-P5; plan of record T2 nexus/plan-schema-type-hygiene-2026-08-12 [22360]).

ARC COMPLETE as of nexus-cefa1.6 (P5, taxonomy-008-link-types-jsonb.xml):
all 10 audited columns (4 Tier A TEXT-to-timestamptz/boolean, 6 Tier B
TEXT-to-jsonb) have shipped their conversions. topic_links.link_types was
the LAST still-TEXT column; its conversion retired this file's
malformed-seed generation machinery entirely (see history below), leaving
``test_column_types_match_migration_state`` as the sole ongoing,
non-vacuous check: a real ``information_schema.columns.data_type`` query
per column, asserted against its final converted type. There is no longer
a "some columns are still text" branch to probe with seeded malformed
rows -- adding a fabricated "prove ran_any_tier is False" assertion here
would be exactly the vacuous check the arc's own design (see HISTORY
below) was built to avoid, so none was added. Should a future Tier C (or a
re-opened, rolled-back column) reintroduce a still-TEXT audited column,
its own malformed-seed probe + non-vacuity test should be authored fresh
against that column's actual failure modes, not resurrected from history
here -- prior probe SQL files remain on disk (see HISTORY) as reference
for that shape.

HISTORY (why this file no longer runs malformed-seed probes): originally
(nexus-cefa1.1/.2) a single combined probe assumed all 10 columns were
still TEXT; that assumption broke the moment the first phase converted a
column, so the probe SQL was split per phase as each shipped:
``schema_type_hygiene_preflight_tier_a.sql`` (Tier A, all 4 columns
converted together by ONE changeset, catalog-031-type-hygiene.xml,
nexus-cefa1.2) and ``..._tier_b*.sql`` (Tier B, converted across FIVE
separate phases -- P2 hook_failures.batch_doc_ids
(telemetry-004-type-hygiene.xml, nexus-cefa1.3), split into
``..._tier_b_hook_failures.sql``; P3 document_aspects.extras/
.salient_sentences (aspects-003-type-hygiene.xml, nexus-cefa1.4), split
into ``..._tier_b_document_aspects.sql``; P4 plans.plan_json/
.default_bindings (plans-002-jsonb.xml, nexus-cefa1.5), split into
``..._tier_b_plans.sql``; P5 topic_links.link_types
(taxonomy-008-link-types-jsonb.xml, nexus-cefa1.6), the LAST column,
converted in place in ``..._tier_b.sql`` itself with nothing left to
split out). Each probe file documents its own RLS/BYPASSRLS caveats and
its own malformed-seed SQL shape; they remain on disk, unexecuted by any
test, as reference material for a pre-P5 (or rolled-back) cluster's own
manual audit -- consistent with how ``..._tier_b_hook_failures.sql`` etc.
already stayed shipped-but-dormant after THEIR phases converted.

A GENUINE PROBE FINDING from that era, still true and still documented in
``schema_type_hygiene_preflight_tier_a.sql``'s own header: PostgreSQL's
timestamp/timestamptz input grammar recognizes a small set of relative
keywords ('yesterday', 'today', 'tomorrow', 'now', 'epoch', 'infinity',
'-infinity', 'allballs') as VALID input -- so
``pg_input_is_valid('yesterday', 'timestamptz')`` is TRUE even though
'yesterday' plainly fails the lexical ``!~ '^[0-9]{4}-'`` ISO-prefix check
a naive audit might reach for instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

# (tier, table, column, expected) -- expected is the live PG type name every
# column must already be, now that the arc (P0-P5) is complete. A column
# that is NOT its expected type is loud, non-vacuous failure -- exactly the
# same assertion shape this list carried while some columns were still
# "text" mid-arc, just with no more "text" entries left.
_COLUMN_EXPECTATIONS: list[tuple[str, str, str, str]] = [
    ("A", "catalog_documents", "indexed_at", "timestamp with time zone"),
    ("A", "catalog_documents", "bib_enriched_at", "timestamp with time zone"),
    ("A", "catalog_documents", "index_started_at", "timestamp with time zone"),
    ("A", "catalog_links", "created_at", "timestamp with time zone"),
    ("B", "plans", "plan_json", "jsonb"),
    ("B", "plans", "default_bindings", "jsonb"),
    ("B", "topic_links", "link_types", "jsonb"),
    ("B", "document_aspects", "extras", "jsonb"),
    ("B", "document_aspects", "salient_sentences", "jsonb"),
    ("B", "hook_failures", "batch_doc_ids", "jsonb"),
]


def _psql(state: dict, sql: str) -> list[str]:
    """Run *sql* against the substrate's PG, returning stripped non-empty rows.

    Verbatim copy of the helper already used by
    ``tests/catalog/test_collection_scoped_tables_schema_parity.py``,
    ``tests/db/test_yu9w5_assign_from_chashes_integration.py``, and
    ``tests/test_o8dil7_prune_misclassified_manifest_antijoin_engine.py``.
    ``pg_user`` is ``os.environ["USER"]`` -- the initdb role (superuser) --
    so this bypasses RLS deliberately.
    """
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql),
            "-h", "127.0.0.1",
            "-p", str(state["pg_port"]),
            "-U", state["pg_user"],
            "-d", state["pg_dbname"],
            "-tAc", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"psql failed ({proc.returncode}) running:\n{sql}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _actual_data_type(state: dict, table: str, column: str) -> str:
    """Live ``information_schema.columns.data_type`` for (table, column) in
    the ``nexus`` schema -- the schema-awareness primitive this file's one
    remaining check is built from."""
    rows = _psql(state, (
        "SELECT data_type FROM information_schema.columns "
        f"WHERE table_schema = 'nexus' AND table_name = '{table}' "
        f"AND column_name = '{column}'"
    ))
    assert rows, f"nexus.{table}.{column} not found in information_schema.columns"
    return rows[0]


def test_column_types_match_migration_state(t2_service_env):
    """Non-vacuous replacement for a skip: EVERY audited column gets a real
    assertion against its LIVE type. A column whose _COLUMN_EXPECTATIONS
    entry drifts from the actual schema (a migration landed without
    updating this list, or a rollback reverted a column) fails here just as
    loudly regardless of which direction the drift goes."""
    from tests._engine_substrate import ensure_engine

    state = ensure_engine()

    for tier, table, column, expected in _COLUMN_EXPECTATIONS:
        actual = _actual_data_type(state, table, column)
        assert actual == expected, (
            f"nexus.{table}.{column} (Tier {tier}): expected data_type "
            f"{expected!r} per _COLUMN_EXPECTATIONS, got {actual!r} -- "
            "either a migration landed without updating this list, or this "
            "list is stale relative to the actual schema state"
        )
