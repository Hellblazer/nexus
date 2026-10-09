// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PgSession;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import javax.sql.DataSource;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.time.Duration;
import java.util.HexFormat;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-227 Step 2 (nexus-43ulx.11): {@link PciCatalog}'s catalog read against the real partitioned layout.
 *
 * <p>Every tenant has a leaf under every model partition of {@code nexus.chunks} (the
 * {@code service_tokens} trigger makes them). The reader runs as {@code nexus_svc} on a pooled connection;
 * the indexes are made by the superuser, who owns the leaves, with the statements jOOQ has no form for
 * (an HNSW operator class, {@code CREATE INDEX CONCURRENTLY}), so those few go through plain JDBC.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciCatalogIntegrationTest {

    static final String SVC_ROLE = "svc_pcicat";
    static final String SVC_PASS = "svc_pcicat_pass";
    static final String T1 = "pcicat-tenant-1";
    static final String T2 = "pcicat-tenant-2";
    static final String M384 = "minilm-l6-v2-384";
    static final String M768 = "bge-base-en-v15-768";
    static final String M1024 = "voyage-code-3";
    static final String SHARED = "code__pcicat-shared__voyage-code-3__v1";

    static final Duration BOUND = Duration.ofSeconds(60);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PciCatalog catalog;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(ctx, "tok-pcicat-1-0123456789abcdef000000", T1, "pcicat1");
            PgContainerHelper.seedServiceToken(ctx, "tok-pcicat-2-0123456789abcdef000000", T2, "pcicat2");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        catalog = new PciCatalog(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // -- helpers -----------------------------------------------------------------------------------------

    /** {@code nexus.partition_name('chunks', model, tenant)}. */
    static String leafName(String model, String tenant) {
        return "chunks_m" + sha256Hex(model).substring(0, 8) + "_t_" + sha256Hex(tenant).substring(0, 16);
    }

    static String sha256Hex(String s) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(s.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    static String lit(String s) {
        return "'" + s.replace("'", "''") + "'";
    }

    static String column(String model) {
        return switch (model) {
            case M384 -> "embedding_384";
            case M768 -> "embedding_768";
            default -> "embedding_1024";
        };
    }

    /** The DDL for a per-collection index the way the builder will write it. */
    static String createIndexDdl(String concurrently, String index, String model, String tenant, String predicate) {
        return "CREATE INDEX " + concurrently + index + " ON nexus." + leafName(model, tenant)
            + " USING hnsw (" + column(model) + " nexus.vector_cosine_ops) WHERE " + predicate;
    }

    /** Run one DDL statement as the superuser who owns the leaves. */
    void ddl(String statement) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, statement);
        }
    }

    void buildIndex(String model, String tenant, String collection) throws Exception {
        ddl(createIndexDdl("", PciCatalog.indexName(model, tenant, collection), model, tenant,
            "collection = " + lit(collection)));
    }

    PciCatalog.Leaf leaf(PciCatalog.Snapshot snap, String model, String tenant) {
        List<PciCatalog.Leaf> hits = snap.leaves().stream()
            .filter(l -> model.equals(l.model()) && tenant.equals(l.tenant())).toList();
        assertThat(hits).as("exactly one leaf for (%s, %s) in %s", model, tenant, snap.leaves()).hasSize(1);
        return hits.get(0);
    }

    Optional<PciCatalog.Index> index(PciCatalog.Leaf leaf, String name) {
        return leaf.indexes().stream().filter(i -> name.equals(i.name())).findFirst();
    }

    static void await(String what, BooleanSupplier condition) throws InterruptedException {
        long deadline = System.nanoTime() + BOUND.toNanos();
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("timed out after " + BOUND + " waiting for " + what);
            }
            Thread.sleep(25);
        }
    }

    // -- tests -------------------------------------------------------------------------------------------

    @Test
    void everyLeafUnderEveryModelIsListed_withItsModelAndTenantFromTheBounds() {
        PciCatalog.Snapshot snap = catalog.read();

        // Non-vacuity: the fixture's two tenants have a leaf under each of the four models.
        for (String tenant : List.of(T1, T2)) {
            for (String model : List.of(M384, M768, M1024, "voyage-context-3")) {
                PciCatalog.Leaf leaf = leaf(snap, model, tenant);
                assertThat(leaf.name()).isEqualTo(leafName(model, tenant));
                assertThat(leaf.schema()).isEqualTo("nexus");
            }
        }
        assertThat(snap.leaves().stream().map(PciCatalog.Leaf::name).distinct().count())
            .as("no leaf listed twice").isEqualTo(snap.leaves().size());
    }

    @Test
    void twoTenantsWithTheSameCollectionName_eachIndexMapsToItsOwnLeafAndTenant() throws Exception {
        buildIndex(M1024, T1, SHARED);
        buildIndex(M1024, T2, SHARED);
        buildIndex(M768, T1, SHARED + "-other-model");

        PciCatalog.Snapshot snap = catalog.read();

        String n1 = PciCatalog.indexName(M1024, T1, SHARED);
        String n2 = PciCatalog.indexName(M1024, T2, SHARED);
        assertThat(n1).isNotEqualTo(n2);
        assertThat(leaf(snap, M1024, T1).indexes()).extracting(PciCatalog.Index::name).contains(n1).doesNotContain(n2);
        assertThat(leaf(snap, M1024, T2).indexes()).extracting(PciCatalog.Index::name).contains(n2).doesNotContain(n1);
        assertThat(index(leaf(snap, M1024, T1), n1)).hasValueSatisfying(i -> {
            assertThat(i.valid()).isTrue();
            assertThat(i.collection()).isEqualTo(SHARED);
        });
        assertThat(index(leaf(snap, M1024, T2), n2)).hasValueSatisfying(i -> {
            assertThat(i.valid()).isTrue();
            assertThat(i.collection()).isEqualTo(SHARED);
        });
        // The router's question, asked per (model, tenant, collection).
        assertThat(snap.hasValidIndex(M1024, T1, SHARED)).isTrue();
        assertThat(snap.hasValidIndex(M1024, T2, SHARED)).isTrue();
        assertThat(snap.hasValidIndex(M1024, "pcicat-tenant-3", SHARED)).isFalse();
        assertThat(snap.hasValidIndex(M768, T1, SHARED)).as("same tenant and name, other model").isFalse();
        assertThat(snap.hasValidIndex(M768, T1, SHARED + "-other-model")).as("the model comes from the model level").isTrue();
        assertThat(index(leaf(snap, M768, T1), PciCatalog.indexName(M768, T1, SHARED + "-other-model")))
            .isPresent();
    }

    @Test
    void aCollectionNameWithAQuote_roundTripsThroughTheNameAndTheCatalog() throws Exception {
        String quoted = "knowledge__it's-a-name__minilm-l6-v2-384__v1";
        buildIndex(M384, T1, quoted);

        PciCatalog.Snapshot snap = catalog.read();

        assertThat(PciCatalog.parseCollection("(collection = " + lit(quoted) + "::text)")).contains(quoted);
        assertThat(index(leaf(snap, M384, T1), PciCatalog.indexName(M384, T1, quoted)))
            .hasValueSatisfying(i -> assertThat(i.collection()).isEqualTo(quoted));
        assertThat(snap.hasValidIndex(M384, T1, quoted)).isTrue();
    }

    @Test
    void anUnparsablePciIndex_isCountedUnparsed_neverRouted_andLeftInTheListing() throws Exception {
        PciCatalog.Snapshot before = catalog.read();
        String compound = "pci_" + "0".repeat(23) + "1";
        String noPredicate = "pci_" + "0".repeat(23) + "2";
        ddl(createIndexDdl("", compound, M1024, T1, "collection = 'a-x' AND tenant_id = 'whatever'"));
        ddl("CREATE INDEX " + noPredicate + " ON nexus." + leafName(M1024, T1) + " USING hnsw ("
            + column(M1024) + " nexus.vector_cosine_ops)");

        PciCatalog.Snapshot snap = catalog.read();

        assertThat(index(leaf(snap, M1024, T1), compound))
            .hasValueSatisfying(i -> {
                assertThat(i.valid()).isTrue();
                assertThat(i.collection()).isNull();
                assertThat(i.parsed()).isFalse();
            });
        assertThat(index(leaf(snap, M1024, T1), noPredicate))
            .hasValueSatisfying(i -> assertThat(i.parsed()).isFalse());
        assertThat(snap.unparsedCount()).isEqualTo(before.unparsedCount() + 2);
        assertThat(snap.validCount()).as("an unparsed index is not a valid one").isEqualTo(before.validCount());
        assertThat(snap.hasValidIndex(M1024, T1, "a-x")).as("never routed to").isFalse();
    }

    @Test
    void aPciIndexWhoseNameIsNotTheBuildersShape_isUnparsedEvenWithAParseablePredicate() throws Exception {
        PciCatalog.Snapshot before = catalog.read();
        String collection = "code__pcicat-badname__voyage-code-3__v1";
        String operatorMade = "pci_foo";
        String reindexLeftover = PciCatalog.indexName(M1024, T1, collection) + "_ccnew";
        ddl(createIndexDdl("", operatorMade, M1024, T1, "collection = " + lit(collection)));
        ddl(createIndexDdl("", reindexLeftover, M1024, T1, "collection = " + lit(collection)));

        PciCatalog.Snapshot snap = catalog.read();

        for (String name : List.of(operatorMade, reindexLeftover)) {
            assertThat(index(leaf(snap, M1024, T1), name)).as(name).hasValueSatisfying(i -> {
                assertThat(i.valid()).isTrue();
                assertThat(i.parsed()).isFalse();
                assertThat(i.collection()).isNull();
            });
        }
        assertThat(snap.unparsedCount()).isEqualTo(before.unparsedCount() + 2);
        assertThat(snap.validCount()).isEqualTo(before.validCount());
        assertThat(snap.hasValidIndex(M1024, T1, collection)).as("never routed to").isFalse();
    }

    @Test
    void aCorrectlyNamedIndexThatIsNotHnsw_isUnparsedEvenWithAParseablePredicate() throws Exception {
        PciCatalog.Snapshot before = catalog.read();
        String collection = "code__pcicat-btree__voyage-code-3__v1";
        String name = PciCatalog.indexName(M1024, T1, collection);
        ddl("CREATE INDEX " + name + " ON nexus." + leafName(M1024, T1) + " (collection) WHERE collection = "
            + lit(collection));

        PciCatalog.Snapshot snap = catalog.read();

        assertThat(index(leaf(snap, M1024, T1), name)).hasValueSatisfying(i -> {
            assertThat(i.valid()).isTrue();
            assertThat(i.parsed()).isFalse();
        });
        assertThat(snap.unparsedCount()).isEqualTo(before.unparsedCount() + 1);
        assertThat(snap.hasValidIndex(M1024, T1, collection)).as("never routed to").isFalse();
    }

    @Test
    void aCollectionNameWithABackslash_roundTripsWithStandardConformingStringsOn() throws Exception {
        String backslashed = "knowledge__back\\slash__minilm-l6-v2-384__v1";
        buildIndex(M384, T1, backslashed);

        PciCatalog.Snapshot snap = catalog.read();

        assertThat(index(leaf(snap, M384, T1), PciCatalog.indexName(M384, T1, backslashed)))
            .hasValueSatisfying(i -> assertThat(i.collection()).isEqualTo(backslashed));
        assertThat(snap.hasValidIndex(M384, T1, backslashed)).isTrue();
    }

    @Test
    void whenStandardConformingStringsIsOff_everyIndexIsUnparsed() throws Exception {
        buildIndex(M384, T2, "knowledge__scs-off__minilm-l6-v2-384__v1");
        PciCatalog.Snapshot on = catalog.read();
        assertThat(on.validCount()).as("non-vacuity: parsed indexes exist with the setting on").isGreaterThan(0);

        try (Connection c = svcDs.getConnection()) {
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            setStandardConformingStrings(ctx, "off");
            PciCatalog.Snapshot off;
            try {
                off = catalog.read(ctx);
            } finally {
                setStandardConformingStrings(ctx, "on");
            }

            assertThat(off.validCount()).isZero();
            assertThat(off.invalidCount()).isZero();
            assertThat(off.unparsedCount()).as("the same indexes, all unparsed")
                .isEqualTo(on.validCount() + on.invalidCount() + on.unparsedCount());
            assertThat(off.hasValidIndex(M384, T2, "knowledge__scs-off__minilm-l6-v2-384__v1")).isFalse();
        }
    }

    private static void setStandardConformingStrings(DSLContext ctx, String value) {
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.inline("standard_conforming_strings"),
            DSL.inline(value), DSL.inline(false))).fetch();
    }

    @Test
    void aTenantNameWithAQuote_roundTripsThroughTheBoundParse() throws Exception {
        String quotedTenant = "pcicat-o'brien-tenant";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES),
                "tok-pcicat-q-0123456789abcdef0000000", quotedTenant, "pcicatq");
        }
        String collection = "code__pcicat-quoted-tenant__voyage-code-3__v1";
        buildIndex(M1024, quotedTenant, collection);

        PciCatalog.Snapshot snap = catalog.read();

        PciCatalog.Leaf leaf = leaf(snap, M1024, quotedTenant);
        assertThat(leaf.tenant()).isEqualTo(quotedTenant);
        assertThat(index(leaf, PciCatalog.indexName(M1024, quotedTenant, collection)))
            .hasValueSatisfying(i -> assertThat(i.parsed()).isTrue());
        assertThat(snap.hasValidIndex(M1024, quotedTenant, collection)).isTrue();
        assertThat(snap.hasValidIndex(M1024, "pcicat-o''brien-tenant", collection)).isFalse();
    }

    @Test
    void indexesWithoutThePrefix_orOutsideTheChunksLeaves_areNotReported() throws Exception {
        ddl("CREATE INDEX not_a_pci_index ON nexus." + leafName(M1024, T1) + " (collection) WHERE collection = 'zzz'");
        ddl("CREATE INDEX pci_on_the_wrong_table ON nexus.catalog_collections (name) WHERE name = 'zzz'");
        // 'pci_' as a LIKE pattern reads the underscore as any one character, so a LIKE filter would admit this
        // leaf index; the reader's filter is a literal prefix.
        String lookalike = "pcix_" + "0".repeat(23);
        ddl("CREATE INDEX " + lookalike + " ON nexus." + leafName(M1024, T1) + " (collection) WHERE collection = 'zzz'");

        PciCatalog.Snapshot snap = catalog.read();

        assertThat(snap.leaves().stream().flatMap(l -> l.indexes().stream()).map(PciCatalog.Index::name))
            .doesNotContain("not_a_pci_index", "pci_on_the_wrong_table", lookalike);
        assertThat(snap.leaves().stream().flatMap(l -> l.indexes().stream()).map(PciCatalog.Index::name))
            .allMatch(n -> n.startsWith("pci_"));
    }

    @Test
    void anInvalidIndex_readsIndisvalidFalse_afterItsBuildBackendIsTerminated() throws Exception {
        String collection = "code__pcicat-invalid__voyage-code-3__v1";
        String name = PciCatalog.indexName(M1024, T1, collection);
        String appName = "pcicat-invalid-build";
        // pg_stat_activity shows another role's wait events only to a superuser, so the poll runs as one.
        var activityApp = DSL.field(DSL.name("application_name"), String.class);
        var activityPid = DSL.field(DSL.name("pid"), Integer.class);
        var activityWait = DSL.field(DSL.name("wait_event_type"), String.class);
        var activity = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));

        // A session that holds a write-class lock on the leaf: the concurrent build must wait for it
        // after it has committed its (invalid) catalog entry, which makes the wait a state to poll for.
        try (Connection monitor = pg.createConnection(""); Connection blocker = pg.createConnection("")) {
            monitor.setAutoCommit(true);
            DSLContext asSuperuser = DSL.using(monitor, SQLDialect.POSTGRES);
            blocker.setAutoCommit(false);
            // A DELETE that matches nothing still takes ROW EXCLUSIVE on the leaf and keeps it to the end of
            // the transaction, which is the lock a concurrent build waits out.
            DSL.using(blocker, SQLDialect.POSTGRES)
                .deleteFrom(DSL.table(DSL.name("nexus", leafName(M1024, T1))))
                .where(DSL.falseCondition())
                .execute();
            CompletableFuture<Void> build = CompletableFuture.runAsync(() -> {
                try (Connection c = pg.createConnection("?ApplicationName=" + appName)) {
                    PgContainerHelper.runSuperuserDdlOutsideTransaction(c,
                        createIndexDdl("CONCURRENTLY ", name, M1024, T1, "collection = " + lit(collection)));
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            });
            try {
                Supplier<Integer> buildPid = () -> asSuperuser.select(activityPid).from(activity)
                    .where(activityApp.eq(appName)).and(activityWait.eq("Lock")).limit(1)
                    .fetchOne(0, Integer.class);
                await("the concurrent build to wait on the blocker's lock", () -> buildPid.get() != null);
                Integer pid = buildPid.get();
                assertThat(pid).isNotNull();

                // In flight: the entry exists and is not valid.
                assertThat(index(leaf(catalog.read(), M1024, T1), name))
                    .hasValueSatisfying(i -> assertThat(i.valid()).isFalse());

                Boolean terminated;
                try (Connection su = pg.createConnection("")) {
                    terminated = DSL.using(su, SQLDialect.POSTGRES)
                        .select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, DSL.val(pid)))
                        .fetchOne(0, Boolean.class);
                }
                assertThat(terminated).isTrue();
                await("the build statement to end", build::isDone);
                assertThat(build).isCompletedExceptionally();
                // The terminated run left the helper's Liquibase lock held; clear it so a later run is not blocked.
                try (Connection su = pg.createConnection("")) {
                    PgContainerHelper.clearSuperuserDdlOutsideTransactionLock(su);
                }
            } finally {
                blocker.rollback();
            }
        }

        PciCatalog.Snapshot snap = catalog.read();

        assertThat(index(leaf(snap, M1024, T1), name)).hasValueSatisfying(i -> {
            assertThat(i.valid()).as("the terminated build left an invalid index").isFalse();
            assertThat(i.collection()).as("an invalid index is still attributed to its collection").isEqualTo(collection);
        });
        assertThat(snap.hasValidIndex(M1024, T1, collection)).as("invalid is never routed to").isFalse();
        assertThat(snap.invalidCount()).isGreaterThanOrEqualTo(1);
        // The valid one built by another test is unaffected by the invalid sibling.
        // ... and the no-transaction helper runs again once its lock row is clear (bounded: a held lock blocks it).
        CompletableFuture.runAsync(() -> {
            try (Connection c = pg.createConnection("")) {
                PgContainerHelper.runSuperuserDdlOutsideTransaction(c,
                    createIndexDdl("CONCURRENTLY ", PciCatalog.indexName(M1024, T2, collection), M1024, T2,
                        "collection = " + lit(collection)));
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }).get(BOUND.toSeconds(), TimeUnit.SECONDS);
        assertThat(catalog.read().hasValidIndex(M1024, T2, collection)).isTrue();
    }

    // -- the read is bounded (nexus-43ulx.12 fix round; nexus-u9zkn convention) -------------------------------

    /**
     * The read touches {@code pg_catalog.pg_am}; a superuser holding ACCESS EXCLUSIVE on it makes the statement wait.
     * The holder is one Liquibase test changeset (one transaction) that takes the lock and then sleeps 8 s, so the lock
     * is held for a known window. With the bound the read ends at the statement timeout (SQLSTATE 57014) well inside
     * that window; unbounded, it would return only after the holder let go.
     */
    @Test
    void aReadBlockedByALock_failsAtTheStatementBound_whileTheLockIsStillHeld() throws Exception {
        PciCatalog bounded = new PciCatalog(svcDs, Duration.ofMillis(500));
        assertThat(bounded.readBound()).isEqualTo(Duration.ofMillis(500));
        var activityPid = DSL.field(DSL.name("pid"), Integer.class);
        var activityWait = DSL.field(DSL.name("wait_event"), String.class);
        var activityQuery = DSL.field(DSL.name("query"), String.class);
        var activity = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));

        CompletableFuture<Void> holder = CompletableFuture.runAsync(() -> {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.runSuperuserDdl(su,
                    "LOCK TABLE pg_catalog.pg_am IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(8)");
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        try (Connection monitor = pg.createConnection("")) {
            monitor.setAutoCommit(true);
            DSLContext asSuperuser = DSL.using(monitor, SQLDialect.POSTGRES);
            await("the holder to sleep while holding the lock", () -> {
                if (holder.isDone()) {
                    holder.join();   // surfaces the holder's failure instead of timing out
                    throw new AssertionError("the holder ended before the read began");
                }
                return asSuperuser.fetchCount(activity,
                    activityWait.eq("PgSleep").and(activityQuery.like("%pg_sleep(8)%"))
                        .and(activityPid.ne(DSL.function("pg_backend_pid", Integer.class)))) > 0;
            });

            long began = System.nanoTime();
            CompletableFuture<PciCatalog.Snapshot> read = CompletableFuture.supplyAsync(bounded::read);

            assertThatThrownBy(() -> read.get(30, TimeUnit.SECONDS))
                .as("the read must end while the lock is held, not wait for its release")
                .isInstanceOf(ExecutionException.class)
                .hasCauseInstanceOf(DataAccessException.class)
                .satisfies(e -> assertThat(((DataAccessException) e.getCause()).sqlState())
                    .as("statement_timeout").isEqualTo("57014"));
            long tookMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - began);
            assertThat(holder).as("the lock was still held when the read gave up").isNotDone();
            assertThat(tookMs).as("it waited out the bound, not less").isGreaterThanOrEqualTo(400);
        } finally {
            holder.get(60, TimeUnit.SECONDS);
        }

        assertThat(catalog.read().leaves()).as("the pool is healthy and the lock is gone").isNotEmpty();
        // The bound was transaction-local: the pooled connection comes back with no statement_timeout.
        try (Connection c = svcDs.getConnection()) {
            assertThat(DSL.using(c, SQLDialect.POSTGRES)
                .select(DSL.function("current_setting", String.class, DSL.val("statement_timeout")))
                .fetchSingle().value1()).isEqualTo("0");
        }
    }

    /** The statement bound and the socket read bound are set together: bound plus the margin. */
    @Test
    void theReadGivesItsConnectionANetworkTimeoutOfTheBoundPlusTheMargin() throws Exception {
        List<Integer> networkTimeouts = new CopyOnWriteArrayList<>();
        DataSource recording = recordingDataSource(svcDs, networkTimeouts);
        PgSession.setNetworkBoundMarginMsForTests(7_000);
        try {
            PciCatalog.Snapshot snap = new PciCatalog(recording, Duration.ofSeconds(2)).read();

            assertThat(snap.leaves()).isNotEmpty();
            assertThat(networkTimeouts).as("setNetworkTimeout(statement bound 2000 ms + margin 7000 ms)").contains(9_000);
        } finally {
            PgSession.setNetworkBoundMarginMsForTests(-1);
        }
    }

    @Test
    void theDefaultReadBound_isTheSweepStatementBound() {
        assertThat(new PciCatalog(svcDs).readBound()).isEqualTo(Duration.ofSeconds(30));
        assertThat(PciCatalog.DEFAULT_READ_BOUND).isEqualTo(dev.nexus.service.db.SweepBounds.STATEMENT_TIMEOUT);
    }

    /** A pool whose connections report every {@code setNetworkTimeout} argument to {@code sink}. */
    static DataSource recordingDataSource(DataSource target, List<Integer> sink) {
        return (DataSource) Proxy.newProxyInstance(DataSource.class.getClassLoader(), new Class<?>[] {DataSource.class},
            (proxy, method, args) -> {
                try {
                    Object result = method.invoke(target, args);
                    if (!"getConnection".equals(method.getName())) {
                        return result;
                    }
                    Connection real = (Connection) result;
                    return Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[] {Connection.class},
                        (cp, cm, cargs) -> {
                            if ("setNetworkTimeout".equals(cm.getName())) {
                                sink.add((Integer) cargs[1]);
                            }
                            try {
                                return cm.invoke(real, cargs);
                            } catch (InvocationTargetException e) {
                                throw e.getCause();
                            }
                        });
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
            });
    }
}
