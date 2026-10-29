// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.io.TempDir;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-5r2sc.2.3 (RDR-235 E4, second half): {@code nack} with {@code dead: true}
 * dead-letters the claim at once. The attempt is counted, the claim log shows {@code nack}
 * then {@code dead} (the rows the {@code max_attempts} path writes), the row counts under
 * {@code dead} in {@code subspace_stats}, and a nack without the flag is unchanged.
 * Ownership and liveness are checked before the flag is looked at.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleNackDeadTest {

    private static final String TENANT = "tuple-tenant-nack-dead";
    private static final String SVC_ROLE = "svc_tuple_nack_dead_test";
    private static final String SVC_PASS = "svc_tuple_nack_dead_test_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TupleRepository repo;

    @BeforeAll
    void startAll(@TempDir Path extraTemplateDir) throws Exception {
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
        // A take-enabled template with NO max_attempts: only the flag can dead-letter its rows.
        Files.writeString(extraTemplateDir.resolve("probe-nomax.yaml"), """
                name: probe-nomax/<room>
                keys:
                  - id
                id_from: keys+nonce
                take:
                  enabled: true
                  max_lease_seconds: 300
                retention_seconds: 3600
                """, StandardCharsets.UTF_8);
        repo = new TupleRepository(new TenantScope(svcDs),
                TemplateRegistry.loadAtBoot(extraTemplateDir.toString(), null,
                        NexusService.SWEEP_INTERVAL_HOURS * 3600L));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    /** queue/<name>: retention_seconds 172800, take.max_attempts 3 (and a required "from" dim). */
    private record Q(String subspace, Map<String, String> pattern, Map<String, String> dims) {
    }

    private static Q newQueue() {
        String name = "nd-" + UUID.randomUUID().toString().substring(0, 10);
        return new Q("queue/" + name, Map.of("queue", name), Map.of("from", "producer"));
    }

    private static Q newNoMaxQueue() {
        String room = "nd-" + UUID.randomUUID().toString().substring(0, 10);
        return new Q("probe-nomax/" + room, Map.of("id", room), Map.of());
    }

    private byte[] out(Q q) {
        return repo.out(TENANT, q.subspace(), q.pattern(), q.dims(), "task", "n-1", null, null, null);
    }

    private TupleRepository.ClaimedTuple claimOrFail(Q q, String claimant) {
        var c = repo.inp(TENANT, q.subspace(), q.pattern(), claimant, 60);
        assertThat(c).as("the row must be claimable").isPresent();
        return c.get();
    }

    private <T> T admin(java.util.function.Function<DSLContext, T> f) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return f.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private record State(String claimState, OffsetDateTime availableAt, int attempts, String claimId,
                         OffsetDateTime consumedAt) {
    }

    private State state(Q q) throws Exception {
        return admin(ctx -> {
            var r = ctx.select(TUPLES.CLAIM_STATE, TUPLES.AVAILABLE_AT, TUPLES.ATTEMPTS, TUPLES.CLAIM_ID,
                            TUPLES.CONSUMED_AT)
                    .from(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(q.subspace())))
                    .fetchOne();
            assertThat(r).isNotNull();
            return new State(r.value1(), r.value2(), r.value3(), r.value4(), r.value5());
        });
    }

    private List<String> transitions(byte[] id) throws Exception {
        return admin(ctx -> ctx.select(TUPLE_CLAIM_LOG.TRANSITION).from(TUPLE_CLAIM_LOG)
                .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(TENANT).and(TUPLE_CLAIM_LOG.TUPLE_ID.eq(id)))
                .orderBy(TUPLE_CLAIM_LOG.LOG_ID.asc())
                .fetch(TUPLE_CLAIM_LOG.TRANSITION));
    }

    // ── scenario: a worker nacks a claim with dead: true ─────────────────────

    @Test
    void nackDeadTrue_deadLettersAtOnce_countsTheAttempt_andLogsNackThenDead() throws Exception {
        Q q = newQueue();
        byte[] id = out(q);
        var c = claimOrFail(q, "w1");

        repo.nack(TENANT, c.claimId(), "w1", null, true);

        State s = state(q);
        assertThat(s.claimState()).as("dead at the FIRST attempt, though max_attempts is 3").isEqualTo("dead");
        assertThat(s.attempts()).as("the attempt is counted").isEqualTo(1);
        assertThat(s.claimId()).as("the claim is cleared").isNull();
        assertThat(s.consumedAt()).as("dead is not consumed").isNull();
        assertThat(transitions(id)).as("the rows the max_attempts path writes")
                .containsExactly("claim", "nack", "dead");
        assertThat(repo.inp(TENANT, q.subspace(), q.pattern(), "w2", 60)).as("a dead row is not claimable").isEmpty();
    }

    @Test
    void aDeadLetteredByFlagRow_countsUnderDeadInSubspaceStats() {
        Q q = newQueue();
        out(q);
        var c = claimOrFail(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1", null, true);

        var census = repo.subspaceStats(TENANT, q.subspace());
        assertThat(census.dead()).isEqualTo(1);
        assertThat(census.available()).isZero();
        assertThat(census.claimed()).isZero();
    }

    @Test
    void nackWithoutTheFlag_behavesAsBefore() throws Exception {
        Q q = newQueue();
        byte[] id = out(q);
        var c = claimOrFail(q, "w1");

        repo.nack(TENANT, c.claimId(), "w1", null, false);

        State s = state(q);
        assertThat(s.claimState()).as("released to available, not dead").isNull();
        assertThat(s.attempts()).isEqualTo(1);
        assertThat(transitions(id)).containsExactly("claim", "nack");
        assertThat(repo.inp(TENANT, q.subspace(), q.pattern(), "w2", 60)).as("claimable again").isPresent();
    }

    @Test
    void theFourArgumentNack_isStillAPlainNack() throws Exception {
        Q q = newQueue();
        out(q);
        var c = claimOrFail(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1", null);
        assertThat(state(q).claimState()).isNull();
    }

    @Test
    void deadTrue_deadLetters_underATemplateWithNoMaxAttempts() throws Exception {
        Q q = newNoMaxQueue();
        byte[] id = out(q);
        var c = claimOrFail(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1", null, true);
        assertThat(state(q).claimState()).isEqualTo("dead");
        assertThat(transitions(id)).containsExactly("claim", "nack", "dead");
    }

    @Test
    void deadTrueWithRetryAfter_theDeadLetterWins_andAvailableAtStaysNull() throws Exception {
        Q q = newQueue();
        out(q);
        var c = claimOrFail(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1", 60L, true);
        State s = state(q);
        assertThat(s.claimState()).isEqualTo("dead");
        assertThat(s.availableAt()).as("a dead row is never retried").isNull();
    }

    // ── ownership and liveness come first ────────────────────────────────────

    @Test
    void deadTrueFromTheWrongClaimant_isRefused_andTheClaimIsUntouched() throws Exception {
        Q q = newQueue();
        byte[] id = out(q);
        var c = claimOrFail(q, "w1");
        assertThatThrownBy(() -> repo.nack(TENANT, c.claimId(), "intruder", null, true))
                .isInstanceOf(ClaimOwnershipException.class);
        State s = state(q);
        assertThat(s.claimState()).isEqualTo("claimed");
        assertThat(s.attempts()).isZero();
        assertThat(transitions(id)).containsExactly("claim");
        repo.ack(TENANT, c.claimId(), "w1");   // still live for its holder
    }

    @Test
    void deadTrueOnAClaimThatIsNoLongerLive_isClaimNotFound_andLogsNothing() throws Exception {
        Q q = newQueue();
        byte[] id = out(q);
        var c = claimOrFail(q, "w1");
        repo.ack(TENANT, c.claimId(), "w1");
        assertThatThrownBy(() -> repo.nack(TENANT, c.claimId(), "w1", null, true))
                .isInstanceOf(ClaimNotFoundException.class);
        assertThat(transitions(id)).containsExactly("claim", "ack");
    }

    // ── release never takes the forced path ──────────────────────────────────

    @Test
    void release_neverDeadLetters_whateverTheAttemptCount() throws Exception {
        Q q = newQueue();
        out(q);
        var c = claimOrFail(q, "w1");
        admin(ctx -> ctx.update(TUPLES).set(TUPLES.ATTEMPTS, 5)
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(q.subspace()))).execute());
        repo.release(TENANT, c.claimId(), "w1");
        assertThat(state(q).claimState()).as("a release is not a failure").isNull();
    }
}
