// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.12 (nexus-p2m1x.32): the source of {@code GET /v1/status}'s {@code live_label_index_present}, whether the
 * partial UNIQUE index {@code idx_catalog_collections_live_label} (catalog-053-1) exists and is VALID. catalog-053-1 is
 * failOnError=false behind a CONTINUE precondition, so on some installs it never builds and nothing else says so.
 * Each test takes its own clone of the migrated template, so dropping or invalidating the index touches no other class.
 */
class LiveLabelIndexProbeTest {

    private static final String INDEX = "idx_catalog_collections_live_label";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;

    @BeforeEach
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        ds = PgContainerHelper.superuserDataSource(pg);
    }

    @AfterEach
    void stop() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private void dropIndex() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // plain SQL: jOOQ renders a PostgreSQL DROP INDEX without the schema qualifier, and nexus is not on the search_path
            DSL.using(su, SQLDialect.POSTGRES).execute("DROP INDEX nexus." + INDEX);
        }
    }

    @Test
    void aFullyMigratedStore_hasTheIndexPresentAndValid() {
        assertThat(LiveLabelIndexProbe.present(ds)).isTrue();
    }

    @Test
    void aFailedBuild_theIndexAbsent_isFalse() throws Exception {
        dropIndex();
        assertThat(LiveLabelIndexProbe.present(ds)).as("what a catalog-053-1 that never built leaves").isFalse();
    }

    @Test
    void anInvalidIndexOfThatName_isFalse() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.markIndexInvalid(su, "nexus", INDEX);
        }
        assertThat(LiveLabelIndexProbe.present(ds)).as("present but INVALID is not present").isFalse();
    }

    @Test
    void aProbeThatCouldNotRun_isNull() {
        ds.close();
        assertThat(LiveLabelIndexProbe.present(ds)).isNull();
    }

    @Test
    void theStatusSupplier_cachesAnswerForTheTtl_andRereadsAfterIt_neverCachingNull() throws Exception {
        long[] now = {Instant.parse("2026-11-22T00:00:00Z").toEpochMilli()};
        Clock clock = new Clock() {
            @Override public java.time.ZoneId getZone() { return ZoneOffset.UTC; }
            @Override public Clock withZone(java.time.ZoneId z) { return this; }
            @Override public Instant instant() { return Instant.ofEpochMilli(now[0]); }
        };
        Supplier<Boolean> supplier = LiveLabelIndexProbe.statusSupplier(ds, clock);
        assertThat(supplier.get()).isTrue();
        dropIndex();
        now[0] += LiveLabelIndexProbe.TTL_MILLIS - 1;
        assertThat(supplier.get()).as("inside the TTL the cached answer stands").isTrue();
        now[0] += 1;
        assertThat(supplier.get()).as("past the TTL it is read again").isFalse();
    }
}
