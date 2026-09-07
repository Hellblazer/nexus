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
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-h5olw — {@code nexus.install_pings} through the product changelog, as
 * {@code nexus_svc} (NOSUPERUSER NOBYPASSRLS): the write needs no tenant GUC,
 * and the 28-day distinct count is the active-install number.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class InstallPingRepositoryTest {

    private static final Instant T0 = Instant.parse("2026-09-07T12:00:00Z");

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;

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
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private InstallPingRepository at(Instant now) {
        return new InstallPingRepository(svcDs, Clock.fixed(now, ZoneOffset.UTC));
    }

    @Test
    void distinctInstallsInsideWindow_countOnce_outsideWindow_notAtAll() {
        UUID a = UUID.randomUUID();
        UUID b = UUID.randomUUID();
        UUID old = UUID.randomUUID();

        at(T0.minus(Duration.ofDays(40))).record(ping(old));
        at(T0.minus(Duration.ofDays(3))).record(ping(a));
        at(T0.minus(Duration.ofDays(2))).record(ping(a));   // same install, second day
        at(T0.minus(Duration.ofDays(1))).record(ping(b));

        assertThat(at(T0).activeInstalls(Duration.ofDays(28))).isEqualTo(2);
        assertThat(at(T0).activeInstalls(Duration.ofDays(60))).isEqualTo(3);
    }

    private static InstallPingRepository.Ping ping(UUID id) {
        return new InstallPingRepository.Ping(id, "7.35.0", "local", "darwin", "arm64", "3.12");
    }
}
