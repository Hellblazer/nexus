// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.DeadlockRetry;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.OwnerlessWriteActivity;
import dev.nexus.service.vectors.OwnerlessWriteMode;
import dev.nexus.service.vectors.OwnershipGuard;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 Phase 3 soak counters (nexus-wbfpw.66 round 2): the in-transaction ownership recheck of
 * {@code PgVectorRepository.upsertChunksInternal} runs INSIDE the {@code DeadlockRetry} lambda, so a
 * 40P01 on the insert that follows it re-runs the recheck. In log-only mode that recheck used to count
 * {@code would_refuse} and log on every attempt, so one request that was retried counted twice. The
 * soak reads {@code would_refuse} to decide the enforce flip, so a retried request must count ONCE.
 *
 * <p>The deadlock is genuine and unmocked. The chash loses its owner and its chunk row during the embed
 * (the same seam {@code OwnerlessWriteRefusalTest} uses), and at that seam a second connection inserts
 * the same chunk key and leaves it uncommitted. The write's {@code INSERT ... ON CONFLICT} then waits on
 * that insert, holding the shared sweep gate it took for the recheck; the second connection asks for the
 * gate EXCLUSIVELY, closing the cycle. Postgres kills the write (its deadlock timeout is the shorter),
 * the second connection commits, and the retried attempt runs the recheck again.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerlessWriteDeadlockRetryTest {

    private static final String SVC_ROLE = "svc_owdr";
    private static final String SVC_PASS = "svc_owdr_pass";
    private static final String TENANT = "owdr-tenant";
    private static final String COLLECTION = "knowledge__owdr-owner__voyage-context-3__v1";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private PgVectorRepository repo;
    private ExecutorService pool;

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
        cfg.setMaximumPoolSize(5);
        cfg.setConnectionTimeout(5000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        var embedder = new UnitEmbedder();
        repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);
        pool = Executors.newFixedThreadPool(2);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }
        OwnerlessWriteActivity.resetForTests();
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void logOnly_aRetriedInTransactionRecheckCountsTheRequestOnce() throws Exception {
        OwnerlessWriteActivity.resetForTests();
        String h = Chash.ofText("owdr-in-tx-retry").toHex();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertOwnedChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION, 1024, h);
        }
        AtomicReference<Connection> otherRef = new AtomicReference<>();
        repo.setAfterNeedEmbedResolvedHookForTests(() -> {
            try (Connection su = pg.createConnection("")) {
                var dsl = DSL.using(su, SQLDialect.POSTGRES);
                // The chash loses its owner and its chunk row, as a concurrent re-index plus sweep would.
                dsl.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                   .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT))
                   .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(Chash.fromHex(h).toBytes()))
                   .execute();
                var ch = DimTables.CHUNKS.get(1024);
                dsl.deleteFrom(ch.table())
                   .where(ch.tenantId().eq(TENANT).and(ch.collection().eq(COLLECTION)).and(ch.chash().eq(h)))
                   .execute();
                // Another writer re-creates the chunk key and leaves it uncommitted.
                Connection other = pg.createConnection("");
                other.setAutoCommit(false);
                DSLContext ex = DSL.using(other, SQLDialect.POSTGRES);
                // Its own deadlock check never fires first: the write is always the victim.
                ex.select(DSL.function("set_config", String.class,
                    DSL.val("deadlock_timeout"), DSL.val("120s"), DSL.val(false))).fetch();
                float[] v = new float[1024];
                v[1] = 1f;
                PgContainerHelper.insertChunks(ex, TENANT, COLLECTION, List.of(h), List.of("other writer"),
                    List.of(v), List.of(Map.of()));
                otherRef.set(other);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        long retriesBefore = DeadlockRetry.retryAttemptCount();
        var guard = new OwnershipGuard(OwnerlessWriteMode.LOG_ONLY, "upsert-chunks");
        try {
            Future<?> writer = pool.submit(() -> repo.upsertChunksWithTokens(TENANT, COLLECTION, List.of(h),
                List.of("text that differs from the stored seed"), List.of(Map.of()), false, List.of(), guard));
            assertThat(PgActivityProbe.waitsOnALock(pg, "%insert into%"))
                .as("the write's insert must block on the other writer's uncommitted chunk (else no race happened)")
                .isTrue();
            Connection other = otherRef.get();
            DSLContext ex = DSL.using(other, SQLDialect.POSTGRES);
            Future<?> closeCycle = pool.submit(() -> ex.select(DSL.function("pg_advisory_xact_lock", Object.class,
                DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + TENANT + "/" + COLLECTION)))).fetch());
            // Returns once the write was killed as the deadlock victim and its shared gate went.
            closeCycle.get(60, TimeUnit.SECONDS);
            other.commit();
            writer.get(60, TimeUnit.SECONDS);
        } finally {
            repo.setAfterNeedEmbedResolvedHookForTests(null);
            Connection other = otherRef.get();
            if (other != null) other.close();
        }

        assertThat(DeadlockRetry.retryAttemptCount() - retriesBefore)
            .as("the write must have been killed by a real 40P01 and retried").isGreaterThanOrEqualTo(1);
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal())
            .as("one request, however many attempts it took, is one would_refuse").isEqualTo(1);
        assertThat(OwnerlessWriteActivity.refusedTotal()).isZero();
        try (Connection su = pg.createConnection("")) {
            var ch = DimTables.CHUNKS.get(1024);
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne().from(ch.table())
                .where(ch.tenantId().eq(TENANT).and(ch.collection().eq(COLLECTION)).and(ch.chash().eq(h)))))
                .as("log-only writes it as today").isTrue();
        }
    }

    /** A vector per text; the width is the collection's model. */
    private static final class UnitEmbedder implements Embedder {
        @Override
        public String modelToken() {
            return "voyage-context-3";
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String ignored : texts) {
                float[] v = new float[1024];
                v[0] = 1f;
                out.add(v);
            }
            return out;
        }

        @Override
        public void close() {
        }
    }
}
