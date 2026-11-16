// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.JSONB;
import org.slf4j.LoggerFactory;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciBuilderSession.DdlOutcome;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd (E): the builder's session carries the bounded {@code maintenance_work_mem} when it builds, and no other
 * session does: a normal request session (the pool's) and a second admin session read Postgres's own value.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciBuildMemoryIntegrationTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    static final long GB = 1L << 30;
    static final PciSettings AUTO = new PciSettings(true, 200, 600, 16);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PciCatalog catalog;
    int tenants;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.startMigratedByOwner();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        catalog = new PciCatalog(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** A tenant with a leaf and one registered, empty collection; returns the leaf. */
    @SuppressWarnings("deprecation")
    PciCatalog.Leaf newLeaf(String collection) throws Exception {
        String tenant = "pcimem-" + (++tenants);
        try (Connection owner = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true, true);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection, M384);
        }
        List<PciCatalog.Leaf> hits = catalog.read().leaves().stream()
            .filter(l -> M384.equals(l.model()) && tenant.equals(l.tenant())).toList();
        assertThat(hits).hasSize(1);
        return hits.get(0);
    }

    PciBuilderSession builder(String nonce, PciSettings s, long ramBytes) {
        return new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, nonce, s, () -> ramBytes);
    }

    /** The session's maintenance_work_mem, as {@code SHOW} prints it. */
    static String setting(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES).select(DSL.function("current_setting", SQLDataType.VARCHAR,
            DSL.val("maintenance_work_mem"))).fetchOne(0, String.class);
    }

    /** The maintenance_work_mem of a fresh session of {@code user}. */
    String showOn(String user, String password) throws Exception {
        try (Connection c = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), user, password)) {
            return setting(c);
        }
    }

    @Test
    void theBuildersSessionCarriesTheBound_andNoOtherSessionDoes() throws Exception {
        String collection = "knowledge__pcimem-a__minilm-l6-v2-384__v1";
        PciCatalog.Leaf leaf = newLeaf(collection);
        String serverDefault = showOn(PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD);
        assertThat(serverDefault).as("a normal request session reads Postgres's own value").isEqualTo("64MB");

        try (PciBuilderSession.Pass pass = builder("aa000001", AUTO, 4 * GB).open()) {
            assertThat(pass.currentSetting("maintenance_work_mem")).as("before a build, still the default")
                .isEqualTo("64MB");

            assertThat(pass.build(leaf, 384, collection)).isEqualTo(DdlOutcome.DONE);

            assertThat(pass.currentSetting("maintenance_work_mem")).as("4 GB of RAM: an eighth").isEqualTo("512MB");
            assertThat(showOn(PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD))
                .as("the pool's role, a new session, while the builder holds its bound").isEqualTo(serverDefault);
            assertThat(showOn(ADMIN_ROLE, ADMIN_PASS)).as("a second session of the builder's own role")
                .isEqualTo(serverDefault);
            // A session the pool already holds is unaffected too: ask the engine's own pool.
            try (Connection pooled = svcDs.getConnection()) {
                assertThat(setting(pooled)).isEqualTo(serverDefault);
            }
        }
    }

    @Test
    void theCapHolds_onALargeHost() throws Exception {
        String collection = "knowledge__pcimem-b__minilm-l6-v2-384__v1";
        PciCatalog.Leaf leaf = newLeaf(collection);
        try (PciBuilderSession.Pass pass = builder("aa000002", AUTO, 64 * GB).open()) {
            assertThat(pass.build(leaf, 384, collection)).isEqualTo(DdlOutcome.DONE);
            assertThat(pass.currentSetting("maintenance_work_mem")).isEqualTo("1GB");
        }
    }

    @Test
    void theOverrideWins_withinItsLimits() throws Exception {
        String collection = "knowledge__pcimem-c__minilm-l6-v2-384__v1";
        PciCatalog.Leaf leaf = newLeaf(collection);
        var override = new PciSettings(true, 200, 600, 16, 100);
        try (PciBuilderSession.Pass pass = builder("aa000003", override, 64 * GB).open()) {
            assertThat(pass.build(leaf, 384, collection)).isEqualTo(DdlOutcome.DONE);
            assertThat(pass.currentSetting("maintenance_work_mem")).isEqualTo("100MB");
        }
    }

    @Test
    void aPostgresOnAnotherHost_withNoOverride_keepsItsOwnSetting() throws Exception {
        String collection = "knowledge__pcimem-d__minilm-l6-v2-384__v1";
        PciCatalog.Leaf leaf = newLeaf(collection);
        // The same database, reached as a name that is not loopback: only the URL's host is read. The container binds
        // a mapped port on the host, so the host's own address serves.
        String remoteUrl = pg.getJdbcUrl().replaceFirst("//[^:/]+", "//db.example.test");
        assertThat(PciBuildMemory.pgOnThisHost(remoteUrl)).isFalse();
        var session = new PciBuilderSession(remoteUrl, ADMIN_ROLE, ADMIN_PASS, "aa000004", AUTO, () -> 64 * GB);
        assertThat(session.buildMemoryMb()).as("the engine host's RAM is not used for a Postgres elsewhere").isEmpty();
        // The local one, for the contrast, with the same RAM:
        assertThat(builder("aa000005", AUTO, 64 * GB).buildMemoryMb()).hasValue(1024);
        assertThat(leaf).isNotNull();
    }

    /**
     * A parallel HNSW build keeps its graph in one shared-memory area as large as maintenance_work_mem. Where
     * /dev/shm is smaller (a container's default is 64 MB) the build fails with 53100 and, left alone, would fail on
     * every pass and be backed off forever. The builder drops the failed index and builds serially, and starts every
     * later build serial. 2047 MB is the override's top value (the Windows limit of maintenance_work_mem): the test
     * container's /dev/shm, Docker's 64 MB default, does not hold it.
     */
    @Test
    void aSharedMemoryShortage_fallsBackToASerialBuild_andLaterBuildsStartSerial() throws Exception {
        String first = "knowledge__pcimem-e__minilm-l6-v2-384__v1";
        String second = "knowledge__pcimem-f__minilm-l6-v2-384__v1";
        PciCatalog.Leaf leaf = newLeaf(first);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), leaf.tenant(), second, M384);
            for (String collection : List.of(first, second)) {
                for (int from = 0; from < 6000; from += 250) {
                    var step = DSL.using(su, SQLDialect.POSTGRES).insertInto(CHUNKS, CHUNKS.TENANT_ID,
                        CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT,
                        CHUNKS.EMBEDDING_384, CHUNKS.METADATA);
                    for (int i = from; i < from + 250; i++) {
                        float[] v = new float[384];
                        v[i % 384] = 1f;
                        v[(i * 7 + 1) % 384] += 0.5f;
                        step = step.values(leaf.tenant(), collection,
                            Chash.fromHex(Chash.ofText(collection + "/" + i).toHex()).toBytes(), M384,
                            "row " + i + " " + "x".repeat(200), Vector.of(v), JSONB.jsonb("{}"));
                    }
                    step.execute();
                }
            }
        }
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(PciBuilderSession.class);
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        Level before = logger.getLevel();
        logger.setLevel(Level.INFO);
        logger.addAppender(appender);
        try (PciBuilderSession.Pass pass = builder("aa000006", new PciSettings(true, 200, 600, 16, 2_047), 64 * GB)
                .open()) {
            assertThat(pass.build(leaf, 384, first)).as("built, after the fallback").isEqualTo(DdlOutcome.DONE);
            assertThat(pass.build(leaf, 384, second)).as("the next build starts serial").isEqualTo(DdlOutcome.DONE);
            assertThat(pass.currentSetting("max_parallel_maintenance_workers")).isEqualTo("0");
        } finally {
            logger.detachAppender(appender);
            logger.setLevel(before);
        }
        long shortages = appender.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(l -> l.contains("event=pci_build_shared_memory_short")).count();
        assertThat(shortages).as("the shortage was met once, by the first build").isEqualTo(1);
        List<PciCatalog.Index> indexes = catalog.read().leaves().stream()
            .filter(l -> leaf.name().equals(l.name())).findFirst().orElseThrow().indexes();
        assertThat(indexes).as("both indexes valid, no failed leftover").hasSize(2).allMatch(PciCatalog.Index::valid);
    }
}
