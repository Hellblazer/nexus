/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.fail;

/**
 * nexus-r0vkh: the taxonomy assign transaction is bounded in BOTH run time
 * and lock wait, on the REAL {@link TaxonomyRepository#assignFromChashes}
 * path against a real Postgres.
 *
 * <p>The production shape (2026-09-16 09:05Z, engine-service-v0.1.123): one
 * {@code assign_from_chashes_1024} call ran 782s and eight identical calls
 * queued on its transactionid, each holding a pool connection, because the
 * doc_count recount trigger and the {@code topic_assignments -> topics} FK
 * both take row locks on {@code nexus.topics}. The waiter half is what this
 * class reproduces: a foreign transaction holds {@code FOR UPDATE} on the
 * topic the assignment will land on, and the assign call must FAIL within
 * the lock bound instead of waiting for the holder.
 *
 * <p>Falsification (measured while writing this): with
 * {@code PgSession.setTaxonomyAssignBounds(ctx)} removed from
 * {@code assignFromChashes}, {@link #aCallerQueuedOnTheTopicsRowLockFailsWithinTheLockBound}
 * fails on its own watchdog with "still waiting", never on the assertion.
 *
 * <p>Since the critic's round, a lock timeout gets ONE retry after a pause
 * ({@code TaxonomyRepository.LOCK_TIMEOUT_RETRY_PAUSE_MS}): a holder that
 * outlives both attempts still fails (first test), a holder that lets go
 * during the pause or the second wait succeeds on the retry (second test).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyAssignBoundsIntegrationTest {

    private static final String SVC_ROLE = "svc_tab_test";
    private static final String SVC_PASS = "svc_tab_test_pass";
    private static final String TENANT = "tab-tenant";
    private static final int DIM = 1024;

    static final String LOCK_NOT_AVAILABLE = "55P03";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TaxonomyRepository repo;
    ExecutorService pool;

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
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyRepository(tenantScope);
        pool = Executors.newSingleThreadExecutor();
    }

    @AfterAll
    void stopAll() {
        if (pool  != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    @Test
    void aCallerQueuedOnTheTopicsRowLockFailsWithinTheLockBound() throws Exception {
        String col = "code__tab_lock__voyage-code-3__v1";
        String c1 = hexChash("tab-chash-1");
        seedChunk(TENANT, col, c1, unit(1.0f, 0.0f));
        long t1 = seedTopic(TENANT, col, "tab-topic-1");
        seedCentroid(TENANT, col, t1, unit(1.0f, 0.0f));

        int lockBoundMs = PgSession.startupTaxonomyAssignLockTimeoutMs();
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES)
               .select(TOPICS.ID).from(TOPICS).where(TOPICS.ID.eq(t1)).forUpdate().fetch();
            // The assign's INSERT into topic_assignments takes KEY SHARE on
            // topics(t1) through the FK and the recount trigger UPDATEs the
            // same row; both conflict with the holder's FOR UPDATE.
            long worstCaseMs = 2L * lockBoundMs + TaxonomyRepository.LOCK_TIMEOUT_RETRY_PAUSE_MS;
            long started = System.nanoTime();
            Future<Throwable> outcome = pool.submit(() -> {
                try {
                    repo.assignFromChashes(TENANT, col, List.of(c1), false);
                    return null;
                } catch (RuntimeException ex) {
                    return ex;
                }
            });
            Throwable thrown;
            try {
                thrown = outcome.get(worstCaseMs + 25_000L, TimeUnit.MILLISECONDS);
            } catch (TimeoutException te) {
                outcome.cancel(true);
                fail("assignFromChashes is still waiting " + (worstCaseMs + 25_000L)
                    + "ms after the topics row lock was taken by another transaction:"
                    + " the lock bound is not in force on that transaction");
                return;
            } catch (ExecutionException ee) {
                thrown = ee.getCause();
            }
            long elapsedMs = (System.nanoTime() - started) / 1_000_000L;
            holder.rollback();

            assertThat(thrown).as("the queued caller must fail, not wait for the holder").isNotNull();
            assertThat(sqlState(thrown))
                .as("lock_timeout trips as lock_not_available: %s", thrown)
                .isEqualTo(LOCK_NOT_AVAILABLE);
            assertThat(elapsedMs)
                .as("failed after two lock bounds and the retry pause (%dms), not the statement bound or the holder's lifetime", worstCaseMs)
                .isGreaterThanOrEqualTo(worstCaseMs - 500L)
                .isLessThan(worstCaseMs + 20_000L);
        }
        // The holder rolled back: the same call now assigns, so the failure
        // above was the bound, not a broken fixture.
        var out = repo.assignFromChashes(TENANT, col, List.of(c1), false);
        assertThat(out.get("assigned")).isEqualTo(1);
    }

    @Test
    void aHolderThatLetsGoDuringTheRetryWindowIsRecoveredByTheSingleRetry() throws Exception {
        String col = "code__tab_retry__voyage-code-3__v1";
        String c1 = hexChash("tab-retry-chash-1");
        seedChunk(TENANT, col, c1, unit(1.0f, 0.0f));
        long t1 = seedTopic(TENANT, col, "tab-retry-topic-1");
        seedCentroid(TENANT, col, t1, unit(1.0f, 0.0f));

        int lockBoundMs = PgSession.startupTaxonomyAssignLockTimeoutMs();
        // Hold past the FIRST bound, release inside the retry's window: the
        // healthy-head overlap the retry exists for.
        long holdMs = lockBoundMs + TaxonomyRepository.LOCK_TIMEOUT_RETRY_PAUSE_MS / 2;
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES)
               .select(TOPICS.ID).from(TOPICS).where(TOPICS.ID.eq(t1)).forUpdate().fetch();
            long started = System.nanoTime();
            Future<Map<String, Object>> outcome = pool.submit(
                () -> repo.assignFromChashes(TENANT, col, List.of(c1), false));
            Thread.sleep(holdMs);
            holder.rollback();
            Map<String, Object> out = outcome.get(2L * lockBoundMs + 25_000L, TimeUnit.MILLISECONDS);
            long elapsedMs = (System.nanoTime() - started) / 1_000_000L;
            assertThat(out.get("assigned")).as("the retry assigned the batch").isEqualTo(1);
            assertThat(elapsedMs)
                .as("succeeded on the retry, i.e. after the first bound tripped")
                .isGreaterThanOrEqualTo(lockBoundMs - 500L);
        }
    }

    @Test
    void theExplicitFormSetsBothGucsTransactionLocally() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            PgSession.setTaxonomyAssignBounds(ctx, 250, 100);
            assertThat(settingMs(ctx, "statement_timeout")).isEqualTo("250");
            assertThat(settingMs(ctx, "lock_timeout")).isEqualTo("100");
            c.rollback();
            assertThat(settingMs(ctx, "lock_timeout"))
                .as("SET LOCAL: reverts with the transaction").isEqualTo("0");
        }
    }

    @Test
    void theEnvResolvedFormBindsTheDocumentedDefaultsWhenUnset() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            PgSession.setTaxonomyAssignBounds(ctx);
            assertThat(settingMs(ctx, "statement_timeout"))
                .isEqualTo(Integer.toString(PgSession.startupTaxonomyAssignStatementTimeoutMs()));
            assertThat(settingMs(ctx, "lock_timeout"))
                .isEqualTo(Integer.toString(PgSession.startupTaxonomyAssignLockTimeoutMs()));
            c.rollback();
        }
    }

    @Test
    void everyTaxonomyAssignBoundsCallSiteIsTheFirstStatementInItsOwnTransaction() throws Exception {
        Path src = Path.of("src", "main", "java", "dev", "nexus", "service", "db", "TaxonomyRepository.java");
        List<String> lines = Files.readAllLines(src);
        // A LIVE statement, not the text inside a comment (the first cut of
        // this pin stayed green with the call commented out).
        List<Integer> sites = new java.util.ArrayList<>();
        for (int i = 0; i < lines.size(); i++) {
            if (lines.get(i).strip().equals("PgSession.setTaxonomyAssignBounds(ctx);")) {
                sites.add(i);
            }
        }
        // nexus-f3yxx rework round (structural lock-hold fix): the own pass and
        // the cross pass now run in SEPARATE transactions, each bounding itself;
        // unassignedChashes reuses the same helper for its own (read-only)
        // transaction. Three legitimate sites, not the pre-rework one.
        // nexus-v4pj4 (round-2 review decision): +1 (4) -- crossPreview (the
        // read-only cross-preview route) is the identical LATERAL-over-
        // centroids shape as the cross pass and bounds itself the same way,
        // via crossPreviewOnePass (see that method's own javadoc for why the
        // actual .selectFrom(fn) fetch is factored into a separate helper --
        // HnswServingGucParityTest's unrelated file-wide GUC-pairing count
        // would otherwise be broken by this route's Java-layer bound).
        assertThat(sites)
            .as("four live call sites: assignFromChashes's own pass, its cross"
                + " pass, unassignedChashes, and crossPreview -- each transaction"
                + " bounds itself")
            .hasSize(4);
        for (int site : sites) {
            int open = lastIndexOfLineBefore(lines, site, "tenantScope.withTenant(tenant, ctx -> {");
            int firstCode = firstNonCommentCodeLineAfter(lines, open);
            assertThat(firstCode)
                .as("line %d: PgSession.setTaxonomyAssignBounds must be the FIRST code"
                    + " statement inside its own enclosing withTenant block (opened at"
                    + " line %d) -- no query may run before the transaction is bounded",
                    site + 1, open + 1)
                .isEqualTo(site);
        }
    }

    private static int lastIndexOfLineBefore(List<String> lines, int before, String needle) {
        for (int i = before - 1; i >= 0; i--) {
            if (lines.get(i).contains(needle)) {
                return i;
            }
        }
        throw new AssertionError("no line containing " + needle + " before line " + before);
    }

    /** The first non-blank, non-comment line after {@code after} -- the actual
     *  first CODE statement inside a block, skipping {@code //}/{@code /* }/{@code *}
     *  lines a raw "next line" check would trip on. */
    private static int firstNonCommentCodeLineAfter(List<String> lines, int after) {
        for (int i = after + 1; i < lines.size(); i++) {
            String s = lines.get(i).strip();
            if (s.isEmpty() || s.startsWith("//") || s.startsWith("/*") || s.startsWith("*")) {
                continue;
            }
            return i;
        }
        throw new AssertionError("no code line found after line " + after);
    }

    // ── helpers (mirroring TaxonomyAssignFromChashesRepositoryTest) ─────────

    private static String sqlState(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLException se && se.getSQLState() != null) {
                return se.getSQLState();
            }
        }
        return null;
    }

    /** pg_settings.setting is always in the GUC's base unit (ms here), where
     *  current_setting() renders 30000ms as "30s". */
    private static String settingMs(DSLContext ctx, String guc) {
        return ctx.select(DSL.field(DSL.name("setting"), String.class))
                  .from(DSL.table(DSL.name("pg_catalog", "pg_settings")))
                  .where(DSL.field(DSL.name("name"), String.class).eq(guc))
                  .fetchOne(0, String.class);
    }

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
