// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-a6mon — {@code nexus.gc_quarantine_orphans_bounded}, the bounded
 * quarantine sweep, through {@link PgVectorRepository#quarantineOrphansBounded}.
 *
 * <p>The unbounded {@code gc_quarantine_orphans} moves every eligible row in one
 * transaction; on 41,032 rows that ran 58 s past the ~30 s edge cut before
 * committing (owner-1.1 cleanup, v0.1.121). This pins the four properties the
 * bounded form adds: the bound is honoured per call, the caller can loop on
 * {@code remaining} to zero, {@code created_at} survives the move, and a
 * non-positive bound is refused rather than silently meaning "unbounded".
 *
 * <p>The created_at test is the one most likely to be read as decorative and is
 * not: the unbounded form omits created_at from its INSERT, so the column
 * default restamps every moved row to quarantine time. The owner-1.1 move did
 * exactly that to all 41,032 rows, destroying the collection's generation
 * history. The fixture BACKDATES the origin rows to a fixed past instant so a
 * restamp is distinguishable from carry-through; asserting merely "created_at
 * is set" would pass either way.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GcQuarantineOrphansBoundedTest {

    private static final String SVC_ROLE = "svc_a6mon_test";
    private static final String SVC_PASS = "svc_a6mon_test_pass";
    private static final String TENANT = "a6mon-tenant";
    private static final OffsetDateTime PAST =
        OffsetDateTime.of(2026, 7, 16, 0, 8, 44, 0, ZoneOffset.UTC);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository vecRepo;

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
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void boundIsHonouredPerCall_andRemainingCountsDownToZero() throws Exception {
        var p = seed("bound", 5);

        var first = vecRepo.quarantineOrphansBounded(TENANT, p.src(), p.dst(), "2026-09-16T00:00:00Z", 20, 2);
        assertThat(first.moved()).as("one call moves at most row_limit").isEqualTo(2);
        assertThat(first.remaining()).as("and says what is left, so the caller need not guess").isEqualTo(3);
        assertThat(chunkCount(p.src())).as("committed: the source really lost 2").isEqualTo(3);
        assertThat(chunkCount(p.dst())).as("committed: the quarantine really gained 2").isEqualTo(2);

        int calls = 1;
        var last = first;
        while (last.remaining() > 0) {
            last = vecRepo.quarantineOrphansBounded(TENANT, p.src(), p.dst(), "2026-09-16T00:00:00Z", 20, 2);
            calls++;
            assertThat(calls).as("loop must terminate").isLessThan(10);
        }
        assertThat(calls).as("5 rows at a bound of 2 is exactly three batches").isEqualTo(3);
        assertThat(chunkCount(p.src())).isZero();
        assertThat(chunkCount(p.dst())).as("nothing lost across the batches").isEqualTo(5);

        var idle = vecRepo.quarantineOrphansBounded(TENANT, p.src(), p.dst(), "2026-09-16T00:00:00Z", 20, 2);
        assertThat(idle.moved()).isZero();
        assertThat(idle.remaining()).as("an idempotent no-op once drained").isZero();
    }

    @Test
    void insertAndDeleteAgreeOnTheVictimSet() throws Exception {
        // The bounded form MUST choose its victims once. If the INSERT and the
        // DELETE re-evaluated the anti-join separately under a LIMIT they could
        // pick different rows and the DELETE could remove something never
        // copied. Conservation across a partial batch is the observable.
        var p = seed("victim", 7);
        vecRepo.quarantineOrphansBounded(TENANT, p.src(), p.dst(), "2026-09-16T00:00:00Z", 20, 3);
        assertThat(chunkCount(p.src()) + chunkCount(p.dst()))
            .as("rows are moved, never lost: source + quarantine == seeded, mid-drain")
            .isEqualTo(7);
        // Explicit intersection rather than doesNotContainAnyElementsOf: AssertJ
        // REFUSES an empty argument with an IllegalArgumentException, so the
        // shorter form errors (not fails) whenever the source is fully drained --
        // which the falsification probe did, and which a seed count at or below
        // the bound would do on a real run. An ERROR here would read as a broken
        // test, not as the conservation property it is meant to check.
        var inBoth = new java.util.HashSet<>(chashes(p.dst()).stream().map(java.util.Arrays::hashCode).toList());
        inBoth.retainAll(chashes(p.src()).stream().map(java.util.Arrays::hashCode).toList());
        assertThat(inBoth)
            .as("no chash is in BOTH the source and the quarantine: moved means moved")
            .isEmpty();
    }

    @Test
    void createdAtIsCarriedThrough_notRestampedToQuarantineTime() throws Exception {
        var p = seed("ts", 3);
        backdate(p.src(), PAST);
        // NON-VACUITY: the backdate must have landed, or "equals PAST" below
        // could only pass by coincidence of the clock.
        assertThat(createdAts(p.src())).as("guard: origin rows are backdated").containsOnly(PAST);

        vecRepo.quarantineOrphansBounded(TENANT, p.src(), p.dst(), "2026-09-16T00:00:00Z", 20, 10);

        assertThat(createdAts(p.dst()))
            .as("quarantined rows keep their ORIGINAL created_at. The unbounded form "
                + "restamps to now() via the column default -- the owner-1.1 move did "
                + "that to 41,032 rows and erased the collection's generation history")
            .containsOnly(PAST);
    }

    @Test
    void nonPositiveBound_isRefused_notTreatedAsUnbounded() throws Exception {
        var p = seed("refuse", 2);
        assertThatThrownBy(() ->
                vecRepo.quarantineOrphansBounded(TENANT, p.src(), p.dst(), "2026-09-16T00:00:00Z", 20, 0))
            .as("a zero bound must be an error, never a silent fallback to the 58-second transaction")
            .hasMessageContaining("p_row_limit must be positive");
        assertThat(chunkCount(p.src())).as("and nothing moved").isEqualTo(2);
    }

    @Test
    void theStatementBoundIsRealBecauseItIsSetBeforeTheCall() throws Exception {
        // substantive-critic finding on a990fe8f1: the function body's own
        // set_config('statement_timeout', '25000', true) cannot bound the
        // statement already running it (Postgres arms that timer once, at
        // top-level statement start), which is why the unbounded sweep's
        // in-body 5 s bound never stopped the 5m41s incident call. The engine
        // now sets the bound as its OWN statement before the call. This test
        // discriminates the two mechanisms: a foreign transaction holds the
        // sweep gate, the call is given a 300 ms statement bound and a 5 s
        // lock bound, and it must die at ~300 ms with 57014 (query_canceled).
        // With only the body's bounds in force it would instead die at the
        // body's 2 s lock_timeout with 55P03 (measured red that way with the
        // Java-side set removed).
        var p = seed("stmtbound", 2);
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES)
               .select(DSL.function("pg_advisory_xact_lock", Object.class,
                       DSL.function("hashtext", Integer.class,
                                    DSL.val("sweepgate:" + TENANT + "/" + p.src()))))
               .fetch();
            long started = System.nanoTime();
            Throwable thrown = null;
            try {
                vecRepo.quarantineOrphansBounded(
                    TENANT, p.src(), p.dst(), "2026-09-16T00:00:00Z", 20, 2, 300, 5_000);
            } catch (RuntimeException ex) {
                thrown = ex;
            }
            long elapsedMs = (System.nanoTime() - started) / 1_000_000L;
            holder.rollback();
            assertThat(thrown).as("the bounded call must fail while the gate is held").isNotNull();
            assertThat(sqlState(thrown))
                .as("died on the STATEMENT bound (57014), not the body's 2 s lock bound (55P03): %s", thrown)
                .isEqualTo("57014");
            assertThat(elapsedMs)
                .as("at the 300 ms statement bound, well inside the body's 2 s lock bound")
                .isLessThan(1_900L);
        }
        assertThat(chunkCount(p.src())).as("a cancelled batch moves nothing").isEqualTo(2);
    }

    private static String sqlState(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof java.sql.SQLException se && se.getSQLState() != null) {
                return se.getSQLState();
            }
        }
        return null;
    }

    // ── fixtures ──────────────────────────────────────────────────────────────

    private record Pair(String src, String dst) {}

    /** {@code n} orphan chunks (no manifest rows at all) in a fresh live collection. */
    private Pair seed(String slug, int n) throws Exception {
        String src = "knowledge__a6mon-" + slug + "__minilm-l6-v2-384__v1";
        String dst = "quarantine-knowledge__a6mon-" + slug + "__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, src);
        }
        List<String> hashes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            hashes.add(Chash.ofText(slug + "-orphan-" + i).toHex());
            texts.add(slug + " orphan " + i);
            metas.add(Map.of("title", slug + "-" + i));
        }
        vecRepo.upsertChunks(TENANT, src, hashes, texts, metas);
        return new Pair(src, dst);
    }

    private int chunkCount(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchCount(
            DSL.table(DSL.name("nexus", "chunks")),
            DSL.field("collection", String.class).eq(collection)));
    }

    private List<byte[]> chashes(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx
            .select(DSL.field("chash", byte[].class))
            .from(DSL.table(DSL.name("nexus", "chunks")))
            .where(DSL.field("collection", String.class).eq(collection))
            .fetch(0, byte[].class));
    }

    private List<OffsetDateTime> createdAts(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx
            .select(DSL.field("created_at", OffsetDateTime.class))
            .from(DSL.table(DSL.name("nexus", "chunks")))
            .where(DSL.field("collection", String.class).eq(collection))
            .fetch(0, OffsetDateTime.class))
            .stream().map(t -> t.withOffsetSameInstant(ZoneOffset.UTC)).toList();
    }

    private void backdate(String collection, OffsetDateTime to) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES)
               .update(DSL.table(DSL.name("nexus", "chunks")))
               .set(DSL.field("created_at", OffsetDateTime.class), to)
               .where(DSL.field("collection", String.class).eq(collection))
               .execute();
        }
    }
}
