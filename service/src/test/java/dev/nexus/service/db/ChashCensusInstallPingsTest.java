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
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6u63y — the census scans only RLS-enabled tables. {@code
 * install_pings} is global (no RLS) and takes hex-shaped tokens from the
 * unauthenticated {@code /v1/install-ping}: {@code source_hash} is a 16-hex
 * HMAC prefix by design (nexus-5zv4j), and client_version/os/arch/python
 * accept 16-hex too. That is exactly the census's legacy 16-hex chunk-ref
 * shape, so scanning it let any anonymous caller (or the cloud's own hash
 * key) fail every tenant's {@code /v1/staging/finalize}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashCensusInstallPingsTest {

    /** 16 lowercase hex: the census's legacy chunk-ref shape. */
    private static final String HEX16 = "0123456789abcdef";

    private static final String TENANT = "census-install-pings-tenant";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        HikariConfig cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void globalInstallPings_hexShapedValuesInAnyColumn_areNotReported() {
        var repo = new InstallPingRepository(svcDs,
            Clock.fixed(Instant.parse("2026-09-29T12:00:00Z"), ZoneOffset.UTC));
        repo.record(new InstallPingRepository.Ping(UUID.randomUUID(), "7.67.0", "cloud",
            "linux", "amd64", "3.12", HEX16));
        repo.record(new InstallPingRepository.Ping(UUID.randomUUID(), HEX16, "cloud",
            HEX16, HEX16, HEX16, HEX16));

        Map<String, Integer> residue = scope.withTenant(TENANT, ChashCensus::scan);

        assertThat(residue)
            .as("a global non-RLS table must be out of the census scope (nexus-6u63y)")
            .isEmpty();
    }

    @Test
    void rlsTenantTable_hexShapedValue_isReported_positiveControl() {
        // Non-vacuity for the test above: the same 16-hex shape in a text
        // column of an RLS tenant table the census covers IS reported, so the
        // empty result above comes from scoping, not from a blind scan.
        scope.withTenant(TENANT, ctx -> ctx.insertInto(RELEVANCE_LOG)
            .set(RELEVANCE_LOG.TENANT_ID, TENANT)
            .set(RELEVANCE_LOG.QUERY, "q")
            .set(RELEVANCE_LOG.CHUNK_ID, "a".repeat(64))   // canonical: chunk_id has a CHECK
            .set(RELEVANCE_LOG.SESSION_ID, HEX16)          // unconstrained TEXT the census scans
            .set(RELEVANCE_LOG.ACTION, "view")
            .set(RELEVANCE_LOG.TIMESTAMP, OffsetDateTime.parse("2026-09-29T12:00:00Z"))
            .execute());
        try {
            Map<String, Integer> residue = scope.withTenant(TENANT, ChashCensus::scan);
            assertThat(residue).containsEntry("relevance_log.session_id", 1);
        } finally {
            scope.withTenant(TENANT, ctx -> ctx.deleteFrom(RELEVANCE_LOG)
                .where(RELEVANCE_LOG.TENANT_ID.eq(TENANT)).execute());
        }
    }

    @Test
    void knownInventory_isStillDiscovered_afterRlsScoping() {
        scope.withTenant(TENANT, ctx -> {
            ChashCensus.assertDiscoversKnownInventory(ctx);
            return null;
        });
    }
}
