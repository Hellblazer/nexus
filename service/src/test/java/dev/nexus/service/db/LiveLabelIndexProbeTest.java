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

import javax.sql.DataSource;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.concurrent.atomic.AtomicBoolean;
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

    /** A pool whose connections fail while {@code failing} is set: a probe that cannot run, then recovers. */
    private static DataSource flaky(DataSource real, AtomicBoolean failing) {
        return (DataSource) Proxy.newProxyInstance(DataSource.class.getClassLoader(), new Class<?>[] {DataSource.class},
            (proxy, method, args) -> {
                if (method.getName().equals("getConnection") && failing.get()) {
                    throw new SQLException("the pool is down");
                }
                try {
                    return method.invoke(real, args);
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
            });
    }

    @Test
    void aNullAnswer_isNeverServedAfterTheProbeRecovers() {
        AtomicBoolean failing = new AtomicBoolean(true);
        // a clock that never moves: only a cached null could answer the second call without reading again
        Clock frozen = Clock.fixed(Instant.parse("2026-11-22T00:00:00Z"), ZoneOffset.UTC);
        Supplier<Boolean> supplier = LiveLabelIndexProbe.statusSupplier(flaky(ds, failing), frozen);
        assertThat(supplier.get()).as("a probe that could not run answers null, so the route omits the key").isNull();
        assertThat(supplier.get()).as("still down: still null, read again each time").isNull();
        failing.set(false);
        assertThat(supplier.get()).as("recovered inside the TTL window: the probe is read again, the null was not cached").isTrue();
    }
}
