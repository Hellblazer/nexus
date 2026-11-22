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
import dev.nexus.service.vectors.PciBuilderSession.DdlOutcome;
import dev.nexus.service.vectors.PciCanary.Result;
import dev.nexus.service.vectors.PciCanary.State;
import dev.nexus.service.vectors.PciCanary.Target;
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
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-230 P2.3 review round 1 (nexus-4lnn1.1.10): the leaf size rule's reads and limits against the real partitioned
 * layout (a dedicated container migrated by the schema owner, which the builder connects as): the shared catalog read
 * takes no lock on a leaf index; the rule's own read of an index's size and anchor is bounded by a 5 s lock timeout; the
 * row floor includes {@code reltuples}; one LEAF_SIZE REINDEX per index per day, held across a restart; and a leaf
 * REINDEX's failure and lost-backend outcomes as the backoff sees them. Every test uses a tenant of its own and
 * removes its leaves afterwards. The identification, leftovers and guards are {@link PciLeafIndexIntegrationTest}'s.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciLeafIndexMaintenanceIntegrationTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    static final String NONCE = "d3e4f5a6";
    static final int B = 200;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource adminDs;
    PciCatalog catalog;
    final List<String> tenants = new ArrayList<>();
    final ExecutorService async = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "pcimaint-test-async");
        t.setDaemon(true);
        return t;
    });
    ListAppender<ILoggingEvent> appender;

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
        for (Class<?> c : List.of(PciReconciler.class, PciBuilderSession.class, PciLeafIndexReads.class)) {
            var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(c);
            logger.setLevel(Level.DEBUG);
            logger.addAppender(appender);
        }
    }

    @AfterEach
    @SuppressWarnings("deprecation")
    void removeTenants() throws Exception {
        for (Class<?> c : List.of(PciReconciler.class, PciBuilderSession.class, PciLeafIndexReads.class)) {
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

    static PciSettings settings() {
        return new PciSettings(true, B, 600, 16);
    }

    PciBuilderSession builderSession(PciSettings s) {
        return new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s);
    }

    PciReconciler reconciler(PciSettings s) {
        return new PciReconciler(catalog, builderSession(s), PciIndexSweep.create(svcDs, s), s,
            java.time.Clock.systemUTC(), PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT,
            Duration.ofSeconds(60));
    }

    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        String tenant = "pcimaint-" + tag;
        tenants.add(tenant);
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true, true);
        }
        return tenant;
    }

    static String name(String tag) {
        return "knowledge__pcimaint-" + tag + "__minilm-l6-v2-384__v1";
    }

    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    static Vector vector(int i) {
        float[] v = new float[384];
        v[i % 384] = 1f;
        v[(i * 7 + 1) % 384] += 0.5f;
        return Vector.of(v);
    }

    /** Register {@code collection} for {@code tenant} and give it {@code rows} rows. */
    void collection(String tenant, String collection, int rows) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            Long cid = CollectionIds.require(su, tenant, collection);
            for (int i = 0; i < rows; i++) {
                su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                        CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA, CHUNKS.COLLECTION_ID)
                    .values(tenant, collection, Chash.fromHex(Chash.ofText(tenant + "/" + collection + "/" + i)
                        .toHex()).toBytes(), M384, "row " + i, vector(i), JSONB.jsonb("{}"), cid)
                    .onConflictDoNothing().execute();
            }
            return null;
        });
    }

    PciCatalog.Leaf leaf(String tenant) {
        List<PciCatalog.Leaf> hits = catalog.read().leaves().stream()
            .filter(l -> M384.equals(l.model()) && tenant.equals(l.tenant())).toList();
        assertThat(hits).as("exactly one leaf for (%s, %s)", M384, tenant).hasSize(1);
        return hits.get(0);
    }

    PciCatalog.LeafIndex own384(String tenant) {
        return leaf(tenant).hnswIndex(384).orElseThrow();
    }

    /** An open transaction holding a RowExclusiveLock on the tenant's leaf: it holds a concurrent reindex open. */
    final class HeldWrite implements AutoCloseable {
        private final Connection conn;
        private boolean closed;

        HeldWrite(String tenant) throws Exception {
            String held = name("held-" + tenant);
            Long heldId = asSuperuser(su -> {
                PgContainerHelper.insertCollection(su, tenant, held, M384);
                return CollectionIds.require(su, tenant, held);
            });
            conn = pg.createConnection("");
            conn.setAutoCommit(false);
            DSL.using(conn, SQLDialect.POSTGRES)
                .insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                    CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA, CHUNKS.COLLECTION_ID)
                .values(tenant, held, Chash.fromHex(Chash.ofText("held/" + tenant).toHex()).toBytes(), M384, "held",
                    vector(1), JSONB.jsonb("{}"), heldId)
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

    static void setSession(Connection conn, String name, String value) {
        DSL.using(conn, SQLDialect.POSTGRES).select(DSL.function("set_config", SQLDataType.VARCHAR,
            DSL.val(name), DSL.val(value), DSL.inline(false))).fetch();
    }

    static boolean causedBy(Throwable t, String needle) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c.getMessage() != null && c.getMessage().contains(needle)) {
                return true;
            }
        }
        return false;
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

    /** The oid of the relation {@code name} in {@code schema}, as text; a REINDEX CONCURRENTLY changes it. */
    String oidOf(String schema, String name) throws Exception {
        return asSuperuser(su -> su.select(DSL.field(DSL.name("c", "oid")).cast(String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(name))
            .fetchOne(0, String.class));
    }

    /** The name of the index {@code name} in {@code schema} is attached to ({@code pg_inherits}), or null. */
    String parentIndexOf(String schema, String name) throws Exception {
        return asSuperuser(su -> su.select(DSL.field(DSL.name("p", "relname"), String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .join(DSL.table(DSL.name("pg_catalog", "pg_inherits")).as("h"))
            .on(DSL.field(DSL.name("h", "inhrelid")).eq(DSL.field(DSL.name("c", "oid"))))
            .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("p"))
            .on(DSL.field(DSL.name("p", "oid")).eq(DSL.field(DSL.name("h", "inhparent"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(name))
            .fetchOne(0, String.class));
    }

    /** The comment of the index {@code name} in {@code schema} ({@code obj_description}), or null. */
    String commentOf(String schema, String name) throws Exception {
        return asSuperuser(su -> su.select(DSL.function(DSL.name("pg_catalog", "obj_description"), SQLDataType.CLOB,
                DSL.field(DSL.name("c", "oid")), DSL.inline("pg_class")))
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(name))
            .fetchOne(0, String.class));
    }

    /** {@code pg_relation_size} of the index {@code name} in {@code schema}. */
    long sizeOf(String schema, String name) throws Exception {
        return asSuperuser(su -> su.select(DSL.function(DSL.name("pg_catalog", "pg_relation_size"), SQLDataType.BIGINT,
                DSL.field(DSL.name("c", "oid"))))
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(name))
            .fetchOne(0, Long.class));
    }

    boolean exists(String schema, String name) throws Exception {
        return oidOf(schema, name) != null;
    }

    static final String REINDEX_APP = "pcimaint-reindex";

    /**
     * {@code REINDEX INDEX CONCURRENTLY} on {@code base} as an operator would, with a writer holding the leaf open so
     * the reindex stalls after it has committed its invalid {@code _ccnew}, and is ended by its statement timeout: the
     * genuine leftover. Returns the leftover's name.
     */
    String reindexAndFailLeftBehind(String tenant, PciCatalog.Leaf leaf, String base) throws Exception {
        try (HeldWrite held = new HeldWrite(tenant); Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            setSession(su, "statement_timeout", "2s");
            Throwable failure = null;
            try {
                PgContainerHelper.reindexIndexConcurrently(su, leaf.schema(), base);
            } catch (Exception e) {
                failure = e;
            }
            assertThat(failure).as("the reindex must end by its timeout").isNotNull();
            assertThat(causedBy(failure, "canceling statement due to statement timeout")).isTrue();
        }
        return base + "_ccnew";
    }


    // -- RDR-230 P2.3c: the leaf size rule in the reconciler's REINDEX phase -------------------------------------

    static final PciSettings REINDEX_OFF = new PciSettings(true, B, 600, 16, null, false);

    /** The canary: every run ends in {@link #state} with ten queries. */
    static final class ScriptedCanary implements PciCanarySchedule.Runner {
        volatile State state = State.GREEN;

        private Result result(Target target, String trigger) {
            boolean red = state == State.RED;
            return new Result(target, trigger, state, null, 10, red ? 0.9 : 1.0, red ? 0.95 : 1.0, red ? 1 : 0,
                java.time.Instant.now(), red ? List.of(new float[] {1f, 2f, 3f}) : null, null);
        }

        @Override public Result run(Target target, String trigger) {
            return result(target, trigger);
        }

        @Override public Result remeasure(Target target, String trigger, List<float[]> sameQueries) {
            return result(target, trigger);
        }
    }

    /** The real clock, moved forward by the test: an anchor's {@code built_at} is compared with database-stamped times. */
    static final class MovingClock extends java.time.Clock {
        private volatile Duration offset = Duration.ZERO;

        void advance(Duration d) {
            offset = offset.plus(d);
        }

        @Override public java.time.ZoneId getZone() {
            return java.time.ZoneOffset.UTC;
        }

        @Override public java.time.Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        /** The real time plus what the test advanced: an anchor's built_at is compared with database-stamped times. */
        @Override public java.time.Instant instant() {
            return java.time.Instant.now().plus(offset);
        }
    }

    PciReconciler reconciler(PciSettings s, ScriptedCanary canary) {
        return reconciler(s, canary, java.time.Clock.systemUTC());
    }

    PciReconciler reconciler(PciSettings s, ScriptedCanary canary, java.time.Clock clock) {
        return new PciReconciler(catalog::read, builderSession(s), PciIndexSweep.create(svcDs, s), s, clock,
            PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60),
            java.util.random.RandomGenerator.getDefault(), tenantsSeen -> 0,
            new PciCanarySchedule(canary, clock, System::nanoTime));
    }

    int generation;

    /** {@code rows} more rows for the collection, in batches. */
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

    void deleteCollection(String tenant, String collection) throws Exception {
        asSuperuser(su -> su.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION.eq(collection)).execute());
    }

    /** A collection that is registered, holds rows, and never builds an index (it is not live). */
    void dormantCollection(String tenant, String collection, int rows) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            su.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "dormant")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
                .execute();
            return null;
        });
        addRows(tenant, collection, rows);
    }

    /**
     * VACUUM the leaf, which refreshes {@code pg_class.reltuples} (CREATE INDEX had set it to the count at the build;
     * the leaf rule floors the rows at it, so a bloated index shows only once deletes have been vacuumed or analyzed,
     * as autovacuum does after them) and leaves the HNSW file as large as it was.
     */
    void vacuumLeaf(PciCatalog.Leaf leaf) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.vacuumTable(su, leaf.schema(), leaf.name());
        }
    }

    /** Wait for the leaf's {@code n_live_tup} to read {@code expected}: the statistics lag the writes. */
    void awaitLiveTuples(PciCatalog.Leaf leaf, long expected) throws Exception {
        await(() -> {
            try {
                Long live = asSuperuser(su -> su.select(DSL.field(DSL.name("s", "n_live_tup"), Long.class))
                    .from(DSL.table(DSL.name("pg_catalog", "pg_stat_user_tables")).as("s"))
                    .where(DSL.field(DSL.name("s", "schemaname"), String.class).eq(leaf.schema()))
                    .and(DSL.field(DSL.name("s", "relname"), String.class).eq(leaf.name()))
                    .fetchOne(0, Long.class));
                return live != null && live == expected;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }, "n_live_tup of " + leaf.name() + " to read " + expected);
    }

    /**
     * A tenant whose leaf's own index is bloated: one live collection of 250 rows (it builds a pci_ index, whose built
     * anchor is the install reference at 384 dimensions) and a dormant one of 2,000 rows that is then deleted, so the
     * leaf's index still holds the graph of 2,250 rows while its {@code n_live_tup} reads 250. The first pass builds
     * and anchors the pci_ index; the leaf index is then about nine times the reference a row.
     */
    PciReconciler bloatedLeaf(String tenant, PciSettings settings, ScriptedCanary canary) throws Exception {
        return bloatedLeaf(tenant, settings, canary, java.time.Clock.systemUTC());
    }

    PciReconciler bloatedLeaf(String tenant, PciSettings settings, ScriptedCanary canary, java.time.Clock clock)
            throws Exception {
        collection(tenant, name("keep"), 0);
        addRows(tenant, name("keep"), 250);
        dormantCollection(tenant, name("bulk"), 2_000);
        PciReconciler r = reconciler(settings, canary, clock);
        assertThat(r.reconcileOnce().builds()).as("the pci_ index that anchors the reference").isEqualTo(1);
        deleteCollection(tenant, name("bulk"));
        vacuumLeaf(leaf(tenant));
        awaitLiveTuples(leaf(tenant), 250);
        return r;
    }

    private long logged(String needle) {
        return logs().stream().filter(l -> l.contains(needle)).count();
    }

    // -- review round 1: no lock in the shared read, row floor, cooldown, backoff -------------------------------

    /**
     * A lock held on the leaf's own index that a size read of it waits for, made with typed statements only: an open
     * transaction that has read the leaf (it holds {@code AccessShareLock} on the leaf and on every index the planner
     * opened), and a plain {@code REINDEX INDEX} of the index waiting for its {@code AccessExclusiveLock} behind it.
     * PostgreSQL queues a later {@code AccessShareLock} request behind a waiting {@code AccessExclusiveLock} request,
     * so a size read of the index waits. Proves, on construction, that this holds: a size read times out on it.
     * {@link #close} ends the reading transaction, lets the REINDEX run and waits for it.
     */
    final class HeldIndexLock implements AutoCloseable {
        private static final String REINDEXER = "pcimaint-reindexer";
        private final Connection reader;
        private final Connection reindexer;
        private final CompletableFuture<Void> reindex;
        private boolean closed;

        HeldIndexLock(PciCatalog.Leaf leaf, String index) throws Exception {
            reader = pg.createConnection("");
            reindexer = pg.createConnection("");
            try {
                reader.setAutoCommit(false);
                DSL.using(reader, SQLDialect.POSTGRES).selectCount()
                    .from(DSL.table(DSL.name(leaf.schema(), leaf.name()))).fetchOne();
                reindexer.setAutoCommit(true);
                setSession(reindexer, "application_name", REINDEXER);
                reindex = CompletableFuture.runAsync(() -> {
                    try {
                        PgContainerHelper.reindexIndex(reindexer, leaf.schema(), index);
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                }, async);
                await(() -> {
                    try {
                        return asSuperuser(su -> su.fetchExists(DSL.selectOne()
                            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                            .where(DSL.field(DSL.name("application_name"), String.class).eq(REINDEXER))
                            .and(DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock"))));
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                }, "the REINDEX waiting for its lock behind the reader");
                try (Connection probe = pg.createConnection("")) {
                    probe.setAutoCommit(true);
                    setSession(probe, "lock_timeout", "500ms");
                    Throwable failure = null;
                    try {
                        DSL.using(probe, SQLDialect.POSTGRES).select(DSL.function(DSL.name("pg_catalog",
                                "pg_relation_size"), SQLDataType.BIGINT, DSL.field(DSL.name("c", "oid"))))
                            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(leaf.schema()))
                            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(index))
                            .fetch();
                    } catch (Exception e) {
                        failure = e;
                    }
                    assertThat(failure).as("the held lock is real: a size read of the index waits").isNotNull();
                    assertThat(causedBy(failure, "lock timeout")).as(String.valueOf(failure)).isTrue();
                }
            } catch (Throwable t) {
                // An open transaction here would hold up every REINDEX CONCURRENTLY of the database.
                reader.rollback();
                reader.close();
                reindexer.close();
                throw t;
            }
        }

        @Override
        public void close() throws Exception {
            if (!closed) {
                closed = true;
                reader.rollback();
                reader.close();
                reindex.get(60, TimeUnit.SECONDS);
                reindexer.close();
            }
        }
    }

    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void theSharedCatalogRead_locksNoIndex_soAnAccessExclusiveLockOnALeafIndexDoesNotStallIt() throws Exception {
        String tenant = newTenant("readlock");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        String own = own384(tenant).name();
        var slowToFail = new PciCatalog(svcDs, Duration.ofSeconds(60));

        try (HeldIndexLock held = new HeldIndexLock(leaf, own)) {
            long startedNanos = System.nanoTime();
            PciCatalog.Snapshot snapshot = slowToFail.read();
            long tookMs = (System.nanoTime() - startedNanos) / 1_000_000;

            assertThat(tookMs).as("a read that locked the index would wait for the whole bound").isLessThan(5_000);
            assertThat(snapshot.leaves().stream().filter(l -> tenant.equals(l.tenant())).findFirst().orElseThrow()
                .hnswIndex(384)).hasValueSatisfying(i -> assertThat(i.valid()).isTrue());
        }
    }

    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void theRulesOwnReadOfALockedLeafIndex_endsOnTheFiveSecondLockTimeout_skipsItWithALogLine_andTheNextReadSucceeds()
            throws Exception {
        // The leaf's counting transaction would wait on the same lock (the planner opens every index of the table), so
        // the read is called directly: it is what the REINDEX phase calls for an index it judges.
        String tenant = newTenant("rulelock");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.LeafIndex own = leaf.hnswIndex(384).orElseThrow();

        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            try (HeldIndexLock held = new HeldIndexLock(leaf, own.name())) {
                long startedNanos = System.nanoTime();
                PciAnchorReads.IndexFacts locked = PciLeafIndexReads.facts(pass, leaf, own, Duration.ofSeconds(30));
                long tookMs = (System.nanoTime() - startedNanos) / 1_000_000;

                assertThat(locked).as("skipped for this pass").isNull();
                assertThat(tookMs).as("it waited out the 5 s lock timeout, not the statement bound")
                    .isBetween(4_500L, 15_000L);
                assertThat(logs()).anyMatch(l -> l.contains("event=pci_leaf_size_rule_skipped")
                    && l.contains("reason=index_lock_timeout") && l.contains("index=" + own.name()));
                assertThat(pass.live()).as("the pass goes on").isTrue();
            }

            PciAnchorReads.IndexFacts free = PciLeafIndexReads.facts(pass, leaf, own, Duration.ofSeconds(30));

            assertThat(free).as("with the lock gone the next read works, on the same session").isNotNull();
            assertThat(free.bytes()).isEqualTo(sizeOf(leaf.schema(), own.name()));
            assertThat(free.comment()).isNull();
        }
    }

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void theRowFloor_includesReltuples_soAZeroedNLiveTupAfterARestartIsNotABloatedIndex() throws Exception {
        String tenant = newTenant("reltuples");
        collection(tenant, name("keep"), 0);
        addRows(tenant, name("keep"), 250);
        dormantCollection(tenant, name("quiet"), 1_000);   // rows in the leaf's index and in no pci_ index
        PciReconciler r = reconciler(settings(), new ScriptedCanary());
        assertThat(r.reconcileOnce().builds()).isEqualTo(1);
        PciCatalog.Leaf leaf = leaf(tenant);
        String own = own384(tenant).name();
        String oid = oidOf(leaf.schema(), own);
        awaitLiveTuples(leaf, 1_250);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.vacuumTable(su, leaf.schema(), leaf.name());   // reltuples now carries the count
            // What an unclean shutdown does to the statistics: n_live_tup back to 0 while reltuples survives.
            DSL.using(su, SQLDialect.POSTGRES).select(DSL.function("pg_stat_reset_single_table_counters",
                    SQLDataType.OTHER, DSL.field(DSL.name("c", "oid"))))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(leaf.schema()))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq(leaf.name()))
                .fetch();
        }
        awaitLiveTuples(leaf, 0);
        Long estimate = asSuperuser(su -> PciAnchorReads.leafRowEstimates(su, List.of(leaf.schema()))
            .get(leaf.schema() + "." + leaf.name()));
        assertThat(estimate).as("max(n_live_tup = 0, reltuples)").isBetween(1_000L, 1_300L);

        r.reconcileOnce();
        r.reconcileOnce();

        assertThat(r.reindexes()).as("250 counted rows alone would read as a 5x bloated index").isZero();
        assertThat(oidOf(leaf.schema(), own)).isEqualTo(oid);
    }

    @Test
    @Timeout(value = 600, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void aLeafSizeReindex_isLimitedToOnePerIndexPerDay_fromTheAnchorOnTheIndex_andSurvivesARestart() throws Exception {
        String tenant = newTenant("cooldown");
        MovingClock clock = new MovingClock();
        PciReconciler first = bloatedLeaf(tenant, settings(), new ScriptedCanary(), clock);
        PciCatalog.Leaf leaf = leaf(tenant);
        String own = own384(tenant).name();
        first.reconcileOnce();
        assertThat(first.reindexes()).as("the first REINDEX, anchoring the index at the clock's now").isEqualTo(1);
        String oidAfterFirst = oidOf(leaf.schema(), own);

        // Bloat it again: rows that go into the leaf's index and are deleted again.
        dormantCollection(tenant, name("bulk2"), 2_000);
        deleteCollection(tenant, name("bulk2"));
        vacuumLeaf(leaf(tenant));
        awaitLiveTuples(leaf(tenant), 250);
        PciCatalog.Anchor reference = catalog.read().anchor(M384, tenant, name("keep")).orElseThrow();
        assertThat((double) sizeOf(leaf.schema(), own) / 250).as("precondition: bloated again")
            .isGreaterThan(1.5 * reference.bytes() / reference.rows());

        first.reconcileOnce();
        assertThat(first.reindexes()).as("inside the 24 hours").isEqualTo(1);
        PciReconciler restarted = reconciler(settings(), new ScriptedCanary(), clock);
        restarted.reconcileOnce();
        assertThat(restarted.reindexes()).as("a fresh engine has no memory; the anchor on the index holds it").isZero();
        assertThat(oidOf(leaf.schema(), own)).isEqualTo(oidAfterFirst);

        clock.advance(Duration.ofHours(25));
        restarted.reconcileOnce();

        assertThat(restarted.reindexes()).as("after the 24 hours").isEqualTo(1);
        assertThat(oidOf(leaf.schema(), own)).isNotEqualTo(oidAfterFirst);
    }

    /** An open REPEATABLE READ transaction with a snapshot: a REINDEX CONCURRENTLY ends by waiting for it. */
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

    /** End the builder's current STATEMENT (57014); its session, and so the pass, carries on. */
    void cancelBuilder() throws Exception {
        asSuperuser(su -> su.select(DSL.function("pg_cancel_backend", SQLDataType.BOOLEAN,
                DSL.field(DSL.name("pid"), Integer.class)))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .and(DSL.field(DSL.name("state"), String.class).eq("active"))
            .fetch());
    }

    /** End the builder's BACKEND (57P01), as a migration walk does: not the statement's failure. */
    void terminateBuilder() throws Exception {
        asSuperuser(su -> su.select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN,
                DSL.field(DSL.name("pid"), Integer.class)))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .fetch());
    }

    /**
     * A pass over the bloated leaf whose leaf-index REINDEX is held open by a snapshot until {@code interrupt} ends it
     * (the REINDEX has left its invalid copy by then).
     */
    void passWithAnInterruptedLeafReindex(PciReconciler r, String tenant, ThrowingRunnable interrupt) throws Exception {
        try (SnapshotHolder snapshot = new SnapshotHolder()) {
            CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
            await(() -> !leaf(tenant).leafLeftovers().isEmpty(), "the leaf REINDEX's invalid copy");
            interrupt.run();
            snapshot.close();
            pass.get(60, TimeUnit.SECONDS);
        }
    }

    @FunctionalInterface
    interface ThrowingRunnable {
        void run() throws Exception;
    }

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void aFailedLeafReindex_getsTheBuildBackoffOnTheNextPass_whichNeedsTheLeafIndexNamesInTheSnapshotSet()
            throws Exception {
        // The second pass runs PciReindexState.candidates, whose forgetAllBut drops the failure of every name not in
        // the pass's snapshot set. The leaf index name is in that set only because runReindexes adds it: without it
        // the failure is forgotten there, and this pass attempts the REINDEX again.
        String tenant = newTenant("leaffail");
        MovingClock clock = new MovingClock();
        PciReconciler r = bloatedLeaf(tenant, settings(), new ScriptedCanary(), clock);
        String own = own384(tenant).name();

        passWithAnInterruptedLeafReindex(r, tenant, this::cancelBuilder);

        assertThat(logged("event=pci_ddl_failed op=reindex")).isEqualTo(1);
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_reindex_failed") && l.contains("trigger=leaf_size")
            && l.contains("consecutive_failures=1") && l.contains("index=" + own));
        assertThat(r.reindexes()).isZero();

        r.reconcileOnce();   // drops the leftover; the index is in its backoff
        assertThat(logged("event=pci_reindex_started")).as("no attempt inside the backoff").isEqualTo(1);
        assertThat(logged("event=pci_ddl_failed op=reindex")).isEqualTo(1);
        assertThat(leaf(tenant).leafLeftovers()).as("the leftover is gone").isEmpty();

        clock.advance(PciReconciler.FIRST_RETRY.plusMinutes(1));
        r.reconcileOnce();

        assertThat(r.reindexes()).as("after the backoff it runs").isEqualTo(1);
    }

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void aLeafReindexEndedByALostBackend_isNotChargedToTheBackoff() throws Exception {
        String tenant = newTenant("leaflost");
        PciReconciler r = bloatedLeaf(tenant, settings(), new ScriptedCanary());

        passWithAnInterruptedLeafReindex(r, tenant, this::terminateBuilder);

        assertThat(logs()).anyMatch(l -> l.contains("event=pci_reindex_not_done") && l.contains("outcome=CONNECTION_LOST"));
        assertThat(logged("event=pci_reindex_failed")).as("not the index's fault").isZero();
        assertThat(r.reindexes()).isZero();

        r.reconcileOnce();   // no backoff: drops the leftover and tries again at once

        assertThat(logged("event=pci_reindex_started")).as("it was tried again on the very next pass").isEqualTo(2);
        assertThat(r.reindexes()).isEqualTo(1);
    }

    /** Rows that go into the leaf's index and are deleted again, then vacuumed (so {@code reltuples} is fresh). */
    void bloatAgain(String tenant, PciCatalog.Leaf leaf, String own, String collection) throws Exception {
        dormantCollection(tenant, name(collection), 2_000);
        deleteCollection(tenant, name(collection));
        vacuumLeaf(leaf(tenant));
        awaitLiveTuples(leaf(tenant), 250);
        PciCatalog.Anchor reference = catalog.read().anchor(M384, tenant, name("keep")).orElseThrow();
        assertThat((double) sizeOf(leaf.schema(), own) / 250).as("precondition: bloated again")
            .isGreaterThan(1.5 * reference.bytes() / reference.rows());
    }

    private HeldIndexLock newHeldLock(PciCatalog.Leaf leaf, String index) {
        try {
            return new HeldIndexLock(leaf, index);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * A bloated leaf index that someone rebuilt by hand and that has bloated again since: the size rule would fire (its
     * anchor is a day old), but the pass finds it rebuilt, anchors it as built now, and judges nothing; the cooldown then
     * counts from that anchor, so the rule fires a day later (nexus-4lnn1.1.10).
     */
    @Test
    @Timeout(value = 600, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void aLeafIndexRebuiltByHand_isReanchored_notJudged_andTheCooldownCountsFromThere() throws Exception {
        String tenant = newTenant("handrebuilt");
        MovingClock clock = new MovingClock();
        PciReconciler r = bloatedLeaf(tenant, settings(), new ScriptedCanary(), clock);
        PciCatalog.Leaf leaf = leaf(tenant);
        String own = own384(tenant).name();
        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            assertThat(pass.anchorAfterLeafReindex(leaf, own384(tenant), 250, clock.instant()).outcome())
                .isEqualTo(PciBuilderSession.DdlOutcome.DONE);
        }
        clock.advance(Duration.ofHours(25));   // its cooldown is over
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.reindexIndexConcurrently(su, leaf.schema(), own);
        }
        bloatAgain(tenant, leaf, own, "bulk2");   // the rule would fire on it now, with the old anchor

        r.reconcileOnce();

        assertThat(r.reindexes()).as("found rebuilt: re-anchored, not judged").isZero();
        assertThat(logged("event=pci_anchor_rebuilt_detected index=" + own)).isEqualTo(1);
        r.reconcileOnce();
        assertThat(r.reindexes()).as("cooling down from the new anchor, though bloated").isZero();

        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();

        assertThat(r.reindexes()).as("a day after the detection").isEqualTo(1);
    }

    @Test
    @Timeout(value = 600, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void aLeafReindexWhoseAnchorIsNotWritten_isOwedIt_isNoCandidateMeanwhile_andOnceItLandsTheCooldownHolds()
            throws Exception {
        String tenant = newTenant("owed");
        MovingClock clock = new MovingClock();
        PciReconciler r = bloatedLeaf(tenant, settings(), new ScriptedCanary(), clock);
        PciCatalog.Leaf leaf = leaf(tenant);
        String own = own384(tenant).name();
        java.util.concurrent.atomic.AtomicReference<HeldIndexLock> held = new java.util.concurrent.atomic.AtomicReference<>();

        // The REINDEX runs; a lock taken after it keeps the anchor's size read from landing (5 s lock timeout).
        r.afterLeafReindexHook = () -> held.set(newHeldLock(leaf, own));
        r.reconcileOnce();
        r.afterLeafReindexHook = null;
        held.get().close();

        assertThat(r.reindexes()).isEqualTo(1);
        assertThat(r.owedAnchors()).as("owed the fresh anchor").containsOnlyKeys(own);
        assertThat(commentOf(leaf.schema(), own)).as("no anchor on the index").isNull();
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_anchor_not_written") && l.contains("index=" + own));
        String oid = oidOf(leaf.schema(), own);

        // Bloat it again while it is owed: the rule is true, and the index must not be judged without its anchor.
        bloatAgain(tenant, leaf, own, "bulk2");
        r.reindexPhaseHook = () -> held.set(newHeldLock(leaf, own));   // the retry fails again
        r.reconcileOnce();
        r.reindexPhaseHook = null;
        held.get().close();   // (its queued plain REINDEX rebuilds the index, in place: same oid)
        assertThat(r.reindexes()).as("no candidate while its anchor is owed").isEqualTo(1);
        assertThat(r.owedAnchors()).containsOnlyKeys(own);
        assertThat(oidOf(leaf.schema(), own)).isEqualTo(oid);

        bloatAgain(tenant, leaf, own, "bulk3");
        r.reconcileOnce();   // the retry lands; the anchor now holds the cooldown
        assertThat(r.owedAnchors()).as("written: no longer owed").isEmpty();
        assertThat(PciCatalog.parseAnchor(commentOf(leaf.schema(), own))).isPresent();
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_anchor_retried") && l.contains("index=" + own));
        assertThat(r.reindexes()).as("cooling down, though still bloated").isEqualTo(1);
        r.reconcileOnce();
        assertThat(r.reindexes()).isEqualTo(1);
        assertThat(oidOf(leaf.schema(), own)).isEqualTo(oid);

        clock.advance(Duration.ofHours(25));
        r.reconcileOnce();

        assertThat(r.reindexes()).as("after the 24 hours").isEqualTo(2);
    }
}
