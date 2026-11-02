// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.ScratchRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.t1.tables.records.ScratchRecord;
import org.jooq.DSLContext;
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
import java.util.UUID;

import static dev.nexus.service.db.TenantScope.T1_TENANT_GUC;
import static dev.nexus.service.jooq.t1.Tables.SCRATCH;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-mtheb.6 - the session-idle sweep (ScratchRepository.idleSessionRows) on tenants of realistic
 * size, with its plan.
 *
 * <p>The sweep deletes every row of a session that nothing has put or read since the cutoff. Its
 * correctness is pinned by ScratchRepositoryTest on a handful of rows; this class seeds a tenant in three
 * shapes (many small sessions, a few huge ones, 100k rows; layout in {@link #seed}), runs the real sweep
 * and asserts it deletes exactly the idle sessions' rows, and reads the plan of the same DELETE as the
 * service role under RLS and as the superuser (sweepExpired). {@code last_accessed} has no index, so the
 * question the plan answers is how often the idle test reads a session. The first form was a per-row
 * {@code NOT EXISTS}: one probe per old row reading the whole session (k rows cost k*k), a Nested Loop Anti
 * Join without statistics and still so with them on large sessions. The second was a whole-table GROUP BY,
 * which reads every row of the tenant even when nothing is idle. The form now in use takes the distinct
 * sessions of the old rows from idx_scratch_ts and aggregates each once. The test pins that shape, with and
 * without statistics (the first seconds of a new table, before autovacuum analyzes it): a probe keyed on the
 * candidate session (the qual of the lateral's own scan of the session carries {@code tenant_id = c.t} and
 * {@code session_id = c.sid}), no anti-join and no SubPlan, and on the unanalyzed table the candidates found
 * through idx_scratch_ts. A revert to either earlier form, or a hybrid that keeps the candidates and replaces
 * the lateral with a whole-table GROUP BY, fails on the positive half even once the table is analyzed.
 *
 * <p>The plan is the planner's estimate ({@code ctx.explain}, typed); its actual timings and buffers were
 * read with EXPLAIN (ANALYZE, BUFFERS) and are in the {@code idleSessionRows} javadoc and the nexus-mtheb.6
 * write-back. The estimate is enough for the pin because the join strategy is what differs.
 *
 * <p>Hermetic: Testcontainers Postgres, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ScratchSweepVolumeTest {

    private static final String SVC_ROLE = "svc_scratch_volume_test";
    private static final String SVC_PASS = "svc_scratch_volume_test_pass";
    private static final String TENANT = "sweep-volume-tenant";
    private static final String OTHER_TENANT = "sweep-volume-other-tenant";

    private static final int IDLE_PERCENT = 50;
    private static final int LIVE_WRITE_PERCENT = 20;
    private static final int LIVE_READ_PERCENT = 15;
    // the remaining 15 percent are fresh

    /** One tenant shape: sessions x rows per session. */
    private record Shape(String name, int sessions, int rowsPerSession) {
        int total() { return sessions * rowsPerSession; }
        int idleSessions() { return sessions * IDLE_PERCENT / 100; }
        int liveWriteEnd() { return idleSessions() + sessions * LIVE_WRITE_PERCENT / 100; }
        int liveReadEnd() { return liveWriteEnd() + sessions * LIVE_READ_PERCENT / 100; }
        int idleRows() { return idleSessions() * rowsPerSession; }
    }

    private static final Shape SMALL_SESSIONS = new Shape("200 sessions x 50 rows", 200, 50);
    private static final List<Shape> SHAPES = List.of(
        SMALL_SESSIONS,
        new Shape("20 sessions x 2000 rows", 20, 2000),
        new Shape("4000 sessions x 25 rows", 4000, 25));

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope tenantScope;
    ScratchRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.grantT1ScratchTo(su, SVC_ROLE);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new ScratchRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * One tenant, every ts back-dated 3 h unless the layout says otherwise (the cutoff in the tests is
     * now - 1 h). By session index: the first 50 percent are idle (every row old, never read); the next
     * 20 percent wrote once recently (row 0 is new); the next 15 percent were read recently (row 0 has
     * last_accessed now, every ts old); the last 15 percent are fresh. A second tenant reuses the same
     * session ids with all-fresh rows, so the tenant correlation matters. Nothing here runs ANALYZE (jOOQ
     * has no typed form and raw SQL is ratcheted out of the test tree), so the planner works from whatever
     * statistics autovacuum has taken, none in the first seconds of a new table; the sweep must not depend on them.
     */
    private void seed(DSLContext su, Shape sh) {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        OffsetDateTime old = now.minusHours(3);
        su.truncate(SCRATCH).execute();
        List<ScratchRecord> batch = new ArrayList<>();
        for (int s = 0; s < sh.sessions(); s++) {
            for (int r = 0; r < sh.rowsPerSession(); r++) {
                boolean liveWrite = s >= sh.idleSessions() && s < sh.liveWriteEnd() && r == 0;
                boolean liveRead = s >= sh.liveWriteEnd() && s < sh.liveReadEnd() && r == 0;
                boolean fresh = s >= sh.liveReadEnd();
                batch.add(row(TENANT, "sess-" + s, "row " + r,
                    (liveWrite || fresh) ? now : old, liveRead ? now : null));
                batch.add(row(OTHER_TENANT, "sess-" + s, "other " + r, now, null));
                if (batch.size() >= 2000) flush(su, batch);
            }
        }
        flush(su, batch);
    }

    private static ScratchRecord row(String tenant, String session, String content, OffsetDateTime ts,
                                     OffsetDateTime lastAccessed) {
        ScratchRecord rec = new ScratchRecord();
        rec.setId(UUID.randomUUID().toString());
        rec.setTenantId(tenant);
        rec.setSessionId(session);
        rec.setContent(content);
        rec.setTs(ts);
        rec.setLastAccessed(lastAccessed);
        return rec;
    }

    private static void flush(DSLContext su, List<ScratchRecord> batch) {
        if (batch.isEmpty()) return;
        su.batchInsert(batch).execute();
        batch.clear();
    }

    private int countRows(DSLContext su, String tenant) {
        return su.fetchCount(SCRATCH, SCRATCH.TENANT_ID.eq(tenant));
    }

    /**
     * The first shape's precondition: nobody has analyzed t1.scratch, by hand or by autovacuum (both stamp
     * pg_stat_user_tables), so the planner is working without statistics. Fails naming the cause, not with a
     * plan mismatch that looks like a code regression.
     */
    private static void assertNeverAnalyzed(DSLContext su) {
        var stats = su.select(DSL.field(DSL.name("last_analyze"), OffsetDateTime.class),
                              DSL.field(DSL.name("last_autoanalyze"), OffsetDateTime.class))
            .from(DSL.table(DSL.name("pg_stat_user_tables")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq("t1"))
            .and(DSL.field(DSL.name("relname"), String.class).eq("scratch"))
            .fetchOne();
        assertThat(stats).as("t1.scratch is in pg_stat_user_tables").isNotNull();
        assertThat(stats.value1()).as("precondition: t1.scratch was analyzed by hand; the no-statistics plan "
            + "assertions below do not apply").isNull();
        assertThat(stats.value2()).as("precondition: autovacuum analyzed t1.scratch between the seed and the "
            + "explain; the no-statistics plan assertions below do not apply (this is the cluster's timing, "
            + "not the sweep's code)").isNull();
    }

    /** The planner's plan for the sweep's DELETE as the service role sees it (RLS on, tenant GUC stamped). */
    private String explainSweepUnderRls(OffsetDateTime cutoff) {
        return tenantScope.withTenant(TENANT, T1_TENANT_GUC, dsl ->
            dsl.explain(dsl.deleteFrom(SCRATCH).where(ScratchRepository.idleSessionRows(cutoff))).plan());
    }

    /** The planner's plan for the same DELETE as sweepExpired runs it: superuser, no RLS, every tenant. */
    private String explainSweepAsSuperuser(DSLContext su, OffsetDateTime cutoff) {
        return su.explain(su.deleteFrom(SCRATCH).where(ScratchRepository.idleSessionRows(cutoff))).plan();
    }

    /**
     * The scan of the lateral aggregate: the node over {@code scratch s} (the lateral's alias) and the qual line
     * under it (Index Cond, Recheck Cond or Filter). A join's own Join Filter, Hash Cond or Merge Cond also name
     * {@code c.sid}, so they cannot be what the pin reads.
     */
    private static final java.util.regex.Pattern LATERAL_SCAN =
        java.util.regex.Pattern.compile("on scratch s\\b[^\\n]*\\n([^\\n]*)");

    /**
     * The shape idleSessionRows promises, in whatever regime the planner is in: the candidate sessions are
     * found from the old rows, each is aggregated by one probe keyed on that session, and nothing probes per old
     * row. The positive half reads the qual of the lateral's own scan of {@code scratch s}: it must carry both
     * lateral parameters ({@code tenant_id = c.t} and {@code session_id = c.sid}). A whole-table GROUP BY joined
     * to the candidates (the hybrid that keeps the candidate subquery and drops the lateral) scans {@code s}
     * with no parameter, and the planner's Hash Semi Join, Hash Anti Join or Merge Join carries
     * {@code c.sid} only in its join condition, so both fail here even once the table has been analyzed.
     */
    private void assertCandidateDrivenPlan(String what, String plan) {
        var scans = LATERAL_SCAN.matcher(plan);
        boolean keyed = false;
        int seen = 0;
        while (scans.find()) {
            seen++;
            String qual = scans.group(1);
            keyed |= qual.contains("session_id = c.sid") && qual.contains("tenant_id = c.t");
        }
        assertThat(keyed).as("%s: the lateral's scan of scratch s (%d seen) is keyed on the candidate, "
            + "tenant_id = c.t and session_id = c.sid:%n%s", what, seen, plan).isTrue();
        assertThat(plan).as("%s: no per-row probe of the session:%n%s", what, plan)
            .doesNotContain("Nested Loop Anti Join")
            .doesNotContain("SubPlan");
    }

    private void sweepAndAssert(DSLContext su, Shape sh, OffsetDateTime cutoff, String what) {
        int swept = repo.sweepTenant(TENANT, cutoff);
        assertThat(swept).as("%s: every row of the %d idle sessions and no other", what, sh.idleSessions())
            .isEqualTo(sh.idleRows());
        assertThat(countRows(su, TENANT)).as("%s: live-write, live-read and fresh sessions keep every row", what)
            .isEqualTo(sh.total() - sh.idleRows());
        assertThat(countRows(su, OTHER_TENANT)).as("%s: the other tenant is untouched", what)
            .isEqualTo(sh.total());
    }

    @Test
    void sweepDeletesExactlyTheIdleSessionsRowsAndNeverProbesPerRow() throws Exception {
        StringBuilder report = new StringBuilder();
        try (Connection conn = pg.createConnection("")) {
            DSLContext su = DSL.using(conn, SQLDialect.POSTGRES);

            // First, on a table that is seconds old: no statistics, as on one autovacuum has not reached yet.
            // The per-row NOT EXISTS planned here as a Nested Loop Anti Join (measured: 270k buffers for
            // 10k rows against 405 with statistics).
            // Autovacuum is off for the table from before the seed to after the explains, so the table stays
            // unanalyzed whatever the cluster's timing or version; the precondition is asserted, not assumed.
            PgContainerHelper.setTableStorageParams(conn, SCRATCH, Map.of("autovacuum_enabled", "false"));
            String noStats;
            String noStatsSu;
            OffsetDateTime cutoff;
            try {
                seed(su, SMALL_SESSIONS);
                assertThat(countRows(su, TENANT)).isEqualTo(SMALL_SESSIONS.total());
                cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusHours(1);
                assertNeverAnalyzed(su);
                noStats = explainSweepUnderRls(cutoff);
                noStatsSu = explainSweepAsSuperuser(su, cutoff);
                assertNeverAnalyzed(su);
            } finally {
                PgContainerHelper.resetTableStorageParams(conn, SCRATCH, "autovacuum_enabled");
            }
            report.append("PLAN ").append(SMALL_SESSIONS.name()).append(" (no statistics, RLS)\n").append(noStats).append("\n");
            assertCandidateDrivenPlan(SMALL_SESSIONS.name() + ", no statistics", noStats);
            // Only here, where the planner has no statistics to change its mind with: the candidates are found
            // through the ts index, not by reading the table (later shapes may have been analyzed by autovacuum,
            // and with most of a table old a seq scan can be the planner's honest choice).
            // Twice: the DELETE reads the old rows through it, and so does the candidate subquery. A candidate
            // set that is not restricted to old rows (every session) would leave one.
            assertThat(java.util.regex.Pattern.compile("(?:on|using) idx_scratch_ts").matcher(noStats).results().count())
                .as("the delete and the candidate subquery both read the old rows through the ts index:%n%s", noStats)
                .isGreaterThanOrEqualTo(2);
            report.append("PLAN ").append(SMALL_SESSIONS.name()).append(" (no statistics, superuser)\n").append(noStatsSu).append("\n");
            assertCandidateDrivenPlan(SMALL_SESSIONS.name() + ", no statistics, superuser", noStatsSu);
            sweepAndAssert(su, SMALL_SESSIONS, cutoff, SMALL_SESSIONS.name() + ", no statistics");

            for (Shape sh : SHAPES) {
                seed(su, sh);
                assertThat(countRows(su, TENANT)).isEqualTo(sh.total());
                cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusHours(1);
                String plan = explainSweepUnderRls(cutoff);
                report.append("PLAN ").append(sh.name()).append(" (RLS)\n").append(plan).append("\n");
                assertCandidateDrivenPlan(sh.name(), plan);
                String planSu = explainSweepAsSuperuser(su, cutoff);
                report.append("PLAN ").append(sh.name()).append(" (superuser)\n").append(planSu).append("\n");
                assertCandidateDrivenPlan(sh.name() + ", superuser", planSu);
                long t0 = System.nanoTime();
                sweepAndAssert(su, sh, cutoff, sh.name());
                report.append("MEASURE ").append(sh.name()).append(": sweep and assertions ")
                      .append((System.nanoTime() - t0) / 1_000_000).append(" ms\n");
            }
        }
        System.out.println("MTHEB6-REPORT\n" + report);
    }
}
