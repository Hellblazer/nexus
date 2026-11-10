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
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PciBuilderSession.BuilderState;
import dev.nexus.service.vectors.PciReconciler.PassReport;
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
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: an engine restart with Postgres left up (a supervisor respawning the engine, {@code nx daemon service
 * stop} without {@code --with-pg}, the upgrade finish's restart) no longer throws away the index build in flight.
 *
 * <p>The old engine's builder session runs a {@code CREATE INDEX CONCURRENTLY} and then loses its client (the engine
 * process is gone: the pass's connection is aborted at the protocol level, the socket closed without waiting for the
 * statement). The backend does not notice until the statement ends, so the build runs on and the session keeps its advisory lock.
 * The next engine therefore finds the builder lock held: its pass reports standby, reads nothing, drops nothing and
 * starts no parallel build. When the build ends the backend exits and frees the lock; the next pass finds a valid
 * index with no anchor and adopts it (a count, and an anchor written on it), and carries on down the queue.
 *
 * <p>The build is held open by a transaction holding ROW EXCLUSIVE on the leaf; the {@code CREATE INDEX CONCURRENTLY}
 * has by then committed its invalid catalog entry and waits for that transaction.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciEngineRestartKeepsBuildIntegrationTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    static final String OLD_NONCE = "01d0e001";
    static final String NEW_NONCE = "4e30e002";
    static final int B = 200;
    static final PciSettings SETTINGS = new PciSettings(true, B, 600, 16);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PciCatalog catalog;
    String tenant;
    ListAppender<ILoggingEvent> appender;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startMigratedByOwner();
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
        if (svcDs != null) svcDs.close();
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
    void detachAndRemoveTheTenantsLeaves() throws Exception {
        for (Class<?> c : List.of(PciReconciler.class, PciBuilderSession.class)) {
            ((ch.qos.logback.classic.Logger) LoggerFactory.getLogger(c)).detachAppender(appender);
        }
        // A pass works every leaf of the database and reports the sum, so one test's leftovers are another's counts.
        if (tenant != null) {
            try (Connection owner = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
                owner.setAutoCommit(true);
                dev.nexus.service.jooq.nexus.Routines.dropTenantPartitions(
                    DSL.using(owner, SQLDialect.POSTGRES).configuration(), tenant);
            }
            tenant = null;
        }
    }

    long logged(String needle) {
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).filter(l -> l.contains(needle)).count();
    }

    // -- fixtures ----------------------------------------------------------------------------------------

    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    static String name(String tag) {
        return "knowledge__pcirestart-" + tag + "__minilm-l6-v2-384__v1";
    }

    void collection(String collection, int rows) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            for (int from = 0; from < rows; from += 250) {
                var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                    CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA);
                for (int i = from; i < Math.min(rows, from + 250); i++) {
                    float[] v = new float[384];
                    v[i % 384] = 1f;
                    v[(i * 7 + 1) % 384] += 0.5f;
                    step = step.values(tenant, collection,
                        Chash.fromHex(Chash.ofText(tenant + "/" + collection + "/" + i).toHex()).toBytes(), M384,
                        "row " + i, Vector.of(v), JSONB.jsonb("{}"));
                }
                step.execute();
            }
            return null;
        });
    }

    /** The tenant's leaves, created by the schema owner (production's leaf owner, and the builder's role). */
    @SuppressWarnings("deprecation")
    void newTenantLeaves() throws Exception {
        try (Connection owner = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true);
        }
    }

    PciBuilderSession session(String nonce) {
        return new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, nonce, SETTINGS);
    }

    PciReconciler reconciler(String nonce) {
        return new PciReconciler(catalog, session(nonce), PciIndexSweep.create(svcDs, SETTINGS), SETTINGS,
            Clock.systemUTC(), PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60));
    }

    PciCatalog.Leaf leaf() {
        List<PciCatalog.Leaf> hits = catalog.read().leaves().stream()
            .filter(l -> M384.equals(l.model()) && tenant.equals(l.tenant())).toList();
        assertThat(hits).hasSize(1);
        return hits.get(0);
    }

    PciCatalog.Index index(String indexName) {
        return leaf().indexes().stream().filter(i -> indexName.equals(i.name())).findFirst().orElse(null);
    }

    long indexOid(PciCatalog.Leaf leaf, String indexName) throws Exception {
        return asSuperuser(su -> su.select(DSL.field(DSL.name("c", "oid")).cast(SQLDataType.BIGINT))
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(leaf.schema()))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(indexName))
            .fetchOne(0, Long.class));
    }

    long backendsNamed(String applicationName) throws Exception {
        return asSuperuser(su -> su.selectCount().from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class).eq(applicationName))
            .fetchOne(0, Long.class));
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

    /** An open transaction holding ROW EXCLUSIVE on the leaf: a concurrent build of it waits for this to end. */
    final class LeafWriter implements AutoCloseable {
        private final Connection conn;
        private boolean closed;

        LeafWriter(PciCatalog.Leaf leaf) throws Exception {
            this(leaf, PgContainerHelper.LockMode.ROW_EXCLUSIVE);
        }

        LeafWriter(PciCatalog.Leaf leaf, PgContainerHelper.LockMode mode) throws Exception {
            conn = pg.createConnection("");
            conn.setAutoCommit(false);
            PgContainerHelper.lockTable(conn, DSL.table(DSL.name(leaf.schema(), leaf.name())), mode);
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

    /**
     * The engine process dying with its builder session mid-build: the pass's connection is aborted (the socket is
     * closed at once, as the OS does for a dead process), without the statement being waited for or signalled.
     */
    static void engineDies(PciBuilderSession.Pass pass) throws Exception {
        var field = PciBuilderSession.Pass.class.getDeclaredField("conn");
        field.setAccessible(true);
        // Protocol-level abort: the socket is closed with no Terminate message and without waiting for the statement,
        // as when the process dies. (Connection.abort needs a permission check the JDK no longer supports.)
        ((Connection) field.get(pass)).unwrap(org.postgresql.jdbc.PgConnection.class).getQueryExecutor().abort();
    }

    // -- tests -------------------------------------------------------------------------------------------

    @Test
    @Timeout(value = 240, unit = TimeUnit.SECONDS)
    void aBuildInFlightWhenTheEngineDies_isNotRebuilt_theNextEngineStandsBy_thenAdoptsIt_andGoesOnDownTheQueue()
            throws Exception {
        tenant = "pcirestart-1";
        newTenantLeaves();
        String big = name("big");
        String other = name("other");
        collection(big, 2 * B + 50);
        collection(other, B + 10);
        String bigIndex = PciCatalog.indexName(M384, tenant, big);
        String otherIndex = PciCatalog.indexName(M384, tenant, other);
        PciCatalog.Leaf leaf = leaf();
        String oldBuilder = PciBuilderSession.builderApplicationName(OLD_NONCE);

        // The old engine: a build in flight, held open.
        PciBuilderSession.Pass oldPass = session(OLD_NONCE).open();
        long oid;
        try (LeafWriter writer = new LeafWriter(leaf)) {
            CompletableFuture<PciBuilderSession.DdlOutcome> build =
                CompletableFuture.supplyAsync(() -> oldPass.build(leaf, 384, big));
            await(() -> index(bigIndex) != null && !index(bigIndex).valid(), "the build's invalid index");
            oid = indexOid(leaf, bigIndex);

            engineDies(oldPass);
            assertThat(build.get(30, TimeUnit.SECONDS)).as("the engine's pass is over")
                .isEqualTo(PciBuilderSession.DdlOutcome.CONNECTION_LOST);
            assertThat(backendsNamed(oldBuilder)).as("the build's backend runs on in Postgres").isEqualTo(1);

            // The next engine, while the build is still running.
            PassReport standby = reconciler(NEW_NONCE).reconcileOnce();

            assertThat(standby.state()).as("the builder lock is held by the orphaned build").isEqualTo(BuilderState.STANDBY);
            assertThat(standby.drops()).as("nothing dropped").isZero();
            assertThat(standby.builds()).as("no parallel build").isZero();
            assertThat(index(bigIndex)).as("the index is still there, building").isNotNull();
            assertThat(index(bigIndex).valid()).isFalse();
            assertThat(indexOid(leaf, bigIndex)).as("the same index, not a rebuilt one").isEqualTo(oid);
            assertThat(backendsNamed(oldBuilder)).as("still running").isEqualTo(1);
        }

        // The build ends; its backend finds its client gone and exits, freeing the builder lock.
        await(() -> index(bigIndex).valid(), "the orphaned build to finish valid");
        await(() -> {
            try {
                return backendsNamed(oldBuilder) == 0;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }, "the orphaned backend to exit");
        assertThat(index(bigIndex).anchor()).as("the orphaned build wrote no anchor").isEmpty();

        // The next engine's next pass: adopts the finished index and builds the next collection in the queue.
        PassReport adopting = reconciler(NEW_NONCE).reconcileOnce();

        assertThat(adopting.state()).isEqualTo(BuilderState.OK);
        assertThat(adopting.drops()).as("the finished index is not dropped").isZero();
        assertThat(adopting.builds()).as("only the next collection is built").isEqualTo(1);
        assertThat(indexOid(leaf, bigIndex)).as("never rebuilt: the index is the one the old engine built")
            .isEqualTo(oid);
        PciCatalog.Anchor anchor = index(bigIndex).anchor().orElseThrow();
        assertThat(anchor.origin()).isEqualTo("adopted");
        assertThat(anchor.rows()).as("counted").isEqualTo(2 * B + 50);
        assertThat(logged("event=pci_anchor_adopted reason=missing")).isEqualTo(1);
        assertThat(index(otherIndex).valid()).isTrue();

        // And the pass after that has nothing to do.
        long adoptedBefore = logged("event=pci_anchor_adopted");
        PassReport quiet = reconciler(NEW_NONCE).reconcileOnce();

        assertThat(quiet.drops()).isZero();
        assertThat(quiet.builds()).isZero();
        assertThat(logged("event=pci_anchor_adopted")).as("nothing adopted twice").isEqualTo(adoptedBefore);
        assertThat(indexOid(leaf, bigIndex)).isEqualTo(oid);
    }

    /** An INVALID index of the builder's name with no backend behind it, as after Postgres itself was restarted. */
    String failedIndex(PciCatalog.Leaf leaf, String collection) throws Exception {
        String index = PciCatalog.indexName(M384, tenant, collection);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.createLeafHnswIndex(su, leaf.schema(), leaf.name(), index, "embedding_384", collection);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.markIndexInvalid(su, leaf.schema(), index);
        }
        return index;
    }

    /**
     * Case D: a build that no other backend is running (Postgres was restarted, or the build failed) cannot be
     * resumed. The pass drops it and builds it again in the SAME pass (nexus-f3yqd F), not a sweep period later.
     */
    @Test
    @Timeout(value = 240, unit = TimeUnit.SECONDS)
    void aFailedIndexNoBackendIsBuilding_isDroppedAndRebuiltInTheSamePass() throws Exception {
        tenant = "pcirestart-3";
        newTenantLeaves();
        String coll = name("failed");
        collection(coll, B + 40);
        PciCatalog.Leaf leaf = leaf();
        String index = failedIndex(leaf, coll);
        long oid = indexOid(leaf, index);
        assertThat(index(index).valid()).isFalse();

        PassReport pass = reconciler(NEW_NONCE).reconcileOnce();

        assertThat(pass.drops()).as("the failed index is dropped").isEqualTo(1);
        assertThat(pass.builds()).as("and built again by the same pass").isEqualTo(1);
        assertThat(index(index).valid()).isTrue();
        assertThat(indexOid(leaf, index)).as("a new index").isNotEqualTo(oid);
        assertThat(index(index).anchor().orElseThrow().origin()).isEqualTo("built");
    }

    /**
     * The guard on the above: when the drop cannot run (the 5 s lock wait ends) the build is not sent, because
     * {@code CREATE INDEX CONCURRENTLY IF NOT EXISTS} would no-op on the failed index and report a build.
     */
    @Test
    @Timeout(value = 240, unit = TimeUnit.SECONDS)
    void aFailedIndexWhoseDropCannotRun_isNotBuiltOver_andTheNextPassDoesBoth() throws Exception {
        tenant = "pcirestart-4";
        newTenantLeaves();
        String coll = name("stuck");
        collection(coll, B + 40);
        PciCatalog.Leaf leaf = leaf();
        String index = failedIndex(leaf, coll);
        long oid = indexOid(leaf, index);

        try (LeafWriter blocker = new LeafWriter(leaf, PgContainerHelper.LockMode.SHARE_UPDATE_EXCLUSIVE)) {
            PassReport stuck = reconciler(NEW_NONCE).reconcileOnce();

            assertThat(stuck.drops()).as("the drop timed out").isZero();
            assertThat(stuck.builds()).as("no build over a failed index").isZero();
            assertThat(logged("event=pci_build_skipped reason=failed_index_not_dropped")).isEqualTo(1);
            assertThat(index(index).valid()).isFalse();
            assertThat(indexOid(leaf, index)).isEqualTo(oid);
        }

        PassReport next = reconciler(NEW_NONCE).reconcileOnce();

        assertThat(next.drops()).isEqualTo(1);
        assertThat(next.builds()).isEqualTo(1);
        assertThat(index(index).valid()).isTrue();
    }

    /** The control: a valid, anchored index after a restart is left alone. */
    @Test
    @Timeout(value = 240, unit = TimeUnit.SECONDS)
    void aValidAnchoredIndex_afterARestart_isLeftAlone() throws Exception {
        tenant = "pcirestart-2";
        newTenantLeaves();
        String big = name("ctl");
        collection(big, B + 40);
        String bigIndex = PciCatalog.indexName(M384, tenant, big);

        PassReport first = reconciler(OLD_NONCE).reconcileOnce();
        assertThat(first.builds()).isEqualTo(1);
        PciCatalog.Leaf leaf = leaf();
        long oid = indexOid(leaf, bigIndex);
        String comment = index(bigIndex).comment();
        assertThat(index(bigIndex).anchor().orElseThrow().origin()).isEqualTo("built");

        PassReport afterRestart = reconciler(NEW_NONCE).reconcileOnce();

        assertThat(afterRestart.state()).isEqualTo(BuilderState.OK);
        assertThat(afterRestart.drops()).isZero();
        assertThat(afterRestart.builds()).isZero();
        assertThat(indexOid(leaf, bigIndex)).isEqualTo(oid);
        assertThat(index(bigIndex).comment()).as("the anchor is as the build wrote it").isEqualTo(comment);
        assertThat(logged("event=pci_anchor_adopted")).isZero();
    }
}
