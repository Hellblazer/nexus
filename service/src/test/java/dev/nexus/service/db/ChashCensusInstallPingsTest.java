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
import java.time.ZoneOffset;
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6u63y — {@code install_pings.source_hash} is the first 16 hex chars of
 * an HMAC-SHA256 digest of an install's source (nexus-5zv4j), which is exactly
 * the census's legacy 16-hex chunk-ref shape. Once the cloud got its hash key,
 * every populated row counted as legacy residue and {@code /v1/staging/finalize}
 * failed for every tenant. The column is an identity, not a chunk pointer, so
 * the census must not read it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashCensusInstallPingsTest {

    /** Exactly the shape InstallPingHandler stores: 16 lowercase hex. */
    private static final String SOURCE_HASH_16_HEX = "0123456789abcdef";

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
    void populatedInstallPingsSourceHash_isNotLegacyResidue() {
        new InstallPingRepository(svcDs, Clock.fixed(Instant.parse("2026-09-29T12:00:00Z"), ZoneOffset.UTC))
            .record(new InstallPingRepository.Ping(UUID.randomUUID(), "7.67.0", "cloud",
                "linux", "amd64", "3.12", SOURCE_HASH_16_HEX));

        Map<String, Integer> residue = scope.withTenant(TENANT, ChashCensus::scan);

        assertThat(residue)
            .as("a 16-hex HMAC source_hash is an identity, not a chunk pointer (nexus-6u63y)")
            .doesNotContainKey("install_pings.source_hash")
            .isEmpty();
    }

    @Test
    void installPingsExclusion_namesAColumnThatExists() {
        // Non-vacuity: the exclusion must point at a real schema-discovered
        // TEXT column, else the test above passes for the wrong reason.
        scope.withTenant(TENANT, ctx -> {
            ChashCensus.assertDiscoversKnownInventory(ctx);
            return null;
        });
        assertThat(ChashCensus.TEXT_EXCLUSIONS)
            .anyMatch(e -> e.table().equals("install_pings") && e.column().equals("source_hash"));
    }
}
