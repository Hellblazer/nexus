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
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER;
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
}
