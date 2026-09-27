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
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNK_LIVE_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.LIVE_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 1, engine half (bead nexus-wbfpw.1): a fixture matrix pinning
 * TODAY's verdict of every engine liveness predicate named in the RDR's Gap 1
 * survey, minus the two client-side (Python) predicates 5 and 8, which are
 * out of scope for this bead:
 *
 * <ul>
 *   <li><b>P1g</b> — {@code PgVectorRepository.liveChunksCondition}, exercised
 *       through the public {@link PgVectorRepository#get} (get() itself;
 *       {@link PgVectorRepository#list} shares the identical helper
 *       (nexus-msz9i) and is asserted as a second call site of this same
 *       column, Gap 1 item 1).</li>
 *   <li><b>P1s</b> — the SAME Gap-1-item-1 visibility question, but through
 *       the public {@link PgVectorRepository#search}, which does NOT call
 *       {@code liveChunksCondition} — it dispatches to the schema function
 *       {@code plain_search_<dim>} (vectors-009, collection-scoped by
 *       vectors-017), an independently-maintained SQL anti-join. {@code
 *       ConstantEmbedder} makes every stored and query embedding identical,
 *       so {@code search} with a large enough limit returns exactly the
 *       visible set of the collection, the same shape {@code get}/{@code
 *       list} return. P1g and P1s are kept as separate table columns
 *       precisely so a future divergence between the two anti-joins is
 *       caught here rather than assumed away by a shared-helper argument
 *       (including R9's GH #1546 shape, added below);
 *       today (verified by this bead) the two columns agree on every row —
 *       see {@link #EXPECTED_VALUE_TABLE}.</li>
 *   <li><b>P2</b> — {@code nexus.live_chunks} (Gap 1 item 2), read directly
 *       via the generated {@code LIVE_CHUNKS} typed table.</li>
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
 *       table-valued function and {@code ctx.fetchExists}. {@code
 *       chunk_live_owners} is a SET-RETURNING function (round-2 fix, T2
 *       nexus/review-wbfpw9-code) — the round-1 scalar {@code RETURNS
 *       boolean} form never inlined and measurably cost a real latency
 *       regression once called; see the changeset's own header. Not yet
 *       wired into any production call site (that is Step 5,
 *       nexus-wbfpw.10) — this column exists to pin what the new predicate
 *       itself returns, independent of P1g/P1s/P2/P9's own existing
 *       bodies.</li>
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

        var chashes = tenantScope.withTenant(tenant, ctx ->
            ctx.select(LIVE_CHUNKS.CHASH).from(LIVE_CHUNKS)
               .where(LIVE_CHUNKS.TENANT_ID.eq(tenant))
               .fetch(r -> java.util.HexFormat.of().formatHex(r.value1())));

        assertVisibility(chashes, fx, "P2");
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
        String quarantineCollection = "quarantine-knowledge__wbfpw1-p7b-a__minilm-l6-v2-384__v1";

        var outcome = vecRepo.quarantineOrphansBounded(tenant, COLLECTION_A, quarantineCollection,
            "2026-09-26T00:00:00Z", 100, 100);

        assertThat(outcome.moved()).as("identical orphan predicate as the unbounded form")
            .isEqualTo(4);
        assertThat(outcome.remaining()).isZero();
        assertExistencePredicate(tenant, fx, "P7");
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
        List.of("P1g", "P1s", "P2", "P3", "P4", "P6", "P7", "P9", "LIVE");

    /**
     * TODAY's verdict for every (row, predicate) pair. This table is now the
     * SOURCE the per-predicate {@code @Test} methods assert FROM (via {@link
     * #assertVisibility}/{@link #assertExistencePredicate}/{@link
     * #live_chunkIsLiveFunction}), not a human-readable transcription of them, so
     * the table cannot silently drift from the live checks. "true" means:
     * P1g/P1s/P2/P9/LIVE visible/live, P3 sweep candidate, P4 swept, P6 deletable,
     * P7 orphaned/moved. Every LIVE cell here (and every other cell) is scoped to
     * COLLECTION_A; R6 and R9's own COLLECTION_B verdict is pinned separately by
     * {@link #live_r6AndR9_falseInA_trueInB}.
     *
     * <p>R9 (GH #1546 shape, nexus-wbfpw.1 round-2 critique) is the row that
     * exercises the compound state no other row can: an own-collection A manifest
     * row pointing ONLY at a tombstoned document, AND a separate LIVE manifest row
     * for the SAME chash in collection B. P1g ({@code liveChunksCondition}) and P1s
     * ({@code plain_search_<dim>}'s inlined anti-join) STILL AGREE on R9 -- both
     * hidden -- because both were already collection-scoped by vectors-017
     * (GH #1546, nexus-ky9ps) before this bead. P2 ({@code nexus.live_chunks}) is
     * the one column that DISAGREES: it is Gap 5, not yet collection-scoped, so its
     * tenant-wide {@code EXISTS} sees B's live manifest row and marks R9 visible in
     * A too -- exactly the R9-SHAPED residual Step 5 (nexus-wbfpw.10) closes by
     * collection-scoping {@code live_chunks} via live(c). LIVE agrees with P1g/P1s
     * on R9 specifically (all three hidden in A) -- this one row does not
     * distinguish the NEW predicate from the two EXISTING, already-fixed ones on
     * THIS shape; it distinguishes both of them from the one predicate (P2) that
     * is not fixed on this shape yet.
     *
     * <p>This is narrower than "P1g/P1s need no further Step 5 work" -- do not
     * over-read it that way. R1 (the base manifest-less case, Gap 1's core
     * defect) already shows P1g=true, P1s=true, LIVE=false: a chunk with NO
     * manifest row anywhere is still VISIBLE under today's P1g/P1s (their
     * dead-set anti-joins require an owning tombstoned row to hide a chunk; a
     * manifest-less chunk has none, so neither predicate ever flags it), while
     * live(c) -- a positive existence check -- correctly reports it not live.
     * The SAME divergence holds for R3, R4, R6-in-A, and R8. Migrating P1g/P1s
     * to live(c) (Step 5) is still a full behavior change on those rows, not a
     * no-op confirmation exercise; R9 only proves the migration is SAFE on the
     * one cross-collection shape that once (pre-vectors-017) split get from
     * search.
     */
    private static final Map<String, Map<String, Boolean>> EXPECTED_VALUE_TABLE = Map.ofEntries(
        Map.entry("R1", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", true,  "P6", true,  "P7", true,  "P9", false, "LIVE", false)),
        Map.entry("R2", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", false, "P7", false, "P9", true,  "LIVE", true)),
        Map.entry("R3", Map.of("P1g", false, "P1s", false, "P2", false, "P3", true,  "P4", false, "P6", false, "P7", false, "P9", true,  "LIVE", false)),
        Map.entry("R4", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", true,  "P7", true,  "P9", false, "LIVE", false)),
        Map.entry("R5", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", false, "P7", false, "P9", true,  "LIVE", true)),
        Map.entry("R6", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", true,  "P7", true,  "P9", false, "LIVE", false)),
        Map.entry("R7", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", false, "P7", false, "P9", true,  "LIVE", true)),
        Map.entry("R8", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", true,  "P7", true,  "P9", false, "LIVE", false)),
        Map.entry("R9", Map.of("P1g", false, "P1s", false, "P2", true,  "P3", true,  "P4", false, "P6", false, "P7", false, "P9", true,  "LIVE", false))
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
                .as("row %s must carry a verdict for every predicate P1g,P1s,P2,P3,P4,P6,P7,P9,LIVE", row)
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
