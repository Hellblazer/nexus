# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Per-site census ledger, part: the call sites of cross-module parser roots,
and the one regex parse outside ``corpus.py`` (RDR-229 P1.R fix round,
nexus-p2m1x.5).

P1.1's helper set came from ``corpus.py`` alone, so a call to ``resolve_corpus``
(its parse is ``startswith(f"{corpus}__")``) or to ``CollectionName.parse`` read
a name's parts with no ledger entry. ``tests/_collection_name_census_cross.py``
now derives every function that holds a name-decomposing site, or calls
``parse_conformant_collection_name``, and counts each call to one. These are the
21 sites that derivation found on develop 118701321: 8 the review named (5
``resolve_corpus`` callers, 3 ``CollectionName.parse`` callers), 12 more by the
same rule (the callers of ``shape_of``, ``_under``,
``target_collection_for``, ``_backfill_papers`` / ``_backfill_knowledge``, and the
one-off census's ``_parse``), and the one-off census's ``_QUARANTINE_RE``.
"""
from __future__ import annotations

from tests._collection_name_census_notes import DIAG_P12
from tests._collection_name_census_scan import Site as S

RC_MINT: str = (
    "Passes a user-typed --corpus value to resolve_corpus, which splits it into a candidate (content_type, owner, model) "
    "through split_candidate_collection_name (item 1: parsing the user's own input is mint), so resolve_corpus is a derived "
    "helper and the call is counted. The caller reads no name itself (RDR-229 P2.7, nexus-p2m1x.12)."
)
RC_LITERAL: str = (
    "Calls resolve_corpus with the literal bare content type 'knowledge', which stage 2 fans out by each candidate's "
    "row; it never reaches the typed-value split. Counted only because resolve_corpus is a derived helper; the caller "
    "reads no name itself (RDR-229 P2.7, nexus-p2m1x.12)."
)
CN_FALLBACK: str = (
    "P1.4 A's temporary empty-model_version fallback in version_from_for_tuple_row: parses the returned name through "
    "CollectionName.parse because an empty-version row carries no version. The fallback comes out once the P1.2 "
    "census reads zero empty-version rows; owner P2.2 (legacy_names carries the legacy rows); the fallback is removed once D2's cloud run completes (no bead owns it)."
)
CN_CANDIDATE: str = (
    "CollectionName.parse is the one sanctioned parse of a candidate name the write verbs receive; the collection may "
    "not be registered, so a row read would refuse it (item 1: mint)."
)
CN_DIAG: str = (
    "Reads the embedding model from a name through CollectionName.parse and falls to voyage_model_for_collection for a "
    "legacy name: a diagnostic of the name (RDR-204 (d)), the same rule as its fallback on the next line."
)
CA_DIAG: str = (
    "Calls shape_of, whose name decode is a diagnostic holdout (nexus-ft04v.21): it reads the row first and "
    "decodes the name only when the row has no content_type or owner."
)
UNDER: str = (
    "Calls _under, the segment match of a scope against a plan-corpus entry (``rdr`` under ``rdr__1-1__...``): the same "
    "mint-class name test as _under's own site."
)
TCF: str = (
    "Calls target_collection_for, which parses a collection name recorded on the SOURCE install of a recovery bundle "
    "(migration source, as its own sites)."
)
BACKFILL_TITLE: str = (
    "Calls a backfill pass that derives a document title from the collection name (a strip_prefix diagnostic, as the "
    "pass's own sites): the name is read for a label, not routed on."
)

SITES: tuple[S, ...] = (
    S('src/nexus/catalog/chunk_quarantine.py',
      ('# new raw parse sites; tests/test_collection_name_parse_census.py).', 'return shape_of({"name": name}).quarantine'),
      'primitive_call', 'diagnostic', CA_DIAG),
    S('src/nexus/catalog/rdr229/collection_row_census.py',
      ('#: ``quarantine-<origin name>``: the shape ``chunk_quarantine.quarantine_collection_name`` renders.', '_QUARANTINE_RE = re.compile(r"^quarantine-(?P<origin>.+)$", re.DOTALL)'),
      'regex_prefix', 'diagnostic', DIAG_P12),
    S('src/nexus/catalog/rdr229/collection_row_census.py',
      ('name = r["name"]', 'parsed, quarantine = _parse(name)'),
      'primitive_call', 'diagnostic', DIAG_P12),
    S('src/nexus/catalog/rdr229/collection_row_census.py',
      ('def _legacy_or_nonconformant(r: dict) -> bool:', 'return bool(r.get("legacy_grandfathered")) or _parse(r["name"])[0] is None'),
      'primitive_call', 'diagnostic', DIAG_P12),
    S('src/nexus/catalog/recovery_bundle.py',
      ('return resolved', 'return target_collection_for(rec["collection"], t3)'),
      'primitive_call', 'migration-source', TCF),
    S('src/nexus/catalog/recovery_bundle.py',
      ('try:', 'target_col = target_collection_for(collection, t3)'),
      'primitive_call', 'migration-source', TCF),
    S('src/nexus/catalog/sensitivity_doctor.py',
      ('try:', 'token = CollectionName.parse(collection).embedding_model'),
      'primitive_call', 'diagnostic', CN_DIAG),
    S('src/nexus/collection_shape.py',
      ('name=name,', 'attrs=shape_of(row),'),
      'primitive_call', 'diagnostic', CA_DIAG),
    S('src/nexus/collection_shape.py',
      ('without adding a second parse site. Never a routing decision."""', 'return shape_of({"name": name}).owner_id'),
      'primitive_call', 'diagnostic', CA_DIAG),
    S('src/nexus/commands/catalog.py',
      ('click.echo("Pass 2: Paper collections (docs__*)...")', 'paper_count = _backfill_papers(cat, t3, dry_run, repo_collections=repo_collections, writer=writer)'),
      'primitive_call', 'diagnostic', BACKFILL_TITLE),
    S('src/nexus/commands/catalog.py',
      ('click.echo("Pass 3: Knowledge collections...")', 'knowledge_count = _backfill_knowledge(cat, t3, dry_run, writer=writer)'),
      'primitive_call', 'diagnostic', BACKFILL_TITLE),
    S('src/nexus/commands/collection.py',
      ('from nexus.corpus import resolve_corpus  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost', 'candidates = resolve_corpus(name, [c["name"] for c in cols])'),
      'primitive_call', 'mint', RC_MINT),
    S('src/nexus/commands/search_cmd.py',
      ('for c in expanded_corpus:', 'matched = resolve_corpus(c, all_collections)'),
      'primitive_call', 'mint', RC_MINT),
    S('src/nexus/doctor_search.py',
      ('from nexus.corpus import resolve_corpus  # noqa: PLC0415 — deferred import; diagnostic dep only needed in this probe', 'return resolve_corpus(name, all_collections)'),
      'primitive_call', 'mint', RC_MINT),
    S('src/nexus/mcp/core.py',
      ('else:', 'fanned_out = resolve_corpus(part, all_names)'),
      'primitive_call', 'mint', RC_MINT),
    S('src/nexus/mcp/core.py',
      ('return store_read_scope(', 'collection, t3=t3, knowledge_collections=lambda: resolve_corpus("knowledge", _get_collection_names()),'),
      'primitive_call', 'mint', RC_LITERAL),
    S('src/nexus/plans/evidence.py',
      ('return collection == self.value', 'return _under(collection, self.value)'),
      'primitive_call', 'mint', UNDER),
    S('src/nexus/plans/evidence.py',
      ('for entry in entries:', 'if _under(entry, scope.value):'),
      'primitive_call', 'mint', UNDER),
    S('src/nexus/plans/evidence.py',
      ('kept.append(entry)', 'elif _under(scope.value, entry):'),
      'primitive_call', 'mint', UNDER),
    S('src/nexus/sensitive_write.py',
      ('try:', 'return CollectionName.parse(collection_name)'),
      'primitive_call', 'mint', CN_CANDIDATE),
)
