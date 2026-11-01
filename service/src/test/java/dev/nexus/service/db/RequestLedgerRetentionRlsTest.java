// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER;
import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER_TENANTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.3): a retention pass as the real {@code nexus_svc}-style role, with
 * FORCE RLS live. The component test runs as a superuser, which bypasses RLS; this one proves the
 * {@code withTenant} stamping is what lets the pass see and delete its own tenant's rows, and only those.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RequestLedgerRetentionRlsTest {

    private static final String SVC_ROLE = "svc_ledger_retention_test";
    private static final String SVC_PASS = "svc_ledger_retention_test_pass";
    private static final OffsetDateTime CUTOFF = OffsetDateTime.of(2026, 10, 11, 12, 0, 0, 0, ZoneOffset.UTC);

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope scope;
    private RequestLedger ledger;

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
        scope = new TenantScope(svcDs);
        ledger = new RequestLedger(scope, svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private UUID seed(String tenant, OffsetDateTime finishedAt) {
        UUID id = UUID.randomUUID();
        scope.withTenant(tenant, ctx -> ctx.insertInto(REQUEST_LEDGER)
            .set(REQUEST_LEDGER.TENANT_ID, tenant)
            .set(REQUEST_LEDGER.REQUEST_ID, id)
            .set(REQUEST_LEDGER.ROUTE, "/v1/vectors/gc/quarantine-orphans")
            .set(REQUEST_LEDGER.BODY_SHA256, new byte[32])
            .set(REQUEST_LEDGER.STATE, "committed")
            .set(REQUEST_LEDGER.STARTED_AT, finishedAt.minusMinutes(1))
            .set(REQUEST_LEDGER.FINISHED_AT, finishedAt)
            .execute());
        return id;
    }

    private boolean exists(String tenant, UUID id) {
        return scope.withTenant(tenant, ctx -> ctx.fetchExists(
            ctx.selectOne().from(REQUEST_LEDGER).where(REQUEST_LEDGER.REQUEST_ID.eq(id))));
    }

    @Test
    void aPassUnderTheRealRoleDeletesItsOwnTenantsOldRows_andTouchesNoOtherTenant() {
        UUID aOld = seed("rls-a", CUTOFF.minusHours(1));
        UUID aFresh = seed("rls-a", CUTOFF.plusHours(1));
        UUID bOld = seed("rls-b", CUTOFF.minusHours(1));

        var passes = ledger.retainAll(java.util.List.of("rls-a"), CUTOFF);

        assertThat(passes).singleElement().satisfies(p -> {
            assertThat(p.failed()).isFalse();
            assertThat(p.deleted()).isEqualTo(1);
        });
        assertThat(exists("rls-a", aOld)).isFalse();
        assertThat(exists("rls-a", aFresh)).isTrue();
        assertThat(exists("rls-b", bOld)).as("tenant b is untouched by a's pass").isTrue();
        // Under RLS with b's stamp, a's row is invisible even by request_id alone.
        assertThat(exists("rls-b", aFresh)).as("RLS is live: b cannot see a's row").isFalse();
    }

    /**
     * nexus-yye8l.21: the ledger is FORCE RLS, so under the service role a read with no tenant stamp sees no row,
     * and the tenant list for retention has to come from somewhere else. A trigger on the ledger records each
     * tenant in {@code request_ledger_tenants}, which the service role reads whole. A superuser would see the ledger rows
     * anyway, which is why this runs as the service role.
     */
    @Test
    void aClaimRecordsTheTenant_andTheServiceRoleListsItWithNoTenantStamp() {
        String tenant = "rls-enum-" + UUID.randomUUID();
        var claim = ledger.claim(tenant, UUID.randomUUID(), "/v1/vectors/gc/quarantine-orphans", new byte[32], 25_000L);
        assertThat(claim).isInstanceOf(RequestLedger.Claimed.class);

        long visibleWithoutAStamp = org.jooq.impl.DSL.using(svcDs, org.jooq.SQLDialect.POSTGRES)
            .fetchCount(REQUEST_LEDGER);
        assertThat(visibleWithoutAStamp).as("FORCE RLS: the ledger itself cannot enumerate its tenants").isZero();

        assertThat(ledger.ledgerTenants(java.time.Duration.ofSeconds(5))).contains(tenant);
        // A second claim for the same tenant leaves one registry row, not two.
        ledger.claim(tenant, UUID.randomUUID(), "/v1/vectors/gc/quarantine-orphans", new byte[32], 25_000L);
        assertThat(ledger.ledgerTenants(java.time.Duration.ofSeconds(5))).containsOnlyOnce(tenant);
    }

    /**
     * nexus-yye8l.21: the registry row comes from a BEFORE INSERT trigger, not from the claim path, so a ledger row
     * written by plain SQL (RDR-222 Phase 2 adds a second insert site that does not go through
     * {@code RequestLedger.claim}) registers its tenant too. The insert runs as the service role under FORCE RLS,
     * so this also proves the trigger function can write the registry from there.
     */
    @Test
    void aLedgerRowInsertedBySql_notViaClaim_registersItsTenant() {
        String tenant = "rls-sql-" + UUID.randomUUID();
        assertThat(ledger.ledgerTenants(java.time.Duration.ofSeconds(5))).doesNotContain(tenant);

        seed(tenant, CUTOFF.minusHours(1));

        assertThat(ledger.ledgerTenants(java.time.Duration.ofSeconds(5))).containsOnlyOnce(tenant);
    }

    /**
     * nexus-yye8l.21: two first claims for one brand-new tenant, with different keys, race for the registry's
     * primary-key entry inside the trigger. Both claim, neither deadlocks or fails on a duplicate key, and one
     * registry row results. Repeated over fresh tenants because the window is narrow.
     */
    @Test
    void twoConcurrentFirstClaimsForANewTenant_bothSucceed_andOneRegistryRowResults() throws Exception {
        var pool = java.util.concurrent.Executors.newFixedThreadPool(2);
        try {
            for (int round = 0; round < 25; round++) {
                String tenant = "rls-race-" + UUID.randomUUID();
                var start = new java.util.concurrent.CyclicBarrier(2);
                java.util.concurrent.Callable<RequestLedger.Claim> claimer = () -> {
                    start.await(10, java.util.concurrent.TimeUnit.SECONDS);
                    return ledger.claim(tenant, UUID.randomUUID(), "/v1/vectors/gc/quarantine-orphans",
                        new byte[32], 25_000L);
                };
                var first = pool.submit(claimer);
                var second = pool.submit(claimer);
                assertThat(first.get(30, java.util.concurrent.TimeUnit.SECONDS))
                    .as("round %d, first claim", round).isInstanceOf(RequestLedger.Claimed.class);
                assertThat(second.get(30, java.util.concurrent.TimeUnit.SECONDS))
                    .as("round %d, second claim", round).isInstanceOf(RequestLedger.Claimed.class);
                try (Connection su = pg.createConnection("")) {
                    assertThat(org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                            .fetchCount(REQUEST_LEDGER_TENANTS, REQUEST_LEDGER_TENANTS.TENANT_ID.eq(tenant)))
                        .as("round %d: exactly one registry row", round).isEqualTo(1);
                }
            }
        } finally {
            pool.shutdownNow();
        }
    }

    /**
     * nexus-yye8l.21: the whole retention path as the service role and nothing else: claim, list the tenants from
     * the registry, retain. No superuser touches the ledger, and neither tenant holds a service token. The
     * tenant's expired rows go and its unexpired row, its live lease and another tenant's rows stay.
     */
    @Test
    void claimThenListThenRetain_asTheServiceRole_purgesATokenlessTenantsExpiredRowsAndNothingElse() {
        String tenant = "rls-e2e-" + UUID.randomUUID();
        String other = "rls-e2e-other-" + UUID.randomUUID();
        UUID expired = claimId(tenant);
        UUID unexpired = claimId(tenant);
        UUID live = claimId(tenant);
        UUID otherFresh = claimId(other);
        age(tenant, expired, CUTOFF.minusHours(1));
        age(tenant, unexpired, CUTOFF.plusHours(1));
        age(other, otherFresh, CUTOFF.plusHours(1));

        List<String> tenants = ledger.ledgerTenants(java.time.Duration.ofSeconds(5));
        assertThat(tenants).contains(tenant, other);
        var passes = ledger.retainAll(tenants.stream().filter(t -> t.equals(tenant) || t.equals(other)).toList(), CUTOFF);

        assertThat(passes).allMatch(p -> !p.failed());
        assertThat(exists(tenant, expired)).isFalse();
        assertThat(exists(tenant, unexpired)).isTrue();
        assertThat(exists(tenant, live)).as("a live lease is never deleted").isTrue();
        assertThat(exists(other, otherFresh)).isTrue();
    }

    private UUID claimId(String tenant) {
        UUID id = UUID.randomUUID();
        assertThat(ledger.claim(tenant, id, "/v1/vectors/gc/quarantine-orphans", new byte[32], 25_000L))
            .isInstanceOf(RequestLedger.Claimed.class);
        return id;
    }

    /** Mark a claimed row committed at {@code finishedAt}, through the tenant stamp as the service role. */
    private void age(String tenant, UUID id, OffsetDateTime finishedAt) {
        scope.withTenant(tenant, ctx -> ctx.update(REQUEST_LEDGER)
            .set(REQUEST_LEDGER.STATE, "committed")
            .set(REQUEST_LEDGER.HOLDER, (UUID) null)
            .set(REQUEST_LEDGER.STARTED_AT, finishedAt.minusMinutes(1))
            .set(REQUEST_LEDGER.FINISHED_AT, finishedAt)
            .where(REQUEST_LEDGER.REQUEST_ID.eq(id))
            .execute());
    }
}
