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

stdlib-only by design so the dependency-light ``nexus.remediation`` package
can import it without pulling ``nexus.health``'s weight.
"""
from __future__ import annotations

#: pgvector chunk + manifest tables whose ``chash`` column is the
#: content-addressed identity (``sha256(chunk_text)[:32]``). A non-32-char
#: value here is the GH #1390 / nexus-pnwu0 poison class.
CHASH_BEARING_TABLES: tuple[str, ...] = (
    "nexus.chunks_384",
    "nexus.chunks_768",
    "nexus.chunks_1024",
    "nexus.chash_index",
    "nexus.catalog_document_chunks",
)


#: RDR-182 Amendment A6 (nexus-9bufb): the structural content boundary. A
#: superuser-owned counts view — the diagnostic role reads COUNTS BY
#: CONSTRUCTION (definer semantics + RLS exemption via the superuser owner),
#: never row content; the runAlways grants changeset revokes nexus_diag's
#: direct table SELECT once this view exists.
DIAG_CONFORMANCE_VIEW: str = "nexus.diag_chash_conformance"


def diag_conformance_view_ddl() -> str:
    """The view's DDL, generated from :data:`CHASH_BEARING_TABLES` so the
    view and the constant cannot drift (pinned by test). Executed by the
    SUPERUSER provisioning path (``pg_provision``) — under FORCE RLS a view
    counts cross-tenant rows only when its OWNER is RLS-exempt, which only
    the superuser context can arrange (the nexus-vounk lesson, structurally).
    Managed/DBA deployments get the rendered copy in docs/configuration.md.
    """
    union = "\nUNION ALL\n".join(
        f"SELECT '{t}' AS table_name, count(*) AS non_conformant "
        f"FROM {t} WHERE length(chash) <> 32"
        for t in CHASH_BEARING_TABLES
    )
    return f"CREATE OR REPLACE VIEW {DIAG_CONFORMANCE_VIEW} AS\n{union}"


def chash_conformance_statements() -> tuple[str, ...]:
    """One aggregate statement per chash-bearing table AGAINST THE COUNTS
    VIEW (Amendment A6) — same one-number-per-statement output shape as the
    legacy direct counts (the probe's parser is unchanged), same read-only
    aggregate-only shape the ``nexus_diag`` lint accepts. The view emits
    exactly one row per table unconditionally, so ``sum`` never returns
    NULL."""
    return tuple(
        f"SELECT sum(non_conformant) FROM {DIAG_CONFORMANCE_VIEW} "
        f"WHERE table_name = '{t}'"
        for t in CHASH_BEARING_TABLES
    )


def legacy_chash_conformance_statements() -> tuple[str, ...]:
    """The pre-A6 direct-table counts. The health probe FALLS BACK to these
    when the view is absent (an engine older than the A6 changeset) — the
    old grants era still carries full-table SELECT, so the fallback works
    exactly as before; without it the install-binary gate would fail loud on
    every store one engine-generation behind."""
    return tuple(
        f"SELECT count(*) FROM {t} WHERE length(chash) <> 32"
        for t in CHASH_BEARING_TABLES
    )
