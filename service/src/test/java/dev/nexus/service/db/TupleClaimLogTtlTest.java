// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
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
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-211 Phase 1 Step 1 (bead nexus-rplay.6) — the per-template claim-log
 * TTL (Scale and Limits item 6, "claim-log volume").
 *
 * <p>Two test-only templates, layered on the bundled resources the same way
 * {@code TupleRepositoryTest}'s {@code probe/<id>} is: {@code
 * claimlog-short/<room>} declares {@code claim_log_ttl_seconds: 3}, {@code
 * claimlog-default/<room>} declares none. The registry is built with a
 * TINY {@code sweepIntervalSeconds} (1s, via {@link
 * TemplateRegistry#loadAtBoot(String, String, long)}'s testable form) so
 * both templates' {@code retention_seconds} can stay at the schema's own
 * minimum (a positive integer) while still satisfying the boot check's
 * "effective TTL exceeds retention by strictly more than one sweep
 * interval" rule — that boot check itself, both directions, is {@code
 * TemplateRegistryTest}'s territory; this class exercises the RUNTIME
 * behaviour the two fields exist for: a shorter stamped {@code expires_at}
 * at claim-log write time, and an earlier purge as a direct consequence.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleClaimLogTtlTest {

    private static final String SVC_ROLE = "svc_tuple_claimlogttl_test";
    private static final String SVC_PASS = "svc_tuple_claimlogttl_test_pass";
    private static final long SHORT_TTL_SECONDS = 3L;
    /** {@code loadAtBoot(templateDirEnv, null, ...)}'s own {@link
     *  TemplateRegistry#DEFAULT_CLAIM_LOG_TTL_DAYS} (180 days) -- left at the
     *  production default rather than overridden, because the bundled v1
     *  resource templates load alongside the two test-only ones in every
     *  {@code loadAtBoot} call, and {@code ledger}/{@code mailbox}/{@code
     *  directory}'s own {@code retention_seconds} (up to 7 days) need a
     *  registry-wide default at least that generous to pass the boot check;
     *  180 days is also what every production engine actually runs with. */
    private static final long DEFAULT_TTL_SECONDS = TemplateRegistry.DEFAULT_CLAIM_LOG_TTL_DAYS * 86_400L;

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope tenantScope;
    TemplateRegistry registry;
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

        Files.writeString(extraTemplateDir.resolve("claimlog-short.yaml"), """
                name: claimlog-short/<room>
                keys:
                  - id
                id_from: keys
                take:
                  enabled: true
                  max_attempts: 3
                  max_lease_seconds: 300
                retention_seconds: 1
                claim_log_ttl_seconds: %d
                """.formatted(SHORT_TTL_SECONDS), StandardCharsets.UTF_8);
        Files.writeString(extraTemplateDir.resolve("claimlog-default.yaml"), """
                name: claimlog-default/<room>
                keys:
                  - id
                id_from: keys
                take:
                  enabled: true
                  max_attempts: 3
                  max_lease_seconds: 300
                retention_seconds: 1
                """, StandardCharsets.UTF_8);

        tenantScope = new TenantScope(svcDs);
        // sweepIntervalSeconds=1: both test-only templates' retention_seconds=1 must
        // clear "exceeds retention + one sweep interval" against their OWN effective
        // TTL (3s and the 180-day default respectively) -- trivially true for either.
        // claimLogTtlDaysEnv=null (the production default, 180 days) rather than a
        // tiny override, because the bundled v1 resource templates load here too and
        // their own retention_seconds needs a generous default to pass the same check.
        registry = TemplateRegistry.loadAtBoot(extraTemplateDir.toString(), null, 1L);
        repo = new TupleRepository(tenantScope, registry);
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

    // ── helpers ──────────────────────────────────────────────────────────────

    private record ClaimLogTimes(OffsetDateTime at, OffsetDateTime expiresAt) {
    }

    private String freshTenant(String label) {
        return "tuple-claimlogttl-" + label + "-" + UUID.randomUUID();
    }

    /** {@code out} then {@code in} on the given template/subspace, driving a REAL
     *  {@code insertClaimLog} write for the "claim" transition. */
    private byte[] outAndClaim(String tenant, String subspace, String idKey, String claimant) {
        byte[] id = repo.out(tenant, subspace, Map.of("id", idKey), Map.of(), null, null, null);
        var claimed = repo.inp(tenant, subspace, Map.of("id", idKey), claimant, 60);
        assertThat(claimed).as("the row must be claimable").isPresent();
        return id;
    }

    private ClaimLogTimes claimLogTimes(String tenant, byte[] tupleId) {
        try (Connection su = pg.createConnection("")) {
            var rec = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .select(TUPLE_CLAIM_LOG.AT, TUPLE_CLAIM_LOG.EXPIRES_AT)
                    .from(TUPLE_CLAIM_LOG)
                    .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant)
                            .and(TUPLE_CLAIM_LOG.TUPLE_ID.eq(tupleId))
                            .and(TUPLE_CLAIM_LOG.TRANSITION.eq("claim")))
                    .fetchOne();
            assertThat(rec).as("exactly one claim log row for this claim").isNotNull();
            return new ClaimLogTimes(rec.value1(), rec.value2());
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private boolean claimLogRowExists(String tenant, byte[] tupleId) {
        try (Connection su = pg.createConnection("")) {
            var dsl = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            return dsl.fetchExists(dsl.selectFrom(TUPLE_CLAIM_LOG)
                    .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant)
                            .and(TUPLE_CLAIM_LOG.TUPLE_ID.eq(tupleId))));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    // ── stamping at write time ───────────────────────────────────────────────

    @Test
    void templateWithShorterClaimLogTtl_stampsItsOwnEffectiveTtl_notTheEngineDefault() {
        String tenant = freshTenant("stamp-short");
        byte[] id = outAndClaim(tenant, "claimlog-short/room-a", "s1", "worker-1");

        ClaimLogTimes times = claimLogTimes(tenant, id);
        assertThat(Duration.between(times.at(), times.expiresAt()).toSeconds())
                .as("the template's own claim_log_ttl_seconds, not the engine default")
                .isEqualTo(SHORT_TTL_SECONDS);
    }

    @Test
    void templateWithNoOverride_keepsTheEngineDefault() {
        String tenant = freshTenant("stamp-default");
        byte[] id = outAndClaim(tenant, "claimlog-default/room-a", "d1", "worker-1");

        ClaimLogTimes times = claimLogTimes(tenant, id);
        assertThat(Duration.between(times.at(), times.expiresAt()).toSeconds())
                .as("no per-template override -- the engine-wide default applies")
                .isEqualTo(DEFAULT_TTL_SECONDS);
    }

    // ── purge behaviour: a direct consequence of the stamped expires_at ───────

    /**
     * The literal Test Plan scenario: two templates, one with a shorter
     * claim-log TTL, rows aged past the short template's own TTL but nowhere
     * near the engine default's — the purge (unchanged: it only ever compares
     * the STORED {@code expires_at} against "now", RDR-205 P1 follow-on
     * nexus-em75s.37) removes the short template's row and leaves the
     * default's alone.
     */
    @Test
    void shorterTemplateTtl_purgesBeforeTheEngineDefaultTemplatesRows() throws Exception {
        String tenant = freshTenant("purge-order");
        byte[] shortId = outAndClaim(tenant, "claimlog-short/room-b", "s2", "worker-1");
        byte[] defaultId = outAndClaim(tenant, "claimlog-default/room-b", "d2", "worker-1");

        Thread.sleep((SHORT_TTL_SECONDS + 2) * 1000L); // past the short TTL; nowhere near the default's

        var purge = repo.purgeOldClaimLogBatch(tenant, 300, null);

        assertThat(purge.purged()).as("exactly the short template's row").isEqualTo(1);
        assertThat(claimLogRowExists(tenant, shortId)).as("purged").isFalse();
        assertThat(claimLogRowExists(tenant, defaultId)).as("survives -- nowhere near its own deadline").isTrue();
    }
}
