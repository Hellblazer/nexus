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
 *       caught here rather than assumed away by a shared-helper argument;
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
                            String d2, String d3, String d4, String d5a, String d5b, String d6, String d7, String d8) {
        List<String> allChashes() {
            return List.of(r1, r2, r3, r4, r5, r6, r7, r8);
        }
    }

    /** Maps a row name (R1..R8) to its chash in {@code fx}, the same mapping
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
     * Seeds R1-R8 into {@code tenant}'s own COLLECTION_A/COLLECTION_B (a fresh
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

        return new Fixture(r1, r2, r3, r4, r5, r6, r7, r8, d2, d3, d4, d5a, d5b, d6, d7, d8);
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
            .as("only R3 (own-collection manifest, tombstoned outside the 0-day grace window)"
                + " is a stranded/sweep candidate").isEqualTo(1L);
        assertThat(preview.get("documents_purged")).as("D3 is tombstoned and aged past 0 days")
            .isEqualTo(1L);

        catalogRepo.purgeTrash(tenant, 0);

        assertExistencePredicate(tenant, fx, "P3");
    }

    // ── P4: engine superseded sweep (sweepChunksQuery, via writeManifestMany sweep=true) ──

    @Test
    void p4_engineSweepChunksQuery() throws Exception {
        String tenant = "wbfpw1-p4";
        Fixture fx = seedLivenessFixture(tenant);

        // A throwaway document DX momentarily manifests all 8 target chashes, then
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
                Map.<String, Object>of("position", 7, "chash", fx.r8(), "chunk_index", 7)))),
            COLLECTION_A, null, false);

        var result = catalogRepo.writeManifestMany(tenant, List.of(
            Map.<String, Object>of("doc_id", dx, "rows", List.<Map<String, Object>>of())),
            COLLECTION_A, null, true);

        assertThat(result.get("swept")).as("only R1 has no manifest anywhere and is not a live"
            + " note's own identity chash").isEqualTo(1);
        @SuppressWarnings("unchecked")
        var detail = (List<Map<String, Object>>) result.get("sweep_detail");
        assertThat(detail).singleElement().satisfies(d -> {
            assertThat(d.get("dropped")).isEqualTo(8);
            assertThat(d.get("swept")).isEqualTo(1);
            assertThat(d.get("kept")).isEqualTo(7);
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

    // ── Expected-value table (row x predicate) + non-vacuity assert ────────

    private static final List<String> ROWS =
        List.of("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8");
    private static final List<String> PREDICATES =
        List.of("P1g", "P1s", "P2", "P3", "P4", "P6", "P7", "P9");

    /**
     * TODAY's verdict for every (row, predicate) pair. This table is now the
     * SOURCE the per-predicate {@code @Test} methods assert FROM (via {@link
     * #assertVisibility}/{@link #assertExistencePredicate}), not a
     * human-readable transcription of them, so the table cannot silently
     * drift from the live checks. "true" means: P1g/P1s/P2/P9 visible, P3
     * sweep candidate, P4 swept, P6 deletable, P7 orphaned/moved.
     *
     * <p>P1g and P1s agree on every row: {@code liveChunksCondition}
     * (get/list) and {@code plain_search_<dim>}'s inlined anti-join
     * (search) are collection-scoped, byte-identical-in-effect predicates
     * as of GH #1546 (nexus-ky9ps, vectors-017) — verified here, not
     * assumed; see this class's own javadoc P1s bullet. Had this bead run
     * before that fix landed, R4/R6 could in principle have split the two
     * columns (a chash live in a DIFFERENT collection could mask a
     * same-collection tombstoned manifest row under the OLD unscoped
     * anti-join) — this fixture doesn't manufacture that exact cross-
     * collection-tombstone shape, so it wouldn't have caught that specific
     * historical bug either, but it does now pin that both surfaces read
     * identically going forward.
     */
    private static final Map<String, Map<String, Boolean>> EXPECTED_VALUE_TABLE = Map.ofEntries(
        Map.entry("R1", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", true,  "P6", true,  "P7", true,  "P9", false)),
        Map.entry("R2", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", false, "P7", false, "P9", true)),
        Map.entry("R3", Map.of("P1g", false, "P1s", false, "P2", false, "P3", true,  "P4", false, "P6", false, "P7", false, "P9", true)),
        Map.entry("R4", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", true,  "P7", true,  "P9", false)),
        Map.entry("R5", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", false, "P7", false, "P9", true)),
        Map.entry("R6", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", true,  "P7", true,  "P9", false)),
        Map.entry("R7", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", false, "P7", false, "P9", true)),
        Map.entry("R8", Map.of("P1g", true,  "P1s", true,  "P2", true,  "P3", false, "P4", false, "P6", true,  "P7", true,  "P9", false))
    );

    /**
     * Non-vacuity assert (acceptance criteria): every row of the expected-value
     * table carries exactly the eight predicate columns this bead covers --
     * catches a row silently missing a predicate that a later Step edits.
     */
    @Test
    void expectedValueTable_hasAllEightPredicateColumns_forEveryRow() {
        assertThat(EXPECTED_VALUE_TABLE.keySet())
            .as("R1-R8, no more, no fewer").containsExactlyInAnyOrderElementsOf(ROWS);
        for (String row : ROWS) {
            assertThat(EXPECTED_VALUE_TABLE.get(row).keySet())
                .as("row %s must carry a verdict for every predicate P1g,P1s,P2,P3,P4,P6,P7,P9", row)
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
