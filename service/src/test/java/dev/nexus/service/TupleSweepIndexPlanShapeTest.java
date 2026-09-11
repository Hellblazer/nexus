// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.Statement;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-205 Phase 1 follow-on (bead nexus-em75s.34) — EXPLAIN-based plan-shape
 * proof that {@code tuples-002-sweep-indexes.xml}'s four indexes are the ones
 * the tuple sweep's three arm queries and the ack/nack claim_id lookup
 * actually use, at a cardinality large enough that the planner's own cost
 * model prefers them over a sequential scan on real statistics — mirroring
 * {@code ChashProbePlanShapeTest}'s / {@code TaxonomyCentroidAnnPlanShapeTest}'s
 * methodology: EXPLAIN of the SHIPPED query shape (copied from {@code
 * TupleRepository}'s own jOOQ construction, not a hand-written duplicate)
 * through the real {@code nexus_svc}/FORCE-RLS path, asserting the expected
 * index name appears and {@code Seq Scan} does not.
 *
 * <p>Cardinality (5,000 rows each of "available", "claimed", and claim-log):
 * modest but non-trivial, sized like {@code TaxonomyCentroidAnnPlanShapeTest}'s
 * own 3,000-row choice — large enough that the planner naturally prefers the
 * index, small enough to seed fast under Testcontainers. Within each set, a
 * SMALL minority (200 rows) are seeded already past their sweep threshold
 * (lapsed lease / expired / past claim-log TTL) so the purge-shaped queries
 * (arm 2, arm 3) are genuinely selective — an unselective predicate would let
 * the planner reasonably prefer a sequential scan even with the index
 * present, proving nothing about which index a REAL (selective) sweep visit
 * uses.
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleSweepIndexPlanShapeTest {

    private static final String TENANT = "planshape-tuples-tenant";
    private static final int AVAILABLE_ROWS = 5_000;
    private static final int CLAIMED_ROWS = 5_000;
    private static final int LOG_ROWS = 5_000;
    /** Of each set above, how many are seeded PAST their sweep threshold. */
    private static final int PAST_THRESHOLD = 200;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        seedAtCardinality();

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    private void seedAtCardinality() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            Statement st = su.createStatement();

            // Available (unclaimed) rows -- the bulk far from expiry, a small
            // PAST_THRESHOLD subset already expired (arm 2's target).
            st.execute(
                "INSERT INTO nexus.tuples (id, tenant_id, subspace, template, keys, expires_at, created_at) " +
                "SELECT decode(md5('avail-' || i), 'hex'), '" + TENANT + "', 'mailbox/planshape-avail', " +
                "       'mailbox/<agent_id>', '{}'::jsonb, " +
                "       CASE WHEN i <= " + PAST_THRESHOLD + " THEN now() - interval '1 hour' " +
                "            ELSE now() + interval '30 days' END, " +
                "       now() - (i || ' seconds')::interval " +
                "FROM generate_series(1, " + AVAILABLE_ROWS + ") i");

            // Claimed rows -- the bulk still within lease, a small PAST_THRESHOLD
            // subset already lapsed (arm 1's target). Every row a unique claim_id
            // (the ack/nack claim_id-lookup query's own target).
            st.execute(
                "INSERT INTO nexus.tuples (id, tenant_id, subspace, template, keys, claim_state, claimant, " +
                "       claim_id, lease_until, expires_at, created_at) " +
                "SELECT decode(md5('claimed-' || i), 'hex'), '" + TENANT + "', 'mailbox/planshape-claimed', " +
                "       'mailbox/<agent_id>', '{}'::jsonb, 'claimed', 'worker-' || i, 'claim-' || i, " +
                "       CASE WHEN i <= " + PAST_THRESHOLD + " THEN now() - interval '1 hour' " +
                "            ELSE now() + interval '30 days' END, " +
                "       now() + interval '30 days', now() - (i || ' seconds')::interval " +
                "FROM generate_series(1, " + CLAIMED_ROWS + ") i");

            // Claim-log rows -- the bulk within the log's own retention, a small
            // PAST_THRESHOLD subset past it (arm 3's target).
            st.execute(
                "INSERT INTO nexus.tuple_claim_log (tenant_id, subspace, template, transition, at, expires_at) " +
                "SELECT '" + TENANT + "', 'mailbox/planshape-log', 'mailbox/<agent_id>', 'claim', " +
                "       CASE WHEN i <= " + PAST_THRESHOLD + " THEN now() - interval '200 days' " +
                "            ELSE now() - interval '1 day' END, " +
                "       now() + interval '180 days' " +
                "FROM generate_series(1, " + LOG_ROWS + ") i");

            PgContainerHelper.analyzeTable(su, TUPLES);
            PgContainerHelper.analyzeTable(su, TUPLE_CLAIM_LOG);
        }
    }

    private String explain(java.util.function.Function<org.jooq.DSLContext, org.jooq.Query> queryBuilder) {
        return tenantScope.withTenant(TENANT, ctx -> {
            String sql = queryBuilder.apply(ctx).getSQL(org.jooq.conf.ParamType.INLINED);
            StringBuilder sb = new StringBuilder();
            for (var r : ctx.resultQuery("EXPLAIN " + sql).fetch()) {
                sb.append(r.get(0, String.class)).append('\n');
            }
            return sb.toString();
        });
    }

    // ── Arm 1: release lapsed claims — idx_tuples_claim_lease ───────────────

    @Test
    void releaseLapsedClaimsQuery_usesClaimLeaseIndex_noSeqScan() {
        String plan = explain(ctx -> ctx.selectFrom(TUPLES)
                .where(TUPLES.TENANT_ID.eq(TENANT)
                        .and(TUPLES.CLAIM_STATE.eq("claimed"))
                        .and(TUPLES.CONSUMED_AT.isNull())
                        .and(TUPLES.LEASE_UNTIL.lt(DSL.currentOffsetDateTime())))
                .orderBy(TUPLES.LEASE_UNTIL.asc(), TUPLES.ID.asc())
                .limit(300)
                .forNoKeyUpdate()
                .skipLocked());

        assertThat(plan)
            .as("the sweep's release-arm scan must use idx_tuples_claim_lease at seeded cardinality")
            .contains("idx_tuples_claim_lease");
        assertThat(plan).as("must not degrade to a sequential scan").doesNotContain("Seq Scan");
    }

    // ── ack/nack claim_id lookup — idx_tuples_claim_id ───────────────────────

    @Test
    void liveClaimRowLookup_usesClaimIdIndex_noSeqScan() {
        String plan = explain(ctx -> ctx.selectFrom(TUPLES)
                .where(TUPLES.TENANT_ID.eq(TENANT)
                        .and(TUPLES.CLAIM_ID.eq("claim-4321"))
                        .and(TUPLES.CLAIM_STATE.eq("claimed"))
                        .and(TUPLES.CONSUMED_AT.isNull())
                        .and(TUPLES.LEASE_UNTIL.gt(DSL.currentOffsetDateTime()))));

        assertThat(plan)
            .as("ack/nack's claim_id lookup must use idx_tuples_claim_id at seeded cardinality")
            .contains("idx_tuples_claim_id");
        assertThat(plan).as("must not degrade to a sequential scan").doesNotContain("Seq Scan");
    }

    // ── Arm 2: purge expired tuples — idx_tuples_expires_at ──────────────────

    @Test
    void purgeExpiredTuplesQuery_usesExpiresAtIndex_noSeqScan() {
        String plan = explain(ctx -> ctx.select(TUPLES.ID)
                .from(TUPLES)
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.EXPIRES_AT.le(DSL.currentOffsetDateTime())))
                .orderBy(TUPLES.CREATED_AT.asc(), TUPLES.ID.asc())
                .limit(300)
                .forUpdate()
                .skipLocked());

        assertThat(plan)
            .as("the sweep's purge-tuples arm must use idx_tuples_expires_at at seeded cardinality")
            .contains("idx_tuples_expires_at");
        assertThat(plan).as("must not degrade to a sequential scan").doesNotContain("Seq Scan");
    }

    // ── Arm 3: purge old claim-log rows — idx_tuple_claim_log_tenant_at ──────

    @Test
    void purgeOldClaimLogQuery_usesTenantAtIndex_noSeqScan() {
        OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusDays(180);
        String plan = explain(ctx -> ctx.select(TUPLE_CLAIM_LOG.LOG_ID)
                .from(TUPLE_CLAIM_LOG)
                .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(TENANT).and(TUPLE_CLAIM_LOG.AT.lt(cutoff)))
                .orderBy(TUPLE_CLAIM_LOG.LOG_ID.asc())
                .limit(300)
                .forUpdate()
                .skipLocked());

        assertThat(plan)
            .as("the sweep's purge-log arm must use idx_tuple_claim_log_tenant_at at seeded cardinality")
            .contains("idx_tuple_claim_log_tenant_at");
        assertThat(plan).as("must not degrade to a sequential scan").doesNotContain("Seq Scan");
    }

    @Test
    void seededCardinalityIsReal() throws Exception {
        try (Connection su = pg.createConnection("");
             var rs = su.createStatement().executeQuery(
                "SELECT (SELECT count(*) FROM nexus.tuples WHERE tenant_id = '" + TENANT + "'), " +
                "       (SELECT count(*) FROM nexus.tuple_claim_log WHERE tenant_id = '" + TENANT + "')")) {
            rs.next();
            assertThat(rs.getLong(1))
                .as("the plan-shape claim is only meaningful at cardinality")
                .isEqualTo((long) AVAILABLE_ROWS + CLAIMED_ROWS);
            assertThat(rs.getLong(2)).isEqualTo((long) LOG_ROWS);
        }
    }
}
