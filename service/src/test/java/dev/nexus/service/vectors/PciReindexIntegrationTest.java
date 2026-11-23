// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PciCanary.Result;
import dev.nexus.service.vectors.PciCanary.State;
import dev.nexus.service.vectors.PciCanary.Target;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import java.util.random.RandomGenerator;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-230 Phase 2 (nexus-4lnn1.1.10): the reconciler's REINDEX, end to end against the real partitioned layout, with the
 * recall canary replaced by a scripted runner (the canary's own measurement is {@code PciCanaryTest}'s). Tests 5 and 6
 * of the bead (the router's set during a REINDEX; a migration ending one) are in
 * {@code PciBuilderTerminationIntegrationTest}, where the held snapshot and the migration walk already are.
 *
 * <p>The clock is the real one, moved forward by the test: an anchor's {@code built_at} is compared with the rows'
 * {@code last_written_at}, which the database stamps, so a clock in the past would count every row as written since the
 * build. B is 200.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciReindexIntegrationTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    static final String NONCE = "b1c2d3e5";
    static final int B = 200;
    static final PciSettings ON = new PciSettings(true, B, 600, 16);
    static final PciSettings REINDEX_OFF = new PciSettings(true, B, 600, 16, null, false);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource adminDs;
    PciCatalog catalog;
    final List<String> tenants = new ArrayList<>();
    final ExecutorService async = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "pcireindex-test-async");
        t.setDaemon(true);
        return t;
    });
    ListAppender<ILoggingEvent> appender;
    int generation;

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
        catalog = new PciCatalog(svcDs);
    }

    @AfterAll
    void stopAll() {
        async.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void captureLogs() {
        appender = new ListAppender<>();
        appender.start();
        for (Class<?> c : List.of(PciReconciler.class, PciBuilderSession.class)) {
            var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(c);
            logger.setLevel(Level.DEBUG);
            logger.addAppender(appender);
        }
    }

    @AfterEach
    @SuppressWarnings("deprecation")
    void removeTenants() throws Exception {
        for (Class<?> c : List.of(PciReconciler.class, PciBuilderSession.class)) {
            ((ch.qos.logback.classic.Logger) LoggerFactory.getLogger(c)).detachAppender(appender);
        }
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

    List<String> logs() {
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    long logged(String needle) {
        return logs().stream().filter(l -> l.contains(needle)).count();
    }

    int firstLogIndex(String needle) {
        List<String> all = logs();
        for (int i = 0; i < all.size(); i++) {
            if (all.get(i).contains(needle)) return i;
        }
        return -1;
    }

    /** The real clock, moved forward by {@link #advance}. */
    static final class MovingClock extends Clock {
        private volatile Instant now = Instant.now();

        void advance(Duration d) {
            now = now.plus(d);
        }

        @Override public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override public Instant instant() {
            return now;
        }
    }

    /** The canary: every run ends in {@link #state} with {@link #queries} queries; records what it was asked. */
    static final class ScriptedCanary implements PciCanarySchedule.Runner {
        volatile State state = State.GREEN;
        volatile int queries = 10;
        final List<String> triggers = new java.util.concurrent.CopyOnWriteArrayList<>();
        final List<List<float[]>> remeasuredWith = new java.util.concurrent.CopyOnWriteArrayList<>();
        final List<float[]> sample = List.of(new float[] {1f, 2f, 3f});
        /** The targets of the re-measures, in order. */
        final List<Target> remeasuredTargets = new java.util.concurrent.CopyOnWriteArrayList<>();

        private Result result(Target target, String trigger) {
            boolean red = state == State.RED;
            return new Result(target, trigger, state, null, queries, red ? 0.9 : 1.0, red ? 0.95 : 1.0, red ? 1 : 0,
                Instant.now(), red ? sample : null, null);
        }

        @Override public Result run(Target target, String trigger) {
            triggers.add(trigger);
            return result(target, trigger);
        }

        @Override public Result remeasure(Target target, String trigger, List<float[]> sameQueries) {
            triggers.add(trigger);
            remeasuredWith.add(sameQueries);
            remeasuredTargets.add(target);
            return result(target, trigger);
        }
    }

    PciReconciler reconciler(PciSettings s, Clock clock, ScriptedCanary canary) {
        return reconciler(s, clock, canary, PciReconciler.SET_LOCAL_TENANT);
    }

    PciReconciler reconciler(PciSettings s, Clock clock, ScriptedCanary canary, PciReconciler.TenantBinder binder) {
        PciIndexSweep sweep = PciIndexSweep.create(svcDs, s);
        var builder = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s);
        return new PciReconciler(catalog::read, builder, sweep, s, clock, binder,
            PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60), RandomGenerator.getDefault(), tenantsSeen -> 0,
            new PciCanarySchedule(canary, clock, System::nanoTime));
    }

    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        String tenant = "pcireindex-" + tag;
        tenants.add(tenant);
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true, true);
        }
        return tenant;
    }

    static String name(String tag) {
        return "knowledge__pcireindex-" + tag + "__minilm-l6-v2-384__v1";
    }

    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    /** Register {@code collection} for {@code tenant} with {@code rows} rows; returns their chash hex, in order. */
    List<String> collection(String tenant, String collection, int rows) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            return null;
        });
        return addRows(tenant, collection, rows);
    }

    /** {@code rows} new rows for the collection. */
    List<String> addRows(String tenant, String collection, int rows) throws Exception {
        int gen = ++generation;
        return asSuperuser(su -> {
            List<String> hex = new ArrayList<>();
            Long cid = CollectionIds.require(su, tenant, collection);
            for (int from = 0; from < rows; from += 250) {
                var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                    CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA, CHUNKS.COLLECTION_ID);
                for (int i = from; i < Math.min(rows, from + 250); i++) {
                    String h = Chash.ofText(tenant + "/" + collection + "/" + gen + "/" + i).toHex();
                    hex.add(h);
                    step = step.values(tenant, collection, Chash.fromHex(h).toBytes(), M384, "row " + i,
                        vector(gen * 1000 + i), JSONB.jsonb("{}"), cid);
                }
                step.onConflictDoNothing().execute();
            }
            return hex;
        });
    }

    /** Delete the rows with these chashes. */
    void deleteRows(String tenant, String collection, List<String> hex) throws Exception {
        asSuperuser(su -> su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.eq(collection))
            .and(CHUNKS.CHASH.in(hex.stream().map(h -> Chash.fromHex(h).toBytes()).toList())).execute());
    }

    static Vector vector(int i) {
        float[] v = new float[384];
        v[i % 384] = 1f;
        v[(i * 7 + 1) % 384] += 0.5f;
        return Vector.of(v);
    }

    PciCatalog.Leaf leaf(String tenant) {
        List<PciCatalog.Leaf> hits = catalog.read().leaves().stream()
            .filter(l -> M384.equals(l.model()) && tenant.equals(l.tenant())).toList();
        assertThat(hits).as("exactly one leaf for (%s, %s)", M384, tenant).hasSize(1);
        return hits.get(0);
    }

    PciCatalog.Anchor anchorOf(String tenant, String collection) {
        return catalog.read().anchor(M384, tenant, collection).orElseThrow();
    }

    static void await(BooleanSupplier condition, String what) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("timed out waiting for " + what);
            }
            Thread.sleep(50);
        }
    }

    /** An open REPEATABLE READ transaction with a snapshot and no lock: a REINDEX CONCURRENTLY ends by waiting for it. */
    final class SnapshotHolder implements AutoCloseable {
        private final Connection conn;
        private boolean closed;

        SnapshotHolder() throws Exception {
            conn = pg.createConnection("");
            conn.setAutoCommit(false);
            conn.setTransactionIsolation(Connection.TRANSACTION_REPEATABLE_READ);
            DSL.using(conn, SQLDialect.POSTGRES).selectCount().from(DSL.table(DSL.name("pg_catalog", "pg_class")))
                .fetchOne();
        }

        @Override
        public void close() throws Exception {
            if (!closed) {
                closed = true;
                conn.rollback();
                conn.close();
            }
        }
    }

    void cancelBuilder() throws Exception {
        asSuperuser(su -> su.select(DSL.function("pg_cancel_backend", SQLDataType.BOOLEAN,
                DSL.field(DSL.name("pid"), Integer.class)))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .and(DSL.field(DSL.name("state"), String.class).eq("active"))
            .fetch());
    }

    /** {@code last_vacuum} (a manual VACUUM; autovacuum has its own column) of the tenant's leaf, or null. */
    java.time.OffsetDateTime lastManualVacuum(PciCatalog.Leaf leaf) throws Exception {
        return asSuperuser(su -> su.select(DSL.field(DSL.name("last_vacuum"), java.time.OffsetDateTime.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_user_tables")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq(leaf.schema()))
            .and(DSL.field(DSL.name("relname"), String.class).eq(leaf.name()))
            .fetchOne(0, java.time.OffsetDateTime.class));
    }

    /** The two passes every scenario starts with: the first builds and anchors, the second reads and records. */
    void buildAndSettle(PciReconciler r) {
        r.reconcileOnce();
        r.reconcileOnce();
    }

    String index(String tenant, String collection) {
        return PciCatalog.indexName(M384, tenant, collection);
    }

    // -- the tests ---------------------------------------------------------------------------------------

    /** Test 1, on the substrate: ingest to twice the size writes bytes per row at the build's figure. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void pureIngestToTwiceTheSize_neverReindexes() throws Exception {
        String tenant = newTenant("ingest");
        String coll = name("ingest");
        collection(tenant, coll, 300);
        var r = reconciler(ON, Clock.systemUTC(), new ScriptedCanary());
        buildAndSettle(r);
        assertThat(anchorOf(tenant, coll).rows()).isEqualTo(300);

        addRows(tenant, coll, 300);
        r.reconcileOnce();
        r.reconcileOnce();

        assertThat(r.reindexes()).as("no REINDEX on ingest").isZero();
        assertThat(logged("op=reindex")).isZero();
        assertThat(r.maintenance().reindexes()).isZero();
    }

    /** Test 2, on the substrate: 80 of 300 rows deleted is deletes-since-build, even after a restart; the REINDEX writes a fresh anchor. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void overAQuarterOfTheRowsDeleted_reindexesOnce_afterARestart_writesAFreshAnchor_andMeasuresTheCanaryAgain() throws Exception {
        String tenant = newTenant("churn");
        String coll = name("churn");
        List<String> rows = collection(tenant, coll, 300);
        var canary = new ScriptedCanary();
        var r = reconciler(ON, Clock.systemUTC(), canary);
        buildAndSettle(r);
        PciCatalog.Anchor before = anchorOf(tenant, coll);
        assertThat(before.rows()).isEqualTo(300);
        assertThat(r.maintenance().reindex()).isEqualTo("on");
        int measuredBefore = canary.triggers.size();

        // A restart: the new engine has no previous count, so a bulk delete is not what it sees; the anchor still
        // says over a quarter of the rows it was built from are gone (deletes since build survives a restart). 80 of
        // 300, not 100: a third gone puts the leaf's own index at exactly 1.5x the reference a row, which is the
        // leaf size rule's trigger too (P2.3), and this test is about the one pci_ index.
        deleteRows(tenant, coll, rows.subList(0, 80));
        r = reconciler(ON, Clock.systemUTC(), canary);
        r.reconcileOnce();

        assertThat(r.reindexes()).isEqualTo(1);
        assertThat(logged("pci_ddl_done op=reindex index=" + index(tenant, coll))).isEqualTo(1);
        assertThat(logged("trigger=deletes_since_build")).isGreaterThanOrEqualTo(1);
        assertThat(leaf(tenant).indexes()).filteredOn(i -> index(tenant, coll).equals(i.name()))
            .singleElement().satisfies(i -> assertThat(i.valid()).isTrue());
        PciCatalog.Anchor after = anchorOf(tenant, coll);
        assertThat(after.rows()).as("the fresh anchor counts the rows now").isEqualTo(220);
        assertThat(after.origin()).isEqualTo(PciCatalog.ORIGIN_BUILT);
        assertThat(after.builtAt()).isAfter(before.builtAt());
        assertThat(canary.triggers.subList(measuredBefore, canary.triggers.size()))
            .as("the canary re-measures after the REINDEX").contains("after_reindex");
        var m = r.maintenance();
        assertThat(m.reindexes()).isEqualTo(1);
        assertThat(m.lastReindexAt()).isNotNull();
        assertThat(m.reindexing()).isNull();

        r.reconcileOnce();
        assertThat(r.reindexes()).as("the fresh anchor resets the rules").isEqualTo(1);
    }

    /** Test 3, on the substrate: a 12% fall since the previous check is REINDEX, then VACUUM (ANALYZE) of the leaf. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aBulkDelete_reindexesThenVacuumsTheLeaf() throws Exception {
        String tenant = newTenant("bulk");
        String coll = name("bulk");
        List<String> rows = collection(tenant, coll, 300);
        var r = reconciler(ON, Clock.systemUTC(), new ScriptedCanary());
        buildAndSettle(r);
        assertThat(lastManualVacuum(leaf(tenant))).as("nothing vacuumed it yet").isNull();

        deleteRows(tenant, coll, rows.subList(0, 36));
        r.reconcileOnce();

        assertThat(r.reindexes()).isEqualTo(1);
        assertThat(logged("trigger=bulk_delete")).isGreaterThanOrEqualTo(1);
        int reindexAt = firstLogIndex("pci_ddl_done op=reindex");
        int vacuumAt = firstLogIndex("pci_ddl_done op=vacuum");
        assertThat(reindexAt).isNotNegative();
        assertThat(vacuumAt).as("VACUUM follows the REINDEX").isGreaterThan(reindexAt);
        PciCatalog.Leaf leaf = leaf(tenant);
        await(() -> {
            try {
                return lastManualVacuum(leaf) != null;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }, "the leaf's manual VACUUM to show in the statistics");
    }

    /**
     * Test 4, on the substrate: red with ten queries rebuilds once a day, a day after the build; a still-red re-measure is
     * red_after_reindex; a restart forgets the engine's memory but not the anchor, which is what holds the day.
     */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aRedCanary_reindexesOncePerDay_andAStillRedReMeasureIsReported() throws Exception {
        String tenant = newTenant("red");
        String coll = name("red");
        collection(tenant, coll, 300);
        var canary = new ScriptedCanary();
        canary.state = State.RED;
        var clock = new MovingClock();
        var r = reconciler(ON, clock, canary);
        r.reconcileOnce();   // builds; the canary runs after the build and is red

        r.reconcileOnce();
        assertThat(r.reindexes()).as("an index built a minute ago waits its day").isZero();
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();

        assertThat(r.reindexes()).isEqualTo(1);
        assertThat(logged("trigger=red_canary")).isGreaterThanOrEqualTo(1);
        assertThat(canary.remeasuredWith).as("re-measured with the same in-memory queries").hasSize(1);
        assertThat(canary.remeasuredWith.get(0)).isSameAs(canary.sample);
        assertThat(r.redAfterReindex(index(tenant, coll))).isTrue();
        assertThat(logged("event=pci_red_after_reindex")).isEqualTo(1);
        assertThat(r.maintenance().canary().indexes()).as("shown on the operator entry")
            .singleElement().satisfies(e -> assertThat(e.redAfterReindex()).isTrue());

        r.reconcileOnce();
        clock.advance(Duration.ofHours(23));
        r.reconcileOnce();
        assertThat(r.reindexes()).as("not again inside the day").isEqualTo(1);
        assertThat(r.redAfterReindex(index(tenant, coll))).as("still inside the day").isTrue();

        // A restart: the new engine has no memory of the last red-canary REINDEX. Its canary reads red on its first
        // pass; the anchor, rewritten by the REINDEX, says the index is 23 hours old.
        var restarted = reconciler(ON, clock, canary);
        restarted.reconcileOnce();
        restarted.reconcileOnce();
        assertThat(restarted.reindexes()).as("a persistent red is not rebuilt on every restart").isZero();

        clock.advance(Duration.ofHours(1));
        assertThat(r.redAfterReindex(index(tenant, coll))).as("red_after_reindex expires after a day").isFalse();
        restarted.reconcileOnce();
        assertThat(restarted.reindexes()).as("a day on: the anchor holds the same red on the same ring (red_unfixed)")
            .isZero();
    }

    /** A red_after_reindex, and the red_unfixed in the anchor, clear on a later green run. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aGreenRun_clearsRedAfterReindexAndTheAnchorsRedUnfixed() throws Exception {
        String tenant = newTenant("redgreen");
        String coll = name("redgreen");
        collection(tenant, coll, 300);
        var canary = new ScriptedCanary();
        canary.state = State.RED;
        var clock = new MovingClock();
        var r = reconciler(ON, clock, canary);
        r.reconcileOnce();
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();
        assertThat(r.redAfterReindex(index(tenant, coll))).isTrue();

        assertThat(anchorOf(tenant, coll).redUnfixed()).as("recorded in the anchor").isNotNull();
        canary.state = State.GREEN;
        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();   // the daily run reads green at the end of this pass
        r.reconcileOnce();

        assertThat(r.reindexes()).as("the persisted red held the index until the green run").isEqualTo(1);
        assertThat(anchorOf(tenant, coll).redUnfixed()).as("cleared by the green run").isNull();
        assertThat(r.redAfterReindex(index(tenant, coll))).as("the green run ended it").isFalse();
        assertThat(r.maintenance().canary().indexes()).singleElement()
            .satisfies(e -> assertThat(e.redAfterReindex()).isFalse());
    }

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aRedCanaryOnFewerThanTenQueries_isShownAndNeverReindexes() throws Exception {
        String tenant = newTenant("red9");
        String coll = name("red9");
        collection(tenant, coll, 300);
        var canary = new ScriptedCanary();
        canary.state = State.RED;
        canary.queries = 9;
        var r = reconciler(ON, Clock.systemUTC(), canary);

        r.reconcileOnce();
        r.reconcileOnce();
        r.reconcileOnce();

        assertThat(r.reindexes()).isZero();
        assertThat(r.maintenance().canary().red()).as("the red is still shown").isEqualTo(1);
        assertThat(logged("op=reindex")).isZero();
    }

    /** Test 7: NX_SEARCH_PCI_REINDEX=0 builds and measures, and never reindexes or vacuums. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void withReindexOff_theEngineBuildsAndMeasures_butNeverReindexesOrVacuums() throws Exception {
        String tenant = newTenant("off");
        String coll = name("off");
        List<String> rows = collection(tenant, coll, 300);
        var canary = new ScriptedCanary();
        var r = reconciler(REINDEX_OFF, Clock.systemUTC(), canary);
        buildAndSettle(r);
        assertThat(anchorOf(tenant, coll).rows()).as("it built and anchored").isEqualTo(300);
        assertThat(canary.triggers).as("it measured").contains("after_build");

        deleteRows(tenant, coll, rows.subList(0, 100));   // far past every REINDEX rule
        canary.state = State.RED;
        r.reconcileOnce();
        r.reconcileOnce();

        assertThat(r.reindexes()).isZero();
        assertThat(logged("op=reindex")).isZero();
        assertThat(logged("op=vacuum")).isZero();
        assertThat(r.maintenance().reindex()).isEqualTo("off");
        assertThat(anchorOf(tenant, coll).rows()).as("the anchor is the build's").isEqualTo(300);
    }

    /** A failed REINDEX is charged to RDR-227's build backoff, per index; the next attempt comes after it. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aFailedReindex_getsTheBuildBackoffPerIndex() throws Exception {
        String tenant = newTenant("fail");
        String coll = name("fail");
        List<String> rows = collection(tenant, coll, 300);
        var clock = new MovingClock();
        var r = reconciler(ON, clock, new ScriptedCanary());
        buildAndSettle(r);
        deleteRows(tenant, coll, rows.subList(0, 80));   // under the leaf size rule's 1.5x (P2.3): only the pci_ index runs

        try (SnapshotHolder snapshot = new SnapshotHolder()) {
            CompletableFuture<?> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
            await(() -> leaf(tenant).indexes().stream().anyMatch(i -> !i.valid()), "the REINDEX's invalid copy");
            cancelBuilder();
            snapshot.close();
            pass.get(60, TimeUnit.SECONDS);
        }
        assertThat(logged("pci_ddl_failed op=reindex")).isEqualTo(1);
        assertThat(r.reindexes()).isZero();

        r.reconcileOnce();   // drops the leftover; the index is in its backoff
        assertThat(logged("pci_ddl_done op=reindex")).as("no attempt inside the backoff").isZero();
        assertThat(logged("pci_ddl_failed op=reindex")).isEqualTo(1);

        clock.advance(PciReconciler.FIRST_RETRY.plusMinutes(1));
        r.reconcileOnce();
        assertThat(r.reindexes()).as("after the backoff it runs").isEqualTo(1);
    }

    // -- fix round 1 (review ecc6621a9): bounds, the phase's guard, outcomes that are not charged, the owed anchor -----

    /** Ingest that cannot have made the index a 1.4x bigger one per row costs no count; ingest that can, does. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void ingestBelowTheSizeBound_isNotRecounted_andIngestPastItIs() throws Exception {
        String tenant = newTenant("bound");
        String coll = name("bound");
        collection(tenant, coll, 300);
        var r = reconciler(ON, Clock.systemUTC(), new ScriptedCanary());
        buildAndSettle(r);
        PciCatalog.Leaf leaf = leaf(tenant);
        String idx = index(tenant, coll);
        PciReconciler.IndexReading before = r.reading(leaf, idx);
        assertThat(before.rows()).isEqualTo(300);

        addRows(tenant, coll, 60);   // about a fifth more bytes: a bound of 1.2, under the trigger
        r.reconcileOnce();

        PciReconciler.IndexReading small = r.reading(leaf, idx);
        assertThat(small.bytes()).as("the index grew").isGreaterThan(before.bytes());
        assertThat(small.rows()).as("the stale count stands: no recount").isEqualTo(300);
        assertThat(small.countedAt()).isEqualTo(before.countedAt());

        addRows(tenant, coll, 300);   // more than twice the bytes: the bound reaches the trigger, so it counts
        r.reconcileOnce();

        assertThat(r.reading(leaf, idx).rows()).as("recounted").isEqualTo(660);
        assertThat(r.reindexes()).as("and read as the index it is, not as a bigger one per row").isZero();
    }

    /** A REINDEX phase that throws never fails the pass: the canary still runs. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aThrowingReindexPhase_neverFailsThePass_andTheCanaryStillRuns() throws Exception {
        String tenant = newTenant("throw");
        collection(tenant, name("throw"), 300);
        var canary = new ScriptedCanary();
        var r = reconciler(ON, Clock.systemUTC(), canary);
        r.reindexPhaseHook = () -> {
            throw new IllegalStateException("scripted");
        };

        PciReconciler.PassReport report = r.reconcileOnce();

        assertThat(report.ranToEnd()).isTrue();
        assertThat(report.builds()).isEqualTo(1);
        assertThat(logged("event=pci_reindex_phase_failed")).isEqualTo(1);
        assertThat(canary.triggers).as("the canary ran after the failed phase").contains("after_build");
    }

    void terminateBuilder() throws Exception {
        asSuperuser(su -> su.select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN,
                DSL.field(DSL.name("pid"), Integer.class)))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .and(DSL.field(DSL.name("state"), String.class).eq("active"))
            .fetch());
    }

    /** The builder's backend ended under a REINDEX (a migration walk does this): the statement is not at fault. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aReindexCutOffByALostConnection_isNotChargedToBackoff() throws Exception {
        String tenant = newTenant("lost");
        String coll = name("lost");
        List<String> rows = collection(tenant, coll, 300);
        var r = reconciler(ON, Clock.systemUTC(), new ScriptedCanary());
        buildAndSettle(r);
        deleteRows(tenant, coll, rows.subList(0, 80));

        try (SnapshotHolder snapshot = new SnapshotHolder()) {
            CompletableFuture<?> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
            await(() -> leaf(tenant).indexes().stream().anyMatch(i -> !i.valid()), "the REINDEX's invalid copy");
            terminateBuilder();
            snapshot.close();
            pass.get(60, TimeUnit.SECONDS);
        }

        assertThat(logged("event=pci_reindex_not_done")).isEqualTo(1);
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_reindex_not_done") && l.contains("outcome=CONNECTION_LOST"));
        assertThat(logged("event=pci_reindex_failed")).as("not charged").isZero();
        assertThat(r.reindexes()).isZero();

        r.reconcileOnce();   // the same instant: no backoff to wait out; the leftover goes, the REINDEX runs
        assertThat(r.reindexes()).isEqualTo(1);
        assertThat(logged("event=pci_reindex_failed")).isZero();
    }

    /** The migrator holds its lock when the REINDEX is about to go out: the pass ends and nothing is charged. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aReindexSkippedForAMigration_isNotChargedToBackoff() throws Exception {
        String tenant = newTenant("migr");
        String coll = name("migr");
        List<String> rows = collection(tenant, coll, 300);
        var r = reconciler(ON, Clock.systemUTC(), new ScriptedCanary());
        buildAndSettle(r);
        deleteRows(tenant, coll, rows.subList(0, 80));

        Connection[] held = new Connection[1];
        r.reindexPhaseHook = () -> {
            try {
                held[0] = pg.createConnection("");
                held[0].setAutoCommit(true);
                DSL.using(held[0], SQLDialect.POSTGRES).select(DSL.function("pg_advisory_lock", SQLDataType.OTHER,
                    DSL.val(dev.nexus.service.db.SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY))).fetch();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        };
        try {
            r.reconcileOnce();
        } finally {
            r.reindexPhaseHook = null;
            if (held[0] != null) held[0].close();
        }

        assertThat(logs()).anyMatch(l -> l.contains("event=pci_reindex_not_done")
            && l.contains("outcome=SKIPPED_MIGRATION"));
        assertThat(logged("event=pci_reindex_failed")).as("not charged").isZero();
        assertThat(r.reindexes()).isZero();

        r.reconcileOnce();   // the migration is over: the same instant, no backoff
        assertThat(r.reindexes()).isEqualTo(1);
    }

    /**
     * A REINDEX whose fresh anchor is not written leaves the old anchor, which would fire the deletes rule on
     * every pass: the index is owed its anchor, is no candidate meanwhile, and the next pass retries the write.
     */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aReindexWhoseAnchorIsNotWritten_isOwedIt_andTheNextPassRetries() throws Exception {
        String tenant = newTenant("owed");
        String coll = name("owed");
        collection(tenant, coll, 300);
        var r = reconciler(ON, Clock.systemUTC(), new ScriptedCanary());
        buildAndSettle(r);
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.Index idx = leaf.indexes().stream().filter(i -> i.parsed() && i.valid()).findFirst().orElseThrow();
        PciCatalog.Anchor old = anchorOf(tenant, coll);

        var state = new PciReindexState();
        boolean[] works = {false};
        int[] writes = {0};
        var phase = new PciReindexPhase(state, null, Clock.systemUTC(), (pass, l, dim, build, afterReindex) -> {
            writes[0]++;
            assertThat(afterReindex).isTrue();
            return works[0];
        });
        var reindex = new PciReconcilePlanner.Reindex(idx.name(), coll, PciReconcilePlanner.Trigger.DELETES_SINCE_BUILD,
            1000);
        var subject = new PciReindexState.Subject(leaf, 384, idx,
            new PciReconciler.IndexReading(old, 1000, 300, 0, Instant.now()));
        var builder = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, ON);

        try (PciBuilderSession.Pass pass = builder.open()) {
            assertThat(phase.reindexOne(pass, subject, reindex)).as("the REINDEX itself finished").isTrue();
        }
        assertThat(state.reindexes()).isEqualTo(1);
        assertThat(state.owedAnchors()).as("owed its anchor").containsOnlyKeys(idx.name());
        assertThat(anchorOf(tenant, coll)).as("the old anchor is still on the index").isEqualTo(old);

        try (PciBuilderSession.Pass pass = builder.open()) {
            phase.retryOwedAnchors(pass, java.util.Set.of(idx.name()));   // the retry fails again
            assertThat(state.owedAnchors()).containsOnlyKeys(idx.name());
            works[0] = true;
            phase.retryOwedAnchors(pass, java.util.Set.of(idx.name()));
            assertThat(state.owedAnchors()).as("written: no longer owed").isEmpty();
            phase.retryOwedAnchors(pass, java.util.Set.of(idx.name()));
        }
        assertThat(writes[0]).as("one write at the REINDEX, two retries, none once written").isEqualTo(3);
        assertThat(logged("event=pci_anchor_retried")).isEqualTo(1);
    }

    // -- fix round 2 (confirmation of a4266b8e8): the owed anchor end to end, the guard, the red floor ------------------

    /**
     * A real anchor-write failure, not a fake: the tenant does not bind for the write that follows the REINDEX (the
     * counting transaction reads it back and skips), so the index is left on its old anchor and owed a fresh one. The
     * next pass writes it through the real {@code writeAnchor} (pci_anchor_retried), and the pass whose retry lands does
     * not judge the index from the old anchor its looks still carry: the REINDEX count does not move.
     */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aRealAnchorWriteFailure_isRetriedByTheNextPass_andThatPassDoesNotReindexAgain() throws Exception {
        String tenant = newTenant("owedreal");
        String coll = name("owedreal");
        List<String> rows = collection(tenant, coll, 300);
        var failBind = new java.util.concurrent.atomic.AtomicBoolean();
        PciReconciler.TenantBinder binder = (tx, t) -> {
            if (!failBind.get()) {
                PciReconciler.SET_LOCAL_TENANT.bind(tx, t);
            }
        };
        var r = reconciler(ON, Clock.systemUTC(), new ScriptedCanary(), binder);
        buildAndSettle(r);
        PciCatalog.Anchor old = anchorOf(tenant, coll);
        assertThat(old.rows()).isEqualTo(300);
        deleteRows(tenant, coll, rows.subList(0, 80));
        r.reindexPhaseHook = () -> failBind.set(true);   // after this pass's leaf counting, before the anchor write

        r.reconcileOnce();

        assertThat(r.reindexes()).isEqualTo(1);
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_anchor_not_written")
            && l.contains("the next pass retries it"));
        assertThat(anchorOf(tenant, coll)).as("still the old anchor").isEqualTo(old);

        failBind.set(false);
        r.reindexPhaseHook = null;
        r.reconcileOnce();

        assertThat(logged("event=pci_anchor_retried")).as("the retry went through the real write").isEqualTo(1);
        assertThat(anchorOf(tenant, coll).rows()).as("the fresh anchor counts the rows now").isEqualTo(220);
        assertThat(anchorOf(tenant, coll).builtAt()).isAfter(old.builtAt());
        assertThat(r.reindexes()).as("the old anchor in this pass's looks fires nothing").isEqualTo(1);
        assertThat(logged("event=pci_anchor_rebuilt_detected")).as("the engine's own REINDEX is no hand rebuild").isZero();

        r.reconcileOnce();
        assertThat(r.reindexes()).as("and the fresh anchor judges it from here on").isEqualTo(1);
    }

    /**
     * The second COMMENT of a still-red REINDEX throws: the failure is its own event, never charged to the canary, and
     * the REINDEX, its anchor and the pass stand (nexus-4lnn1.1.10).
     */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aRedMarkerThatThrows_isLoggedAsItsOwnEvent_andNotAsACanaryFailure() throws Exception {
        String tenant = newTenant("redmarkfail");
        String coll = name("redmarkfail");
        collection(tenant, coll, 300);
        var canary = new ScriptedCanary();
        canary.state = State.RED;
        var r = reconciler(ON, Clock.systemUTC(), canary);
        buildAndSettle(r);
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.Index idx = leaf.indexes().stream().filter(i -> i.parsed() && i.valid()).findFirst().orElseThrow();
        var schedule = new PciCanarySchedule(canary, Clock.systemUTC(), System::nanoTime);
        schedule.remeasure(new PciCanary.Target(PciReconciler.leafId(leaf), tenant, M384, coll, idx.name()), null);
        var state = new PciReindexState();
        var phase = new PciReindexPhase(state, schedule, Clock.systemUTC(), (pass, l, dim, build, after) -> true,
            (pass, l, index, collection, at, queries) -> {
                throw new IllegalStateException("the marker failed");
            });
        var reindex = new PciReconcilePlanner.Reindex(idx.name(), coll, PciReconcilePlanner.Trigger.RED_CANARY, 1000);
        var subject = new PciReindexState.Subject(leaf, 384, idx,
            new PciReconciler.IndexReading(anchorOf(tenant, coll), 1000, 300, 0, Instant.now()));
        long canaryFailures = logged("event=pci_canary_failed");

        try (PciBuilderSession.Pass pass = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, ON)
            .open()) {
            assertThat(phase.reindexOne(pass, subject, reindex)).as("the REINDEX finished").isTrue();
        }

        assertThat(logged("event=pci_red_unfixed_failed")).isEqualTo(1);
        assertThat(logged("event=pci_canary_failed")).as("not charged to the canary").isEqualTo(canaryFailures);
        assertThat(logged("event=pci_red_after_reindex")).as("the still-red re-measure was reported").isEqualTo(1);
    }

    private PciReindexPhase.Anchorer never() {
        return (pass, l, dim, build, afterReindex) -> {
            throw new IllegalStateException("the builder pass is over: CONNECTION_LOST");
        };
    }

    /** A pass that ended between the liveness check and the anchor's transaction throws: the index is owed, not lost. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void anAnchorWriteThatThrows_isOwed_andNeverEscapesThePhase() throws Exception {
        String tenant = newTenant("owedthrow");
        String coll = name("owedthrow");
        collection(tenant, coll, 300);
        var r = reconciler(ON, Clock.systemUTC(), new ScriptedCanary());
        buildAndSettle(r);
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.Index idx = leaf.indexes().stream().filter(i -> i.parsed() && i.valid()).findFirst().orElseThrow();
        var state = new PciReindexState();
        var phase = new PciReindexPhase(state, null, Clock.systemUTC(), never());
        var reindex = new PciReconcilePlanner.Reindex(idx.name(), coll, PciReconcilePlanner.Trigger.DELETES_SINCE_BUILD,
            1000);
        var subject = new PciReindexState.Subject(leaf, 384, idx,
            new PciReconciler.IndexReading(anchorOf(tenant, coll), 1000, 300, 0, Instant.now()));
        var builder = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, ON);

        try (PciBuilderSession.Pass pass = builder.open()) {
            assertThat(phase.reindexOne(pass, subject, reindex)).as("the REINDEX finished").isTrue();
            assertThat(state.owedAnchors()).as("owed, and nothing escaped").containsOnlyKeys(idx.name());
            assertThat(phase.retryOwedAnchors(pass, java.util.Set.of(idx.name()))).as("a retry that throws is none")
                .isEmpty();
            assertThat(state.owedAnchors()).containsOnlyKeys(idx.name());
        }
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_anchor_not_written") && l.contains("reason=error"));
    }

    /**
     * A red that did not meet the ten-query floor is shown and triggers nothing; it also must not stamp the 24 hour limit
     * or arm {@code red_after_reindex} against an index a different trigger rebuilt.
     */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aRedBelowTheTenQueryFloor_isNotCountedAsTheCanaryBeingRed_whenAnotherTriggerReindexes() throws Exception {
        String tenant = newTenant("floor");
        String coll = name("floor");
        collection(tenant, coll, 300);
        var r = reconciler(ON, Clock.systemUTC(), new ScriptedCanary());
        buildAndSettle(r);
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.Index idx = leaf.indexes().stream().filter(i -> i.parsed() && i.valid()).findFirst().orElseThrow();
        var target = new PciCanary.Target(PciReconciler.leafId(leaf), tenant, M384, coll, idx.name());
        var reading = new PciReconciler.IndexReading(anchorOf(tenant, coll), 1000, 300, 0, Instant.now());
        var subject = new PciReindexState.Subject(leaf, 384, idx, reading);
        var reindex = new PciReconcilePlanner.Reindex(idx.name(), coll, PciReconcilePlanner.Trigger.SIZE, 1000);
        var builder = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, ON);

        for (int queries : new int[] {9, 10}) {
            var runner = new ScriptedCanary();
            runner.state = State.RED;
            runner.queries = queries;
            var schedule = new PciCanarySchedule(runner, Clock.systemUTC(), System::nanoTime);
            schedule.remeasure(target, null);   // the canary's latest result: red on `queries` queries
            var state = new PciReindexState();
            var phase = new PciReindexPhase(state, schedule, Clock.systemUTC(), (pass, l, dim, b, after) -> true);

            try (PciBuilderSession.Pass pass = builder.open()) {
                assertThat(phase.reindexOne(pass, subject, reindex)).isTrue();
            }

            var candidate = state.candidates(List.of(new PciReindexState.Look(leaf, 384, java.util.Map.of(idx.name(),
                reading))), java.util.Set.of(), java.util.Set.of(idx.name()), n -> null, Instant.now(),
                new java.util.HashMap<>()).get(0);
            if (queries >= PciReindexPolicy.RED_CANARY_MIN_QUERIES) {
                assertThat(candidate.lastRedReindexAt()).as("red on %d queries", queries).isNotNull();
                assertThat(state.redAfterReindex(idx.name(), Instant.now())).isTrue();
            } else {
                assertThat(candidate.lastRedReindexAt()).as("red on %d queries", queries).isNull();
                assertThat(state.redAfterReindex(idx.name(), Instant.now())).isFalse();
            }
        }
    }

    /** RDR-229 P3.4 (critic S2): an id index's REINDEX is re-measured with an id target once the canary follows the latch (not before); a still-red one is reported and marked. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void anIdIndexReindex_isReMeasuredWithAnIdTarget_onceTheCanaryFollowsTheLatch_andAStillRedOneIsReportedAndMarked() throws Exception {
        String tenant = newTenant("idremeasure");
        String coll = name("idremeasure");
        collection(tenant, coll, 300);
        long id = asSuperuser(su -> CollectionIds.require(su, tenant, coll));
        var canary = new ScriptedCanary();
        canary.state = State.RED;
        var schedule = new PciCanarySchedule(canary, Clock.systemUTC(), System::nanoTime);
        try (var pass = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, ON).open()) {
            assertThat(pass.buildById(leaf(tenant), 384, id)).isEqualTo(PciBuilderSession.DdlOutcome.DONE);
        }
        PciCatalog.Leaf leaf = leaf(tenant);
        var idx = leaf.indexes().stream().filter(i -> i.idParsed() && i.valid()).findFirst().orElseThrow();
        var target = new PciCanary.Target(PciReconciler.leafId(leaf), tenant, M384, coll, idx.name(), id);
        schedule.remeasure(target, null);   // the canary's latest result for the index: red on ten queries
        var reindex = new PciReconcilePlanner.Reindex(idx.name(), coll, PciReconcilePlanner.Trigger.RED_CANARY, 1000);
        var subject = new PciReindexState.Subject(leaf, 384, idx, new PciReconciler.IndexReading(null, 1000, 300, 0, Instant.now()));
        var marked = new java.util.concurrent.atomic.AtomicInteger();
        PciReindexPhase.RedMarker marker = (pass, l, index, collection, at, queries) -> marked.incrementAndGet() > 0;
        int before = canary.remeasuredTargets.size();

        // control: before the latch (the canary does not measure id indexes) the REINDEX is not re-measured
        var phase = new PciReindexPhase(new PciReindexState(), schedule, Clock.systemUTC(), (pass, l, dim, build, after) -> true, marker);
        phase.idCanaryWhen(() -> false);
        try (PciBuilderSession.Pass pass = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, ON).open()) {
            assertThat(phase.reindexOne(pass, subject, reindex)).isTrue();
        }
        assertThat(canary.remeasuredTargets).as("not re-measured before the latch").hasSize(before);
        assertThat(marked).hasValue(0);

        // latched: re-measured with the id target; still red, so reported and marked
        long reported = logged("event=pci_red_after_reindex");   // latched next
        phase.idCanaryWhen(() -> true);
        try (PciBuilderSession.Pass pass = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, ON).open()) {
            assertThat(phase.reindexOne(pass, subject, reindex)).isTrue();
        }
        assertThat(canary.remeasuredTargets).hasSize(before + 1);
        assertThat(canary.remeasuredTargets.get(before)).as("an id target on the id index").extracting(PciCanary.Target::collectionId,
            PciCanary.Target::index).containsExactly(id, idx.name());
        assertThat(logged("event=pci_red_after_reindex")).as("the still-red re-measure of an id index is reported").isEqualTo(reported + 1);
        assertThat(marked).as("and marked red_unfixed").hasValue(1);
    }
}
