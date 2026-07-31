# SPDX-License-Identifier: AGPL-3.0-or-later
"""``COLLECTION_SCOPED_TABLES`` must equal the SCHEMA, not merely equal itself (nexus-20890).

WHAT THIS GATES, AND WHY IT IS NOT WHERE YOU WOULD EXPECT IT
------------------------------------------------------------
``CatalogRepository.COLLECTION_SCOPED_TABLES`` is the single list driving BOTH
``renameCollectionTxn`` step 2 (re-home every child row X->Y) and ``collectionIsEmpty``
(may this retired name be revived?). Sharing one list makes those two operations
equal BY CONSTRUCTION, which killed the drift that shipped a silent two-collection
merge (nexus-v6za0: the re-home covered 17 tables, the emptiness check covered 5).

It does NOT protect against the list being incomplete against the SCHEMA. A new table
with a denormalized collection column that nobody registers is un-re-homed AND
invisible to the emptiness check *simultaneously* — consistent, and wrong. No test
could catch that, because both consumers agree with each other.

That is not hypothetical. It was realized within 24 hours: ``nexus.gc_audit`` landed
2026-07-30 (catalog-018-gc-audit.xml) carrying an indexed ``collection`` column, and
the nexus-v6za0 fix written the next day did not list it. Two human reviewers caught
it by reading the changelogs. This test is that reading, mechanized.

WHY IT LIVES IN PYTEST AND QUERIES A LIVE DATABASE
--------------------------------------------------
Two placement facts, both verified rather than assumed:

1. ``service-ci`` is NOT a required check on develop or main (nexus-hq9na). A Java
   test of this invariant would be ADVISORY at merge — which, for a class that has
   now recurred three times and shipped a P0 twice, is not a gate at all. This file
   rides ``pytest (Python 3.12)``, which IS required.

2. The obvious cheap implementation — regex the Liquibase changelogs — would make
   this gate a PROXY FOR THE SCHEMA. That is precisely the failure shape of the three
   attempts that preceded it (liveness proxied emptiness; identity proxied
   provenance; a 5-table list proxied "holds no data"). It would also be wrong today:
   ``gc_audit``'s column is declared inside a raw ``<sql>`` block, not as a Liquibase
   ``<column name=...>`` attribute, so an attribute-scanning parser reports it absent.

   So this asks POSTGRES, via the engine substrate — which boots a real PG and lets
   the engine apply every changelog. What columns EXIST is then ground truth: it
   cannot drift from the schema, because it *is* the schema.

SCOPE — WHAT "PARITY" MEANS HERE, AND WHAT IT DOES NOT
-------------------------------------------------------
The *existence* of columns is ground truth; WHICH of them count as collection
references is still decided by a name pattern (``LIKE '%collection%'``). So this
gate closes the case that actually bit us — a table added with a conventionally
named collection column and never registered — and does NOT close the case of a
collection reference that is named unconventionally or buried in JSONB. The
residual is enumerated on ``_COLLECTION_COLUMN_PREDICATE``; read it before relying
on a green run here as proof that rename re-homes everything, because it is not
that proof.

Non-vacuity is asserted explicitly in each direction; see ``test_parser_is_not_vacuous``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests._engine_substrate import ensure_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CATALOG_REPOSITORY = (
    _REPO_ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus"
    / "service" / "db" / "CatalogRepository.java"
)

#: SQL predicate identifying a column that denormalizes a collection name.
#:
#: DELIBERATELY A PATTERN, NOT A LIST. The first draft of this gate enumerated
#: ``('collection', 'source_collection', 'physical_collection')`` — and that
#: enumeration immediately reproduced the exact defect the gate exists to catch: it
#: could not see ``chash_remap.target_collection``, a column that plainly exists, so
#: the gate reported a documented exclusion as "no longer in the schema" while the
#: real answer was that the query was blind to it.
#:
#: An enumerated vocabulary standing in for a universal is the failure shape behind
#: every attempt in this arc (liveness for emptiness, identity for provenance, five
#: tables for seventeen). A pattern is strictly weaker evidence than an enumeration
#: is, but it is still NAME-BASED, and honesty about that matters more than the
#: rhetorical win of calling this "asking the schema".
#:
#: WHAT THIS GATE STILL CANNOT SEE — the residual, stated so nobody reads the
#: assertions as broader than they are:
#:   * a scalar column holding a collection name WITHOUT "collection" in its name
#:     (``corpus``, ``store``, ``owner_scope``, a bare ``name`` on a child table);
#:   * a collection name nested inside JSONB, which is not a column at all — this
#:     is not theoretical, it is ``migration_jobs`` below;
#:   * a collection referenced indirectly, via an id that resolves to a name.
#: The first class is the dangerous one, because it looks exactly like the columns
#: this gate DOES catch. Closing it needs a value-shaped check (does any column hold
#: a string matching a live collection name?) rather than a name-shaped one, which is
#: a bigger and slower piece of work — not attempted here, and not pretended away.
#:
#: The cost of the pattern is that unrelated ``%collection%`` columns surface here and
#: must be documented in ``_DOCUMENTED_EXCLUSIONS``. That is the correct direction to
#: fail: a spurious entry is loud and cheap, a missed column is silent and corrupting.
_COLLECTION_COLUMN_PREDICATE = "c.column_name LIKE '%collection%'"

#: The list is deliberately smaller than the schema in exactly these places, and each
#: entry states WHY. An exclusion that is not written down is indistinguishable from
#: an omission — which is the whole defect class this file exists for, so an
#: undocumented one must fail here rather than be tolerated.
#:
#: Every entry is LIVENESS-CHECKED below: an allowlist that outlives the table it
#: names silently pre-approves whatever lands at that name next.
_DOCUMENTED_EXCLUSIONS: dict[tuple[str, str], str] = {
    ("pdf_pipeline", "collection"): (
        "Transient per-job work queue, not collection-scoped storage. Rows are "
        "consumed and deleted; a rename mid-job is out of scope and re-homing them "
        "would mutate in-flight work."
    ),
    ("chash_remap", "source_collection"): (
        "RF-186-1 raw-fact migration ledger. source_collection is INSIDE the primary "
        "key (tenant_id, source_collection, old_id), so re-homing rewrites a PK and can "
        "collide 23505; and because the ledger is permanent, counting it as data would "
        "block a legitimate undo rename FOREVER. Genuine design question, not an "
        "oversight — tracked as nexus-4nll0."
    ),
    ("chash_remap", "target_collection"): (
        "Same ledger as above; the target leg is historical fact about a completed "
        "migration, not a live pointer. See nexus-4nll0."
    ),
    ("migration_jobs", "collections"): (
        "JSONB ARRAY of collection names (a job's whole input set), not a scalar "
        "denormalized name. COLLECTION_SCOPED_TABLES drives a scalar "
        "`UPDATE ... SET col = new WHERE col = old`, which cannot express a rewrite "
        "inside a JSONB document. A rename during an ACTIVE job therefore leaves the "
        "job pointing at a name that no longer exists — a real gap, but one this "
        "mechanism cannot close. Tracked as nexus-rvr1n."
    ),
    ("migration_jobs", "collections_key"): (
        "Idempotency key DERIVED from the collections set (at most one active job per "
        "(tenant, collection-set), nexus-melvx). Derived value, not a reference: "
        "rewriting it would forge a different job's identity. See nexus-rvr1n."
    ),
    ("migration_jobs", "per_collection_counts"): (
        "JSONB map keyed BY collection name — progress counters, not a reference. "
        "Same scalar-UPDATE limitation as `collections`. See nexus-rvr1n."
    ),
}

_ENTRY_RE = re.compile(
    r"new\s+CollectionScopedTable\(\s*"
    r'"(?P<key>[^"]+)"\s*,\s*'
    r"(?P<table>[A-Z0-9_]+)\s*,\s*"
    r"[A-Z0-9_]+\.(?P<column>[A-Z0-9_]+)\s*\)"
)

#: Floor for the parsed-entry count. Not a magic number: the list held 17 entries when
#: this gate was written and only ever grows. If the regex silently stops matching (a
#: refactor to a builder, a rename of the record) the parse returns a small set, every
#: set-difference assertion below passes vacuously, and the gate reports GREEN while
#: checking nothing. This turns that into a loud failure.
_MIN_EXPECTED_ENTRIES = 15


def _parse_java_list() -> set[tuple[str, str]]:
    """(table_name, column_name) pairs registered in COLLECTION_SCOPED_TABLES.

    Derived from the jOOQ constants (``CHUNKS_384``, ``.SOURCE_COLLECTION``) rather
    than from the human-written count key, because the constants are what the SQL
    actually touches. The count key is cross-checked against them separately.
    """
    source = _CATALOG_REPOSITORY.read_text(encoding="utf-8")
    body = source.split("COLLECTION_SCOPED_TABLES", 1)
    assert len(body) > 1, (
        f"COLLECTION_SCOPED_TABLES not found in {_CATALOG_REPOSITORY}. If it was "
        f"renamed or restructured, update this gate — do not delete it."
    )
    return {
        (m.group("table").lower(), m.group("column").lower())
        for m in _ENTRY_RE.finditer(source)
    }


def _parse_java_count_keys() -> list[tuple[str, str]]:
    """(count_key, table_constant_lowercased) for the count-key drift check."""
    source = _CATALOG_REPOSITORY.read_text(encoding="utf-8")
    return [
        (m.group("key"), m.group("table").lower())
        for m in _ENTRY_RE.finditer(source)
    ]


def _psql(state: dict, sql: str) -> list[str]:
    """Run *sql* against the substrate's PG, returning stripped non-empty rows."""
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


@pytest.fixture
def schema_pairs(t2_service_env: str) -> set[tuple[str, str]]:
    """Every (table, collection-ish column) in the live ``nexus`` schema.

    Depends on ``t2_service_env`` so the engine substrate is booted and every
    Liquibase changelog has been applied by the engine itself — the same code path
    a real install takes, so this cannot drift from production DDL.
    """
    state = ensure_engine()
    rows = _psql(
        state,
        "SELECT c.table_name || ' ' || c.column_name "
        "FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = 'nexus' "
        f"  AND {_COLLECTION_COLUMN_PREDICATE} "
        "  AND t.table_type = 'BASE TABLE' "
        "ORDER BY 1",
    )
    pairs = {tuple(r.split(" ", 1)) for r in rows}  # type: ignore[misc]
    assert len(pairs) >= _MIN_EXPECTED_ENTRIES, (
        f"information_schema returned only {len(pairs)} collection-scoped columns in "
        f"schema 'nexus'. The engine substrate almost certainly did not apply the "
        f"changelogs — this gate would pass vacuously. Rows: {sorted(pairs)}"
    )
    return pairs  # type: ignore[return-value]


def test_parser_is_not_vacuous() -> None:
    """The Java parse must actually find the list.

    Guards the direction every set-difference assertion below is blind to: if the
    regex matches nothing, ``registered`` is empty, and "everything in the schema is
    missing" would be the finding — loud. But if the regex matched a *subset*, the
    schema-side assertion would still fire while the java-side one passed quietly.
    Pin the floor so a partial parse cannot masquerade as a small list.
    """
    registered = _parse_java_list()
    assert len(registered) >= _MIN_EXPECTED_ENTRIES, (
        f"parsed only {len(registered)} entries from COLLECTION_SCOPED_TABLES "
        f"(expected >= {_MIN_EXPECTED_ENTRIES}). The regex has almost certainly "
        f"stopped matching the source. Parsed: {sorted(registered)}"
    )
    assert all(
        "collection" in col for _, col in registered
    ), f"parsed a non-collection column: {sorted(registered)}"


def test_count_keys_match_their_tables() -> None:
    """The human-written count key must name the table it re-homes.

    The count key is what the rename endpoint reports back to the operator. A key
    that disagrees with its table makes the response actively misleading about what
    moved, and is a cheap early signal that an entry was copy-pasted.
    """
    mismatched = [
        (key, table) for key, table in _parse_java_count_keys() if key != table
    ]
    assert not mismatched, (
        "count key does not match its jOOQ table constant: "
        + ", ".join(f"key {k!r} on table {t!r}" for k, t in mismatched)
    )


def test_documented_exclusions_are_live(
    schema_pairs: set[tuple[str, str]],
) -> None:
    """Every allowlisted exclusion must still exist in the schema.

    Without this, a dropped or renamed table leaves a permanent standing exemption,
    and the next table to take that name is pre-approved for exclusion by an entry
    written about something else entirely.
    """
    stale = sorted(set(_DOCUMENTED_EXCLUSIONS) - schema_pairs)
    assert not stale, (
        f"_DOCUMENTED_EXCLUSIONS names {stale}, which no longer exist in schema "
        f"'nexus'. Remove the stale entries — a dead exclusion silently pre-approves "
        f"whatever lands at that name next."
    )


def test_every_collection_scoped_table_is_registered_or_excluded(
    schema_pairs: set[tuple[str, str]],
) -> None:
    """THE GATE. A table with a collection column is re-homed, or documented as not.

    This is the assertion that would have caught ``gc_audit``. Failure here means a
    rename will leave rows behind under the old collection name AND ``collectionIsEmpty``
    will not see them — so the name reads empty and a later rename can merge two
    collections on top of each other, silently.
    """
    registered = _parse_java_list()
    unaccounted = sorted(schema_pairs - registered - set(_DOCUMENTED_EXCLUSIONS))
    assert not unaccounted, (
        "these columns denormalize a collection name but are neither registered in "
        f"COLLECTION_SCOPED_TABLES nor documented as excluded: {unaccounted}\n\n"
        "A rename will not re-home them, and collectionIsEmpty will not see them, so "
        "the collection reads EMPTY while still holding rows — the silent-merge class "
        "(nexus-v6za0). Add each to COLLECTION_SCOPED_TABLES in CatalogRepository, or "
        "to _DOCUMENTED_EXCLUSIONS here WITH a reason."
    )


def test_no_registered_table_is_missing_from_the_schema(
    schema_pairs: set[tuple[str, str]],
) -> None:
    """The other direction: the list must not name something the schema lacks.

    A superset is not harmless. ``collectionIsEmpty`` short-circuits on the first
    table holding a row, and the re-home loop UPDATEs each entry — an entry naming a
    dropped table fails the whole rename transaction, and an entry naming the wrong
    column re-homes nothing while reporting a count. Equality is the invariant, in
    both directions.
    """
    registered = _parse_java_list()
    phantom = sorted(registered - schema_pairs)
    assert not phantom, (
        f"COLLECTION_SCOPED_TABLES registers {phantom}, absent from schema 'nexus'. "
        f"The rename transaction will fail on those entries."
    )
