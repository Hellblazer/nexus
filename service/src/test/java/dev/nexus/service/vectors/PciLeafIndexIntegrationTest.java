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
 * RDR-230 P2.3 (nexus-4lnn1.1.10): a leaf's own HNSW index, its leftovers and their drop, against the real partitioned
 * layout (a dedicated container migrated by the schema owner, which the builder connects as), as
 * {@link PciReconcilerIntegrationTest} does for {@code pci_} indexes. Every test uses a tenant of its own and removes
 * its leaves afterwards.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciLeafIndexIntegrationTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    static final String NONCE = "c2d3e4f5";
    static final int B = 200;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource adminDs;
    PciCatalog catalog;
    final List<String> tenants = new ArrayList<>();
    final ExecutorService async = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "pcileaf-test-async");
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
        String tenant = "pcileaf-" + tag;
        tenants.add(tenant);
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true, true);
        }
        return tenant;
    }

    static String name(String tag) {
        return "knowledge__pcileaf-" + tag + "__minilm-l6-v2-384__v1";
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

    static final String REINDEX_APP = "pcileaf-reindex";

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

    // -- test 1: REINDEX keeps the leaf index attached (RDR-230 A5, second half) -------------------------------

    @Test
    void aReindexConcurrentlyOfALeafIndex_keepsItAttachedToTheParentIndex_andTheReadStillIdentifiesIt()
            throws Exception {
        String tenant = newTenant("attach");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        String base = own384(tenant).name();
        String parentBefore = parentIndexOf(leaf.schema(), base);
        String oidBefore = oidOf(leaf.schema(), base);
        assertThat(parentBefore).as("attached before").isNotNull();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.reindexIndexConcurrently(su, leaf.schema(), base);
        }

        assertThat(oidOf(leaf.schema(), base)).as("a new index took the name").isNotEqualTo(oidBefore);
        assertThat(parentIndexOf(leaf.schema(), base)).as("still attached to the same parent index")
            .isEqualTo(parentBefore);
        PciCatalog.Leaf after = leaf(tenant);
        assertThat(after.hnswIndex(384)).hasValueSatisfying(i -> {
            assertThat(i.name()).isEqualTo(base);
            assertThat(i.valid()).isTrue();
        });
        assertThat(after.leafLeftovers()).isEmpty();
        PassReport pass = reconciler(settings()).reconcileOnce();
        assertThat(pass.drops()).as("nothing to drop on a healthy leaf").isZero();
        assertThat(exists(leaf.schema(), base)).isTrue();
    }

    // -- test 2: the read identifies the leaf's own HNSW indexes and nothing else -------------------------------

    @Test
    void theCatalogRead_identifiesTheLeafsOwnHnswIndexes_andNothingElse() throws Exception {
        String tenant = newTenant("ident");
        collection(tenant, name("big"), B + 50);
        PciSettings s = settings();
        assertThat(reconciler(s).reconcileOnce().builds()).as("a pci_ partial index to be excluded").isEqualTo(1);
        PciCatalog.Leaf leaf = leaf(tenant);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // An operator's unpredicated hnsw index that is NOT attached to the parent's index, and a btree.
            PgContainerHelper.createHnswIndex(su, leaf.schema(), leaf.name(), "op_unattached_hnsw", "embedding_384");
            DSL.using(su, SQLDialect.POSTGRES).createIndex(DSL.name("op_plain_btree"))
                .on(DSL.table(DSL.name(leaf.schema(), leaf.name())), DSL.field(DSL.name("chash"))).execute();
        }

        PciCatalog.Snapshot snapshot = catalog.read();
        PciCatalog.Leaf read = snapshot.leaves().stream()
            .filter(l -> M384.equals(l.model()) && tenant.equals(l.tenant())).findFirst().orElseThrow();

        assertThat(read.hnswIndexes()).extracting(PciCatalog.LeafIndex::name).containsExactlyInAnyOrder(
            leaf.name() + "_embedding_384_idx", leaf.name() + "_embedding_768_idx",
            leaf.name() + "_embedding_1024_idx");
        assertThat(read.hnswIndexes()).extracting(PciCatalog.LeafIndex::dimension)
            .containsExactlyInAnyOrder(384, 768, 1024);
        assertThat(read.hnswIndexes()).allSatisfy(i -> assertThat(i.valid()).isTrue());
        assertThat(sizeOf(leaf.schema(), leaf.name() + "_embedding_384_idx")).as("the populated one is the largest")
            .isGreaterThan(sizeOf(leaf.schema(), leaf.name() + "_embedding_768_idx"));
        // The pci_ index stays a pci_ index, and neither the operator's indexes nor the btree appear in either list.
        assertThat(read.indexes()).extracting(PciCatalog.Index::name)
            .containsExactly(PciCatalog.indexName(M384, tenant, name("big")));
        assertThat(read.indexes().get(0).parsed()).isTrue();
        assertThat(read.leafLeftovers()).isEmpty();
        assertThat(snapshot.leafLeftoverCount()).isZero();
        assertThat(snapshot.unparsedCount()).as("the new kinds are in no existing count").isZero();
        // A pass leaves every one of them alone.
        PassReport pass = reconciler(s).reconcileOnce();
        assertThat(pass.drops()).isZero();
        assertThat(exists(leaf.schema(), "op_unattached_hnsw")).isTrue();
        assertThat(exists(leaf.schema(), "op_plain_btree")).isTrue();
    }

    // -- test 3: an invalid leaf leftover is dropped through runDrop on the next pass ---------------------------

    @Test
    void aRealFailedLeafReindex_leavesAnInvalidCcnew_theNextPassDropsItThroughRunDrop_andKeepsTheLeafIndex()
            throws Exception {
        String tenant = newTenant("leftover");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        String base = own384(tenant).name();

        String ccnew = reindexAndFailLeftBehind(tenant, leaf, base);

        PciCatalog.Snapshot snapshot = catalog.read();
        PciCatalog.Leaf read = snapshot.leaves().stream()
            .filter(l -> M384.equals(l.model()) && tenant.equals(l.tenant())).findFirst().orElseThrow();
        assertThat(read.leafLeftovers()).singleElement().satisfies(i -> {
            assertThat(i.name()).isEqualTo(ccnew);
            assertThat(i.valid()).isFalse();
            assertThat(i.leafLeftover()).isTrue();
            assertThat(i.leafLeftoverOf()).isEqualTo(base);
            assertThat(i.parsed()).isFalse();
            assertThat(i.leftover()).isFalse();
        });
        assertThat(read.hnswIndexes()).as("a leftover is never the leaf's own index").extracting(
            PciCatalog.LeafIndex::name).doesNotContain(ccnew);
        assertThat(read.hnswIndex(384).orElseThrow().valid()).as("the base still serves").isTrue();
        assertThat(snapshot.leafLeftoverCount()).isEqualTo(1);
        assertThat(snapshot.reindexLeftoverCount()).as("not a pci_ leftover").isZero();

        PassReport pass = reconciler(settings()).reconcileOnce();

        assertThat(pass.drops()).isEqualTo(1);
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_ddl_done") && l.contains("op=drop")
            && l.contains("index=" + ccnew));
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_index_dropped") && l.contains("index=" + ccnew)
            && l.contains("reason=leaf_reindex_leftover"));
        assertThat(exists(leaf.schema(), ccnew)).as("the leftover is gone").isFalse();
        assertThat(exists(leaf.schema(), base)).as("the leaf index stays").isTrue();
        assertThat(leaf(tenant).leafLeftovers()).isEmpty();
        assertThat(reconciler(settings()).reconcileOnce().drops()).as("a second pass has nothing to drop").isZero();
    }

    @Test
    void theLeafLeftoverDrop_rereadsIndisvalidBeforeTheDrop_aLeftoverThatIsValidNowOrGoneIsStale() throws Exception {
        String tenant = newTenant("stale");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        String base = own384(tenant).name();
        String validNow = base + "_ccnew";
        String gone = base + "_ccold";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String n : new String[] {validNow, gone}) {
                PgContainerHelper.createHnswIndex(su, leaf.schema(), leaf.name(), n, "embedding_384");
                PgContainerHelper.markIndexInvalid(su, leaf.schema(), n);
            }
        }
        PciCatalog.Leaf snapshot = leaf(tenant);
        assertThat(snapshot.leafLeftovers()).extracting(PciCatalog.Index::name)
            .containsExactlyInAnyOrder(validNow, gone);
        PciCatalog.Index validNowIndex = snapshot.leafLeftovers().stream()
            .filter(i -> i.name().equals(validNow)).findFirst().orElseThrow();
        PciCatalog.Index goneIndex = snapshot.leafLeftovers().stream()
            .filter(i -> i.name().equals(gone)).findFirst().orElseThrow();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.markIndexValid(su, leaf.schema(), validNow);
            PgContainerHelper.dropIndex(su, DSL.name(leaf.schema(), gone));
        }

        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            assertThat(pass.drop(snapshot, validNowIndex)).isEqualTo(DdlOutcome.STALE);
            assertThat(pass.drop(snapshot, goneIndex)).isEqualTo(DdlOutcome.STALE);
        }

        assertThat(exists(leaf.schema(), validNow)).as("a valid index is never dropped").isTrue();
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_drop_skipped_stale") && l.contains("reason=valid_now")
            && l.contains(validNow));
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_drop_skipped_stale") && l.contains("reason=gone")
            && l.contains(gone));
    }

    // -- test 4: a running leaf REINDEX is not dropped ----------------------------------------------------------

    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void aLeafReindexStillRunning_holdsTheLeafLock_soTheLeftoverDropTimesOutOnTheLockAndTheIndexSurvives()
            throws Exception {
        String tenant = newTenant("inflight");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        String base = own384(tenant).name();
        String ccnew = base + "_ccnew";
        PciReconciler r = reconciler(settings());

        try (HeldWrite held = new HeldWrite(tenant); Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            setSession(su, "application_name", REINDEX_APP);
            setSession(su, "statement_timeout", "60s");
            CompletableFuture<Void> reindex = CompletableFuture.runAsync(() -> {
                try {
                    PgContainerHelper.reindexIndexConcurrently(su, leaf.schema(), base);
                } catch (Exception e) {
                    // The test ends it by cancelling below.
                }
            }, async);
            await(() -> leaf(tenant).leafLeftovers().stream().anyMatch(i -> i.name().equals(ccnew)),
                "the in-progress reindex's invalid _ccnew");

            long startedNanos = System.nanoTime();
            PassReport during = r.reconcileOnce();
            long tookMs = (System.nanoTime() - startedNanos) / 1_000_000;

            // No pg_stat_progress_create_index guard: the leaf lock the reindex holds is the guard.
            assertThat(during.drops()).as("the drop did not complete").isZero();
            assertThat(tookMs).as("the drop waited out the 5 s lock timeout").isGreaterThanOrEqualTo(4_500);
            assertThat(logs()).anyMatch(l -> l.contains("event=pci_ddl_failed") && l.contains("op=drop")
                && l.contains(ccnew) && l.contains("sqlstate=55P03"));
            assertThat(logs()).anyMatch(l -> l.contains("event=pci_index_drop_not_done") && l.contains(ccnew));
            assertThat(exists(leaf.schema(), ccnew)).as("the running reindex's index survives").isTrue();
            assertThat(exists(leaf.schema(), base)).isTrue();

            asSuperuser(sctx -> sctx.select(DSL.function("pg_cancel_backend", SQLDataType.BOOLEAN,
                    DSL.field(DSL.name("pid"), Integer.class)))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class).eq(REINDEX_APP))
                .fetch());
            reindex.get(60, TimeUnit.SECONDS);
        }

        // The reindex has ended and the leftover stays invalid: the next pass drops it.
        assertThat(reconciler(settings()).reconcileOnce().drops()).isEqualTo(1);
        assertThat(exists(leaf.schema(), ccnew)).isFalse();
        assertThat(exists(leaf.schema(), base)).isTrue();
    }

    // -- the guard: no statement touches an index outside the two kinds ------------------------------------------

    @Test
    void theDropGuard_admitsOnlyALeafLeftoverOfTheLeafsOwnIndex_everythingElseIsRejected() throws Exception {
        String tenant = newTenant("guard");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        String base = own384(tenant).name();
        String other = leaf.hnswIndex(768).orElseThrow().name();
        String operator = "op_unattached_hnsw";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.createHnswIndex(su, leaf.schema(), leaf.name(), operator, "embedding_384");
            // The operator's own index has a leftover-shaped sibling: not a leftover of the leaf's own index.
            PgContainerHelper.createHnswIndex(su, leaf.schema(), leaf.name(), operator + "_ccnew", "embedding_384");
            PgContainerHelper.markIndexInvalid(su, leaf.schema(), operator + "_ccnew");
            // A genuine leftover-shaped index of the leaf's own index, to prove the contrast.
            PgContainerHelper.createHnswIndex(su, leaf.schema(), leaf.name(), base + "_ccnew", "embedding_384");
            PgContainerHelper.markIndexInvalid(su, leaf.schema(), base + "_ccnew");
        }
        PciCatalog.Leaf snapshot = leaf(tenant);
        assertThat(snapshot.leafLeftovers()).extracting(PciCatalog.Index::name).containsExactly(base + "_ccnew");

        PciCatalog.Index ownAsPlainIndex = new PciCatalog.Index(base, true, null);
        PciCatalog.Index ownAsInvalidPlain = new PciCatalog.Index(base, false, null);
        PciCatalog.Index forgedOfNonOwn = new PciCatalog.Index(operator + "_ccnew", false, null, null, null, operator);
        PciCatalog.Index wrongName = new PciCatalog.Index(base + "_ccnewX", false, null, null, null, base);
        PciCatalog.Index otherIndexLeftover = new PciCatalog.Index(base + "_ccnew", false, null, null, null, other);
        PciCatalog.Leaf unresolved = new PciCatalog.Leaf(snapshot.schema(), snapshot.name(), null, snapshot.tenant(),
            snapshot.indexes(), snapshot.hnswIndexes(), snapshot.leafLeftovers());
        PciCatalog.Index genuine = snapshot.leafLeftovers().get(0);

        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            assertThat(pass.drop(snapshot, ownAsPlainIndex)).as("the leaf's own index itself").isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.drop(snapshot, ownAsInvalidPlain)).isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.drop(snapshot, forgedOfNonOwn)).as("an operator's index is not the leaf's own")
                .isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.drop(snapshot, wrongName)).isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.drop(snapshot, otherIndexLeftover)).as("named for another own index")
                .isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.drop(unresolved, genuine)).as("a leaf the builder did not resolve")
                .isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.drop(snapshot, genuine)).isEqualTo(DdlOutcome.DONE);
        }

        for (String untouched : new String[] {base, other, operator, operator + "_ccnew"}) {
            assertThat(exists(leaf.schema(), untouched)).as(untouched).isTrue();
        }
        assertThat(exists(leaf.schema(), base + "_ccnew")).isFalse();
        // A full pass drops nothing else either.
        assertThat(reconciler(settings()).reconcileOnce().drops()).isZero();
        assertThat(exists(leaf.schema(), operator + "_ccnew")).isTrue();
    }

    // -- RDR-230 P2.3b: REINDEX and COMMENT of the leaf's own HNSW index ----------------------------------------

    @Test
    void aLeafReindexThroughTheBuilder_keepsItAttached_andTheAnchorIsWrittenThroughTheCommentForm() throws Exception {
        String tenant = newTenant("lreindex");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.LeafIndex own = own384(tenant);
        String parentBefore = parentIndexOf(leaf.schema(), own.name());
        String oidBefore = oidOf(leaf.schema(), own.name());
        assertThat(commentOf(leaf.schema(), own.name())).isNull();
        java.time.Instant builtAt = java.time.Instant.parse("2026-11-14T00:00:00Z");

        PciCatalog.Anchor written;
        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            assertThat(pass.reindex(leaf, own)).isEqualTo(DdlOutcome.DONE);
            PciBuilderSession.Pass.AnchorOutcome anchored = pass.anchorAfterLeafReindex(leaf, own, 20, builtAt);
            assertThat(anchored.outcome()).isEqualTo(DdlOutcome.DONE);
            written = anchored.anchor();
        }

        assertThat(oidOf(leaf.schema(), own.name())).as("the index was rebuilt").isNotEqualTo(oidBefore);
        assertThat(parentIndexOf(leaf.schema(), own.name())).as("still attached").isEqualTo(parentBefore);
        assertThat(leaf(tenant).hnswIndex(384)).hasValueSatisfying(i -> assertThat(i.valid()).isTrue());
        assertThat(leaf(tenant).leafLeftovers()).isEmpty();
        assertThat(written.rows()).isEqualTo(20);
        assertThat(written.origin()).isEqualTo(PciCatalog.ORIGIN_BUILT);
        assertThat(written.builtAt()).isEqualTo(builtAt);
        assertThat(written.bytes()).as("the size of the rebuilt index").isEqualTo(sizeOf(leaf.schema(), own.name()));
        assertThat(PciCatalog.parseAnchor(commentOf(leaf.schema(), own.name()))).contains(written);
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_ddl_done") && l.contains("op=reindex")
            && l.contains("index=" + own.name()));
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_ddl_done") && l.contains("op=comment")
            && l.contains("index=" + own.name()));
    }

    @Test
    void theLeafIndexCommentForm_writesTheGivenAnchor_onAnyOfTheLeafsOwnIndexes() throws Exception {
        String tenant = newTenant("lcomment");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.LeafIndex own = leaf.hnswIndex(768).orElseThrow();
        var anchor = new PciCatalog.Anchor(java.time.Instant.parse("2026-11-14T01:02:03Z"), 7, 8192,
            PciCatalog.ORIGIN_BUILT);

        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            assertThat(pass.comment(leaf, own, anchor)).isEqualTo(DdlOutcome.DONE);
        }

        assertThat(commentOf(leaf.schema(), own.name())).startsWith(anchor.toJson().replace("}", ",\"relfilenode\":"));
    }

    @Test
    void theLeafIndexGuard_rejectsAnythingButAValidMemberOfTheLeafsOwnIndexes_andSendsNoStatement() throws Exception {
        String tenant = newTenant("lguard");
        String otherTenant = newTenant("lguard2");
        collection(tenant, name("c"), 20);
        collection(otherTenant, name("d"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.LeafIndex own = own384(tenant);
        PciCatalog.LeafIndex fromOtherLeaf = own384(otherTenant);
        String operator = "op_unattached_hnsw";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.createHnswIndex(su, leaf.schema(), leaf.name(), operator, "embedding_384");
        }
        PciCatalog.LeafIndex operatorIndex = new PciCatalog.LeafIndex(operator, true, 384);
        PciCatalog.LeafIndex forged = new PciCatalog.LeafIndex(own.name() + "_x", true, 384);
        PciCatalog.LeafIndex wrongDimension = new PciCatalog.LeafIndex(own.name(), true, 768);
        PciCatalog.LeafIndex invalidOwn = new PciCatalog.LeafIndex(own.name(), false, 384);
        PciCatalog.Leaf unresolved = new PciCatalog.Leaf(leaf.schema(), leaf.name(), null, leaf.tenant(),
            leaf.indexes(), leaf.hnswIndexes(), leaf.leafLeftovers());
        var anchor = new PciCatalog.Anchor(java.time.Instant.parse("2026-11-14T00:00:00Z"), 1, 1,
            PciCatalog.ORIGIN_BUILT);
        String ownOid = oidOf(leaf.schema(), own.name());
        String operatorOid = oidOf(leaf.schema(), operator);
        String otherOid = oidOf(leaf(otherTenant).schema(), fromOtherLeaf.name());

        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            for (PciCatalog.LeafIndex bad : List.of(operatorIndex, fromOtherLeaf, forged, wrongDimension,
                    invalidOwn)) {
                assertThat(pass.reindex(leaf, bad)).as("reindex " + bad).isEqualTo(DdlOutcome.REJECTED);
                assertThat(pass.comment(leaf, bad, anchor)).as("comment " + bad).isEqualTo(DdlOutcome.REJECTED);
            }
            assertThat(pass.reindex(unresolved, own)).as("a leaf the builder did not resolve")
                .isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.comment(unresolved, own, anchor)).isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.anchorAfterLeafReindex(leaf, operatorIndex, 1, anchor.builtAt()).outcome())
                .as("the anchor helper goes through the same guard").isEqualTo(DdlOutcome.REJECTED);
        }

        assertThat(oidOf(leaf.schema(), own.name())).as("nothing was reindexed").isEqualTo(ownOid);
        assertThat(oidOf(leaf.schema(), operator)).isEqualTo(operatorOid);
        assertThat(oidOf(leaf(otherTenant).schema(), fromOtherLeaf.name())).isEqualTo(otherOid);
        for (String touched : new String[] {own.name(), operator}) {
            assertThat(commentOf(leaf.schema(), touched)).as("no comment on " + touched).isNull();
        }
        assertThat(commentOf(leaf(otherTenant).schema(), fromOtherLeaf.name())).isNull();
        assertThat(logs()).noneMatch(l -> l.contains("event=pci_ddl_done") && (l.contains("op=reindex")
            || l.contains("op=comment")));
    }

    @Test
    void anInvalidLeafIndexAsTheCatalogReadsIt_isNotReindexed() throws Exception {
        String tenant = newTenant("linvalid");
        collection(tenant, name("c"), 20);
        PciCatalog.Leaf before = leaf(tenant);
        String name384 = own384(tenant).name();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.markIndexInvalid(su, before.schema(), name384);
        }
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.LeafIndex invalid = leaf.hnswIndex(384).orElseThrow();
        assertThat(invalid.valid()).isFalse();
        String oid = oidOf(leaf.schema(), name384);

        try (PciBuilderSession.Pass pass = builderSession(settings()).open()) {
            assertThat(pass.reindex(leaf, invalid)).isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.comment(leaf, invalid, new PciCatalog.Anchor(java.time.Instant.EPOCH, 0, 0,
                PciCatalog.ORIGIN_BUILT))).isEqualTo(DdlOutcome.REJECTED);
        }

        assertThat(oidOf(leaf.schema(), name384)).isEqualTo(oid);
        assertThat(commentOf(leaf.schema(), name384)).isNull();
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

    /** VACUUM refreshes {@code reltuples}, which the leaf rule floors the rows at: deletes show once it is refreshed. */
    void vacuumLeaf(PciCatalog.Leaf leaf) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.vacuumTable(su, leaf.schema(), leaf.name());
        }
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
        collection(tenant, name("keep"), 0);
        addRows(tenant, name("keep"), 250);
        dormantCollection(tenant, name("bulk"), 2_000);
        PciReconciler r = reconciler(settings, canary);
        assertThat(r.reconcileOnce().builds()).as("the pci_ index that anchors the reference").isEqualTo(1);
        deleteCollection(tenant, name("bulk"));
        vacuumLeaf(leaf(tenant));
        awaitLiveTuples(leaf(tenant), 250);
        return r;
    }

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aBloatedLeafIndex_isReindexedAndReanchoredOnTheNextPass_andThenLeftAlone() throws Exception {
        String tenant = newTenant("bloat");
        PciReconciler r = bloatedLeaf(tenant, settings(), new ScriptedCanary());
        PciCatalog.Leaf leaf = leaf(tenant);
        PciCatalog.LeafIndex own = leaf.hnswIndex(384).orElseThrow();
        PciCatalog.Anchor reference = catalog.read().anchor(M384, tenant, name("keep")).orElseThrow();
        assertThat(PciCatalog.ORIGIN_BUILT).isEqualTo(reference.origin());
        long bytesBefore = sizeOf(leaf.schema(), own.name());
        assertThat((double) bytesBefore / 250).as("precondition: the leaf index is far past 1.5x the reference")
            .isGreaterThan(1.5 * reference.bytes() / reference.rows());
        String oid = oidOf(leaf.schema(), own.name());
        String parent = parentIndexOf(leaf.schema(), own.name());
        String pciName = PciCatalog.indexName(M384, tenant, name("keep"));
        String pciOid = oidOf(leaf.schema(), pciName);

        r.reconcileOnce();

        assertThat(r.reindexes()).isEqualTo(1);
        assertThat(oidOf(leaf.schema(), own.name())).as("the leaf index was rebuilt").isNotEqualTo(oid);
        assertThat(parentIndexOf(leaf.schema(), own.name())).as("still attached").isEqualTo(parent);
        assertThat(oidOf(leaf.schema(), pciName)).as("the pci_ index is not a candidate").isEqualTo(pciOid);
        PciCatalog.Anchor anchor = PciCatalog.parseAnchor(commentOf(leaf.schema(), own.name())).orElseThrow();
        assertThat(anchor.origin()).isEqualTo(PciCatalog.ORIGIN_BUILT);
        assertThat(anchor.rows()).isEqualTo(250);
        assertThat(anchor.bytes()).isEqualTo(sizeOf(leaf.schema(), own.name())).isLessThan(bytesBefore);
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_index_reindexed") && l.contains("trigger=leaf_size")
            && l.contains("index=" + own.name()));
        assertThat(logs()).anyMatch(l -> l.contains("event=pci_anchor_written") && l.contains("index=" + own.name()));
        assertThat(leaf(tenant).leafLeftovers()).isEmpty();

        r.reconcileOnce();

        assertThat(r.reindexes()).as("a fresh graph is within the multiple").isEqualTo(1);
    }

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void withReindexOff_aBloatedLeafIndexIsLeftAlone_andNothingIsReportedUnanchored() throws Exception {
        String tenant = newTenant("bloatoff");
        PciReconciler r = bloatedLeaf(tenant, REINDEX_OFF, new ScriptedCanary());
        PciCatalog.Leaf leaf = leaf(tenant);
        String name = leaf.hnswIndex(384).orElseThrow().name();
        String oid = oidOf(leaf.schema(), name);

        r.reconcileOnce();
        r.reconcileOnce();

        assertThat(r.reindexes()).isZero();
        assertThat(oidOf(leaf.schema(), name)).isEqualTo(oid);
        assertThat(commentOf(leaf.schema(), name)).isNull();
        assertThat(logged("op=reindex")).isZero();
        assertThat(r.maintenance().unanchored()).as("the rule is off, not inactive").isZero();
    }

    private long logged(String needle) {
        return logs().stream().filter(l -> l.contains(needle)).count();
    }

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aDimensionWithNoReference_384_isReportedUnanchored_notReindexed_andTheCountFallsOnceAnAnchorExists()
            throws Exception {
        String tenant = newTenant("unanch");
        collection(tenant, name("small"), 20);
        PciCatalog.Leaf leaf = leaf(tenant);
        String name = leaf.hnswIndex(384).orElseThrow().name();
        String oid = oidOf(leaf.schema(), name);
        awaitLiveTuples(leaf, 20);
        PciReconciler r = reconciler(settings(), new ScriptedCanary());

        r.reconcileOnce();

        assertThat(r.maintenance().unanchored()).as("no built anchor at 384 on this install").isGreaterThanOrEqualTo(1);
        assertThat(r.reindexes()).isZero();
        assertThat(oidOf(leaf.schema(), name)).isEqualTo(oid);
        int before = r.maintenance().unanchored();

        // A collection that builds gives the install a built anchor at 384; the count is recomputed every pass.
        addRows(tenant, name("small"), 250);
        awaitLiveTuples(leaf(tenant), 270);
        r.reconcileOnce();   // builds and anchors
        r.reconcileOnce();   // its look now reads the anchor

        long leavesAt384 = catalog.read().leaves().stream().filter(l -> M384.equals(l.model())).count();
        assertThat(before - r.maintenance().unanchored()).as("every 384-dimension leaf's index left the count")
            .isEqualTo(leavesAt384);
        assertThat(before).isGreaterThanOrEqualTo(1);
        assertThat(r.reindexes()).as("a leaf index of a fresh graph is quiet").isZero();
        assertThat(oidOf(leaf.schema(), name)).isEqualTo(oid);
    }

    @Test
    @Timeout(value = 600, unit = TimeUnit.SECONDS)
    void aLeafCandidate_sharesThePerPassLimitWithThePciCandidates_largestFirst() throws Exception {
        // Three tenants, each with one live collection (a pci_ index). The canary reads red for all three, so each
        // pci_ index is a red-canary candidate on the second pass; tenant "lim" also has a bloated leaf index, the
        // largest index of the four. MAX_PER_PASS is 2: the leaf index and one pci_ index run, two pci_ indexes wait.
        String lim = newTenant("lim");
        String p1 = newTenant("lim1");
        String p2 = newTenant("lim2");
        collection(p1, name("one"), 0);
        addRows(p1, name("one"), 250);
        collection(p2, name("two"), 0);
        addRows(p2, name("two"), 250);
        ScriptedCanary canary = new ScriptedCanary();
        canary.state = State.RED;
        collection(lim, name("keep"), 0);
        addRows(lim, name("keep"), 250);
        dormantCollection(lim, name("bulk"), 2_000);
        var clock = new PciReindexIntegrationTest.MovingClock();   // the real clock, advanced by the test
        PciReconciler r = reconciler(settings(), canary, clock);
        assertThat(r.reconcileOnce().builds()).isEqualTo(3);
        clock.advance(Duration.ofHours(25));   // a red canary rebuilds an index a day after its anchor, not before
        deleteCollection(lim, name("bulk"));
        vacuumLeaf(leaf(lim));
        awaitLiveTuples(leaf(lim), 250);
        awaitLiveTuples(leaf(p1), 250);
        awaitLiveTuples(leaf(p2), 250);
        PciCatalog.Leaf limLeaf = leaf(lim);
        String leafIndex = limLeaf.hnswIndex(384).orElseThrow().name();
        String leafOid = oidOf(limLeaf.schema(), leafIndex);
        List<String> pci = List.of(
            PciCatalog.indexName(M384, lim, name("keep")), PciCatalog.indexName(M384, p1, name("one")),
            PciCatalog.indexName(M384, p2, name("two")));
        String schema = limLeaf.schema();
        List<String> pciOids = new ArrayList<>();
        for (String n : pci) {
            pciOids.add(oidOf(schema, n));
        }

        r.reconcileOnce();

        assertThat(r.reindexes()).as("the per-pass limit of two").isEqualTo(2);
        assertThat(oidOf(schema, leafIndex)).as("the largest index, the leaf's, ran").isNotEqualTo(leafOid);
        long pciReindexed = 0;
        for (int i = 0; i < pci.size(); i++) {
            if (!oidOf(schema, pci.get(i)).equals(pciOids.get(i))) {
                pciReindexed++;
            }
        }
        assertThat(pciReindexed).as("one pci_ index shared the pass with it").isEqualTo(1);
    }
}
