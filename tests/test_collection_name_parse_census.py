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

FIVE PATTERN CLASSES (the pre-P1.1 definition, kept as history; RDR-229
P1.1 below replaced the fifth class's two named functions with a derived
helper set and added the shapes it lists). The original four, per RDR-204 Phase 3 item 1
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
``shape_of``'s positional 4-field decode of a malformed name
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
collection_name``/``parse_conformant_collection_name`` were left out of
that tuple then; since RDR-229 P1.1 they are counted, as parser roots of the
derived helper set (see the P1.1 paragraph below), and the three primitives
are the non-vacuity floor of that set. Attribute-form calls
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
unconditionally). The engine's own register-time 422 on a mismatch is
the correctness guard of last resort for any future call site that
bypasses the funnel; nexus-aotql (2026-09-27) closed out the last two
production sites that still called ``register_collection`` directly
(``indexer.py``'s ``_migrate_legacy_collections`` and ``commands/
catalog_cmds/migration.py``'s ``migrate_fallback_cmd``) -- see
``corpus.ensure_collection_registered``'s own docstring for the current,
fully-consolidated site list. Tests:
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

RDR-229 P1.1 (2026-11-20, nexus-p2m1x.1) WIDENED THE CENSUS and re-pinned it
at the measured count: 46 -> 243. The five classes above kept their names;
the scanner (now ``tests/_collection_name_census_scan.py``) gained seven shapes
(``fstring_prefix``, ``const_prefix``, ``loop_prefix``, ``substring``,
``glob_default``, ``sql_prefix``, ``fnmatch``) and its fifth class now covers
every ``corpus.py`` function that reaches a parser, DERIVED from the module's
call graph (23 helpers), not the three typed in
``_CANDIDATE_STRING_PRIMITIVES``; ``taxonomy__`` joined the prefix set. The
census is now a per-site LEDGER (``tests/_collection_name_census_ledger.py``,
content-anchored the way the exclusions are): each site carries one class from
the RDR's set (mint, migration source, diagnostic, routing, URI-string
classifier) or one of the two non-parse fnmatch labels (label pattern, file
path) or an explicit holdover with a note naming the step that moves it, and
a live site with no entry is refused, naming file, line and shape. The pin is
the measured total, per class and per shape; the per-file table below is
derived from the ledger. 197 sites are new: 130 more calls to derived
helpers, and 67 of the raw and routing shapes (the routing census scans 60 on
this tree, 59 when T2 ``229-research-39`` measured it, less the two it shares
with the 46, plus the sites its method does not look for). The 46 are all
still there. Review round 1 (2026-11-20) added the shapes the first pass
left out: concatenated and formatted prefixes, locals bound by AnnAssign,
walrus, tuple-unpack and prefix literals, computed prefix constants,
``removeprefix``/``removesuffix``/``replace``, ``find``/``index``/``count``,
``sep="__"`` and a named ``"__"``, ``endswith("__")`` and the hash-suffix leg,
and it made exclusions shape-specific. The 22 holdovers that first said
"needs an owner" are RDR-204 class (b) model reads, owned by P2.2 (baton's
ruling, nexus-p2m1x.7): 19 are P2.2 holdovers (including
``doctor_search._default_model_for``, whose model_drift probe compares two
name derivations today and sets the exit status, so it is not the ruling's
print-only exception); the two ``info_cmd`` hits that print the name's model
beside the row's own value are diagnostic; and ``attribute_collection``,
which reads the row first, is mint (the NoRow outcome). ``scripts/`` (10 raw sites, T2 ``229-research-21``) stays outside
``SRC`` and so outside this gate. Phase 2 lowers the pins by deleting ledger
entries.

RDR-229 P1.R fix round (2026-11-20, nexus-p2m1x.5) re-pinned it at 265; P2.5
(nexus-p2m1x.10) lowered it to 255 and P2.3 (nexus-p2m1x.8) to 245. The
review found eight call sites the helper set could not see: five callers of
``corpus.resolve_corpus`` (its parse is ``startswith(f"{corpus}__")``, a shape
the P1.1 roots, which were regexes and raw ``"__"`` tests, did not include) and
three callers of ``CollectionName.parse`` (the helpers were derived from
``corpus.py`` only). The derivation now takes every function that holds a
name-decomposing site, or calls ``parse_conformant_collection_name``, as a root
across ``src/nexus`` (``tests/_collection_name_census_cross.py``, which also
lists what it still cannot see), and the same rule found twelve more call sites
(of ``shape_of``, ``_under``, ``target_collection_for``,
``_backfill_papers`` / ``_backfill_knowledge`` and the one-off census's
``_parse``). A new ``regex_prefix`` shape counts the one-off census's
``_QUARANTINE_RE`` (a diagnostic). The 21 live in
``tests/_collection_name_census_sites_callers.py``.
``tests/test_collection_name_census_cross_roots.py`` proves a new caller of
``resolve_corpus`` or ``CollectionName.parse`` in any module turns the census
red.

WHY A CENSUS-IN-TEST-FILE RATHER THAN A ``src/`` LINT MODULE (the
alternative the bead names, ``storage_boundary_lint.py``-style). Nothing in
``src/nexus`` needs to call this scanner at runtime or share it across
modules -- it exists only to be asserted against in this file, exactly like
``tests/test_private_handle_access_census.py``. A ``src/`` module would add
an import surface with no production consumer.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
from collections import Counter

import pytest

import nexus.catalog.rows.legacy_names as legacy_names_mod
from tests import _collection_name_census_scan as _scan
from tests._collection_name_census_exclusions import EXCLUDED_SITES as _EXCLUDED_SITES
from tests._collection_name_census_ledger import LEDGER
from tests._collection_name_census_notes import BYPASS as _BYPASS_NOTE
from tests._collection_name_census_notes import LABEL_TYPED
from tests._lint_line_anchor import resolve_anchor, resolve_ledger

pytestmark = pytest.mark.lint

REPO_ROOT = _scan.REPO_ROOT
SRC = _scan.SRC

#: Collection-type / lifecycle prefixes a raw ``startswith``/``endswith``
#: call names when it is parsing a collection NAME (RDR-204 Phase 3 item 1;
#: RDR-229 P1.1 added ``taxonomy__``). Single-sourced in the scan module.
_TYPE_PREFIXES = _scan.TYPE_PREFIXES

#: The per-site ledger's count per file, DERIVED (RDR-229 P1.1): the old
#: hand-kept dict, with its per-file prose, became the ledger's per-site
#: classes and notes. A dropped file and an absent file both read as zero.
COLLECTION_NAME_PARSE_CENSUS: dict[str, int] = {}
for _site in LEDGER:
    COLLECTION_NAME_PARSE_CENSUS[_site.rel] = (
        COLLECTION_NAME_PARSE_CENSUS.get(_site.rel, 0) + _site.n
    )

#: THE PIN (RDR-229 P1.1, 2026-11-20, measured on develop 970f73223 and
#: re-measured by the P1.R fix round on 118701321): 265 sites, by class and by
#: shape. 243 at P1.1, plus the one-off P1.2 collections-census parse (a
#: diagnostic ledger entry that is deleted before Phase 4) = 244, plus the 21
#: the fix round's cross-module roots found (20 calls to a root, 8 of them the
#: review's, and the one-off census's ``_QUARANTINE_RE``). Phase 2 lowers these
#: by deleting ledger entries; nothing here may rise without a ledger entry and
#: a reason.
#:
#: P2.5 (nexus-p2m1x.10, 2026-11-20), re-measured on P1.R's 11da32740: 265 -> 255. The twelve
#: ``_BYPASS_SCHEMA_PREFIXES`` tests became calls to ONE helper (``db/rows/lifecycle.bypasses_canonical_schema``),
#: whose two name tests are the only sites left: the ``taxonomy__`` half (routing, P2.9) and the quarantine
#: fallback for a name no row backs (migration-source). routing 36 -> 25, migration-source 17 -> 18,
#: const_prefix 31 -> 21.
#:
#: P2.3 (nexus-p2m1x.8, 2026-11-20), re-measured on the tree rebased onto P2.5's ac84addc3: 255 -> 245. The repo
#: indexer's target models became row reads (nine holdovers went), two t3 holdovers were relabelled diagnostic, the
#: post-rename conformance check and its else arm went (two migration-source), and the legacy-name fallback of the
#: NoRow/Unreadable outcome is one new mint. holdover 45 -> 34, diagnostic 40 -> 42, mint 111 -> 112,
#: migration-source 18 -> 16, primitive_call 192 -> 182.
#:
#: P2.7 part A (nexus-p2m1x.12, 2026-11-20), re-measured on develop 6341b2ac5: 245 -> 240. ``repo_identity.
#: list_sibling_collections`` was deleted (no caller in ``src``; RDR-210 finds siblings by row tuple), and with it its
#: five holdovers: holdover 34 -> 29, primitive_call 182 -> 181, startswith 4 -> 3, hash_suffix 2 -> 0, substring 1 -> 0.
#:
#: P2.7 part B (nexus-p2m1x.12, 2026-11-20): 240 -> 240. ``resolve_corpus`` stage 3 matches the row tuple first; its
#: label-prefix fallback stays as one site, classed label-pattern (it matches the label the user typed and decomposes
#: nothing; the validator admits that class on a non-fnmatch site only with the ``LABEL_TYPED`` note). The root rule
#: (``_collection_name_census_cross.root_hit_exclusions``) no longer counts a non-parse-class site as root evidence, so
#: ``resolve_corpus`` is not a root of its own. It is still a derived helper, because its typed-value split reaches
#: ``split_candidate_collection_name``, so its five callers stay counted and are classed mint (item 1: parsing the user's
#: own input). holdover 29 -> 23, label-pattern 9 -> 10, mint 112 -> 117; no shape moves.
#:
#: P2.9 (nexus-p2m1x.14, 2026-11-20), re-measured on develop 303a22d29 with P2.8 (P2.7's 240): 240 -> 228. The 22 routing
#: sites that now read the row's content_type left the ledger (const_prefix 14, loop_prefix 7, glob_default 1); the
#: three ``taxonomy__`` name tests went routing -> migration-source (no row attribute identifies them; P2.2,
#: nexus-p2m1x.7, moves them into legacy_names); ten callers of the shared NoRow derivation
#: (``corpus.routing_content_type`` / ``candidate_content_type``) are new mint entries, each empty at P3.8.
#: routing 25 -> 0, mint 117 -> 127, migration-source 16 -> 19, const_prefix 21 -> 7, loop_prefix 7 -> 0,
#: primitive_call 181 -> 191, glob_default 1 -> 0. (The tenth mint is ``topic_route.is_prose``: the request's
#: names carry no row on a cold cache, so the prose test answers NoRow from the name.)
#:
#: P2.9 reland (nexus-p2m1x.14): 228 -> 229. The import's routing read falls back to the name when the row
#: resolver fails (``exporter.import_collection``, P2.5's contract that a failed lookup never aborts the import):
#: one more NoRow mint, empty at P3.8. mint 127 -> 128, primitive_call 191 -> 192.
#:
#: PHASE 2 INTEGRATION (2026-11-21), measured on the stacked integration/rdr229-phase2 tree (develop 0701d16d9c
#: plus D2, P2.4 with gh1680 and gh1679, P2.10, the shared-tuple guards, owner-is-source and P2.2): 229 -> 217,
#: the moves below being each step's own, taken on its own tree (D2, P2.10, gh1680, gh1679 and the shared-tuple
#: guards moved nothing); the pins are the one measurement of the sum.
#:   P2.2 (nexus-p2m1x.7), -11: the parsers moved into ``legacy_names`` (the floor: calls inside it are not
#:     sites). Of the 19 holdovers it owned, 13 read the row through ``embedding_model_for`` and left the ledger,
#:     the five ``T3Database`` reads became mint (no catalog: NoRow by construction), ``doctor_search``'s drift
#:     probe diagnostic. The NoRow derivation is one mint site each in ``rows/resolve.py`` and ``embed_window.py``;
#:     the two blank-version fallbacks and P2.9's three taxonomy tests are migration-source. holdover 23 -> 4,
#:     diagnostic +1, mint 128 -> 134, migration-source +1, primitive_call 192 -> 184, const_prefix -2, startswith -1.
#:   P2.4 (nexus-p2m1x.9), -3: the four taxonomy-threshold holdovers (three calls in ``taxonomy_cmd.py``, one in
#:     ``http_taxonomy_store.py``, each a caller of ``default_projection_threshold``) read the catalog row now, and
#:     the one NoRow name derivation they share is a single mint-class entry in ``projection_threshold.py``.
#:     holdover 4 -> 0 (the class is empty), mint 134 -> 135, primitive_call 184 -> 181.
#:   owner-is-source (marlowe), +2: two migration-source sites in ``nxexp_owner.py``, the callers of
#:     ``legacy_names.owner_stem`` (it reads the name of a recovery bundle's collection, MIG_BUNDLE_STEM; the parse is in the
#:     floor, not a site, since owner_stem moved there): migration-source 20 -> 22, primitive_call 181 -> 183, split 2. P3.1 (nexus-p2m1x.17), +4 mint, each a registration seam (MINT_REG) that registers the collection BEFORE the first catalog document, since the engine refuses a document under an unregistered one: ``note_write`` (put_note), ``exporter`` (the import pre-pass), ``registration_retry`` (the 422 retry), ``doc_indexer`` (the pre-flight). mint 135 -> 139, primitive_call 183 -> 187, total 217 -> 221. P3.8 (nexus-p2m1x.28), -2: register_collection derives nothing from the name. mint 139 -> 137, primitive_call 187 -> 185, total 221 -> 219.
#:
#: P3.6 (nexus-p2m1x.26, 2026-11-21), 219 -> 217: ``validate_collection_name`` is the label rule and reads no part of a name
#: (the ChromaDB-shape regex and the overflow hint's ``split_candidate_collection_name`` are gone), so it is no longer a
#: derived helper and its two ``T3Database`` callers (create, and the rename target) left the ledger. mint 137 -> 135,
#: primitive_call 185 -> 183.
PARSE_SITE_PIN: int = 217
CLASS_PIN: dict[str, int] = {
    "mint": 135, "diagnostic": 43, "migration-source": 22,
    "label-pattern": 10, "file-path": 5, "uri-classifier": 2,
}
SHAPE_PIN: dict[str, int] = {
    "primitive_call": 183, "const_prefix": 5, "fnmatch": 14,
    "strip_prefix": 4, "startswith": 2, "split": 2,
    "fstring_prefix": 2, "regex_prefix": 1, "in": 1,
    "sql_prefix": 1, "concat_prefix": 1, "endswith": 1,
}

#: The three candidate-string PRIMITIVES (RDR-204 Phase 3 repoint,
#: nexus-ft04v.26; single-sourced in ``nexus.catalog.rows.legacy_names._CANDIDATE_STRING_PRIMITIVES``
#: by nexus-ft04v.28 item 6). Since RDR-229 P1.1 they are the NON-VACUITY FLOOR
#: of the census's derived helper set, not its definition: the counted helpers
#: are derived from corpus.py's call graph (``_scan.derive_parser_helpers``),
#: and ``test_derived_helper_set_is_non_empty_and_holds_the_current_primitives``
#: asserts the derived set contains these three, so a rename that drops one
#: from the derivation fails loud. A call to any derived helper outside
#: corpus.py is a ``primitive_call`` site; corpus.py's own helper-to-helper
#: calls are not counted (coordinator ruling 2026-09-09). The tuple is
#: imported, not retyped, so a rename that forgets it is caught by
#: ``test_candidate_string_primitives_all_resolve_to_real_functions`` below.
from nexus.catalog.rows.legacy_names import _CANDIDATE_STRING_PRIMITIVES  # noqa: E402 — import after the module docstring/constants above it, matching this file's existing style

def test_candidate_string_primitives_all_resolve_to_real_functions() -> None:
    """Non-vacuity for the single-sourcing itself: every name in
    ``nexus.catalog.rows.legacy_names._CANDIDATE_STRING_PRIMITIVES`` must be a real,
    currently-defined function in that module. Catches the failure mode
    this whole repoint exists to prevent -- a rename that updates the
    function but not the tuple would otherwise leave a stale STRING in
    the primitive set that matches nothing (silently undercounting,
    since no call site can ever match a name nothing is defined under),
    with no test noticing."""
    assert _CANDIDATE_STRING_PRIMITIVES, "the primitive tuple must not be empty"
    for name in _CANDIDATE_STRING_PRIMITIVES:
        obj = getattr(legacy_names_mod, name, None)
        assert obj is not None and inspect.isfunction(obj), (
            f"{name!r} in legacy_names._CANDIDATE_STRING_PRIMITIVES does not "
            f"resolve to a real function -- renamed or removed without "
            f"updating the tuple."
        )


def _scan_tree(path: pathlib.Path) -> list[tuple[int, str]]:
    """(lineno, shape) for every raw collection-name-parse-shaped site in
    one file, BEFORE the exclusion filter. The walk itself lives in
    ``tests/_collection_name_census_scan.py`` (its docstring lists the
    shapes); a fixture file sees its own module-level prefix constants."""
    return _scan.scan_file(path)


def _raw_collection_name_parse_sites() -> dict[str, list[tuple[int, str]]]:
    """Every raw match under SRC, keyed by path relative to REPO_ROOT, with
    NO exclusion filter applied (``primitive_call`` hits inside ``corpus.py``
    are dropped, see ``raw_sites``)."""
    return _scan.raw_sites()


def _resolve_excluded_sites() -> tuple[dict[str, dict[tuple[int, str], tuple[str, ...]]], list[str]]:
    """Resolve every ``_EXCLUDED_SITES`` content anchor to its CURRENT
    live line number (nexus-vkpr3). Returns ``(by_file, problems)``:
    ``by_file`` maps path -> {(resolved lineno, shape): content anchor}; an
    exclusion removes only the shape it names (RDR-229 P1.1 review), so a
    different shape on the same line stays a hit. A
    ``problems`` entry (STALE or AMBIGUOUS) means the anchor did not
    resolve at all -- callers must not silently treat that as "nothing
    to exclude here", `test_excluded_sites_are_real_matches_not_omissions`
    fails loud on any non-empty `problems`."""
    items = [
        (rel, content, (content, shape))
        for (rel, content, shape) in _EXCLUDED_SITES
    ]
    resolved, problems = resolve_ledger(REPO_ROOT, items)
    by_file: dict[str, dict[tuple[int, str], tuple[str, ...]]] = {}
    for rel, lineno, (content, shape) in resolved:
        by_file.setdefault(rel, {})[(lineno, shape)] = content
    return by_file, problems


def _without_excluded(
    raw: dict[str, list[tuple[int, str]]],
    excluded: dict[str, dict[tuple[int, str], tuple[str, ...]]],
) -> dict[str, list[tuple[int, str]]]:
    result: dict[str, list[tuple[int, str]]] = {}
    for rel, hits in raw.items():
        kept = [(ln, sh) for ln, sh in hits if (ln, sh) not in excluded.get(rel, {})]
        if kept:
            result[rel] = kept
    return result


def _collection_name_parse_sites() -> dict[str, list[tuple[int, str]]]:
    """Raw matches under SRC with `_EXCLUDED_SITES` filtered out -- this is
    what the pin counts. An anchor that fails to resolve excludes
    nothing (fail-safe toward MORE reported violations, never fewer) --
    `test_excluded_sites_are_real_matches_not_omissions` is what makes a
    resolution failure loud in its own right."""
    excluded_by_file, _problems = _resolve_excluded_sites()
    return _without_excluded(_raw_collection_name_parse_sites(), excluded_by_file)


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
    """Every _EXCLUDED_SITES entry's content anchor must resolve to a
    unique CURRENT line (nexus-vkpr3: neither STALE -- the anchor text no
    longer occurs -- nor AMBIGUOUS -- it occurs more than once), and that
    resolved line must be a genuine raw hit. Either failure means the
    entry is a stale judgement call masquerading as a live exclusion --
    this is what tells a later reader "excluded" apart from "forgotten"."""
    by_file, problems = _resolve_excluded_sites()
    assert not problems, (
        f"{len(problems)} _EXCLUDED_SITES anchor(s) failed to resolve:\n  "
        + "\n  ".join(problems)
        + "\n\nRetarget with the line's current stripped text if it moved "
        "(insertions above it do not require this -- the anchor tracks "
        "the content, not the number), or delete the entry if the code "
        "changed."
    )
    raw = _raw_collection_name_parse_sites()
    missing = [
        f"{rel}:{lineno} shape={shape} (anchor {content!r})"
        for rel, keyed in by_file.items()
        for (lineno, shape), content in keyed.items()
        if (lineno, shape) not in set(raw.get(rel, []))
    ]
    assert not missing, (
        f"declared exclusion(s) resolve to a live line the raw scan no "
        f"longer flags: {missing}. The code changed shape -- delete the "
        "exclusion, or investigate why the scanner stopped matching it."
    )


def test_excluded_sites_do_not_reach_the_pin() -> None:
    """The filter actually removes what it claims to: no excluded,
    content-resolved line survives into the pinned census."""
    live = _collection_name_parse_sites()
    by_file, _problems = _resolve_excluded_sites()
    leaked = [
        f"{rel}:{lineno} shape={shape}"
        for rel, keyed in by_file.items()
        for (lineno, shape) in keyed
        if (lineno, shape) in set(live.get(rel, []))
    ]
    assert not leaked, f"excluded site(s) still counted toward the pin: {leaked}"


def test_excluded_site_anchor_survives_insertion_above_it(tmp_path: pathlib.Path) -> None:
    """nexus-vkpr3 regression, against THIS file's real scanner
    (``_scan_tree``) and the real ``resolve_anchor`` primitive: inserting
    a line above an excluded site must not silently move the exclusion
    onto a different, unreviewed raw hit that happens to shift into the
    old stored line number.

    Fixture mirrors an ``_EXCLUDED_SITES`` entry's own shape (the mcp__
    tool-name strip) with a genuine, DIFFERENT raw hit ("decoy") placed
    directly above it -- a plain ``split("__")`` call the scanner also
    flags, which is exactly the shape a stale line-number key could
    silently swallow after a shift."""
    sample = tmp_path / "sample.py"
    original = (
        "def f(raw_tool, other_name):\n"
        '    decoy = other_name.split("__", 1)\n'
        '    bare = raw_tool.rsplit("__", 1)[-1] if '
        'raw_tool.startswith("mcp__") else raw_tool\n'
        "    return decoy, bare\n"
    )
    sample.write_text(original, encoding="utf-8")

    victim_content = (
        'bare = raw_tool.rsplit("__", 1)[-1] if '
        'raw_tool.startswith("mcp__") else raw_tool',
    )
    lineno, err = resolve_anchor(tmp_path, "sample.py", victim_content)
    assert (lineno, err) == (3, "")

    hits_before = dict(_scan_tree(sample))
    assert hits_before[2] == "split"  # the decoy
    assert hits_before[3] == "rsplit"  # VICTIM

    # Insert exactly one line above both sites -- enough to shift the
    # decoy (originally line 2) onto VICTIM's OLD stored line number (3).
    modified = "# inserted by an unrelated edit\n" + original
    sample.write_text(modified, encoding="utf-8")

    hits_after = dict(_scan_tree(sample))
    assert hits_after[3] == "split", (
        "fixture stopped demonstrating the hazard -- the decoy must now "
        "sit at VICTIM's old stored line number (3)"
    )
    assert hits_after[4] == "rsplit"

    # A stale line-number key (3) would now silently exclude the DECOY
    # split() call under the mcp__ rationale. The content anchor instead
    # resolves to VICTIM's real, shifted location and nothing else.
    lineno, err = resolve_anchor(tmp_path, "sample.py", victim_content)
    assert err == ""
    assert lineno == 4
    assert lineno != 3  # never the decoy's line


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


def _live_and_ledger() -> tuple[
    Counter[tuple[str, int, str]], dict[tuple[str, int, str], _scan.Site],
]:
    """The live widened scan (exclusions applied) and the ledger resolved to
    current lines. An unresolved anchor fails ``test_ledger_anchors_resolve``."""
    excluded, _problems = _resolve_excluded_sites()
    raw = _without_excluded(_raw_collection_name_parse_sites(), excluded)
    resolved, _problems = _scan.resolve_sites(REPO_ROOT, list(LEDGER))
    return _scan.live_counter(raw), resolved


def test_ledger_is_well_formed_and_every_anchor_resolves() -> None:
    """Every entry has a known class and shape, a holdover carries a note,
    an fnmatch is labelled by what it matches, and every content anchor
    resolves to exactly one current line (neither STALE nor AMBIGUOUS)."""
    assert not _scan.ledger_problems(list(LEDGER))
    _resolved, problems = _scan.resolve_sites(REPO_ROOT, list(LEDGER))
    assert not problems, "ledger anchor(s) failed to resolve:\n  " + "\n  ".join(problems)


def test_no_new_parse_sites() -> None:
    """THE GUARD. Every live site has a ledger entry carrying a class; one
    without is refused, naming file, line and shape. (Phase 3's whole point is
    that nothing re-derives from the name a fact the row carries, so a site
    that cannot be labelled is a site that should not exist.)"""
    live, resolved = _live_and_ledger()
    unlabelled, _stale = _scan.diff_ledger(live, resolved)
    assert not unlabelled, (
        "UNLABELLED collection-name parse site(s) (RDR-229 P1.1):\n  "
        + "\n  ".join(unlabelled)
        + "\nRoute the read through the row (resolve_collection / the "
        "CollectionName helpers), or add a ledger entry with one class "
        "from tests._collection_name_census_scan.CLASSES and the reason. A "
        "genuine mcp__ tool-name or rdr- document-id parse goes in "
        "tests/_collection_name_census_exclusions.py instead."
    )


def test_census_has_no_stale_entries() -> None:
    """Exact-census discipline: a ledger entry whose site is gone overstates
    how much is left and hides a later regrowth inside the slack."""
    live, resolved = _live_and_ledger()
    _unlabelled, stale = _scan.diff_ledger(live, resolved)
    assert not stale, (
        "stale ledger entr(y/ies), the site was removed (good): delete the "
        "entry and lower the pins.\n  " + "\n  ".join(stale)
    )


def test_per_file_table_still_matches_the_live_scan() -> None:
    live, _resolved = _live_and_ledger()
    per_file: Counter[str] = Counter()
    for (rel, _ln, _shape), n in live.items():
        per_file[rel] += n
    assert not _grown(dict(per_file), COLLECTION_NAME_PARSE_CENSUS)
    assert not _shrunk(dict(per_file), COLLECTION_NAME_PARSE_CENSUS)


def test_pins_are_the_measured_counts_by_total_class_and_shape() -> None:
    """The pin is the scanner's own measurement: total, per class and per
    shape, each exact in both directions."""
    live, resolved = _live_and_ledger()
    assert sum(live.values()) == PARSE_SITE_PIN
    by_class: Counter[str] = Counter()
    by_shape: Counter[str] = Counter()
    for key, site in resolved.items():
        by_class[site.cls] += site.n
        by_shape[key[2]] += site.n
    assert dict(by_class) == CLASS_PIN
    assert dict(by_shape) == SHAPE_PIN
    assert sum(CLASS_PIN.values()) == sum(SHAPE_PIN.values()) == PARSE_SITE_PIN


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
    gained a class-(a) candidate split on a user-typed scope token; then
    53 -- 52 on 2026-09-24 when index-md's knowledge__ extractor warning,
    and its class-(a) split, were deleted, nexus-kk4ut; then 52 -- 46 on
    2026-10-01 when db/embed_migrate.py (4 sites) and db/reconcile.py's
    two parse helpers were deleted as dead code, nexus-z0o2p.25; then 46 -- 45
    on 2026-10-21 when HttpVectorClient.put was retired, nexus-nt76j; then 45 -- 46
    on 2026-11-03 for the rename's class-(a) target-model token in
    HttpCatalogClient.rename_collection_cascade, nexus-ucagi; then 46 -- 243
    on 2026-11-20 when RDR-229 P1.1 (nexus-p2m1x.1) widened the scanner and
    made the census a per-site ledger, see the module docstring; then 243 -- 265
    on 2026-11-20 when the P1.R fix round (nexus-p2m1x.5) derived cross-module
    parser roots: +1 for the P1.2 one-off census parse, +21 for the callers of
    ``resolve_corpus``, ``CollectionName.parse`` and the other roots; then
    265 -- 255 at P2.5, nexus-p2m1x.10, when twelve bypass-schema prefix tests
    became one helper; then 255 -- 245 at P2.3, nexus-p2m1x.8, when the repo
    indexer's target models became row reads; then 245 -- 240 at P2.7 part A,
    nexus-p2m1x.12, when ``list_sibling_collections`` was deleted; then 240 -- 228
    at P2.9, nexus-p2m1x.14, and 228 -- 229 at its reland; then at P2.4,
    nexus-p2m1x.9, when the taxonomy threshold became a row read; then at P2.2,
    nexus-p2m1x.7, when the read-path model reads became row reads and the name
    parsers moved to ``legacy_names``; 217 -- 221 at P3.1, 221 -- 219 at P3.8, nexus-p2m1x.28) is derived
    from the same ledger the guards above check against -- this catches a
    hand-edited number drifting from the table it claims to summarize."""
    assert PARSE_SITE_PIN == sum(COLLECTION_NAME_PARSE_CENSUS.values())
    assert PARSE_SITE_PIN == 217


# ---------------------------------------------------------------------------
# The derived helper set, the class labels, and the measured routing class.
# ---------------------------------------------------------------------------


def test_derived_helper_set_is_non_empty_and_holds_the_current_primitives() -> None:
    """The fifth class is DERIVED from corpus.py's call graph (RDR-229 item
    4): it must hold the three typed primitives, the two conformant-name
    parser roots, and more than the typed three (a helper that reaches a
    parser through another function)."""
    helpers = _scan.live_parser_helpers()
    assert helpers
    assert set(_CANDIDATE_STRING_PRIMITIVES) <= helpers
    assert {"is_conformant_collection_name", "parse_conformant_collection_name"} <= helpers
    assert {"ensure_collection_registered", "t3_collection_name"} <= helpers
    assert len(helpers) > len(_CANDIDATE_STRING_PRIMITIVES)


def test_derivation_follows_the_call_graph_and_aliases(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "corpus_like.py"
    sample.write_text(
        "import re\n"
        "_RE = re.compile(r'^a__b$')\n"
        "def root(n):\n    return _RE.match(n)\n"
        "def mid(n):\n    return root(n)\n"
        "def top(n):\n    return mid(n)\n"
        "def raw(n):\n    return n.split('__')\n"
        "def unrelated(n):\n    return n.upper()\n"
        "alias = top\n",
        encoding="utf-8",
    )
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    assert _scan.derive_parser_helpers(tree) == {
        "root", "mid", "top", "raw", "alias",
    }


def test_the_census_refuses_a_live_site_with_no_class() -> None:
    """Drop one ledger entry: the site goes UNLABELLED and is named."""
    live, resolved = _live_and_ledger()
    key = next(k for k, v in sorted(resolved.items()) if v.shape == "const_prefix")
    without = {k: v for k, v in resolved.items() if k != key}
    unlabelled, stale = _scan.diff_ledger(live, without)
    assert not stale
    assert unlabelled == [
        f"{key[0]}:{key[1]} shape=const_prefix: UNLABELLED, add a ledger entry with a class"
    ]


def test_the_census_refuses_a_bad_class_a_bare_holdover_and_a_mislabelled_fnmatch() -> None:
    def site(tag: str, shape: str, cls: str, note: str = "") -> _scan.Site:
        return _scan.Site("src/nexus/x.py", (tag,), shape, cls, note)

    problems = _scan.ledger_problems([
        site("a", "startswith", "parse"),
        site("b", "startswith", "holdover"),
        site("c", "startswith", "holdover", "P2.9"),
        site("d", "fnmatch", "mint"),
        site("e", "startswith", "label-pattern"),
        site("f", "const_prefix", "file-path"),
        site("g", "startswith", "holdover", "needs an owner"),
        site("h", "fnmatch", "label-pattern"),
        site("i", "fnmatch", "file-path"),
        site("j", "fstring_prefix", "label-pattern", LABEL_TYPED),
        site("k", "fstring_prefix", "label-pattern", "any note at all"),
    ])
    assert len(problems) == 7, problems
    assert "class 'parse'" in problems[0]
    assert "Phase 2 step (P2.n)" in problems[1]
    assert "an fnmatch is a label-pattern" in problems[2]
    assert "'label-pattern' is for an fnmatch" in problems[3]
    assert "'file-path' is for an fnmatch" in problems[4]
    assert "Phase 2 step (P2.n)" in problems[5]
    # a non-fnmatch label-pattern is allowed only with the named LABEL_TYPED note, not any note
    assert "'label-pattern' is for an fnmatch" in problems[6] and "'k'" in problems[6]
    assert not [p for p in problems if "'j'" in p]


def test_an_exclusion_removes_only_the_shape_it_names(tmp_path: pathlib.Path) -> None:
    """Review round 1, item 5: a line holding two shapes, one excluded, keeps
    the other as a hit (the old line-keyed filter dropped both)."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        'def f(raw):\n    return raw.rsplit("__", 1)[-1] if "__" in raw else raw\n',
        encoding="utf-8",
    )
    raw = {"src/nexus/sample.py": _scan.scan_file(sample)}
    assert raw == {"src/nexus/sample.py": [(2, "in"), (2, "rsplit")]}
    kept = _without_excluded(raw, {"src/nexus/sample.py": {(2, "rsplit"): ("x",)}})
    assert kept == {"src/nexus/sample.py": [(2, "in")]}
    assert _without_excluded(raw, {"src/nexus/sample.py": {(2, "in"): ("x",)}}) == {
        "src/nexus/sample.py": [(2, "rsplit")]
    }


def _by_class(cls: str) -> list[_scan.Site]:
    return [s for s in LEDGER if s.cls == cls]


def test_every_fnmatch_is_labelled_by_what_it_matches() -> None:
    """14 fnmatch sites: 9 match user-configured name patterns (label
    patterns), 5 match source file paths. None is routing and none is a
    parse class, so the exporter's include/exclude filter never inflates
    the routing count."""
    fn = [s for s in LEDGER if s.shape == "fnmatch"]
    assert len(fn) == 14
    assert Counter(s.cls for s in fn) == {"label-pattern": 9, "file-path": 5}
    paths = {(s.rel, s.anchor[-1]) for s in fn if s.cls == "file-path"}
    assert any(rel == "src/nexus/exporter.py" for rel, _ in paths)
    assert not [s for s in fn if s.cls == "routing"]


def test_the_routing_class_is_measured_not_listed() -> None:
    """RDR-229 P2.9 emptied the routing class: 22 sites read a row now, and the three ``taxonomy__`` name
    tests left (the lifecycle bypass, ``manifest_backfill``, ``taxonomy_cmd``) are migration sources (a legacy
    name no row backs and no row can tell apart; read through ``legacy_names.is_taxonomy_collection_name``, P2.2). Nothing
    is left to route by name."""
    assert not _by_class("routing")
    assert "routing" not in CLASS_PIN  # the class is empty: no pin to keep
    taxonomy = [s for s in _by_class("migration-source") if "taxonomy__" in s.note and "P2.2" in s.note]
    assert sum(s.n for s in taxonomy) == 3
    assert sum(1 for s in taxonomy if s.note == _BYPASS_NOTE) == 1
    assert all("nexus-p2m1x.7" in s.note for s in taxonomy)
    norow = [s for s in _by_class("mint") if "routing_content_type" in s.note]
    assert len(norow) == 11 and all("P3.8" in s.note for s in norow)
    # Recovery-bundle classification of ANOTHER install's name is a migration
    # source, never routing (item 1); the URI-string classifiers stay at 2.
    bundle = [s for s in LEDGER if s.rel.endswith("catalog/recovery_bundle.py")]
    assert bundle and {s.cls for s in bundle} == {"migration-source"}
    uri = {(s.rel.rsplit("/", 1)[-1], s.shape) for s in _by_class("uri-classifier")}
    assert uri == {("aspect_readers.py", "const_prefix"), ("note_uri_pairs.py", "sql_prefix")}


def test_only_the_five_rdr_classes_and_the_two_fnmatch_labels_are_final() -> None:
    """``holdover`` is the one provisional label, and it never stands bare:
    each carries a note that names the Phase 2 step (P2.n) that moves it
    (the P2.2 read-path model reads, the P2.3 write model, the P2.4 taxonomy
    threshold and the P2.7 name matcher holdovers are gone). No holdover is left
    without an owner."""
    assert set(_scan.CLASSES) == {
        "mint", "migration-source", "diagnostic", "routing", "uri-classifier",
        "label-pattern", "file-path", "holdover",
    }
    holdovers = _by_class("holdover")
    assert sum(s.n for s in holdovers) == CLASS_PIN.get("holdover", 0)  # the class is empty: no pin to keep
    assert all(re.search(r"\bP2\.[0-9]+\b", s.note) for s in holdovers), (
        "a holdover note must name the Phase 2 step (P2.n) that moves it"
    )
