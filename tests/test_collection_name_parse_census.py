# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Client census of raw collection-name parsing (RDR-204 Phase 3 item 1,
nexus-ft04v.20). ACCEPTANCE SIGNAL 2 of 2.

THE BUG CLASS THIS FREEZES. Collection identity (content type, model,
lifecycle state) has moved to ``catalog_collections`` as the single source
of truth (RDR-204 Phase 1/2). Client code that still reaches for the
metadata by string-splitting the collection NAME reads a fact the row
already carries, and drifts from the row the moment a name and its row
disagree (the exact defect class RDR-204 exists to close). This gate does
not fix a single site — Phase 3 items 2-5 (the funnel slices, nexus-ft04v.21
through .23) do that, each lowering the pin here. This gate exists FIRST,
pinned at the count measured BEFORE any of those slices land, so every
later slice's acceptance is "the pin fell", not a claim nobody can check.

FIVE PATTERN CLASSES. The original four, per RDR-204 Phase 3 item 1
(docs/rdr/rdr-204-embedding-profile-and-collection-authority.md, sections
"Implementation Plan / Phase 3" and "Validation / Testing Strategy"):
``split("__")`` / ``rsplit("__")``, ``partition("__")`` / ``rpartition("__")``,
``startswith(<type>__ or "quarantine-")`` / ``endswith(...)`` (bare or in a
tuple), and ``"__" in x`` / ``"__" not in x``. A FIFTH, added by
nexus-ft04v.26 (THE REPOINT, coordinator ruling 2026-09-09): a bare call
to ``split_candidate_collection_name`` or ``embedding_model_for_collection_name``
OUTSIDE ``corpus.py``'s own helper bodies -- once the funnel helpers
stopped parsing the name and started reading the catalog row, "parsing
that moves into one helper with N callers while the gate reads zero" is
the exact failure this class exists to close: those two functions are
STILL parsing (deliberately, for the callers that genuinely cannot use a
row -- see ``COLLECTION_NAME_PARSE_CENSUS``'s own docstring for the
classification), so a caller of either is doing what the first four
classes already caught, just relocated behind a function call. All five
are AST-matched (``ast.Call`` / ``ast.Compare`` nodes), not grepped — a
comment or docstring containing the string ``"__"`` or either function
name must never count.

WHY AST, NOT GREP. The originating bead's own prior grep measured 63 sites
under the first three classes and needed correcting twice: once because it
missed the fourth class entirely (at least two live sites, including
commands/store.py:672, are matched ONLY by ``"__" in``), and once because
four of the 63 parse ``mcp__`` TOOL names, not collection names, and were
never filtered. A grep re-measurement drifts the same way again the next
time the tree moves; an AST walk with an explicit, tested exclusion list
does not.

RE-MEASURED ON THIS TREE (2026-09-09, commit a821d6622): the four classes
match 83 raw sites across 27 files. FOUR are excluded (all four rsplit(__)
sites in the entire tree, and only those) because they strip an
``mcp__...__`` tool-name prefix, not a collection name -- see
``_EXCLUDED_SITES`` below, each with its own reason and each independently
verified (``test_excluded_sites_are_real_matches_not_omissions``) to be a
genuine raw match rather than a stale or invented entry. The pin was
79 == 83 - 4 at that baseline.

nexus-ft04v.21 (Phase 3 funnel slice 1, corpus.py/scoring.py/
collection_shape.py/context.py/search_engine.py/exporter.py/mcp/core.py)
landed 2026-09-09 and lowered the pin from 79 to 54 (-25): scoring.py,
context.py, search_engine.py, exporter.py and mcp/core.py reached zero
and were dropped; corpus.py fell from 16 to 2 (the two helpers' own
shared internal parse is the accepted floor they leave behind).
collection_shape.py's 3 sites (188, 191, 195) were LEFT UNFUNNELLED, not
overlooked, per the coordinator's ruling on nexus-ft04v.21's hand-off:
``collection_attributes``'s positional 4-field decode of a malformed name
cannot be reproduced byte-for-byte by the funnel helpers' "owner = whole
remainder" convention. These stay raw and counted -- nexus-ft04v.26 (THE
REPOINT) landed the row-based helpers and the fifth pattern class but did
NOT close this holdout; it remains open (see COLLECTION_NAME_PARSE_CENSUS's
per-site comment and the bead's hand-off report).

nexus-ft04v.23 (Phase 3 funnel slice 3, the commands/ CLI surface)
funnelled 32 of the 54-site nexus-ft04v.21 baseline to the three
``CollectionName`` helpers, lowering the pin to 22. One site,
``src/nexus/commands/collection.py``'s ``reindex_cmd`` (the ``corpus =
name.split("__", 1)[1] if "__" in name else ""`` re-index provenance
label), was deliberately left raw rather than funnelled through
``collection_owner()`` -- confirmed by the coordinator's ruling on this
bead's hand-off report -- see the comment on that file's census entry
below for why the two are not equivalent for a genuinely conformant
4-segment name -- nexus-ft04v.26 (THE REPOINT) did NOT close this
holdout either; it remains open at its shifted line (see
COLLECTION_NAME_PARSE_CENSUS's per-site comment).

nexus-ft04v.22 (Phase 3 funnel slice 2, db/ and catalog/) funnelled 9 of
the 15 sites in its slice (embed_migrate.py:94/:262, http_vector_client.
py:836/:869/:3680, t3.py:77/:1204, orphan_backfill.py:322, recovery_
bundle.py:379), lowering the pin from 22 to 13. Three files' sites were
LEFT RAW, matching the SAME excluded shape as commands/collection.py's
``reindex_cmd`` site above (confirmed by the coordinator's ruling on
this bead's hand-off report):

- ``db/reconcile.py``'s ``_is_same_model_passthrough`` (:267),
  ``_dim_for_collection`` (:362), and the passthrough ``declared_model``
  read (:610) all use a LAXER `len(name.split("__")) == 4` count-based
  check than `is_conformant_collection_name` (which
  `collection_model()`/`collection_content_type()` require internally):
  an owner segment with a single underscore (e.g. ``"my_repo"``) still
  produces 4 segments under a literal ``"__"`` split but FAILS the
  conformant regex (``[a-zA-Z0-9-]+``, no underscore) -- funnelling would
  NARROW these functions from accepting such a name to rejecting it,
  forbidden by the bead.
- ``db/embed_migrate.py``'s ``migrate_collection_safe`` corpus derivation
  (:387, two hits: the ``split`` and the ``"__" in`` check on the same
  line) needs the OWNER **and** MODEL/VERSION tail together for a
  conformant name -- byte-identical to commands/collection.py's
  ``reindex_cmd`` site, right down to the source line's shape.
- ``catalog/chunk_quarantine.py``'s ``quarantine_collection_name`` (:69,
  AT THE TIME) needed the same full post-content-type tail preserved (to
  build an exact-model/version-matching quarantine sibling name); none of
  the three helpers exposed a combined owner+model+version read. This one
  was CLOSED by nexus-ft04v.26 item 6 (below) -- it now reads the row.

The other two stay counted -- nexus-ft04v.26 (THE REPOINT) did NOT resolve
them against the catalog row's columns; they remain open (see
COLLECTION_NAME_PARSE_CENSUS's per-site comments).

nexus-ft04v.26 (THE REPOINT) landed 2026-09-09: the three funnel helpers
(collection_content_type/collection_owner/collection_model) now read the
catalog row and fail loud on a name with none; resolve_corpus/
_resolve_corpus_target/_group_collections_by_model repointed to the row
+ lifecycle_state; corpus.py's own 4-class floor dropped from 2 to 0 (its
shared string-shape primitive, renamed split_candidate_collection_name,
is now regex-based); and the fifth pattern class (above) was ADDED per
the coordinator's ruling, raising the pin from 13 to 50 -- an increase
that reflects the gate becoming HONEST about a parsing surface it
previously could not see (the two primitives' external callers), not a
regression. Item 6 of this SAME bead then gave TWO of the five original
holdouts the row-read treatment, lowering the pin from 50 to 49:

- ``catalog/chunk_quarantine.py``'s ``quarantine_collection_name`` now
  PREFERS the origin's catalog row (content_type/owner_id/
  embedding_model) and falls to ``split_candidate_collection_name`` (one
  primitive_call, still counted) only when the origin has no row -- see
  that file's census-entry comment for the full design note. (The
  paired indexer.py pre-registration of the quarantine sibling that
  landed with it was deleted 2026-09-09 -- the engine registers the
  sibling from the origin's row on first insert, and the client-side
  row was the nexus-syfes drift the shakeout's Phase E caught.) Site
  count unchanged (1 -> 1, a primitive_call instead of a raw split).
- ``db/reconcile.py``'s ``_is_same_model_passthrough`` and
  ``_dim_for_collection`` now share a new ``_model_for_collection``
  helper that prefers the row and falls to the same laxer name-split
  only when there is none; the third site (the passthrough
  ``declared_model`` read) now calls that same helper instead of
  re-parsing the name itself, closing it entirely. Site count: 3 -> 2
  (see that file's census-entry comment for the full design note).

The fixture-seam fix round (same bead, same day) then raised the pin
from 49 to 50: ``health.py``'s ``check_chash_conformance_report``
unroutable-collection probe was calling ``collection_model`` (class b,
strict row-read, raises ``CollectionNotRegisteredError`` when a
collection has no row) for a scan whose whole POINT is to find
conformant-SHAPED collections that may have no row at all -- the raise
landed inside this method's blanket ``except Exception`` and silently
zeroed the entire probe the instant one such collection existed,
masking the nexus-4ijv4 false-clean-by-omission finding it exists to
catch. Fixed by reading ``embedding_model_for_collection_name`` (class
d, the name-parser this diagnostic's purpose actually calls for)
instead -- a genuinely NEW tracked site, not a reclassification of an
existing one, so the pin rises rather than holds.

The SAME fixture-seam round then raised it again, 50 to 51:
``per_collection_chunk_cap``/``_upsert_byte_budget``'s original
``_model == "voyage-context-3"`` CCE-vs-code dispatch was itself a real
regression -- comparing the embedding MODEL string instead of
content_type broke a structurally-CCE ``docs``/``knowledge``/``rdr``
collection whose row or name carried any non-canonical model token
(found live: ``test_per_collection_chunk_cap_values``'s own
fixture-only tokens "x"/"onnx-x", which were legitimate under the
pre-repoint prefix-based dispatch). Fixed by adding
``_is_cce_collection``, a new shared helper that reads content_type
(row-preferred, falling to ``split_candidate_collection_name`` for the
no-row case) instead of the model string -- a fourth, genuinely new
tracked site in ``http_vector_client.py``, not a reclassification.

The Phase 3 FIX ROUND (nexus-ft04v.28 item 6, 2026-09-09) then raised
the pin 51 to 53: the fifth class's primitive set moved from a
hand-duplicated literal in THIS file to ``nexus.corpus.
_CANDIDATE_STRING_PRIMITIVES``, single-sourced so a rename can no
longer silently stop matching (the failure mode the critique flagged --
"so a renamed parsing primitive evades it"). That audit surfaced a
genuinely uncounted THIRD primitive of the exact same shape as the
other two -- ``model_version_for_collection_name``, regex-based
(``_CONFORMANT_COLLECTION_RE``), never carried by the row cache -- with
TWO already-live callers in ``catalog/chunk_quarantine.py`` (one
pre-existing, in ``quarantine_collection_name``; one new, in this same
bead's C1 fix, ``quarantine_registration_kwargs``) that the old
two-name hardcoded set could never have seen. ``is_conformant_
collection_name``/``parse_conformant_collection_name`` were considered
and deliberately excluded -- see ``_CANDIDATE_STRING_PRIMITIVES``'s own
docstring in corpus.py for why they are the sanctioned public API, not
a drift risk the way the three primitives are. Attribute-form calls
(``corpus.split_candidate_collection_name(x)``) are now ALSO matched,
not just bare-name imports -- the boundary
``test_scanner_is_not_vacuous`` used to pin as "must NOT match" is now
pinned the other way, deliberately.

2026-09-09, 53 to 52: ``quarantine_registration_kwargs`` and the
client-side sibling pre-registration it fed were deleted (the engine
registers the sibling from the origin's row on first insert; the
client's row was the nexus-syfes drift the shakeout's Phase E caught).

The remaining THREE holdouts (collection_shape.py's 3,
commands/collection.py's reindex_cmd, db/embed_migrate.py's
migrate_collection_safe) were NOT closed by this bead -- they remain
exactly as nexus-ft04v.21/.22/.23 left them, tracked as open work in
the bead's hand-off report.

Item 3 of this SAME bead (RDR Technical Design 1a's "profile-as-data")
is UNRELATED to this file's own parse-site count (it never touches a
collection NAME) but its design history is recorded here since the
Phase 3 critique cross-walks the whole bead against Technical Design
1a: a first pass (commit 5935b1bf8) read the engine's
``nexus.embedding_profile`` directly inside
``corpus.effective_embedding_model_for_writes`` -- the client's
write-model CHOKEPOINT, reached from every write path. A full-suite
run measured 155 failures across unrelated test files (test_indexer.py,
test_search_cmd.py, test_store_cmd.py and ~15 more) that mock only the
db/T3 layer and never anticipated that chokepoint making a real network
call. Coordinator design correction (2026-09-09): the reds were the
design saying the chokepoint must stay pure, not evidence of
under-fixtured tests -- a conftest-level stub to paper over 155 tests
would itself be the silent fallback RDR-204 forbids. The profile
comparison moved to the REGISTRATION SEAM instead
(``corpus.ensure_collection_registered``, immediately before its
``writer.register_collection`` call, where a catalog client is already
about to be used for real I/O and the model is about to be committed);
``effective_embedding_model_for_writes`` reverted to pure local
computation (``_write_intent_embedding_model``, delegated to
unconditionally). The engine's own register-time 422 on a mismatch
remains the correctness guard for every registration call site OUTSIDE
this one funnel, until nexus-ft04v.27 consolidates them. Tests:
``tests/test_collection_registration.py``'s
``TestRegistrationSeamProfileCheck`` (agree / mismatch / empty-profile
bootstrap / pre-Phase-2 route-missing propagation).

RDR-204'S OTHER NAMED EXCLUSION -- ``rdr-`` document ids -- MATCHES ZERO
SITES HERE. The one ``rdr-`` id parse in the tree,
``catalog/rdr_canonical.py:124``'s ``name.lower().startswith("rdr-")``,
uses the literal ``"rdr-"`` (single hyphen), which is not in
``_TYPE_PREFIXES`` (``"rdr__"``, double underscore, is a collection prefix;
``"rdr-"`` is an RDR document id) -- it was never going to be picked up by
this scanner's ``startswith``/``endswith`` matcher in the first place. This
is recorded as a checked zero, not a silent omission
(``test_rdr_id_parse_is_not_a_scanner_hit``), exactly the "kept at 0 rather
than dropped" discipline of the private-handle census this gate is modelled
on (``tests/test_private_handle_access_census.py``).

WHY A CENSUS-IN-TEST-FILE RATHER THAN A ``src/`` LINT MODULE (the
alternative the bead names, ``storage_boundary_lint.py``-style). Nothing in
``src/nexus`` needs to call this scanner at runtime or share it across
modules -- it exists only to be asserted against in this file, exactly like
``tests/test_private_handle_access_census.py``. A ``src/`` module would add
an import surface with no production consumer.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "nexus"

#: Collection-type / lifecycle prefixes a raw ``startswith``/``endswith``
#: call names when it is parsing a collection NAME rather than checking an
#: unrelated string. Per RDR-204 Phase 3 item 1's four pattern classes.
_TYPE_PREFIXES = frozenset({"code__", "docs__", "rdr__", "knowledge__", "quarantine-"})

#: (relative-path, lineno) -> one-line reason, for sites the raw AST scan
#: WOULD flag but which the RDR-204 Phase 3 item 1 exclusion rule (``mcp__``
#: tool names, ``rdr-`` document ids) removes from the pin. Every entry here
#: is independently verified as a real raw match by
#: ``test_excluded_sites_are_real_matches_not_omissions`` — an exclusion
#: that no longer matches anything is a stale entry, not a quieter gate.
_EXCLUDED_SITES: dict[tuple[str, int], str] = {
    # Line moved 7887 -> 7900 (nexus-ft04v.21's funnel of this file's other
    # three sites) -> 7934 (nexus-ft04v.26's THE REPOINT: _resolve_corpus_target/
    # _group_collections_by_model rewritten, _collection_family_prefix added).
    ("src/nexus/mcp/core.py", 8025): (
        "mcp__ tool name: `raw_tool.rsplit(\"__\", 1)[-1] if "
        'raw_tool.startswith("mcp__")` strips an MCP tool-name prefix for '
        "planner-step normalization, not a collection name."
    ),
    ("src/nexus/plans/bundle.py", 230): (
        "mcp__ tool name: `_extract_tool_name`'s own docstring says it "
        '"strips any mcp__...__ prefix" from a plan step\'s tool identifier.'
    ),
    ("src/nexus/plans/cost_estimate.py", 447): (
        "mcp__ tool name: `_extract_tool`, the documented local copy of "
        "bundle._extract_tool_name, strips the same mcp__...__ prefix."
    ),
    ("src/nexus/plans/runner.py", 2377): (
        "mcp__ tool name: strips an mcp__...__ prefix from a resolved plan "
        "step's tool identifier before dispatch, guarded by the same "
        '`startswith("mcp__")` check as the other three sites.'
    ),
}

#: 2026-09-09 census of collection-name parse sites, per file, AFTER the
#: `_EXCLUDED_SITES` filter. Pin sum: 13 (nexus-ft04v.21 lowered the
#: 79-site 2026-09-09 baseline to 54; nexus-ft04v.23 funnelled a further
#: 32 of those 54 -- the commands/ CLI surface -- lowering the pin to
#: 22; nexus-ft04v.22 funnelled 9 more -- the db/ and catalog/ surface --
#: lowering the pin to 13). This may only shrink — each of
#: nexus-ft04v.21/.22/.23 (the funnel slices) lowers the entries for its
#: files as raw sites move to the three CollectionName helpers; when a
#: file reaches zero, drop its entry (a dropped key and an absent file
#: both read as zero to the guards below).
#:
#: nexus-ft04v.21 (funnel slice 1, corpus.py/scoring.py/collection_shape.py/
#: context.py/search_engine.py/exporter.py/mcp/core.py) landed 2026-09-09:
#: scoring.py, context.py, search_engine.py, exporter.py and mcp/core.py
#: reached zero and were dropped; corpus.py fell from 16 to 2 (the two
#: `collection_content_type` / `collection_owner` helpers' own shared
#: internal parse -- see `_split_legacy_collection_name` in nexus/corpus.py
#: -- is the accepted floor those two helpers leave behind, per the bead's
#: "sites inside the three helpers themselves ... stay counted" rule).
#: collection_shape.py's 3 sites (188, 191, 195) were LEFT UNFUNNELLED, not
#: overlooked, per the coordinator's ruling on nexus-ft04v.21's hand-off
#: report: `collection_attributes`'s positional 4-field decode of a
#: malformed name is the same class as the commands/collection.py site
#: below -- the funnel helpers' "owner = whole remainder" convention
#: cannot reproduce a strict positional decode byte-for-byte, and this
#: bead forbids behaviour change. These stay raw and counted until
#: nexus-ft04v.26 (the repoint), where the decode reads the catalog row
#: directly instead of parsing.
#:
#: nexus-ft04v.26 (THE REPOINT) landed 2026-09-09 and added the FIFTH
#: pattern class (coordinator ruling: a caller of split_candidate_collection_name/
#: embedding_model_for_collection_name outside corpus.py's own helper
#: bodies re-derives the same fact the four original classes did, just
#: behind a function call -- "parsing that moves into one helper with N
#: callers while the gate reads zero" is exactly the failure this closes).
#: corpus.py's own floor (2, the two funnel helpers' shared internal
#: parse) dropped to 0: `split_candidate_collection_name` (renamed from
#: `_split_legacy_collection_name`) is now REGEX-based
#: (`_LEGACY_SPLIT_RE.match`), which is invisible to the ORIGINAL four
#: classes by construction (a regex `.match()` call is none of split/
#: rsplit/partition/rpartition/startswith/endswith/"__" in) -- the
#: mechanism moved, so the site the four original classes tracked
#: genuinely no longer exists there. The 38 primitive_call sites below are
#: every caller of those two functions outside corpus.py, each classified
#: per the coordinator's four dispositions (see the per-site comments in
#: source for the reason; a-d recorded here for the census reader):
#:   a) mint/candidate-time (no row can exist yet -- registering, minting,
#:      renaming, or dispatching on a --collection/--corpus argument that
#:      may not name a real collection at all)
#:   b) write-model resolution the write authority (effective_embedding_model_for_writes/
#:      resolve_write_embedding_model) cannot serve without a content_type
#:      already in hand (batch-sizing dispatch); reads the row first,
#:      falls to the conformant name's own embedded model token (the
#:      write authority's decision at render time, read back rather than
#:      re-derived) only when no row exists
#:   c) filtering/best-effort dispatch over a list where a row-absent
#:      result is simply excluded, not parsed -- converted to a direct
#:      nexus.mcp_infra.get_collection_row read wherever that was possible
#:      without losing coverage; the residual primitive_call sites below
#:      are the ones where a plain string comparison remained necessary
#:      (e.g. matching against the STRING VALUE of a --corpus token)
#:   d) backfill/reconcile/doctor diagnostics whose purpose IS to report a
#:      name-versus-row disagreement or discover an unregistered
#:      collection -- the RDR's own named exception
COLLECTION_NAME_PARSE_CENSUS: dict[str, int] = {
    # nexus-ft04v.26 item 6: `quarantine_collection_name` (RDR-204 Phase 3
    # THE REPOINT) no longer raw-splits -- it now PREFERS *origin*'s
    # catalog row (content_type/owner_id/embedding_model, authoritative
    # per Gap 1) and falls to the shared candidate-string primitive
    # `split_candidate_collection_name` (one primitive_call, counted here)
    # only when *origin* has no row. Live-tested against the real GC
    # integration suite (tests/test_rdr191_gc_serverside_prune.py): a
    # fixture collection there frequently has NO catalog row (never
    # registered before its first GC pass), so this deliberately does NOT
    # fail loud on a missing row the way collection_content_type/
    # collection_owner/collection_model do -- one unregistered origin must
    # not abort the whole GC sweep. This is the coordinator's own named
    # exception for "chunk_quarantine's sibling minting IF IT MUST"
    # (ruling 2026-09-08). The fallback branch still preserves the ENTIRE
    # tail after the content-type segment (owner + model + version) so the
    # quarantine sibling keeps exactly the source's model/version --
    # `collection_owner()` alone would discard model/version.
    #
    # nexus-ft04v.28 item 6 (this file, 1 -> 2): the fifth class's
    # primitive set gained `model_version_for_collection_name` (previously
    # an uncounted third primitive, same regex-based shape as the other
    # two -- see nexus.corpus._CANDIDATE_STRING_PRIMITIVES's own docstring
    # for why it belongs). One class-(d) site: `quarantine_collection_
    # name`'s call (line ~91, reading the origin's own model_version
    # segment -- the catalog row never carries a version field, so this is
    # the only place that fact lives, same allowance as the primitive_call
    # above it). The C1 fix's `quarantine_registration_kwargs` (a second
    # such call) was deleted 2026-09-09 with the client-side sibling
    # pre-registration it fed: the engine registers the sibling from the
    # origin's row on first insert, and the client's row was the
    # nexus-syfes drift the shakeout's Phase E caught (3 -> 2).
    "src/nexus/catalog/chunk_quarantine.py": 2,
    # class (d): _content_type_for_collection synthesizes a row for the
    # orphan GC backfill -- the collection being registered has no row by
    # construction.
    "src/nexus/catalog/orphan_backfill.py": 1,
    # (a) mint/candidate-time: resolve_owner_scope's _is_corpus_scope reads
    # the first segment of a user-typed --scope / corpus token to decide
    # whether it names a corpus at all (GH #1527 review fix, batch-2).
    "src/nexus/catalog/owner_scope.py": 1,
    "src/nexus/collection_shape.py": 3,
    # class (d): _backfill_knowledge/_backfill_rdrs/_backfill_papers (3
    # sites) find and register T3 collections NOT YET in the catalog;
    # :1421/:1427/:1455 recover-per-file reads a possibly-broken
    # collection's shape from the string, not a row it may not have;
    # :1613/:1614 `register --from-t3` discovers unregistered collections
    # to register. All 8 are the RDR's named backfill-and-doctor
    # exception.
    "src/nexus/commands/catalog.py": 8,
    # class (d): _run_name_vs_embed_dim compares what the NAME claims
    # against the actual embedding dim (a name-vs-reality diagnostic; a
    # row read would make it vacuous by construction under Phase 1).
    "src/nexus/commands/catalog_cmds/doctor.py": 1,
    # class (d): reports a catalog document whose physical_collection may
    # itself be drifted/unregistered.
    "src/nexus/commands/catalog_cmds/integrity.py": 1,
    # class (d): reconcile-stale's whole purpose is diagnosing drifted
    # catalog entries, which can reference a physical_collection with no
    # row (2 sites: the no-provenance classifier and the single-chunk
    # heuristic).
    "src/nexus/commands/catalog_cmds/reconcile_stale.py": 2,
    # :525/:526 rename_cmd, class (a): `new` is the rename TARGET, no row
    # yet; `old` may be a never-registered legacy collection this rename
    # is meant to fix. :371, class (d): _find_dimension_mismatched_collections
    # compares the NAME's declared dim against the active embedder (same
    # shape as catalog_cmds/doctor.py:980 above). :713/:787/:816
    # reindex_cmd, class (a): the row was just deleted by
    # purge_collection_cascade and only best-effort re-registered, so
    # every content-type dispatch in that function must not assume a row.
    # :780 (2 hits, "in"+"split") is nexus-ft04v.23's ORIGINAL raw
    # holdout -- see its own comment below.
    "src/nexus/commands/collection.py": 8,
    # nexus-ft04v.23: `corpus = name.split("__", 1)[1] if "__" in name
    # else ""` in `reindex_cmd` is deliberately left raw. It derives a
    # re-index provenance label from EVERYTHING after the first "__", not
    # just the owner segment -- for a genuinely conformant 4-segment name
    # (`<type>__<owner>__<model>__v<n>`) that differs from
    # `collection_owner()` (which returns only the second segment), so
    # funnelling it would silently truncate the label instead of leaving
    # behaviour byte-identical. Confirmed by the coordinator's ruling on
    # nexus-ft04v.23's hand-off report; nexus-ft04v.26 (THE REPOINT) did
    # not touch this ONE site (item 6 in the bead's own list) -- open
    # work, tracked in the hand-off report.
    # class (d): the census census, prefers the row (already fetched for
    # this same fan-out-floor scan) and falls to candidate-string
    # derivation only for a genuinely unregistered collection this
    # diagnostic must still report on.
    "src/nexus/commands/doctor.py": 1,
    # class (a): `collection` reaches here because select_config already
    # rejected it as unsupported -- may be a mistyped --collection
    # argument with no row at all.
    "src/nexus/commands/enrich.py": 1,
    # class (d) (nexus-ft04v.26, fixture-seam fix round): check_chash_
    # conformance_report's unroutable-collection probe reads
    # embedding_model_for_collection_name(name) directly -- the whole
    # POINT of this scan is to report on a conformant-SHAPED collection
    # /v1/vectors/stats lists that may carry no catalog row at all
    # (unregistered, or a legacy pre-Phase-1 collection); the strict
    # row-reading collection_model (class b) it previously called raised
    # CollectionNotRegisteredError for exactly that case, silently
    # zeroing the whole scan inside this method's blanket `except
    # Exception` and masking the false-clean-by-omission finding this
    # probe exists to catch (nexus-4ijv4).
    "src/nexus/health.py": 1,
    # :113 class (a), registering `new_name`; :1053/:1062 class (a),
    # first-index synthesis explicitly for the not-yet-registered case;
    # :1755 class (a), the function's OWN docstring defines "kind" as the
    # name prefix, not a catalog fact; :2940 class (a), `collection` is
    # the just-minted candidate from t3_collection_name on the line above.
    "src/nexus/commands/index.py": 5,
    # class (a): `collection` is the raw --collection CLI argument for
    # `nx store export`, not necessarily an existing registered name.
    "src/nexus/commands/store.py": 1,
    "src/nexus/corpus.py": 0,
    # class (d)/(a): _classify (:108, prefers the row, falls to
    # candidate-string for the unregistered-legacy population migration
    # exists to handle) and _default_reindex's target_name check (:285,
    # class (a) -- the migration TARGET, model-swapped from an
    # already-conformant old name, has no row by construction). :403 (2
    # hits) is nexus-ft04v.22's ORIGINAL raw holdout -- see its own
    # comment below.
    # nexus-ft04v.22: :387 (now :403 after nexus-ft04v.26's edits)
    # `migrate_collection_safe`'s `corpus = stale.name.split("__", 1)[1]
    # if "__" in stale.name else ""` (two hits: the split and the "__" in
    # check) is deliberately left raw -- byte-for-byte the SAME shape as
    # commands/collection.py's reindex_cmd site above, same reason: needs
    # owner+model/version together, which `collection_owner()` cannot
    # reproduce for a conformant name. Confirmed by the coordinator's
    # ruling on nexus-ft04v.22's hand-off report; nexus-ft04v.26 did not
    # touch this ONE site (item 6) -- open work, tracked in the hand-off
    # report.
    "src/nexus/db/embed_migrate.py": 4,
    # class (b): per_collection_chunk_cap/_upsert_byte_budget size the
    # write that will create a collection's FIRST-EVER chunks, so they
    # structurally cannot rely on a row in the common first-write case.
    # _write_model_for_collection prefers the row (the re-write case,
    # authoritative) and falls to embedding_model_for_collection_name
    # (reading the model the write authority already embedded in an
    # already-rendered conformant name at mint time, not re-deriving it)
    # only when no row exists; :2364 is an existing, pre-nexus-ft04v.26
    # call unrelated to this bead's own additions. A fourth site,
    # _is_cce_collection (fixture-seam fix round, 2026-09-09), was added
    # to REPLACE per_collection_chunk_cap/_upsert_byte_budget's original
    # ``_model == "voyage-context-3"`` model-STRING comparison, which was
    # a real regression: content_type, not the model token, is the
    # correct CCE-vs-code signal (fragile to any model rename or
    # non-canonical fixture token otherwise) -- it falls to
    # split_candidate_collection_name (a DIFFERENT primitive than
    # embedding_model_for_collection_name above) for the same no-row
    # first-write case.
    "src/nexus/db/http_vector_client.py": 4,
    # nexus-ft04v.26 item 6 (THE REPOINT): `_is_same_model_passthrough`
    # (formerly :267) and `_dim_for_collection` (formerly :362) now share
    # a new `_model_for_collection` helper that PREFERS the catalog row's
    # `embedding_model` column (RDR-204 Gap 1 -- a row that disagrees with
    # the name wins) and falls to the SAME LAXER `len(segments) == 4`
    # count-based name split -- deliberately still not the conformant-
    # gated `collection_model()` funnel helper, which requires
    # `is_conformant_collection_name` internally (`[a-zA-Z0-9-]+` owner
    # charset, no underscore) and fails loud on a name with no row; both
    # would silently NARROW this migration tool from accepting an
    # underscored-owner name or an unregistered source to rejecting it,
    # forbidden by the nexus-ft04v.22 ruling this class still honors --
    # only when the name has no row (the normal case: a migration source
    # that may predate RDR-204 Phase 1 registration entirely). The former
    # third site, the passthrough `declared_model` read (formerly :610),
    # is now `_model_for_collection(name) if passthrough else None` --
    # no raw parse of its own, since it shares the SAME resolution
    # `_is_same_model_passthrough` already made, closing that site
    # entirely (3 raw sites -> 2: one inside `_model_for_collection`'s
    # fallback branch, one inside `_dim_for_collection`'s own fallback,
    # which needs distinct error-reason strings the shared helper's plain
    # string return cannot carry).
    "src/nexus/db/reconcile.py": 2,
    # _infer_content_type (write-path metadata normalization, prefers the
    # row, falls to its own documented "anything but code -> prose"
    # default) and the two TTL-expire sweeps (class (c), a collection
    # with no row is simply skipped this pass) all read
    # nexus.mcp_infra.get_collection_row directly and do NOT call either
    # primitive. 3 of this file's 4 counted sites are PRE-EXISTING
    # embedding_model_for_collection_name calls (RDR-109 nexus-6e6u1/
    # nexus-a4h7b's conformant-name fast paths, unrelated to
    # nexus-ft04v.26's own additions) the fifth class now also tracks.
    # The 4th, class (d): T3Database.list_collections() (the retired
    # Chroma-era, TEST-ONLY substrate -- RDR-155 P4a.2 made
    # HttpVectorClient the only production path) synthesizes a row from
    # the name since it has no catalog to join against; without this,
    # nexus.mcp_infra's row cache reports "no row" for every
    # T3Database-backed test, silently emptying resolve_corpus's
    # bare-content-type fan-out for the test suite's primary substrate
    # (found live: test_store_put_invalidates_page_cache /
    # test_store_delete_invalidates_page_cache in tests/test_mcp_server.py).
    "src/nexus/db/t3.py": 4,
    # class (b)/pre-existing: embedding_model_for_collection's conformant-
    # name fast path (RDR-109 nexus-6e6u1), unrelated to nexus-ft04v.26's
    # own additions -- the fifth class now also tracks it.
    "src/nexus/exporter.py": 1,
    # class (a): *part* is a user-typed --corpus TOKEN, not necessarily an
    # existing collection.
    "src/nexus/mcp/core.py": 1,
    # class (b)/pre-existing: embedding_model_for_collection's conformant-
    # name fast path, same as exporter.py above.
    "src/nexus/search_engine.py": 1,
}

PARSE_SITE_PIN: int = sum(COLLECTION_NAME_PARSE_CENSUS.values())


def _get_str_const(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


#: The candidate-string PRIMITIVES the funnel helpers themselves are
#: allowed to use (RDR-204 Phase 3 repoint, nexus-ft04v.26 THE REPOINT --
#: coordinator ruling 2026-09-09). A caller reaching for one of these
#: OUTSIDE corpus.py's own helper bodies is doing the same name-derives-
#: a-fact parsing the original four classes exist to catch, just
#: relocated behind a function call instead of a raw split/partition --
#: "parsing that moves into one helper with N callers while the gate
#: reads zero" is the exact failure this fifth class exists to close.
#: corpus.py's OWN internal use (each primitive's own definition, and
#: the other funnel helpers' calls into them) is excluded -- see
#: ``_raw_collection_name_parse_sites``.
#:
#: SINGLE-SOURCED in ``nexus.corpus._CANDIDATE_STRING_PRIMITIVES``
#: (RDR-204 Phase 3 fix round, nexus-ft04v.28 item 6) rather than
#: hand-duplicated as a literal here: a hardcoded copy in THIS file,
#: ~400 lines from any of the three definitions, is exactly "a renamed
#: parsing primitive evades it" -- nothing forces this list to track a
#: rename. Importing the source-of-truth tuple means a rename that
#: forgets to update it is caught by
#: ``test_candidate_string_primitives_all_resolve_to_real_functions``
#: below, loud, rather than silently under-counting forever. See that
#: constant's own docstring in corpus.py for why
#: ``is_conformant_collection_name``/``parse_conformant_collection_name``
#: are deliberately NOT primitives here despite also being regex-based.
from nexus.corpus import _CANDIDATE_STRING_PRIMITIVES  # noqa: E402 — import after the module docstring/constants above it, matching this file's existing style

_PRIMITIVE_CALL_NAMES = frozenset(_CANDIDATE_STRING_PRIMITIVES)

_CORPUS_PY_REL = "src/nexus/corpus.py"


def test_candidate_string_primitives_all_resolve_to_real_functions() -> None:
    """Non-vacuity for the single-sourcing itself: every name in
    ``nexus.corpus._CANDIDATE_STRING_PRIMITIVES`` must be a real,
    currently-defined function in that module. Catches the failure mode
    this whole repoint exists to prevent -- a rename that updates the
    function but not the tuple would otherwise leave a stale STRING in
    the primitive set that matches nothing (silently undercounting,
    since no call site can ever match a name nothing is defined under),
    with no test noticing."""
    import inspect

    import nexus.corpus as corpus_mod

    assert _CANDIDATE_STRING_PRIMITIVES, "the primitive tuple must not be empty"
    for name in _CANDIDATE_STRING_PRIMITIVES:
        obj = getattr(corpus_mod, name, None)
        assert obj is not None and inspect.isfunction(obj), (
            f"{name!r} in nexus.corpus._CANDIDATE_STRING_PRIMITIVES does not "
            f"resolve to a real function -- renamed or removed without "
            f"updating the tuple."
        )


def _scan_tree(path: pathlib.Path) -> list[tuple[int, str]]:
    """(lineno, pattern_class) for every raw collection-name-parse-shaped
    site in one file -- BEFORE the ``_EXCLUDED_SITES`` filter is applied.

    AST-based by construction: a match requires an actual ``ast.Call`` to
    ``split``/``rsplit``/``partition``/``rpartition``/``startswith``/
    ``endswith`` with a literal argument, an actual ``ast.Compare`` with
    ``In``/``NotIn`` against the literal ``"__"``, or an actual ``ast.Call``
    to one of :data:`_PRIMITIVE_CALL_NAMES`'s functions (pattern class
    ``"primitive_call"``) -- never a text match, so a comment or docstring
    mentioning ``"__"`` or a primitive's name cannot contribute a hit. A
    primitive call counts in EITHER form (RDR-204 Phase 3 fix round,
    nexus-ft04v.28 item 6): bare-name (``split_candidate_collection_name(x)``,
    a module-level import) or attribute (``corpus.split_candidate_
    collection_name(x)``, a module-qualified reference) -- the earlier
    bare-name-only check let an attribute-form caller (``import
    nexus.corpus as corpus; corpus.split_candidate_collection_name(x)``)
    through undetected.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in ("split", "rsplit", "partition", "rpartition"):
                if node.args and _get_str_const(node.args[0]) == "__":
                    hits.append((node.lineno, attr))
            elif attr in ("startswith", "endswith"):
                matched = False
                for arg in node.args:
                    sv = _get_str_const(arg)
                    if sv is not None and sv in _TYPE_PREFIXES:
                        matched = True
                    elif isinstance(arg, (ast.Tuple, ast.List)):
                        for elt in arg.elts:
                            if _get_str_const(elt) in _TYPE_PREFIXES:
                                matched = True
                if matched:
                    hits.append((node.lineno, attr))
            elif attr in _PRIMITIVE_CALL_NAMES:
                # Attribute-form primitive call, e.g.
                # ``corpus.split_candidate_collection_name(x)``.
                hits.append((node.lineno, "primitive_call"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _PRIMITIVE_CALL_NAMES:
                hits.append((node.lineno, "primitive_call"))
        elif isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                if _get_str_const(node.left) == "__" or _get_str_const(comparator) == "__":
                    hits.append((node.lineno, "not_in" if isinstance(op, ast.NotIn) else "in"))
    return hits


def _raw_collection_name_parse_sites() -> dict[str, list[tuple[int, str]]]:
    """Every raw match under SRC, keyed by path relative to REPO_ROOT,
    with NO exclusion filter applied. Used to prove exclusions are real.

    ``primitive_call`` hits inside ``corpus.py`` itself are dropped here,
    not via ``_EXCLUDED_SITES``: the coordinator's ruling scopes the fifth
    class to callers OUTSIDE corpus.py's own helper bodies (the funnel
    helpers necessarily call their own shared string-shape primitive), a
    file-level exclusion distinct in kind from the per-line judgement
    calls ``_EXCLUDED_SITES`` records for the original four classes.
    """
    found: dict[str, list[tuple[int, str]]] = {}
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        hits = _scan_tree(path)
        if rel == _CORPUS_PY_REL:
            hits = [(ln, cls) for ln, cls in hits if cls != "primitive_call"]
        if hits:
            found[rel] = hits
    return found


def _collection_name_parse_sites() -> dict[str, list[tuple[int, str]]]:
    """Raw matches under SRC with `_EXCLUDED_SITES` filtered out -- this is
    what the pin counts."""
    result: dict[str, list[tuple[int, str]]] = {}
    for rel, hits in _raw_collection_name_parse_sites().items():
        kept = [(lineno, cls) for lineno, cls in hits if (rel, lineno) not in _EXCLUDED_SITES]
        if kept:
            result[rel] = kept
    return result


def _grown(live: dict[str, int], census: dict[str, int]) -> list[str]:
    return sorted(
        f"{f}: {live.get(f, 0)} > {census.get(f, 0)}"
        for f in live.keys() | census.keys()
        if live.get(f, 0) > census.get(f, 0)
    )


def _shrunk(live: dict[str, int], census: dict[str, int]) -> list[str]:
    return sorted(
        f"{f}: {live.get(f, 0)} < {census.get(f, 0)}"
        for f in live.keys() | census.keys()
        if live.get(f, 0) < census.get(f, 0)
    )


def test_scanner_is_not_vacuous(tmp_path: pathlib.Path) -> None:
    """Prove the AST walk actually matches all five classes, and that the
    TYPE_PREFIXES filter and the primitive-name match (bare-name AND
    attribute-form, nexus-ft04v.28 item 6) are not accidentally
    permissive, on a sample with a known answer -- independent of how
    large the live debt happens to be."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f(name, other, corpus):\n"
        '    a = name.split("__", 1)\n'          # split: MUST match
        '    b = name.rsplit("__", 1)\n'         # rsplit: MUST match
        '    c = name.partition("__")\n'         # partition: MUST match
        '    d = name.rpartition("__")\n'        # rpartition: MUST match
        '    e = name.startswith("code__")\n'    # startswith: MUST match
        '    g = name.endswith(("docs__", "rdr__"))\n'  # endswith tuple: MUST match
        '    h = "__" in name\n'                 # in: MUST match
        '    i = "__" not in name\n'             # not in: MUST match
        '    j = name.startswith("mcp__")\n'     # not a type prefix: must NOT match
        '    k = name.split("-", 1)\n'           # wrong literal: must NOT match
        '    m = split_candidate_collection_name(name)\n'      # primitive_call (bare): MUST match
        '    n = embedding_model_for_collection_name(name)\n'  # primitive_call (bare): MUST match
        '    q = model_version_for_collection_name(name)\n'    # primitive_call (bare, third primitive): MUST match
        '    o = corpus.split_candidate_collection_name(name)\n'  # primitive_call (attribute form, item 6): MUST match
        '    r = other.some_unrelated_method(name)\n'  # attribute call to a NON-primitive name: must NOT match
        '    p = some_other_function(name)\n'    # unrelated bare call: must NOT match
        "    return a, b, c, d, e, g, h, i, j, k, m, n, q, o, r, p\n",
        encoding="utf-8",
    )
    hits = _scan_tree(sample)
    classes = [cls for _lineno, cls in hits]
    assert classes == [
        "split", "rsplit", "partition", "rpartition",
        "startswith", "endswith", "in", "not_in",
        "primitive_call", "primitive_call", "primitive_call", "primitive_call",
    ], (
        f"scanner drifted from the five RDR-204 pattern classes (four from "
        f"Phase 3 item 1, plus nexus-ft04v.26's primitive_call, widened to "
        f"attribute form by nexus-ft04v.28 item 6): {hits}. Either a real "
        f"class stopped matching, or a negative case (mcp__/wrong-literal/"
        f"non-primitive-attribute-access/unrelated-call) started matching "
        f"-- both make every guard below pass by doing nothing or by "
        f"over-counting."
    )


def test_scanner_still_sees_the_live_tree() -> None:
    """Companion to the synthetic check: the walk is actually pointed at
    src/nexus, not an empty or missing directory."""
    assert any(SRC.rglob("*.py")), f"SRC does not resolve to python sources: {SRC}"


def test_excluded_sites_are_real_matches_not_omissions() -> None:
    """Every _EXCLUDED_SITES entry must be a genuine raw hit at that exact
    line. An exclusion that stops matching (the line moved, the code
    changed) is a stale entry masquerading as a live judgement call --
    this is what tells a later reader "excluded" apart from "forgotten"."""
    raw = _raw_collection_name_parse_sites()
    missing = [
        f"{rel}:{lineno}"
        for (rel, lineno) in _EXCLUDED_SITES
        if lineno not in {ln for ln, _cls in raw.get(rel, [])}
    ]
    assert not missing, (
        f"declared exclusion(s) no longer match any raw site: {missing}. "
        "Either the line moved (update the lineno) or the code changed and "
        "the exclusion should be deleted -- a stale exclusion silently "
        "widens the census's blind spot without lowering the pin."
    )


def test_excluded_sites_do_not_reach_the_pin() -> None:
    """The filter actually removes what it claims to: no excluded
    (rel, lineno) pair survives into the pinned census."""
    live = _collection_name_parse_sites()
    leaked = [
        f"{rel}:{lineno}"
        for (rel, lineno) in _EXCLUDED_SITES
        if lineno in {ln for ln, _cls in live.get(rel, [])}
    ]
    assert not leaked, f"excluded site(s) still counted toward the pin: {leaked}"


def test_rdr_id_parse_is_not_a_scanner_hit() -> None:
    """RDR-204's other named exclusion -- rdr- document ids -- is a checked
    zero, not an unexamined one. The one rdr- id parse in the tree uses the
    literal "rdr-" (single hyphen), which is not in _TYPE_PREFIXES ("rdr__",
    double underscore, is the collection prefix); confirm it truly does not
    reach the scanner, rather than assuming the literal difference is
    enough without ever running the scan against it."""
    target = SRC / "catalog" / "rdr_canonical.py"
    assert target.exists(), f"reference file moved or was deleted: {target}"
    hits = {lineno for lineno, _cls in _scan_tree(target)}
    assert 124 not in hits, (
        "catalog/rdr_canonical.py:124's rdr- id check now matches the "
        "scanner (the literal or the call shape changed) -- if it is a "
        "genuine rdr- document-id parse, add it to _EXCLUDED_SITES with "
        "that reason rather than letting it inflate the pin silently."
    )


def test_synthetic_new_parse_site_fails_the_growth_guard() -> None:
    """A new (or grown) site is what test_no_new_parse_sites exists to
    catch -- proven here against a synthetic dict, decoupled from whatever
    the live tree happens to contain today."""
    some_file = next(iter(COLLECTION_NAME_PARSE_CENSUS))
    live = dict(COLLECTION_NAME_PARSE_CENSUS)
    live[some_file] += 1
    grown = _grown(live, COLLECTION_NAME_PARSE_CENSUS)
    assert grown == [f"{some_file}: {live[some_file]} > {COLLECTION_NAME_PARSE_CENSUS[some_file]}"]

    live_new_file = dict(COLLECTION_NAME_PARSE_CENSUS)
    live_new_file["src/nexus/not_a_real_file.py"] = 1
    assert _grown(live_new_file, COLLECTION_NAME_PARSE_CENSUS) == [
        "src/nexus/not_a_real_file.py: 1 > 0"
    ]


def test_synthetic_shrink_without_lowering_pin_fails_the_stale_guard() -> None:
    """Removing a site without lowering its file's pin is the OTHER half
    of reduce-only discipline -- test_census_has_no_stale_entries exists to
    catch a pin that overstates reality, proven here against a synthetic
    dict for the same reason as the growth guard above."""
    some_file = next(f for f, n in COLLECTION_NAME_PARSE_CENSUS.items() if n > 0)
    live = dict(COLLECTION_NAME_PARSE_CENSUS)
    live[some_file] -= 1
    shrunk = _shrunk(live, COLLECTION_NAME_PARSE_CENSUS)
    assert shrunk == [f"{some_file}: {live[some_file]} < {COLLECTION_NAME_PARSE_CENSUS[some_file]}"]


def test_no_new_parse_sites() -> None:
    """THE GUARD. A new raw collection-name parse site outside the three
    RDR-204 helpers is exactly the regression Phase 3 exists to prevent --
    each funnel slice is supposed to LOWER this census, never add to it."""
    live = {f: len(h) for f, h in _collection_name_parse_sites().items()}
    grown = _grown(live, COLLECTION_NAME_PARSE_CENSUS)
    assert not grown, (
        f"collection-name parse sites GREW at {grown}.\n"
        "RDR-204 moved collection identity (content type, model, lifecycle "
        "state) to catalog_collections; a new raw split/partition/"
        "startswith/\"__\" in\" parse re-derives a fact the row already "
        "carries and can drift from it (docs/rdr/"
        "rdr-204-embedding-profile-and-collection-authority.md § "
        "Implementation Plan Phase 3). Use the three CollectionName "
        "helpers the funnel slices (nexus-ft04v.21/.22/.23) route existing "
        "sites through instead. If this is a genuine mcp__ tool-name or "
        "rdr- document-id parse, add it to _EXCLUDED_SITES with the guard "
        "named."
    )


def test_census_has_no_stale_entries() -> None:
    """Exact-census discipline, matching test_no_new_sqlite and
    test_private_handle_access_census's own stale-entry guard: a pin that
    overstates the live tree is a lie about how much of Phase 3 is left,
    and hides a later regrowth inside the slack."""
    live = {f: len(h) for f, h in _collection_name_parse_sites().items()}
    shrunk = _shrunk(live, COLLECTION_NAME_PARSE_CENSUS)
    assert not shrunk, (
        f"stale census entry {shrunk}: the site was removed (good, a "
        "funnel slice landed) -- lower the count so the pin stays exact."
    )


def test_pin_matches_documented_total() -> None:
    """The PARSE_SITE_PIN docstring claim (49 -- 50 after nexus-ft04v.26
    added the fifth pattern class, then -1 when that SAME bead's item 6
    gave db/reconcile.py's three sites the row-read treatment closing one
    of them, then +1 for health.py's check_chash_conformance_report
    unroutable-collection probe (fixture-seam fix round: collection_model
    raising for an unregistered-but-conformant name was silently zeroing
    the whole probe inside a blanket except, masking the nexus-4ijv4
    false-clean-by-omission finding -- fixed by reading the class-(d)
    name-parser instead, a new tracked site), then +1 again for
    http_vector_client.py's new _is_cce_collection (same round: the
    original model-STRING CCE check was a real regression, fixed by
    reading content_type instead) -- see COLLECTION_NAME_PARSE_CENSUS's
    own docstring -- then 51 -- 53 for nexus-ft04v.28 item 6: the fifth
    class's primitive set is now single-sourced from nexus.corpus.
    _CANDIDATE_STRING_PRIMITIVES (a rename can no longer silently
    undercount) and gained a genuinely uncounted third primitive,
    model_version_for_collection_name, whose two chunk_quarantine.py
    callers were already live but invisible to the old two-name
    hardcoded set; then 53 -- 52 on 2026-09-09 when the client-side
    quarantine-sibling pre-registration and its kwargs derivation were
    deleted; then 52 -- 53 the same day when the owner-scope resolver
    gained a class-(a) candidate split on a user-typed scope token) is derived from the same dict the guards above check
    against -- this catches a hand-edited docstring number drifting from
    the dict it claims to summarize."""
    assert PARSE_SITE_PIN == sum(COLLECTION_NAME_PARSE_CENSUS.values())
    assert PARSE_SITE_PIN == 53
