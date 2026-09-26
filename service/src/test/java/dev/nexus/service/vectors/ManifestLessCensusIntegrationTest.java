// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 2 (bead nexus-wbfpw.4): {@link PgVectorRepository#manifestLessCensus}
 * classifies every chunk in a collection carrying no OWN-COLLECTION manifest row
 * into exactly one of five buckets, following the MVV (a) producer-to-bucket mapping
 * (docs/rdr/rdr-192-superseded-note-chunks-outlive-manifest-less-is-live.md).
 *
 * <p>One seeded row per bucket, plus a "current" chunk that DOES carry an
 * own-collection manifest row (must be excluded from the census entirely), a
 * fallback-lookup row proving {@code catalog_doc_id} absent falls back to
 * {@code doc_id}, and (round-1 fix, critic Critical) the REVERSE notes-guard path:
 * a chunk with NO forward key at all is still resolved when a live, note-shaped
 * {@code catalog_documents} row's OWN {@code metadata.doc_id} names this chunk's
 * chash — the identical predicate {@code CatalogRepository.sweepChunksQuery}'s
 * "nl3fn NOTES GUARD" and {@code src/nexus/indexer_utils.py::live_note_chashes}
 * already use. Fixture shape:
 * <ul>
 *   <li><strong>superseded</strong> — doc D1 live, has a manifest row in collection
 *       A for chash CURRENT; test chash OLD carries {@code catalog_doc_id=D1} and
 *       no manifest row of its own (a lost reap).</li>
 *   <li><strong>legacy-unmanifested</strong> — doc D2 live, zero manifest rows
 *       anywhere; test chash LEGACY carries {@code catalog_doc_id=D2}. A second
 *       chash, LEGACY_FALLBACK, carries only {@code doc_id=D2B} (no
 *       {@code catalog_doc_id}) against an equally manifest-less live doc D2B,
 *       proving the fallback lookup. A third chash, REV_ONLY, carries NO forward
 *       key at all (empty chunk metadata) and is resolved purely via the REVERSE
 *       path against live note-shaped doc D5, whose own {@code metadata.doc_id}
 *       names REV_ONLY's chash.</li>
 *   <li><strong>dead-owner</strong> — two distinct producers: doc D3 is
 *       tombstoned (chash TOMBSTONED); doc D4 is live but its only manifest row is
 *       in collection B, not A (chash RENAME_COPY) — the rename-COPY leftover,
 *       {@code CatalogRepository.java} ~8101-8125.</li>
 *   <li><strong>no-owner</strong> — chash NO_OWNER_EMPTY carries no
 *       {@code catalog_doc_id}/{@code doc_id} at all AND no live note-shaped
 *       document's own {@code metadata.doc_id} names it either (a {@code .nxexp}
 *       import); chash NO_OWNER_GHOST names a tumbler that was never
 *       registered.</li>
 *   <li><strong>unclassified</strong> — structurally unreachable given today's
 *       schema (the CASE in {@code manifest_less_census.sql} is exhaustive over
 *       owner-found/tombstoned/live x total-manifest-count/own-manifest-count);
 *       this suite instead pins the NEGATIVE property the bucket exists to
 *       guarantee — every one of the manifest-less rows gets a non-null bucket
 *       and the returned count equals the seeded population, so nothing is ever
 *       silently dropped.</li>
 * </ul>
 *
 * <p>Round-2 fix (critic + code-review Critical): owner precedence is "a LIVE
 * owner by either path beats a dead or absent one; forward wins over reverse only
 * when the forward-resolved owner is ITSELF live." Round 1 shipped an
 * unconditional "forward always wins" rule, which misclassified a chunk whose
 * forward pointer names a TOMBSTONED document while a SEPARATE live note
 * resolves the same chash via the reverse path as {@code dead-owner} instead of
 * {@code legacy-unmanifested} — exactly the row shape production's own
 * {@code sweepChunksQuery} notes-guard protects today. Two fixtures cover this:
 * <ul>
 *   <li>chash FWD_VS_REV — forward names TOMBSTONED doc D6; live note-shaped doc
 *       D7 ALSO carries {@code metadata.doc_id} = FWD_VS_REV's chash. D7 (live)
 *       RESCUES the classification: {@code legacy-unmanifested}, since D7 has no
 *       manifest rows anywhere. See {@link
 *       #forwardResolution_isRescuedByALiveReverseMatchWhenForwardOwnerIsDead}.</li>
 *   <li>chash FWD_LIVE_VS_REV — forward names LIVE doc D8, which has a manifest
 *       row in collection A for a DIFFERENT chash; a separate live note D9 ALSO
 *       carries {@code metadata.doc_id} = FWD_LIVE_VS_REV's chash. Forward (D8,
 *       live) wins here: {@code superseded}, not {@code legacy-unmanifested} —
 *       proving the reverse path is not simply preferred whenever it exists. See
 *       {@link #forwardResolution_winsWhenBothOwnersAreLive}.</li>
 * </ul>
 *
 * <p>Round-3 fix (both reviews, Significant): when MORE THAN ONE live
 * note-shaped document reverse-matches the same chash, the pick must be
 * deterministic and must prefer the candidate yielding the MOST CONSERVATIVE
 * bucket — zero manifest rows anywhere, i.e. legacy-unmanifested — over one
 * with fewer manifest rows purely by chance of tumbler ordering. Chash
 * TIE_TARGET is reverse-matched by BOTH D_TIE_SOME (tumbler sorts FIRST
 * alphabetically, but has a manifest row in collection B — total_count=1) and
 * D_TIE_ZERO (tumbler sorts AFTER D_TIE_SOME's, but has zero manifest rows
 * anywhere — total_count=0). D_TIE_ZERO must win despite its higher tumbler,
 * proving the tie-break orders by total manifest count first and tumbler only
 * as the FURTHER tie-break, never the reverse. See {@link
 * #reverseTieBreak_prefersTheMostConservativeBucket_deterministicallyAcrossLiteralAndBoundForms}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestLessCensusIntegrationTest {

    private static final String TENANT = "wbfpw4-census";
    private static final String SVC_ROLE = "svc_wbfpw4_census";
    private static final String SVC_PASS = "svc_wbfpw4_census_pass";
    private static final String COLLECTION_A = "knowledge__wbfpw4-fixture-a__minilm-l6-v2-384__v1";
    private static final String COLLECTION_B = "knowledge__wbfpw4-fixture-b__minilm-l6-v2-384__v1";
    private static final String COLLECTION_UNKNOWN = "knowledge__wbfpw4-fixture-unknown__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    PgVectorRepository vecRepo;
    HikariDataSource svcDs;

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        catalogRepo = new CatalogRepository(tenantScope);
        var embedder = new ConstantEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION_A);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION_B);
        }

        seedFixture();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture chashes ──────────────────────────────────────────────────────

    private static final String CURRENT          = ch("wbfpw4-current");
    private static final String OLD              = ch("wbfpw4-old-superseded");
    private static final String LEGACY           = ch("wbfpw4-legacy");
    private static final String LEGACY_FALLBACK  = ch("wbfpw4-legacy-fallback");
    private static final String REV_ONLY         = ch("wbfpw4-reverse-only");
    private static final String TOMBSTONED       = ch("wbfpw4-tombstoned");
    private static final String RENAME_COPY      = ch("wbfpw4-rename-copy");
    private static final String FWD_VS_REV       = ch("wbfpw4-forward-wins");
    private static final String FWD_LIVE_VS_REV  = ch("wbfpw4-forward-live-wins");
    private static final String FWD_LIVE_ANCHOR  = ch("wbfpw4-forward-live-anchor");
    private static final String MANIFEST_B_ONLY  = ch("wbfpw4-manifest-b-only");
    private static final String NO_OWNER_EMPTY   = ch("wbfpw4-no-owner-empty");
    private static final String NO_OWNER_GHOST   = ch("wbfpw4-no-owner-ghost");
    private static final String TIE_TARGET       = ch("wbfpw4-tie-target");
    private static final String TIE_SOME_MANIFEST = ch("wbfpw4-tie-some-manifest");

    private static final String D1  = "wbfpw4-doc-superseded";
    private static final String D2  = "wbfpw4-doc-legacy";
    private static final String D2B = "wbfpw4-doc-legacy-fallback";
    private static final String D3  = "wbfpw4-doc-tombstoned";
    private static final String D4  = "wbfpw4-doc-rename-copy";
    private static final String D5  = "wbfpw4-doc-reverse-only-note";
    private static final String D6  = "wbfpw4-doc-forward-target-tombstoned";
    private static final String D7  = "wbfpw4-doc-reverse-rescue";
    private static final String D8  = "wbfpw4-doc-forward-live-owner";
    private static final String D9  = "wbfpw4-doc-reverse-coincidence-live";
    /** Tumbler sorts alphabetically BEFORE D_TIE_ZERO's, yet must LOSE the tie-break
     *  (it has a manifest row elsewhere -> total_count=1, less conservative). */
    private static final String D_TIE_SOME = "wbfpw4-doc-tie-some-manifest";
    /** Tumbler sorts alphabetically AFTER D_TIE_SOME's, yet must WIN the tie-break
     *  (zero manifest rows anywhere -> total_count=0, most conservative). */
    private static final String D_TIE_ZERO = "wbfpw4-doc-tie-zero-manifest";

    /** Every manifest-less chash seeded into COLLECTION_A (11 total; CURRENT and
     *  FWD_LIVE_ANCHOR excluded — both carry their own own-collection manifest row). */
    private static final int TOTAL_MANIFEST_LESS_IN_A = 11;
    /** Every physical chunk row seeded into COLLECTION_A, manifest-less or not. */
    private static final int TOTAL_CHUNKS_IN_A = TOTAL_MANIFEST_LESS_IN_A + 2; // + CURRENT, FWD_LIVE_ANCHOR

    private void registerDoc(String tumbler, String physicalCollection) {
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler,
            "title", "RDR-192 census fixture " + tumbler,
            "content_type", "prose",
            "corpus", "knowledge",
            "physical_collection", physicalCollection
        ));
    }

    /**
     * Registers a live, NOTE-SHAPED document (no {@code file_path}, matching the
     * "nl3fn NOTES GUARD" / {@code live_note_chashes} predicate) whose OWN
     * {@code metadata.doc_id} names {@code identityChashHex} — the REVERSE
     * resolution path a chunk with no forward key falls back to.
     */
    private void registerNoteDoc(String tumbler, String physicalCollection, String identityChashHex) {
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler,
            "title", "RDR-192 census fixture " + tumbler,
            "content_type", "prose",
            "corpus", "knowledge",
            "physical_collection", physicalCollection,
            "metadata", Map.of("doc_id", identityChashHex)
        ));
    }

    private void seedFixture() {
        // D1: live, manifest row in A for CURRENT; OLD is the superseded leftover.
        registerDoc(D1, COLLECTION_A);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(CURRENT), List.of("current text"),
            List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, D1, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", CURRENT, "chunk_index", 0)));
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(OLD), List.of("old superseded text"),
            List.of(Map.of("catalog_doc_id", D1)));

        // D2: live, zero manifest rows anywhere -> legacy-unmanifested via catalog_doc_id.
        registerDoc(D2, COLLECTION_A);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(LEGACY), List.of("legacy note text"),
            List.of(Map.of("catalog_doc_id", D2)));

        // D2B: live, zero manifest rows anywhere -> legacy-unmanifested via the doc_id FALLBACK
        // (no catalog_doc_id present at all).
        registerDoc(D2B, COLLECTION_A);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(LEGACY_FALLBACK), List.of("legacy fallback text"),
            List.of(Map.of("doc_id", D2B)));

        // D5: live, note-shaped, zero manifest rows -> REV_ONLY carries NO forward key at
        // all (empty chunk metadata); resolved purely via D5's own metadata.doc_id.
        registerNoteDoc(D5, COLLECTION_A, REV_ONLY);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(REV_ONLY), List.of("reverse-only note text"),
            List.of(Map.of()));

        // D3: tombstoned -> dead-owner regardless of manifest state.
        registerDoc(D3, COLLECTION_A);
        catalogRepo.deleteDocument(TENANT, D3);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(TOMBSTONED), List.of("tombstoned owner text"),
            List.of(Map.of("catalog_doc_id", D3)));

        // D4: live, but its ONLY manifest row is in collection B (rename-COPY leftover) ->
        // dead-owner in A even though the doc itself is live.
        registerDoc(D4, COLLECTION_B);
        vecRepo.upsertChunks(TENANT, COLLECTION_B, List.of(MANIFEST_B_ONLY), List.of("manifest b only text"),
            List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, D4, COLLECTION_B,
            List.of(Map.<String, Object>of("position", 0, "chash", MANIFEST_B_ONLY, "chunk_index", 0)));
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(RENAME_COPY), List.of("rename copy leftover text"),
            List.of(Map.of("catalog_doc_id", D4)));

        // D6 (TOMBSTONED, the forward owner) vs D7 (live note-shaped, a COINCIDENTAL
        // reverse match on the same chash) -- D7 (live) RESCUES the classification: a
        // dead forward owner loses to a live reverse one. See
        // forwardResolution_isRescuedByALiveReverseMatchWhenForwardOwnerIsDead.
        registerDoc(D6, COLLECTION_A);
        catalogRepo.deleteDocument(TENANT, D6);
        registerNoteDoc(D7, COLLECTION_A, FWD_VS_REV);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(FWD_VS_REV), List.of("forward vs reverse text"),
            List.of(Map.of("catalog_doc_id", D6)));

        // D8 (LIVE forward owner, with a manifest row in A for a DIFFERENT chash) vs D9
        // (live note-shaped, a COINCIDENTAL reverse match on FWD_LIVE_VS_REV's chash) --
        // forward (D8, live) wins: a live forward owner is never displaced by a reverse
        // match. See forwardResolution_winsWhenBothOwnersAreLive.
        registerDoc(D8, COLLECTION_A);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(FWD_LIVE_ANCHOR), List.of("forward live anchor text"),
            List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, D8, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", FWD_LIVE_ANCHOR, "chunk_index", 0)));
        registerNoteDoc(D9, COLLECTION_A, FWD_LIVE_VS_REV);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(FWD_LIVE_VS_REV), List.of("forward live vs reverse text"),
            List.of(Map.of("catalog_doc_id", D8)));

        // no-owner: no catalog_doc_id/doc_id at all, and a catalog_doc_id naming a tumbler
        // that was never registered. Neither has a reverse match either.
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(NO_OWNER_EMPTY), List.of("no owner empty text"),
            List.of(Map.of()));
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(NO_OWNER_GHOST), List.of("no owner ghost text"),
            List.of(Map.of("catalog_doc_id", "wbfpw4-doc-never-registered")));

        // Round-3 fix: D_TIE_SOME and D_TIE_ZERO both reverse-match TIE_TARGET's
        // chash. D_TIE_SOME's tumbler sorts FIRST alphabetically but has a manifest
        // row in COLLECTION_B (total_count=1, would be dead-owner if picked alone);
        // D_TIE_ZERO's tumbler sorts AFTER D_TIE_SOME's but has zero manifest rows
        // anywhere (would be legacy-unmanifested if picked alone). The tie-break
        // must prefer D_TIE_ZERO (fewest total manifest rows = most conservative
        // bucket) despite its higher tumbler.
        registerNoteDoc(D_TIE_SOME, COLLECTION_A, TIE_TARGET);
        vecRepo.upsertChunks(TENANT, COLLECTION_B, List.of(TIE_SOME_MANIFEST), List.of("tie some manifest text"),
            List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, D_TIE_SOME, COLLECTION_B,
            List.of(Map.<String, Object>of("position", 0, "chash", TIE_SOME_MANIFEST, "chunk_index", 0)));
        registerNoteDoc(D_TIE_ZERO, COLLECTION_A, TIE_TARGET);
        vecRepo.upsertChunks(TENANT, COLLECTION_A, List.of(TIE_TARGET), List.of("tie target text"),
            List.of(Map.of()));
    }

    // ── assertions ───────────────────────────────────────────────────────────

    @Test
    void classifiesEveryManifestLessChunkIntoExactlyOneBucket() {
        var result = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 300, 0);

        assertThat(result.returned()).isEqualTo(TOTAL_MANIFEST_LESS_IN_A);
        assertThat(result.chashes().get("superseded")).containsExactlyInAnyOrder(OLD, FWD_LIVE_VS_REV);
        assertThat(result.chashes().get("legacy-unmanifested"))
            .containsExactlyInAnyOrder(LEGACY, LEGACY_FALLBACK, REV_ONLY, FWD_VS_REV, TIE_TARGET);
        assertThat(result.chashes().get("dead-owner"))
            .containsExactlyInAnyOrder(TOMBSTONED, RENAME_COPY);
        assertThat(result.chashes().get("no-owner"))
            .containsExactlyInAnyOrder(NO_OWNER_EMPTY, NO_OWNER_GHOST);
        assertThat(result.chashes().get("unclassified")).isEmpty();

        assertThat(result.totals().get("superseded")).isEqualTo(2L);
        assertThat(result.totals().get("legacy-unmanifested")).isEqualTo(5L);
        assertThat(result.totals().get("dead-owner")).isEqualTo(2L);
        assertThat(result.totals().get("no-owner")).isEqualTo(2L);
        assertThat(result.totals().get("unclassified")).isEqualTo(0L);
        assertThat(result.scopeChunkTotal()).isEqualTo(TOTAL_CHUNKS_IN_A);

        // CURRENT carries its own-collection manifest row -- excluded from the
        // census population entirely, never labeled into any bucket.
        Set<String> allReturned = new HashSet<>();
        result.chashes().values().forEach(allReturned::addAll);
        assertThat(allReturned).doesNotContain(CURRENT);
        assertThat(allReturned).hasSize(TOTAL_MANIFEST_LESS_IN_A);
    }

    /**
     * Round-1 fix (critic Critical): a chunk with NO forward {@code catalog_doc_id}/
     * {@code doc_id} key at all must still resolve via the REVERSE notes-guard path
     * -- REV_ONLY carries empty chunk metadata, and is found only because live
     * note-shaped doc D5's own {@code metadata.doc_id} names REV_ONLY's chash.
     * Asserted again here, isolated from the bucket-membership test above, so a
     * revert of just the reverse-path join shows exactly this test red (see the
     * task's red/green/red requirement).
     */
    @Test
    void reverseNotesGuardMatch_withNoForwardKey_classifiesAsLegacyUnmanifested() {
        var result = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 300, 0);
        assertThat(result.chashes().get("legacy-unmanifested")).contains(REV_ONLY);
    }

    /**
     * Round-2 fix (critic + code-review Critical): round 1 shipped "forward wins
     * whenever it resolves, live or dead," which misclassified this EXACT fixture
     * as {@code dead-owner}. The correct rule is "a LIVE owner by either path
     * beats a dead or absent one" — a forward pointer to a TOMBSTONED document is
     * stale historical metadata, not evidence of true current ownership, and must
     * not silently outrank a live reverse match that protects the chunk in
     * PRODUCTION today ({@code CatalogRepository.sweepChunksQuery}'s "nl3fn NOTES
     * GUARD" never consults the forward pointer at all).
     *
     * <p>FWD_VS_REV's forward key names TOMBSTONED doc D6; a SEPARATE live
     * note-shaped doc D7 carries {@code metadata.doc_id} = FWD_VS_REV's chash and
     * has zero manifest rows anywhere. D7 (live) RESCUES the classification:
     * {@code legacy-unmanifested}, not {@code dead-owner} — a chunk misrouted to
     * dead-owner is invisible to Step 3's legacy-unmanifested-only backfill gate
     * and becomes unprotected reaper bait once Step 11 removes the notes-guard.
     */
    @Test
    void forwardResolution_isRescuedByALiveReverseMatchWhenForwardOwnerIsDead() {
        var result = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 300, 0);
        assertThat(result.chashes().get("legacy-unmanifested")).contains(FWD_VS_REV);
        assertThat(result.chashes().get("dead-owner")).doesNotContain(FWD_VS_REV);
    }

    /**
     * Round-2 fix companion: a live forward owner is NEVER displaced by a
     * coincidental live reverse match — the rescue above only fires when the
     * forward-resolved owner is itself dead or absent. FWD_LIVE_VS_REV's forward
     * key names LIVE doc D8 (which has a manifest row in collection A for a
     * DIFFERENT chash, so this chash's own_count &gt; 0 -&gt; superseded); a
     * SEPARATE live note D9 ALSO carries {@code metadata.doc_id} =
     * FWD_LIVE_VS_REV's chash (-&gt; would be legacy-unmanifested if reverse were
     * preferred whenever it exists). Asserting superseded here proves the live
     * forward owner still wins.
     */
    @Test
    void forwardResolution_winsWhenBothOwnersAreLive() {
        var result = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 300, 0);
        assertThat(result.chashes().get("superseded")).contains(FWD_LIVE_VS_REV);
        assertThat(result.chashes().get("legacy-unmanifested")).doesNotContain(FWD_LIVE_VS_REV);
    }

    /**
     * Round-3 fix (both reviews, Significant): when multiple live note-shaped
     * documents reverse-match the same chash, the tie-break must be
     * deterministic AND must produce the SAME result whether the caller
     * supplies its parameters via a genuine JDBC bind (the actual route, as
     * {@link PgVectorRepository#manifestLessCensus} issues it) or via literal
     * values spliced directly into {@link PgVectorRepository#MANIFEST_LESS_CENSUS_SQL}'s
     * own text (the hand-run-in-psql shape that SQL's own header instructs an
     * operator to use) -- proving the tie-break is a property of the statement
     * text itself, not of how a caller happens to supply its parameters.
     */
    @Test
    void reverseTieBreak_prefersTheMostConservativeBucket_deterministicallyAcrossLiteralAndBoundForms() {
        var routed = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 300, 0);
        assertThat(routed.chashes().get("legacy-unmanifested")).contains(TIE_TARGET);
        assertThat(routed.chashes().get("dead-owner")).doesNotContain(TIE_TARGET);
        assertThat(routed.chashes().get("superseded")).doesNotContain(TIE_TARGET);

        String literalSql = PgVectorRepository.MANIFEST_LESS_CENSUS_SQL
            .replaceFirst("\\?", "'" + TENANT + "'")
            .replaceFirst("\\?", "'" + COLLECTION_A + "'")
            .replaceFirst("\\?", "'" + TENANT + "'")
            .replaceFirst("\\?", "'" + COLLECTION_A + "'")
            .replaceFirst("\\?", "300")
            .replaceFirst("\\?", "0");
        assertThat(literalSql).doesNotContain("?");

        Set<String> literalLegacyUnmanifested = new HashSet<>();
        tenantScope.withTenant(TENANT, ctx -> {
            ctx.fetch(literalSql).forEach(r -> {
                if ("item".equals(r.get("row_kind", String.class))
                        && "legacy-unmanifested".equals(r.get("bucket", String.class))) {
                    literalLegacyUnmanifested.add(r.get("chash", String.class));
                }
            });
            return null;
        });
        assertThat(literalLegacyUnmanifested)
            .as("the literal-value form of the IDENTICAL statement text must classify TIE_TARGET"
                + " the same way the bound-parameter route does")
            .contains(TIE_TARGET);
    }

    @Test
    void pagesWithoutLosingOrDuplicatingRows() {
        Set<String> paged = new HashSet<>();
        int offset = 0;
        int limit = 3;
        int pages = 0;
        while (true) {
            var page = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, limit, offset);
            page.chashes().values().forEach(paged::addAll);
            pages++;
            if (page.returned() < limit) break;
            offset += limit;
            assertThat(pages).isLessThan(10); // non-vacuity: this loop must terminate
        }

        var full = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 300, 0);
        Set<String> fullSet = new HashSet<>();
        full.chashes().values().forEach(fullSet::addAll);

        assertThat(paged).isEqualTo(fullSet);
        assertThat(paged).hasSize(TOTAL_MANIFEST_LESS_IN_A);
        assertThat(pages).isGreaterThan(1); // proves pagination actually exercised >1 page
    }

    /**
     * Round-1 fix (critic + code-review Significant): {@code totals}/
     * {@code scopeChunkTotal} are computed over the WHOLE collection before
     * LIMIT/OFFSET, so they must be IDENTICAL regardless of which page is
     * requested -- a wrong page-only count is exactly the "clean census" footgun
     * both reviews named.
     */
    @Test
    void totalsAndScopeAreIdenticalAcrossPages() {
        var page1 = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 1, 0);
        var page2 = vecRepo.manifestLessCensus(TENANT, COLLECTION_A, 1, 5);

        assertThat(page1.totals()).isEqualTo(page2.totals());
        assertThat(page1.scopeChunkTotal()).isEqualTo(page2.scopeChunkTotal());
        assertThat(page1.totals().get("superseded")).isEqualTo(2L);
        assertThat(page1.totals().get("legacy-unmanifested")).isEqualTo(5L);
        assertThat(page1.totals().get("dead-owner")).isEqualTo(2L);
        assertThat(page1.totals().get("no-owner")).isEqualTo(2L);
        assertThat(page1.totals().get("unclassified")).isEqualTo(0L);
        assertThat(page1.scopeChunkTotal()).isEqualTo(TOTAL_CHUNKS_IN_A);

        // Each page still returns exactly its own items, not the whole collection.
        assertThat(page1.returned()).isEqualTo(1);
        assertThat(page2.returned()).isEqualTo(1);
    }

    /**
     * Round-1 fix (critic + code-review Significant): a wrong tenant, or an
     * unknown/typo'd collection, must be distinguishable from a genuinely clean
     * census. {@code scope_chunk_total=0} is that signal -- an operator seeing
     * every bucket at 0 AND scope_chunk_total at 0 knows the SCOPE itself is
     * empty, not merely that this collection happens to be fully manifested.
     */
    @Test
    void emptyOrUnknownCollection_returnsZeroScopeAndZeroTotals() {
        var result = vecRepo.manifestLessCensus(TENANT, COLLECTION_UNKNOWN, 100, 0);

        assertThat(result.returned()).isZero();
        assertThat(result.scopeChunkTotal()).isZero();
        for (String bucket : List.of("superseded", "legacy-unmanifested", "dead-owner",
                                      "no-owner", "unclassified")) {
            assertThat(result.totals().get(bucket)).isEqualTo(0L);
            assertThat(result.chashes().get(bucket)).isEmpty();
        }
    }

    /** Minimal {@link Embedder}: content of the vector is irrelevant — only chash/collection/metadata movement is under test. */
    private static final class ConstantEmbedder implements Embedder {
        private final int dim;
        ConstantEmbedder(int dim) { this.dim = dim; }

        @Override
        public List<float[]> embed(List<String> texts) {
            float[] v = new float[dim];
            v[0] = 1.0f;
            return texts.stream().map(t -> v.clone()).toList();
        }

        @Override
        public void close() { }
    }
}
