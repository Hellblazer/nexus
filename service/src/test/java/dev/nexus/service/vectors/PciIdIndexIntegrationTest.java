// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.P31CheckReplay;
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
import java.util.Optional;
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
    /** vectors-068's changeset that adds the two NOT VALID CHECKs of nexus.chunks. */
    static final String CHUNKS_CHECKS = "vectors-068-3";

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
    volatile boolean gate3;

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
            P31CheckReplay.lift(su, CHUNKS_CHECKS);   // the changeset's own rollback: the unstamped state an upgraded install has
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
        gate3 = false;
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
        r.retireNameIndexesWhen(() -> gate3);
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
        asSuperuser(su -> su.update(CHUNKS).set(CHUNKS.COLLECTION_ID, (Long) null)
            .where(DSL.row(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH).in(firstRows(su, tenant, collection, 1))).execute());
    }

    /** Deletes {@code n} rows of the collection. */
    void deleteRows(String tenant, String collection, int n) throws Exception {
        asSuperuser(su -> su.deleteFrom(CHUNKS)
            .where(DSL.row(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH).in(firstRows(su, tenant, collection, n))).execute());
    }

    private static org.jooq.Select<? extends org.jooq.Record3<String, String, byte[]>> firstRows(DSLContext su, String tenant,
                                                                                                String collection, int n) {
        return su.select(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection)).limit(n);
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

    /** The collections gate 3 reads for this tenant's leaf: required, and still without a VALID id index (read as the check reads, on the admin role). */
    Set<String> uncovered(PciReconciler r, String tenant) {
        var coverage = coverage(r);
        assertThat(coverage).as("the database read answers").isPresent();
        Set<String> mine = new HashSet<>();
        for (String key : coverage.get().uncovered()) {
            if (key.startsWith(M384 + "\0" + tenant + "\0")) mine.add(key);
        }
        return mine;
    }

    Optional<CompletionCheck.IdIndexCoverage> coverage(PciReconciler r) {
        try (Connection admin = adminDs.getConnection()) {
            admin.setReadOnly(true);   // the check's session is read-only
            return r.gate3Source().read(DSL.using(admin, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
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

        String predicate = asSuperuser(su -> su.select(DSL.function(DSL.name("pg_catalog", "pg_get_expr"), SQLDataType.CLOB,
                    DSL.field(DSL.name("i", "indpred")), DSL.field(DSL.name("i", "indrelid"))))
            .from(DSL.table(DSL.name("pg_catalog", "pg_index")).as("i"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("i", "indexrelid"))))
            .where(DSL.field(DSL.name("c", "relname"), String.class).eq(PciCatalog.indexNameById(M384, tenant, id)))
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
        var coverage = coverage(r).orElseThrow();
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
        assertThat(uncovered(r, tenant)).as("gate 3 is false between the kill and the next pass").hasSize(1);
        assertThat(coverage(r).orElseThrow().invalidIdIndexed()).as("the INVALID id index is reported").hasSize(1);

        PassReport next = r.reconcileOnce();
        assertThat(next.drops()).as("the invalid index is dropped").isEqualTo(1);
        assertThat(next.builds()).as("and built again from the start in the same pass").isEqualTo(1);
        assertThat(validIdIndexed(tenant)).containsExactly(id);
        assertThat(hasInvalidId(tenant)).isFalse();
        assertThat(uncovered(r, tenant)).isEmpty();
    }

    // -- linda 2026-11-22: the name family retires at gate 3; gate 3's PCI clause reads the database --------------

    @Test
    void atGate3_theNameIndexesGoWhereTheIdIndexIsValid_andGate3StillReadsCovered() throws Exception {
        String tenant = newTenant("retire");
        long a = collection(tenant, name("ra"), 300);
        long b = collection(tenant, name("rb"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        assertThat(validNameIndexed(tenant)).as("before gate 3 the name indexes stay").containsExactlyInAnyOrder(name("ra"), name("rb"));
        assertThat(validIdIndexed(tenant)).containsExactlyInAnyOrder(a, b);
        assertThat(uncovered(r, tenant)).isEmpty();

        gate3 = true;
        PassReport pass = r.reconcileOnce();

        assertThat(pass.drops()).as("each name index, one DROP INDEX CONCURRENTLY after another").isEqualTo(2);
        assertThat(pass.builds()).as("and no name index is built back").isZero();
        assertThat(validNameIndexed(tenant)).isEmpty();
        assertThat(validIdIndexed(tenant)).containsExactlyInAnyOrder(a, b);
        assertThat(uncovered(r, tenant)).as("dropping the name family does not flip gate 3").isEmpty();
        assertThat(coverage(r).orElseThrow().planned()).as("both collections are still required").hasSize(2);
        PassReport again = r.reconcileOnce();
        assertThat(again.drops()).isZero();
        assertThat(again.builds()).as("a further pass builds no name index").isZero();
    }

    @Test
    void aCollectionWhoseIdBuildWasKilled_keepsItsNameIndexAfterGate3_untilItsIdIndexIsValid() throws Exception {
        String tenant = newTenant("keep");
        long id = collection(tenant, name("keep"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        gate3 = true;
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
            await(() -> hasInvalidId(tenant), "the concurrent id build's invalid index");
            terminateBuilder();
            held.close();
            pass.get(60, TimeUnit.SECONDS);
        }
        assertThat(hasInvalidId(tenant)).isTrue();

        PassReport next = r.reconcileOnce();                // drops the invalid index and builds it again

        assertThat(validIdIndexed(tenant)).containsExactly(id);
        assertThat(validNameIndexed(tenant)).as("the id index was not valid when this pass planned").containsExactly(name("keep"));
        assertThat(next.drops()).isEqualTo(1);

        PassReport last = r.reconcileOnce();                // now the id index is valid: the name index is replaced
        assertThat(last.drops()).isEqualTo(1);
        assertThat(validNameIndexed(tenant)).isEmpty();
        assertThat(validIdIndexed(tenant)).containsExactly(id);
    }

    @Test
    void aStandbyEnginesGate3Clause_agreesWithTheLockHolders() throws Exception {
        String tenant = newTenant("standby");
        collection(tenant, name("sb"), 300);
        PciSettings s = settings(16);
        PciReconciler holder = reconciler(s);
        holder.reconcileOnce();
        gate2 = true;
        holder.reconcileOnce();
        PciReconciler standby = reconciler(s);              // never ran a pass: it has no plan of its own

        assertThat(coverage(standby)).as("same database, same answer").isEqualTo(coverage(holder));
        assertThat(uncovered(standby, tenant)).isEmpty();
        // and where the holder's builds are incomplete, so are the standby's
        long other = collection(tenant, name("sb2"), 300);
        holder.reconcileOnce();                             // builds sb2's name index and its id index (gate 3 is off: no retirement)
        PciCatalog.Leaf current = leaf(tenant);
        PciCatalog.Index victim = current.idIndexes().stream().filter(i -> i.collectionId() == other).findFirst().orElseThrow();
        try (var pass = new PciBuilderSession(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS, NONCE, s).open()) {
            assertThat(pass.drop(current, victim)).isEqualTo(PciBuilderSession.DdlOutcome.DONE);   // the production drop path
        }
        assertThat(coverage(standby)).isEqualTo(coverage(holder));
        assertThat(uncovered(standby, tenant)).hasSize(1);
    }

    @Test
    void withPciOff_theClauseIsNotApplicable() throws Exception {
        String tenant = newTenant("off");
        collection(tenant, name("off"), 300);
        var off = new PciSettings(false, B, 600, 16, null, false);

        var coverage = coverage(reconciler(off));

        assertThat(coverage).isPresent();
        assertThat(coverage.get().notApplicable()).isTrue();
        assertThat(coverage.get().uncovered()).isEmpty();
    }

    @Test
    void withPciOnAndNoIndexAtAll_aQualifyingCollectionFailsTheClause_andAStoreOfSmallOnesPasses() throws Exception {
        String tenant = newTenant("empty");
        collection(tenant, name("small"), 50);
        PciReconciler r = reconciler(settings(16));

        var nothing = coverage(r).orElseThrow();
        assertThat(nothing.nothingToIndex()).as("no collection reaches B: nothing to build").isTrue();
        assertThat(nothing.uncovered()).isEmpty();

        collection(tenant, name("big"), 300);
        var qualifying = coverage(reconciler(settings(16))).orElseThrow();     // a fresh instance: no cached answer
        assertThat(qualifying.nothingToIndex()).isFalse();
        assertThat(qualifying.planned()).as("a store that should have an index and has none").isNotEmpty();
        assertThat(qualifying.uncovered()).isNotEmpty();
    }

    // -- S2: backoff, the hysteresis band and an INVALID index keep gate 3 shut ---------------------------------

    void cancelBuilder() throws Exception {
        asSuperuser(su -> su.select(DSL.function("pg_cancel_backend", SQLDataType.BOOLEAN,
                DSL.field(DSL.name("pid"), Integer.class)))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class)
                .eq(PciBuilderSession.builderApplicationName(NONCE)))
            .fetch());
    }

    boolean builderRunningCreateIndex() {
        try {
            return asSuperuser(su -> su.fetchExists(DSL.selectOne()
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class)
                    .eq(PciBuilderSession.builderApplicationName(NONCE)))
                .and(DSL.field(DSL.name("state"), String.class).eq("active"))
                .and(DSL.field(DSL.name("query"), String.class).likeIgnoreCase("CREATE INDEX%"))));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void aFailedIdBuildThatIsBackedOff_keepsGate3Shut_andIsNotBuiltAgainWhileBackedOff() throws Exception {
        String tenant = newTenant("backoff");
        long id = collection(tenant, name("backoff"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        PassReport failed;
        try (HeldWrite held = new HeldWrite(tenant)) {
            CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
            await(this::builderRunningCreateIndex, "the builder's CREATE INDEX CONCURRENTLY");
            cancelBuilder();                     // the statement fails (57014), the session carries on: charged to backoff
            held.close();
            failed = pass.get(60, TimeUnit.SECONDS);
        }
        assertThat(failed.failedBuilds()).isEqualTo(1);
        assertThat(r.trackedFailures()).as("backed off").isEqualTo(1);
        assertThat(uncovered(r, tenant)).as("gate 3 stays false").hasSize(1);

        PassReport next = r.reconcileOnce();     // the backoff has not lapsed: the invalid index is dropped, nothing is built

        assertThat(next.builds()).as("backed off: not built again").isZero();
        assertThat(validIdIndexed(tenant)).isEmpty();
        assertThat(validNameIndexed(tenant)).containsExactly(name("backoff"));
        assertThat(uncovered(r, tenant)).as("a backed-off collection with a name index still holds gate 3 shut").hasSize(1);
        assertThat(id).isPositive();
    }

    @Test
    void aCollectionInTheHysteresisBand_keepsGate3ShutUntilItsIdIndexIsBuilt_andThenGetsOne() throws Exception {
        String tenant = newTenant("band");
        long id = collection(tenant, name("band"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();                                       // the name index, at 300 rows
        assertThat(validNameIndexed(tenant)).containsExactly(name("band"));
        // Rows fall into B/2 .. B-1 (B is 200): the name index is kept.
        deleteRows(tenant, name("band"), 150);
        gate2 = true;

        assertThat(uncovered(r, tenant)).as("a name index with no id index").hasSize(1);
        r.reconcileOnce();

        assertThat(validNameIndexed(tenant)).as("kept in the band").containsExactly(name("band"));
        assertThat(validIdIndexed(tenant)).as("and the id index is built so gate 3 is reachable").containsExactly(id);
        assertThat(uncovered(r, tenant)).isEmpty();
    }

    // -- S6: the id index has the name index's lifecycle: anchor, REINDEX ----------------------------------------

    static PciSettings reindexSettings() {
        return new PciSettings(true, B, 600, 16);      // REINDEX on, no canary in this class
    }

    PciCatalog.Index idIndex(String tenant) {
        return leaf(tenant).indexes().stream().filter(PciCatalog.Index::idParsed).findFirst().orElseThrow();
    }

    @Test
    void anIdBuildGetsItsAnchorComment() throws Exception {
        String tenant = newTenant("anchor");
        long id = collection(tenant, name("anchor"), 300);
        PciReconciler r = reconciler(reindexSettings());
        r.reconcileOnce();
        gate2 = true;

        r.reconcileOnce();

        PciCatalog.Index index = idIndex(tenant);
        assertThat(index.collectionId()).isEqualTo(id);
        assertThat(index.anchor()).as("the id index is anchored like a name index").isPresent();
        var anchor = index.anchor().orElseThrow();
        assertThat(anchor.origin()).isEqualTo(PciCatalog.ORIGIN_BUILT);
        assertThat(anchor.rows()).as("the rows its predicate selects").isEqualTo(300);
        assertThat(anchor.bytes()).isPositive();
        assertThat(anchor.relfilenode()).as("stamped with the storage it was written against").isNotNull();
    }

    @Test
    void aReindexOfAnIdIndex_runs_keepsItValid_andWritesAFreshAnchor() throws Exception {
        String tenant = newTenant("reindex");
        long id = collection(tenant, name("reindex"), 300);
        PciReconciler r = reconciler(reindexSettings());
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        r.reconcileOnce();
        var before = idIndex(tenant).anchor().orElseThrow();
        assertThat(before.rows()).isEqualTo(300);
        // Over a quarter of the rows go (80 of 300): deletes since build. A restarted engine sees it through the anchor.
        deleteRows(tenant, name("reindex"), 80);
        PciReconciler restarted = reconciler(reindexSettings());

        for (int pass = 0; pass < 4 && idIndex(tenant).anchor().orElseThrow().rows() != 220; pass++) {
            restarted.reconcileOnce();        // at most one REINDEX a pass: the name index and the id index take turns
        }

        PciCatalog.Index after = idIndex(tenant);
        assertThat(after.valid()).as("the REINDEX leaves it valid").isTrue();
        assertThat(after.collectionId()).isEqualTo(id);
        assertThat(after.anchor().orElseThrow().rows()).as("a fresh anchor counts the rows now").isEqualTo(220);
        assertThat(after.anchor().orElseThrow().builtAt()).isAfter(before.builtAt());
        assertThat(after.anchor().orElseThrow().relfilenode()).as("new storage").isNotEqualTo(before.relfilenode());
    }

    // -- S5: end to end through CompletionCheck.evaluate with the reconciler's real source ------------------------

    @Test
    void completionCheckSeesGate3True_withTheReconcilersRealSourceAndTheExtras_whenEverythingHolds() throws Exception {
        String tenant = newTenant("e2e");
        collection(tenant, name("e2e"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        CompletionCheck.ReadSet gates = new CompletionCheck.ReadSet(List.of(), List.of());

        try (Connection admin = adminDs.getConnection()) {
            admin.setReadOnly(true);
            DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
            var holds = CompletionCheck.evaluate(ctx, gates, r.gate3Source(), List.of(c -> true));
            assertThat(holds.gate2()).as("non-vacuity: gate 2 holds").isTrue();
            assertThat(holds.gate3()).as("every id index valid, the extras hold: %s", holds.gate3Why()).isTrue();
            assertThat(holds.gate3PciNotApplicable()).isFalse();

            var extraFalse = CompletionCheck.evaluate(ctx, gates, r.gate3Source(), List.of(c -> false));
            assertThat(extraFalse.gate3()).as("the id unique indexes are still required").isFalse();

            // The name family retires: gate 3 stays true after.
            gate3 = true;
            r.reconcileOnce();
            assertThat(validNameIndexed(tenant)).isEmpty();
            assertThat(CompletionCheck.evaluate(ctx, gates, r.gate3Source(), List.of(c -> true)).gate3())
                .as("dropping the name index does not flip it").isTrue();
        }
    }

    // -- S4: the null-id scan -------------------------------------------------------------------------------------

    @Test
    void theNullIdScanIsSkippedWhileTheChecksAreValidated_andRunsWhenOneIsMissingOrNotValid() throws Exception {
        String tenant = newTenant("checks");
        collection(tenant, name("checks"), 300);
        var candidate = List.of(new PciReconcilePlanner.RegistryRow(name("checks"), "live", "", idOf(tenant, name("checks"))));
        unstampOneRow(tenant, name("checks"));
        try (Connection admin = adminDs.getConnection()) {
            DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
            assertThat(PciIdBuilds.checksValidated(ctx)).as("both CHECKs are absent in this class").isFalse();
            assertThat(PciIdBuilds.nullIdCollections(ctx, leaf(tenant), candidate)).as("scanned, and found").containsExactly(name("checks"));

            stamp(tenant, name("checks"));
            asSuperuser(su -> { P31CheckReplay.restore(su, CHUNKS_CHECKS); return null; });   // the changeset's own text: both NOT VALID
            try {
                assertThat(PciIdBuilds.checksValidated(ctx)).as("present but NOT VALID").isFalse();
                asSuperuser(su -> {
                    P31CheckReplay.lift(su, CHUNKS_CHECKS);
                    // added validated (every row is stamped), as the stamp move's on_complete leaves them
                    su.alterTable(CHUNKS).add(DSL.constraint(DSL.name("chunks_collection_id_check"))
                        .check(DSL.field(DSL.nullif(CHUNKS.COLLECTION, "").isNull()).eq(DSL.field(CHUNKS.COLLECTION_ID.isNull())))).execute();
                    su.alterTable(CHUNKS).add(DSL.constraint(DSL.name("chunks_collection_id_not_null"))
                        .check(CHUNKS.COLLECTION_ID.isNotNull())).execute();
                    return null;
                });
                assertThat(PciIdBuilds.checksValidated(ctx)).as("both validated: no row can have a null id").isTrue();
                assertThat(PciIdBuilds.nullIdCollections(ctx, leaf(tenant), candidate)).as("no scan").isEmpty();
            } finally {
                asSuperuser(su -> { P31CheckReplay.lift(su, CHUNKS_CHECKS); return null; });
            }
        }
    }

    @Test
    void aNullIdProbeThatFails_holdsBackOnlyTheIdBuild_andTheCountingTransactionSurvives() throws Exception {
        String tenant = newTenant("probe");
        long id = collection(tenant, name("probe"), 300);
        PciCatalog.Leaf leaf = leaf(tenant);
        var candidate = List.of(new PciReconcilePlanner.RegistryRow(name("probe"), "live", "", id));
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            // An uncommitted rename of the leaf holds ACCESS EXCLUSIVE on it until the rollback below.
            DSL.using(holder, SQLDialect.POSTGRES).alterTable(DSL.table(DSL.name(leaf.schema(), leaf.name())))
                .renameTo(DSL.name(leaf.name() + "_held")).execute();
            try (Connection c = adminDs.getConnection()) {
                c.setAutoCommit(false);
                DSLContext tx = DSL.using(c, SQLDialect.POSTGRES);
                tx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("lock_timeout"), DSL.val("200ms"), DSL.inline(true)))
                    .fetch();

                var held = PciIdBuilds.nullIdCollections(tx, leaf, candidate);

                assertThat(held).as("a probe that fails answers: held back (fail closed)").containsExactly(name("probe"));
                assertThat(tx.selectOne().fetchOne(0, Integer.class)).as("the transaction was not aborted").isEqualTo(1);
                c.rollback();
            }
            holder.rollback();
        }
    }
}
