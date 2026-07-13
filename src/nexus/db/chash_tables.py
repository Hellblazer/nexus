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


def chash_conformance_statements() -> tuple[str, ...]:
    """One aggregate ``count`` statement per chash-bearing table — the
    read-only, metadata-only shape the ``nexus_diag`` lint accepts and the
    BYPASSRLS diagnostic role counts across every tenant (nexus-vounk)."""
    return tuple(
        f"SELECT count(*) FROM {t} WHERE length(chash) <> 32"
        for t in CHASH_BEARING_TABLES
    )
