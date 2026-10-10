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
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantScope;
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
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-227 Step 2 (nexus-43ulx.19): {@link PciReconciler}, the DDL half, against the real partitioned layout in
 * production's shape: a DEDICATED container migrated by a non-superuser schema owner that the reconciler connects as
 * (so {@code nexus.chunks} is FORCE ROW LEVEL SECURITY to it, and a count taken without the tenant reads nothing).
 * Seeding goes through the container superuser, which bypasses row-level security; each test uses tenants of its own
 * and removes their leaves afterwards, so a pass over "every leaf" only ever sees the test's own data. B is 200.
 *
 * <p>A build that has to be held open (to kill it, or to race a second reconciler against it) is held by an
 * uncommitted insert into the tenant's leaf: its RowExclusiveLock conflicts with the ShareUpdateExclusiveLock wait
 * of {@code CREATE INDEX CONCURRENTLY}, which has by then committed its invalid catalog entry. The held session is
 * opened BEFORE the pass and closed before anything else needs the leaf.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciReconcilerIntegrationTest {

    static final String ADMIN_ROLE = "pcirec_admin";
    static final String ADMIN_PASS = "pcirec_admin_pass";
    static final String M384 = "minilm-l6-v2-384";
    static final String NONCE = "b1c2d3e4";
    static final int B = 200;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource adminDs;
    PciCatalog catalog;
    final List<String> tenants = new ArrayList<>();
    final ExecutorService async = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "pcirec-test-async");
        t.setDaemon(true);
        return t;
    });
    ListAppender<ILoggingEvent> appender;
    int generation;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var adminCfg = new HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        adminDs = new HikariDataSource(adminCfg);
        SchemaMigrator.migrate(adminDs);
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
    void removeTenants() throws Exception {
        for (Class<?> c : List.of(PciReconciler.class, PciBuilderSession.class)) {
            ((ch.qos.logback.classic.Logger) LoggerFactory.getLogger(c)).detachAppender(appender);
        }
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            DSLContext ctx = DSL.using(owner, SQLDialect.POSTGRES);
            for (String tenant : tenants) {
                dropLeaves(ctx, tenant);
            }
        }
        tenants.clear();
        PgSession.resetSearchExactMaxRowsForTests();
    }

    @SuppressWarnings("deprecation")
    static void dropLeaves(DSLContext ctx, String tenant) {
        dev.nexus.service.jooq.nexus.Routines.dropTenantPartitions(ctx.configuration(), tenant);
    }

    List<String> logs() {
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    long logged(String needle) {
        return logs().stream().filter(l -> l.contains(needle)).count();
    }

    // -- fixtures ----------------------------------------------------------------------------------------

    static PciSettings settings(int maxPerLeaf) {
        return new PciSettings(true, B, 600, maxPerLeaf);
    }

    static final class MutableClock extends Clock {
        private volatile Instant now = Instant.parse("2026-10-10T00:00:00Z");

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

    PciBuilderSession builderSession(PciSettings s) {
        return new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s);
    }

    PciReconciler reconciler(PciSettings s, PciIndexSweep sweep, Clock clock) {
        return new PciReconciler(catalog, builderSession(s), sweep, s, clock, PciReconciler.SET_LOCAL_TENANT,
            PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60));
    }

    PciReconciler reconciler(PciSettings s, PciIndexSweep sweep) {
        return reconciler(s, sweep, Clock.systemUTC());
    }

    PciIndexSweep sweep(PciSettings s) {
        return PciIndexSweep.create(svcDs, s);
    }

    /** A tenant with its leaves created by the schema owner (production's leaf owner). */
    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        String tenant = "pcirec-" + tag;
        tenants.add(tenant);
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true);
        }
        return tenant;
    }

    static String name(String tag) {
        return "knowledge__pcirec-" + tag + "__minilm-l6-v2-384__v1";
    }

    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    /** Register {@code collection} for {@code tenant} and give it {@code rows} rows; returns their chash hex. */
    List<String> collection(String tenant, String collection, int rows) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            return null;
        });
        return setRows(tenant, collection, rows);
    }

    /** Replace the collection's rows with {@code rows} new ones. */
    List<String> setRows(String tenant, String collection, int rows) throws Exception {
        int gen = ++generation;
        return asSuperuser(su -> {
            su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection)).execute();
            List<String> hex = new ArrayList<>();
            for (int from = 0; from < rows; from += 250) {
                var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                    CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA);
                for (int i = from; i < Math.min(rows, from + 250); i++) {
                    String h = Chash.ofText(tenant + "/" + collection + "/" + gen + "/" + i).toHex();
                    hex.add(h);
                    step = step.values(tenant, collection, Chash.fromHex(h).toBytes(), M384, "row " + i,
                        vector(i), JSONB.jsonb("{}"));
                }
                step.onConflictDoNothing().execute();
            }
            return hex;
        });
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

    Set<String> validIndexed(String tenant) {
        Set<String> out = new HashSet<>();
        for (PciCatalog.Index i : leaf(tenant).indexes()) {
            if (i.parsed() && i.valid()) out.add(i.collection());
        }
        return out;
    }

    boolean hasInvalid(String tenant) {
        return leaf(tenant).indexes().stream().anyMatch(i -> !i.valid());
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

    /** An open transaction holding a RowExclusiveLock on the tenant's leaf: it holds a concurrent build open. */
    final class HeldWrite implements AutoCloseable {
        private final Connection conn;
        private boolean closed;

        HeldWrite(String tenant) throws Exception {
            String held = name("held-" + tenant);
            asSuperuser(su -> {
                PgContainerHelper.insertCollection(su, tenant, held, M384);
                return null;
            });
            conn = pg.createConnection("");
            conn.setAutoCommit(false);
            DSL.using(conn, SQLDialect.POSTGRES)
                .insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                    CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA)
                .values(tenant, held, Chash.fromHex(Chash.ofText("held/" + tenant).toHex()).toBytes(), M384, "held",
                    vector(1), JSONB.jsonb("{}"))
                .execute();
        }

        @Override
        public void close() throws Exception {
            if (closed) {
                return;
            }
            closed = true;
            conn.rollback();
            conn.close();
        }
    }

    void terminateBuilder() throws Exception {
        asSuperuser(su -> su.select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN,
                DSL.field(DSL.name("pid"), Integer.class)))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .fetch());
    }

    CompletableFuture<PassReport> inBackground(PciReconciler r) {
        return CompletableFuture.supplyAsync(r::reconcileOnce, async);
    }

    /** Hold a build open, then kill the builder's backend: one failed build, an invalid index left behind. */
    PassReport failOneBuild(String tenant, PciReconciler r) throws Exception {
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = inBackground(r);
            await(() -> hasInvalid(tenant), "the concurrent build's invalid index");
            terminateBuilder();
            held.close();
            return pass.get(60, TimeUnit.SECONDS);
        }
    }

    // -- lifecycle ---------------------------------------------------------------------------------------

    @Test
    void reachingB_builds_andTheRouterSetFollowsWithoutWaitingForTheNextRead() throws Exception {
        String tenant = newTenant("life");
        collection(tenant, name("c0"), 0);
        collection(tenant, name("half-1"), B / 2 - 1);
        collection(tenant, name("half"), B / 2);
        collection(tenant, name("b-1"), B - 1);
        collection(tenant, name("b"), B);
        collection(tenant, name("big"), 2 * B + 50);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);

        PassReport report = reconciler(s, sweep).reconcileOnce();

        assertThat(report.state()).isEqualTo(BuilderState.OK);
        assertThat(report.ranToEnd()).isTrue();
        assertThat(report.builds()).isEqualTo(2);
        assertThat(validIndexed(tenant)).containsExactlyInAnyOrder(name("b"), name("big"));
        // The sweep was never started: only the refresh after each build can have put these in its set.
        assertThat(sweep.hasValidIndex(M384, tenant, name("b"))).isTrue();
        assertThat(sweep.hasValidIndex(M384, tenant, name("big"))).isTrue();
        assertThat(sweep.hasValidIndex(M384, tenant, name("b-1"))).isFalse();

        // One more row takes B-1 to B.
        setRows(tenant, name("b-1"), B);
        PassReport second = reconciler(s, sweep).reconcileOnce();
        assertThat(second.builds()).isEqualTo(1);
        assertThat(validIndexed(tenant)).contains(name("b-1")).hasSize(3);
    }

    @Test
    void fallingBelowHalfB_drops_butBetweenHalfAndB_theIndexStays_andTheRouterSetFollowsTheDrop() throws Exception {
        String tenant = newTenant("hyst");
        collection(tenant, name("shrink"), B);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        reconciler(s, sweep).reconcileOnce();
        assertThat(validIndexed(tenant)).containsExactly(name("shrink"));

        setRows(tenant, name("shrink"), B / 2);          // B/2 * 2 == B: not below
        PassReport keep = reconciler(s, sweep).reconcileOnce();
        assertThat(keep.drops()).isZero();
        assertThat(validIndexed(tenant)).containsExactly(name("shrink"));

        setRows(tenant, name("shrink"), B / 2 - 1);      // below half
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.hasValidIndex(M384, tenant, name("shrink"))).isTrue();
        PassReport drop = reconciler(s, sweep).reconcileOnce();
        assertThat(drop.drops()).isEqualTo(1);
        assertThat(validIndexed(tenant)).isEmpty();
        // No read between the drop and this question: the refresh after the drop took it out of the set.
        assertThat(sweep.hasValidIndex(M384, tenant, name("shrink"))).isFalse();
    }

    @Test
    void deleteAndSupersedeDrop_aCanonicalRenameBuildsTheNewNameInTheSamePass_aCopyRenameKeepsTheOldIndex()
            throws Exception {
        String tenant = newTenant("gone");
        collection(tenant, name("del"), 300);
        collection(tenant, name("sup"), 300);
        collection(tenant, name("copy-src"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        reconciler(s, sweep).reconcileOnce();
        assertThat(validIndexed(tenant)).containsExactlyInAnyOrder(name("del"), name("sup"), name("copy-src"));

        // Delete: the rows go, then the registry row.
        asSuperuser(su -> {
            su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(name("del"))).execute();
            su.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                .and(CATALOG_COLLECTIONS.NAME.eq(name("del"))).execute();
            return null;
        });
        // Canonical rename of "sup": the rows are re-homed under the new name and the old one is superseded.
        collection(tenant, name("sup-new"), 300);
        asSuperuser(su -> su.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, name("sup-new"))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name("sup"))).execute());
        // COPY rename of "copy-src": the old collection stays live with its rows, the copy has its own.
        collection(tenant, name("copy-dst"), 300);

        PassReport report = reconciler(s, sweep).reconcileOnce();

        assertThat(report.drops()).isEqualTo(2);
        assertThat(report.builds()).isEqualTo(2);
        assertThat(validIndexed(tenant)).containsExactlyInAnyOrder(name("sup-new"), name("copy-src"),
            name("copy-dst"));
        assertThat(sweep.hasValidIndex(M384, tenant, name("sup"))).isFalse();
        assertThat(sweep.hasValidIndex(M384, tenant, name("sup-new"))).isTrue();
    }

    @Test
    void aLiveIndexedCollectionThatCountsZero_isLoggedAndKept_neverDropped() throws Exception {
        String tenant = newTenant("zero");
        collection(tenant, name("emptied"), 300);
        PciSettings s = settings(16);
        reconciler(s, sweep(s)).reconcileOnce();
        assertThat(validIndexed(tenant)).containsExactly(name("emptied"));

        asSuperuser(su -> su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.eq(name("emptied"))).execute());
        PassReport report = reconciler(s, sweep(s)).reconcileOnce();

        assertThat(report.drops()).isZero();
        assertThat(validIndexed(tenant)).containsExactly(name("emptied"));
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_count_zero_indexed")
            && l.contains("collection=" + name("emptied")));
    }

    @Test
    void aCollectionBetweenBAndT_getsAnIndex_andStillRoutesExact_aboveTItWalksTheIndex() throws Exception {
        String tenant = newTenant("band");
        String coll = name("band");
        List<String> hex = collection(tenant, coll, 300);
        asSuperuser(su -> {
            PgContainerHelper.ownChunks(su, tenant, coll, hex.toArray(new String[0]));
            return null;
        });
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        reconciler(s, sweep).reconcileOnce();
        assertThat(validIndexed(tenant)).containsExactly(coll);

        Embedder embedder = new Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                List<float[]> out = new ArrayList<>();
                for (String t : texts) {
                    float[] v = new float[384];
                    v[Math.abs(t.hashCode()) % 384] = 1f;
                    out.add(v);
                }
                return out;
            }
            @Override public void close() { }
        };
        var repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder, sweep);

        PgSession.overrideSearchExactMaxRowsForTests(400);          // B (200) <= 300 rows <= T (400)
        long exact = PgVectorRepository.routedExactCount();
        long hnsw = PgVectorRepository.routedHnswCount();
        repo.searchWithTokens(tenant, "query", List.of(coll), 5, null, false);
        assertThat(PgVectorRepository.routedExactCount() - exact).as("indexed, and still exact below T").isEqualTo(1);
        assertThat(PgVectorRepository.routedHnswCount() - hnsw).isZero();

        PgSession.overrideSearchExactMaxRowsForTests(100);          // 300 rows > T
        hnsw = PgVectorRepository.routedHnswCount();
        repo.searchWithTokens(tenant, "query", List.of(coll), 5, null, false);
        assertThat(PgVectorRepository.routedHnswCount() - hnsw).as("above T it takes the index").isEqualTo(1);
    }

    // -- counting and tenants ----------------------------------------------------------------------------

    @Test
    void aWrongTenantInTheCountingTransaction_isCaught_andDropsNothing() throws Exception {
        String tenant = newTenant("tmm");
        collection(tenant, name("keep"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        reconciler(s, sweep).reconcileOnce();
        assertThat(validIndexed(tenant)).containsExactly(name("keep"));

        PciReconciler wrong = new PciReconciler(catalog, builderSession(s), sweep, s, Clock.systemUTC(),
            (tx, t) -> tx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("nexus.tenant"),
                DSL.val("somebody-else"), DSL.inline(true))).fetch(),
            PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60));
        PassReport report = wrong.reconcileOnce();

        assertThat(report.drops()).isZero();
        assertThat(report.builds()).isZero();
        assertThat(report.skippedLeaves()).as("every leaf was skipped").isEqualTo(report.leaves()).isPositive();
        assertThat(logged("event=pci_count_tenant_mismatch")).isPositive();
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_count_tenant_mismatch leaf=")
            && l.contains("expected_tenant=" + tenant) && l.contains("actual_tenant=somebody-else"));
        assertThat(validIndexed(tenant)).as("the index survived").containsExactly(name("keep"));
    }

    @Test
    void twoTenantsWithTheSameCollectionName_eachIndexLandsOnItsOwnLeaf() throws Exception {
        String t1 = newTenant("same1");
        String t2 = newTenant("same2");
        String t3 = newTenant("same3");
        collection(t1, name("shared"), 300);
        collection(t2, name("shared"), 250);
        collection(t3, name("shared"), 30);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);

        PassReport report = reconciler(s, sweep).reconcileOnce();

        assertThat(report.builds()).isEqualTo(2);
        assertThat(validIndexed(t1)).containsExactly(name("shared"));
        assertThat(validIndexed(t2)).containsExactly(name("shared"));
        assertThat(validIndexed(t3)).isEmpty();
        assertThat(leaf(t1).indexes().get(0).name()).isNotEqualTo(leaf(t2).indexes().get(0).name());
        assertThat(sweep.hasValidIndex(M384, t1, name("shared"))).isTrue();
        assertThat(sweep.hasValidIndex(M384, t2, name("shared"))).isTrue();
        assertThat(sweep.hasValidIndex(M384, t3, name("shared"))).isFalse();
    }

    @Test
    void aCountThatTimesOut_skipsTheLeaf_buildsAndDropsNothing_andLogsIt() throws Exception {
        String tenant = newTenant("slow");
        collection(tenant, name("keep"), 300);
        collection(tenant, name("wants-index"), 300);
        PciSettings s = settings(1);
        reconciler(s, sweep(s)).reconcileOnce();                     // one slot: exactly one of the two is built
        Set<String> before = validIndexed(tenant);
        assertThat(before).hasSize(1);
        PciCatalog.Leaf leaf = leaf(tenant);

        PciReconciler impatient = new PciReconciler(catalog, builderSession(settings(16)), sweep(s), settings(16),
            Clock.systemUTC(), PciReconciler.SET_LOCAL_TENANT, Duration.ofMillis(1500), Duration.ofSeconds(60));
        PassReport report;
        try (Connection locker = pg.createConnection("")) {
            locker.setAutoCommit(false);
            // The registry read is inside the counting transaction and the catalog read is not, so locking the
            // registry stalls exactly the counting transaction. (Locking a chunks leaf or the parent also stalls the
            // catalog read, which deparses their index predicates and bounds.) The lock is the ACCESS EXCLUSIVE
            // that a typed ALTER TABLE takes and holds until the rollback below undoes the column.
            DSL.using(locker, SQLDialect.POSTGRES).alterTable(CATALOG_COLLECTIONS)
                .addColumn(DSL.name("pcirec_lock_probe"), SQLDataType.INTEGER).execute();
            report = impatient.reconcileOnce();
            locker.rollback();
        }

        assertThat(report.leaves()).isPositive();
        assertThat(report.skippedLeaves()).as("every leaf waits for the registry, then is skipped")
            .isEqualTo(report.leaves());
        assertThat(report.builds()).isZero();
        assertThat(report.drops()).isZero();
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_count_timeout leaf=" + leaf.schema() + "."
            + leaf.name()) && l.contains("timeout_ms=1500"));
        assertThat(validIndexed(tenant)).as("nothing changed, a partial count is never acted on").isEqualTo(before);
    }

    @Test
    void theCountingTransactionsShortNetworkBound_isPutBack_soALongBuildIsNotCutOffByTheClient() throws Exception {
        try (PciBuilderSession.Pass pass = builderSession(settings(16)).open()) {
            assertThat(pass.networkTimeoutMs()).isEqualTo(PciBuilderSession.SOCKET_TIMEOUT_SECONDS * 1000);
            int[] inside = new int[1];
            pass.transaction(tx -> {
                PgSession.setLocal(tx, "statement_timeout", "1500");
                try {
                    inside[0] = pass.networkTimeoutMs();
                } catch (java.sql.SQLException e) {
                    throw new IllegalStateException(e);
                }
                return null;
            });
            assertThat(inside[0]).as("setLocal bound a short read timeout").isLessThan(60_000).isPositive();
            assertThat(pass.networkTimeoutMs()).as("and the pass's own bound is back")
                .isEqualTo(PciBuilderSession.SOCKET_TIMEOUT_SECONDS * 1000);
        }
    }

    // -- failed builds -----------------------------------------------------------------------------------

    @Test
    void aKilledConcurrentBuild_leavesAnInvalidIndexRoutingIgnores_theNextPassDropsItByName_andTheOneAfterRebuilds()
            throws Exception {
        String tenant = newTenant("inv");
        collection(tenant, name("victim"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        MutableClock clock = new MutableClock();
        PciReconciler r = reconciler(s, sweep, clock);

        PassReport failed = failOneBuild(tenant, r);

        assertThat(failed.failedBuilds()).isEqualTo(1);
        assertThat(hasInvalid(tenant)).isTrue();
        assertThat(validIndexed(tenant)).isEmpty();
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.hasValidIndex(M384, tenant, name("victim"))).as("routing ignores it").isFalse();

        clock.advance(Duration.ofMinutes(11));                  // past the first retry
        PassReport drop = r.reconcileOnce();
        assertThat(drop.drops()).isEqualTo(1);
        assertThat(drop.builds()).as("the rebuild is the pass after the drop").isZero();
        assertThat(leaf(tenant).indexes()).isEmpty();

        PassReport rebuild = r.reconcileOnce();
        assertThat(rebuild.builds()).isEqualTo(1);
        assertThat(validIndexed(tenant)).containsExactly(name("victim"));
        assertThat(sweep.hasValidIndex(M384, tenant, name("victim"))).isTrue();
    }

    @Test
    void aFailedBuildBacksOff_ten_twenty_fortyMinutes_andThreeInARowCountAsFailing() throws Exception {
        String tenant = newTenant("backoff");
        collection(tenant, name("stubborn"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        MutableClock clock = new MutableClock();
        PciReconciler r = reconciler(s, sweep, clock);

        assertThat(r.status().failing()).as("not a holder before the first pass").isNull();
        failOneBuild(tenant, r);                                              // failure 1
        assertThat(r.status().failing()).isZero();

        clock.advance(Duration.ofMinutes(9));                                 // inside the 10 minutes
        assertThat(r.reconcileOnce().drops()).as("the invalid index goes").isEqualTo(1);
        assertThat(r.reconcileOnce().builds()).as("backed off: nothing builds").isZero();
        assertThat(leaf(tenant).indexes()).isEmpty();

        clock.advance(Duration.ofMinutes(2));                                 // 11 minutes after failure 1
        failOneBuild(tenant, r);                                              // failure 2, next retry in 20
        clock.advance(Duration.ofMinutes(19));
        r.reconcileOnce();                                                    // drops the invalid one
        assertThat(r.reconcileOnce().builds()).as("20 minutes not yet up").isZero();
        assertThat(r.status().failing()).isZero();

        clock.advance(Duration.ofMinutes(2));
        failOneBuild(tenant, r);                                              // failure 3, next retry in 40
        assertThat(r.status().failing()).as("three in a row").isEqualTo(1);
        assertThat(logged("event=pci_build_failing")).isEqualTo(1);

        clock.advance(Duration.ofMinutes(39));
        r.reconcileOnce();
        assertThat(r.reconcileOnce().builds()).as("40 minutes not yet up").isZero();
        clock.advance(Duration.ofMinutes(2));
        assertThat(r.reconcileOnce().builds()).isEqualTo(1);
        assertThat(validIndexed(tenant)).containsExactly(name("stubborn"));
        assertThat(r.status().failing()).as("a success clears it").isZero();
    }

    @Test
    void theRetryDelayDoublesFromTenMinutes_andIsCappedAtADay() {
        assertThat(PciReconciler.retryDelay(1)).isEqualTo(Duration.ofMinutes(10));
        assertThat(PciReconciler.retryDelay(2)).isEqualTo(Duration.ofMinutes(20));
        assertThat(PciReconciler.retryDelay(3)).isEqualTo(Duration.ofMinutes(40));
        assertThat(PciReconciler.retryDelay(8)).isEqualTo(Duration.ofMinutes(1280));
        assertThat(PciReconciler.retryDelay(9)).isEqualTo(Duration.ofHours(24));
        assertThat(PciReconciler.retryDelay(500)).isEqualTo(Duration.ofHours(24));
    }

    // -- two reconcilers ---------------------------------------------------------------------------------

    @Test
    void aPassDuringABuild_theSecondReconcilerStandsBy_doesNotDropTheInFlightIndex_andTheIndexIsBuiltOnce()
            throws Exception {
        String tenant = newTenant("two");
        collection(tenant, name("once"), 300);
        PciSettings s = settings(16);
        PciReconciler first = reconciler(s, sweep(s));
        PciReconciler second = reconciler(s, sweep(s));

        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> running = inBackground(first);
            await(() -> hasInvalid(tenant), "the first reconciler's build in flight");
            await(() -> first.status().building() != null && first.status().building() == 1,
                "the holder to report building=1");

            PassReport standby = second.reconcileOnce();

            assertThat(standby.state()).isEqualTo(BuilderState.STANDBY);
            assertThat(standby.drops()).isZero();
            assertThat(standby.builds()).isZero();
            assertThat(hasInvalid(tenant)).as("the in-flight index is still there").isTrue();
            assertThat(second.status().builderState()).isEqualTo(BuilderState.STANDBY);
            assertThat(second.status().building()).as("only the holder reports building").isNull();
            assertThat(second.status().failing()).isNull();
            assertThat(first.status().failing()).isZero();

            held.close();
            PassReport done = running.get(60, TimeUnit.SECONDS);
            assertThat(done.builds()).isEqualTo(1);
        }
        assertThat(validIndexed(tenant)).containsExactly(name("once"));
        assertThat(second.reconcileOnce().builds()).as("the new holder finds it built").isZero();
        assertThat(logs().stream().filter(l -> l.contains("event=pci_ddl_done op=build")).count()).isEqualTo(1);
        assertThat(first.status().building()).as("the holder that finished is not building").isZero();
    }

    @Test
    void twoReconcilersStartedTogether_buildEachIndexExactlyOnce() throws Exception {
        String tenant = newTenant("race");
        collection(tenant, name("r1"), 300);
        collection(tenant, name("r2"), 300);
        PciSettings s = settings(16);
        PciReconciler a = reconciler(s, sweep(s));
        PciReconciler b = reconciler(s, sweep(s));
        CountDownLatch go = new CountDownLatch(1);
        CompletableFuture<PassReport> fa = CompletableFuture.supplyAsync(() -> {
            awaitQuietly(go);
            return a.reconcileOnce();
        }, async);
        CompletableFuture<PassReport> fb = CompletableFuture.supplyAsync(() -> {
            awaitQuietly(go);
            return b.reconcileOnce();
        }, async);
        go.countDown();
        PassReport ra = fa.get(120, TimeUnit.SECONDS);
        PassReport rb = fb.get(120, TimeUnit.SECONDS);

        assertThat(ra.builds() + rb.builds()).as("each of the two indexes once").isEqualTo(2);
        assertThat(validIndexed(tenant)).containsExactlyInAnyOrder(name("r1"), name("r2"));
        assertThat(logs().stream().filter(l -> l.contains("event=pci_ddl_done op=build")).count()).isEqualTo(2);
        assertThat(leaf(tenant).indexes()).hasSize(2);
    }

    static void awaitQuietly(CountDownLatch latch) {
        try {
            latch.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // -- the cap -----------------------------------------------------------------------------------------

    @Test
    void whenMoreCollectionsQualifyThanTheCapAdmits_theLargestWin_notTheFirstByName() throws Exception {
        String tenant = newTenant("cap");
        collection(tenant, name("a-small"), B);                  // alphabetically first, smallest
        collection(tenant, name("b-mid"), 450);
        collection(tenant, name("c-big"), 1500);
        collection(tenant, name("d-huge"), 10 * B + 500);        // beyond the ranking cap: ties with nothing smaller
        PciSettings s = settings(2);
        PciIndexSweep sweep = sweep(s);

        PassReport report = reconciler(s, sweep).reconcileOnce();

        assertThat(report.builds()).isEqualTo(2);
        assertThat(validIndexed(tenant)).containsExactlyInAnyOrder(name("c-big"), name("d-huge"));

        // A leaf at the cap builds no more, however large the newcomer.
        collection(tenant, name("e-giant"), 3000);
        assertThat(reconciler(s, sweep).reconcileOnce().builds()).isZero();
        assertThat(validIndexed(tenant)).hasSize(2).doesNotContain(name("e-giant"));
    }

    @Test
    void aCapOfZeroBuildsNothing_butTheDropsStillRun() throws Exception {
        String tenant = newTenant("cap0");
        collection(tenant, name("had"), 300);
        reconciler(settings(16), sweep(settings(16))).reconcileOnce();
        assertThat(validIndexed(tenant)).containsExactly(name("had"));

        collection(tenant, name("new"), 300);
        setRows(tenant, name("had"), 10);
        PciSettings none = settings(0);
        PassReport report = reconciler(none, sweep(none)).reconcileOnce();

        assertThat(report.builds()).isZero();
        assertThat(report.drops()).isEqualTo(1);
        assertThat(validIndexed(tenant)).isEmpty();
    }

    // -- switches and schedule ---------------------------------------------------------------------------

    @Test
    void withTheSwitchOff_nothingRuns_noConnectionIsOpened_andStartSchedulesNothing() throws Exception {
        String tenant = newTenant("off");
        collection(tenant, name("big"), 300);
        PciSettings off = new PciSettings(false, B, 600, 16);
        PciReconciler r = reconciler(off, sweep(off));

        PassReport report = r.reconcileOnce();
        r.start();

        assertThat(report.state()).isEqualTo(BuilderState.OFF);
        assertThat(validIndexed(tenant)).isEmpty();
        assertThat(r.isRunning()).isFalse();
        assertThat(r.status().builderState()).isEqualTo(BuilderState.OFF);
        assertThat(r.status().building()).isNull();
    }

    @Test
    void theScheduleRunsPassesOnAThreadOfItsOwn_stopEndsItWithoutWaiting() throws Exception {
        String tenant = newTenant("sched");
        collection(tenant, name("big"), 300);
        PciSettings s = settings(16);
        PciReconciler r = new PciReconciler(catalog, builderSession(s), sweep(s), s, Clock.systemUTC(),
            PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofMillis(200));

        r.start();
        r.start();                                                            // idempotent
        await(() -> r.passes() >= 3, "three scheduled passes");
        assertThat(r.isRunning()).isTrue();
        assertThat(Thread.getAllStackTraces().keySet().stream().map(Thread::getName))
            .contains(PciReconciler.THREAD_NAME);
        long start = System.nanoTime();
        r.stop();
        assertThat(Duration.ofNanos(System.nanoTime() - start)).isLessThan(Duration.ofSeconds(2));
        assertThat(r.isRunning()).isFalse();
        assertThat(validIndexed(tenant)).containsExactly(name("big"));
        assertThat(r.status().lastDdlPassAt()).isNotNull();
        assertThat(r.status().builderState()).isEqualTo(BuilderState.OK);
        assertThat(r.status().building()).isZero();
        org.junit.jupiter.api.Assertions.assertThrows(IllegalStateException.class, r::start);
    }
}
