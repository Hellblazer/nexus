// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.http.StatusHandler;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PciReconciler.PassReport;
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
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.InetSocketAddress;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd.3: the reconciler's {@code boot_work} entry, against the real partitioned layout in the shape
 * {@link PciReconcilerIntegrationTest} uses (an owner-migrated database, the reconciler connecting as the owner). A
 * build is held open the way that class holds one: an uncommitted insert into the tenant's leaf, whose lock conflicts
 * with the wait of {@code CREATE INDEX CONCURRENTLY}. While it waits, the builder's backend shows in
 * {@code pg_stat_progress_create_index}, so the sampled {@code step} is the phase "waiting for writers before build".
 * A build that is not held, on a collection big enough to run for seconds, shows the counting phases, and one test
 * compares the production sampler's columns with a raw read of the same view.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 300, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
class PciReconcilerBootWorkIntegrationTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    static final String NONCE = "f3a4b5c6";
    static final int B = 200;
    static final Duration SAMPLE = Duration.ofMillis(100);
    static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource adminDs;
    PciCatalog catalog;
    final List<String> tenants = new ArrayList<>();
    final ExecutorService async = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "pcibootwork-test-async");
        t.setDaemon(true);
        return t;
    });
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

    // -- fixtures (the shapes PciReconcilerIntegrationTest uses) -------------------------------------------------

    static PciSettings settings() {
        return new PciSettings(true, B, 600, 16);
    }

    PciReconciler reconciler(BootWorkRegistry registry) {
        PciSettings s = settings();
        return new PciReconciler(catalog,
            new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s), PciIndexSweep.create(svcDs, s),
            s, Clock.systemUTC(), PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60))
            .bootWork(registry, SAMPLE);
    }

    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        String tenant = "pcibw-" + tag;
        tenants.add(tenant);
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true);
        }
        return tenant;
    }

    static String hash16(String tenant) {
        try {
            byte[] d = java.security.MessageDigest.getInstance("SHA-256")
                .digest(tenant.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(d).substring(0, 16);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * A new tenant whose {@value M384} leaf sorts after every existing one of that model (so its leaf is the pass's
     * last build-capable leaf, the model partition's hash being the highest of the layout's) or, with
     * {@code last == false}, before the highest (so another leaf follows it). A leaf is named from the model's and the
     * tenant's SHA-256, and the pass visits leaves by name.
     */
    String newTenantSortingLast(String tag, boolean last) throws Exception {
        String highest = catalog.read().leaves().stream().filter(l -> M384.equals(l.model()) && l.tenant() != null)
            .map(l -> hash16(l.tenant())).max(String::compareTo).orElseThrow();
        for (int i = 0; i < 2000; i++) {
            String candidate = tag + "-" + i;
            int order = hash16("pcibw-" + candidate).compareTo(highest);
            if (last ? order > 0 : order < 0) {
                String tenant = newTenant(candidate);
                assertThat(lastPlannableIs(tenant)).as("%s sorts %s", tenant, last ? "last" : "not last")
                    .isEqualTo(last);
                return tenant;
            }
        }
        throw new AssertionError("no tenant name sorted " + (last ? "last" : "before the last"));
    }

    static String name(String tag) {
        return "knowledge__pcibw-" + tag + "__minilm-l6-v2-384__v1";
    }

    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    void collection(String tenant, String collection, int rows) throws Exception {
        int gen = ++generation;
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            for (int from = 0; from < rows; from += 250) {
                var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                    CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA);
                for (int i = from; i < Math.min(rows, from + 250); i++) {
                    String h = Chash.ofText(tenant + "/" + collection + "/" + gen + "/" + i).toHex();
                    step = step.values(tenant, collection, Chash.fromHex(h).toBytes(), M384, "row " + i,
                        vector(i), JSONB.jsonb("{}"));
                }
                step.onConflictDoNothing().execute();
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

    boolean hasInvalid(String tenant) {
        return catalog.read().leaves().stream().filter(l -> M384.equals(l.model()) && tenant.equals(l.tenant()))
            .flatMap(l -> l.indexes().stream()).anyMatch(i -> !i.valid());
    }

    void signalBuilder(String function) throws Exception {
        asSuperuser(su -> su.select(DSL.function(function, SQLDataType.BOOLEAN,
                DSL.field(DSL.name("pid"), Integer.class)))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .fetch());
    }

    static void await(BooleanSupplier condition, String what) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("timed out waiting for " + what);
            }
            Thread.sleep(25);
        }
    }

    static <T> T awaitValue(Supplier<T> source, java.util.function.Predicate<T> ok, String what) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        T v = source.get();
        while (!ok.test(v)) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("timed out waiting for " + what + "; last value " + v);
            }
            Thread.sleep(25);
            v = source.get();
        }
        return v;
    }

    CompletableFuture<PassReport> inBackground(PciReconciler r) {
        return CompletableFuture.supplyAsync(r::reconcileOnce, async);
    }

    /** GET /v1/status through the real handler, fed the registry's snapshot. */
    JsonNode bootWorkOnTheWire(BootWorkRegistry registry) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/status", new StatusHandler(null, null).bootWork(registry::snapshot));
        server.start();
        try {
            HttpResponse<String> resp = dev.nexus.service.TestHttp.client().send(
                dev.nexus.service.TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/status")
                    .GET().build(), HttpResponse.BodyHandlers.ofString());
            assertThat(resp.statusCode()).isEqualTo(200);
            return MAPPER.readTree(resp.body()).get("boot_work");
        } finally {
            server.stop(0);
        }
    }

    /** Whether the last leaf of the catalog that can plan a build (model and tenant parsed) is {@code tenant}'s. */
    boolean lastPlannableIs(String tenant) {
        PciCatalog.Leaf last = null;
        for (PciCatalog.Leaf l : catalog.read().leaves()) {
            if (l.model() != null && l.tenant() != null) {
                last = l;
            }
        }
        return last != null && M384.equals(last.model()) && tenant.equals(last.tenant());
    }

    /**
     * Takes a snapshot of the registry as fast as it can while a pass runs, and keeps every way the entry can mislead:
     * done past a known total, a total that changes or goes back to unknown once it is known, done going backward.
     * One pass is one run of the entry, so its memory is cleared when the list empties.
     */
    final class Watcher implements AutoCloseable {
        final List<String> violations = java.util.Collections.synchronizedList(new ArrayList<>());
        final java.util.concurrent.atomic.AtomicLong snapshotsWithEntry = new java.util.concurrent.atomic.AtomicLong();
        private final java.util.concurrent.atomic.AtomicBoolean running = new java.util.concurrent.atomic.AtomicBoolean(true);
        private final CompletableFuture<Void> loop;

        Watcher(BootWorkRegistry registry) {
            loop = CompletableFuture.runAsync(() -> {
                Long lastTotal = null;
                long lastDone = 0;
                while (running.get()) {
                    var e = only(registry);
                    if (e == null) {
                        lastTotal = null;
                        lastDone = 0;
                    } else {
                        snapshotsWithEntry.incrementAndGet();
                        if (e.total() != null && e.done() != null && e.done() > e.total()) {
                            violations.add("done " + e.done() + " is past total " + e.total());
                        }
                        if (lastTotal != null && e.total() == null) {
                            violations.add("total " + lastTotal + " went back to unknown");
                        }
                        if (lastTotal != null && e.total() != null && !e.total().equals(lastTotal)) {
                            violations.add("total changed " + lastTotal + " -> " + e.total());
                        }
                        if (e.done() != null && e.done() < lastDone) {
                            violations.add("done went backward " + lastDone + " -> " + e.done());
                        }
                        if (e.total() != null) {
                            lastTotal = e.total();
                        }
                        lastDone = e.done() == null ? lastDone : e.done();
                    }
                    Thread.onSpinWait();
                }
            }, async);
        }

        @Override
        public void close() throws Exception {
            running.set(false);
            loop.get(10, TimeUnit.SECONDS);
        }
    }

    /** One raw row of the builder backend's {@code pg_stat_progress_create_index}, or null when it has none. */
    record Raw(String phase, long blocksDone, long blocksTotal, long tuplesDone, long tuplesTotal) { }

    Raw rawProgress() throws Exception {
        return asSuperuser(su -> {
            var p = DSL.table(DSL.name("pg_catalog", "pg_stat_progress_create_index")).as("p");
            var a = DSL.table(DSL.name("pg_catalog", "pg_stat_activity")).as("a");
            var row = su.select(DSL.field(DSL.name("p", "phase"), String.class),
                    DSL.field(DSL.name("p", "blocks_done"), Long.class),
                    DSL.field(DSL.name("p", "blocks_total"), Long.class),
                    DSL.field(DSL.name("p", "tuples_done"), Long.class),
                    DSL.field(DSL.name("p", "tuples_total"), Long.class))
                .from(p).join(a).on(DSL.field(DSL.name("a", "pid"), Integer.class)
                    .eq(DSL.field(DSL.name("p", "pid"), Integer.class)))
                .where(DSL.field(DSL.name("a", "application_name"), String.class)
                    .eq(PciBuilderSession.builderApplicationName(NONCE)))
                .limit(1)
                .fetchOne();
            return row == null ? null : new Raw(row.value1(), row.value2(), row.value3(), row.value4(), row.value5());
        });
    }

    static BootWorkRegistry.Entry only(BootWorkRegistry registry) {
        List<BootWorkRegistry.Entry> now = registry.snapshot();
        return now.isEmpty() ? null : now.get(0);
    }

    // -- tests -------------------------------------------------------------------------------------------------

    private static final String ON_STOP_TODAY =
        "the build keeps running in the database and is adopted after a restart, unless that restart applies a schema change; "
        + "stopping the database, or a restart that applies a schema change, ends it and it is rebuilt from the start";

    @Test
    void aPassWithBuildsInItsLastLeaf_announcesThemWithTheRealTotal_samplesTheBuildersPhase_andLeavesTheListEmpty() throws Exception {
        String tenant = newTenantSortingLast("run", true);
        collection(tenant, name("b"), B);
        collection(tenant, name("big"), 2 * B + 50);
        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());
        assertThat(registry.snapshot()).as("idle before the pass").isEmpty();
        // Both builds are this tenant's leaf's, and that leaf is the pass's last build-capable one: the total is the
        // real one as soon as the leaf has planned.
        Long expectedTotal = 2L;

        try (HeldWrite held = new HeldWrite(tenant); Watcher watcher = new Watcher(registry)) {
            CompletableFuture<PassReport> pass = inBackground(reconciler(registry));
            var entry = awaitValue(() -> only(registry), e -> e != null && e.step() != null,
                "the entry with a sampled step");

            assertThat(entry.id()).isEqualTo("pci-reconcile");
            assertThat(entry.what()).isEqualTo("building search indexes");
            assertThat(entry.unit()).isEqualTo("indexes");
            assertThat(entry.done()).isEqualTo(0L);
            assertThat(entry.total()).as("the last build-capable leaf has planned: the total is known")
                .isEqualTo(expectedTotal);
            assertThat(entry.safeToStop()).isTrue();
            assertThat(entry.onStop()).isEqualTo(ON_STOP_TODAY);
            assertThat(entry.startedAt()).isNotNull();
            assertThat(entry.step().what()).containsIgnoringCase("waiting for writers");

            // The same state through the real status handler.
            JsonNode wire = bootWorkOnTheWire(registry);
            assertThat(wire).hasSize(1);
            assertThat(wire.get(0).get("id").asText()).isEqualTo("pci-reconcile");
            assertThat(wire.get(0).get("total").asLong()).isEqualTo(2L);
            assertThat(wire.get(0).get("step").get("what").asText()).containsIgnoringCase("waiting for writers");

            held.close();
            PassReport report = pass.get(60, TimeUnit.SECONDS);
            assertThat(report.builds()).isEqualTo(2);
            watcher.close();
            assertThat(watcher.snapshotsWithEntry.get()).as("the watcher saw the entry").isPositive();
            assertThat(watcher.violations).isEmpty();
        }

        assertThat(registry.snapshot()).as("empty once the pass ends").isEmpty();
        assertThat(bootWorkOnTheWire(registry)).isEmpty();
    }

    @Test
    void aPassWithBuildsInAnEarlierLeaf_announcesThemWithNoTotalYet_andLeavesTheListEmpty() throws Exception {
        String tenant = newTenantSortingLast("early", false);
        collection(tenant, name("b"), B);
        collection(tenant, name("big"), 2 * B + 50);
        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());
        assertThat(registry.snapshot()).as("idle before the pass").isEmpty();
        // Both builds are this tenant's leaf's, but another build-capable leaf follows it: the total is not known yet.
        Long expectedTotal = null;

        try (HeldWrite held = new HeldWrite(tenant); Watcher watcher = new Watcher(registry)) {
            CompletableFuture<PassReport> pass = inBackground(reconciler(registry));
            var entry = awaitValue(() -> only(registry), e -> e != null && e.step() != null,
                "the entry with a sampled step");

            assertThat(entry.id()).isEqualTo("pci-reconcile");
            assertThat(entry.what()).isEqualTo("building search indexes");
            assertThat(entry.unit()).isEqualTo("indexes");
            assertThat(entry.done()).isEqualTo(0L);
            assertThat(entry.total()).as("a later leaf has still to plan: the total is not known")
                .isEqualTo(expectedTotal);
            assertThat(entry.safeToStop()).isTrue();
            assertThat(entry.onStop()).isEqualTo(ON_STOP_TODAY);
            assertThat(entry.startedAt()).isNotNull();
            assertThat(entry.step().what()).containsIgnoringCase("waiting for writers");

            // The same state through the real status handler.
            JsonNode wire = bootWorkOnTheWire(registry);
            assertThat(wire).hasSize(1);
            assertThat(wire.get(0).get("id").asText()).isEqualTo("pci-reconcile");
            assertThat(wire.get(0).get("total").isNull()).isTrue();
            assertThat(wire.get(0).get("step").get("what").asText()).containsIgnoringCase("waiting for writers");

            held.close();
            PassReport report = pass.get(60, TimeUnit.SECONDS);
            assertThat(report.builds()).isEqualTo(2);
            watcher.close();
            assertThat(watcher.snapshotsWithEntry.get()).as("the watcher saw the entry").isPositive();
            assertThat(watcher.violations).isEmpty();
        }

        assertThat(registry.snapshot()).as("empty once the pass ends").isEmpty();
        assertThat(bootWorkOnTheWire(registry)).isEmpty();
    }

    /** A SHARE UPDATE EXCLUSIVE holder on the leaf: a DROP INDEX CONCURRENTLY there waits out its lock timeout. */
    final class DropBlocker implements AutoCloseable {
        private final Connection conn;
        private boolean closed;

        DropBlocker(PciCatalog.Leaf leaf) throws Exception {
            conn = pg.createConnection("");
            conn.setAutoCommit(false);
            PgContainerHelper.lockTable(conn, DSL.table(DSL.name(leaf.schema(), leaf.name())),
                PgContainerHelper.LockMode.SHARE_UPDATE_EXCLUSIVE);
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
     * The branch where the executor skips a planned build because its failed index could not be dropped
     * ({@code PciReconciler.execute} calling {@code buildSkipped}): leaf A holds a failed (INVALID) index whose drop
     * is blocked, leaf B (the pass's last) holds a build, held open. While B's build is in flight, A's skipped build
     * has been closed out and B's is pending: done 1 of 2.
     */
    @Test
    void aPlannedBuildSkippedForAFailedIndexWhoseDropCannotRun_countsAsEnded_whileTheNextLeafBuilds() throws Exception {
        String a = newTenantSortingLast("skip-a", false);
        String b = newTenantSortingLast("skip-b", true);
        String stuck = name("stuck");
        collection(a, stuck, B);
        collection(b, name("y"), B);
        PciCatalog.Leaf leafA = catalog.read().leaves().stream()
            .filter(l -> M384.equals(l.model()) && a.equals(l.tenant())).findFirst().orElseThrow();
        String failed = PciCatalog.indexName(M384, a, stuck);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.createLeafHnswIndex(su, leafA.schema(), leafA.name(), failed, "embedding_384", stuck);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.markIndexInvalid(su, leafA.schema(), failed);
        }
        assertThat(hasInvalid(a)).as("A holds a failed index").isTrue();
        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());

        try (DropBlocker blocker = new DropBlocker(leafA); HeldWrite held = new HeldWrite(b);
             Watcher watcher = new Watcher(registry)) {
            CompletableFuture<PassReport> pass = inBackground(reconciler(registry));
            // B's build is held at "waiting for writers"; A (visited first) has been skipped by then.
            var entry = awaitValue(() -> only(registry), e -> e != null && e.step() != null,
                "the entry with B's sampled build");

            assertThat(entry.total()).as("B is the last leaf and has planned: A's build and B's").isEqualTo(2L);
            assertThat(entry.done()).as("A's skipped build is closed out, B's is pending").isEqualTo(1L);

            // A's drop has given up by now; release both so the rest of the pass is not queued behind A's lock.
            held.close();
            blocker.close();
            PassReport report = pass.get(60, TimeUnit.SECONDS);
            assertThat(report.drops()).as("A's drop timed out").isZero();
            assertThat(report.builds()).as("only B's build ran").isEqualTo(1);
            watcher.close();
            assertThat(watcher.violations).isEmpty();
        }
        assertThat(registry.snapshot()).as("empty once the pass ends").isEmpty();
    }

    @Test
    void doneCountsFinishedBuilds_andTheTotalNeverGoesBackwardOrReadsFullBeforeTheLastLeafHasPlanned() throws Exception {
        // a sorts before b, and b's leaf is the pass's last build-capable one: a builds first with the total unknown,
        // b's plan then completes it.
        String a = newTenantSortingLast("tot-a", false);
        String b = newTenantSortingLast("tot-b", true);
        collection(a, name("x"), B);
        collection(b, name("y"), B);
        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());

        try (HeldWrite heldA = new HeldWrite(a); HeldWrite heldB = new HeldWrite(b);
             Watcher watcher = new Watcher(registry)) {
            CompletableFuture<PassReport> pass = inBackground(reconciler(registry));
            // The pass blocks in the first leaf's build; which leaf is first is not fixed, so ask the catalog.
            await(() -> hasInvalid(a) || hasInvalid(b), "the first build's invalid index");
            var first = awaitValue(() -> only(registry), e -> e != null, "the entry");
            assertThat(first.done()).isEqualTo(0L);
            assertThat(first.total()).as("a second leaf with a build is still to come: the total is not known")
                .isNull();

            String firstTenant = hasInvalid(a) ? a : b;
            String secondTenant = firstTenant.equals(a) ? b : a;
            (firstTenant.equals(a) ? heldA : heldB).close();
            await(() -> hasInvalid(secondTenant), "the second build's invalid index");
            var second = awaitValue(() -> only(registry), e -> e != null && e.done() == 1L,
                "one build done");
            assertThat(second.unit()).isEqualTo("indexes");
            assertThat(firstTenant).as("the leaves are visited in name order").isEqualTo(a);
            assertThat(second.total()).as("the second tenant's leaf is the last build-capable one: the total is known")
                .isEqualTo(2L);

            heldA.close();
            heldB.close();
            assertThat(pass.get(60, TimeUnit.SECONDS).builds()).isEqualTo(2);
            watcher.close();
            assertThat(watcher.snapshotsWithEntry.get()).as("the watcher saw the entry").isPositive();
            assertThat(watcher.violations).as("done never past a known total; a known total never moves").isEmpty();
        }
        assertThat(registry.snapshot()).isEmpty();
    }

    @Test
    void theWireNamesNoCollectionTenantOrIndex_inTheStepOrAnywhereElseInTheEntry() throws Exception {
        String tenant = newTenant("quiet");
        collection(tenant, name("secretly-named"), B);
        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());

        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = inBackground(reconciler(registry));
            var e = awaitValue(() -> only(registry), x -> x != null && x.step() != null, "a step");
            assertThat(e.step().what()).containsIgnoringCase("waiting for writers");

            String wire = bootWorkOnTheWire(registry).toString();
            assertThat(wire).contains("waiting for writers")
                .doesNotContain("secretly-named").doesNotContain(tenant).doesNotContain("pci_")
                .doesNotContain("knowledge__");
            held.close();
            pass.get(60, TimeUnit.SECONDS);
        }
    }

    /**
     * A build that is not held, on a collection big enough to run for seconds, passes through the counting phases. The
     * production sampler's columns are compared with a raw read of the same view taken either side of it: a swapped
     * column (blocks for tuples, done for total) cannot match.
     */
    @Test
    @Timeout(value = 600, unit = TimeUnit.SECONDS)
    void aCountingPhaseSample_matchesTheViewsColumns_andReachesTheWire() throws Exception {
        String tenant = newTenant("count");
        collection(tenant, name("large"), 20_000);   // big enough that the HNSW build runs for seconds
        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());
        PciBuilderSession probe = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, settings());
        int matched = 0;
        int onTheWire = 0;
        {
            CompletableFuture<PassReport> pass = inBackground(reconciler(registry));
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(240);
            while (!pass.isDone() && System.nanoTime() < deadline) {
                Raw before = rawProgress();
                var sample = probe.sampleBuildProgress();
                Raw after = rawProgress();
                if (before != null && after != null && sample.isPresent()
                    && before.phase().equals(after.phase()) && before.phase().equals(sample.get().phase())
                    && before.blocksTotal() == after.blocksTotal() && before.tuplesTotal() == after.tuplesTotal()
                    && (before.blocksTotal() > 0 || before.tuplesTotal() > 0)
                    && before.blocksTotal() != before.tuplesTotal()) {
                    var got = sample.get();
                    assertThat(got.blocksTotal()).as("blocks_total in phase %s", got.phase()).isEqualTo(before.blocksTotal());
                    assertThat(got.tuplesTotal()).as("tuples_total in phase %s", got.phase()).isEqualTo(before.tuplesTotal());
                    assertThat(got.blocksDone()).as("blocks_done in phase %s", got.phase())
                        .isBetween(before.blocksDone(), after.blocksDone());
                    assertThat(got.tuplesDone()).as("tuples_done in phase %s", got.phase())
                        .isBetween(before.tuplesDone(), after.tuplesDone());
                    matched++;
                }
                var e = only(registry);
                if (e != null && e.step() != null && e.step().total() != null) {
                    assertThat(e.step().total()).isPositive();
                    assertThat(e.step().done()).as("a step's done never passes its total").isLessThanOrEqualTo(e.step().total());
                    assertThat(e.step().unit()).isIn("blocks", "tuples");
                    onTheWire++;
                }
                Thread.sleep(15);
            }
            assertThat(pass.get(120, TimeUnit.SECONDS).builds()).isEqualTo(1);
        }

        assertThat(matched).as("samples in a counting phase that a raw read of the view confirmed").isPositive();
        assertThat(onTheWire).as("steps with counts the registry held while the build ran").isPositive();
    }

    @Test
    void aFailedBuild_endsTheEntry() throws Exception {
        String tenant = newTenant("fail");
        collection(tenant, name("b"), B);
        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());

        PassReport report;
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = inBackground(reconciler(registry));
            awaitValue(() -> only(registry), e -> e != null, "the entry");
            await(() -> hasInvalid(tenant), "the build's invalid index");
            signalBuilder("pg_cancel_backend");
            held.close();
            report = pass.get(60, TimeUnit.SECONDS);
        }

        assertThat(report.failedBuilds()).isEqualTo(1);
        assertThat(registry.snapshot()).isEmpty();
    }

    @Test
    void aPassEndedUnderItByALostBackend_endsTheEntry() throws Exception {
        String tenant = newTenant("lost");
        collection(tenant, name("b"), B);
        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());

        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = inBackground(reconciler(registry));
            awaitValue(() -> only(registry), e -> e != null, "the entry");
            await(() -> hasInvalid(tenant), "the build's invalid index");
            signalBuilder("pg_terminate_backend");
            held.close();
            pass.get(60, TimeUnit.SECONDS);
        }

        assertThat(registry.snapshot()).isEmpty();
    }

    @Test
    void aFailedIndexIsDroppedAndRebuiltInOnePass_theRebuildIsPlannedAndCounted_andTheEntryEndsClean() throws Exception {
        // nexus-f3yqd (F): the plan that drops a failed index also holds its rebuild, so the pass's count of builds
        // includes it. A lost backend leaves the INVALID index and is not charged to the failure backoff, so the next
        // pass (a fresh reconciler, as after a restart) drops and rebuilds at once.
        String tenant = newTenantSortingLast("again", true);
        collection(tenant, name("b"), B);
        BootWorkRegistry first = new BootWorkRegistry(Clock.systemUTC());
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = inBackground(reconciler(first));
            await(() -> hasInvalid(tenant), "the build's invalid index");
            signalBuilder("pg_terminate_backend");
            held.close();
            pass.get(60, TimeUnit.SECONDS);
        }
        assertThat(hasInvalid(tenant)).as("the ended build left its index INVALID").isTrue();

        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());
        PassReport second;
        try (Watcher watcher = new Watcher(registry)) {
            second = reconciler(registry).reconcileOnce();
            watcher.close();
            assertThat(watcher.violations).as("done never past a known total; a known total never moves").isEmpty();
        }

        assertThat(second.drops()).isEqualTo(1);
        assertThat(second.builds()).as("the drop frees the name, so the same pass rebuilds").isEqualTo(1);
        assertThat(hasInvalid(tenant)).isFalse();
        assertThat(registry.snapshot()).isEmpty();
    }

    @Test
    void aPassThatPlansNoBuild_registersNothing() throws Exception {
        String tenant = newTenant("idle");
        collection(tenant, name("small"), B / 4);
        BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());
        List<Integer> sizes = new ArrayList<>();
        // A sampler thread watches the whole pass: the entry must never appear.
        var watching = new java.util.concurrent.atomic.AtomicBoolean(true);
        CompletableFuture<Void> watcher = CompletableFuture.runAsync(() -> {
            while (watching.get()) {
                sizes.add(registry.snapshot().size());
                Thread.onSpinWait();
            }
        }, async);

        PassReport report = reconciler(registry).reconcileOnce();
        watching.set(false);
        watcher.get(10, TimeUnit.SECONDS);

        assertThat(report.builds()).isZero();
        assertThat(sizes).isNotEmpty().allMatch(n -> n == 0);
    }
}
