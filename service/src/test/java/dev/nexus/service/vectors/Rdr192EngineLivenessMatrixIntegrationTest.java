// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashHex;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.SQLDataType;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNK_IS_REAPABLE;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_LIVE_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.LIVE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 1, engine half (bead nexus-wbfpw.1): a fixture matrix pinning
 * TODAY's verdict of every engine liveness predicate named in the RDR's Gap 1
 * survey, minus the two client-side (Python) predicates 5 and 8, which are
 * out of scope for this bead:
 *
 * <ul>
 *   <li><b>P1g</b> — {@code PgVectorRepository.liveChunksCondition}, exercised
 *       through the public {@link PgVectorRepository#get} and
 *       {@link PgVectorRepository#list}, which share the helper.</li>
 *   <li><b>P1s</b> — {@link PgVectorRepository#search}, which dispatches to the
 *       schema function {@code plain_search_<dim>}. {@code ConstantEmbedder}
 *       makes every embedding identical, so a large enough limit returns the
 *       collection's whole visible set.</li>
 *   <li><b>P1h</b> — {@link PgVectorRepository#hybridSearch}, through both gate
 *       branches ({@code text_gated_search_by_chash_<dim>} and
 *       {@code text_gated_search_hnsw_first_<dim>}).</li>
 *   <li><b>P1t</b> — {@link PgVectorRepository#searchTopicScoped}
 *       ({@code search_topic_scoped_<dim>}).</li>
 *   <li><b>P2</b> — {@code nexus.live_chunks}, read for COLLECTION_A via the
 *       generated {@code LIVE_CHUNKS} typed table.</li>
 *   <li><b>P3</b> — {@code purge_trash} Step 1 candidacy /
 *       {@code CatalogRepository.strandedChunkCount} (Gap 1 item 3), read via
 *       the public {@link CatalogRepository#purgeTrashPreview}/{@link
 *       CatalogRepository#purgeTrash}.</li>
 *   <li><b>P4</b> — the engine superseded sweep, {@code sweepChunksQuery}
 *       (Gap 1 item 4), driven through the public {@code sweep=true}
 *       overload of {@link CatalogRepository#writeManifestMany}.</li>
 *   <li><b>P6</b> — {@link PgVectorRepository#delete}'s anti-join (Gap 1 item
 *       6).</li>
 *   <li><b>P7</b> — {@code gc_quarantine_orphans} (hygiene-005-1) and its
 *       bounded variant {@code gc_quarantine_orphans_bounded} (catalog-037-1)
 *       (Gap 1 item 7), driven through {@link PgVectorRepository#quarantineOrphans}/
 *       {@link PgVectorRepository#quarantineOrphansBounded}.</li>
 *   <li><b>P9</b> — {@code taxonomy_unassigned_chashes_384} (Gap 1 item 9),
 *       driven through {@link TaxonomyRepository#unassignedChashes}.</li>
 *   <li><b>LIVE</b> — RDR-192 Step 4's own {@code EXISTS (SELECT 1 FROM
 *       nexus.chunk_live_owners(tenant, collection, chash))} (bead
 *       nexus-wbfpw.9), called via the generated {@code CHUNK_LIVE_OWNERS}
 *       table-valued function and {@code ctx.fetchExists}. Step 5
 *       (nexus-wbfpw.10) wires it into every read-visibility predicate above.</li>
 * </ul>
 *
 * <p>Fixture rows, seeded once per (isolated) tenant by {@link
 * #seedLivenessFixture}, matching the bead's own row definitions:
 * <ul>
 *   <li><b>R1</b> — chunk in A, no manifest row anywhere.</li>
 *   <li><b>R2</b> — chunk in A, own-collection manifest row, live owner.</li>
 *   <li><b>R3</b> — chunk in A, own-collection manifest row(s), owner
 *       tombstoned only.</li>
 *   <li><b>R4</b> — chunk in A, its only manifest row is in collection B
 *       (a live owner) — the {@code fk_catalog_chunks_chunk} FK requires a
 *       physical row in B too, so this chash is seeded in both A and B.</li>
 *   <li><b>R5</b> — chunk in A shared by two live documents (D5A, D5B); D5B
 *       is re-manifested to drop it, leaving D5A's own-collection manifest
 *       row as the sole survivor.</li>
 *   <li><b>R6</b> — one chash present physically in both A and B, with a
 *       live manifest row in B only (structurally identical to R4's fixture
 *       shape; kept as its own row because the bead names it independently —
 *       see the report's own note on R4/R6 producing identical verdicts
 *       across every predicate in this bead's scope).</li>
 *   <li><b>R7</b> — a current note (post-nexus-b6enc {@code store_put}
 *       shape): a live, note-shaped document with its own manifest row.</li>
 *   <li><b>R8</b> — a legacy current note (the pre-nexus-b6enc shape): a
 *       live, note-shaped document whose OWN {@code metadata.doc_id} names
 *       this chunk's chash, with NO manifest row anywhere.</li>
 *   <li><b>R9</b> — the historical GH #1546 shape (nexus-ky9ps), added per
 *       the nexus-wbfpw.1 round-2 critique (T2 nexus/critique-wbfpw1-r2):
 *       chunk in A, own-collection A manifest row points ONLY at a
 *       tombstoned document (D9A, identical shape to R3's own-collection
 *       row) — AND a separate, LIVE manifest row for the SAME chash exists
 *       in collection B (D9B), the exact compound state neither R3 (no
 *       cross-collection row at all) nor R4/R6 (no own-collection row at
 *       all) can produce. Before vectors-017, an identical chash's live
 *       manifest row in ANY collection masked a tombstoned own-collection
 *       row; this row exists to pin that every predicate under test here
 *       stays collection-scoped on exactly that shape.</li>
 * </ul>
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, one container, one
 * NOSUPERUSER NOBYPASSRLS service role (measuring every predicate through
 * the RLS-subject pool a superuser connection would silently bypass), many
 * isolated tenants — one per destructive predicate (P3/P4/P6/P7) so that one
 * predicate's DELETE/quarantine/purge never corrupts another's fixture, plus
 * one shared read-only tenant for the four non-mutating predicates
 * (P1g/P1s/P2/P9). <strong>One sanctioned exception:</strong> {@link
 * #seedCentroid384} (P9's fixture support only, never the predicate under
 * test) writes the taxonomy centroid row via a superuser JDBC connection —
 * no public writer exists for a bare centroid row outside the real
 * assignment pipeline; registered in {@code RawSqlGateTest}'s ratchet
 * (count=1) and commented SANCTIONED RAW at its own call site.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr192EngineLivenessMatrixIntegrationTest {

    private static final String SVC_ROLE = "svc_wbfpw1_liveness";
    private static final String SVC_PASS = "svc_wbfpw1_liveness_pass";

    private static final String COLLECTION_A = "knowledge__wbfpw1-a__minilm-l6-v2-384__v1";
    private static final String COLLECTION_B = "knowledge__wbfpw1-b__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    HikariDataSource svcDs;
    CatalogRepository catalogRepo;
    PgVectorRepository vecRepo;
    TaxonomyRepository taxRepo;

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
        taxRepo = new TaxonomyRepository(tenantScope);
        var embedder = new ConstantEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    /** One fixture instance's chashes and doc tumblers, so each predicate's
     *  isolated tenant can assert against its own copy. */
    private record Fixture(String r1, String r2, String r3, String r4, String r5, String r6, String r7, String r8,
                            String r9,
                            String d2, String d3, String d4, String d5a, String d5b, String d6, String d7, String d8,
                            String d9a, String d9b) {
        List<String> allChashes() {
            return List.of(r1, r2, r3, r4, r5, r6, r7, r8, r9);
        }
    }

    /** Maps a row name (R1..R9) to its chash in {@code fx}, the same mapping
     *  {@link #EXPECTED_VALUE_TABLE}'s row keys index into. */
    private static String chashForRow(Fixture fx, String row) {
        return switch (row) {
            case "R1" -> fx.r1();
            case "R2" -> fx.r2();
            case "R3" -> fx.r3();
            case "R4" -> fx.r4();
            case "R5" -> fx.r5();
            case "R6" -> fx.r6();
            case "R7" -> fx.r7();
            case "R8" -> fx.r8();
            case "R9" -> fx.r9();
            default -> throw new IllegalArgumentException("unknown row " + row);
        };
    }

    private void registerCollections(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, COLLECTION_A);
            PgContainerHelper.insertCollection(ctx, tenant, COLLECTION_B);
        }
    }

    private void registerDoc(String tenant, String tumbler, String physicalCollection) {
        catalogRepo.upsertDocument(tenant, Map.of(
            "tumbler", tumbler, "title", "RDR-192 S1 fixture " + tumbler,
            "content_type", "prose", "corpus", "knowledge",
            "physical_collection", physicalCollection));
    }

    private void registerNoteDoc(String tenant, String tumbler, String physicalCollection, String identityChashHex) {
        catalogRepo.upsertDocument(tenant, Map.of(
            "tumbler", tumbler, "title", "RDR-192 S1 fixture " + tumbler,
            "content_type", "prose", "corpus", "knowledge",
            "physical_collection", physicalCollection,
            "metadata", Map.of("doc_id", identityChashHex)));
    }

    /**
     * Seeds R1-R9 into {@code tenant}'s own COLLECTION_A/COLLECTION_B (a fresh
     * tenant per call keeps every destructive predicate's fixture isolated).
     */
    private Fixture seedLivenessFixture(String tenant) throws Exception {
        registerCollections(tenant);

        String r1 = ch(tenant + "-r1");
        String r2 = ch(tenant + "-r2");
        String r3 = ch(tenant + "-r3");
        String r4 = ch(tenant + "-r4");
        String r5 = ch(tenant + "-r5");
        String r5bNew = ch(tenant + "-r5b-new");
        String r6 = ch(tenant + "-r6");
        String r7 = ch(tenant + "-r7");
        String r8 = ch(tenant + "-r8");
        String r9 = ch(tenant + "-r9");

        // R1: physically in A, no catalog_doc_id/doc_id key, no manifest row anywhere.
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r1), List.of("r1 text"), List.of(Map.of()));

        // R2: own-collection A manifest row, live owner.
        String d2 = "wbfpw1-d2";
        registerDoc(tenant, d2, COLLECTION_A);
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r2), List.of("r2 text"), List.of(Map.of()));
        catalogRepo.writeManifest(tenant, d2, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", r2, "chunk_index", 0)));

        // R3: own-collection A manifest row, owner tombstoned only.
        String d3 = "wbfpw1-d3";
        registerDoc(tenant, d3, COLLECTION_A);
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r3), List.of("r3 text"), List.of(Map.of()));
        catalogRepo.writeManifest(tenant, d3, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", r3, "chunk_index", 0)));
        catalogRepo.deleteDocument(tenant, d3);

        // R4: physically in A AND B (fk_catalog_chunks_chunk requires a physical row
        // wherever a manifest row names it); its ONLY manifest row is in B, live owner.
        String d4 = "wbfpw1-d4";
        registerDoc(tenant, d4, COLLECTION_B);
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r4), List.of("r4 text a"), List.of(Map.of()));
        vecRepo.upsertChunks(tenant, COLLECTION_B, List.of(r4), List.of("r4 text b"), List.of(Map.of()));
        catalogRepo.writeManifest(tenant, d4, COLLECTION_B,
            List.of(Map.<String, Object>of("position", 0, "chash", r4, "chunk_index", 0)));

        // R5: shared by two live documents (D5A, D5B); D5B is re-manifested WITHOUT it,
        // leaving D5A's own-collection manifest row as the sole survivor.
        String d5a = "wbfpw1-d5a";
        String d5b = "wbfpw1-d5b";
        registerDoc(tenant, d5a, COLLECTION_A);
        registerDoc(tenant, d5b, COLLECTION_A);
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r5), List.of("r5 text"), List.of(Map.of()));
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r5bNew), List.of("r5b new text"), List.of(Map.of()));
        catalogRepo.writeManifest(tenant, d5a, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", r5, "chunk_index", 0)));
        catalogRepo.writeManifest(tenant, d5b, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", r5, "chunk_index", 0)));
        // Re-manifest D5B without r5 (replace with an unrelated new chash) -- D5A's own
        // manifest row for r5 is untouched.
        catalogRepo.writeManifest(tenant, d5b, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", r5bNew, "chunk_index", 0)));

        // R6: one chash present physically in both A and B, live manifest row in B only.
        String d6 = "wbfpw1-d6";
        registerDoc(tenant, d6, COLLECTION_B);
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r6), List.of("r6 text a"), List.of(Map.of()));
        vecRepo.upsertChunks(tenant, COLLECTION_B, List.of(r6), List.of("r6 text b"), List.of(Map.of()));
        catalogRepo.writeManifest(tenant, d6, COLLECTION_B,
            List.of(Map.<String, Object>of("position", 0, "chash", r6, "chunk_index", 0)));

        // R7: current note (post-nexus-b6enc store_put shape) -- own-collection
        // manifest row, live owner, note-shaped document (no file_path).
        String d7 = "wbfpw1-d7note";
        registerDoc(tenant, d7, COLLECTION_A);
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r7), List.of("r7 text"), List.of(Map.of()));
        catalogRepo.writeManifest(tenant, d7, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", r7, "chunk_index", 0)));

        // R8: legacy current note (pre-nexus-b6enc shape) -- live, note-shaped document
        // whose own metadata.doc_id names r8's chash, with NO manifest row at all.
        String d8 = "wbfpw1-d8note";
        registerNoteDoc(tenant, d8, COLLECTION_A, r8);
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r8), List.of("r8 text"), List.of(Map.of()));

        // R9 (GH #1546 shape, nexus-wbfpw.1 round-2 critique T2 nexus/critique-wbfpw1-r2):
        // own-collection A manifest row points ONLY at a tombstoned document (D9A, same
        // shape as R3's own-collection row) -- AND a separate LIVE manifest row for the
        // SAME chash exists in collection B (D9B). Physically present in both A and B
        // (fk_catalog_chunks_chunk requires a physical row wherever a manifest row names
        // it, same requirement as R4/R6).
        String d9a = "wbfpw1-d9a";
        String d9b = "wbfpw1-d9b";
        registerDoc(tenant, d9a, COLLECTION_A);
        registerDoc(tenant, d9b, COLLECTION_B);
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(r9), List.of("r9 text a"), List.of(Map.of()));
        vecRepo.upsertChunks(tenant, COLLECTION_B, List.of(r9), List.of("r9 text b"), List.of(Map.of()));
        catalogRepo.writeManifest(tenant, d9a, COLLECTION_A,
            List.of(Map.<String, Object>of("position", 0, "chash", r9, "chunk_index", 0)));
        catalogRepo.writeManifest(tenant, d9b, COLLECTION_B,
            List.of(Map.<String, Object>of("position", 0, "chash", r9, "chunk_index", 0)));
        catalogRepo.deleteDocument(tenant, d9a);

        return new Fixture(r1, r2, r3, r4, r5, r6, r7, r8, r9, d2, d3, d4, d5a, d5b, d6, d7, d8, d9a, d9b);
    }

    private boolean chunkExistsInCollection(String tenant, String collection, String chashHex) {
        return tenantScope.withTenant(tenant, ctx -> ctx.fetchExists(
            ctx.selectOne().from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(tenant)
                      .and(CHUNKS.COLLECTION.eq(collection))
                      .and(ChashHex.hex(CHUNKS.CHASH).eq(chashHex)))));
    }

    // ── table-driven assertion helpers ───────────────────────────────────────

    private static boolean expected(String row, String predicate) {
        Boolean v = EXPECTED_VALUE_TABLE.get(row).get(predicate);
        if (v == null) {
            throw new IllegalArgumentException("no EXPECTED_VALUE_TABLE cell for " + row + "/" + predicate);
        }
        return v;
    }

    /**
     * Asserts a visibility-style predicate (P1g/P1s/P2/P9: "true" means the
     * row's chash is present in {@code haystack}) against every row's
     * {@link #EXPECTED_VALUE_TABLE} cell for {@code predicate}.
     */
    private void assertVisibility(List<String> haystack, Fixture fx, String predicate) {
        for (String row : ROWS) {
            String chash = chashForRow(fx, row);
            boolean visible = expected(row, predicate);
            var assertion = assertThat(haystack)
                .as("%s / %s: expected %s", row, predicate, visible ? "visible" : "hidden");
            if (visible) {
                assertion.contains(chash);
            } else {
                assertion.doesNotContain(chash);
            }
        }
    }

    /**
     * Asserts an existence-style predicate (P3/P4/P6/P7: "true" means the
     * row's chash was swept/deleted/quarantined out of {@code COLLECTION_A},
     * i.e. it no longer physically exists there) against every row's {@link
     * #EXPECTED_VALUE_TABLE} cell for {@code predicate}.
     */
    private void assertExistencePredicate(String tenant, Fixture fx, String predicate) {
        for (String row : ROWS) {
            String chash = chashForRow(fx, row);
            boolean actedOn = expected(row, predicate);
            boolean stillExists = chunkExistsInCollection(tenant, COLLECTION_A, chash);
            assertThat(stillExists)
                .as("%s / %s: expected %s", row, predicate, actedOn ? "removed" : "retained")
                .isEqualTo(!actedOn);
        }
    }

    // ── P1g: get()/list() visibility (PgVectorRepository.liveChunksCondition) ──

    @Test
    void p1g_getVisibility() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);

        Map<String, Object> envelope = vecRepo.get(tenant, COLLECTION_A, fx.allChashes(), 300, 0);
        @SuppressWarnings("unchecked")
        List<String> gotIds = (List<String>) envelope.get("ids");
        assertVisibility(gotIds, fx, "P1g");

        // list() shares the identical liveChunksCondition helper with get() (nexus-msz9i)
        // -- same predicate/column, a second call site rather than an independent check.
        var listing = vecRepo.list(tenant, COLLECTION_A, 300, 0);
        @SuppressWarnings("unchecked")
        List<String> listedIds = (List<String>) listing.get("ids");
        assertVisibility(listedIds, fx, "P1g");
    }

    // ── P1s: search() visibility (plain_search_<dim>'s own SQL anti-join) ────

    @Test
    void p1s_searchVisibility() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);

        // ConstantEmbedder: query and stored embeddings are all identical, so a
        // limit >= the collection's chunk count returns its whole visible set.
        List<Map<String, Object>> rows = vecRepo.search(tenant, "rdr-192 liveness probe",
            List.of(COLLECTION_A), 300, null);
        List<String> visible = rows.stream().map(r -> (String) r.get("id")).toList();
        assertVisibility(visible, fx, "P1s");
    }

    // ── P2: nexus.live_chunks (tenant-wide, Gap 5) ───────────────────────────

    @Test
    void p2_liveChunksView() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);

        assertVisibility(liveChunksIn(tenant, COLLECTION_A), fx, "P2");
    }

    private List<String> liveChunksIn(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(LIVE_CHUNKS.CHASH).from(LIVE_CHUNKS)
               .where(LIVE_CHUNKS.TENANT_ID.eq(tenant).and(LIVE_CHUNKS.COLLECTION.eq(collection)))
               .fetch(r -> java.util.HexFormat.of().formatHex(r.value1())));
    }

    /**
     * RDR-192 Step 5 Test Plan, last scenario (nexus-wbfpw.10): {@code live_chunks}
     * agrees with live(c) per collection. R6 and R9 are physically in both A and B
     * with a live owner only in B, so each is a {@code live_chunks} row in B and not
     * in A. Before Step 5 the view's liveness check was tenant-wide (Gap 5), so B's
     * live manifest row made the A row visible too.
     */
    @Test
    void p2_liveChunksView_isCollectionScoped_r6AndR9() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);

        List<String> inA = liveChunksIn(tenant, COLLECTION_A);
        List<String> inB = liveChunksIn(tenant, COLLECTION_B);
        assertThat(inA).as("R6 / live_chunks in A").doesNotContain(fx.r6());
        assertThat(inB).as("R6 / live_chunks in B").contains(fx.r6());
        assertThat(inA).as("R9 / live_chunks in A").doesNotContain(fx.r9());
        assertThat(inB).as("R9 / live_chunks in B").contains(fx.r9());
    }

    // ── presentRows: physical presence, deliberately NOT live(c) ────────────

    /**
     * RDR-192 Step 5 amendment (nexus-wbfpw.10, Sam 2026-09-27: split inventory
     * from liveness): {@link PgVectorRepository#presentRows} answers "which of these
     * chashes are physically stored in this collection", ignoring ownership, for
     * callers whose question is existence, not visibility (existing_ids: catalog
     * verify, migration ETL, skip-existing). Every
     * fixture row is physically in A, so all nine come back, including the six that
     * live(c) hides; a chash stored only in B does not.
     */
    @Test
    void presentRows_reportsPhysicalPresence_regardlessOfLiveness() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);
        String onlyInB = ch(tenant + "-only-in-b");
        vecRepo.upsertChunks(tenant, COLLECTION_B, List.of(onlyInB), List.of("only in b"), List.of(Map.of()));

        List<String> asked = new java.util.ArrayList<>(fx.allChashes());
        asked.add(onlyInB);
        @SuppressWarnings("unchecked")
        List<String> present = (List<String>) vecRepo.presentRows(tenant, COLLECTION_A, asked).get("ids");

        assertThat(present).as("every fixture chash is physically in A, live or not")
            .containsExactlyInAnyOrderElementsOf(fx.allChashes());
        assertThat(present).as("a chash stored only in B is not present in A").doesNotContain(onlyInB);
        assertThat((List<?>) vecRepo.presentRows(tenant, COLLECTION_A, List.of()).get("ids")).isEmpty();
    }

    // ── fetchChunkText: the chroma:// permalink is a physical read ───────────

    /**
     * RDR-192 Phase 2 gate M7 (nexus-wbfpw.35): {@link PgVectorRepository#fetchChunkText}
     * backs the {@code chroma://<collection>/<chash>} resolver and reads by (collection, chash)
     * with no liveness filter, while search, get and list are live(c). That is the contract for
     * a content-addressed permalink, so it is pinned rather than left implicit: the two rows live(c)
     * hides in this collection (R1 unowned, R3 tombstoned-owner only) still resolve, and so does a live
     * one.
     */
    @Test
    void fetchChunkText_readsPhysically_regardlessOfLiveness() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);

        assertThat(vecRepo.fetchChunkText(tenant, COLLECTION_A, fx.r2())).as("R2, live").isEqualTo("r2 text");
        assertThat(vecRepo.fetchChunkText(tenant, COLLECTION_A, fx.r1())).as("R1, unowned, hidden by live(c)")
            .isEqualTo("r1 text");
        assertThat(vecRepo.fetchChunkText(tenant, COLLECTION_A, fx.r3()))
            .as("R3, owner tombstoned, hidden by live(c)").isEqualTo("r3 text");
        assertThat(vecRepo.fetchChunkText(tenant, COLLECTION_B, fx.r1()))
            .as("a chash stored only in A does not resolve in B").isNull();
    }

    // ── P1h: hybridSearch() visibility (text_gated_search_*_<dim>) ───────────

    @Test
    void p1h_hybridSearchVisibility() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);

        // Every fixture chunk's text contains "text", so the lexical gate admits all
        // of them; ConstantEmbedder makes the dense ranking a tie, and the limit is
        // above the collection's size. Both gate branches are exercised: the
        // selective (text-first) branch with the default cutoff, and the HNSW-first
        // branch forced by a cutoff of 1.
        for (int selectiveGateMax : new int[] {PgVectorRepository.SELECTIVE_GATE_MAX, 1}) {
            List<Map<String, Object>> rows = vecRepo.hybridSearch(tenant, "text",
                List.of(COLLECTION_A), 300, null, selectiveGateMax);
            List<String> visible = rows.stream().map(r -> (String) r.get("id")).toList();
            assertVisibility(visible, fx, "P1h");
        }
    }

    // ── P1p: text_gate_probe_384 (the hybrid dispatch's gate count) ──────────

    /**
     * nexus-wbfpw.35 (Phase 2 gate M2): the probe is the gate the hybrid dispatch counts to
     * choose between the exact-rank and HNSW-first plans, and it kept the tenant-wide dead-set
     * anti-join after vectors-019 moved every other read path onto live(c). A chunk live(c)
     * hides was still counted, so a gate selective among live rows could read as dense. Every
     * fixture chunk's text contains "text", so the lexical gate admits all nine and the only
     * thing deciding presence is liveness.
     */
    @Test
    void p1p_textGateProbeVisibility() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);

        List<String> probed = tenantScope.withTenant(tenant, ctx -> {
            org.jooq.Table<?> probe = TEXT_GATE_PROBE_384.call(
                "text", new String[] {COLLECTION_A}, null, null, 300);
            return ctx.selectFrom(probe).fetch(r -> java.util.HexFormat.of().formatHex(r.get(0, byte[].class)));
        });
        assertVisibility(probed, fx, "P1p");
    }

    // ── P1t: searchTopicScoped() visibility (search_topic_scoped_<dim>) ──────

    @Test
    void p1t_topicScopedSearchVisibility() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);
        long topicId = taxRepo.insertTopic(tenant, "wbfpw10-topic", null, COLLECTION_A, 0,
            "2026-01-01T00:00:00Z", null);
        for (String chash : fx.allChashes()) {
            taxRepo.assignTopic(tenant, chash, topicId, "wbfpw10", null, COLLECTION_A,
                "2026-01-01T00:00:00Z");
        }

        List<Map<String, Object>> rows = vecRepo.searchTopicScoped(tenant, "rdr-192 liveness probe",
            "wbfpw10-topic", COLLECTION_A, 300);
        List<String> visible = rows.stream().map(r -> (String) r.get("id")).toList();
        assertVisibility(visible, fx, "P1t");
    }

    // ── P9: taxonomy_unassigned_chashes_384 ──────────────────────────────────

    @Test
    void p9_taxonomyUnassignedChashes() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);
        seedCentroid384(tenant, COLLECTION_A, "wbfpw1-topic");

        Map<String, Object> out = taxRepo.unassignedChashes(tenant, COLLECTION_A, 300, null);
        assertThat(out.get("has_taxonomy")).isEqualTo(true);
        @SuppressWarnings("unchecked")
        List<String> unassigned = (List<String>) out.get("chashes");

        assertVisibility(unassigned, fx, "P9");
    }

    private void seedCentroid384(String tenant, String collection, String label) throws Exception {
        long topicId = taxRepo.insertTopic(tenant, label, null, collection, 0,
            "2026-01-01T00:00:00Z", null);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            float[] v = new float[384];
            v[0] = 1.0f;
            // SANCTIONED RAW (nexus-wbfpw.1, TEST-TREE RATCHET): mirrors the identical
            // seed helper TaxonomyUnassignedChashesRepositoryTest#seedCentroid already
            // uses -- no public writer exists for a bare centroid row outside the real
            // assignment pipeline, and jOOQ's generated pgvector Binding is not reachable
            // from this seam without hand-rolling the same literal this precedent uses.
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.taxonomy_centroids"
                    + " (tenant_id, collection, topic_id, label, embedding_384) VALUES (?, ?, ?, ?, ?::nexus.vector)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setLong(3, topicId);
                ps.setString(4, "seed-centroid-" + label);
                StringBuilder sb = new StringBuilder(v.length * 4 + 2).append('[');
                for (int i = 0; i < v.length; i++) {
                    if (i > 0) sb.append(',');
                    sb.append(v[i]);
                }
                sb.append(']');
                ps.setString(5, sb.toString());
                ps.executeUpdate();
            }
        }
    }

    // ── P3: purge_trash Step 1 / CatalogRepository.strandedChunkCount ──────

    @Test
    void p3_purgeTrashCandidacy() throws Exception {
        String tenant = "wbfpw1-p3";
        Fixture fx = seedLivenessFixture(tenant);

        Map<String, Object> preview = catalogRepo.purgeTrashPreview(tenant, 0);
        assertThat(preview.get("chunks_384_stranded"))
            .as("R3 and R9 (own-collection manifest, tombstoned outside the 0-day grace"
                + " window) are the stranded/sweep candidates -- R9's own live manifest row"
                + " lives in collection B, which purge_trash's own-collection-scoped stranding"
                + " check (vectors-017-3) never sees").isEqualTo(2L);
        assertThat(preview.get("documents_purged")).as("D3 and D9A are tombstoned and aged past 0 days")
            .isEqualTo(2L);

        catalogRepo.purgeTrash(tenant, 0);

        assertExistencePredicate(tenant, fx, "P3");
    }

    // ── P4: engine superseded sweep (sweepChunksQuery, via writeManifestMany sweep=true) ──

    @Test
    void p4_engineSweepChunksQuery() throws Exception {
        String tenant = "wbfpw1-p4";
        Fixture fx = seedLivenessFixture(tenant);

        // A throwaway document DX momentarily manifests all 9 target chashes, then
        // replaces its manifest with an empty one -- the only public path to make
        // runSweepTransaction/sweepChunksQuery consider a chosen candidate set as
        // "dropped." Each chash's REAL owner (or absence of one), seeded above, is
        // what the union/notes guards actually see once DX's own reference is gone.
        String dx = "wbfpw1-dx";
        registerDoc(tenant, dx, COLLECTION_A);
        catalogRepo.writeManifestMany(tenant, List.of(
            Map.<String, Object>of("doc_id", dx, "rows", List.of(
                Map.<String, Object>of("position", 0, "chash", fx.r1(), "chunk_index", 0),
                Map.<String, Object>of("position", 1, "chash", fx.r2(), "chunk_index", 1),
                Map.<String, Object>of("position", 2, "chash", fx.r3(), "chunk_index", 2),
                Map.<String, Object>of("position", 3, "chash", fx.r4(), "chunk_index", 3),
                Map.<String, Object>of("position", 4, "chash", fx.r5(), "chunk_index", 4),
                Map.<String, Object>of("position", 5, "chash", fx.r6(), "chunk_index", 5),
                Map.<String, Object>of("position", 6, "chash", fx.r7(), "chunk_index", 6),
                Map.<String, Object>of("position", 7, "chash", fx.r8(), "chunk_index", 7),
                Map.<String, Object>of("position", 8, "chash", fx.r9(), "chunk_index", 8)))),
            COLLECTION_A, null, false);

        var result = catalogRepo.writeManifestMany(tenant, List.of(
            Map.<String, Object>of("doc_id", dx, "rows", List.<Map<String, Object>>of())),
            COLLECTION_A, null, true);

        assertThat(result.get("swept")).as("only R1 has no manifest anywhere and is not a live"
            + " note's own identity chash").isEqualTo(1);
        @SuppressWarnings("unchecked")
        var detail = (List<Map<String, Object>>) result.get("sweep_detail");
        assertThat(detail).singleElement().satisfies(d -> {
            assertThat(d.get("dropped")).isEqualTo(9);
            assertThat(d.get("swept")).isEqualTo(1);
            assertThat(d.get("kept")).as("R9 keeps its own D9A (tombstoned) manifest row and its"
                + " cross-collection D9B (live, in B) manifest row -- sweepChunksQuery has no"
                + " tombstone check (RDR-192 migration order item 4: predicate 4 keeps its"
                + " current shape), so ANY remaining manifest row anywhere keeps it")
                .isEqualTo(8);
        });

        assertExistencePredicate(tenant, fx, "P4");
    }

    // ── P6: PgVectorRepository.delete's anti-join ───────────────────────────

    @Test
    void p6_deleteAntiJoin() throws Exception {
        String tenant = "wbfpw1-p6";
        Fixture fx = seedLivenessFixture(tenant);

        int deleted = vecRepo.delete(tenant, COLLECTION_A, fx.allChashes());

        assertThat(deleted).as("R1, R4, R6, R8 have no OWN-COLLECTION manifest row -- deletable;"
            + " R8's absence of a notes guard here is the Gap-1 divergence from P4").isEqualTo(4);
        assertExistencePredicate(tenant, fx, "P6");
    }

    // ── P7: gc_quarantine_orphans (unbounded) ───────────────────────────────

    @Test
    void p7_gcQuarantineOrphans_unbounded() throws Exception {
        String tenant = "wbfpw1-p7u";
        Fixture fx = seedLivenessFixture(tenant);
        ageTenantChunks(tenant);
        String quarantineCollection = "quarantine-knowledge__wbfpw1-p7u-a__minilm-l6-v2-384__v1";

        var outcome = vecRepo.quarantineOrphans(tenant, COLLECTION_A, quarantineCollection,
            "2026-09-26T00:00:00Z", 100);

        assertThat(outcome.moved()).as("R1, R4, R6, R8: no own-collection manifest row, in any"
            + " owner state -- orphaned; P7 has NO tombstone check and NO notes guard")
            .isEqualTo(4);
        assertExistencePredicate(tenant, fx, "P7");
    }

    // ── P7: gc_quarantine_orphans_bounded ────────────────────────────────────

    @Test
    void p7_gcQuarantineOrphans_bounded() throws Exception {
        String tenant = "wbfpw1-p7b";
        Fixture fx = seedLivenessFixture(tenant);
        ageTenantChunks(tenant);
        String quarantineCollection = "quarantine-knowledge__wbfpw1-p7b-a__minilm-l6-v2-384__v1";

        var outcome = vecRepo.quarantineOrphansBounded(tenant, COLLECTION_A, quarantineCollection,
            "2026-09-26T00:00:00Z", 100, 100);

        assertThat(outcome.moved()).as("identical orphan predicate as the unbounded form")
            .isEqualTo(4);
        assertThat(outcome.remaining()).isZero();
        assertExistencePredicate(tenant, fx, "P7");
    }

    /**
     * RDR-192 Step 8 (nexus-wbfpw.16): P7 selects with reapable(c), so it honours the
     * grace window. Every row of a freshly seeded fixture is younger than 30 days, so
     * not one of the four orphans (R1, R4, R6, R8) moves; before Step 8 it moved all four.
     */
    @Test
    void p7_freshOrphans_areNotQuarantined_graceWindowApplies_unbounded() throws Exception {
        String tenant = "wbfpw1-p7uf";
        Fixture fx = seedLivenessFixture(tenant);
        String quarantineCollection = "quarantine-knowledge__wbfpw1-p7uf-a__minilm-l6-v2-384__v1";

        var outcome = vecRepo.quarantineOrphans(tenant, COLLECTION_A, quarantineCollection,
            "2026-09-26T00:00:00Z", 100);

        assertThat(outcome.moved()).as("nothing has aged past the grace window").isZero();
        for (String row : ROWS) {
            assertThat(chunkExistsInCollection(tenant, COLLECTION_A, chashForRow(fx, row)))
                .as("%s stays in A: fresh", row).isTrue();
        }
    }

    @Test
    void p7_freshOrphans_areNotQuarantined_graceWindowApplies_bounded() throws Exception {
        String tenant = "wbfpw1-p7bf";
        Fixture fx = seedLivenessFixture(tenant);
        String quarantineCollection = "quarantine-knowledge__wbfpw1-p7bf-a__minilm-l6-v2-384__v1";

        var outcome = vecRepo.quarantineOrphansBounded(tenant, COLLECTION_A, quarantineCollection,
            "2026-09-26T00:00:00Z", 100, 100);

        assertThat(outcome.moved()).isZero();
        assertThat(outcome.remaining()).as("the bounded form's remaining count is reapable(c) too").isZero();
        for (String row : ROWS) {
            assertThat(chunkExistsInCollection(tenant, COLLECTION_A, chashForRow(fx, row)))
                .as("%s stays in A: fresh", row).isTrue();
        }
    }

    /** A mixed population: one aged orphan moves, a fresh orphan of the identical shape does not. */
    @Test
    void p7_agedOrphanMoves_whileAFreshOrphanOfTheSameShapeStays() throws Exception {
        String tenant = "wbfpw1-p7m";
        Fixture fx = seedLivenessFixture(tenant);
        ageTenantChunks(tenant);
        String freshR1 = ch(tenant + "-r1-fresh");
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(freshR1), List.of("r1 fresh text"), List.of(Map.of()));
        String quarantineCollection = "quarantine-knowledge__wbfpw1-p7m-a__minilm-l6-v2-384__v1";

        var outcome = vecRepo.quarantineOrphans(tenant, COLLECTION_A, quarantineCollection,
            "2026-09-26T00:00:00Z", 100);

        assertThat(outcome.moved()).as("R1, R4, R6, R8 aged; the fresh R1 twin is not").isEqualTo(4);
        assertExistencePredicate(tenant, fx, "P7");
        assertThat(chunkExistsInCollection(tenant, COLLECTION_A, freshR1)).as("fresh R1 stays").isTrue();
    }

    // ── REAP: EXISTS(nexus.chunk_is_reapable(...)) (RDR-192 Step 7, bead nexus-wbfpw.15) ──

    /** Pushes every chunk of {@code tenant} 40 days into the past (past the 30 day default grace). */
    private void ageTenantChunks(String tenant) throws Exception {
        OffsetDateTime then = OffsetDateTime.now().minus(Duration.ofDays(40));
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS)
               .set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then)
               .where(CHUNKS.TENANT_ID.eq(tenant)).execute();
        }
    }

    /** reapable(c) for {@code chashHex} in {@code collection}, as the RLS-subject service role, default grace. */
    private boolean chunkIsReapable(String tenant, String collection, String chashHex) {
        byte[] chash = Chash.fromHex(chashHex).toBytes();
        return tenantScope.withTenant(tenant, ctx -> ctx.fetchExists(
            ctx.selectOne().from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                      .and(CHUNKS.CHASH.eq(chash)))
               .and(DSL.exists(DSL.selectFrom(CHUNK_IS_REAPABLE.call(
                   CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.LAST_WRITTEN_AT,
                   DSL.val(null, SQLDataType.INTERVAL)))))));
    }

    @Test
    void reap_chunkIsReapableFunction_agedRows() throws Exception {
        String tenant = "wbfpw1-reap";
        Fixture fx = seedLivenessFixture(tenant);
        ageTenantChunks(tenant);

        for (String row : ROWS) {
            String chash = chashForRow(fx, row);
            boolean expected = expected(row, "REAP");
            assertThat(chunkIsReapable(tenant, COLLECTION_A, chash))
                .as("%s / REAP in %s: expected %s", row, COLLECTION_A, expected)
                .isEqualTo(expected);
        }
    }

    /** The same fixture seeded at created-now: every REAP cell is false, whatever the owner state. */
    @Test
    void reap_chunkIsReapableFunction_freshRows_areNeverReapable() throws Exception {
        String tenant = "wbfpw1-reapf";
        Fixture fx = seedLivenessFixture(tenant);

        for (String row : ROWS) {
            assertThat(chunkIsReapable(tenant, COLLECTION_A, chashForRow(fx, row)))
                .as("%s / REAP when created now", row).isFalse();
        }
    }

    /** R1's shape, created now, next to aged R1: the grace window is the only difference. */
    @Test
    void reap_freshR1_isNotReapable_agedR1IsReapable() throws Exception {
        String tenant = "wbfpw1-reapr1";
        Fixture fx = seedLivenessFixture(tenant);
        ageTenantChunks(tenant);
        String freshR1 = ch(tenant + "-r1-fresh");
        vecRepo.upsertChunks(tenant, COLLECTION_A, List.of(freshR1), List.of("r1 fresh text"), List.of(Map.of()));

        assertThat(chunkIsReapable(tenant, COLLECTION_A, fx.r1())).as("aged R1").isTrue();
        assertThat(chunkIsReapable(tenant, COLLECTION_A, freshR1)).as("fresh R1").isFalse();
    }

    /** R4, R6 and R9 are owned in B only (or tombstoned in A): reapable(c) is collection-scoped like live(c). */
    @Test
    void reap_isCollectionScoped_r4R6R9_inB() throws Exception {
        String tenant = "wbfpw1-reapb";
        Fixture fx = seedLivenessFixture(tenant);
        ageTenantChunks(tenant);

        assertThat(chunkIsReapable(tenant, COLLECTION_A, fx.r4())).as("R4 in A").isTrue();
        assertThat(chunkIsReapable(tenant, COLLECTION_B, fx.r4())).as("R4 in B").isFalse();
        assertThat(chunkIsReapable(tenant, COLLECTION_A, fx.r6())).as("R6 in A").isTrue();
        assertThat(chunkIsReapable(tenant, COLLECTION_B, fx.r6())).as("R6 in B").isFalse();
        assertThat(chunkIsReapable(tenant, COLLECTION_A, fx.r9()))
            .as("R9 in A: a manifest row exists, its owner is tombstoned").isFalse();
        assertThat(chunkIsReapable(tenant, COLLECTION_B, fx.r9())).as("R9 in B").isFalse();
    }

    // ── LIVE: EXISTS(nexus.chunk_live_owners(tenant, collection, chash)) (RDR-192 Step 4, bead nexus-wbfpw.9) ──

    /** live(c) is EXISTS(SELECT 1 FROM nexus.chunk_live_owners(...)), not a direct
     *  scalar call -- {@code nexus.chunk_live_owners} is a set-returning function
     *  (RETURNS TABLE), the shape PostgreSQL's inliner actually accepts for a body
     *  needing a join (round-2 fix; a scalar RETURNS-boolean form does not inline,
     *  see the changeset's own header). Inside the tenant's own RLS session
     *  (SECURITY INVOKER + FORCE RLS on catalog_document_chunks/catalog_documents
     *  means the {@code nexus.tenant} GUC {@link TenantScope#withTenant} stamps is
     *  the only reason this predicate sees any manifest/document rows at all). */
    private boolean chunkIsLive(String tenant, String collection, String chashHex) {
        byte[] chash = Chash.fromHex(chashHex).toBytes();
        var fn = CHUNK_LIVE_OWNERS.call(tenant, collection, chash);
        return tenantScope.withTenant(tenant, ctx -> ctx.fetchExists(ctx.selectFrom(fn)));
    }

    @Test
    void live_chunkIsLiveFunction() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);

        for (String row : ROWS) {
            String chash = chashForRow(fx, row);
            boolean expected = expected(row, "LIVE");
            assertThat(chunkIsLive(tenant, COLLECTION_A, chash))
                .as("%s / LIVE in %s: expected %s", row, COLLECTION_A, expected)
                .isEqualTo(expected);
        }
    }

    /**
     * R6 and R9 are the two rows whose own-collection-A verdict and cross-
     * collection-B verdict DIFFER -- {@link #EXPECTED_VALUE_TABLE}'s LIVE column
     * (like every other column in it) is scoped to COLLECTION_A only, so the B-side
     * verdict needs its own assertion here. R9 is the row that matters most: it is
     * the exact historical GH #1546 shape (own-collection A manifest tombstoned-
     * only, a LIVE manifest row for the SAME chash in B) that once let a live
     * manifest row in ANY collection mask a tombstoned own-collection row. live(c)
     * being collection-scoped BY CONSTRUCTION (its own p_collection parameter) is
     * what this test pins.
     */
    @Test
    void live_r6AndR9_falseInA_trueInB() throws Exception {
        String tenant = "wbfpw1-ro";
        Fixture fx = seedLivenessFixture(tenant);

        assertThat(chunkIsLive(tenant, COLLECTION_A, fx.r6())).as("R6 / LIVE in A").isFalse();
        assertThat(chunkIsLive(tenant, COLLECTION_B, fx.r6())).as("R6 / LIVE in B").isTrue();

        assertThat(chunkIsLive(tenant, COLLECTION_A, fx.r9())).as("R9 / LIVE in A").isFalse();
        assertThat(chunkIsLive(tenant, COLLECTION_B, fx.r9())).as("R9 / LIVE in B").isTrue();
    }

    // ── Expected-value table (row x predicate) + non-vacuity assert ────────

    private static final List<String> ROWS =
        List.of("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9");
    private static final List<String> PREDICATES =
        List.of("P1g", "P1s", "P1h", "P1p", "P1t", "P2", "P3", "P4", "P6", "P7", "P9", "LIVE", "REAP");

    /**
     * The verdict for every (row, predicate) pair. This table is the SOURCE the
     * per-predicate {@code @Test} methods assert FROM (via {@link
     * #assertVisibility}/{@link #assertExistencePredicate}/{@link
     * #live_chunkIsLiveFunction}), so it cannot silently drift from the live
     * checks. "true" means: P1g/P1s/P1h/P1p/P1t/P2/P9/LIVE visible/live, P3 sweep
     * candidate, P4 swept, P6 deletable, P7 orphaned/moved. Every cell is scoped
     * to COLLECTION_A; R6 and R9's COLLECTION_B verdict is pinned separately by
     * {@link #live_r6AndR9_falseInA_trueInB} and {@link
     * #p2_liveChunksView_isCollectionScoped_r6AndR9}.
     *
     * <p>RDR-192 Step 5 (nexus-wbfpw.10) moved every read-visibility predicate
     * (P1g get/list, P1s plain search, P1h hybrid search, P1t topic-scoped search,
     * P2 {@code live_chunks}) onto live(c), so those five columns equal LIVE on
     * every row. P1p, the hybrid dispatch's gate probe ({@code text_gate_probe_<dim>}),
     * followed in vectors-023 (nexus-wbfpw.35) and equals LIVE too. Before Step 5 they differed from LIVE on R1, R4, R6 and R8, the
     * rows with no live own-collection owner that the old dead-set anti-join kept
     * visible, and P2 differed on R9 as well because its check was tenant-wide.
     * They always agreed on R3 (tombstoned owner only).
     *
     * <p>P9 ({@code taxonomy_unassigned_chashes}) keeps its own shape (RDR-192
     * Migration order, item 3): it asks only whether an own-collection manifest
     * row exists, with no tombstone check, so it differs from LIVE on R3 and R9.
     * The destructive columns P3, P4 and P6 are Phase 4 work.
     *
     * <p>REAP is reapable(c) (RDR-192 Step 7, nexus-wbfpw.15): no own-collection
     * manifest row in ANY owner state, last_written_at older than the grace window
     * and no in-flight index run naming the chunk's document. The cells assume rows
     * aged past the 30 day default (a row created now is false in every row; the
     * fresh-R1 control pins that). It is not the complement of LIVE: R3 and R9 are
     * neither live nor reapable (a manifest row exists, so the chunk is dead but
     * belongs to purge_trash's tombstone arm), and R1, R4, R6 and R8 are reapable
     * only once aged. On aged rows REAP, P6 and P7 agree cell for cell; P6 (delete's
     * anti-join) keeps no grace window and P7 moved onto REAP in Step 8
     * (nexus-wbfpw.16), so a row created now separates them.
     */
    private static final Map<String, Map<String, Boolean>> EXPECTED_VALUE_TABLE = Map.ofEntries(
        Map.entry("R1", Map.ofEntries(Map.entry("P1g", false), Map.entry("P1s", false), Map.entry("P1h", false), Map.entry("P1p", false), Map.entry("P1t", false), Map.entry("P2", false), Map.entry("P3", false), Map.entry("P4", true), Map.entry("P6", true), Map.entry("P7", true), Map.entry("P9", false), Map.entry("LIVE", false), Map.entry("REAP", true))),
        Map.entry("R2", Map.ofEntries(Map.entry("P1g", true), Map.entry("P1s", true), Map.entry("P1h", true), Map.entry("P1p", true), Map.entry("P1t", true), Map.entry("P2", true), Map.entry("P3", false), Map.entry("P4", false), Map.entry("P6", false), Map.entry("P7", false), Map.entry("P9", true), Map.entry("LIVE", true), Map.entry("REAP", false))),
        Map.entry("R3", Map.ofEntries(Map.entry("P1g", false), Map.entry("P1s", false), Map.entry("P1h", false), Map.entry("P1p", false), Map.entry("P1t", false), Map.entry("P2", false), Map.entry("P3", true), Map.entry("P4", false), Map.entry("P6", false), Map.entry("P7", false), Map.entry("P9", true), Map.entry("LIVE", false), Map.entry("REAP", false))),
        Map.entry("R4", Map.ofEntries(Map.entry("P1g", false), Map.entry("P1s", false), Map.entry("P1h", false), Map.entry("P1p", false), Map.entry("P1t", false), Map.entry("P2", false), Map.entry("P3", false), Map.entry("P4", false), Map.entry("P6", true), Map.entry("P7", true), Map.entry("P9", false), Map.entry("LIVE", false), Map.entry("REAP", true))),
        Map.entry("R5", Map.ofEntries(Map.entry("P1g", true), Map.entry("P1s", true), Map.entry("P1h", true), Map.entry("P1p", true), Map.entry("P1t", true), Map.entry("P2", true), Map.entry("P3", false), Map.entry("P4", false), Map.entry("P6", false), Map.entry("P7", false), Map.entry("P9", true), Map.entry("LIVE", true), Map.entry("REAP", false))),
        Map.entry("R6", Map.ofEntries(Map.entry("P1g", false), Map.entry("P1s", false), Map.entry("P1h", false), Map.entry("P1p", false), Map.entry("P1t", false), Map.entry("P2", false), Map.entry("P3", false), Map.entry("P4", false), Map.entry("P6", true), Map.entry("P7", true), Map.entry("P9", false), Map.entry("LIVE", false), Map.entry("REAP", true))),
        Map.entry("R7", Map.ofEntries(Map.entry("P1g", true), Map.entry("P1s", true), Map.entry("P1h", true), Map.entry("P1p", true), Map.entry("P1t", true), Map.entry("P2", true), Map.entry("P3", false), Map.entry("P4", false), Map.entry("P6", false), Map.entry("P7", false), Map.entry("P9", true), Map.entry("LIVE", true), Map.entry("REAP", false))),
        Map.entry("R8", Map.ofEntries(Map.entry("P1g", false), Map.entry("P1s", false), Map.entry("P1h", false), Map.entry("P1p", false), Map.entry("P1t", false), Map.entry("P2", false), Map.entry("P3", false), Map.entry("P4", false), Map.entry("P6", true), Map.entry("P7", true), Map.entry("P9", false), Map.entry("LIVE", false), Map.entry("REAP", true))),
        Map.entry("R9", Map.ofEntries(Map.entry("P1g", false), Map.entry("P1s", false), Map.entry("P1h", false), Map.entry("P1p", false), Map.entry("P1t", false), Map.entry("P2", false), Map.entry("P3", true), Map.entry("P4", false), Map.entry("P6", false), Map.entry("P7", false), Map.entry("P9", true), Map.entry("LIVE", false), Map.entry("REAP", false)))
    );

    /**
     * Non-vacuity assert (acceptance criteria): every row of the expected-value
     * table carries exactly the nine predicate columns this bead covers --
     * catches a row silently missing a predicate that a later Step edits.
     */
    @Test
    void expectedValueTable_hasAllPredicateColumns_forEveryRow() {
        assertThat(EXPECTED_VALUE_TABLE.keySet())
            .as("R1-R9, no more, no fewer").containsExactlyInAnyOrderElementsOf(ROWS);
        for (String row : ROWS) {
            assertThat(EXPECTED_VALUE_TABLE.get(row).keySet())
                .as("row %s must carry a verdict for every predicate in PREDICATES", row)
                .containsExactlyInAnyOrderElementsOf(PREDICATES);
        }
    }

    /** Minimal {@link Embedder}: content of the vector is irrelevant -- only chash/collection/metadata movement is under test. */
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
