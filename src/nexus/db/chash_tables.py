# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The canonical set of chash-bearing tables (single source of truth).

Both the pre-upgrade poison probe (``nexus.health._check_migration_state``)
and the ``install-binary`` gate's guidance
(``nexus.upgrade_finish._ChashPoisonGuidance`` — the RDR-182 forensics
topic that used to share this table set was deleted at nexus-lgdel) count
non-32-char chash rows across these tables. Keeping ONE list here means the
operator's ``nx doctor`` warning and the ``install-binary`` gate can never
drift to checking different tables (which would let a poisoned table slip
past one surface but not another).

RDR-191 (nexus-o8dil.19): ``chunks_384`` / ``chunks_768`` / ``chunks_1024``
unify into ONE physical relation, :data:`CHUNKS_TABLE` (three mutually
exclusive nullable ``embedding_<dim>`` columns, exactly-one CHECK). Every
site below that used to iterate the three per-dim tables now needs only
this one name — the dim CONCEPT survives (which embedding column is
populated), but it is no longer a reason to iterate tables, since there is
only one. ``nexus.chash_index`` was already retired ahead of this (RDR-187
/ nexus-piwya.5) and is independent of the unify.

Two severity classes (nexus-z5j0t), decided by ONE structural criterion —
whether the table carries a width CHECK constraint that a Liquibase
``VALIDATE CONSTRAINT`` will run on the next engine upgrade:

- **poison** (``chunks``, ``catalog_document_chunks``): these carry
  ``*_chash_len_check`` / ``*_chash_octet_check`` width constraints
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

from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple, Sequence


class ProbeState(str, Enum):
    """Tri-state outcome of a chash-conformance probe (nexus-hdumg).

    The two non-MEASURED arms exist so no consumer can collapse "I could
    not count" into "I counted zero" — the vacuous-verification class. This
    mirrors what ``health._check_migration_state`` and
    ``diag_connection.live_store_detail`` already do in prose/sentinel form
    (``nonconforming = -1``; "treat store state as UNKNOWN, not clean"),
    hoisted into a type so the ladder rung cannot re-lose it.
    """

    MEASURED = "measured"        #: the diagnostic ran and produced a count
    UNAVAILABLE = "unavailable"  #: no diagnostic path on this box (pre-P2.1 install)
    FAILED = "failed"            #: the diagnostic was attempted and could not execute


@dataclass(frozen=True)
class ConformanceProbe:
    """A conformance measurement, or an explicit reason there isn't one.

    Frozen (the ``migrations.py`` StepOutcome precedent): a consumer can
    never "fix up" a probe result into a clean verdict.
    """

    state: ProbeState
    count: int = -1
    reason: str = ""

    @classmethod
    def measured(cls, count: int) -> "ConformanceProbe":
        if count < 0:
            raise ValueError(f"a measured conformance count cannot be negative: {count}")
        return cls(state=ProbeState.MEASURED, count=count)

    @classmethod
    def unavailable(cls, reason: str) -> "ConformanceProbe":
        return cls(state=ProbeState.UNAVAILABLE, reason=reason)

    @classmethod
    def failed(cls, reason: str) -> "ConformanceProbe":
        return cls(state=ProbeState.FAILED, reason=reason)

    @property
    def conformant(self) -> bool:
        """True ONLY on a real measurement of zero. The single predicate any
        verification gate may assert on — absence of a signal is never it."""
        return self.state is ProbeState.MEASURED and self.count == 0

    @property
    def unmeasured(self) -> bool:
        return self.state is not ProbeState.MEASURED


class ChashBearingTable(NamedTuple):
    """One chash-bearing PG relation: where the content address lives and
    whether a non-conformant value there gates upgrades (see module doc)."""

    table: str  #: schema-qualified relation name (``nexus.<table>``)
    column: str  #: the chash-bearing column (``chash`` / ``doc_id`` / ``chunk_id``)
    poison: bool  #: True → counts gate install-binary (GH #1390 class)
    bytea: bool = False
    """True when the column itself is stored as ``bytea`` (RDR-194 P3c,
    nexus-tk070.p3c). Every POISON entry is bytea by construction (RDR-180
    converted chash/chunk-content columns first) and does not need this
    flag read — the octet_length predicate is already era-safe for those.
    It exists for the DEBT entries, which are NOT era-uniform: RDR-194 P3c
    moved ``topic_assignments.doc_id`` to bytea while ``frecency.chunk_id``
    and ``relevance_log.chunk_id`` stay TEXT (nexus-lgdel.l1's canonical-only
    CHECK covers those two; they were never part of D1's scope). A bytea
    debt column's anti-join is direct equality against the chunk key (no
    regex/decode — the value already IS bytes); a TEXT debt column's
    anti-join still needs the hex-shape guard + decode(). See
    :func:`diag_conformance_view_ddl` for where this branches."""


#: The unified chunk-storage relation (RDR-191, nexus-o8dil.19): the ONE
#: declaration every chunk-table reference in this module derives from —
#: both the tuple entry below and the debt-leg anti-join in
#: :func:`diag_conformance_view_ddl` name this constant, never a re-typed
#: literal, so the three-per-dim -> one-unified collapse cannot drift
#: between the two sites that used to need it independently.
CHUNKS_TABLE: str = "nexus.chunks"

#: The authoritative chash-bearing set. A non-32-char value in a ``poison``
#: entry is the GH #1390 / nexus-pnwu0 poison class; in a non-poison entry it
#: is legacy debt (see module doc). Column names are NOT uniform — the
#: RDR-185 .13 audit's chunk_id-naming blind spot is exactly why the entry
#: shape is column-aware (nexus-z5j0t).
CHASH_BEARING_TABLES: tuple[ChashBearingTable, ...] = (
    # RDR-191 (nexus-o8dil.19): chunks_384/768/1024 unify into ONE physical
    # relation (CHUNKS_TABLE, above) — three mutually exclusive nullable
    # embedding_<dim> columns behind one table, so this is one entry, not
    # three. nexus.chash_index was already retired (RDR-187 / nexus-piwya.5,
    # ahead of the table DROP) and is independent of this unify; the
    # gate/forensics statements filter by table_name, so any deployed view
    # generation still satisfies the gate unchanged, and the LEGACY
    # direct-table fallback no longer queries a dropped relation.
    ChashBearingTable(CHUNKS_TABLE, "chash", poison=True, bytea=True),
    ChashBearingTable("nexus.catalog_document_chunks", "chash", poison=True, bytea=True),
    # RDR-194 P3c (nexus-tk070.p3c, taxonomy-011-doc-id-bytea.xml): doc_id
    # moved from TEXT to bytea. See ChashBearingTable.bytea's own docstring
    # for why this is a per-entry flag, not a blanket era assumption.
    ChashBearingTable("nexus.topic_assignments", "doc_id", poison=False, bytea=True),
    ChashBearingTable("nexus.frecency", "chunk_id", poison=False),
    ChashBearingTable("nexus.relevance_log", "chunk_id", poison=False),
)

#: The upgrade-gating subset — the probe statements the install-binary gate
#: and the forensics topic run. Two tables post-RDR-191 unify (was four
#: post-RDR-187: chunks_384/768/1024 collapsed to one, chash_index already
#: retired); per-table table_name filtering keeps every deployed view
#: generation satisfying the gate unchanged.
POISON_CHASH_TABLES: tuple[ChashBearingTable, ...] = tuple(
    t for t in CHASH_BEARING_TABLES if t.poison
)

#: The observed-only subset (see module doc). Probed best-effort against the
#: diag view; a stale (pre-z5j0t) deployed view simply has no rows for these
#: table_names, which callers treat as "unknown", never as clean or poisoned.
DEBT_CHASH_TABLES: tuple[ChashBearingTable, ...] = tuple(
    t for t in CHASH_BEARING_TABLES if not t.poison
)


#: RDR-191 F14a mirror-direction straddle in the POISON GATE (nexus-o8dil,
#: 2026-08-14): the pre-convergence chash-poison probe (``nexus.health.
#: _check_migration_state``, reused by the install-binary gate and by
#: ``nx daemon restart-stale``'s convergence check) runs CLIENT-SIDE Python
#: that already carries the POST-unify :data:`CHASH_BEARING_TABLES` (naming
#: :data:`CHUNKS_TABLE` = ``"nexus.chunks"``) against a store whose engine
#: has NOT yet run the migration that creates that relation — the exact
#: "retargeting early validates against a table that does not exist yet"
#: class RDR-191 named for ``chash_rekey`` (``nexus.upgrade_ladder.rungs.
#: chash_rekey``'s ``OCTET_CHECKS`` partition / ``admin_sql.py``'s F14a
#: existence gate), recurring here for the poison probe instead of a
#: VALIDATE. This is the PRE-unify per-dim shape the probe must be able to
#: verify against on an old box: ``chunks_384``/``chunks_768``/
#: ``chunks_1024`` + ``catalog_document_chunks`` (the last is unchanged
#: across the unify — included here so this inventory is self-contained,
#: not because its name differs). Existence-gated at the call site
#: (``nexus.health._check_migration_state``'s era discriminator), never
#: assumed — a box that has neither this set nor the unified set is
#: genuinely unverifiable and defers, per the existing fail-safe.
LEGACY_CHASH_BEARING_TABLES: tuple[ChashBearingTable, ...] = (
    ChashBearingTable("nexus.chunks_384", "chash", poison=True),
    ChashBearingTable("nexus.chunks_768", "chash", poison=True),
    ChashBearingTable("nexus.chunks_1024", "chash", poison=True),
    ChashBearingTable("nexus.catalog_document_chunks", "chash", poison=True),
)

#: The (all-poison) gating subset of the legacy inventory — mirrors
#: :data:`POISON_CHASH_TABLES` for the pre-unify era.
LEGACY_POISON_CHASH_TABLES: tuple[ChashBearingTable, ...] = tuple(
    t for t in LEGACY_CHASH_BEARING_TABLES if t.poison
)


#: The poison-detail matcher token (nexus-jxizy.5): the health probe's
#: poison HealthResult embeds this exact phrase in its ``detail``, and the
#: install-binary gate (``commands/daemon.py``) plus the convergence gate
#: (``upgrade_finish.py``) substring-match on it to distinguish REAL poison
#: from probe-degraded WARNs under the same label. ONE constant so the
#: wording and its matchers cannot drift (era-neutral: octet_length ≠ 32 is
#: the predicate in both the text era and the bytea era).
POISON_DETAIL_TOKEN: str = "width-non-conformant chash"


#: The HealthResult label the chash-conformance probe reports under
#: (nexus-pgdcv round-2 MEDIUM-1). ``health._check_migration_state`` appends
#: every conformance outcome — poison, probe-couldn't-run WARN — under this
#: label, and the two gates (``upgrade_finish._poison_probe``,
#: ``commands/daemon.py`` install-binary) filter on it to classify the
#: store. ONE constant so a label rename cannot silently collapse both
#: filters to empty (which would misread a poisoned store as clean).
CHASH_CONFORMANCE_LABEL: str = "Chunk chash conformance"


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
    which statements a caller runs, not by view membership.

    ``WITH (security_invoker = true)`` (RDR-194 P3c critical fix round,
    2026-08-17, nexus-i3k3e/Sig-2): PG15+ view option that evaluates RLS
    (and privilege checks) against the INVOKING role, not the view's OWNER.
    Before this flag existed, the nexus-vounk lesson held without
    exception — "under FORCE RLS a view counts cross-tenant rows only when
    its OWNER is RLS-exempt" — which meant only a superuser-owned view
    (the ``pg_provision`` provisioning path) could ever see all tenants,
    and a Liquibase-owned view (created as ``nexus_admin``, NOSUPERUSER
    NOCREATEROLE, no BYPASSRLS) would silently degrade to zero rows.
    ``security_invoker`` inverts that: RLS is now evaluated against
    whichever role RUNS the query, so ``nexus_diag`` (LOGIN ... BYPASSRLS,
    ``grants-nexus-diag.xml``) sees every tenant's rows regardless of who
    owns the view. This is what makes it safe for BOTH consumers of this
    generator to create the SAME view text: the superuser provisioning
    path (``pg_provision._provision_diag_conformance_view``, still the
    only route for a pre-P3c engine upgrading, and a harmless idempotent
    no-op — ``CREATE OR REPLACE`` — if it races with or follows the
    Liquibase changeset) AND, since RDR-194 P3c
    (``taxonomy-011-doc-id-bytea.xml``'s ``taxonomy-011-8``, runAlways),
    the engine's own Liquibase walk. A superuser querying a
    ``security_invoker`` view is unaffected (superuser already bypasses
    RLS regardless of invoker/definer semantics), so this is a strict
    widening, not a behavior change for the existing superuser-owned
    deployments. Managed/DBA deployments get the rendered copy in
    docs/configuration.md.

    Two independent copies of this exact DDL text exist by necessity — the
    Python string here, and a literal SQL copy embedded in
    ``taxonomy-011-doc-id-bytea.xml``'s ``taxonomy-011-8`` changeset (a
    static XML changelog cannot import this function) — pinned to each
    other by ``tests/test_diag_conformance_view.py::
    test_liquibase_owned_view_matches_the_generator``, the same
    containment-check pattern ``test_docs_rendered_copy_matches_the_
    generator`` already uses for the docs/configuration.md copy.

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

    DEBT LEGS ARE ANTI-JOINS (RDR-180 .6 amendment 1): a hex-shaped
    reference that misses its chunk-table join. CORRECTED (RDR-194 D1,
    nexus-tk070.p3a, nexus-yo9mi): ``topic_assignments.doc_id`` is NOT a
    mixed identity space and does NOT hold memory-note titles: every live
    writer emits a chunk chash (RDR-180 Item6/Item6a; the one real
    memory-note-clustering path died with the SQLite store at commit
    ``f24bdb853``). Non-hex-shaped values are excluded by the hex guard as
    ETL-era external ids, not titles.

    MIXED-ERA DEBT COLUMNS (RDR-194 P3c, nexus-tk070.p3c): the debt legs are
    no longer uniformly TEXT. ``topic_assignments.doc_id`` moved to
    ``bytea`` (:attr:`ChashBearingTable.bytea`); ``frecency.chunk_id`` and
    ``relevance_log.chunk_id`` stay TEXT (nexus-lgdel.l1's canonical-only
    CHECK covers those two directly, so their debt leg here is genuinely
    the SOFT reference this view exists to observe, not width conformance).
    A bytea debt column's anti-join is direct bytea equality against
    :data:`CHUNKS_TABLE`'s ``chash`` — no regex guard, no ``decode()``, the
    value already IS bytes. A TEXT debt column's anti-join keeps the
    hex-shape guard + ``decode()`` form. Both shapes still degrade the same
    way: this view only CREATEs once every referenced table/column exists
    in its CURRENT type (a stale, pre-P3c engine's provisioning attempt
    against an already-bytea doc_id, or a not-yet-converted engine's
    attempt with this generator, would fail the CREATE the same way a
    pre-rdr180 text-era store already did) — provisioning's best-effort
    catch degrades the probe to legacy statements meanwhile (the
    converged-pair floor makes that window transient).

    RDR-191 (nexus-o8dil.19): the anti-join used to be a 3-way ``AND`` over
    ``chunks_384``/``chunks_768``/``chunks_1024`` (a debt chash counted as
    "referenced" only if it matched none of the three per-dim tables). With
    the chunk tables unified into :data:`CHUNKS_TABLE`, a chash either has a
    row there or it does not — dim was never part of the debt predicate's
    identity, only an artifact of it having to probe three tables to answer
    one question. The anti-join collapses to ONE ``NOT EXISTS``, derived
    from the same :data:`CHUNKS_TABLE` declaration :data:`CHASH_BEARING_TABLES`
    uses, so the two sites cannot re-diverge on the table name.
    """
    poison_legs = [
        f"SELECT '{t.table}' AS table_name, count(*) AS non_conformant "
        f"FROM {t.table} WHERE octet_length({t.column}) <> 32"
        for t in POISON_CHASH_TABLES
    ]
    def _debt_leg(t: ChashBearingTable) -> str:
        if t.bytea:
            # RDR-194 P3c: the column is bytea already -- direct equality,
            # no hex-shape guard, no decode(). Same NOT EXISTS anti-join
            # SHAPE as the engine-side ChashCensus.unresolvableBytesCount
            # idiom, but deliberately WIDER in scope (critic finding,
            # 2026-08-17): that idiom restricts to
            # octet_length(byteCol) IN (16, 32) before its anti-join -- a
            # narrow census-leg scope tuned to the two widths ITS OWN
            # callers care about (canonical vs. one legacy width). This
            # view's anti-join carries no width restriction at all, so it
            # also surfaces a row of any OTHER width as unresolvable --
            # the more conservative, no-silent-miss choice for a pure
            # observability surface (a malformed row of an unexpected
            # width is exactly the kind of thing a diagnostic view should
            # never quietly exclude). "Mirrors exactly" was inaccurate
            # prose, not a behavior bug; left as documented, not narrowed
            # to match, since narrowing would REDUCE what this view can
            # see.
            return (
                f"SELECT '{t.table}' AS table_name, count(*) AS non_conformant "
                f"FROM {t.table} t "
                f"WHERE NOT EXISTS (SELECT 1 FROM {CHUNKS_TABLE} c "
                f"WHERE c.chash = t.{t.column})"
            )
        return (
            f"SELECT '{t.table}' AS table_name, count(*) AS non_conformant "
            f"FROM {t.table} t "
            f"WHERE t.{t.column} ~ '^[0-9a-f]+$' AND length(t.{t.column}) % 2 = 0 "
            f"AND NOT EXISTS (SELECT 1 FROM {CHUNKS_TABLE} c "
            f"WHERE c.chash = decode(t.{t.column}, 'hex'))"
        )

    debt_legs = [_debt_leg(t) for t in DEBT_CHASH_TABLES]
    union = "\nUNION ALL\n".join(poison_legs + debt_legs)
    return (
        f"CREATE OR REPLACE VIEW {DIAG_CONFORMANCE_VIEW} "
        f"WITH (security_invoker = true) AS\n{union}"
    )


def chash_conformance_statements() -> tuple[str, ...]:
    """One aggregate statement per POISON table AGAINST THE COUNTS VIEW
    (Amendment A6) — same one-number-per-statement output shape as the
    legacy direct counts (the probe's parser is unchanged), same read-only
    aggregate-only shape the ``nexus_diag`` lint accepts. Poison-only BY
    DESIGN (nexus-z5j0t): these feed the install-binary gate, and every
    deployed view generation (5-, 7- or 8-leg) carries these rows, so the
    gate's behavior is invariant across view generations.

    CAVEAT (nexus-o8dil, 2026-08-14, RDR-191 F14a mirror-direction straddle
    in the poison gate — GH #1414 class recurrence): "every deployed view
    generation carries these rows" is true only when the deployed view was
    (re)created under the SAME table-name era this function filters by. A
    store whose engine has not yet migrated to the unified ``nexus.chunks``
    relation can still have a LIVE, queryable view — just one built by an
    OLDER engine's provisioning, keyed by the PRE-unify per-dim names. This
    function's ``WHERE table_name = 'nexus.chunks'`` leg then matches no
    row, and ``sum`` legitimately returns NULL (psql renders an empty
    line) — callers MUST treat that as unmeasured, never assume it cannot
    happen. :func:`legacy_era_chash_conformance_statements` is the
    era-matched counterpart for that straddle window; see
    ``nexus.health._check_migration_state``'s era discriminator."""
    return tuple(
        f"SELECT sum(non_conformant) FROM {DIAG_CONFORMANCE_VIEW} "
        f"WHERE table_name = '{t.table}'"
        for t in POISON_CHASH_TABLES
    )


def legacy_era_chash_conformance_statements() -> tuple[str, ...]:
    """The view-path statements, but filtered by the PRE-RDR-191 per-dim
    table names (:data:`LEGACY_POISON_CHASH_TABLES`) instead of the unified
    set.

    RDR-191 F14a mirror-direction straddle in the poison gate (nexus-o8dil,
    2026-08-14): on a store whose engine has not yet run the migration
    that creates ``nexus.chunks``, the deployed ``nexus.diag_chash_
    conformance`` view — built by that OLDER engine's own provisioning —
    still carries rows keyed by ``chunks_384``/``chunks_768``/
    ``chunks_1024``/``catalog_document_chunks``, not ``nexus.chunks``.
    :func:`chash_conformance_statements`'s unified filter matches no row
    there (NULL aggregate, empty psql line); these statements are the
    era-matched query against the SAME view for that window. Callers
    choose between the two sets via an existence probe on ``nexus.chunks``
    (``nexus.health._check_migration_state``), never by guessing from a
    blank result — a genuinely poisoned unified store must not be
    misread as "must be legacy-era" just because a leg came back empty."""
    return tuple(
        f"SELECT sum(non_conformant) FROM {DIAG_CONFORMANCE_VIEW} "
        f"WHERE table_name = '{t.table}'"
        for t in LEGACY_POISON_CHASH_TABLES
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


def legacy_chash_conformance_statements(
    tables: tuple[ChashBearingTable, ...] = POISON_CHASH_TABLES,
) -> tuple[str, ...]:
    """The pre-A6 direct-table counts. The health probe FALLS BACK to these
    when the view is absent (an engine older than the A6 changeset, or —
    nexus-o8dil, 2026-08-14 — a straddle-era box where the view has never
    been provisioned at all) — the old grants era still carries full-table
    SELECT (the A6 ``grants-nexus-diag-2`` REVOKE only fires once the view
    EXISTS, so "view absent" and "direct SELECT still granted" go
    together), so the fallback works exactly as before; without it the
    install-binary gate would fail loud on every store one engine-
    generation behind. Poison-only: pre-A6 engines predate the
    telemetry-001 debt tables, so direct debt counts there would fail on
    missing relations. Same era-safe ``octet_length`` predicate as the view
    (see :func:`diag_conformance_view_ddl`).

    *tables* defaults to :data:`POISON_CHASH_TABLES` (the unified set,
    unchanged prior behavior). Pass :data:`LEGACY_POISON_CHASH_TABLES` for
    the RDR-191 straddle window's pre-unify per-dim shape — SAME direct-
    count mechanism, different table names, so the two eras cannot drift
    apart on the predicate itself."""
    return tuple(
        f"SELECT count(*) FROM {t.table} WHERE octet_length({t.column}) <> 32"
        for t in tables
    )


def chash_era_probe_statement(table: str = CHUNKS_TABLE) -> str:
    """A lint-legal, grant-free existence probe for *table* (nexus-o8dil,
    2026-08-14): ``to_regclass`` is a metadata function callable by ANY
    role regardless of the A6 ``grants-nexus-diag-2`` direct-table-SELECT
    revoke, and the statement has no ``FROM`` clause, so
    :mod:`nexus.remediation.sql_lint`'s aggregate-only content guard never
    applies to it (nothing to fail-closed on). Used by the pre-convergence
    poison probe as the era discriminator: ``nexus.chunks`` existing means
    the unified (post-RDR-191) statement set applies; its absence means
    either the pre-unify per-dim shape or a genuinely-unprovisioned store —
    the caller falls through to :data:`LEGACY_POISON_CHASH_TABLES` and,
    failing that too, the existing unverifiable/defer path."""
    return f"SELECT (to_regclass('{table}') IS NOT NULL)::int"


def parse_conformance_sum(
    tables: tuple[ChashBearingTable, ...], outputs: Sequence[str],
) -> int:
    """Defensively sum per-table diagnostic COUNT outputs against *tables*
    (nexus-o8dil, 2026-08-14 — RDR-191 F14a mirror-direction straddle in
    the poison gate, GH #1414-class recurrence). A bare
    ``sum(int(c) for c in outputs)`` turns ANY blank/NULL aggregate
    (era-mismatched view row, a partial result) into an opaque
    ``ValueError: invalid literal for int() with base 10: ''`` that names
    no table and gives the operator nothing to act on. This raises the
    SAME exception type (callers' existing ``except ... ValueError``
    degrade-cleanly handling is unchanged) but always NAMES the probed
    table, so "which table couldn't be verified" survives into the
    surfaced detail instead of being lost at the ``int()`` boundary."""
    if len(tables) != len(outputs):
        raise ValueError(
            f"conformance probe returned {len(outputs)} result(s) for "
            f"{len(tables)} statement(s) — a partial scan is not a clean store"
        )
    total = 0
    for t, raw in zip(tables, outputs):
        stripped = raw.strip()
        if not stripped:
            raise ValueError(
                f"empty/NULL conformance count for {t.table!r} — the "
                "counted view carries no row for that table (stale or "
                "era-mismatched view generation)"
            )
        try:
            total += int(stripped)
        except ValueError as exc:
            raise ValueError(
                f"non-numeric conformance count for {t.table!r}: {stripped!r}"
            ) from exc
    return total
