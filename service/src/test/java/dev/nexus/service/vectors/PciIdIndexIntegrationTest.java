// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.PgSession.PciSettings;
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
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): the reconciler builds each collection's HNSW index on {@code collection_id} beside the
 * name-predicate one, against a real PostgreSQL in production's shape (the harness of {@link PciReconcilerIntegrationTest}:
 * a dedicated container migrated by a non-superuser owner the reconciler connects as; B is 200; each test has tenants of
 * its own). The unstamped state (rows whose id is NULL) is built the way an upgraded install has it: the two NOT VALID
 * CHECKs of {@code nexus.chunks} are dropped and the id is set NULL.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciIdIndexIntegrationTest {

    static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;
    static final String M384 = "minilm-l6-v2-384";
    static final String NONCE = "c4d5e6f7";
    static final int B = 200;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource adminDs;
    PciCatalog catalog;
    final List<String> tenants = new ArrayList<>();
    final ExecutorService async = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "pciid-test-async");
        t.setDaemon(true);
        return t;
    });
    int generation;
    volatile boolean gate2;

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
        // The unstamped state is the only way to a NULL id beside a name; the CHECKs would refuse it.
        asSuperuser(su -> {
            su.execute("ALTER TABLE nexus.chunks DROP CONSTRAINT IF EXISTS chunks_collection_id_check");
            su.execute("ALTER TABLE nexus.chunks DROP CONSTRAINT IF EXISTS chunks_collection_id_not_null");
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        async.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    @AfterEach
    void removeTenants() throws Exception {
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            DSLContext ctx = DSL.using(owner, SQLDialect.POSTGRES);
            for (String tenant : tenants) {
                PciReconcilerIntegrationTest.dropLeaves(ctx, tenant);
            }
        }
        tenants.clear();
        PgSession.resetSearchExactMaxRowsForTests();
        gate2 = false;
    }

    // -- fixtures --------------------------------------------------------------------------------------------

    static PciSettings settings(int maxPerLeaf) {
        return new PciSettings(true, B, 600, maxPerLeaf, null, false);
    }

    PciReconciler reconciler(PciSettings s) {
        var builder = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s);
        var r = new PciReconciler(catalog, builder, PciIndexSweep.create(svcDs, s), s, Clock.systemUTC(),
            PciReconciler.SET_LOCAL_TENANT, PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(60));
        r.idBuildGate(() -> gate2);
        return r;
    }

    @SuppressWarnings("deprecation")
    String newTenant(String tag) throws Exception {
        String tenant = "pciid-" + tag;
        tenants.add(tenant);
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(
                DSL.using(owner, SQLDialect.POSTGRES).configuration(), "nexus.chunks", tenant, true, true);
        }
        return tenant;
    }

    static String name(String tag) {
        return "knowledge__pciid-" + tag + "__minilm-l6-v2-384__v1";
    }

    <T> T asSuperuser(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    /** Registers {@code collection}, gives it {@code rows} stamped rows, and returns its registry id. */
    long collection(String tenant, String collection, int rows) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, collection, M384);
            return null;
        });
        int gen = ++generation;
        asSuperuser(su -> {
            for (int from = 0; from < rows; from += 250) {
                var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                    CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384, CHUNKS.METADATA);
                for (int i = from; i < Math.min(rows, from + 250); i++) {
                    step = step.values(tenant, collection,
                        Chash.fromHex(Chash.ofText(tenant + "/" + collection + "/" + gen + "/" + i).toHex()).toBytes(),
                        M384, "row " + i, vector(i), JSONB.jsonb("{}"));
                }
                step.onConflictDoNothing().execute();
            }
            return null;
        });
        return stamp(tenant, collection);
    }

    /** Sets the id of every row of the collection from its registry row, as the P3.1 stamp does; returns the id. */
    long stamp(String tenant, String collection) throws Exception {
        long id = idOf(tenant, collection);
        asSuperuser(su -> su.update(CHUNKS).set(CHUNKS.COLLECTION_ID, id)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection)).execute());
        return id;
    }

    long idOf(String tenant, String collection) throws Exception {
        return asSuperuser(su -> su.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
            .fetchOne(0, Long.class));
    }

    /** One row of the collection back to the unstamped state. */
    void unstampOneRow(String tenant, String collection) throws Exception {
        asSuperuser(su -> su.execute("UPDATE nexus.chunks SET collection_id = NULL WHERE (tenant_id, collection, chash) IN "
            + "(SELECT tenant_id, collection, chash FROM nexus.chunks WHERE tenant_id = ? AND collection = ? LIMIT 1)",
            tenant, collection));
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

    Set<String> validNameIndexed(String tenant) {
        Set<String> out = new HashSet<>();
        for (PciCatalog.Index i : leaf(tenant).indexes()) {
            if (i.parsed() && i.valid()) out.add(i.collection());
        }
        return out;
    }

    Set<Long> validIdIndexed(String tenant) {
        Set<Long> out = new HashSet<>();
        for (PciCatalog.Index i : leaf(tenant).indexes()) {
            if (i.idParsed() && i.valid()) out.add(i.collectionId());
        }
        return out;
    }

    boolean hasInvalidId(String tenant) {
        return leaf(tenant).indexes().stream().anyMatch(i -> i.idParsed() && !i.valid());
    }

    /** The collections gate 3 reads for this tenant's leaf: planned, and still without a VALID id index. */
    Set<String> uncovered(PciReconciler r, String tenant) {
        var coverage = r.gate3Source().read(null);
        assertThat(coverage).as("the reconciler finished a pass, so it has a plan to report").isPresent();
        Set<String> mine = new HashSet<>();
        for (String key : coverage.get().uncovered()) {
            if (key.startsWith(M384 + "\0" + tenant + "\0")) mine.add(key);
        }
        return mine;
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

    // -- the deparsed predicate --------------------------------------------------------------------------

    @Test
    void theIdPredicateDeparsesAsThePatternExpects_andTheIndexIsReadBackByItsIdNotItsName() throws Exception {
        String tenant = newTenant("deparse");
        long id = collection(tenant, name("deparse"), 300);
        PciSettings s = settings(16);
        var builder = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s);
        PciCatalog.Leaf leaf = leaf(tenant);

        try (var pass = builder.open()) {
            assertThat(pass.buildById(leaf, 384, id)).isEqualTo(PciBuilderSession.DdlOutcome.DONE);
        }

        String predicate = asSuperuser(su -> su.select(DSL.field("pg_get_expr(i.indpred, i.indrelid)", String.class))
            .from(DSL.table("pg_index i JOIN pg_class c ON c.oid = i.indexrelid"))
            .where(DSL.field("c.relname", String.class).eq(PciCatalog.indexNameById(M384, tenant, id)))
            .fetchOne(0, String.class));
        assertThat(predicate).isEqualTo("(collection_id = '" + id + "'::bigint)");
        var read = leaf(tenant).indexes().stream().filter(PciCatalog.Index::idParsed).toList();
        assertThat(read).hasSize(1);
        assertThat(read.get(0).collectionId()).isEqualTo(id);
        assertThat(read.get(0).valid()).isTrue();
        assertThat(read.get(0).name()).isEqualTo(PciCatalog.indexNameById(M384, tenant, id));
        assertThat(PciBuilderSession.Pass.ownsForComment(leaf(tenant), read.get(0)))
            .as("the ownership check accepts the id-hash name").isTrue();
        assertThat(PciBuilderSession.Pass.ownsForComment(leaf(tenant),
            new PciCatalog.Index(read.get(0).name(), true, null, null, null, null, id + 1)))
            .as("and refuses an id whose hash is not the name").isFalse();
    }

    // -- before gate 2 ---------------------------------------------------------------------------------------

    @Test
    void beforeGate2_noIdBuildIsPlannedOrStarted_andTheNameIndexIsBuiltAsToday() throws Exception {
        String tenant = newTenant("pre");
        collection(tenant, name("pre"), 300);
        PciReconciler r = reconciler(settings(16));
        gate2 = false;

        PassReport pass = r.reconcileOnce();

        assertThat(pass.builds()).as("the name index only").isEqualTo(1);
        assertThat(validNameIndexed(tenant)).containsExactly(name("pre"));
        assertThat(validIdIndexed(tenant)).isEmpty();
        assertThat(hasInvalidId(tenant)).isFalse();
        assertThat(uncovered(r, tenant)).as("planned, and no VALID id index: gate 3 cannot hold").hasSize(1);
    }

    // -- the transition --------------------------------------------------------------------------------------

    @Test
    void afterGate2_theIdIndexIsBuiltBesideTheNameIndex_andGate3ReadsItAsCovered() throws Exception {
        String tenant = newTenant("go");
        long id = collection(tenant, name("go"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();                                   // gate 2 not held: the name index
        gate2 = true;

        PassReport pass = r.reconcileOnce();

        assertThat(pass.builds()).isEqualTo(1);
        assertThat(validNameIndexed(tenant)).as("the name index is not dropped").containsExactly(name("go"));
        assertThat(validIdIndexed(tenant)).containsExactly(id);
        assertThat(uncovered(r, tenant)).as("covered").isEmpty();
        var coverage = r.gate3Source().read(null).orElseThrow();
        assertThat(coverage.validIdIndexed()).contains(PciCatalog.idKey(M384, tenant, id));
        // A further pass changes nothing.
        PassReport again = r.reconcileOnce();
        assertThat(again.builds()).isZero();
        assertThat(again.drops()).isZero();
    }

    @Test
    void aCollectionWithANullIdRow_getsNoIdIndex_keepsItsNameIndex_andGate3DoesNotCountItComplete() throws Exception {
        String tenant = newTenant("null");
        collection(tenant, name("null"), 300);
        long okId = collection(tenant, name("ok"), 300);
        unstampOneRow(tenant, name("null"));
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;

        PassReport pass = r.reconcileOnce();

        assertThat(validNameIndexed(tenant)).containsExactlyInAnyOrder(name("null"), name("ok"));
        assertThat(validIdIndexed(tenant)).as("only the stamped collection").containsExactly(okId);
        assertThat(pass.builds()).isEqualTo(1);
        assertThat(uncovered(r, tenant)).as("the null-id collection is planned and uncovered").hasSize(1);

        // Once its last null row is stamped the next pass builds it and gate 3 reads complete.
        long nullId = stamp(tenant, name("null"));
        r.reconcileOnce();
        assertThat(validIdIndexed(tenant)).containsExactlyInAnyOrder(okId, nullId);
        assertThat(uncovered(r, tenant)).isEmpty();
    }

    @Test
    void aKilledIdBuild_isLeftInvalid_gate3DoesNotHold_theNameIndexStays_andTheNextPassDropsAndRebuildsIt() throws Exception {
        String tenant = newTenant("kill");
        long id = collection(tenant, name("kill"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;

        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
            await(() -> hasInvalidId(tenant), "the concurrent id build's invalid index");
            terminateBuilder();
            held.close();
            PassReport ended = pass.get(60, TimeUnit.SECONDS);
            assertThat(ended.ranToEnd()).as("the pass is over").isFalse();
        }

        assertThat(hasInvalidId(tenant)).as("the killed CIC left its invalid id index").isTrue();
        assertThat(validNameIndexed(tenant)).as("the name index is not dropped").containsExactly(name("kill"));
        assertThat(validIdIndexed(tenant)).isEmpty();

        PassReport next = r.reconcileOnce();
        assertThat(next.drops()).as("the invalid index is dropped").isEqualTo(1);
        assertThat(next.builds()).as("and built again from the start in the same pass").isEqualTo(1);
        assertThat(validIdIndexed(tenant)).containsExactly(id);
        assertThat(hasInvalidId(tenant)).isFalse();
        assertThat(uncovered(r, tenant)).isEmpty();
    }
}
