# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Class reasons the per-site census ledger shares (RDR-229 P1.1,
nexus-p2m1x.1). Each constant is the note a group of sites carries beside its
class; a site with its own reason carries that text inline.

The letters (a) to (d) are RDR-204's four dispositions, which RDR-229
Technical Design item 1 maps onto its classes: (a) is mint, (d) is
diagnostic, (b) write-model resolution leaves the list as a row read and so
stays a ``holdover`` until its step lands, and (c) filtering dispatch
became row reads or fell under one of the others.
"""
from __future__ import annotations

MINT_A: str = (
    'RDR-204 (a): the name is a user argument, a rename target or one nx is about to register; no row exists yet. Empties at Phase 3.'
)
MINT_REG: str = (
    "Registration seam: derives the new row's content type and owner from the name because no row exists yet (RDR-204 (a)); the model it registers is the caller's resolved one where it has one (nx index repo: the row's, RDR-229 P2.3). Empties at Phase 3."
)
MINT_NOROW: str = (
    'Item 1 NoRow/Unreadable outcome: reads the row first and answers with the mint-class name derivation only when no row answers. Stays until P3.8.'
)
MINT_ROUTE: str = (
    "Item 1 NoRow/Unreadable outcome at a router (RDR-229 P2.9): the row's content_type decides, and the name's own `<type>__` segment (corpus.routing_content_type / candidate_content_type) answers only for a collection with no row. Empties at P3.8."
)
MIG_BUNDLE_STEM: str = (
    "RDR-229 owner-is-source: reads a collection name's owner stem (`<slug>-<hash8>`, the repo owner's repo_hash) from a name no row of this catalog backs: a name from another install's .nxexp bundle, or a name of an older era's collections on this install (the code__ collections whose stem is compared), so a migration source (RDR-229 item 1). The parse itself is legacy_names.owner_stem (the floor, moved there from nxexp_owner.py; P2.2 missed it), so the sites here are the two callers in nxexp_owner.py; P4.4 removes those with the old-bundle import."
)
DIAG_D: str = (
    'RDR-204 (d): its purpose is to report or discover a name the catalog row does not back, so a row read would make it vacuous.'
)
DIAG_P12: str = (
    'RDR-229 P1.2 (nexus-p2m1x.2): the one-off collections-census measures how many rows disagree with their names, so it must parse the name to compare; a row read would make the measurement vacuous. A measurement tool, deleted before Phase 4 removes the parser (P1.R / Phase 4).'
)
MIG_SRC: str = (
    'Migration source: the name comes from another install or an older era.'
)
BYPASS: str = (
    'The taxonomy__ half of the schema-bypass test, one site since P2.5 gathered the twelve into db/rows/lifecycle.py: a legacy name no row backs and no row can tell apart, so a migration source (RDR-229 P2.9 ruling). P2.2 (nexus-p2m1x.7) moves it into legacy_names; the quarantine- half of the same test reads the row (P2.5).'
)
TAXONOMY_LEGACY: str = (
    'A taxonomy__ name test: a legacy name no row backs and no row can tell apart, so a migration source (RDR-229 P2.9 ruling). It reads through legacy_names.is_taxonomy_collection_name (RDR-229 P2.2, nexus-p2m1x.7).'
)
QUAR_NOROW: str = (
    "The quarantine- half of the schema-bypass test, for a name with no row or a row with no lifecycle_state only: a row's lifecycle_state decides when there is one (P2.5). Goes with the name parsers at P4."
)
MINT_QUAR: str = (
    "Mints the quarantine sibling's name from the origin's; the row is preferred and the name read only when the origin has none (RDR-204 coordinator ruling 2026-09-08)."
)
MINT_PARSE: str = (
    "The render path's parse counterpart (RDR-204); empties at Phase 3."
)
MINT_T3: str = (
    'Builds the candidate name `<user_arg>__` to find an existing collection for a bare content type (item 1: mint).'
)
LABEL: str = (
    'User-configured name pattern matched against a collection name: it names labels, not a prefix (item 1).'
)
FILEPATH: str = (
    'Matches source file paths, not collection names.'
)
LABEL_TYPED: str = (
    'RDR-229 P2.7 (nexus-p2m1x.12): resolve_corpus stage 3b, after the row tuple (stage 3a) finds nothing. '
    'It matches the label string the user typed (`<value>__` as a name prefix) and decomposes nothing; it stays because a '
    "repo row's owner_id is the tumbler segment, so code__<slug>-<hash8> matches no tuple. A prefix match "
    'whose own row is not live (or superseded) is dropped by it; a name with no row is kept.'
)
MINT_T3DB: str = (
    'T3Database is the test-only Chroma-era facade (it dies with the chromadb dependency, RDR-155 P3) and holds no catalog, so the NoRow outcome is the only one it can have: the model comes from the name (RDR-229 item 1).'
)
DIAG_DRIFT: str = (
    "doctor_search's model_drift probe compares what the NAME says (this site) with the model the collection reports, which is the row's since P2.2 (HttpVectorClient.collection_metadata); a row read here would make the probe vacuous (RDR-204 (d))."
)
MIG_BLANK: str = (
    "RDR-229 P2.2: a registry row with a blank model_version takes the version from its conformant label (legacy_names.version_of_blank_row_label) until D2 (nexus-f3yqd.2.23) completes; the label is from an older era. Removed once D2's cloud run completes (no bead owns it, RDR-229 Phase 1.4)."
)
