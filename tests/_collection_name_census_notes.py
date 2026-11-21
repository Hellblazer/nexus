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
    'The taxonomy__ half of the schema-bypass test, one site since P2.5 gathered the twelve into db/rows/lifecycle.py (P2.9 moves it to the row); the quarantine- half of the same test reads the row (P2.5).'
)
QUAR_NOROW: str = (
    "The quarantine- half of the schema-bypass test, for a name with no row or a row with no lifecycle_state only: a row's lifecycle_state decides when there is one (P2.5). Goes with the name parsers at P4."
)
ROUTE: str = (
    "Picks behaviour by the name's content-type prefix; P2.9 reads the row's content_type instead."
)
WM_P24: str = (
    "Taxonomy threshold keyed by the name's prefix; becomes a row read at P2.4."
)
WM_P22: str = (
    'RDR-204 (b) read-path model read from the name; becomes a resolve_collection Row read at P2.2.'
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
ROUTE_DEFAULT: str = (
    'Shipped default exclude pattern that is itself a prefix pattern; P2.9 replaces it.'
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
