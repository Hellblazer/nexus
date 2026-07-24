# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration package (RDR-155) — post-P4b survivor state.

The Chroma→pgvector ETL machinery (chroma_read, driver, sequencer,
vector_etl, orchestrator, verify_fill, wire re-id, ...) was deleted at
RDR-155 P4b together with the chroma substrate; installs needing the
migration install the LAST_MIGRATION_CAPABLE pinned release.

What remains:

- ``state``  — the cross-process migration sentinel (RDR-159); the
  hidden ``nx migration`` verb reads/clears it.
- ``banner`` — the read-surface warning while a sentinel is present.
- ``pg_read`` — the PG-source read leg (reconcile/verify tooling reads
  the pgvector store through it).
"""
