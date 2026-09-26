/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-f3yxx rework round (coordinator's structural lock-hold fix,
 * confirmed by both code-review-expert and substantive-critic as Significant):
 * {@link TaxonomyRepository#assignFromChashes}'s own pass and cross pass now
 * run in SEPARATE transactions — the own pass commits (and releases its
 * {@code nexus.topics} row lock) BEFORE the cross pass's transaction ever
 * opens, independent of either pass's speed.
 *
 * <p><strong>Why a deterministic lock-inspection seam, not a real concurrent
 * caller.</strong> A "does a second concurrent caller block" test measures the
 * SAME fact this seam measures, but only probabilistically: it depends on
 * winning a timing race (the second caller's request must land while the
 * first caller's own pass is still mid-flight), which is either flaky under
 * load or requires an artificial slowdown hook of its own — at which point
 * the hook, not the timing, is doing the real work anyway. This test instead
 * uses {@link TaxonomyRepository#duringOwnPassHookForTests} and
 * {@link TaxonomyRepository#betweenOwnAndCrossPassHookForTests} (package-
 * visible test seams added for exactly this purpose) to query {@code
 * pg_locks} from a SECOND, independent connection at two fixed points in the
 * SAME call: once while the own pass's transaction is still open, once after
 * it has committed and before the cross pass's transaction begins. No
 * timing race, no sleep, no thread pool — the assertion is exact regardless
 * of machine speed.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyAssignSeparateTransactionsTest {

    private static final String SVC_ROLE = "svc_afc_txnsplit_test";
    private static final String SVC_PASS = "svc_afc_txnsplit_pass";
    private static final String TENANT = "afc-txnsplit-tenant";
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
                PgContainerHelper.grantExecuteOnFunction(
                    su, "nexus.assign_from_chashes_" + dim + "(text, text[], boolean)", SVC_ROLE);
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

    @AfterEach
    void resetHooks() {
        // Test seams default to no-op in production; restore that between tests
        // so one test's inspection lambda can never leak into the next.
        repo.duringOwnPassHookForTests = () -> { };
        repo.betweenOwnAndCrossPassHookForTests = () -> { };
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    @Test
    void ownPassTopicsLockIsHeldDuringItsOwnTransactionAndReleasedBeforeCrossBegins() throws Exception {
        String col = "code__txnsplit_own__voyage-code-3__v1";
        String colForeign = "code__txnsplit_fgn__voyage-code-3__v1";
        String c1 = hexChash("txnsplit-c1");
        seedChunk(TENANT, col, c1, unit(1.0f, 0.0f));
        long tOwn = seedTopic(TENANT, col, "txnsplit-own-topic");
        seedCentroid(TENANT, col, tOwn, unit(1.0f, 0.0f));
        long tForeign = seedTopic(TENANT, colForeign, "txnsplit-foreign-topic");
        seedCentroid(TENANT, colForeign, tForeign, unit(1.0f, 0.0f));

        List<Boolean> lockedDuringOwn = new ArrayList<>();
        List<Boolean> lockedBetweenPasses = new ArrayList<>();
        repo.duringOwnPassHookForTests = () -> lockedDuringOwn.add(topicsLockHeldByAnotherBackend());
        repo.betweenOwnAndCrossPassHookForTests = () -> lockedBetweenPasses.add(topicsLockHeldByAnotherBackend());

        Map<String, Object> out = repo.assignFromChashes(TENANT, col, List.of(c1), true);
        assertThat(out.get("assigned")).isEqualTo(1);
        assertThat(out.get("cross_assigned")).isEqualTo(1);

        assertThat(lockedDuringOwn).as("duringOwnPassHookForTests must fire exactly once").hasSize(1);
        assertThat(lockedBetweenPasses).as("betweenOwnAndCrossPassHookForTests must fire exactly once").hasSize(1);
        assertThat(lockedDuringOwn.get(0))
            .as("the own pass's persisted row (topic_assignments -> topics FK) plus the"
                + " doc_count recount trigger's UPDATE both hold a lock on nexus.topics"
                + " WHILE the own pass's own transaction is still open -- proving this"
                + " fixture actually exercises a real lock, not a vacuous check")
            .isTrue();
        assertThat(lockedBetweenPasses.get(0))
            .as("that lock is fully released by the time the hook fires between the two"
                + " passes -- i.e. the own pass's transaction has COMMITTED and the cross"
                + " pass's transaction has not yet opened. This is the structural proof:"
                + " a future regression that merges the two passes back into one shared"
                + " transaction would see this hook fire while the own-pass lock is STILL"
                + " held, going red here.")
            .isFalse();
    }

    @Test
    void ownPassAloneStillReleasesItsLockEvenWithoutACrossPass() throws Exception {
        // crossCollection=false: betweenOwnAndCrossPassHookForTests still fires
        // (it marks "own pass committed", independent of whether cross follows)
        // and must see the lock already gone.
        String col = "code__txnsplit_ownonly__voyage-code-3__v1";
        String c1 = hexChash("txnsplit-ownonly-c1");
        seedChunk(TENANT, col, c1, unit(1.0f, 0.0f));
        long tOwn = seedTopic(TENANT, col, "txnsplit-ownonly-topic");
        seedCentroid(TENANT, col, tOwn, unit(1.0f, 0.0f));

        List<Boolean> lockedBetweenPasses = new ArrayList<>();
        repo.betweenOwnAndCrossPassHookForTests = () -> lockedBetweenPasses.add(topicsLockHeldByAnotherBackend());

        Map<String, Object> out = repo.assignFromChashes(TENANT, col, List.of(c1), false);
        assertThat(out.get("assigned")).isEqualTo(1);
        assertThat(out.get("cross_assigned")).isEqualTo(0);

        assertThat(lockedBetweenPasses).hasSize(1);
        assertThat(lockedBetweenPasses.get(0))
            .as("own pass's lock released even when no cross pass follows")
            .isFalse();
    }

    /** True iff any OTHER backend currently holds a lock touching {@code
     *  nexus.topics} (relation- or tuple-level; the doc_count trigger's row
     *  UPDATE and the topic_assignments FK's implicit KEY SHARE both show up
     *  this way). Opens its OWN, independent connection each call -- a
     *  superuser connection so RLS never filters {@code pg_locks} itself
     *  (a system catalog, unaffected by RLS in any case, but consistent with
     *  every other diagnostic query in this test tree). */
    private boolean topicsLockHeldByAnotherBackend() {
        // Plain JDBC, not DSL.condition("...") -- pg_locks.relation = 'X'::regclass
        // and pg_backend_pid() are assembled SQL expressions with no typed jOOQ DSL
        // form (RawSqlGateTest's noRawSqlDslTemplatesInMainOrTestSources scans for
        // exactly this DSL.condition(String) shape); raw JDBC is the established,
        // ceiling-tracked escape for a system-catalog diagnostic read like this one.
        try (Connection su = pg.createConnection("");
             var st = su.createStatement();
             var rs = st.executeQuery(
                 "SELECT count(*) FROM pg_catalog.pg_locks"
                 + " WHERE relation = 'nexus.topics'::regclass AND pid <> pg_backend_pid()")) {
            return rs.next() && rs.getInt(1) > 0;
        } catch (Exception e) {
            throw new RuntimeException("pg_locks probe failed", e);
        }
    }

    // ── helpers (mirroring TaxonomyAssignBoundsIntegrationTest) ─────────────

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
            return HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private void registerCollection(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection);
        }
    }

    private void seedChunk(String tenant, String collection, String hexChashValue, float[] emb) throws Exception {
        registerCollection(tenant, collection);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES)
               .insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                           CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_1024)
               .values(tenant, collection, HexFormat.of().parseHex(hexChashValue),
                       "seed text " + hexChashValue, Vector.of(emb))
               .execute();
        }
    }

    private long seedTopic(String tenant, String collection, String label) throws Exception {
        registerCollection(tenant, collection);
        return repo.insertTopic(tenant, label, null, collection, 0, "2026-01-01T00:00:00Z", null);
    }

    private void seedCentroid(String tenant, String collection, long topicId, float[] emb) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES)
               .insertInto(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                           TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.EMBEDDING_1024)
               .values(tenant, collection, topicId, "seed-centroid-label", Vector.of(emb))
               .execute();
        }
    }
}
