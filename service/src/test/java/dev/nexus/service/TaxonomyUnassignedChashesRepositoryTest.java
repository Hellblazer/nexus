// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_UNASSIGNED_CHASHES_1024;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-iygza (indexing-brittleness P0.1, engine half) — {@code
 * TaxonomyRepository#unassignedChashes} / {@code
 * nexus.taxonomy_unassigned_chashes_<dim>} contract suite
 * (taxonomy-019-unassigned-chashes.xml).
 *
 * <p>Sam's design (bd comment, 2026-09-25): derive from state, never a
 * pending table. "Unassigned" is defined precisely as: a chunk with a
 * non-null embedding at the collection's own dim, in a collection that HAS
 * centroids at that dim, WITH A LIVE CATALOG MANIFEST REFERENCE (rework
 * round: excludes RDR-192's manifest-less/orphan population), carrying NO
 * {@code topic_assignments} row whose topic belongs to THIS collection
 * ({@code topics.collection}). A chunk that already carries a foreign
 * ("projection") cross-collection assignment but no own-collection one is
 * STILL reported — the two passes are independent.
 *
 * <p>Every fixture chunk in this suite goes through {@link #seedChunk}, which
 * ALSO seeds a real {@code catalog_document_chunks} manifest row (one
 * {@code catalog_documents} row per chash, position 0) — without one, the
 * rework round's manifest filter would exclude every chunk this suite seeds,
 * silently emptying every pre-existing test's expectations. {@link
 * #seedChunkNoManifest} is the deliberate exception, used only by {@link
 * #manifestLessChunk_excluded_evenWhenOtherwiseUnassigned}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyUnassignedChashesRepositoryTest {

    private static final String SVC_ROLE = "svc_unassigned_test";
    private static final String SVC_PASS = "svc_unassigned_test_pass";

    private static final String TENANT_A = "unassigned-tenant-a";
    private static final String TENANT_B = "unassigned-tenant-b";

    private static final int DIM = 1024;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TaxonomyRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT EXECUTE ON FUNCTION nexus.taxonomy_unassigned_chashes_" + dim
                    + "(text, int, text) TO " + SVC_ROLE);
                su.createStatement().execute(
                    "GRANT EXECUTE ON FUNCTION nexus.assign_from_chashes_" + dim
                    + "(text, text[], boolean) TO " + SVC_ROLE);
            }
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    @Test
    void noCentroids_hasTaxonomyFalse_chashesEmpty() throws Exception {
        String col = "code__unassigned_nocentroids__voyage-code-3__v1";
        String c1 = hexChash("unassigned-nocentroids-1");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        // Deliberately NO centroids seeded for this collection.

        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 100, null);
        assertThat(out.get("has_taxonomy")).as("no centroids at all for this"
            + " collection's own dim").isEqualTo(false);
        assertThat((List<?>) out.get("chashes")).as("chashes must be empty when"
            + " has_taxonomy is false, never a partial/best-effort list").isEmpty();
        assertThat(out.get("next_after")).as("no page, no cursor").isNull();
    }

    @Test
    void unassignedChunk_listed_assignedChunk_not() throws Exception {
        String col = "code__unassigned_basic__voyage-code-3__v1";
        String cUnassigned = hexChash("unassigned-basic-unassigned");
        String cAssigned   = hexChash("unassigned-basic-assigned");
        seedChunk(TENANT_A, col, cUnassigned, unit(1.0f, 0.0f));
        seedChunk(TENANT_A, col, cAssigned, unit(0.0f, 1.0f));
        seedCentroid(TENANT_A, col, "unassigned-basic-topic", unit(1.0f, 0.0f));

        // Only cAssigned gets an own-collection assignment.
        repo.assignFromChashes(TENANT_A, col, List.of(cAssigned), false);

        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 100, null);
        assertThat(out.get("has_taxonomy")).isEqualTo(true);
        assertThat((List<String>) (List<?>) out.get("chashes"))
            .as("only the chunk with no own-collection topic_assignments row")
            .containsExactly(cUnassigned);
    }

    @Test
    void manifestLessChunk_excluded_evenWhenOtherwiseUnassigned() throws Exception {
        // Rework round (code-review-expert + substantive-critic, Significant):
        // a chunk with NO live catalog manifest reference is never reported,
        // regardless of assignment state (RDR-192's manifest-less population).
        String col = "code__unassigned_manifestless__voyage-code-3__v1";
        String cGhost = hexChash("unassigned-manifestless-ghost");
        String cReal = hexChash("unassigned-manifestless-real");
        seedChunkNoManifest(TENANT_A, col, cGhost, unit(1.0f, 0.0f));
        seedChunk(TENANT_A, col, cReal, unit(0.0f, 1.0f));
        seedCentroid(TENANT_A, col, "unassigned-manifestless-topic", unit(1.0f, 0.0f));

        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 100, null);
        assertThat(out.get("has_taxonomy")).isEqualTo(true);
        assertThat((List<String>) (List<?>) out.get("chashes"))
            .as("cGhost has no live catalog manifest row -- excluded even though it"
                + " is, by assignment state alone, otherwise unassigned")
            .containsExactly(cReal);
    }

    @Test
    void chunkWithOnlyCrossCollectionAssignment_stillReportedUnassigned() throws Exception {
        // A chunk that carries a foreign ("projection") cross-collection
        // assignment but NO own-collection one is still "unassigned" for this
        // route's purpose -- the own and cross passes are independent (bd
        // comment, nexus-iygza). assignFromChashes always runs BOTH passes in
        // one call, so this test seeds the cross ('projection') row directly
        // (raw INSERT, same idiom TaxonomyAssignFromChashesRepositoryTest's
        // crossPass_onConflict_greatestSimilarityWins_bothDirections uses) to
        // isolate the scenario: col carries an own centroid of its own
        // (has_taxonomy=true) that c1 was simply never run against.
        String col = "code__unassigned_crossonly__voyage-code-3__v1";
        String colForeign = "code__unassigned_crossonlyfgn__voyage-code-3__v1";
        String c1 = hexChash("unassigned-crossonly-1");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        seedCentroid(TENANT_A, col, "unassigned-crossonly-topic-own", unit(0.0f, 1.0f));
        long tForeign = seedCentroid(TENANT_A, colForeign,
            "unassigned-crossonly-topic-foreign", unit(1.0f, 0.0f));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.topic_assignments"
                    + " (tenant_id, doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection)"
                    + " VALUES (?, decode(?, 'hex'), ?, 'projection', 1.0, now(), ?)")) {
                ps.setString(1, TENANT_A);
                ps.setString(2, c1);
                ps.setLong(3, tForeign);
                ps.setString(4, col);
                ps.executeUpdate();
            }
        }

        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 100, null);
        assertThat(out.get("has_taxonomy")).as("col has its own centroid").isEqualTo(true);
        assertThat((List<String>) (List<?>) out.get("chashes"))
            .as("c1's ONLY topic_assignments row belongs to a topic in colForeign,"
                + " not col itself -- still reported unassigned")
            .containsExactly(c1);
    }

    @Test
    void limitHonoured() throws Exception {
        String col = "code__unassigned_limit__voyage-code-3__v1";
        seedCentroid(TENANT_A, col, "unassigned-limit-topic", unit(1.0f, 0.0f));
        String[] chashes = new String[5];
        for (int i = 0; i < 5; i++) {
            chashes[i] = hexChash("unassigned-limit-" + i);
            seedChunk(TENANT_A, col, chashes[i], unit(0.0f, 1.0f));
        }

        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 2, null);
        assertThat(out.get("has_taxonomy")).isEqualTo(true);
        assertThat((List<?>) out.get("chashes")).as("limit=2 honoured").hasSize(2);
        assertThat(out.get("next_after")).as("page came back FULL -- more to drain").isNotNull();
    }

    @Test
    void limitOverMax_rejected() {
        assertThatThrownBy(() -> repo.unassignedChashes(TENANT_A,
                "code__unassigned_overlimit__voyage-code-3__v1",
                TaxonomyRepository.MAX_UNASSIGNED_CHASHES + 1, null))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void limitZeroOrNegative_rejected() {
        assertThatThrownBy(() -> repo.unassignedChashes(TENANT_A,
                "code__unassigned_zerolimit__voyage-code-3__v1", 0, null))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void keysetCursor_progressesThroughAllPagesAndTerminates() throws Exception {
        String col = "code__unassigned_cursor__voyage-code-3__v1";
        seedCentroid(TENANT_A, col, "unassigned-cursor-topic", unit(1.0f, 0.0f));
        List<String> expected = new ArrayList<>();
        for (int i = 0; i < 10; i++) {
            String c = hexChash("unassigned-cursor-" + i);
            seedChunk(TENANT_A, col, c, unit(0.0f, 1.0f));
            expected.add(c);
        }
        Collections.sort(expected); // matches ORDER BY chash (hex encoding preserves byte order)

        List<String> collected = new ArrayList<>();
        String after = null;
        int pages = 0;
        while (true) {
            Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 3, after);
            List<String> page = (List<String>) (List<?>) out.get("chashes");
            collected.addAll(page);
            after = (String) out.get("next_after");
            pages++;
            assertThat(pages).as("must terminate within a small, bounded number of pages"
                + " -- a cursor that never advances would loop forever").isLessThan(10);
            if (after == null) {
                break;
            }
        }
        assertThat(collected)
            .as("the keyset drain visits every unassigned chash EXACTLY once, no"
                + " duplicates and no gaps, in ascending chash order")
            .containsExactlyElementsOf(expected);
        assertThat(pages).as("10 chashes at page size 3 -- 4 pages (3+3+3+1)").isEqualTo(4);
    }

    @Test
    void anotherTenant_rowsInvisible() throws Exception {
        String col = "code__unassigned_rls__voyage-code-3__v1";
        String c1 = hexChash("unassigned-rls-1");
        seedChunk(TENANT_A, col, c1, unit(1.0f, 0.0f));
        seedCentroid(TENANT_A, col, "unassigned-rls-topic", unit(1.0f, 0.0f));

        // TENANT_B has never registered this collection at all -- fails loud via
        // CollectionRegistry, the same discipline assignFromChashes uses.
        assertThatThrownBy(() -> repo.unassignedChashes(TENANT_B, col, 100, null))
            .isInstanceOf(dev.nexus.service.db.UnregisteredCollectionException.class);
    }

    @Test
    void anotherTenant_sameCollectionName_seesOnlyItsOwnRows() throws Exception {
        // A DIFFERENT tenant registering the SAME collection NAME must see only
        // its own chunks/centroids (RLS), never TENANT_A's.
        String col = "code__unassigned_rls_shared__voyage-code-3__v1";
        String cA = hexChash("unassigned-rls-shared-a");
        String cB = hexChash("unassigned-rls-shared-b");
        seedChunk(TENANT_A, col, cA, unit(1.0f, 0.0f));
        seedCentroid(TENANT_A, col, "unassigned-rls-shared-topic-a", unit(1.0f, 0.0f));
        seedChunk(TENANT_B, col, cB, unit(0.0f, 1.0f));
        seedCentroid(TENANT_B, col, "unassigned-rls-shared-topic-b", unit(0.0f, 1.0f));

        Map<String, Object> outA = repo.unassignedChashes(TENANT_A, col, 100, null);
        Map<String, Object> outB = repo.unassignedChashes(TENANT_B, col, 100, null);
        assertThat((List<String>) (List<?>) outA.get("chashes")).containsExactly(cA);
        assertThat((List<String>) (List<?>) outB.get("chashes")).containsExactly(cB);
    }

    /**
     * Scale test (rework round, code-review-expert's Significant finding: no
     * plan-shape/scale coverage at all for a query the reviewer named as
     * structurally unable to stop early once gaps are sparse). 50,000 chunks
     * in one collection, ALL manifest-referenced, all-but-5 own-assigned; the
     * 5 scattered gaps (at deterministic positions across the whole
     * keyspace) must still be found correctly, within a generous time bound,
     * and the underlying plan must use an INDEX for the antijoin rather than
     * a sequential scan of {@code nexus.chunks} that would touch every OTHER
     * tenant's/collection's rows too.
     */
    @Test
    void scale_50kChunks_fewUnassignedScattered_correctAndIndexBacked() throws Exception {
        String col = "code__unassigned_scale__voyage-code-3__v1";
        registerCollection(TENANT_A, col);
        long topicId = seedCentroid(TENANT_A, col, "unassigned-scale-topic", unit(1.0f, 0.0f));

        int total = 50_000;
        int[] unassignedIdx = {1000, 15000, 25000, 35000, 49000};
        List<String> expected = new ArrayList<>();
        for (int idx : unassignedIdx) {
            expected.add(chashForIndex(idx));
        }
        Collections.sort(expected);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Bulk chunks: deterministic 64-hex chash per i (lpad(to_hex(i),64,'0')) --
            // one fixed filler embedding for all rows (correctness of the EMBEDDING
            // VALUE is irrelevant to this read-route test; only non-null matters).
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_1024)"
                    + " SELECT ?, ?, decode(lpad(to_hex(i), 64, '0'), 'hex'), 'text', ?::nexus.vector"
                    + "   FROM generate_series(1, ?) i")) {
                ps.setString(1, TENANT_A);
                ps.setString(2, col);
                ps.setString(3, vectorLiteral(unit(1.0f, 0.0f)));
                ps.setInt(4, total);
                ps.executeUpdate();
            }

            // One manifest document, 50,000 catalog_document_chunks positions --
            // the real production shape (one document, many chunk positions).
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title)"
                    + " VALUES (?, ?, ?) ON CONFLICT DO NOTHING")) {
                ps.setString(1, TENANT_A);
                ps.setString(2, "unassigned-scale-doc");
                ps.setString(3, "unassigned-scale-doc");
                ps.executeUpdate();
            }
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks"
                    + " (tenant_id, doc_id, position, chash, chunk_index, collection)"
                    + " SELECT ?, ?, i, decode(lpad(to_hex(i), 64, '0'), 'hex'), i, ?"
                    + "   FROM generate_series(1, ?) i")) {
                ps.setString(1, TENANT_A);
                ps.setString(2, "unassigned-scale-doc");
                ps.setString(3, col);
                ps.setInt(4, total);
                ps.executeUpdate();
            }

            // Own-collection assignments for every row EXCEPT the 5 scattered gaps.
            java.sql.Array excluded = su.createArrayOf("integer",
                java.util.Arrays.stream(unassignedIdx).boxed().toArray());
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, source_collection)"
                    + " SELECT ?, decode(lpad(to_hex(i), 64, '0'), 'hex'), ?, 'centroid', ?"
                    + "   FROM generate_series(1, ?) i"
                    + "  WHERE i <> ALL (?)")) {
                ps.setString(1, TENANT_A);
                ps.setLong(2, topicId);
                ps.setString(3, col);
                ps.setInt(4, total);
                ps.setArray(5, excluded);
                ps.executeUpdate();
            }
        }

        long started = System.nanoTime();
        Map<String, Object> out = repo.unassignedChashes(TENANT_A, col, 100, null);
        long elapsedMs = (System.nanoTime() - started) / 1_000_000L;

        assertThat(out.get("has_taxonomy")).isEqualTo(true);
        assertThat((List<String>) (List<?>) out.get("chashes"))
            .as("exactly the 5 scattered gaps, found correctly at 50k-chunk scale")
            .containsExactlyElementsOf(expected);
        assertThat(elapsedMs)
            .as("generous bound -- correctness matters more than the exact number,"
                + " but a regression to an unindexed scan should still fail this")
            .isLessThan(15_000L);

        // Plan shape: the SAME real function, EXPLAINed directly (no hand-mirrored
        // query) -- must bind an index for the (tenant, collection) scope, never a
        // sequential scan of the WHOLE nexus.chunks table (which would touch every
        // OTHER tenant's/collection's rows too).
        Table<?> fn = TAXONOMY_UNASSIGNED_CHASHES_1024.call(col, 100, (String) null);
        String plan = tenantScope.withTenant(TENANT_A, ctx -> ctx.explain(ctx.selectFrom(fn)).plan());
        assertThat(plan)
            .as("must bind an index for the antijoin/scope scan, not a table-wide"
                + " sequential scan. Plan was:%n%s", plan)
            .containsIgnoringCase("index");
        assertThat(plan)
            .as("must not sequentially scan the WHOLE nexus.chunks table (every"
                + " tenant/collection), only this (tenant, collection)'s own range."
                + " Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
        // Round-2 review (code-review-expert): the two probed tables must stay
        // index-backed too. At 50k rows a sequential scan of either would not
        // trip the time bound above, so only the plan can catch it.
        assertThat(plan)
            .as("the manifest-liveness EXISTS must probe catalog_document_chunks by"
                + " index, not scan it. Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on catalog_document_chunks");
        assertThat(plan)
            .as("the unassigned antijoin must probe topic_assignments by index, not"
                + " scan it. Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on topic_assignments");
    }

    // ── helpers (mirrors TaxonomyAssignFromChashesRepositoryTest's own idiom) ────

    private static float[] unit(float x, float y) {
        float[] v = new float[DIM];
        v[0] = x;
        v[1] = y;
        return v;
    }

    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Matches SQL's {@code lpad(to_hex(i), 64, '0')} exactly. */
    private static String chashForIndex(int i) {
        return String.format("%064x", i);
    }

    /** Seeds a chunk AND a real {@code catalog_document_chunks} manifest row
     *  referencing it (one dedicated {@code catalog_documents} row per chash,
     *  position 0) -- the manifest-liveness fact the rework round's filter
     *  requires. See {@link #seedChunkNoManifest} for the deliberate exception. */
    private void seedChunk(String tenant, String collection, String hexChashValue, float[] emb) throws Exception {
        seedChunkNoManifest(tenant, collection, hexChashValue, emb);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String doc = "doc-" + hexChashValue;
            PgContainerHelper.insertCatalogDocument(ctx, tenant, doc);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks"
                    + " (tenant_id, doc_id, position, chash, chunk_index, collection)"
                    + " VALUES (?, ?, 0, decode(?, 'hex'), 0, ?)")) {
                ps.setString(1, tenant);
                ps.setString(2, doc);
                ps.setString(3, hexChashValue);
                ps.setString(4, collection);
                ps.executeUpdate();
            }
        }
    }

    /** {@link #seedChunk} WITHOUT the manifest row -- the deliberate "ghost"
     *  fixture {@link #manifestLessChunk_excluded_evenWhenOtherwiseUnassigned} needs. */
    private void seedChunkNoManifest(String tenant, String collection, String hexChashValue, float[] emb) throws Exception {
        registerCollection(tenant, collection);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks"
                    + " (tenant_id, collection, chash, chunk_text, embedding_1024)"
                    + " VALUES (?, ?, decode(?, 'hex'), ?, ?::nexus.vector)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setString(3, hexChashValue);
                ps.setString(4, "seed text " + hexChashValue);
                ps.setString(5, vectorLiteral(emb));
                ps.executeUpdate();
            }
        }
    }

    private void registerCollection(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection);
        }
    }

    /** Seeds one topic + its centroid in {@code collection} (own-collection). */
    private long seedCentroid(String tenant, String collection, String label, float[] emb) throws Exception {
        registerCollection(tenant, collection);
        long topicId = repo.insertTopic(tenant, label, null, collection, 0,
            "2026-01-01T00:00:00Z", null);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.taxonomy_centroids"
                    + " (tenant_id, collection, topic_id, label, embedding_1024) VALUES (?, ?, ?, ?, ?::nexus.vector)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setLong(3, topicId);
                ps.setString(4, "seed-centroid-" + label);
                ps.setString(5, vectorLiteral(emb));
                ps.executeUpdate();
            }
        }
        return topicId;
    }

    private static String vectorLiteral(float[] vec) {
        StringBuilder sb = new StringBuilder(vec.length * 8 + 2).append('[');
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }
}
