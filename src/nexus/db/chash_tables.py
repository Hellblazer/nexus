# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The canonical set of chash-bearing tables (single source of truth).

Both the pre-upgrade poison probe (``nexus.health._check_migration_state``)
and the RDR-182 ``chash-poison`` forensics topic
(``nexus.remediation.playbook``) count non-32-char chash rows across these
tables. Keeping ONE list here means the operator's ``nx doctor`` warning, the
``install-binary`` gate, and the agent-facing forensics diagnostic can never
drift to checking different tables (which would let a poisoned table slip past
one surface but not another).

Two severity classes (nexus-z5j0t), decided by ONE structural criterion —
whether the table carries a width CHECK constraint that a Liquibase
``VALIDATE CONSTRAINT`` will run on the next engine upgrade:

- **poison** (``chunks_{384,768,1024}``, ``chash_index``,
  ``catalog_document_chunks``): these carry ``*_chash_len_check`` /
  ``*_chash_octet_check`` width constraints
  (catalog-002/catalog-013), so a non-conformant row crash-loops the next
  upgrade (GH #1390 / nexus-pnwu0). Counts here GATE ``install-binary``.
- **legacy debt** (``topic_assignments.doc_id``, ``frecency.chunk_id``,
  ``relevance_log.chunk_id``): chash-bearing soft references with NO check
  (FK-less by design — nexus-sa14p; telemetry-001). A non-conformant value
  cannot fail any VALIDATE; it silently degrades topic membership or
  frecency ranking instead. Counts here are OBSERVED (diag view, forensics,
  a non-gating doctor warning) and converged by the remap cascade /
  RDR-180 Item6 ETL — never an upgrade gate (a legitimately debt-carrying
  install must not brick its install-binary path).

stdlib-only by design so the dependency-light ``nexus.remediation`` package
can import it without pulling ``nexus.health``'s weight.
"""
from __future__ import annotations

from typing import NamedTuple


class ChashBearingTable(NamedTuple):
    """One chash-bearing PG relation: where the content address lives and
    whether a non-conformant value there gates upgrades (see module doc)."""

    table: str  #: schema-qualified relation name (``nexus.<table>``)
    column: str  #: the chash-bearing column (``chash`` / ``doc_id`` / ``chunk_id``)
    poison: bool  #: True → counts gate install-binary (GH #1390 class)


#: The authoritative chash-bearing set. A non-32-char value in a ``poison``
#: entry is the GH #1390 / nexus-pnwu0 poison class; in a non-poison entry it
#: is legacy debt (see module doc). Column names are NOT uniform — the
#: RDR-185 .13 audit's chunk_id-naming blind spot is exactly why the entry
#: shape is column-aware (nexus-z5j0t).
CHASH_BEARING_TABLES: tuple[ChashBearingTable, ...] = (
    ChashBearingTable("nexus.chunks_384", "chash", poison=True),
    ChashBearingTable("nexus.chunks_768", "chash", poison=True),
    ChashBearingTable("nexus.chunks_1024", "chash", poison=True),
    ChashBearingTable("nexus.chash_index", "chash", poison=True),
    ChashBearingTable("nexus.catalog_document_chunks", "chash", poison=True),
    ChashBearingTable("nexus.topic_assignments", "doc_id", poison=False),
    ChashBearingTable("nexus.frecency", "chunk_id", poison=False),
    ChashBearingTable("nexus.relevance_log", "chunk_id", poison=False),
)

#: The upgrade-gating subset — the probe statements the install-binary gate
#: and the forensics topic run. Identical to the pre-z5j0t five-table set,
#: so deployed 5-leg views keep satisfying the gate unchanged.
POISON_CHASH_TABLES: tuple[ChashBearingTable, ...] = tuple(
    t for t in CHASH_BEARING_TABLES if t.poison
)

#: The observed-only subset (see module doc). Probed best-effort against the
#: diag view; a stale (pre-z5j0t) deployed view simply has no rows for these
#: table_names, which callers treat as "unknown", never as clean or poisoned.
DEBT_CHASH_TABLES: tuple[ChashBearingTable, ...] = tuple(
    t for t in CHASH_BEARING_TABLES if not t.poison
)


#: The poison-detail matcher token (nexus-jxizy.5): the health probe's
#: poison HealthResult embeds this exact phrase in its ``detail``, and the
#: install-binary gate (``commands/daemon.py``) plus the convergence gate
#: (``upgrade_finish.py``) substring-match on it to distinguish REAL poison
#: from probe-degraded WARNs under the same label. ONE constant so the
#: wording and its matchers cannot drift (era-neutral: octet_length ≠ 32 is
#: the predicate in both the text era and the bytea era).
POISON_DETAIL_TOKEN: str = "width-non-conformant chash"


#: RDR-182 Amendment A6 (nexus-9bufb): the structural content boundary. A
#: superuser-owned counts view — the diagnostic role reads COUNTS BY
#: CONSTRUCTION (definer semantics + RLS exemption via the superuser owner),
#: never row content; the runAlways grants changeset revokes nexus_diag's
#: direct table SELECT once this view exists.
DIAG_CONFORMANCE_VIEW: str = "nexus.diag_chash_conformance"


def diag_conformance_view_ddl() -> str:
    """The view's DDL, generated from :data:`CHASH_BEARING_TABLES` so the
    view and the constant cannot drift (pinned by test). Covers BOTH severity
    classes — the view is the observability surface; gating is decided by
    which statements a caller runs, not by view membership. Executed by the
    SUPERUSER provisioning path (``pg_provision``) — under FORCE RLS a view
    counts cross-tenant rows only when its OWNER is RLS-exempt, which only
    the superuser context can arrange (the nexus-vounk lesson, structurally).
    Managed/DBA deployments get the rendered copy in docs/configuration.md.

    ERA-SAFE PREDICATE (RDR-180 Item6a, nexus-jxizy.5): the conformance
    predicate is ``octet_length(col) <> 32`` — deliberately NOT ``length``.
    ``octet_length`` of today's 32-hex TEXT value is 32 (bytes==chars for
    hex ASCII) and of the post-flip 32-byte BYTEA value is also 32, so ONE
    spelling accepts exactly the era-canonical form in each era: a
    premature 64-hex text write counts as poison today, and a leftover
    16-byte legacy value counts as poison after the BYTEA cutover. The
    32-vs-64 units ambiguity (hex chars vs bytes) cannot recur in this
    predicate for DIGEST-SHAPED values (always ASCII hex, bytes==chars).
    Known asymmetry (reviewer-180-foundation, accepted): a corrupt value
    whose multi-byte UTF-8 chars happen to sum to exactly 32 octets with
    length()<32 would have been flagged under the old spelling and passes
    under this one — width conformance is a byte property here, and the
    upgrade-crash-loop risk this gate exists for (VALIDATE of the byte
    CHECKs) tracks octets, not chars. Deliberately NO hex-charset leg:
    ETL-era non-hex 32-char ids are contract-legal pre-rekey and must not
    fire the install gate.

    DEBT LEGS ARE ANTI-JOINS (RDR-180 .6 amendment 1): the debt columns
    stay TEXT (mixed identity space — chunk chashes AND memory-note titles
    in ``topic_assignments.doc_id``), so a width predicate mismeasures them
    across eras (64-hex text = 64 octets; titles always flagged). The
    honest, era-independent debt definition is SEMANTIC: a hex-shaped
    reference that misses its chunk-table join. Titles and other non-hex
    identities are excluded by the hex guard — they are not chash debt.
    NOTE: the debt legs decode() against the bytea chunk keys, so this view
    only CREATEs against a post-rdr180 (bytea) engine schema — on an older
    text-era store the CREATE fails and provisioning's best-effort catch
    degrades the probe to legacy statements (the converged-pair floor makes
    that window transient).
    """
    poison_legs = [
        f"SELECT '{t.table}' AS table_name, count(*) AS non_conformant "
        f"FROM {t.table} WHERE octet_length({t.column}) <> 32"
        for t in POISON_CHASH_TABLES
    ]
    chunk_anti_join = " AND ".join(
        f"NOT EXISTS (SELECT 1 FROM nexus.chunks_{dim} c "
        f"WHERE c.chash = decode(t.{{col}}, 'hex'))"
        for dim in (384, 768, 1024)
    )
    debt_legs = [
        f"SELECT '{t.table}' AS table_name, count(*) AS non_conformant "
        f"FROM {t.table} t "
        f"WHERE t.{t.column} ~ '^[0-9a-f]+$' AND length(t.{t.column}) % 2 = 0 "
        f"AND " + chunk_anti_join.format(col=t.column)
        for t in DEBT_CHASH_TABLES
    ]
    union = "\nUNION ALL\n".join(poison_legs + debt_legs)
    return f"CREATE OR REPLACE VIEW {DIAG_CONFORMANCE_VIEW} AS\n{union}"


def chash_conformance_statements() -> tuple[str, ...]:
    """One aggregate statement per POISON table AGAINST THE COUNTS VIEW
    (Amendment A6) — same one-number-per-statement output shape as the
    legacy direct counts (the probe's parser is unchanged), same read-only
    aggregate-only shape the ``nexus_diag`` lint accepts. Poison-only BY
    DESIGN (nexus-z5j0t): these feed the install-binary gate, and every
    deployed view generation (5-leg or 8-leg) carries these rows, so the
    gate's behavior is invariant across view generations. The view emits
    exactly one row per covered table unconditionally, so ``sum`` never
    returns NULL here."""
    return tuple(
        f"SELECT sum(non_conformant) FROM {DIAG_CONFORMANCE_VIEW} "
        f"WHERE table_name = '{t.table}'"
        for t in POISON_CHASH_TABLES
    )


def debt_chash_conformance_statements() -> tuple[str, ...]:
    """One aggregate statement per LEGACY-DEBT table against the counts view
    (nexus-z5j0t). Non-gating observability: callers run these best-effort
    AFTER the poison statements succeed. Against a stale (pre-z5j0t) view the
    ``WHERE table_name`` filter matches no rows and ``sum`` returns NULL —
    psql renders that as an empty line, which callers MUST treat as
    "unknown (stale view)", never as a count."""
    return tuple(
        f"SELECT sum(non_conformant) FROM {DIAG_CONFORMANCE_VIEW} "
        f"WHERE table_name = '{t.table}'"
        for t in DEBT_CHASH_TABLES
    )


def legacy_chash_conformance_statements() -> tuple[str, ...]:
    """The pre-A6 direct-table counts. The health probe FALLS BACK to these
    when the view is absent (an engine older than the A6 changeset) — the
    old grants era still carries full-table SELECT, so the fallback works
    exactly as before; without it the install-binary gate would fail loud on
    every store one engine-generation behind. Poison-only: pre-A6 engines
    predate the telemetry-001 debt tables, so direct debt counts there would
    fail on missing relations. Same era-safe ``octet_length`` predicate as
    the view (see :func:`diag_conformance_view_ddl`)."""
    return tuple(
        f"SELECT count(*) FROM {t.table} WHERE octet_length({t.column}) <> 32"
        for t in POISON_CHASH_TABLES
    )
