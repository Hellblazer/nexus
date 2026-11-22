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
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): the fixtures of the id-index integration tests ({@link PciIdIndexIntegrationTest} and
 * {@link PciIdGate3IntegrationTest}), against a real PostgreSQL in production's shape (the harness of
 * {@link PciReconcilerIntegrationTest}: a dedicated container migrated by a non-superuser owner the reconciler connects as;
 * B is 200; each test has tenants of its own). Each subclass boots its own container. The unstamped state (rows whose id is
 * NULL) is built the way an upgraded install has it: the two NOT VALID CHECKs of {@code nexus.chunks} are dropped and the id
 * is set NULL.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class PciIdIndexSupport {


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

    static PciSettings reindexSettings() {
        return new PciSettings(true, B, 600, 16);      // REINDEX on, no canary in this class
    }

    PciCatalog.Index idIndex(String tenant) {
        return leaf(tenant).indexes().stream().filter(PciCatalog.Index::idParsed).findFirst().orElseThrow();
    }
}
