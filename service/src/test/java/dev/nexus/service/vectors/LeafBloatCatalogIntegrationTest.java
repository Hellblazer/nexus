// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.http.StatusHandler;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import javax.sql.DataSource;
import java.io.PrintWriter;
import java.net.InetSocketAddress;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.ConnectionBuilder;
import java.sql.SQLException;
import java.sql.SQLFeatureNotSupportedException;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Logger;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-dxsjc (nexus-4lnn1 R4): {@link LeafBloatCatalog} and {@link LeafBloatSweep} against the real partitioned
 * layout, in production's shape (a dedicated container migrated by a non-superuser owner, read through the service
 * role). Each test makes a tenant of its own and drops its leaves afterwards.
 *
 * <p>Dead tuples are made by deleting rows from a leaf that is too small to trigger autovacuum (the leaf threshold is
 * 1000 dead rows plus 5 percent of the live rows), or from one with autovacuum switched off, so the numbers asserted
 * are exactly the deletes and nothing races them.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class LeafBloatCatalogIntegrationTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource adminDs;
    LeafBloatCatalog catalog;
    PciCatalog pciCatalog;
    final List<String> tenants = new ArrayList<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startMigratedByOwner();
        var adminCfg = new HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        adminDs = new HikariDataSource(adminCfg);
        var svcCfg = new HikariConfig();
        svcCfg.setJdbcUrl(pg.getJdbcUrl());
        svcCfg.setUsername(PgContainerHelper.SVC_USERNAME);
        svcCfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        svcCfg.setMaximumPoolSize(4);
        svcCfg.setAutoCommit(true);
        svcDs = new HikariDataSource(svcCfg);
        catalog = new LeafBloatCatalog(svcDs);
        pciCatalog = new PciCatalog(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    @AfterEach
    @SuppressWarnings("deprecation")
    void removeTenants() throws Exception {
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            DSLContext ctx = DSL.using(owner, SQLDialect.POSTGRES);
            for (String tenant : tenants) {
                dev.nexus.service.jooq.nexus.Routines.dropTenantPartitions(ctx.configuration(), tenant);
            }
        }
        tenants.clear();
    }

    // -- fixtures ----------------------------------------------------------------------------------------

    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        String tenant = "leafbloat-" + tag;
        tenants.add(tenant);
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true);
        }
        return tenant;
    }

    static String collectionName(String tag) {
        return "knowledge__leafbloat-" + tag + "__minilm-l6-v2-384__v1";
    }

    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    /** {@code ALTER TABLE nexus.leaf SET (options)} as the superuser. */
    void setStorage(String leaf, Map<String, String> options) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.setTableStorageParams(su, DSL.table(DSL.name("nexus", leaf)), options);
        }
    }

    /** {@code ALTER TABLE nexus.leaf RESET (names)} as the superuser. */
    void resetStorage(String leaf, String... names) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.resetTableStorageParams(su, DSL.table(DSL.name("nexus", leaf)), names);
        }
    }

    /** VACUUM cannot run in a transaction block, so a changelog changeset with runInTransaction="false" runs it. */
    void vacuum(String leaf) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.vacuumTable(su, "nexus", leaf);
        }
    }

    /** Register {@code collection} for {@code tenant} and give it {@code rows} rows; returns their chash hex. */
    List<String> rows(String tenant, String collection, int rows) throws Exception {
        return asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            List<String> hex = new ArrayList<>();
            var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA);
            for (int i = 0; i < rows; i++) {
                String h = Chash.ofText(tenant + "/" + collection + "/" + i).toHex();
                hex.add(h);
                step = step.values(tenant, collection, Chash.fromHex(h).toBytes(), M384, "row " + i, vector(i),
                    JSONB.jsonb("{}"));
            }
            step.execute();
            return hex;
        });
    }

    void deleteRows(String tenant, String collection, List<String> hex) throws Exception {
        asSuperuser(su -> {
            for (String h : hex) {
                su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection))
                    .and(CHUNKS.CHASH.eq(Chash.fromHex(h).toBytes())).execute();
            }
            return null;
        });
    }

    static Vector vector(int i) {
        float[] v = new float[384];
        v[i % 384] = 1f;
        v[(i * 7 + 1) % 384] += 0.5f;
        return Vector.of(v);
    }

    String leafName(String tenant) {
        List<String> hits = pciCatalog.read().leaves().stream()
            .filter(l -> M384.equals(l.model()) && tenant.equals(l.tenant())).map(PciCatalog.Leaf::name).toList();
        assertThat(hits).as("exactly one leaf for (%s, %s)", M384, tenant).hasSize(1);
        return hits.get(0);
    }

    LeafBloatCatalog.Leaf leaf(String name) {
        return catalog.read().leaves().stream().filter(l -> l.name().equals(name)).findFirst().orElseThrow();
    }

    /** Statistics reach other sessions on the writer's next flush; force it, then wait for the counts to land. */
    void awaitStats(String leafName, long live, long dead) throws Exception {
        long deadline = System.nanoTime() + Duration.ofSeconds(30).toNanos();
        LeafBloatCatalog.Leaf seen = null;
        while (System.nanoTime() < deadline) {
            asSuperuser(su -> su.select(DSL.function("pg_stat_force_next_flush", SQLDataType.OTHER)).fetch());
            seen = leaf(leafName);
            if (seen.liveRows() == live && seen.deadRows() == dead) {
                return;
            }
            Thread.sleep(100);
        }
        throw new AssertionError("statistics for " + leafName + " never reached live=" + live + " dead=" + dead
            + "; last saw " + seen);
    }

    // -- the leaf view -----------------------------------------------------------------------------------

    @Test
    void deadFractionAfterDeletes_isDeadOverLivePlusDead_onTheLeaf() throws Exception {
        String tenant = newTenant("dead");
        String collection = collectionName("dead");
        List<String> hex = rows(tenant, collection, 100);
        String leaf = leafName(tenant);
        awaitStats(leaf, 100, 0);
        assertThat(leaf(leaf).deadFraction()).as("before any delete").isZero();
        assertThat(leaf(leaf).overdue()).isFalse();
        assertThat(leaf(leaf).vacuumCount()).isZero();

        deleteRows(tenant, collection, hex.subList(0, 30));
        awaitStats(leaf, 70, 30);

        LeafBloatCatalog.Leaf l = leaf(leaf);
        assertThat(l.liveRows()).isEqualTo(70);
        assertThat(l.deadRows()).isEqualTo(30);
        assertThat(l.deadFraction()).isEqualTo(0.3);
        assertThat(l.overdue()).as("30 percent dead, but 30 rows: autovacuum has no reason to run, so not overdue")
            .isFalse();
        // 30 dead rows are under the leaf's vacuum threshold of 1000, so no autovacuum ran.
        assertThat(l.lastAutovacuum()).isNull();
        System.out.println("DXSJC evidence: leaf " + leaf + " live=" + l.liveRows() + " dead=" + l.deadRows()
            + " fraction=" + l.deadFraction());
    }

    /**
     * nexus-i827c: a leaf's {@code pci_} indexes and their sizes are read, so the authenticated leaf listing can say
     * how much index a leaf carries. A leaf with none reads as empty and 0 bytes; the leaf's own primary-key and
     * chash indexes, which carry other names, are not counted.
     */
    @Test
    void aLeafReportsItsPciIndexesAndTheirBytes_andItsOtherIndexesAreNotCounted() throws Exception {
        String tenant = newTenant("pcibytes");
        String collection = collectionName("pcibytes");
        rows(tenant, collection, 50);
        String leaf = leafName(tenant);
        assertThat(leaf(leaf).pciIndexes()).as("no pci_ index yet").isEmpty();
        assertThat(leaf(leaf).pciIndexBytes()).isZero();

        String pci = PciCatalog.indexName(M384, tenant, collection);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.createLeafHnswIndex(su, "nexus", leaf, pci, "embedding_384", collection);
        }

        LeafBloatCatalog.Leaf l = leaf(leaf);
        assertThat(l.pciIndexes()).extracting(LeafBloatCatalog.PciIndex::name).containsExactly(pci);
        assertThat(l.pciIndexes().get(0).valid()).isTrue();
        assertThat(l.pciIndexes().get(0).bytes()).as("an HNSW index over 50 rows occupies pages").isPositive();
        assertThat(l.pciIndexBytes()).isEqualTo(l.pciIndexes().get(0).bytes());
    }

    /**
     * nexus-i827c review: {@code pg_relation_size} takes an ACCESS SHARE lock on the index, so a non-concurrent
     * {@code DROP INDEX} or {@code REINDEX} (ACCESS EXCLUSIVE on it) would stall the read and fail the aggregate view
     * with it. The size read has a lock bound of its own and its failure costs the sizes only: the counts of every
     * leaf are still read, the index is listed with an unknown size (never 0), and the next read after the lock ends
     * has the size again. The statement bound is 3 s here against a 300 ms lock bound, so a read that waited on the
     * lock instead of giving up on it fails this test on the bound.
     */
    @Test
    void aLockHeldOnAPciIndexCostsTheSizesNotTheAggregateRead() throws Exception {
        String tenant = newTenant("pcilock");
        String collection = collectionName("pcilock");
        List<String> hex = rows(tenant, collection, 100);
        String leaf = leafName(tenant);
        awaitStats(leaf, 100, 0);
        deleteRows(tenant, collection, hex.subList(0, 30));
        awaitStats(leaf, 70, 30);
        String pci = PciCatalog.indexName(M384, tenant, collection);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.createLeafHnswIndex(su, "nexus", leaf, pci, "embedding_384", collection);
        }
        var bounded = new LeafBloatCatalog(svcDs, Duration.ofSeconds(3), Duration.ofMillis(300));

        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            // An uncommitted DROP INDEX holds ACCESS EXCLUSIVE on the index until it ends.
            PgContainerHelper.dropIndex(holder, DSL.name("nexus", pci));
            long start = System.nanoTime();
            LeafBloatCatalog.Snapshot snapshot = bounded.read();
            long elapsedMs = (System.nanoTime() - start) / 1_000_000;
            holder.rollback();

            assertThat(elapsedMs).as("the read gave up on the lock, it did not wait for the statement bound")
                .isLessThan(2_500);
            LeafBloatCatalog.Leaf l = snapshot.leaves().stream().filter(x -> x.name().equals(leaf)).findFirst().orElseThrow();
            assertThat(l.liveRows()).as("the aggregate read still ran").isEqualTo(70);
            assertThat(l.deadRows()).isEqualTo(30);
            assertThat(snapshot.leaves()).as("leaves examined").hasSizeGreaterThanOrEqualTo(8);
            assertThat(l.pciIndexes()).extracting(LeafBloatCatalog.PciIndex::name).containsExactly(pci);
            assertThat(l.pciIndexes().get(0).bytes()).as("the size that could not be read is unknown, not 0").isNull();
            assertThat(l.pciIndexBytes()).isNull();
        }
        // With the lock released, the next read has the size again (and the lock bound did not leak into the pool).
        LeafBloatCatalog.Leaf after = bounded.read().leaves().stream().filter(x -> x.name().equals(leaf)).findFirst().orElseThrow();
        assertThat(after.pciIndexBytes()).isPositive();
    }

    @Test
    void anEmptyLeaf_hasDeadFractionZero_notNaN() throws Exception {
        String tenant = newTenant("empty");
        LeafBloatCatalog.Leaf l = leaf(leafName(tenant));
        assertThat(l.liveRows()).isZero();
        assertThat(l.deadRows()).isZero();
        assertThat(l.deadFraction()).isZero();
        assertThat(l.plannerRows()).as("reltuples is -1 on a leaf never analyzed; it reads as 0").isZero();
        assertThat(l.overdue()).isFalse();
    }

    @Test
    void theParentAndTheModelPartitionsAreNeverReported_onlyLeaves() throws Exception {
        String tenant = newTenant("parent");
        String mine = leafName(tenant);
        List<String> names = catalog.read().leaves().stream().map(LeafBloatCatalog.Leaf::name).toList();
        // Non-vacuity: the four real models each have the default tenant's leaf, plus this tenant's.
        assertThat(names).as("leaves examined").hasSizeGreaterThanOrEqualTo(8).contains(mine);
        assertThat(names).as("the partitioned parent").doesNotContain("chunks");
        // Every name read from the catalog as a model partition (a relation that has children) is absent.
        List<String> modelPartitions = asSuperuser(su -> su.select(DSL.field(DSL.name("c", "relname"), String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .where(DSL.field(DSL.name("c", "relkind"), String.class).eq("p"))
            .and(DSL.field(DSL.name("c", "relname"), String.class).like("chunks%"))
            .fetch(DSL.field(DSL.name("c", "relname"), String.class)));
        assertThat(modelPartitions).as("partitioned relations examined").hasSizeGreaterThanOrEqualTo(5);
        assertThat(names).doesNotContainAnyElementsOf(modelPartitions);
        assertThat(names).as("sorted by name").isSorted();
    }

    @Test
    void aLeafPastTheOverdueLineIsOverdue_andAVacuumMovesTheCountAndClearsIt() throws Exception {
        String tenant = newTenant("overdue");
        String collection = collectionName("overdue");
        List<String> hex = rows(tenant, collection, 1300);
        String leaf = leafName(tenant);
        // Autovacuum off so the counts stay exactly what the deletes made them (nothing races the read).
        setStorage(leaf, Map.of("autovacuum_enabled", "false"));
        awaitStats(leaf, 1300, 0);
        assertThat(leaf(leaf).overdue()).isFalse();

        deleteRows(tenant, collection, hex.subList(0, 1200));
        awaitStats(leaf, 100, 1200);
        LeafBloatCatalog.Leaf bloated = leaf(leaf);
        assertThat(bloated.deadFraction()).as("92 percent dead, information only").isGreaterThan(0.9);
        assertThat(bloated.overdue()).as("1200 > 1000 + 0.10 x 1300 = 1130").isTrue();
        assertThat(bloated.vacuumCount()).isZero();
        assertThat(bloated.autovacuumEnabled()).isFalse();

        vacuum(leaf);
        long deadline = System.nanoTime() + Duration.ofSeconds(30).toNanos();
        LeafBloatCatalog.Leaf after = null;
        while (System.nanoTime() < deadline) {
            asSuperuser(su -> su.select(DSL.function("pg_stat_force_next_flush", SQLDataType.OTHER)).fetch());
            after = leaf(leaf);
            if (after.vacuumCount() >= 1 && after.deadRows() == 0) break;
            Thread.sleep(100);
        }
        assertThat(after.vacuumCount()).as("vacuum_count + autovacuum_count moved by the manual VACUUM").isEqualTo(1);
        assertThat(after.deadRows()).isZero();
        assertThat(after.overdue()).isFalse();
    }

    @Test
    void reloptions_presentOnANewLeaf_absentAfterAReset_andAutovacuumDisabledIsItsOwnFlag() throws Exception {
        String tenant = newTenant("opts");
        String leaf = leafName(tenant);

        LeafBloatCatalog.Leaf fresh = leaf(leaf);
        assertThat(fresh.policySet()).as("a new leaf carries the four (partition_ensure_leaf)").isTrue();
        assertThat(fresh.autovacuumEnabled()).isTrue();

        resetStorage(leaf, "autovacuum_vacuum_scale_factor");
        LeafBloatCatalog.Leaf missingOne = leaf(leaf);
        assertThat(missingOne.policySet()).as("one of the four missing").isFalse();
        assertThat(missingOne.autovacuumEnabled()).isTrue();

        setStorage(leaf, Map.of("autovacuum_vacuum_scale_factor", "0.05", "autovacuum_enabled", "false"));
        LeafBloatCatalog.Leaf disabled = leaf(leaf);
        assertThat(disabled.policySet()).as("all four back, autovacuum off").isTrue();
        assertThat(disabled.autovacuumEnabled()).isFalse();

        // Other leaves are untouched by this tenant's alterations.
        long others = catalog.read().leaves().stream().filter(l -> !l.name().equals(leaf))
            .filter(l -> !l.policySet() || !l.autovacuumEnabled()).count();
        assertThat(others).as("other leaves flagged").isZero();
    }

    /**
     * The reviewer's measured case (nexus-dxsjc): a statistics reset (or an unclean shutdown, a restore) zeroes
     * {@code n_live_tup} and it stays 0 until a vacuum or analyze, while autovacuum keeps scaling by
     * {@code pg_class.reltuples}. 1200 deletes on a 5000-row leaf are over the old {@code live + dead} line (1000 + 0.10 x
     * 1200 = 1120) and under autovacuum's real trigger (1000 + 0.05 x 5000 = 1250), so autovacuum is correctly idle and
     * the leaf must not read overdue.
     */
    @Test
    void aStatisticsResetLeafWithAFewDeletes_isNotOverdue_becauseTheRowEstimateSurvivesTheReset() throws Exception {
        String tenant = newTenant("reset");
        String collection = collectionName("reset");
        List<String> hex = rows(tenant, collection, 5000);
        String leaf = leafName(tenant);
        // Autovacuum off so nothing races the numbers; the trigger it would use is checked arithmetically below.
        setStorage(leaf, Map.of("autovacuum_enabled", "false"));
        vacuum(leaf);
        awaitStats(leaf, 5000, 0);
        assertThat(leaf(leaf).plannerRows()).as("a vacuum set reltuples").isEqualTo(5000);

        int resets = asSuperuser(su -> su.select(DSL.function(DSL.name("pg_catalog",
                    "pg_stat_reset_single_table_counters"), SQLDataType.OTHER, DSL.field(DSL.name("c", "oid"))))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .where(DSL.field(DSL.name("c", "relname"), String.class).eq(leaf)).fetch().size());
        assertThat(resets).as("the reset ran for exactly the one leaf (non-vacuity)").isEqualTo(1);
        awaitStats(leaf, 0, 0);
        assertThat(leaf(leaf).plannerRows()).as("reltuples survives the reset of the counters").isEqualTo(5000);

        deleteRows(tenant, collection, hex.subList(0, 1200));
        awaitStats(leaf, 0, 1200);
        LeafBloatCatalog.Leaf l = leaf(leaf);
        assertThat(l.liveRows()).as("n_live_tup is still 0 after the reset").isZero();
        assertThat(l.deadRows()).isEqualTo(1200);
        assertThat(l.deadRows()).as("over the old live + dead line, which is what made this read overdue")
            .isGreaterThan((long) (LeafBloatCatalog.OVERDUE_BASE_ROWS
                + LeafBloatCatalog.OVERDUE_SCALE_FACTOR * (l.liveRows() + l.deadRows())));
        assertThat(l.deadRows()).as("under autovacuum's real trigger, 1000 + 0.05 x reltuples")
            .isLessThan((long) (1000 + 0.05 * l.plannerRows()));
        assertThat(l.overdue()).as("autovacuum is correctly idle, so not overdue").isFalse();
        assertThat(l.vacuumCount()).as("the reset cleared the counts and nothing has vacuumed since").isZero();
    }

    // -- the cache, not the catalog ----------------------------------------------------------------------

    /** A DataSource that counts the connections borrowed from the one behind it. */
    static final class CountingDataSource implements DataSource {
        final DataSource delegate;
        final AtomicInteger borrowed = new AtomicInteger();

        CountingDataSource(DataSource delegate) {
            this.delegate = delegate;
        }

        @Override public Connection getConnection() throws SQLException {
            borrowed.incrementAndGet();
            return delegate.getConnection();
        }
        @Override public Connection getConnection(String u, String p) throws SQLException {
            borrowed.incrementAndGet();
            return delegate.getConnection(u, p);
        }
        @Override public PrintWriter getLogWriter() throws SQLException { return delegate.getLogWriter(); }
        @Override public void setLogWriter(PrintWriter out) throws SQLException { delegate.setLogWriter(out); }
        @Override public void setLoginTimeout(int seconds) throws SQLException { delegate.setLoginTimeout(seconds); }
        @Override public int getLoginTimeout() throws SQLException { return delegate.getLoginTimeout(); }
        @Override public Logger getParentLogger() throws SQLFeatureNotSupportedException { return delegate.getParentLogger(); }
        @Override public <T> T unwrap(Class<T> iface) throws SQLException { return delegate.unwrap(iface); }
        @Override public boolean isWrapperFor(Class<?> iface) throws SQLException { return delegate.isWrapperFor(iface); }
        @Override public ConnectionBuilder createConnectionBuilder() throws SQLException { return delegate.createConnectionBuilder(); }
    }

    @Test
    void theStatusRouteServesTheCache_andBorrowsNoConnection() throws Exception {
        String tenant = newTenant("route");
        String collection = collectionName("route");
        List<String> hex = rows(tenant, collection, 100);
        String leaf = leafName(tenant);
        deleteRows(tenant, collection, hex.subList(0, 25));
        awaitStats(leaf, 75, 25);

        var counting = new CountingDataSource(svcDs);
        var sweep = new LeafBloatSweep(new LeafBloatCatalog(counting)::read, Duration.ofSeconds(600),
            Clock.systemUTC(), Duration.ofSeconds(30));
        assertThat(sweep.refresh()).isTrue();
        int afterRead = counting.borrowed.get();
        assertThat(afterRead).as("the read itself borrowed a connection (non-vacuity)").isPositive();

        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/status", new StatusHandler(null, null, 0L, null, null, null, null,
            () -> StatusHandler.ChunksLeafBloat.of(sweep.status(), sweep.periodSeconds())));
        server.start();
        JsonNode body;
        try {
            body = null;
            for (int i = 0; i < 3; i++) {
                HttpResponse<String> resp = dev.nexus.service.TestHttp.client().send(
                    dev.nexus.service.TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/status")
                        .GET().build(), HttpResponse.BodyHandlers.ofString());
                assertThat(resp.statusCode()).isEqualTo(200);
                body = MAPPER.readTree(resp.body());
            }
        } finally {
            server.stop(0);
        }

        assertThat(counting.borrowed.get()).as("three status requests borrowed no connection").isEqualTo(afterRead);
        JsonNode bloat = body.get("chunks_leaf_bloat");
        assertThat(bloat).isNotNull();
        // The route is unauthenticated: aggregates only. The leaf this test made is in the engine's cache with its
        // counts, and nowhere in the body.
        LeafBloatSweep.LeafStatus mine = sweep.status().leaves().stream()
            .filter(l -> l.leaf().name().equals(leaf)).findFirst().orElse(null);
        assertThat(mine).as("the tenant's leaf is in the sweep's cache").isNotNull();
        assertThat(mine.leaf().liveRows()).isEqualTo(75);
        assertThat(mine.leaf().deadRows()).isEqualTo(25);
        assertThat(mine.state()).as("25 percent of 100 rows is not overdue").isEqualTo(LeafBloatSweep.OverdueState.NONE);
        assertThat(bloat.has("leaves")).as("no per-leaf list on the wire").isFalse();
        assertThat(bloat.toString()).as("no leaf name on the wire").doesNotContain(leaf).doesNotContain("chunks_");
        assertThat(bloat.get("ever_read").asBoolean()).isTrue();
        assertThat(bloat.get("consecutive_failures").asInt()).isZero();
        assertThat(bloat.get("leaves_total").asInt()).isGreaterThanOrEqualTo(8);
        System.out.println("DXSJC evidence: status leaves_total=" + bloat.get("leaves_total").asInt()
            + " overdue=" + bloat.get("overdue").asInt() + " pinned=" + bloat.get("pinned").asInt()
            + " starved=" + bloat.get("starved").asInt() + " policy_unset=" + bloat.get("policy_unset").asInt()
            + " autovacuum_disabled=" + bloat.get("autovacuum_disabled").asInt()
            + " max_overdue_for_seconds=" + bloat.get("max_overdue_for_seconds").asLong());
    }

}
