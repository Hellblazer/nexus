// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.vectors.PciBuilderSession.BuilderState;
import dev.nexus.service.vectors.PciBuilderSession.DdlOutcome;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.util.List;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-227 Step 2 (nexus-43ulx.16): {@link PciBuilderSession} against the real partitioned layout.
 *
 * <p>The builder's admin credentials are the container superuser (it owns the leaves, as {@code nexus_admin} does
 * in production). The no-privilege case uses the service role, which the engine falls back to when
 * {@code NX_DB_ADMIN_*} are absent and which owns nothing. Catalog reads and the lock/migration fixtures go
 * through typed jOOQ; the only statements this class sends as text are the builder's own.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciBuilderSessionIntegrationTest {

    static final String SVC_ROLE = "svc_pcibld";
    static final String SVC_PASS = "svc_pcibld_pass";
    static final String T1 = "pcibld-tenant-1";
    static final String T2 = "pcibld-tenant-2";
    static final String M1024 = "voyage-code-3";
    static final String M384 = "minilm-l6-v2-384";
    static final String NONCE = "a1b2c3d4";
    static final PciSettings ON = new PciSettings(true, 20_000, 600, 16);

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
            PgContainerHelper.seedServiceToken(ctx, "tok-pcibld-1-0123456789abcdef000000", T1, "pcibld1");
            PgContainerHelper.seedServiceToken(ctx, "tok-pcibld-2-0123456789abcdef000000", T2, "pcibld2");
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

    PciBuilderSession adminSession() {
        return new PciBuilderSession(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), NONCE, ON);
    }

    PciBuilderSession serviceRoleSession() {
        return new PciBuilderSession(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, NONCE, ON);
    }

    PciCatalog.Leaf leaf(String model, String tenant) {
        List<PciCatalog.Leaf> hits = catalog.read().leaves().stream()
            .filter(l -> model.equals(l.model()) && tenant.equals(l.tenant())).toList();
        assertThat(hits).as("exactly one leaf for (%s, %s)", model, tenant).hasSize(1);
        return hits.get(0);
    }

    java.util.Optional<PciCatalog.Index> indexOn(String model, String tenant, String name) {
        return leaf(model, tenant).indexes().stream().filter(i -> name.equals(i.name())).findFirst();
    }

    static String collectionName(String tag) {
        return "code__pcibld-" + tag + "__voyage-code-3__v1";
    }

    /** Run {@code work} on a fresh superuser connection (autocommit) with a typed context. */
    interface Work<T> {
        T run(DSLContext ctx) throws Exception;
    }

    <T> T asSuperuser(Work<T> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return work.run(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    static Field<Object> col(String alias, String column) {
        return DSL.field(DSL.name(alias, column), Object.class);
    }

    /** Count of backends with this exact application_name. */
    long backendsNamed(String applicationName) throws Exception {
        return asSuperuser(ctx -> ctx.selectCount()
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class).eq(applicationName))
            .fetchOne(0, Long.class));
    }

    /** Sessions holding the advisory lock {@code key} (granted). */
    long holdersOf(long key) throws Exception {
        return asSuperuser(ctx -> ctx.selectCount()
            .from(DSL.table(DSL.name("pg_catalog", "pg_locks")))
            .where(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))
            .and(DSL.field(DSL.name("granted"), Boolean.class).isTrue())
            .and(DSL.field(DSL.name("classid"), Long.class).cast(SQLDataType.BIGINT).eq(key >>> 32))
            .and(DSL.field(DSL.name("objid"), Long.class).cast(SQLDataType.BIGINT).eq(key & 0xffffffffL))
            .and(DSL.field(DSL.name("objsubid"), Integer.class).eq(1))
            .fetchOne(0, Long.class));
    }

    /** A session of the test that holds the migration lock, as a migrator mid-walk does. */
    final class HeldMigrationLock implements AutoCloseable {
        private final Connection conn;
        private final DSLContext ctx;
        final int pid;

        HeldMigrationLock() throws Exception {
            conn = pg.createConnection("");
            conn.setAutoCommit(true);
            ctx = DSL.using(conn, SQLDialect.POSTGRES);
            ctx.select(DSL.function("pg_advisory_lock", SQLDataType.OTHER,
                DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY))).fetch();
            pid = ctx.select(DSL.function("pg_backend_pid", SQLDataType.INTEGER)).fetchOne(0, Integer.class);
        }

        @Override
        public void close() throws Exception {
            conn.close();
        }
    }

    /** Capture the builder's log lines for the duration of {@code body}. */
    List<String> captureLogs(Work<Void> body) throws Exception {
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(PciBuilderSession.class);
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        Level before = logger.getLevel();
        logger.setLevel(Level.DEBUG);
        logger.addAppender(appender);
        try {
            body.run(null);
        } finally {
            logger.detachAppender(appender);
            logger.setLevel(before);
        }
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    static long count(List<String> lines, String needle) {
        return lines.stream().filter(l -> l.contains(needle)).count();
    }

    // -- tests -------------------------------------------------------------------------------------------

    @Test
    void build_createsTheIndexOnTheNamedLeaf_withTheLeafIndexsOwnAccessMethodOpclassAndOptions() throws Exception {
        String collection = collectionName("shape");
        String name = PciCatalog.indexName(M1024, T1, collection);
        PciCatalog.Leaf leaf = leaf(M1024, T1);

        PciBuilderSession session = adminSession();
        try (PciBuilderSession.Pass pass = session.open()) {
            assertThat(pass.state()).isEqualTo(BuilderState.OK);
            assertThat(pass.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.DONE);
        }

        assertThat(indexOn(M1024, T1, name)).hasValueSatisfying(i -> {
            assertThat(i.valid()).isTrue();
            assertThat(i.parsed()).isTrue();
            assertThat(i.collection()).isEqualTo(collection);
        });
        assertThat(catalog.read().hasValidIndex(M1024, T1, collection)).isTrue();
        assertThat(leaf(M1024, T2).indexes()).as("the other tenant's leaf is untouched")
            .extracting(PciCatalog.Index::name).doesNotContain(name);

        // vectors-030 / vectors-004:321-327: the per-collection index must match the leaf's own index in
        // access method, operator class (schema included) and options.
        IndexShape reference = shapeOf(leaf, "embedding_1024", false);
        IndexShape built = shapeOf(leaf, "embedding_1024", true);
        assertThat(reference.accessMethod()).isEqualTo("hnsw");
        assertThat(built.accessMethod()).isEqualTo(reference.accessMethod());
        assertThat(built.opclass()).as("opclass text").isEqualTo(reference.opclass());
        assertThat(built.options()).isEqualTo(reference.options());
        assertThat(built.options()).contains("m=" + PciBuilderSession.BUILD_M)
            .contains("ef_construction=" + PciBuilderSession.BUILD_EF_CONSTRUCTION);
        assertThat(PciBuilderSession.BUILD_M).isEqualTo(16);
        assertThat(PciBuilderSession.BUILD_EF_CONSTRUCTION).isEqualTo(64);
    }

    record IndexShape(String accessMethod, String opclass, String options) { }

    /**
     * The access method, operator-class text and reloptions of the hnsw index on {@code column} of the leaf:
     * the builder's {@code pci_} one when {@code builder}, else the leaf's own.
     */
    IndexShape shapeOf(PciCatalog.Leaf leaf, String column, boolean builder) throws Exception {
        return asSuperuser(ctx -> {
            Field<String> relname = DSL.field(DSL.name("ix", "relname"), String.class);
            Field<String> def = DSL.function(DSL.name("pg_catalog", "pg_get_indexdef"), SQLDataType.CLOB,
                col("i", "indexrelid"));
            var rows = ctx.select(DSL.field(DSL.name("am", "amname"), String.class),
                    DSL.field(DSL.name("ix", "reloptions"), Object.class).cast(SQLDataType.CLOB), def)
                .from(DSL.table(DSL.name("pg_catalog", "pg_index")).as("i"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("ix")).on(col("ix", "oid").eq(col("i", "indexrelid")))
                .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("t")).on(col("t", "oid").eq(col("i", "indrelid")))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n")).on(col("n", "oid").eq(col("t", "relnamespace")))
                .join(DSL.table(DSL.name("pg_catalog", "pg_am")).as("am")).on(col("am", "oid").eq(col("ix", "relam")))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(leaf.schema()))
                .and(DSL.field(DSL.name("t", "relname"), String.class).eq(leaf.name()))
                .and(builder ? DSL.left(relname, 4).eq("pci_") : DSL.left(relname, 4).ne("pci_"))
                .fetch();
            var matching = rows.stream().filter(r -> r.value3().contains("(" + column + " ")).toList();
            assertThat(matching).as("one %s hnsw index on the column", builder ? "pci_" : "leaf").hasSize(1);
            var r = matching.get(0);
            Matcher m = Pattern.compile("\\(" + column + " ([^)]+)\\)").matcher(r.value3());
            assertThat(m.find()).as(r.value3()).isTrue();
            return new IndexShape(r.value1(), m.group(1), r.value2());
        });
    }

    @Test
    void aCollectionNameWithAQuote_isRenderedByTheServer_andRoundTripsThroughTheCatalog() throws Exception {
        String collection = "knowledge__it's-a \"name\" \\ with; DROP__minilm-l6-v2-384__v1";
        PciCatalog.Leaf leaf = leaf(M384, T1);

        try (PciBuilderSession.Pass pass = adminSession().open()) {
            assertThat(pass.build(leaf, 384, collection)).isEqualTo(DdlOutcome.DONE);
        }

        assertThat(indexOn(M384, T1, PciCatalog.indexName(M384, T1, collection)))
            .hasValueSatisfying(i -> assertThat(i.collection()).isEqualTo(collection));
        assertThat(catalog.read().hasValidIndex(M384, T1, collection)).isTrue();
    }

    @Test
    void buildingTwice_isIdempotent() throws Exception {
        String collection = collectionName("twice");
        PciCatalog.Leaf leaf = leaf(M1024, T1);
        try (PciBuilderSession.Pass pass = adminSession().open()) {
            assertThat(pass.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.DONE);
            assertThat(pass.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.DONE);
        }
        assertThat(leaf(M1024, T1).indexes().stream()
            .filter(i -> collection.equals(i.collection())).count()).isEqualTo(1);
    }

    @Test
    void theBuildTakesNoLockTimeoutAndThirtyMinutes_theDropFiveSeconds() throws Exception {
        String collection = collectionName("settings");
        String name = PciCatalog.indexName(M1024, T1, collection);
        PciCatalog.Leaf leaf = leaf(M1024, T1);

        try (PciBuilderSession.Pass pass = adminSession().open()) {
            assertThat(pass.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.DONE);
            assertThat(pass.currentSetting("lock_timeout")).isEqualTo("0");
            assertThat(pass.currentSetting("statement_timeout")).isEqualTo("30min");
            PciCatalog.Index built = indexOn(M1024, T1, name).orElseThrow();
            assertThat(pass.drop(leaf, built)).isEqualTo(DdlOutcome.DONE);
            assertThat(pass.currentSetting("lock_timeout")).isEqualTo("5s");
        }
        assertThat(indexOn(M1024, T1, name)).as("dropped").isEmpty();
    }

    @Test
    void aDropBlockedByAnOpenTransaction_givesUpAfterFiveSeconds_andLeavesTheIndex() throws Exception {
        String collection = collectionName("droptimeout");
        String name = PciCatalog.indexName(M1024, T1, collection);
        PciCatalog.Leaf leaf = leaf(M1024, T1);
        try (PciBuilderSession.Pass pass = adminSession().open()) {
            assertThat(pass.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.DONE);
        }
        PciCatalog.Index built = indexOn(M1024, T1, name).orElseThrow();

        // A reader that never finishes: DROP INDEX CONCURRENTLY waits for it, and the wait is a lock wait.
        try (Connection reader = pg.createConnection("")) {
            reader.setAutoCommit(false);
            DSL.using(reader, SQLDialect.POSTGRES).selectCount()
                .from(DSL.table(DSL.name(leaf.schema(), leaf.name()))).fetch();

            long start = System.nanoTime();
            DdlOutcome outcome;
            try (PciBuilderSession.Pass pass = adminSession().open()) {
                outcome = pass.drop(leaf, built);
            }
            Duration took = Duration.ofNanos(System.nanoTime() - start);

            assertThat(outcome).isEqualTo(DdlOutcome.FAILED);
            assertThat(took).isBetween(Duration.ofSeconds(4), Duration.ofSeconds(30));
            reader.rollback();
        }
        assertThat(indexOn(M1024, T1, name)).as("a timed-out drop leaves the index").isPresent();

        try (PciBuilderSession.Pass pass = adminSession().open()) {
            assertThat(pass.drop(leaf, indexOn(M1024, T1, name).orElseThrow())).isEqualTo(DdlOutcome.DONE);
        }
        assertThat(indexOn(M1024, T1, name)).as("retried once the reader ended").isEmpty();
    }

    @Test
    void theLockKey_isOutsideInt4_andIsNotTheMigrationKey() {
        long key = PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY;
        assertThat(key).isNotEqualTo(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY);
        assertThat(key).isGreaterThan((long) Integer.MAX_VALUE);
    }

    @Test
    void twoSessionsOnOneDatabase_theSecondReportsStandby_andBuildsNothing() throws Exception {
        String collection = collectionName("standby");
        PciCatalog.Leaf leaf = leaf(M1024, T1);
        PciBuilderSession first = adminSession();
        PciBuilderSession second = adminSession();

        try (PciBuilderSession.Pass holder = first.open()) {
            assertThat(holder.state()).isEqualTo(BuilderState.OK);
            assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY))
                .as("the pass holds the builder lock for its whole length").isEqualTo(1);

            try (PciBuilderSession.Pass peer = second.open()) {
                assertThat(peer.state()).isEqualTo(BuilderState.STANDBY);
                assertThat(second.state()).isEqualTo(BuilderState.STANDBY);
                assertThat(peer.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.INACTIVE);
            }
            assertThat(indexOn(M1024, T1, PciCatalog.indexName(M1024, T1, collection)))
                .as("the standby built nothing").isEmpty();
            assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY))
                .as("the standby's failed try left the holder alone").isEqualTo(1);
        }
        assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY))
            .as("closing the pass releases the lock").isZero();

        try (PciBuilderSession.Pass later = second.open()) {
            assertThat(later.state()).isEqualTo(BuilderState.OK);
            assertThat(later.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.DONE);
        }
        assertThat(second.state()).isEqualTo(BuilderState.OK);
    }

    @Test
    void aHeldMigrationLock_makesTheOpeningPassSkipDdl() throws Exception {
        String collection = collectionName("migskip");
        String name = PciCatalog.indexName(M1024, T1, collection);
        PciCatalog.Leaf leaf = leaf(M1024, T1);

        List<String> logs = captureLogs(ignored -> {
            try (HeldMigrationLock migrating = new HeldMigrationLock();
                 PciBuilderSession.Pass pass = adminSession().open()) {
                assertThat(pass.skippedForMigration()).isTrue();
                assertThat(pass.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.SKIPPED_MIGRATION);
                assertThat(backendsNamed(PciBuilderSession.APPLICATION_NAME_PREFIX + NONCE))
                    .as("the skipped pass closed its session").isZero();
            }
            return null;
        });

        assertThat(indexOn(M1024, T1, name)).as("no DDL ran while the migration lock was held").isEmpty();
        assertThat(count(logs, "event=pci_ddl_skipped reason=migration_in_progress")).isEqualTo(1);
    }

    @Test
    void aMigrationStartingMidPass_skipsTheNextStatement_andEndsThePass() throws Exception {
        String first = collectionName("midpass-1");
        String second = collectionName("midpass-2");
        PciCatalog.Leaf leaf = leaf(M1024, T1);

        try (PciBuilderSession.Pass pass = adminSession().open()) {
            assertThat(pass.skippedForMigration()).isFalse();
            assertThat(pass.build(leaf, 1024, first)).isEqualTo(DdlOutcome.DONE);
            try (HeldMigrationLock migrating = new HeldMigrationLock()) {
                assertThat(pass.build(leaf, 1024, second)).isEqualTo(DdlOutcome.SKIPPED_MIGRATION);
            }
            assertThat(pass.skippedForMigration()).isTrue();
            assertThat(pass.build(leaf, 1024, second))
                .as("the pass ended; a released migration lock does not revive it").isEqualTo(DdlOutcome.SKIPPED_MIGRATION);
        }
        assertThat(indexOn(M1024, T1, PciCatalog.indexName(M1024, T1, first))).isPresent();
        assertThat(indexOn(M1024, T1, PciCatalog.indexName(M1024, T1, second))).isEmpty();

        try (PciBuilderSession.Pass next = adminSession().open()) {
            assertThat(next.build(leaf, 1024, second)).as("the next pass builds").isEqualTo(DdlOutcome.DONE);
        }
        assertThat(indexOn(M1024, T1, PciCatalog.indexName(M1024, T1, second))).isPresent();
    }

    @Test
    void migrationLockHolderPid_namesTheHolder_andIsMinusOneWhenFree() throws Exception {
        assertThat(asSuperuser(SchemaMigrator::migrationLockHolderPid)).isEqualTo(-1);
        try (HeldMigrationLock migrating = new HeldMigrationLock()) {
            assertThat(asSuperuser(SchemaMigrator::migrationLockHolderPid)).isEqualTo(migrating.pid);
        }
        assertThat(asSuperuser(SchemaMigrator::migrationLockHolderPid)).isEqualTo(-1);
    }

    /**
     * Advisory locks are database-scoped, and {@code pg_locks} lists a lock from every database of the cluster. A
     * migration walking in ANOTHER database on the same cluster must not make this database's builder skip DDL, so
     * {@code migrationLockHolderPid} filters on the current database. Without the filter this reports the other
     * database's pid (the control below proves the row is visible in {@code pg_locks}).
     */
    @Test
    void aMigrationLockHeldInAnotherDatabase_isNotAHolderHere_butIsInItsOwn() throws Exception {
        String other = "pcibld_otherdb";
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdlOutsideTransaction(su, "CREATE DATABASE " + other);
        }
        try {
            String url = pg.getJdbcUrl().replace("/" + pg.getDatabaseName(), "/" + other);
            assertThat(url).as("the url names the other database").isNotEqualTo(pg.getJdbcUrl());
            try (Connection elsewhere = java.sql.DriverManager.getConnection(url, pg.getUsername(), pg.getPassword())) {
                elsewhere.setAutoCommit(true);
                DSLContext there = DSL.using(elsewhere, SQLDialect.POSTGRES);
                there.select(DSL.function("pg_advisory_lock", SQLDataType.OTHER,
                    DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY))).fetch();
                int elsewherePid = there.select(DSL.function("pg_backend_pid", SQLDataType.INTEGER))
                    .fetchOne(0, Integer.class);

                assertThat(holdersOf(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY))
                    .as("control: the row is in pg_locks (a cluster-wide view), held by the other database's session")
                    .isEqualTo(1);
                assertThat(asSuperuser(SchemaMigrator::migrationLockHolderPid))
                    .as("but it is not this database's migration").isEqualTo(-1);
                assertThat(SchemaMigrator.migrationLockHolderPid(there)).as("and it is, in its own database")
                    .isEqualTo(elsewherePid);
                try (PciBuilderSession.Pass pass = adminSession().open()) {
                    assertThat(pass.skippedForMigration()).as("this database's pass is not skipped").isFalse();
                }
            }
        } finally {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.runSuperuserDdlOutsideTransaction(su, "DROP DATABASE IF EXISTS " + other);
            }
        }
    }

    @Test
    void aWrongAdminPassword_givesAuthFailed_onEveryPass_andRunsNoDdl() throws Exception {
        String collection = collectionName("auth");
        PciCatalog.Leaf leaf = leaf(M1024, T1);
        // Never written to a log or an assertion message.
        String wrong = "wrong-" + UUID.randomUUID();
        PciBuilderSession session = new PciBuilderSession(pg.getJdbcUrl(), pg.getUsername(), wrong, NONCE, ON);

        List<String> logs = captureLogs(ignored -> {
            for (int pass = 1; pass <= 2; pass++) {
                try (PciBuilderSession.Pass p = session.open()) {
                    assertThat(p.state()).isEqualTo(BuilderState.AUTH_FAILED);
                    assertThat(session.state()).isEqualTo(BuilderState.AUTH_FAILED);
                    assertThat(p.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.INACTIVE);
                }
            }
            return null;
        });

        assertThat(count(logs, "event=pci_builder_auth_failed")).as("logged on every pass").isEqualTo(2);
        assertThat(logs).noneMatch(l -> l.contains(wrong));
        assertThat(logs).as("the driver's reason is logged, so an operator sees why")
            .anyMatch(l -> l.contains("event=pci_builder_auth_failed") && l.contains("password authentication failed"));
        assertThat(indexOn(M1024, T1, PciCatalog.indexName(M1024, T1, collection))).isEmpty();
    }

    @Test
    void aRoleWithoutPrivilegeOnTheLeaf_givesNoPrivilege_logsOnce_andBuildsNothing() throws Exception {
        String collection = collectionName("nopriv");
        String other = collectionName("nopriv-2");
        PciCatalog.Leaf leaf = leaf(M1024, T1);
        PciBuilderSession session = serviceRoleSession();

        List<String> logs = captureLogs(ignored -> {
            try (PciBuilderSession.Pass pass = session.open()) {
                assertThat(pass.state()).isEqualTo(BuilderState.OK);
                assertThat(pass.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.NO_PRIVILEGE);
                assertThat(pass.state()).isEqualTo(BuilderState.NO_PRIVILEGE);
                assertThat(pass.build(leaf, 1024, other)).as("no second attempt in the pass")
                    .isEqualTo(DdlOutcome.NO_PRIVILEGE);
            }
            try (PciBuilderSession.Pass pass = session.open()) {
                assertThat(pass.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.NO_PRIVILEGE);
            }
            return null;
        });

        assertThat(session.state()).isEqualTo(BuilderState.NO_PRIVILEGE);
        assertThat(count(logs, "event=pci_builder_no_privilege")).as("logged once, not per pass").isEqualTo(1);
        assertThat(indexOn(M1024, T1, PciCatalog.indexName(M1024, T1, collection))).isEmpty();
        assertThat(indexOn(M1024, T1, PciCatalog.indexName(M1024, T1, other))).isEmpty();
    }

    @Test
    void theBuilderBackend_carriesTheBootNonceInItsApplicationName_andIsGoneWhenThePassEnds() throws Exception {
        String applicationName = PciBuilderSession.APPLICATION_NAME_PREFIX + NONCE;
        assertThat(applicationName).isEqualTo("nexus-pci-builder-a1b2c3d4");

        try (PciBuilderSession.Pass pass = adminSession().open()) {
            assertThat(pass.state()).isEqualTo(BuilderState.OK);
            assertThat(backendsNamed(applicationName)).isEqualTo(1);
        }
        assertThat(backendsNamed(applicationName)).as("the connection closes at the end of the pass").isZero();
    }

    @Test
    void withTheSwitchOff_thePassOpensNoConnection_andRunsNoDdl() throws Exception {
        String collection = collectionName("off");
        PciCatalog.Leaf leaf = leaf(M1024, T1);
        PciBuilderSession session = new PciBuilderSession(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), NONCE,
            new PciSettings(false, 20_000, 600, 16));

        try (PciBuilderSession.Pass pass = session.open()) {
            assertThat(pass.state()).isEqualTo(BuilderState.OFF);
            assertThat(session.state()).isEqualTo(BuilderState.OFF);
            assertThat(backendsNamed(PciBuilderSession.APPLICATION_NAME_PREFIX + NONCE)).isZero();
            assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY)).isZero();
            assertThat(pass.build(leaf, 1024, collection)).isEqualTo(DdlOutcome.INACTIVE);
        }
        assertThat(indexOn(M1024, T1, PciCatalog.indexName(M1024, T1, collection))).isEmpty();
    }

    @Test
    void aStatementTheNameCheckRefuses_isRejectedBeforeAnyDdl() throws Exception {
        PciCatalog.Leaf leaf = leaf(M1024, T1);
        String collection = collectionName("reject");
        // An operator-made pci_ index: parsed()==false, a non-builder name.
        String operatorMade = "pci_foo";
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su,
                "CREATE INDEX " + operatorMade + " ON " + leaf.schema() + "." + leaf.name()
                    + " USING hnsw (embedding_1024 nexus.vector_cosine_ops) WHERE collection = 'x'");
        }
        PciCatalog.Index unparsed = leaf(M1024, T1).indexes().stream()
            .filter(i -> operatorMade.equals(i.name())).findFirst().orElseThrow();
        assertThat(unparsed.parsed()).isFalse();
        // A parsed index that nonetheless carries a name the builder never makes.
        PciCatalog.Index wrongShape = new PciCatalog.Index("pci_" + "0".repeat(23) + "G", true, collection);

        try (PciBuilderSession.Pass pass = adminSession().open()) {
            assertThat(pass.drop(leaf, unparsed)).isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.drop(leaf, wrongShape)).isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.build(leaf, 7, collection)).as("no such embedding column").isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.build(new PciCatalog.Leaf(leaf.schema(), leaf.name(), null, leaf.tenant(), List.of()),
                1024, collection)).as("a leaf whose model did not parse").isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.build(leaf, 1024, "has\0nul")).isEqualTo(DdlOutcome.REJECTED);
            assertThat(pass.state()).as("a rejection is not a state change").isEqualTo(BuilderState.OK);
        }
        assertThat(leaf(M1024, T1).indexes()).extracting(PciCatalog.Index::name).contains(operatorMade);
    }
}
