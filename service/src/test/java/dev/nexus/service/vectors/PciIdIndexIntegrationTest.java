// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.datamove.CompletionBootWork;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.db.AdminConnection;
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
    /** Whether this engine's id readers are present (P3.5's IdReaders.PRESENT); the retire needs it as well as the latched gate 3. */
    volatile boolean readers = true;

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
        readers = true;
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
        r.idReadersPresent(() -> readers);
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

    // -- linda 2026-11-22: the retire's structural guard, and the gate 3 latch ---------------------------------

    @Test
    void aLatchedGate3WithTheReadersAbsent_dropsNoNameIndex_andWithThemPresentDropsThem() throws Exception {
        String tenant = newTenant("guard");
        long id = collection(tenant, name("guard"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        gate3 = true;                   // the latched gate 3
        readers = false;                // IdReaders.PRESENT is false or the class is absent

        PassReport without = r.reconcileOnce();

        assertThat(without.drops()).as("no name index is dropped without the readers").isZero();
        assertThat(without.builds()).as("and the name family is not retired, so nothing builds it differently").isZero();
        assertThat(validNameIndexed(tenant)).containsExactly(name("guard"));
        assertThat(validIdIndexed(tenant)).containsExactly(id);

        readers = true;
        PassReport with = r.reconcileOnce();

        assertThat(with.drops()).isEqualTo(1);
        assertThat(validNameIndexed(tenant)).isEmpty();
        assertThat(validIdIndexed(tenant)).containsExactly(id);
    }

    CompletionBootWork reporter(PciReconciler r) {
        return new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), new AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS),
            new CompletionCheck.ReadSet(List.of(), List.of()), r.gate3Source(), List.of(c -> true));
    }

    static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));

    boolean latchRow(String gate) throws Exception {
        return asSuperuser(su -> su.fetchExists(su.selectOne().from(LATCH).where(DSL.field(DSL.name("gate"), String.class).eq(gate))));
    }

    @Test
    void gate3Latches_andStaysLatchedWhileTheLiveGate3GoesFalseOnAnInvalidIdIndex_andANewReporterReadsItAgain() throws Exception {
        String tenant = newTenant("latch");
        collection(tenant, name("latchA"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        asSuperuser(su -> su.deleteFrom(LATCH).execute());                 // a clean slate: another test may have latched
        var reporter = reporter(r);
        try {
            var held = reporter.refresh();
            assertThat(held.gate3()).as("every id index valid, the extras hold: %s", held.gate3Why()).isTrue();
            assertThat(held.gate3Latched()).as("the check latched it").isTrue();
            assertThat(reporter.gate3Latched()).isTrue();
            assertThat(latchRow(CompletionCheck.GATE3)).as("the row is in the database").isTrue();

            // A second collection whose id build is killed: its id index is INVALID, so the live gate 3 goes false.
            gate2 = false;
            collection(tenant, name("latchB"), 300);
            r.reconcileOnce();                                            // B's name index
            gate2 = true;
            try (HeldWrite heldWrite = new HeldWrite(tenant)) {
                CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(r::reconcileOnce, async);
                await(() -> hasInvalidId(tenant), "B's concurrent id build's invalid index");
                terminateBuilder();
                heldWrite.close();
                pass.get(60, TimeUnit.SECONDS);
            }
            var after = reporter.refresh();
            assertThat(after.gate3()).as("the live gate 3 is false").isFalse();
            assertThat(after.idIndexInvalid()).as("and says why, on its own").isTrue();
            assertThat(after.gate3Latched()).as("the latched value stays true").isTrue();
            assertThat(reporter.gate3Latched()).isTrue();
            assertThat(reporter.statusJson()).contains("\"gate3_latched\":true").contains("\"id_index_invalid\":true").contains("\"gate3\":false");

            // A new engine over the same database reads the latch.
            var restarted = reporter(r);
            try {
                restarted.refresh();
                assertThat(restarted.gate3Latched()).as("a new reporter over the same database reads latched").isTrue();
            } finally {
                restarted.close();
            }

            // A latch row from before a restore: gone from the database, so a new reporter reads not latched. The old one is sticky.
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
            var restored = reporter(r);
            try {
                var read = restored.refresh();
                assertThat(read.gate3()).as("non-vacuity: gate 3 is still false, so nothing writes the row back").isFalse();
                assertThat(read.gate3Latched()).isFalse();
                assertThat(restored.gate3Latched()).as("a restore that lost the row reads not latched").isFalse();
                assertThat(reporter.gate3Latched()).as("an engine that already saw the row keeps it for its life").isTrue();
            } finally {
                restored.close();
            }
        } finally {
            reporter.close();
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    CompletionBootWork reporter(PciReconciler r, CompletionCheck.Gate3LatchWriter seam) {
        return new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), new AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS),
            new CompletionCheck.ReadSet(List.of(), List.of()), r.gate3Source(), List.of(c -> true), seam);
    }

    @Test
    void theLatchIsWrittenOnItsOwnSession_notTheReadOnlyOne_andThroughASeamThatCanFailOrWait() throws Exception {
        String tenant = newTenant("seam");
        collection(tenant, name("seam"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        asSuperuser(su -> su.deleteFrom(LATCH).execute());
        try {
            // The check's own session is read only at the server: a write on it is refused (25006), so the row is never written there.
            try (Connection admin = adminDs.getConnection()) {
                DSLContext readOnly = DSL.using(admin, SQLDialect.POSTGRES);
                // as the check's own session is opened (CompletionBootWork.DbReader): default_transaction_read_only = on. The connection is
                // pooled, so the setting is put back whatever happens.
                readOnly.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("default_transaction_read_only"), DSL.val("on"),
                    DSL.inline(false))).fetch();
                try {
                    assertThat(readOnly.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val("transaction_read_only")))
                        .fetchOne(0, String.class)).as("non-vacuity: this is a read-only session").isEqualTo("on");
                    org.assertj.core.api.Assertions.assertThatThrownBy(() -> CompletionCheck.insertLatch(readOnly, CompletionCheck.GATE3, "v"))
                        .as("the latch INSERT cannot run on the read-only session").hasMessageContaining("read-only");
                } finally {
                    readOnly.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("default_transaction_read_only"), DSL.val("off"),
                        DSL.inline(false))).fetch();
                }
            }
            assertThat(latchRow(CompletionCheck.GATE3)).isFalse();

            // A writer that throws: gate 3 holds, nothing is latched (not the row, not the flag), and the failure does not escape the check.
            var failing = reporter(r, writeRow -> { throw new IllegalStateException("the rename lock was refused"); });
            try {
                var read = failing.refresh();
                assertThat(read.gate3()).as("non-vacuity: gate 3 holds: %s", read.gate3Why()).isTrue();
                assertThat(read.gate3Latched()).isFalse();
                assertThat(failing.gate3Latched()).isFalse();
                assertThat(latchRow(CompletionCheck.GATE3)).as("the row is absent").isFalse();
            } finally {
                failing.close();
            }

            // A writer that never runs the row write latches nothing either.
            var skipping = reporter(r, writeRow -> { });
            try {
                assertThat(skipping.refresh().gate3Latched()).isFalse();
                assertThat(latchRow(CompletionCheck.GATE3)).isFalse();
            } finally {
                skipping.close();
            }

            // A writer that waits (P3.7: for in-flight renames, under its lock) still latches once it returns, after the row is written.
            java.util.concurrent.atomic.AtomicBoolean rowWritten = new java.util.concurrent.atomic.AtomicBoolean();
            java.util.concurrent.atomic.AtomicReference<Boolean> flagWhenRowWritten = new java.util.concurrent.atomic.AtomicReference<>();
            var holder = new CompletionBootWork[1];
            holder[0] = reporter(r, writeRow -> {
                try {
                    Thread.sleep(300);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                writeRow.run();
                rowWritten.set(true);
                flagWhenRowWritten.set(holder[0].gate3Latched());   // the in-process flag is not set before the writer returns
            });
            try {
                long start = System.nanoTime();
                var read = holder[0].refresh();
                assertThat(System.nanoTime() - start).as("the read waited for the writer").isGreaterThanOrEqualTo(TimeUnit.MILLISECONDS.toNanos(250));
                assertThat(rowWritten).isTrue();
                assertThat(flagWhenRowWritten.get()).as("the flag is set only after the writer returns").isFalse();
                assertThat(read.gate3Latched()).isTrue();
                assertThat(holder[0].gate3Latched()).isTrue();
                assertThat(latchRow(CompletionCheck.GATE3)).isTrue();
            } finally {
                holder[0].close();
            }
        } finally {
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    // -- N1: a name-indexed collection that counts zero still gets an id index, so gate 3 can hold -------------

    @Test
    void aLiveCollectionEmptiedOfItsRows_keepsItsNameIndex_getsAnIdIndexToo_andGate3Holds() throws Exception {
        String tenant = newTenant("empty-rows");
        long id = collection(tenant, name("empty-rows"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();                                    // the name index, at 300 rows
        assertThat(validNameIndexed(tenant)).containsExactly(name("empty-rows"));
        deleteRows(tenant, name("empty-rows"), 300);          // every row gone: the zero-count guard keeps the name index
        gate2 = true;

        r.reconcileOnce();

        assertThat(validNameIndexed(tenant)).as("kept: a live collection that counts zero is a counting failure, never a drop")
            .containsExactly(name("empty-rows"));
        assertThat(validIdIndexed(tenant)).as("and it gets its id index, an empty partial index").containsExactly(id);
        assertThat(uncovered(r, tenant)).isEmpty();
        asSuperuser(su -> su.deleteFrom(LATCH).execute());
        var reporter = reporter(r);
        try {
            var read = reporter.refresh();
            assertThat(read.gate3()).as("gate 3 reaches true: %s", read.gate3Why()).isTrue();
        } finally {
            reporter.close();
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    // -- N2: a killed REINDEX of an id index leaves an INVALID leftover that the next pass drops --------------------

    boolean hasIdLeftover(String tenant) {
        return leaf(tenant).indexes().stream().anyMatch(i -> i.leftover() && i.leftoverOfId() != null);
    }

    @Test
    void aKilledReindexOfAnIdIndex_leavesAnInvalidLeftover_gate3StaysLatched_andTheNextPassDropsIt() throws Exception {
        String tenant = newTenant("idreindex");
        long id = collection(tenant, name("idreindex"), 300);
        PciReconciler r = reconciler(reindexSettings());
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        r.reconcileOnce();
        asSuperuser(su -> su.deleteFrom(LATCH).execute());
        var reporter = reporter(r);
        try {
            assertThat(reporter.refresh().gate3Latched()).as("non-vacuity: gate 3 latched").isTrue();
            // Retire the name family (latched + readers present), so the REINDEX candidate is the id index.
            gate3 = true;
            r.reconcileOnce();
            assertThat(validNameIndexed(tenant)).isEmpty();
            assertThat(validIdIndexed(tenant)).containsExactly(id);
            deleteRows(tenant, name("idreindex"), 80);           // deletes since build: the id index is a REINDEX candidate
            PciReconciler restarted = reconciler(reindexSettings());

            try (HeldWrite held = new HeldWrite(tenant)) {
                CompletableFuture<PassReport> pass = CompletableFuture.supplyAsync(restarted::reconcileOnce, async);
                await(() -> hasIdLeftover(tenant), "the REINDEX's INVALID _ccnew copy");
                terminateBuilder();
                held.close();
                pass.get(60, TimeUnit.SECONDS);
            }

            assertThat(hasIdLeftover(tenant)).as("the killed REINDEX left an INVALID id-predicate leftover").isTrue();
            assertThat(validIdIndexed(tenant)).as("the original stays valid").containsExactly(id);
            var after = reporter.refresh();
            assertThat(after.idIndexInvalid()).as("id_index_invalid sees the leftover").isTrue();
            assertThat(after.gate3Latched()).as("the latch outlives it").isTrue();
            assertThat(reporter.gate3Latched()).isTrue();

            PassReport next = restarted.reconcileOnce();

            assertThat(hasIdLeftover(tenant)).as("the next pass dropped the leftover").isFalse();
            assertThat(next.drops()).isGreaterThanOrEqualTo(1);
            assertThat(validIdIndexed(tenant)).containsExactly(id);
            assertThat(reporter.refresh().idIndexInvalid()).isFalse();
        } finally {
            reporter.close();
            asSuperuser(su -> su.deleteFrom(LATCH).execute());
        }
    }

    // -- M1 and M2: the coverage read as a row-secured role, and on an install with no leaf ---------------------------

    @Test
    void theCoverageReadAsNexusSvc_needsItsTenantBind_toSeeTheRegistry() throws Exception {
        String tenant = newTenant("svcread");
        collection(tenant, name("svcread"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();

        try (Connection svc = svcDs.getConnection()) {
            // nexus_svc is subject to the registry's row-level security: without the coverage read's own nexus.tenant bind it sees no registry row,
            // a leaf with a VALID name index and no visible row is unknown, and the read answers nothing.
            var coverage = r.gate3Source().read(DSL.using(svc, SQLDialect.POSTGRES));
            assertThat(coverage).as("non-vacuity: the read answers as the row-secured role").isPresent();
            assertThat(coverage.get().planned()).as("it saw the registry row").hasSize(1);
            assertThat(coverage.get().uncovered()).isEmpty();
            assertThat(DSL.using(svc, SQLDialect.POSTGRES).select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val("nexus.tenant"),
                DSL.inline(true))).fetchOne(0, String.class)).as("the bind is cleared after the read").isIn(null, "");
        }
    }

    @Test
    void aPciOnInstallWithNoLeafAtAll_hasNothingRequired_andNothingToIndex() throws Exception {
        var empty = new PciGate3Coverage(() -> new PciCatalog.Snapshot(List.of()), settings(16), Clock.systemUTC());
        try (Connection admin = adminDs.getConnection()) {
            var coverage = empty.read(DSL.using(admin, SQLDialect.POSTGRES));
            assertThat(coverage).as("not unknown: a fresh install with no tenant has nothing to index").isPresent();
            assertThat(coverage.get().nothingToIndex()).isTrue();
            assertThat(coverage.get().uncovered()).isEmpty();
        }
    }

    // -- elsinore's _by_id search functions: which query shapes use the id-predicate partial index ----------------

    /** The ANN query vector as pgvector text, 384 wide. */
    static String vectorText() {
        StringBuilder b = new StringBuilder("[");
        for (int i = 0; i < 384; i++) {
            b.append(i == 0 ? "" : ",").append(i == 5 ? "1" : "0");
        }
        return b.append(']').toString();
    }

    long idxScan(String index) throws Exception {
        return asSuperuser(su -> su.select(DSL.field(DSL.name("idx_scan"), Long.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_user_indexes")))
            .where(DSL.field(DSL.name("indexrelname"), String.class).eq(index)).fetchOne(0, Long.class));
    }

    @Test
    void whichByIdQueryShapesUseTheIdPredicatePartialIndex() throws Exception {
        String tenant = newTenant("shapes");
        long id = collection(tenant, name("shapes"), 400);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        String index = PciCatalog.indexNameById(M384, tenant, id);
        assertThat(validIdIndexed(tenant)).as("non-vacuity: the id index exists and is valid").containsExactly(id);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-id-index-plan-shapes.xml",
                "databasechangelog_test_id_shapes", java.util.Map.of("svcRole", PgContainerHelper.SVC_USERNAME));
        }
        java.util.Map<String, String> plans = new java.util.LinkedHashMap<>();
        try (Connection svc = svcDs.getConnection()) {
            DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
            for (String shape : List.of("a", "b", "c", "d")) {
                plans.put(shape, ctx.select(DSL.function(DSL.name("nexus", "test_id_shape_plan"), SQLDataType.CLOB, DSL.val(shape),
                    DSL.val(tenant), DSL.val(M384), DSL.val(id), DSL.val(vectorText()))).fetchOne(0, String.class));
            }
        }
        // plpgsql: eight calls on one session, the index's idx_scan read after each. The plan cache keeps a custom plan for the first five calls and
        // may then adopt a generic one, which cannot prove the statement implies the partial index's predicate.
        java.util.Map<String, String> perCall = new java.util.LinkedHashMap<>();
        for (String mode : List.of("direct", "execute")) {
            try (Connection svc = svcDs.getConnection()) {
                svc.setAutoCommit(true);
                DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
                StringBuilder used = new StringBuilder();
                for (int call = 1; call <= 8; call++) {
                    long before = idxScan(index);
                    ctx.select(DSL.function(DSL.name("nexus", "test_id_shape_call"), SQLDataType.BIGINT, DSL.val(mode), DSL.val(tenant),
                        DSL.val(M384), DSL.val(id), DSL.val(vectorText()))).fetchOne(0, Long.class);
                    used.append(idxScan(index) > before ? 'I' : '-');
                }
                perCall.put(mode, used.toString());
            }
        }
        String own = "Index Scan using " + leaf(tenant).name() + "_embedding_384_idx";
        // Measured on PG 17 with enable_seqscan, enable_bitmapscan and enable_sort off (see the changelog), as nexus_svc with the tenant qual, 400 rows.
        assertThat(plans.get("a")).as("(a) collection_id = 42, an int4 literal: the partial index serves it").contains("Index Scan using " + index);
        assertThat(plans.get("b")).as("(b) collection_id = 42::bigint").contains("Index Scan using " + index);
        assertThat(plans.get("c")).as("(c) PREPAREd $1 bigint, force_generic_plan: the planner cannot prove the predicate, so the leaf's own HNSW index serves it")
            .doesNotContain(index).contains(own);
        assertThat(plans.get("d")).as("(d) the same, force_custom_plan: the partial index again").contains("Index Scan using " + index);
        assertThat(perCall.get("direct")).as("(e) a plpgsql variable used directly: every call, the sixth to the eighth included (I = the partial index served the call)")
            .isEqualTo("IIIIIIII");
        assertThat(perCall.get("execute")).as("(f) EXECUTE format('... = %L', v): every call").isEqualTo("IIIIIIII");
    }
}
