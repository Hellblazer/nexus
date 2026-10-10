// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.http.StatusHandler;
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

import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-227 Step 2 (nexus-43ulx.19): {@link PciReconciler}, the DDL half, against the real partitioned layout in
 * production's shape: a DEDICATED container migrated by a non-superuser schema owner that the reconciler connects as
 * (so {@code catalog_collections} is FORCE ROW LEVEL SECURITY to it and a registry read without the tenant is empty;
 * {@code nexus.chunks} is not filtered for it, since {@code vectors-029}'s owner-read policy lets it read every tenant).
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

    /** End the builder's current STATEMENT (57014); its session, and so the pass, carries on. */
    void cancelBuilder() throws Exception {
        asSuperuser(su -> su.select(DSL.function("pg_cancel_backend", SQLDataType.BOOLEAN,
                DSL.field(DSL.name("pid"), Integer.class)))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .fetch());
    }

    long builderBackends() throws Exception {
        return asSuperuser(su -> su.selectCount().from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .fetchOne(0, Long.class));
    }

    CompletableFuture<PassReport> inBackground(PciReconciler r) {
        return CompletableFuture.supplyAsync(r::reconcileOnce, async);
    }

    /**
     * Hold a build open, then cancel the builder's statement: one FAILED build (57014: the statement's own failure,
     * which is charged to backoff), an invalid index left behind. The session survives, as after a statement timeout.
     */
    PassReport failOneBuild(String tenant, PciReconciler r) throws Exception {
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = inBackground(r);
            await(() -> hasInvalid(tenant), "the concurrent build's invalid index");
            cancelBuilder();
            held.close();
            return pass.get(60, TimeUnit.SECONDS);
        }
    }

    /**
     * Hold a build open, then end the builder's BACKEND (57P01), as a migration walk or the shutdown hook does: not
     * the statement's failure, and not charged. The pass is over.
     */
    PassReport terminateOneBuild(String tenant, PciReconciler r) throws Exception {
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = inBackground(r);
            await(() -> hasInvalid(tenant), "the concurrent build's invalid index");
            terminateBuilder();
            held.close();
            return pass.get(60, TimeUnit.SECONDS);
        }
    }

    /** A session of the test that holds the migration lock, as a migrator mid-walk does. */
    final class HeldMigrationLock implements AutoCloseable {
        private final Connection conn;

        HeldMigrationLock() throws Exception {
            conn = pg.createConnection("");
            conn.setAutoCommit(true);
            DSL.using(conn, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_lock", SQLDataType.OTHER,
                DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY))).fetch();
        }

        @Override
        public void close() throws Exception {
            conn.close();
        }
    }

    void setLifecycle(String tenant, String collection, String state) throws Exception {
        asSuperuser(su -> su.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, state)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection)).execute());
    }

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** GET /v1/status through the real {@link StatusHandler}, fed the reconciler's and the sweep's own status. */
    JsonNode statusJson(PciReconciler r, PciIndexSweep sweep) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/status", new StatusHandler(null, null, 0L, null, null, null,
            () -> StatusHandler.PerCollectionIndexes.of(sweep.status(), r.status())));
        server.start();
        try {
            HttpResponse<String> resp = dev.nexus.service.TestHttp.client().send(
                HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/status"))
                    .GET().build(), HttpResponse.BodyHandlers.ofString());
            assertThat(resp.statusCode()).isEqualTo(200);
            return MAPPER.readTree(resp.body());
        } finally {
            server.stop(0);
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

    /** nexus-43ulx.23: the pass line carries the two per-engine status fields, so a log reader sees what /v1/status says. */
    @Test
    void thePassLineCarriesTheBuilderStateAndTheFailingCount() throws Exception {
        String tenant = newTenant("passline");
        collection(tenant, name("passline"), 300);
        PciSettings s = settings(16);
        reconciler(s, sweep(s)).reconcileOnce();

        assertThat(logs()).anyMatch(l -> l.contains("event=pci_reconcile_pass ")
            && l.contains(" builder_state=ok failing=0"));
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
    void aFailedConcurrentBuild_leavesAnInvalidIndexRoutingIgnores_theNextPassDropsItByName_andTheOneAfterRebuilds()
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

    // -- status truthfulness (nexus-43ulx.23 fix round) -----------------------------------------------------------

    void superuserDdl(String ddl) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, ddl);
        }
    }

    private PciReconciler reconcilerAs(String user, String password, PciSettings s, PciIndexSweep sweep, Clock clock) {
        return new PciReconciler(catalog, new PciBuilderSession(pg.getJdbcUrl(), user, password, NONCE, s), sweep, s,
            clock, PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60));
    }

    @Test
    void aRoleWithoutPrivilege_showsAsNoPrivilegeInTheStatus_andTheIndexesThatExistKeepServing() throws Exception {
        String tenant = newTenant("nopriv");
        collection(tenant, name("had"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        reconciler(s, sweep).reconcileOnce();
        assertThat(validIndexed(tenant)).containsExactly(name("had"));
        collection(tenant, name("wanted"), 300);

        // The service role: it counts (it is the application's role) and owns no leaf, so the first CREATE is 42501.
        PciReconciler r = reconcilerAs(PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD, s, sweep,
            Clock.systemUTC());
        PassReport report = r.reconcileOnce();

        assertThat(report.state()).isEqualTo(BuilderState.NO_PRIVILEGE);
        assertThat(report.builds()).isZero();
        assertThat(r.status().builderState()).as("the status object, not only the pass report")
            .isEqualTo(BuilderState.NO_PRIVILEGE);
        assertThat(validIndexed(tenant)).as("what exists keeps serving").containsExactly(name("had"));
        JsonNode me = statusJson(r, sweep).at("/per_collection_indexes/this_engine");
        assertThat(me.get("builder_state").asText()).isEqualTo("no_privilege");
        assertThat(me.get("pass_in_progress").asBoolean()).isFalse();

        // It stays so on the next pass: no success has cleared it.
        r.reconcileOnce();
        assertThat(r.status().builderState()).isEqualTo(BuilderState.NO_PRIVILEGE);
        assertThat(validIndexed(tenant)).containsExactly(name("had"));
        assertThat(sweep.hasValidIndex(M384, tenant, name("had"))).isTrue();
    }

    @Test
    void aWrongAdminPassword_showsAsAuthFailedInTheStatus_buildsNothing_andNeverLogsThePassword() throws Exception {
        String tenant = newTenant("authfail");
        collection(tenant, name("had"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        reconciler(s, sweep).reconcileOnce();
        collection(tenant, name("wanted"), 300);
        String wrong = "wrong-" + UUID.randomUUID();       // never written to an assertion message

        PciReconciler r = reconcilerAs(ADMIN_ROLE, wrong, s, sweep, Clock.systemUTC());
        PassReport report = r.reconcileOnce();

        assertThat(report.state()).isEqualTo(BuilderState.AUTH_FAILED);
        assertThat(report.leaves()).isZero();
        assertThat(r.status().builderState()).isEqualTo(BuilderState.AUTH_FAILED);
        assertThat(r.status().building()).isNull();
        assertThat(r.status().failing()).isNull();
        JsonNode me = statusJson(r, sweep).at("/per_collection_indexes/this_engine");
        assertThat(me.get("builder_state").asText()).isEqualTo("auth_failed");
        assertThat(me.get("building").isNull()).isTrue();
        assertThat(me.get("last_ddl_pass_at").isNull()).as("this engine never completed a pass").isTrue();
        assertThat(validIndexed(tenant)).as("nothing built, nothing dropped").containsExactly(name("had"));
        assertThat(logs()).noneMatch(l -> l.contains(wrong));
        assertThat(logged("event=pci_builder_auth_failed")).isEqualTo(1);
    }

    @Test
    void aStandbyEngineKeepsItsLastCompletedPassTime_andReportsNoBuildingFailingOrPassInFlight() throws Exception {
        String tenant = newTenant("standby");
        collection(tenant, name("big"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        MutableClock clock = new MutableClock();
        PciReconciler r = reconciler(s, sweep, clock);
        assertThat(r.status().lastDdlPassAt()).as("before its first pass").isNull();
        r.reconcileOnce();
        Instant completed = r.status().lastDdlPassAt();
        assertThat(completed).isEqualTo(clock.instant());

        clock.advance(Duration.ofHours(1));
        try (PciBuilderSession.Pass peer = builderSession(s).open()) {      // a peer engine holds the builder lock
            assertThat(peer.state()).isEqualTo(BuilderState.OK);
            PassReport standby = r.reconcileOnce();

            assertThat(standby.state()).isEqualTo(BuilderState.STANDBY);
            assertThat(r.status().builderState()).isEqualTo(BuilderState.STANDBY);
            assertThat(r.status().building()).isNull();
            assertThat(r.status().failing()).isNull();
            assertThat(r.status().passInProgress()).isFalse();
            assertThat(r.status().passStartedAt()).isNull();
            assertThat(r.status().lastDdlPassAt()).as("this engine's own history survives becoming a standby")
                .isEqualTo(completed);
            JsonNode me = statusJson(r, sweep).at("/per_collection_indexes/this_engine");
            assertThat(me.get("builder_state").asText()).isEqualTo("standby");
            assertThat(me.get("last_ddl_pass_at").asText()).isEqualTo(completed.truncatedTo(
                java.time.temporal.ChronoUnit.SECONDS).toString());
            assertThat(me.get("failing").isNull()).isTrue();
        }
    }

    @Test
    void anOpenFailure_resetsTheHolderAndBuilding_butKeepsTheLastPassTime_andLogsTheDriversReason() throws Exception {
        String tenant = newTenant("openfail");
        collection(tenant, name("big"), 300);
        PciSettings s = settings(16);
        PciReconciler r = reconciler(s, sweep(s));
        r.reconcileOnce();
        assertThat(r.status().building()).as("a holder").isZero();
        Instant completed = r.status().lastDdlPassAt();
        assertThat(completed).isNotNull();

        superuserDdl("ALTER ROLE " + ADMIN_ROLE + " CONNECTION LIMIT 0");
        try {
            PassReport report = r.reconcileOnce();

            assertThat(report.ranToEnd()).isFalse();
            assertThat(r.status().building()).as("no longer a holder: the database is unreachable").isNull();
            assertThat(r.status().failing()).isNull();
            assertThat(r.status().lastDdlPassAt()).isEqualTo(completed);
            assertThat(logs()).as("the driver's reason is in the log")
                .anyMatch(l -> l.contains("event=pci_reconcile_pass_failed reason=builder_open")
                    && l.contains("too many connections"));
        } finally {
            superuserDdl("ALTER ROLE " + ADMIN_ROLE + " CONNECTION LIMIT -1");
        }
    }

    @Test
    void aPassThatSkippedEveryLeaf_doesNotCountAsTheLastCompletedPass() throws Exception {
        String tenant = newTenant("allskip");
        collection(tenant, name("keep"), 300);
        PciSettings s = settings(16);
        MutableClock clock = new MutableClock();
        PciReconciler impatient = new PciReconciler(catalog, builderSession(s), sweep(s), s, clock,
            PciReconciler.SET_LOCAL_TENANT, Duration.ofMillis(1500), Duration.ofSeconds(60));
        PassReport report;
        try (Connection locker = pg.createConnection("")) {
            locker.setAutoCommit(false);
            DSL.using(locker, SQLDialect.POSTGRES).alterTable(CATALOG_COLLECTIONS)
                .addColumn(DSL.name("pcirec_lock_probe2"), SQLDataType.INTEGER).execute();
            report = impatient.reconcileOnce();
            locker.rollback();
        }

        assertThat(report.ranToEnd()).as("the pass got to its end").isTrue();
        assertThat(report.skippedLeaves()).isEqualTo(report.leaves()).isPositive();
        assertThat(impatient.status().lastDdlPassAt()).as("but nothing was checked").isNull();

        PciReconciler normal = reconciler(s, sweep(s), clock);
        normal.reconcileOnce();
        assertThat(normal.status().lastDdlPassAt()).as("a pass that counted a leaf does").isEqualTo(clock.instant());
    }

    @Test
    void aFailureEntry_isClearedWhenTheCollectionIsQuarantined_notOnlyWhenItIsDeleted() throws Exception {
        assertThatAFailureEntryClearsWhen("quar",
            (tenant, collection) -> setLifecycle(tenant, collection, "quarantine"));
    }

    @Test
    void aFailureEntry_isClearedWhenTheCollectionIsSuperseded() throws Exception {
        assertThatAFailureEntryClearsWhen("super", (tenant, collection) -> {
            collection(tenant, name("elsewhere"), 0);
            asSuperuser(su -> su.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, name("elsewhere"))
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
                .execute());
        });
    }

    @FunctionalInterface
    interface RegistryChange {
        void apply(String tenant, String collection) throws Exception;
    }

    private void assertThatAFailureEntryClearsWhen(String tag, RegistryChange change) throws Exception {
        String tenant = newTenant("fail-" + tag);
        collection(tenant, name("victim"), 300);
        PciSettings s = settings(16);
        PciReconciler r = reconciler(s, sweep(s), new MutableClock());

        failOneBuild(tenant, r);
        assertThat(r.trackedFailures()).as("a failed build is tracked").isEqualTo(1);

        change.apply(tenant, name("victim"));
        r.reconcileOnce();

        assertThat(r.trackedFailures()).as("a collection that can no longer build has nothing to retry").isZero();
        assertThat(r.status().failing()).isZero();
    }

    @Test
    void aBuildEndedByTheBackendBeingTerminated_isNotAFailure_andIsRetriedAtOnce() throws Exception {
        String tenant = newTenant("term");
        collection(tenant, name("victim"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        PciReconciler r = reconciler(s, sweep, new MutableClock());       // the clock never moves: no backoff can lapse

        PassReport ended = terminateOneBuild(tenant, r);

        assertThat(ended.ranToEnd()).as("the pass is over").isFalse();
        assertThat(ended.failedBuilds()).as("not the statement's failure").isZero();
        assertThat(r.trackedFailures()).as("nothing to back off").isZero();
        assertThat(r.status().lastDdlPassAt()).as("an interrupted pass is not a completed one").isNull();
        assertThat(hasInvalid(tenant)).as("the terminated CIC left its invalid index").isTrue();
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_index_build_not_done") && l.contains("CONNECTION_LOST"));

        assertThat(r.reconcileOnce().drops()).as("the next pass drops it").isEqualTo(1);
        assertThat(r.reconcileOnce().builds()).as("and rebuilds with no backoff to wait out").isEqualTo(1);
        assertThat(validIndexed(tenant)).containsExactly(name("victim"));
    }

    /**
     * A real CREATE INDEX CONCURRENTLY, held open behind a writer, terminated by a real {@code SchemaMigrator.migrate}
     * walk (not a {@code pg_sleep}): the pass ends, the build is not charged to backoff, and the walk completes.
     */
    @Test
    @Timeout(value = 600, unit = TimeUnit.SECONDS)
    void aRealMigrationWalk_terminatesARealBuild_theBuildIsNotCharged_andTheWalkCompletes() throws Exception {
        String tenant = newTenant("walk");
        collection(tenant, name("victim"), 300);
        PciSettings s = settings(16);
        PciReconciler r = reconciler(s, sweep(s), new MutableClock());
        var migrator = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(SchemaMigrator.class);
        var migratorLogs = new ListAppender<ILoggingEvent>();
        migratorLogs.start();
        migrator.addAppender(migratorLogs);
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = inBackground(r);
            await(() -> hasInvalid(tenant), "the real concurrent build's invalid index");
            assertThat(builderBackends()).as("a live builder session, mid-CIC").isEqualTo(1);

            CompletableFuture<Void> walk = CompletableFuture.runAsync(() -> SchemaMigrator.migrate(adminDs), async);
            PassReport ended = pass.get(120, TimeUnit.SECONDS);             // the walk's first act ends the builder
            held.close();
            walk.get(300, TimeUnit.SECONDS);                                // and the walk itself completes

            assertThat(ended.ranToEnd()).isFalse();
            assertThat(ended.builds()).isZero();
            assertThat(ended.failedBuilds()).as("terminated, so not a failed build").isZero();
        } finally {
            migrator.detachAppender(migratorLogs);
        }
        assertThat(migratorLogs.list.stream().map(ILoggingEvent::getFormattedMessage))
            .anyMatch(l -> l.contains("event=pci_builders_terminated") && !l.contains("count=0"));
        assertThat(builderBackends()).as("the builder session is gone and so is its lock").isZero();
        assertThat(r.trackedFailures()).isZero();
        assertThat(r.reconcileOnce().drops()).as("the invalid index goes").isEqualTo(1);
        assertThat(r.reconcileOnce().builds()).as("the rebuild needs no backoff to lapse").isEqualTo(1);
        assertThat(validIndexed(tenant)).containsExactly(name("victim"));
    }

    // -- the migration check inside a pass -----------------------------------------------------------------------

    @Test
    void aMigrationLockHeldWhenThePassOpens_endsThePassBeforeAnyCountOrBuild_andTheNextPassBuildsAtOnce()
            throws Exception {
        String tenant = newTenant("migopen");
        collection(tenant, name("big"), 300);
        PciSettings s = settings(16);
        PciReconciler r = reconciler(s, sweep(s), new MutableClock());

        try (HeldMigrationLock migrating = new HeldMigrationLock()) {
            PassReport report = r.reconcileOnce();

            assertThat(report.ranToEnd()).isFalse();
            assertThat(report.leaves()).as("not even a count").isZero();
            assertThat(report.builds()).isZero();
            assertThat(r.status().lastDdlPassAt()).isNull();
            assertThat(logged("event=pci_ddl_skipped reason=migration_in_progress")).isEqualTo(1);
        }
        assertThat(validIndexed(tenant)).isEmpty();
        assertThat(r.trackedFailures()).isZero();
        assertThat(r.reconcileOnce().builds()).as("the migration is over; no backoff was charged").isEqualTo(1);
    }

    @Test
    void aMigrationLockTakenMidPass_skipsTheBuild_endsThePass_andChargesNothing() throws Exception {
        String tenant = newTenant("migmid");
        collection(tenant, name("big"), 300);
        PciSettings s = settings(16);
        AtomicReference<HeldMigrationLock> migrating = new AtomicReference<>();
        // A tenant has one leaf per embedding model, and the pass counts them in the catalog's order. The migration
        // must begin while THIS leaf (the one with the work) is being counted: after the pass opened and checked and
        // after the check before this leaf's count, so that the only check left is the one before the statement.
        PciCatalog.Leaf target = leaf(tenant);
        List<PciCatalog.Leaf> counted = catalog.read().leaves().stream()
            .filter(l -> l.model() != null && l.tenant() != null).toList();
        int targetPosition = -1;
        for (int i = 0; i < counted.size(); i++) {
            if (counted.get(i).name().equals(target.name()) && counted.get(i).schema().equals(target.schema())) {
                targetPosition = i;
            }
        }
        assertThat(targetPosition).as("the target leaf is among the counted ones").isGreaterThanOrEqualTo(0);
        int startsAt = targetPosition;
        AtomicInteger counts = new AtomicInteger();
        PciReconciler.TenantBinder startsAMigration = (tx, t) -> {
            PciReconciler.SET_LOCAL_TENANT.bind(tx, t);
            if (counts.getAndIncrement() == startsAt) {
                try {
                    migrating.set(new HeldMigrationLock());
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            }
        };
        MutableClock clock = new MutableClock();
        PciReconciler r = new PciReconciler(catalog, builderSession(s), sweep(s), s, clock, startsAMigration,
            PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60));
        try {
            PassReport report = r.reconcileOnce();

            assertThat(migrating.get()).as("the migration began mid-pass").isNotNull();
            assertThat(report.ranToEnd()).as("the pass ended on the migration").isFalse();
            assertThat(report.builds()).isZero();
            assertThat(report.failedBuilds()).isZero();
            assertThat(r.trackedFailures()).as("a build that never started is not charged").isZero();
            assertThat(r.status().lastDdlPassAt()).isNull();
            assertThat(validIndexed(tenant)).isEmpty();
            assertThat(hasInvalid(tenant)).as("no statement was sent").isFalse();
            assertThat(logs()).anyMatch(l -> l.contains("event=pci_ddl_skipped reason=migration_in_progress"));
        } finally {
            migrating.get().close();
        }
        assertThat(r.reconcileOnce().builds()).as("the next pass builds with no clock advance").isEqualTo(1);
        assertThat(validIndexed(tenant)).containsExactly(name("big"));
    }

    @Test
    void aBuilderSessionEndedWhileIdleBetweenStatements_endsThePassQuietly_noErrorLine() throws Exception {
        String tenant = newTenant("idlekill");
        collection(tenant, name("big"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        // The catalog read happens with the pass open and idle: end the builder's session there, as a peer migrator
        // of a rolling deploy does. The first leaf's migration check is then the first statement on a dead session.
        PciReconciler r = new PciReconciler(() -> {
            PciCatalog.Snapshot snapshot = catalog.read();
            try {
                terminateBuilder();
                await(() -> {
                    try {
                        return builderBackends() == 0;
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                }, "the builder session to end");
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            return snapshot;
        }, builderSession(s), sweep, s, Clock.systemUTC(), PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT,
            Duration.ofSeconds(60), new Random(1));

        PassReport report = r.reconcileOnce();

        assertThat(report.ranToEnd()).isFalse();
        assertThat(report.builds()).isZero();
        assertThat(logged("event=pci_reconcile_pass_failed reason=migration_check")).as("one WARN").isEqualTo(1);
        assertThat(appender.list).as("no ERROR line and no stack: a routine end of a pass")
            .noneMatch(e -> e.getLevel() == Level.ERROR || e.getThrowableProxy() != null);
        assertThat(validIndexed(tenant)).isEmpty();
        assertThat(reconciler(s, sweep).reconcileOnce().builds()).as("the next pass starts clean").isEqualTo(1);
    }

    // -- the pass in flight ----------------------------------------------------------------------------------

    @Test
    void aPassInFlight_isVisibleInTheStatus_whileLastPassTimeIsStillNull() throws Exception {
        String tenant = newTenant("inflight");
        collection(tenant, name("big"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        MutableClock clock = new MutableClock();
        PciReconciler r = reconciler(s, sweep, clock);
        assertThat(r.status().passInProgress()).isFalse();
        assertThat(r.status().passStartedAt()).isNull();

        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = inBackground(r);
            await(() -> r.status().passInProgress(), "the pass to be in progress");

            assertThat(r.status().passStartedAt()).isEqualTo(clock.instant());
            assertThat(r.status().lastDdlPassAt()).as("a long first pass shows nothing here until it ends").isNull();
            JsonNode me = statusJson(r, sweep).at("/per_collection_indexes/this_engine");
            assertThat(me.get("pass_in_progress").asBoolean()).isTrue();
            assertThat(me.get("pass_started_at").asText()).isEqualTo("2026-10-10T00:00:00Z");
            assertThat(me.get("last_ddl_pass_at").isNull()).isTrue();

            held.close();
            pass.get(60, TimeUnit.SECONDS);
        }
        assertThat(r.status().passInProgress()).isFalse();
        assertThat(r.status().passStartedAt()).isNull();
        assertThat(r.status().lastDdlPassAt()).isEqualTo(clock.instant());
        JsonNode me = statusJson(r, sweep).at("/per_collection_indexes/this_engine");
        assertThat(me.get("pass_in_progress").asBoolean()).isFalse();
        assertThat(me.get("pass_started_at").isNull()).isTrue();
    }

    // -- the router set after a drop -------------------------------------------------------------------------------

    @Test
    void aRefreshThatFailsAfterADrop_isTriedOnceMore_soTheRouterSetDoesNotKeepTheDroppedIndex() throws Exception {
        String tenant = newTenant("refreshretry");
        collection(tenant, name("shrink"), B);
        PciSettings s = settings(16);
        AtomicInteger reads = new AtomicInteger();
        AtomicInteger failNext = new AtomicInteger();
        PciIndexSweep sweep = new PciIndexSweep(() -> {
            reads.incrementAndGet();
            return failNext.getAndUpdate(n -> Math.max(0, n - 1)) > 0 ? null : catalog.read();
        }, s);
        reconciler(s, sweep).reconcileOnce();
        assertThat(sweep.hasValidIndex(M384, tenant, name("shrink"))).isTrue();

        setRows(tenant, name("shrink"), B / 2 - 1);
        reads.set(0);
        failNext.set(1);                                                  // the refresh right after the drop fails once
        PassReport drop = reconciler(s, sweep).reconcileOnce();

        assertThat(drop.drops()).isEqualTo(1);
        assertThat(reads.get()).as("the failed refresh and its retry").isEqualTo(2);
        assertThat(sweep.hasValidIndex(M384, tenant, name("shrink")))
            .as("the dropped index is out of the router set without waiting for a tick").isFalse();
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_reconcile_refresh_retried"));
    }

    // -- the registry's states reach the planner -------------------------------------------------------------------

    @Test
    void theRegistrysLifecycleStates_reachThePlanner_aStateAloneNeverDrops_aNonLiveCollectionNeverBuilds()
            throws Exception {
        String tenant = newTenant("states");
        collection(tenant, name("live"), 300);
        collection(tenant, name("stays"), 300);
        collection(tenant, name("moved"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        reconciler(s, sweep).reconcileOnce();
        assertThat(validIndexed(tenant)).containsExactlyInAnyOrder(name("live"), name("stays"), name("moved"));

        // "stays": quarantined with its rows still there. "moved": quarantined and its rows moved away (count 0).
        // "never-q" and "never-d" arrive already non-live with plenty of rows.
        setLifecycle(tenant, name("stays"), "quarantine");
        setLifecycle(tenant, name("moved"), "quarantine");
        asSuperuser(su -> su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.eq(name("moved"))).execute());
        collection(tenant, name("never-q"), 300);
        collection(tenant, name("never-d"), 300);
        setLifecycle(tenant, name("never-q"), "quarantine");
        setLifecycle(tenant, name("never-d"), "disputed");

        PassReport report = reconciler(s, sweep).reconcileOnce();

        assertThat(report.builds()).as("a collection that is not 'live' never builds").isZero();
        assertThat(report.drops()).as("only the one whose rows are gone (not suspect: it is not live)").isEqualTo(1);
        assertThat(validIndexed(tenant)).as("the state alone drops nothing")
            .containsExactlyInAnyOrder(name("live"), name("stays"));
        assertThat(logs()).as("a non-live zero is not a counting failure")
            .noneMatch(l -> l.contains("event=pci_count_zero_indexed") && l.contains(name("moved")));
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

    // -- recovery, the production factory and the tenant predicates (Batch B test-validation fixes) --------------

    /**
     * The path out of {@code no_privilege}: a role that could not create an index is later granted the right, and the
     * next pass builds and reports {@code ok}. Two things have to hold for that: the success clears the session's
     * state, and the pass still treats a {@code no_privilege} engine as the lock holder (a holder that drops
     * {@code NO_PRIVILEGE} would stop every later pass, so the engine could never recover by itself).
     */
    @Test
    void anEngineThatWasNoPrivilege_recovers_onceTheRoleCanCreateTheIndex() throws Exception {
        String tenant = newTenant("recov");
        collection(tenant, name("wanted"), 300);
        PciSettings s = settings(16);
        PciReconciler r = reconcilerAs(PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD, s, sweep(s),
            Clock.systemUTC());

        r.reconcileOnce();
        assertThat(r.status().builderState()).isEqualTo(BuilderState.NO_PRIVILEGE);
        assertThat(validIndexed(tenant)).isEmpty();

        PciCatalog.Leaf leaf = leaf(tenant);
        superuserDdl("ALTER TABLE " + leaf.schema() + "." + leaf.name() + " OWNER TO " + PgContainerHelper.SVC_USERNAME);
        superuserDdl("GRANT CREATE ON SCHEMA " + leaf.schema() + " TO " + PgContainerHelper.SVC_USERNAME);
        try {
            PassReport second = r.reconcileOnce();
            assertThat(second.builds()).as("the next pass tries again and builds").isEqualTo(1);
            assertThat(r.status().builderState()).as("a success clears no_privilege").isEqualTo(BuilderState.OK);
            assertThat(validIndexed(tenant)).containsExactly(name("wanted"));
        } finally {
            superuserDdl("REVOKE CREATE ON SCHEMA " + leaf.schema() + " FROM " + PgContainerHelper.SVC_USERNAME);
            superuserDdl("ALTER TABLE " + leaf.schema() + "." + leaf.name() + " OWNER TO " + ADMIN_ROLE);
        }
    }

    /** The factory Main calls: it must connect the builder with the admin values, user and password in order. */
    @Test
    void theProductionFactory_buildsWithTheAdminValues() throws Exception {
        String tenant = newTenant("factory");
        collection(tenant, name("wanted"), 300);
        PciSettings s = settings(16);
        PciReconciler r = PciReconciler.create(svcDs,
            new dev.nexus.service.db.AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS), NONCE, sweep(s), s);

        PassReport report = r.reconcileOnce();

        assertThat(report.state()).isEqualTo(BuilderState.OK);
        assertThat(report.builds()).isEqualTo(1);
        assertThat(validIndexed(tenant)).containsExactly(name("wanted"));
    }

    /**
     * With a superuser (or BYPASSRLS) admin role, row-level security filters nothing, so the registry read's own
     * {@code tenant_id} predicate is the only thing between a leaf and another tenant's registry row of the same name.
     * Tenant B keeps a live row called {@code same}; tenant A's registry row and rows are gone. A's index must drop.
     * Without the predicate A's registry shows B's live {@code same}, the planner reads it as still registered, and
     * the index stays.
     */
    @Test
    void asSuperuserAdmin_aDeletedCollectionsIndexIsDropped_evenWhenAnotherTenantHasTheSameName() throws Exception {
        String a = newTenant("suA");
        String b = newTenant("suB");
        collection(a, name("same"), 300);
        collection(b, name("same"), 300);
        PciSettings s = settings(16);
        PciReconciler r = reconcilerAs(pg.getUsername(), pg.getPassword(), s, sweep(s), Clock.systemUTC());
        r.reconcileOnce();
        assertThat(validIndexed(a)).containsExactly(name("same"));
        assertThat(validIndexed(b)).containsExactly(name("same"));

        asSuperuser(su -> su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(a))
            .and(CHUNKS.COLLECTION.eq(name("same"))).execute());
        asSuperuser(su -> su.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(a))
            .and(CATALOG_COLLECTIONS.NAME.eq(name("same"))).execute());
        r.reconcileOnce();

        assertThat(validIndexed(a)).as("A's collection left the registry: its index is dropped").isEmpty();
        assertThat(validIndexed(b)).as("B is untouched").containsExactly(name("same"));
    }

    /**
     * {@code vectors-029} gave the migrating role (here {@value #ADMIN_ROLE}, in production {@code nexus_admin}) a
     * permissive {@code SELECT ... USING (true)} policy on {@code nexus.chunks}. The builder's admin session therefore
     * reads EVERY tenant's chunks whatever {@code nexus.tenant} says, and the count is per-tenant only because its
     * probe carries an explicit {@code tenant_id} predicate. Tenant A holds 100 rows of {@code same} (below B, so no
     * index) and tenant B holds 300 of the same name. A count that read both would reach 400 and build on A's leaf.
     * The control proves the premise on this substrate: the admin role does see the other tenant's rows.
     */
    @Test
    void underTheAdminRoleWhichReadsEveryTenantsChunks_theCountIsStillPerTenant() throws Exception {
        String a = newTenant("cntA");
        String b = newTenant("cntB");
        collection(a, name("same"), B / 2);
        collection(b, name("same"), 300);

        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(false);
            DSLContext ctx = DSL.using(owner, SQLDialect.POSTGRES);
            PciReconciler.SET_LOCAL_TENANT.bind(ctx, a);
            int seenFromA = ctx.selectCount().from(CHUNKS).where(CHUNKS.COLLECTION.eq(name("same")))
                .fetchOne(0, Integer.class);
            owner.rollback();
            assertThat(seenFromA).as("control: with nexus.tenant = A the admin role still sees B's rows (vectors-029)")
                .isEqualTo(B / 2 + 300);
        }

        PciSettings s = settings(16);
        PassReport report = reconciler(s, sweep(s)).reconcileOnce();

        assertThat(report.builds()).isEqualTo(1);
        assertThat(validIndexed(a)).as("A has 100 rows of its own: below B, no index").isEmpty();
        assertThat(validIndexed(b)).containsExactly(name("same"));
    }

    /**
     * A failure entry exists only on the engine that ran the failing builds. When a peer later succeeds, the next pass
     * here sees a valid index for that collection and must forget the failures: otherwise {@code failing} warns the
     * doctor forever on an engine that has nothing left to retry.
     */
    @Test
    void aFailureEntry_isClearedWhenAValidIndexAppearsFromAPeer() throws Exception {
        String tenant = newTenant("peerok");
        collection(tenant, name("stubborn"), 300);
        PciSettings s = settings(16);
        PciIndexSweep sweep = sweep(s);
        MutableClock clock = new MutableClock();
        PciReconciler failing = reconciler(s, sweep, clock);

        failOneBuild(tenant, failing);
        clock.advance(Duration.ofMinutes(11));
        failing.reconcileOnce();                                              // drops the invalid index
        failOneBuild(tenant, failing);
        clock.advance(Duration.ofMinutes(21));
        failing.reconcileOnce();
        failOneBuild(tenant, failing);
        assertThat(failing.status().failing()).as("three in a row").isEqualTo(1);

        // A peer with no failure history drops the invalid index and builds the collection.
        PciReconciler peer = reconciler(s, sweep, new MutableClock());
        peer.reconcileOnce();
        assertThat(peer.reconcileOnce().builds()).isEqualTo(1);
        assertThat(validIndexed(tenant)).containsExactly(name("stubborn"));

        failing.reconcileOnce();                                              // still inside its own 40 minute backoff
        assertThat(failing.status().failing()).as("a valid index from a peer ends the failing").isZero();
        assertThat(failing.trackedFailures()).isZero();
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
        // Read the status BEFORE stop(): stop interrupts the pass in flight, and a connect that is interrupted is an
        // open failure, which (rightly) ends this engine's claim to be the holder.
        assertThat(r.status().lastDdlPassAt()).isNotNull();
        assertThat(r.status().builderState()).isEqualTo(BuilderState.OK);
        assertThat(r.status().building()).isZero();
        long start = System.nanoTime();
        r.stop();
        assertThat(Duration.ofNanos(System.nanoTime() - start)).isLessThan(Duration.ofSeconds(2));
        assertThat(r.isRunning()).isFalse();
        assertThat(validIndexed(tenant)).containsExactly(name("big"));
        org.junit.jupiter.api.Assertions.assertThrows(IllegalStateException.class, r::start);
    }
}
