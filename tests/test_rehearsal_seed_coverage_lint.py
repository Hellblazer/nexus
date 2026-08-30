# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gm38i: rehearsal seed-coverage parity lint.

Provenance: substantive-critic Significant on the nexus-u5dln data-bearing
rehearsal leg. ``SchemaUpgradeRehearsalIntegrationTest``'s data leg seeds
inputs for the FORCE-RLS row-DML changesets of the CURRENT old-tag-to-HEAD
hop (catalog-013 chash normalization, catalog-014-0 manifest stamp), but
that coverage list was maintained by convention only: a FUTURE hop's new
row-DML changeset could pass the syntactic FORCE-RLS lint
(``tests/test_changelog_rls_lint.py`` — toggle-wrap/backstop shape, a
different property) while the rehearsal silently reverts to under-covering
the new hop — the exact generalization-overclaim class nexus-u5dln closed,
one hop later.

This lint closes that loop mechanically:

1. Parse OLD_TAG out of the Java rehearsal test (strict — a parse miss
   FAILS, never falls back; the run.sh nexus-b6qlf lesson). The parse and
   the integrity canonicalization are IMPORTED from
   ``scripts/gen_rehearsal_hop_manifest.py`` (``pyproject.toml`` puts
   ``scripts`` on the test pythonpath), so the generator and this lint can
   never regex- or hash-drift.
2. Load the committed OLD_TAG changeset snapshot
   (``tests/data/rehearsal_old_tag_changesets.json``) and verify THREE
   properties: its tag matches the Java OLD_TAG (release tags are
   immutable, so a tag-matched snapshot cannot rot; a rotated OLD_TAG with
   a stale manifest fails with regeneration instructions), its content
   hash matches the embedded integrity field (nexus-4sl9k: everything in
   the snapshot is SUBTRACTED from the required-coverage set, so a single
   hand-added entry would otherwise silently defeat the gate — the hash is
   computed only by the generator, from git truth; the documented residual
   is deliberate hand-forgery of the hash, which carries the same intent
   as editing this lint's assertions), and its ``(id, author)`` pairs are
   unique (Liquibase identity is ``(id, author, filename)``; a cross-file
   duplicate pair would make the hop subtraction silently drop a genuinely
   new changeset whose key collides with a pre-OLD_TAG one). No
   git/network at test time — this lint has NO skip path (feedback: gates
   are scripted, never ambient).
3. Derive, from the HEAD changelog, every changeset carrying migration-time
   row-DML risk on a FORCE-RLS table. Two detection rules:
   - a literal top-level (or DO-block) INSERT/UPDATE/DELETE whose target OR
     any referenced ``nexus.``/``t1.`` table was FORCE at changeset entry
     (in-changeset toggles do not exempt — the table is FORCE-managed and
     the rehearsal must prove the toggle discipline dynamically);
   - any ``NO FORCE ROW LEVEL SECURITY`` toggle in the changeset — the tell
     for the ``SELECT some_fn()`` static blind spot (catalog-014-0's shape:
     the DML hides inside a function body the analyzer cannot see, but that
     one real instance toggles FORCE in its own ``<sql>`` body, making the
     toggle visible). This is an EMPIRICAL tell, not a structural
     guarantee (same hedge as the RLS lint's own SELECT-fn() disclosure):
     a future fn-hiding changeset needing NO caller-side toggle (e.g. a
     SECURITY DEFINER function, or one toggling FORCE inside its own body)
     would escape both rules.
4. Hop set = derived(HEAD) minus the snapshot's changesets. Assert it
   EQUALS (exact ``==``) the rehearsal's declared seed coverage — which is
   itself asserted equal to the SEED-COVERAGE contract block inside the
   Java data leg, so the declaration cannot move without a diff to the
   rehearsal file the coverage claim is about.

Inherited/documented blind spots (mirroring the RLS lint's own admissions):

- **CTE-prefixed DML** (``WITH x AS (DELETE FROM nexus.t ...) INSERT ...``)
  is invisible to the shared ``_DML_TARGET_RE`` (literal leading
  INSERT/UPDATE/DELETE only) — inherited verbatim from the RLS lint, which
  documents it as grep-verified absent from the real corpus; if such a
  statement also carries no FORCE toggle it escapes BOTH rules here.
- **``sqlFile`` / ``customChange`` / ``createProcedure``** elements cannot
  be scanned; unlike the RLS lint (which reports them as findings), this
  derivation RAISES on sight — extend both lints before using one.
- Rule (a)'s entry-state semantics implicitly lean on the RLS lint
  independently gating the one shape entry-state cannot see: a changeset
  that establishes FORCE for the first time and row-DMLs pre-existing rows
  in the SAME changeset is entry-False/toggle-free here, but is a live
  NAKED_DML finding there (php10 forces it into toggle-wrap — then caught
  by rule (b) — or a same-changeset backstop, which is safe-or-loud without
  rehearsal data). Loosening the RLS lint loosens this lint's coverage.
- **Accepted residual** (same class as the hash-forgery one above): the
  contract-block↔declaration parity cannot verify the Java data leg
  SEMANTICALLY seeds what the block claims — adding a new hop changeset to
  both declarations without writing real seed code satisfies every
  mechanical check here. What the parity buys is the diff locus: that
  fabrication now requires an edit inside the rehearsal file itself,
  adjacent to the real seed code, in front of the reviewer.

When the hop gate fails, the fix is never to edit the declarations alone:
extend the rehearsal's data leg to seed the new changeset's input shape and
assert its effect (see the data-leg javadoc's "template to extend"), THEN
update the Java SEED-COVERAGE block and the declaration below, together.
On OLD_TAG rotation: regenerate the manifest
(``uv run python scripts/gen_rehearsal_hop_manifest.py``), re-derive the new
hop's coverage, and re-point all three (Java seeding, Java contract block,
Python declaration).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from gen_rehearsal_hop_manifest import manifest_integrity, parse_old_tag
from tests.test_changelog_rls_lint import (
    CHANGELOG_DIR,
    MASTER_CHANGELOG,
    _DML_TARGET_RE,
    _FORCE_TOGGLE_RE,
    _LEADING_DO_BEGIN_RE,
    _TABLE_MENTION_RE,
    _split_statements,
    _table_key,
    parse_master_include_order,
)

REPO_ROOT = Path(__file__).parent.parent
JAVA_REHEARSAL_TEST = (
    REPO_ROOT
    / "service/src/test/java/dev/nexus/service/SchemaUpgradeRehearsalIntegrationTest.java"
)
MANIFEST_PATH = REPO_ROOT / "tests/data/rehearsal_old_tag_changesets.json"

_XSD_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"
_UNSCANNED_ELEMENT_TAGS = ("sqlFile", "customChange", "createProcedure")

_SEED_COVERAGE_BLOCK_RE = re.compile(
    r"SEED-COVERAGE-BEGIN.*?\):(?P<body>.*?)SEED-COVERAGE-END", re.DOTALL
)
_SEED_COVERAGE_LINE_RE = re.compile(r"//\s+(\S+)\s+(\S+)\s*$", re.MULTILINE)

#: The rehearsal data leg's DECLARED seed coverage — the (id, author) pairs of
#: every hop changeset whose row-DML the leg seeds inputs for and asserts the
#: effect of. Kept in mechanical parity with the SEED-COVERAGE contract block
#: inside SchemaUpgradeRehearsalIntegrationTest's data-leg method (asserted
#: below) — neither side can move alone:
#:   catalog-013-0 / catalog-013-1b — legacy 64-char chash_index rows
#:   catalog-014-0                  — un-stamped manifest rows (collection NULL)
#:   catalog-025-0                  — NULL/dangling manifest row DELETE, NOT NULL SET
#:   vectors-004-1 / taxonomy-007-1 — RDR-191 Phase 4 unify: straddling
#:                                     per-dim chunks_384/768/1024 and
#:                                     taxonomy_centroids_384/768/1024 content
#:   catalog-032-1                  — RDR-194 P1 (D2): catalog_links dangling
#:                                     tumbler-endpoint anti-join DELETE
DECLARED_SEED_COVERAGE: frozenset[tuple[str, str]] = frozenset(
    {
        ("catalog-013-0", "nexus-e0hd2"),
        ("catalog-013-1b", "nexus-1wjmq"),
        ("catalog-014-0", "nexus-x6kdz"),
        # nexus-78n33: the source_uri dedup backfill (FORCE-RLS-toggled row
        # DML) — seeded as duplicate live-uri docs 1.1.201/1.1.202 in the
        # data leg; effect-asserted (loser tombstoned, winner survives,
        # 016-1 index exists).
        ("catalog-016-0", "nexus-78n33"),
        # nexus-71gw2 (nexus-j862l follow-up): catalog_document_chunks.collection
        # NOT NULL — its own NO FORCE/FORCE toggle wraps a DELETE-null +
        # DELETE-dangling + chunk_count resync + SET NOT NULL body. Seeded as
        # doc 1.1.101's deliberately content-less manifest row (stamped by
        # catalog-014-0's backfill, same as 1.1.100's two rows, but with no
        # matching chunks_384/768/1024 content); effect-asserted (the row is
        # gone post-hop — proves the DELETE arm fires under FORCE-RLS, not
        # just that 1.1.100's content-backed rows survive the KEEP arm).
        ("catalog-025-0", "nexus-71gw2"),
        # nexus-o8dil.12 (RDR-191 Phase 4 unify): vectors-004-1 collapses
        # chunks_384/768/1024 into nexus.chunks -- FORCE-RLS row-DML solely
        # via its own step-0 NO FORCE toggle (defusing the DML-blindness trap
        # before the copy), no literal top-level DML keyword. Seeded as
        # straddling per-dim content (chunks_384 reuses the existing j862l
        # rows; chunks_768/1024 get fresh rows); effect-asserted (each row
        # lands in nexus.chunks under the correct typed embedding_<dim>
        # column, the three source tables are gone).
        ("vectors-004-1", "nexus-o8dil.12"),
        # nexus-jv3ue (RDR-191 Phase 4 unify, Hal ruling o8dil.47): the
        # analogous collapse of taxonomy_centroids_384/768/1024 into
        # nexus.taxonomy_centroids, same FORCE-RLS row-DML mechanism and same
        # straddling-seed/effect-assert shape as vectors-004-1 (all three
        # dims freshly seeded here, no chunks-style reuse available).
        ("taxonomy-007-1", "nexus-jv3ue"),
        # nexus-o8dil.29 (RDR-191 Phase 5): catalog-029-1's anti-join DELETE
        # runs inside the same NO FORCE/FORCE toggle-wrap shape as
        # catalog-013-1b/catalog-014-0/catalog-025-0. Reuses the 1.1.100
        # KEEP-arm fixture rather than a fresh dangling row: any row that
        # would exercise catalog-029-1's DELETE arm is, by construction,
        # ALSO dangling under catalog-025-0's structurally identical
        # anti-join (against the pre-unify per-dim tables vectors-004-1
        # copies verbatim into nexus.chunks earlier in this same hop), so
        # catalog-025-0 would remove it first; effect-asserted via the
        # KEEP-arm survival (1.1.100's two content-backed rows still present)
        # plus fk_catalog_chunks_chunk existing and VALIDATED at HEAD.
        ("catalog-029-1", "nexus-o8dil.29"),
        # nexus-o8dil.49 (RDR-191 Phase 5): fk-004-1-reconcile's additive
        # stub-register, same NO FORCE/FORCE toggle-wrap shape as
        # catalog-029-1. Structurally always finds zero unregistered rows in
        # this hop (fk-002, already applied at OLD_TAG, enforces collection
        # registration on every chunks_384/768/1024 write before this hop
        # even starts, and vectors-004-1 only copies already-FK-compliant
        # rows into nexus.chunks) -- effect-asserted via chunks_collection_fk
        # existing+VALIDATED at HEAD plus FORCE ROW LEVEL SECURITY restored
        # on both nexus.chunks and nexus.catalog_collections.
        ("fk-004-1-reconcile", "nexus-o8dil.49"),
        # nexus-iq0qr (RDR-191 Phase 5 follow-up): fk-004-0-reconcile-precount
        # is a READ-ONLY audit changeset, positioned in FILE ORDER immediately
        # BEFORE fk-004-1-reconcile, that RAISE NOTICEs the anti-join
        # pre-count of unregistered (tenant_id, collection) pairs
        # fk-004-1-reconcile is about to insert (adding the auditability
        # fk-004-1-reconcile's own frozen body can never carry, conexus
        # deploy receipt [22579]). It carries no INSERT/UPDATE/DELETE, but
        # its own NO FORCE/FORCE toggle (needed so the SELECT can see real
        # rows under FORCE RLS, the identical trap fk-004-1-reconcile's own
        # header documents for its INSERT...SELECT) trips this lint's rule
        # (b) regardless. By the identical structural reasoning as
        # fk-004-1-reconcile's own entry immediately above, the pre-count is
        # ALWAYS zero in this hop -- effect-asserted the same way (changeset
        # EXECUTES; FORCE restored on both toggled tables), no new seed data
        # needed.
        ("fk-004-0-reconcile-precount", "nexus-iq0qr"),
        # nexus-tk070.p1 (RDR-194 Phase P1, Decision D2): catalog-032-1's
        # anti-join DELETE on nexus.catalog_links, the SAME NO FORCE/FORCE
        # toggle-wrap shape as catalog-013-1b/catalog-014-0/catalog-025-0/
        # catalog-029-1. UNLIKE catalog-029-1 (which could reuse the 1.1.100
        # KEEP-arm fixture because any row exercising its DELETE arm was
        # ALREADY dead via catalog-025-0's identical anti-join earlier in the
        # hop), catalog_links carries NO prior FK-backed remediation anywhere
        # in this hop -- catalog-032-1 is the FIRST FK ever added to this
        # table -- so a fresh dangling-row seed is required to exercise the
        # DELETE arm at all. Seeded as two catalog_links rows sharing
        # from_tumbler=1.1.100 (already registered above): one to 1.1.101 (a
        # real, already-seeded document -- the KEEP arm) and one to
        # 1.1.999-ghost (never registered -- the DELETE arm, modeling the
        # real aged-fleet population, 277 rows, nexus-ysrwi 2026-07-25);
        # effect-asserted (the ghost-endpoint row is gone, the real edge
        # survives, both FKs exist+VALIDATED at HEAD, FORCE restored on both
        # toggled tables).
        ("catalog-032-1", "nexus-tk070.p1"),
        # nexus-o8dil.37 (RDR-191 Phase 7): NO changeset here -- a dedicated
        # catalog-030 changeset was drafted and DROPPED (2026-08-15, standing
        # convergence directive) once found structurally vestigial:
        # catalog-025-collection-not-null.xml (nexus-71gw2) already closes the
        # entire NULL-collection population and promotes SET NOT NULL,
        # strictly earlier in db.changelog-master.xml's include order, on
        # every real deployment. Phase 7 is closed on catalog-025's own
        # evidence.
        # nexus-lgdel.l1 (epic nexus-lgdel, THE DELETE): legacy-001-1/
        # legacy-001-2's shape-agnostic DELETE (chunk_id !~ '^[0-9a-f]{64}$')
        # on nexus.frecency/nexus.relevance_log, the SAME NO FORCE/FORCE
        # toggle-wrap shape as catalog-013-1b/catalog-014-0/catalog-025-0/
        # catalog-029-1/catalog-032-1. Seeded as one legacy-width (32-hex)
        # and one canonical-width (64-hex) row per table -- the DELETE/KEEP
        # dual-arm proof; effect-asserted (the legacy-width row is gone
        # post-hop, the canonical-width row survives, FORCE restored on
        # both tables).
        ("legacy-001-1", "nexus-lgdel.l1"),
        ("legacy-001-2", "nexus-lgdel.l1"),
        # nexus-tk070.p3b (RDR-194 § D1 steps b/c/d): taxonomy-010-1's
        # source_collection backfill + counted delete of the ambiguous/
        # unresolvable remainders + SET NOT NULL, all in one changeset,
        # RLS toggle-wrapped around the three DML statements (nexus.
        # topic_assignments and nexus.chunks both FORCE RLS). Seeded as
        # three topic_assignments rows sharing one topic, all DELETE arms:
        # an ambiguous row (chash under two collections, deleted), an
        # unresolvable row (conformant 64-hex shape, no matching chunk at
        # all, deleted -- proves the anti-join arm independently of the
        # shape predicate), and a legacy-shape-coincidental-match row
        # (32-hex doc_id that DOES text-match a real chunk, deleted anyway
        # -- the cc4/HAL no-wedge proof that the shape predicate wins over
        # a coincidental anti-join match); effect-asserted (all three
        # gone, FORCE restored on both toggled tables, source_collection is
        # SET NOT NULL at HEAD). The positive unique-resolution BACKFILL
        # arm cannot be constructed in this old-tag-hop fixture (the OLD
        # leg's chunks_384/768/1024 carry a length(chash)=32 CHECK, so
        # every chash seedable pre-hop is legacy-width and therefore
        # excluded from backfill candidacy by taxonomy-010-1's own shape
        # guard) -- it is proven separately by a dedicated HEAD-schema
        # integration test (Taxonomy010BackfillDirectIntegrationTest),
        # outside the seed-coverage lint's scope (that lint only covers
        # the hop's own toggle-wrapped row-DML changesets).
        ("taxonomy-010-1", "nexus-tk070.p3b"),
        # nexus-tk070.p3c (RDR-194 § D1 step (e)): taxonomy-011-1's doc_id
        # TEXT -> bytea ALTER carries NO INSERT/UPDATE/DELETE of its own --
        # its guard is a read-only SELECT COUNT(*) wrapped in the SAME NO
        # FORCE/FORCE toggle shape as fk-004-0-reconcile-precount
        # (nexus-iq0qr, above), which trips rule (b) regardless of carrying
        # no literal DML. No NEW seed data needed: taxonomy-010-1's own
        # seeded rows (immediately above in the same hop) ARE the
        # population the guard walks, and by construction every surviving
        # row is canonical 64-hex once taxonomy-010-1's three DELETE arms
        # have run -- the guard is expected to find zero bad rows, exactly
        # mirroring fk-004-0-reconcile-precount's "always zero in this hop"
        # reasoning. Effect-asserted the same minimal way: the changeset
        # EXECUTES (a RAISE EXCEPTION would abort the whole migration walk
        # before any assertion could run), doc_id is bytea at HEAD, and
        # FORCE ROW LEVEL SECURITY is restored on topic_assignments.
        ("taxonomy-011-1", "nexus-tk070.p3c"),
        # nexus-tk070.p3d (RDR-194 § D1): taxonomy-012-2's composite-FK
        # anti-join DELETE on nexus.topic_assignments (tenant_id,
        # source_collection, doc_id) -> nexus.chunks (tenant_id, collection,
        # chash), the same NO FORCE/FORCE toggle-wrap shape as
        # catalog-013-1b/catalog-014-0/catalog-025-0/catalog-029-1/
        # catalog-032-1/legacy-001-1/legacy-001-2. Reuses taxonomy-010-1's
        # seed rather than a fresh fixture: by the time this changeset runs,
        # taxonomy-010-1's own three DELETE arms have already removed every
        # row the fixture seeded (the ambiguous, unresolvable, and both
        # shape-invalid rows), so taxonomy-012-2's anti-join finds zero
        # dangling rows in this hop by construction -- the same
        # already-dead-by-an-earlier-identical-anti-join reasoning as
        # catalog-029-1's own entry above; effect-asserted (the population
        # is independently still zero, topic_assignments_chunk_fk exists and
        # is VALIDATED at HEAD, FORCE restored on both toggled tables).
        ("taxonomy-012-2", "nexus-tk070.p3d"),
        # nexus-tk070.p5a (RDR-194 Phase P5, Decision D4): taxonomy-014-2/-3/
        # -4/-5's four FK repoints onto nexus.topics' new UNIQUE (tenant_id,
        # id), the same NO FORCE/FORCE toggle-wrap shape as taxonomy-012-2
        # above but FAIL-LOUD on a nonzero cross-tenant anti-join rather than
        # deleting it (a cross-tenant topic reference is corruption, not a
        # population to remediate silently). No new seed data needed: the
        # sole nexus.topics row this hop seeds (taxonomy-010-1's FK parent,
        # parent_id NULL) and the fact that nexus.topic_assignments/
        # nexus.topic_links carry zero rows for this hop's tenant by the time
        # these changesets run (topic_assignments already drained to zero by
        # taxonomy-010-1's own three DELETE arms; topic_links never seeded at
        # all) make all four anti-joins structurally zero in this hop, the
        # same "always zero" reasoning as fk-004-0-reconcile-precount /
        # taxonomy-011-1 / taxonomy-012-2 above; effect-asserted the same
        # minimal way (each changeset EXECUTES, its own anti-join population
        # is independently still zero, its new composite FK exists and is
        # VALIDATED at HEAD, FORCE restored on every table its own
        # toggle-wrap covers). taxonomy-014-1 (the UNIQUE add) carries no
        # DML/toggle of its own and is not derived by this lint's rules --
        # it is still effect-asserted in the Java leg (existence at HEAD)
        # but needs no coverage entry here.
        ("taxonomy-014-2", "nexus-tk070.p5a"),
        ("taxonomy-014-3", "nexus-tk070.p5a"),
        ("taxonomy-014-4", "nexus-tk070.p5a"),
        ("taxonomy-014-5", "nexus-tk070.p5a"),
        # nexus-tk070.p6a (RDR-194 § D5): memory-003-1 / plans-003-1's
        # counted DELETE of ttl=0 rows, the same NO FORCE/FORCE toggle-wrap
        # shape as catalog-013-1b/catalog-014-0/catalog-025-0/catalog-029-1/
        # catalog-032-1/legacy-001-1/legacy-001-2. Seeded as one ttl=0 row
        # (DELETE arm) and one NULL-ttl row (KEEP arm, permanent) per table;
        # effect-asserted (the ttl=0 rows are gone, the NULL-ttl rows
        # survive, both columns renamed to ttl_days, both CHECK constraints
        # exist, FORCE restored on both tables).
        ("memory-003-1", "nexus-tk070.p6a"),
        ("plans-003-1", "nexus-tk070.p6a"),
        # nexus-tk070.p6b (RDR-194 § D5): telemetry-006-1's counted UPDATE
        # (0 -> NULL, the ONLY genuine population rewrite in the arc —
        # memory-003-1/plans-003-1 above are DELETEs) on nexus.frecency, the
        # same NO FORCE/FORCE toggle-wrap shape as memory-003-1/plans-003-1/
        # legacy-001-1/legacy-001-2. Seeded as one ttl_days=0 row (CONVERSION
        # arm) and one positive-ttl_days row (KEEP arm — a NULL KEEP arm
        # cannot be seeded pre-hop, since nexus.frecency.ttl_days is still
        # NOT NULL at OLD_TAG; the permanent-sentinel arm is proven instead
        # by TelemetryRepositoryTest's own fresh-HEAD-schema test); also
        # effect-asserts that legacy-001-1's own pre-existing canonical-width
        # KEEP-arm row (DEFAULT ttl_days=0) is likewise converted, proving
        # the UPDATE reaches every ttl_days=0 row in the hop, not merely the
        # dedicated fixture.
        ("telemetry-006-1", "nexus-tk070.p6b"),
        # telemetry-006-2 (staging.frecency's mirrored UPDATE) needs NO new
        # seed data: staging.frecency does not exist at OLD_TAG at all
        # (staging-001-landing-tables.xml creates it fresh, itself INSIDE
        # this hop, strictly before telemetry-006-2 runs) and nothing else
        # in the hop populates it before telemetry-006-2 runs, so its
        # UPDATE structurally operates over zero rows in this rehearsal —
        # not merely un-seeded, the same "always zero in this hop, no new
        # seed needed" reasoning as fk-004-1-reconcile / taxonomy-011-1
        # above. Effect-asserted the minimal way: the changeset EXECUTES,
        # and the DROP DEFAULT/DROP NOT NULL shape (with no CHECK, staging
        # stays typeless) lands at HEAD.
        ("telemetry-006-2", "nexus-tk070.p6b"),
        # nexus-ubnwk (RDR-156 Decision 5): aspects-004-1's doc_id backfill —
        # its own NO FORCE/FORCE toggle wraps an UPDATE joining
        # nexus.document_aspects to nexus.catalog_documents on exact
        # source_uri equality (both tables toggled, the catalog-014-0
        # both-tables lesson). Seeded as a legacy NULL-doc_id
        # document_aspects row whose source_uri exactly matches a fresh
        # catalog document (tumbler 1.1.204); effect-asserted (doc_id is
        # stamped to 1.1.204 post-hop, FORCE restored on both tables).
        ("aspects-004-1", "nexus-ubnwk"),
        # nexus-v3w9n (Sam decision, tumbler-grammar data remediation):
        # catalog-034-0's own NO FORCE/FORCE toggle wraps an UPDATE
        # tombstoning every live catalog_documents row with fewer than 3
        # non-empty dot-separated tumbler segments (the 2026-08-28 census's
        # two phantom registrations, "1.1"/"1.2"). Seeded as a fresh
        # 2-segment ghost tumbler ("1.5", the DELETE/tombstone arm) plus a
        # conforming 3-segment control row ("1.1.205", the KEEP arm);
        # effect-asserted (the ghost row is tombstoned, the control row
        # survives untouched, FORCE restored on catalog_documents via the
        # existing generic check aspects-004-1 already exercises).
        ("catalog-034-0", "nexus-v3w9n"),
        # hygiene-001-1..11 (nexus-tk070.p6a follow-on, coordinator review
        # round item 2): the schema-hygiene NOT NULL arc's twelve FORCE-RLS
        # row-DML changesets (hygiene-001-1..9, 9b, 10, 11 -- 8b is a
        # BEFORE INSERT trigger, no row-DML, not in the hop derivation).
        # Each is seeded with a DEDICATED row (or, for -4/-6/-7/-9, an
        # EXISTING row from earlier in this same seed block whose relevant
        # column was already left NULL by its own helper) and
        # effect-asserted (deleted / backfilled / value-correct) — see the
        # data leg's own per-changeset comments immediately above each
        # seed/assert pair for the full per-row rationale.
        ("hygiene-001-1", "nexus-tk070.p6a"),
        ("hygiene-001-2", "nexus-tk070.p6a"),
        ("hygiene-001-3", "nexus-tk070.p6a"),
        ("hygiene-001-4", "nexus-tk070.p6a"),
        ("hygiene-001-5", "nexus-tk070.p6a"),
        ("hygiene-001-6", "nexus-tk070.p6a"),
        ("hygiene-001-7", "nexus-tk070.p6a"),
        ("hygiene-001-8", "nexus-tk070.p6a"),
        ("hygiene-001-9", "nexus-tk070.p6a"),
        ("hygiene-001-9b", "nexus-tk070.p6a"),
        ("hygiene-001-10", "nexus-tk070.p6a"),
        ("hygiene-001-11", "nexus-tk070.p6a"),
    }
)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def parse_java_seed_coverage(java_source: str) -> set[tuple[str, str]]:
    """The SEED-COVERAGE contract block inside the Java data leg (strict)."""
    m = _SEED_COVERAGE_BLOCK_RE.search(java_source)
    if m is None:
        raise AssertionError(
            "could not find the SEED-COVERAGE-BEGIN/END contract block in "
            "SchemaUpgradeRehearsalIntegrationTest.java — it moved or was "
            "deleted; the block is this lint's mechanical tie to the actual "
            "rehearsal seeding (no fallback)"
        )
    pairs = {
        (mid, author)
        for mid, author in _SEED_COVERAGE_LINE_RE.findall(m.group("body"))
        if mid != "SEED-COVERAGE-END"
    }
    if not pairs:
        raise AssertionError(
            "SEED-COVERAGE contract block parsed empty — malformed block"
        )
    return pairs


def _iter_changesets_with_author(changelog_dir: Path, basename: str):
    """Yield ``(id, author, sql_text)`` per changeSet, document order.

    Own iterator (the shared one yields no author): only direct ``<sql>``
    children are read, so ``<rollback>`` bodies are structurally excluded —
    rollback SQL never runs at migration time. An element kind this
    analyzer family cannot see into RAISES (the RLS lint reports the same
    class as an unsuppressible finding; this derivation must not silently
    under-derive past one).
    """
    root = ET.parse(changelog_dir / basename).getroot()
    for cs in root.iter(f"{_XSD_NS}changeSet"):
        for tag in _UNSCANNED_ELEMENT_TAGS:
            if cs.find(f"{_XSD_NS}{tag}") is not None:
                raise AssertionError(
                    f"changeset {cs.get('id')!r} in {basename} uses <{tag}> — "
                    "unscanned Liquibase element; extend this lint (and the "
                    "RLS lint) before relying on either for FORCE-RLS DML "
                    "coverage"
                )
        sql_texts = [el.text or "" for el in cs.findall(f"{_XSD_NS}sql") if el.text]
        yield cs.get("id", ""), cs.get("author", ""), "\n".join(sql_texts)


def derive_force_row_dml_changesets(
    changelog_dir: Path = CHANGELOG_DIR,
    master_path: Path = MASTER_CHANGELOG,
) -> set[tuple[str, str]]:
    """Every (id, author) whose changeset carries FORCE-RLS row-DML risk.

    Rule (a): a literal DML statement whose target or any referenced table
    was FORCE at changeset ENTRY (pre-changeset state — in-changeset toggles
    do not exempt). Rule (b): the changeset issues any ``NO FORCE`` toggle
    (the SELECT-fn() blind-spot tell — empirical, see module docstring).

    Self-checks: the master include list must match the directory contents
    (a file on disk but not included would silently escape the walk), and
    ``(id, author)`` pairs must be unique across the whole walked tree —
    the hop subtraction keys on that pair, so a cross-file duplicate would
    let a NEW changeset silently hide behind a pre-OLD_TAG key.
    """
    include_order = parse_master_include_order(master_path)
    on_disk = {
        p.name for p in changelog_dir.glob("*.xml") if p.name != master_path.name
    }
    assert set(include_order) == on_disk, (
        "master include list drifted from the changelog directory: "
        f"included-not-on-disk={set(include_order) - on_disk}, "
        f"on-disk-not-included={on_disk - set(include_order)}"
    )

    global_force: dict[str, bool] = {}
    marked: set[tuple[str, str]] = set()
    seen_keys: set[tuple[str, str]] = set()

    for basename in include_order:
        for cs_id, author, sql_text in _iter_changesets_with_author(
            changelog_dir, basename
        ):
            key = (cs_id, author)
            assert key not in seen_keys, (
                f"duplicate changeset key {key} (second occurrence in "
                f"{basename}) — Liquibase identity is (id, author, filename) "
                "but the hop subtraction keys on (id, author); a cross-file "
                "duplicate would silently hide a new changeset behind a "
                "pre-OLD_TAG one. Rename the new changeset id."
            )
            seen_keys.add(key)

            entry_force = dict(global_force)
            live_force = dict(global_force)
            toggled_no_force = False

            for stmt in _split_statements(sql_text):
                m = _FORCE_TOGGLE_RE.search(stmt)
                if m and stmt.upper().lstrip().startswith("ALTER TABLE"):
                    tkey = _table_key(m.group(1), m.group(2))
                    is_no_force = bool(m.group(3))
                    live_force[tkey] = not is_no_force
                    if is_no_force:
                        toggled_no_force = True
                    continue

                stmt_for_dml = _LEADING_DO_BEGIN_RE.sub("", stmt)
                dml = _DML_TARGET_RE.match(stmt_for_dml)
                if dml:
                    touched = {_table_key(dml.group(1), dml.group(2))}
                    touched.update(
                        _table_key(g[0], g[1])
                        for g in _TABLE_MENTION_RE.findall(stmt)
                    )
                    if any(entry_force.get(t) for t in touched):
                        marked.add(key)

            if toggled_no_force:
                marked.add(key)
            global_force = live_force

    return marked


def load_manifest() -> tuple[str, set[tuple[str, str]]]:
    """Load + integrity-verify the OLD_TAG snapshot (nexus-4sl9k)."""
    data = json.loads(MANIFEST_PATH.read_text())
    tag = data["tag"]
    changesets = data["changesets"]
    expected = manifest_integrity(tag, changesets)
    assert data.get("integrity") == expected, (
        "rehearsal OLD_TAG snapshot failed its integrity check — the file "
        "was edited by hand (everything in it is SUBTRACTED from the "
        "required seed coverage, so a hand-added entry silently defeats the "
        "gate). NEVER hand-edit; regenerate from the immutable tag: "
        "uv run python scripts/gen_rehearsal_hop_manifest.py"
    )
    pairs = {(c["id"], c["author"]) for c in changesets}
    assert len(pairs) == len(changesets), (
        "duplicate (id, author) pairs inside the OLD_TAG snapshot — "
        "regenerate and investigate (Liquibase identity is per-file; the "
        "subtraction keys on the pair)"
    )
    return tag, pairs


# ---------------------------------------------------------------------------
# Synthetic detector tests (fixtures mirror the RLS lint's harness)
# ---------------------------------------------------------------------------


def _write_changelog(tmp_path: Path, changesets_xml: str) -> tuple[Path, Path]:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<databaseChangeLog\n"
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
    )
    (changelog_dir / "synthetic-001.xml").write_text(
        f"{header}{changesets_xml}\n</databaseChangeLog>\n"
    )
    master = changelog_dir / "db.changelog-master.xml"
    master.write_text(
        f'{header}    <include file="synthetic-001.xml"/>\n</databaseChangeLog>\n'
    )
    return changelog_dir, master


_FORCE_WIDGETS = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
"""


def test_naked_dml_on_force_table_is_derived(tmp_path):
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-dml" author="t">
        <sql splitStatements="true">
DELETE FROM nexus.widgets WHERE stale = true;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-dml", "t")}


def test_toggle_wrapped_dml_is_still_derived(tmp_path):
    """The rehearsal must seed toggle-wrapped changesets too — the leg is the
    DYNAMIC proof the toggle discipline works (catalog-013-1b shape)."""
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-wrapped" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets NO FORCE ROW LEVEL SECURITY;
UPDATE nexus.widgets SET name = substr(name, 1, 8);
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-wrapped", "t")}


def test_toggle_only_changeset_is_derived_via_the_fn_call_tell(tmp_path):
    """catalog-014-0's exact blind-spot shape: no literal DML keyword at all
    (the DML hides inside SELECT some_fn()), but the NO FORCE toggle is the
    visible tell."""
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-fn" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets NO FORCE ROW LEVEL SECURITY;
SELECT nexus.widget_backfill();
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-fn", "t")}


def test_dml_on_never_force_table_is_not_derived(tmp_path):
    xml = """
    <changeSet id="cs-plain" author="t">
        <sql splitStatements="true">
UPDATE nexus.service_tokens SET scope = 'root' WHERE label = 'x';
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == set()


def test_do_block_dml_on_force_table_is_derived(tmp_path):
    """taxonomy-004-1 shape: DML inside DO $$ ... $$ executes at migration
    time and must be derived, never stripped as an exempt function body."""
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-do" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    DELETE FROM nexus.widgets WHERE stale = true;
END $$;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-do", "t")}


def test_create_function_body_dml_is_not_derived(tmp_path):
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-createfn" author="t">
        <sql splitStatements="false">
CREATE OR REPLACE FUNCTION nexus.widget_trash(wid text)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER
AS $$
BEGIN
    UPDATE nexus.widgets SET deleted_at = NOW() WHERE id = wid;
END;
$$
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == set()


def test_dml_reading_a_force_table_is_derived(tmp_path):
    """INSERT into a never-FORCE table SELECTing from a FORCE one is still
    data-dependent under RLS (the read side sees zero rows) — derived."""
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-read" author="t">
        <sql splitStatements="true">
INSERT INTO nexus.summary (name) SELECT name FROM nexus.widgets;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-read", "t")}


def test_duplicate_changeset_key_fails_loud(tmp_path):
    """A cross-changeset (id, author) duplicate would let a NEW changeset
    hide behind a pre-OLD_TAG key in the hop subtraction — hard error."""
    xml = """
    <changeSet id="cs-dup" author="t">
        <sql splitStatements="true">CREATE TABLE nexus.a (id int);</sql>
    </changeSet>
    <changeSet id="cs-dup" author="t">
        <sql splitStatements="true">CREATE TABLE nexus.b (id int);</sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    with pytest.raises(AssertionError, match="duplicate changeset key"):
        derive_force_row_dml_changesets(d, m)


def test_unscanned_element_fails_loud(tmp_path):
    xml = """
    <changeSet id="cs-unscanned" author="t">
        <sqlFile path="some.sql"/>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    with pytest.raises(AssertionError, match="unscanned Liquibase element"):
        derive_force_row_dml_changesets(d, m)


def test_old_tag_parse_miss_fails_loud():
    with pytest.raises(AssertionError, match="could not parse OLD_TAG"):
        parse_old_tag("public class Foo { /* no constant here */ }")


def test_manifest_tamper_fails_integrity(tmp_path, monkeypatch):
    """nexus-4sl9k regression: a single hand-added snapshot entry (claiming
    a hop changeset already existed at OLD_TAG) must fail the integrity
    check — the exact silent-defeat the critic reproduced on the first cut."""
    data = json.loads(MANIFEST_PATH.read_text())
    data["changesets"].append(
        {"id": "catalog-099-fabricated", "author": "attacker", "file": "x.xml"}
    )
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data))
    monkeypatch.setattr(
        "tests.test_rehearsal_seed_coverage_lint.MANIFEST_PATH", tampered
    )
    with pytest.raises(AssertionError, match="integrity check"):
        load_manifest()


def test_java_contract_block_parse_miss_fails_loud():
    with pytest.raises(AssertionError, match="SEED-COVERAGE"):
        parse_java_seed_coverage("class Foo {}")


# ---------------------------------------------------------------------------
# The real-corpus gate
# ---------------------------------------------------------------------------


def test_manifest_tag_matches_java_old_tag():
    """OLD_TAG rotation tripwire: a rotated Java OLD_TAG with a stale
    snapshot fails HERE, with the regeneration instruction."""
    java_tag = parse_old_tag(JAVA_REHEARSAL_TEST.read_text())
    manifest_tag, changesets = load_manifest()
    assert manifest_tag == java_tag, (
        f"rehearsal OLD_TAG rotated to {java_tag!r} but the changeset "
        f"snapshot is still for {manifest_tag!r} — regenerate it: "
        "uv run python scripts/gen_rehearsal_hop_manifest.py, then re-derive "
        "the new hop's seed coverage (the Java data leg's seeding/gates, its "
        "SEED-COVERAGE block, and this lint's DECLARED_SEED_COVERAGE all "
        "need re-pointing)"
    )
    # Non-vacuity floor: an empty/truncated snapshot must never silently
    # subtract nothing (the v0.1.17 tree has 142; any real tag has >= 100).
    # Secondary to the integrity hash — kept as an independent belt.
    assert len(changesets) >= 100, (
        f"snapshot holds only {len(changesets)} changesets — truncated or "
        "misgenerated; regenerate via scripts/gen_rehearsal_hop_manifest.py"
    )


def test_declaration_matches_java_contract_block():
    """The Python declaration and the Java data leg's SEED-COVERAGE block
    must agree — neither can be edited alone (the block lives next to the
    seeding code, so moving coverage requires a diff to the rehearsal file
    the claim is about)."""
    java_pairs = parse_java_seed_coverage(JAVA_REHEARSAL_TEST.read_text())
    assert java_pairs == set(DECLARED_SEED_COVERAGE), (
        "DECLARED_SEED_COVERAGE and the Java SEED-COVERAGE contract block "
        "disagree — update them TOGETHER, alongside the actual seeding:\n"
        f"  Python-only: {sorted(set(DECLARED_SEED_COVERAGE) - java_pairs)}\n"
        f"  Java-only:   {sorted(java_pairs - set(DECLARED_SEED_COVERAGE))}"
    )


def test_detector_reproduces_known_historical_members():
    """Detector sanity against the real corpus: every historically-known
    FORCE-RLS row-DML member must be derived (containment, not exact — the
    full set legitimately GROWS with new safe changesets; the exact gate is
    the hop assertion below, and the synthetic tests pin detector behavior
    exactly)."""
    derived = derive_force_row_dml_changesets()
    known = {
        ("catalog-013-0", "nexus-e0hd2"),
        ("catalog-013-1b", "nexus-1wjmq"),
        ("catalog-014-0", "nexus-x6kdz"),
        ("taxonomy-004-1", "nexus-slcn7"),
        ("fk-002-0-backfill-stubs", "nexus-70r3c.2"),
        ("fk-002-6-reconcile", "nexus-70r3c.3"),
        ("fk-003-0-backfill-stubs", "nexus-dcqml"),
        ("fk-003-6-reconcile", "nexus-p9aw6"),
    }
    missing = known - derived
    assert not missing, (
        f"detector no longer derives known FORCE-RLS row-DML members: "
        f"{sorted(missing)} — the classification logic regressed"
    )


def test_hop_row_dml_changesets_equal_declared_rehearsal_seed_coverage():
    """THE GATE (nexus-gm38i): every FORCE-RLS row-DML changeset the
    old-tag-to-HEAD hop runs must be seed-covered by the rehearsal's data
    leg. Exact ``==``: a NEW hop changeset fails until the rehearsal is
    extended; a REMOVED one (OLD_TAG rotation) fails until coverage is
    re-derived."""
    _, old_tag_changesets = load_manifest()
    derived = derive_force_row_dml_changesets()
    hop = derived - old_tag_changesets
    assert hop == DECLARED_SEED_COVERAGE, (
        "the old-tag-to-HEAD hop's FORCE-RLS row-DML changesets drifted from "
        "the rehearsal's declared seed coverage.\n"
        f"  in hop but NOT seed-covered: {sorted(hop - DECLARED_SEED_COVERAGE)}\n"
        f"  declared but no longer in hop: {sorted(DECLARED_SEED_COVERAGE - hop)}\n"
        "Fix: extend SchemaUpgradeRehearsalIntegrationTest's data leg to seed "
        "the new changeset's input shape and assert its EFFECT (see the "
        "data-leg javadoc's 'template to extend'), then update the Java "
        "SEED-COVERAGE block and DECLARED_SEED_COVERAGE here, together. "
        "Never update the declarations alone."
    )
