// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TupleRepository;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Query;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.jooq.types.DayToSecond;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.Map;
import java.util.function.Function;

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
 * methodology: EXPLAIN of the SHIPPED query, the very {@code Query} the
 * repository builds through its static factories ({@code
 * TupleRepository#releaseLapsedClaimsQuery} and its siblings; nexus-cbo4a: the
 * test no longer carries a twin of them) through the real {@code nexus_svc}/FORCE-RLS path, asserting the expected
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
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Field<OffsetDateTime> now = DSL.currentOffsetDateTime();

            // Available (unclaimed) rows -- the bulk far from expiry, a small
            // PAST_THRESHOLD subset already expired (arm 2's target).
            var avail = DSL.generateSeries(1, AVAILABLE_ROWS).as("s", "i");
            Field<Integer> availI = avail.field("i", Integer.class);
            ctx.insertInto(TUPLES, TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE, TUPLES.KEYS,
                    TUPLES.EXPIRES_AT, TUPLES.CREATED_AT)
                .select(ctx.select(
                        idOf("avail-", availI),
                        DSL.val(TENANT, TUPLES.TENANT_ID),
                        DSL.val("mailbox/planshape-avail", TUPLES.SUBSPACE),
                        DSL.val("mailbox/<agent_id>", TUPLES.TEMPLATE),
                        DSL.val(JSONB.valueOf("{}"), TUPLES.KEYS),
                        pastOrAhead(availI, now, Duration.ofDays(30)),
                        secondsAgo(now, availI))
                    .from(avail))
                .execute();

            // Claimed rows -- the bulk still within lease, a small PAST_THRESHOLD
            // subset already lapsed (arm 1's target). Every row a unique claim_id
            // (the ack/nack claim_id-lookup query's own target).
            var claimed = DSL.generateSeries(1, CLAIMED_ROWS).as("s", "i");
            Field<Integer> claimedI = claimed.field("i", Integer.class);
            ctx.insertInto(TUPLES, TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE, TUPLES.KEYS,
                    TUPLES.CLAIM_STATE, TUPLES.CLAIMANT, TUPLES.CLAIM_ID, TUPLES.LEASE_UNTIL, TUPLES.EXPIRES_AT,
                    TUPLES.CREATED_AT)
                .select(ctx.select(
                        idOf("claimed-", claimedI),
                        DSL.val(TENANT, TUPLES.TENANT_ID),
                        DSL.val("mailbox/planshape-claimed", TUPLES.SUBSPACE),
                        DSL.val("mailbox/<agent_id>", TUPLES.TEMPLATE),
                        DSL.val(JSONB.valueOf("{}"), TUPLES.KEYS),
                        DSL.val("claimed", TUPLES.CLAIM_STATE),
                        DSL.concat(DSL.inline("worker-"), asText(claimedI)),
                        DSL.concat(DSL.inline("claim-"), asText(claimedI)),
                        pastOrAhead(claimedI, now, Duration.ofDays(30)),
                        now.add(DayToSecond.valueOf(Duration.ofDays(30))),
                        secondsAgo(now, claimedI))
                    .from(claimed))
                .execute();

            // Claim-log rows -- the bulk within the log's own retention (expires_at
            // in the future), a small PAST_THRESHOLD subset past it (arm 3's
            // target: purgeOldClaimLogBatch filters on expires_at, the log row's
            // OWN retention deadline stamped at write time by insertClaimLog --
            // NOT on `at`, the write timestamp; nexus-xapt8 / tuples-005).
            var log = DSL.generateSeries(1, LOG_ROWS).as("s", "i");
            Field<Integer> logI = log.field("i", Integer.class);
            ctx.insertInto(TUPLE_CLAIM_LOG, TUPLE_CLAIM_LOG.TENANT_ID, TUPLE_CLAIM_LOG.SUBSPACE,
                    TUPLE_CLAIM_LOG.TEMPLATE, TUPLE_CLAIM_LOG.TRANSITION, TUPLE_CLAIM_LOG.AT,
                    TUPLE_CLAIM_LOG.EXPIRES_AT)
                .select(ctx.select(
                        DSL.val(TENANT, TUPLE_CLAIM_LOG.TENANT_ID),
                        DSL.val("mailbox/planshape-log", TUPLE_CLAIM_LOG.SUBSPACE),
                        DSL.val("mailbox/<agent_id>", TUPLE_CLAIM_LOG.TEMPLATE),
                        DSL.val("claim", TUPLE_CLAIM_LOG.TRANSITION),
                        secondsAgo(now, logI),
                        pastOrAhead(logI, now, Duration.ofDays(180)))
                    .from(log))
                .execute();

            PgContainerHelper.analyzeTable(su, TUPLES);
            PgContainerHelper.analyzeTable(su, TUPLE_CLAIM_LOG);
        }
    }

    private static Field<String> asText(Field<Integer> i) {
        return DSL.cast(i, SQLDataType.VARCHAR);
    }

    /** {@code decode(md5(prefix || i), 'hex')}: a 16-byte tuple id, unique per series value. */
    private static Field<byte[]> idOf(String prefix, Field<Integer> i) {
        return DSL.function("decode", SQLDataType.BLOB,
            DSL.md5(DSL.concat(DSL.inline(prefix), asText(i))), DSL.inline("hex"));
    }

    /** One hour ago for the first {@link #PAST_THRESHOLD} series values, {@code ahead} from now for the rest. */
    private static Field<OffsetDateTime> pastOrAhead(Field<Integer> i, Field<OffsetDateTime> now, Duration ahead) {
        return DSL.when(i.le(PAST_THRESHOLD), now.sub(DayToSecond.valueOf(Duration.ofHours(1))))
            .otherwise(now.add(DayToSecond.valueOf(ahead)));
    }

    /** {@code now() - i seconds}: a creation time that orders the series. */
    private static Field<OffsetDateTime> secondsAgo(Field<OffsetDateTime> now, Field<Integer> i) {
        return now.sub(DSL.function("make_interval", SQLDataType.INTERVALDAYTOSECOND,
            DSL.inline(0), DSL.inline(0), DSL.inline(0), DSL.inline(0), DSL.inline(0), DSL.inline(0),
            DSL.cast(i, SQLDataType.DOUBLE)));
    }

    /** The plan of the PRODUCT's own query (a {@link TupleRepository} factory), literals inline as the sweep plans it. */
    private String explain(Function<DSLContext, Query> queryBuilder) {
        return tenantScope.withTenant(TENANT, ctx -> PgContainerHelper.explainPlanInlined(ctx, queryBuilder.apply(ctx)));
    }

    // ── Arm 1: release lapsed claims — idx_tuples_claim_lease ───────────────

    @Test
    void releaseLapsedClaimsQuery_usesClaimLeaseIndex_noSeqScan() {
        String plan = explain(ctx -> TupleRepository.releaseLapsedClaimsQuery(ctx, TENANT, 300));

        assertThat(plan)
            .as("the sweep's release-arm scan must use idx_tuples_claim_lease at seeded cardinality")
            .contains("idx_tuples_claim_lease");
        assertThat(plan).as("must not degrade to a sequential scan").doesNotContain("Seq Scan");
    }

    // ── ack/nack claim_id lookup — idx_tuples_claim_id ───────────────────────

    @Test
    void liveClaimRowLookup_usesClaimIdIndex_noSeqScan() {
        String plan = explain(ctx -> TupleRepository.liveClaimRowQuery(ctx, TENANT, "claim-4321"));

        assertThat(plan)
            .as("ack/nack's claim_id lookup must use idx_tuples_claim_id at seeded cardinality")
            .contains("idx_tuples_claim_id");
        assertThat(plan).as("must not degrade to a sequential scan").doesNotContain("Seq Scan");
    }

    // ── Arm 2: purge expired tuples — idx_tuples_expires_at ──────────────────

    @Test
    void purgeExpiredTuplesQuery_usesExpiresAtIndex_noSeqScan() {
        String plan = explain(ctx -> TupleRepository.purgeExpiredTuplesQuery(ctx, TENANT, 300));

        assertThat(plan)
            .as("the sweep's purge-tuples arm must use idx_tuples_expires_at at seeded cardinality")
            .contains("idx_tuples_expires_at");
        assertThat(plan).as("must not degrade to a sequential scan").doesNotContain("Seq Scan");
    }

    // ── Arm 3: purge old claim-log rows — idx_tuple_claim_log_tenant_expires_at ──

    /**
     * nexus-xapt8 (a scalability research pass over this design): rewritten to match
     * {@code TupleRepository.purgeOldClaimLogBatch}'s ACTUAL filter column,
     * {@code expires_at} (the log row's own retention deadline, stamped at
     * write time by {@code insertClaimLog} — see that method's javadoc), not
     * {@code at} (the write timestamp) this test previously EXPLAINed. The
     * old shape pinned a query the repository has not issued since the
     * nexus-em75s.37 follow-on that moved the purge arm onto {@code
     * expires_at}; {@code idx_tuple_claim_log_tenant_at}, the index that old
     * shape used, is dropped by {@code tuples-005-subspace-and-claim-log-
     * indexes.xml} changeset 3 as dead weight for exactly that reason.
     */
    @Test
    void purgeOldClaimLogQuery_usesTenantExpiresAtIndex_noSeqScan() {
        String plan = explain(ctx -> TupleRepository.purgeOldClaimLogQuery(ctx, TENANT, 300));

        assertThat(plan)
            .as("the sweep's purge-log arm must use idx_tuple_claim_log_tenant_expires_at at seeded cardinality")
            .contains("idx_tuple_claim_log_tenant_expires_at");
        assertThat(plan).as("must not degrade to a sequential scan").doesNotContain("Seq Scan");
    }

    // ── rd/rdp read scan — idx_tuples_subspace_scan (nexus-xapt8) ────────────

    /**
     * nexus-xapt8 (a scalability research pass over this design): {@code
     * TupleRepository.queryOnce} (rd/rdp's read path) needs every row for a
     * (tenant, subspace) with {@code consumed_at IS NULL AND expires_at >
     * now()}, ordered by {@code (created_at, id)} — a shape neither existing
     * partial index on {@code nexus.tuples} can serve (both exclude a
     * claim_state/consumed_at subset this query needs in full). The
     * "available" pool seeded above already mixes live and
     * already-expired rows (the {@code PAST_THRESHOLD} subset) in the SAME
     * subspace, so this EXPLAIN is genuinely selective.
     */
    @Test
    void rdQuery_usesSubspaceScanIndex_noSeqScan() {
        String plan = explain(ctx -> TupleRepository.readQuery(ctx,
                TupleRepository.readCondition(TENANT, "mailbox/planshape-avail", Map.of()),
                TupleRepository.ReadOrder.ASC, 300));

        assertThat(plan)
            .as("rd/rdp's read scan must use idx_tuples_subspace_scan at seeded cardinality")
            .contains("idx_tuples_subspace_scan");
        assertThat(plan).as("must not degrade to a sequential scan").doesNotContain("Seq Scan");
    }

    // ── GROUP BY census — idx_tuples_subspace_scan does NOT cover this shape ──

    /**
     * Critique finding (nexus-xapt8 fix round): the changelog comment and
     * {@code subspaceListPage}'s own javadoc both asserted this index serves
     * the census query too, but the only EXPLAIN this file carried was the
     * NARROWER {@code rd}/{@code rdp} shape above (subspace EQUALITY plus a
     * {@code LIMIT 300}). {@code subspaceListPage}'s UNFILTERED form -- no
     * subspace predicate at all, exactly the shape {@code health.py}'s
     * {@code tuples.oldest_unclaimed} doctor row issues via {@code
     * subspace_list(prefix=None)} -- is a materially different query: it
     * must aggregate EVERY row for the tenant, and none of the {@code GROUP
     * BY}'s aggregated columns ({@code claim_state}, {@code consumed_at},
     * {@code expires_at}) are covered by the index, so an index scan would
     * still need a heap fetch per row.
     *
     * <p>MEASURED (this arm, this seeded fixture -- 10,000 rows across 2
     * subspaces for this tenant): Postgres chooses {@code Seq Scan on
     * tuples} -> {@code HashAggregate} -> {@code Sort}, NOT {@code
     * idx_tuples_subspace_scan}. This confirms the critique's prediction
     * exactly: the index does not help this query shape, because there is
     * no subspace equality/prefix predicate for it to seek on, and the
     * planner reasonably prefers a full scan + hash aggregate over an
     * index scan that still pays a heap fetch per row. The changelog
     * header and {@code subspaceListPage}'s javadoc are corrected in the
     * same fix round to say so, rather than implying coverage this arm
     * disproves. This is asserted as a POSITIVE fact (Seq Scan IS chosen),
     * not left open-ended, so a future planner-version or cardinality
     * change that flips the choice is visible here as a FAILURE demanding
     * the prose be re-checked, rather than silently drifting from what the
     * docs claim.
     */
    @Test
    void unfilteredGroupByCensusQuery_doesNotUseTheSubspaceScanIndex_seqScanInstead() {
        String plan = explain(ctx -> TupleRepository.subspaceCensusQuery(ctx, TENANT, null, null));

        assertThat(plan)
                .as("measured fact, not an assumption: the unfiltered census query does NOT use "
                        + "idx_tuples_subspace_scan at this cardinality -- no subspace predicate for it to "
                        + "seek on, so a heap-fetch-per-row index scan loses to a full scan + hash aggregate")
                .contains("Seq Scan on tuples")
                .doesNotContain("idx_tuples_subspace_scan");
    }

    @Test
    void seededCardinalityIsReal() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchCount(TUPLES, TUPLES.TENANT_ID.eq(TENANT)))
                .as("the plan-shape claim is only meaningful at cardinality")
                .isEqualTo(AVAILABLE_ROWS + CLAIMED_ROWS);
            assertThat(ctx.fetchCount(TUPLE_CLAIM_LOG, TUPLE_CLAIM_LOG.TENANT_ID.eq(TENANT))).isEqualTo(LOG_ROWS);
        }
    }
}
