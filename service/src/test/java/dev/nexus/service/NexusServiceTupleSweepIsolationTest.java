// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.security.MessageDigest;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.UUID;

import static dev.nexus.service.NexusService.TupleSweepIncompleteCause.TENANT_ERROR;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_TENANTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Round-2 verification (CRE pass 2, 2026-09-13) on the fix-round's sweep-arm
 * isolation ({@link NexusService#runScheduledTupleSweep}'s three per-arm
 * try/catch blocks plus {@link NexusService#worseTupleSweepCause}): no test in
 * the tree forced a GENUINE exception in one arm and asserted the other two
 * still ran in the same tick -- the only prior coverage exercised the pre-
 * existing per-arm batch-share CAP (nexus-em75s.34), a different code path.
 *
 * <p>Unlike {@link NexusServiceTupleSweepTest} (which runs {@code service}
 * over the container's own superuser, so there is no narrower role to revoke
 * a privilege from), this class bootstraps a real, restricted service role
 * exactly the way {@code TupleRepositoryTest}/{@code TupleAckWithReplyTest}
 * do, so a REAL SQL error (a REVOKE'd privilege, the same "genuine failure,
 * not a fabricated Java exception" standard {@code CatalogManifestSweepRepositoryTest#
 * writeManifestMany_sweepTrue_deletePermissionDenied_failsOpen_...} sets) can
 * be forced against exactly one arm.
 *
 * <p>Getting a failure that is ARM-1-SPECIFIC took two attempts (both round-2
 * verification, this file's own history): {@code REVOKE UPDATE ON nexus.tuples}
 * looked like the obvious choice, since only {@link dev.nexus.service.db.
 * TupleRepository#releaseLapsedClaimsBatch} (arm 1) writes to {@code
 * nexus.tuples}'s claim columns -- but {@link dev.nexus.service.db.
 * TupleRepository#purgeExpiredTuplesBatch} (arm 2) ALSO opens with a {@code
 * SELECT ... FOR UPDATE} against the SAME table (to lock candidate rows before
 * its DELETE), so revoking table-level UPDATE breaks BOTH arms identically --
 * confirmed empirically: arm 2's own row went unpurged under that revoke, so
 * the "other arms still ran" half of this test was unfalsifiable with it. The
 * two arms diverge on WHICH COLUMNS their locking SELECT reads: arm 1's
 * {@code ctx.selectFrom(TUPLES)} is a full-row {@code SELECT *} (needs {@code
 * body} among everything else), arm 2's {@code ctx.select(TUPLES.ID)} reads
 * only {@code id}. Revoking table-level SELECT and re-granting every column
 * EXCEPT {@code body} exploits exactly that asymmetry: arm 1's full-row read
 * fails with a genuine "permission denied for column body" before its
 * per-row loop (and per-row savepoint) ever starts, while arm 2's one-column
 * read is untouched. Arm 3 ({@code nexus.tuple_claim_log}, a different table
 * entirely) is unaffected either way.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceTupleSweepIsolationTest {

    private static final String TOKEN = "tuple-sweep-iso-test-token-9d4f1a";
    private static final String SVC_ROLE = "svc_tuple_sweep_iso_test";
    private static final String SVC_PASS = "svc_tuple_sweep_iso_test_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TemplateRegistry registry;
    NexusService service;
    Connection suConn;
    DSLContext su;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(10);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        service = new NexusService(0, TOKEN, svcDs, null, null, null, null, registry);

        // One superuser connection (bypasses RLS and the REVOKE below), reused for
        // every raw-SQL seed/read helper -- autoCommit=true, so each call is its own
        // statement, immediately visible to every other connection.
        suConn = pg.createConnection("");
        su = DSL.using(suConn, SQLDialect.POSTGRES);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) {
            try {
                service.stop();
            } catch (Exception ignored) {
                // never started; nothing to tear down
            }
        }
        if (suConn != null) suConn.close();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static byte[] fakeId(String label) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(label.getBytes(StandardCharsets.UTF_8));
    }

    private void insertTuple(byte[] id, String tenant, String subspace, String claimState, String claimant,
                              String claimId, OffsetDateTime leaseUntil, int attempts,
                              OffsetDateTime consumedAt, OffsetDateTime expiresAt, OffsetDateTime createdAt) {
        su.insertInto(TUPLES,
                        TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE, TUPLES.KEYS,
                        TUPLES.CLAIM_STATE, TUPLES.CLAIMANT, TUPLES.CLAIM_ID, TUPLES.LEASE_UNTIL,
                        TUPLES.ATTEMPTS, TUPLES.CONSUMED_AT, TUPLES.CONSUMED_BY, TUPLES.EXPIRES_AT,
                        TUPLES.CREATED_AT)
                .values(id, tenant, subspace, "mailbox", JSONB.valueOf("{}"),
                        claimState, claimant, claimId, leaseUntil,
                        attempts, consumedAt, consumedAt == null ? null : "sweep-iso-test-consumer",
                        expiresAt, createdAt)
                .execute();
    }

    private void insertClaimLogRow(String tenant, String subspace, byte[] tupleId, String transition,
                                    OffsetDateTime at) {
        su.insertInto(TUPLE_CLAIM_LOG,
                        TUPLE_CLAIM_LOG.TENANT_ID, TUPLE_CLAIM_LOG.SUBSPACE, TUPLE_CLAIM_LOG.TEMPLATE,
                        TUPLE_CLAIM_LOG.TUPLE_ID, TUPLE_CLAIM_LOG.TRANSITION, TUPLE_CLAIM_LOG.AT,
                        TUPLE_CLAIM_LOG.EXPIRES_AT)
                .values(tenant, subspace, "mailbox", tupleId, transition, at,
                        at.plusSeconds(registry.claimLogTtlSeconds()))
                .execute();
    }

    private void insertTupleTenant(String tenant, OffsetDateTime firstSeen, OffsetDateTime lastSeen,
                                    OffsetDateTime lastSweptAt) {
        su.insertInto(TUPLE_TENANTS,
                        TUPLE_TENANTS.TENANT_ID, TUPLE_TENANTS.FIRST_SEEN, TUPLE_TENANTS.LAST_SEEN,
                        TUPLE_TENANTS.LAST_SWEPT_AT)
                .values(tenant, firstSeen, lastSeen, lastSweptAt)
                .execute();
    }

    private OffsetDateTime lastSweptAt(String tenant) {
        return su.select(TUPLE_TENANTS.LAST_SWEPT_AT)
                .from(TUPLE_TENANTS)
                .where(TUPLE_TENANTS.TENANT_ID.eq(tenant))
                .fetchOne(TUPLE_TENANTS.LAST_SWEPT_AT);
    }

    private boolean tupleExists(byte[] id) {
        return su.fetchExists(su.selectFrom(TUPLES).where(TUPLES.ID.eq(id)));
    }

    private String claimStateOf(byte[] id) {
        return su.select(TUPLES.CLAIM_STATE).from(TUPLES).where(TUPLES.ID.eq(id)).fetchOne(TUPLES.CLAIM_STATE);
    }

    private boolean claimLogRowExists(String tenant, String subspace) {
        return su.fetchExists(su.selectFrom(TUPLE_CLAIM_LOG)
                .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant).and(TUPLE_CLAIM_LOG.SUBSPACE.eq(subspace))));
    }

    @Test
    void releaseArm_throwsARealDbError_purgeArmsStillRunSameTick_tenantErrorNotStamped() throws Exception {
        String tenant = "sweep-iso-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertTupleTenant(tenant, now.minusDays(1), now.minusDays(1), null);

        // Arm 1 target: a lapsed claim the release arm would otherwise release.
        byte[] lapsed = fakeId(tenant + "-lapsed");
        insertTuple(lapsed, tenant, "mailbox/agent-iso-release", "claimed", "worker-iso", "claim-iso",
                now.minusMinutes(5), 0, null, now.plusDays(1), now.minusHours(1));

        // Arm 2 target: an expired, never-claimed tuple the purge-tuples arm would
        // otherwise purge.
        byte[] expired = fakeId(tenant + "-expired");
        insertTuple(expired, tenant, "mailbox/agent-iso-purge", null, null, null,
                null, 0, null, now.minusMinutes(1), now.minusHours(2));

        // Arm 3 target: an old claim-log row the purge-log arm would otherwise purge.
        insertClaimLogRow(tenant, "mailbox/agent-iso-log", null, "claim", now.minusDays(200));

        // A REAL SQL error, scoped to exactly arm 1's own statement shape: its
        // release query is a full-row SELECT (reads every column, including
        // `body`) taken under FOR NO KEY UPDATE; arm 2's purge query selects only
        // `id`. Revoke table-level SELECT and re-grant every OTHER column, so
        // arm 1's full-row read fails ("permission denied for column body")
        // before its per-row loop even starts, while arm 2's one-column read is
        // untouched -- see the class javadoc for why a bare UPDATE revoke does
        // not isolate the two arms.
        try (Connection admin = pg.createConnection("")) {
            admin.setAutoCommit(true);
            admin.createStatement().execute("REVOKE SELECT ON nexus.tuples FROM " + SVC_ROLE);
            admin.createStatement().execute("GRANT SELECT (id, tenant_id, subspace, template, keys, dims, "
                    + "claim_state, claimant, claim_id, lease_until, attempts, consumed_at, consumed_by, "
                    + "expires_at, created_at) ON nexus.tuples TO " + SVC_ROLE);
        }
        try {
            NexusService.TupleSweepRunResult result = service.runScheduledTupleSweep(
                    now, Duration.ofSeconds(30), 300, 50, Duration.ofMinutes(2));

            assertThat(result.incompleteCause()).isEqualTo(TENANT_ERROR);

            // Arm 1 (release) threw before touching its row: still claimed, exactly
            // as seeded.
            assertThat(claimStateOf(lapsed)).isEqualTo("claimed");

            // Arms 2 and 3 ran regardless, in the SAME tick, and completed their own
            // work despite arm 1's failure.
            assertThat(tupleExists(expired)).isFalse();
            assertThat(claimLogRowExists(tenant, "mailbox/agent-iso-log")).isFalse();

            // Cut short by a real error: never stamped, so this tenant sorts first
            // again next run (RDR-205 Phase 1 review, Sam's ruling, nexus-em75s.7).
            assertThat(lastSweptAt(tenant)).isNull();
        } finally {
            try (Connection admin = pg.createConnection("")) {
                admin.setAutoCommit(true);
                admin.createStatement().execute("GRANT SELECT ON nexus.tuples TO " + SVC_ROLE);
            }
        }

        // A LATER run, with the privilege restored, finishes arm 1 too and stamps
        // the tenant -- the failure was transient and bounded, not a permanent wedge.
        NexusService.TupleSweepRunResult recovered = service.runScheduledTupleSweep(
                OffsetDateTime.now(ZoneOffset.UTC), Duration.ofSeconds(30), 300, 50, Duration.ofMinutes(2));
        assertThat(recovered.incompleteCause()).isEqualTo(NexusService.TupleSweepIncompleteCause.NONE);
        assertThat(claimStateOf(lapsed)).isNull();
        assertThat(lastSweptAt(tenant)).isNotNull();
    }
}
