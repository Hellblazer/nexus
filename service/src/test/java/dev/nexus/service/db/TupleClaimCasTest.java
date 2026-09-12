// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-206 Phase 1 Step 1 (bead nexus-h61dl.2): the compare-and-swap on {@code ack}
 * and {@code nack}. {@link TupleRepository#liveClaimRow} reads without a lock, and
 * before this step the two updates matched on {@code id} alone, so an {@code ack} or
 * {@code nack} that lost the race with the sweep's release arm wrote over a row the
 * sweep had just released. The race is built deterministically, not with a sleep:
 * {@link TupleRepository#TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY} runs inside the
 * ack/nack transaction between the claim read and the update, and the hook installed
 * here lapses the lease and runs the REAL sweep batch in that window. Package-local
 * to {@code dev.nexus.service.db} to reach the package-private seam, the same shape
 * as {@link TupleClaimContentionTest}.
 *
 * <p><b>Weakening experiment (performed once while authoring, then reverted):</b>
 * with {@code liveClaimCondition} reduced to {@code id = ?} alone, both race cases
 * failed ("Expecting code to raise a throwable"): the stale ack consumed the released
 * row and the stale nack released it a second time. The committed diff never carried
 * the weakening.
 *
 * <p><b>Vacuity guard:</b> every race case asserts the hook ran exactly once and
 * that the sweep it ran actually released the row (its own {@code released} count),
 * so a run where the window never opened, or the sweep found nothing, fails rather
 * than passing on an unraced path.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleClaimCasTest {

    private static final String SVC_ROLE = "svc_tuple_cas_test";
    private static final String SVC_PASS = "svc_tuple_cas_test_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope tenantScope;
    TemplateRegistry registry;
    TupleRepository repo;

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
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        repo = new TupleRepository(tenantScope, registry);
    }

    @AfterEach
    void restoreSeam() {
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };
    }

    @AfterAll
    void stopAll() {
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private record Seeded(String tenant, String subspace, String to, byte[] id, String claimId) {
    }

    private Seeded outAndClaim(String label, String claimant) {
        String tenant = "tuple-cas-" + label + "-" + UUID.randomUUID();
        String to = "agent-cas-" + label + "-" + UUID.randomUUID();
        String subspace = "mailbox/" + to;
        byte[] id = repo.out(tenant, subspace, Map.of("to", to), Map.of("from", "sender-cas"),
                "body", "nonce-cas-" + label, null);
        var claimed = repo.inp(tenant, subspace, Map.of("to", to), claimant, 60);
        assertThat(claimed).isPresent();
        return new Seeded(tenant, subspace, to, id, claimed.get().claimId());
    }

    /** Lapse the row's lease directly (the repo's API only ever writes "now"). */
    private void lapseLease(byte[] id) {
        try (Connection su = pg.createConnection("")) {
            org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .update(TUPLES)
                    .set(TUPLES.LEASE_UNTIL, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(5))
                    .where(TUPLES.ID.eq(id))
                    .execute();
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private List<String> transitionsFor(String tenant, byte[] id) {
        try (Connection su = pg.createConnection("")) {
            return org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .select(TUPLE_CLAIM_LOG.TRANSITION)
                    .from(TUPLE_CLAIM_LOG)
                    .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant).and(TUPLE_CLAIM_LOG.TUPLE_ID.eq(id)))
                    .orderBy(TUPLE_CLAIM_LOG.LOG_ID.asc())
                    .fetch(TUPLE_CLAIM_LOG.TRANSITION);
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private dev.nexus.service.jooq.nexus.tables.records.TuplesRecord rawRow(byte[] id) {
        try (Connection su = pg.createConnection("")) {
            return org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .selectFrom(TUPLES)
                    .where(TUPLES.ID.eq(id))
                    .fetchOne();
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * Installs the race: in the ack/nack window, lapse the lease and run one real
     * sweep release batch against the tenant, capturing its result.
     */
    private AtomicInteger installSweepInWindow(Seeded s, AtomicReference<TupleRepository.ReleaseBatchResult> sweep) {
        AtomicInteger hookRuns = new AtomicInteger();
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> {
            hookRuns.incrementAndGet();
            lapseLease(s.id());
            sweep.set(repo.releaseLapsedClaimsBatch(s.tenant(), 300, null));
        };
        return hookRuns;
    }

    // ── ack loses the race with the sweep ─────────────────────────────────────

    @Test
    void ack_sweepReleasedBetweenReadAndUpdate_claimNotFound_noAckLogRow_rowStaysReleased() {
        Seeded s = outAndClaim("ack", "holder-ack");
        var sweep = new AtomicReference<TupleRepository.ReleaseBatchResult>();
        AtomicInteger hookRuns = installSweepInWindow(s, sweep);

        assertThatThrownBy(() -> repo.ack(s.tenant(), s.claimId(), "holder-ack"))
                .isInstanceOf(ClaimNotFoundException.class);

        assertThat(hookRuns.get()).as("the race window opened exactly once").isEqualTo(1);
        assertThat(sweep.get().released()).as("the sweep released the row inside the window").isEqualTo(1);

        var row = rawRow(s.id());
        assertThat(row.get(TUPLES.CONSUMED_AT)).as("the stale ack did not consume the released row").isNull();
        assertThat(row.get(TUPLES.CONSUMED_BY)).isNull();
        assertThat(row.get(TUPLES.CLAIM_STATE)).as("the sweep's release stands").isNull();
        assertThat(row.get(TUPLES.CLAIM_ID)).isNull();
        assertThat(row.get(TUPLES.ATTEMPTS)).as("one attempt, counted by the sweep's expire").isEqualTo(1);

        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("claim, then the sweep's expire; no ack row for the lost race")
                .containsExactly("claim", "expire");

        // The released row is available again: a fresh claimant takes it.
        var again = repo.inp(s.tenant(), s.subspace(), Map.of("to", s.to()), "next-claimant", 60);
        assertThat(again).isPresent();
        assertThat(again.get().claimId()).isNotEqualTo(s.claimId());
    }

    // ── nack loses the race with the sweep ────────────────────────────────────

    @Test
    void nack_sweepReleasedBetweenReadAndUpdate_claimNotFound_noNackLogRow_attemptsCountedOnce() {
        Seeded s = outAndClaim("nack", "holder-nack");
        var sweep = new AtomicReference<TupleRepository.ReleaseBatchResult>();
        AtomicInteger hookRuns = installSweepInWindow(s, sweep);

        assertThatThrownBy(() -> repo.nack(s.tenant(), s.claimId(), "holder-nack"))
                .isInstanceOf(ClaimNotFoundException.class);

        assertThat(hookRuns.get()).isEqualTo(1);
        assertThat(sweep.get().released()).isEqualTo(1);

        var row = rawRow(s.id());
        assertThat(row.get(TUPLES.CLAIM_STATE)).isNull();
        assertThat(row.get(TUPLES.CLAIM_ID)).isNull();
        assertThat(row.get(TUPLES.ATTEMPTS))
                .as("the sweep's expire counted one attempt; the stale nack did not count a second")
                .isEqualTo(1);

        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("no nack row for the lost race")
                .containsExactly("claim", "expire");
    }

    // ── the happy paths still write exactly their log rows ───────────────────

    @Test
    void ack_withNoRace_consumesAndLogsOnce() {
        Seeded s = outAndClaim("ack-plain", "holder-plain");
        repo.ack(s.tenant(), s.claimId(), "holder-plain");
        var row = rawRow(s.id());
        assertThat(row.get(TUPLES.CONSUMED_AT)).isNotNull();
        assertThat(row.get(TUPLES.CONSUMED_BY)).isEqualTo("holder-plain");
        assertThat(transitionsFor(s.tenant(), s.id())).containsExactly("claim", "ack");
    }

    @Test
    void nack_withNoRace_releasesAndLogsOnce() {
        Seeded s = outAndClaim("nack-plain", "holder-plain");
        repo.nack(s.tenant(), s.claimId(), "holder-plain");
        var row = rawRow(s.id());
        assertThat(row.get(TUPLES.CLAIM_STATE)).isNull();
        assertThat(row.get(TUPLES.ATTEMPTS)).isEqualTo(1);
        assertThat(transitionsFor(s.tenant(), s.id())).containsExactly("claim", "nack");
    }

    // ── the sweep's own counts are unchanged under the condition ─────────────

    @Test
    void sweep_releaseArm_stillReleasesEveryLapsedRowItSelects() {
        String tenant = "tuple-cas-sweep-" + UUID.randomUUID();
        int n = 3;
        byte[][] ids = new byte[n][];
        for (int i = 0; i < n; i++) {
            String to = "agent-cas-sweep-" + i + "-" + UUID.randomUUID();
            ids[i] = repo.out(tenant, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender-cas"),
                    "body", "nonce-cas-sweep-" + i, null);
            var claimed = repo.inp(tenant, "mailbox/" + to, Map.of("to", to), "holder-" + i, 60);
            assertThat(claimed).isPresent();
            lapseLease(ids[i]);
        }

        var result = repo.releaseLapsedClaimsBatch(tenant, 300, null);
        assertThat(result.scanned()).isEqualTo(n);
        assertThat(result.released()).isEqualTo(n);
        assertThat(result.deadLettered()).isZero();
        for (byte[] id : ids) {
            assertThat(transitionsFor(tenant, id)).containsExactly("claim", "expire");
            assertThat(rawRow(id).get(TUPLES.CLAIM_STATE)).isNull();
        }
    }
}
