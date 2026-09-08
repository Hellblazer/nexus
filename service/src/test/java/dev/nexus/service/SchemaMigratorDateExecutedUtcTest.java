// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.TimeZone;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-rph82: {@code databasechangelog.dateexecuted} must read as UTC.
 *
 * <p>Liquibase stamps that column (TIMESTAMP WITHOUT TIME ZONE) in the JVM's
 * default zone; every post-deploy audit windows it against {@code now()} on a
 * GMT database. A JVM seven hours behind wrote rows that a "last 30 minutes"
 * window could not see sixty seconds after the walk (PITR fork, 2026-08-27).
 * This test starts the JVM in a far-from-UTC zone, runs the migrator, and
 * asserts the newest row is within minutes of wall-clock UTC — which only
 * holds because {@link SchemaMigrator#migrate} pins the zone first.
 */
class SchemaMigratorDateExecutedUtcTest {

    private static final TimeZone ORIGINAL = TimeZone.getDefault();

    @AfterAll
    static void restoreZone() {
        TimeZone.setDefault(ORIGINAL);
    }

    @Test
    void dateExecuted_readsAsUtc_evenWhenTheJvmStartedInAnotherZone() throws Exception {
        // Seven hours behind GMT in August — the measured production shape.
        TimeZone.setDefault(TimeZone.getTimeZone("America/Los_Angeles"));

        try (var pg = PgContainerHelper.start()) {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                // KEPT RAW: conditional CREATE ROLE via a DO $$ block -- no typed jOOQ
                // DDL form for "create this role only if it does not already exist",
                // and no nexus_test bootstrap function covers it this round (candidate
                // for one, per the task brief's ladder-file exclusion list).
                su.createStatement().execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                    + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
            }
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(pg.getUsername());
            cfg.setPassword(pg.getPassword());
            cfg.setMaximumPoolSize(1);
            cfg.setAutoCommit(true);
            Instant before = Instant.now();
            try (var ds = new HikariDataSource(cfg)) {
                SchemaMigrator.migrate(ds);
            }
            Instant after = Instant.now();

            assertThat(TimeZone.getDefault().getID())
                .as("the migrator pins the JVM default zone before Liquibase runs")
                .isEqualTo("UTC");

            try (Connection su = pg.createConnection("")) {
                var dateExecuted = DSL.field(DSL.name("dateexecuted"), LocalDateTime.class);
                LocalDateTime stamped = DSL.using(su, SQLDialect.POSTGRES)
                    .select(DSL.max(dateExecuted))
                    .from(DSL.table(DSL.name("databasechangelog")))
                    .fetchOne(0, LocalDateTime.class);
                assertThat(stamped).as("a walk that ran must have stamped rows").isNotNull();
                Instant asUtc = stamped.toInstant(ZoneOffset.UTC);
                // Read as UTC, the newest row lands inside the walk's own window.
                // A JVM-local write from Los Angeles would sit ~7h earlier.
                assertThat(asUtc)
                    .as("dateexecuted read as UTC is within the walk's wall-clock window")
                    .isAfter(before.minus(Duration.ofMinutes(5)))
                    .isBefore(after.plus(Duration.ofMinutes(5)));
            }
        }
    }
}
