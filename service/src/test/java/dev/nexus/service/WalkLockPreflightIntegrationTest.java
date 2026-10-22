// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.SchemaMigrator.MigrationException;
import dev.nexus.service.jooq.binding.Vector;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * nexus-vxmcl: the walk's lock-capacity preflight. Before the Liquibase walk, while {@code vectors-030-1} is
 * pending, the engine refuses to start when the walk's estimated relation-lock count exceeds the server's shared
 * lock table. The estimate is written down by hand here ({@code 100 + 80 x 4 models + 19 x leaves}, leaves = 4 models x tenants x
 * 2 parents) and checked against the count the walk itself reports at its end ({@code RAISE NOTICE 'rdr225 walk: %
 * relation lock(s) held'}), so an estimate below a real walk's need fails here. The estimate test uses token
 * tenants; the refusal test uses tenants that hold a chunk and no token, which the preflight can only see with
 * FORCE ROW LEVEL SECURITY off (a plain read as the migration role returns no rows).
 */
class WalkLockPreflightIntegrationTest {

    private static final String ADMIN = "nexus_admin_p225lock";
    private static final String ADMIN_PASS = "nexus_admin_p225lock_pass";
    private static final String MASTER = "db/changelog/db.changelog-master.xml";
    private static final String WALK = "vectors-030-1";
    private static final Pattern WALK_LOCKS = Pattern.compile("rdr225 walk: (\\d+) relation lock");

    /** The four seeded models x the tenants (token tenants plus 'default') x the two partitioned parents. */
    private static long leaves(int tokenTenants) {
        return 4L * (tokenTenants + 1) * 2;
    }

    private static long estimate(long leaves) {
        return 100 + 80 * 4 + 19 * leaves;
    }

    @Test
    void theEstimateCoversWhatTheWalkHolds_atOneAndAtTwentyTokenTenants() throws Exception {
        for (int tokenTenants : new int[] { 1, 20 }) {
            long measured = walkAndCountLocks(tokenTenants);
            long leaves = leaves(tokenTenants);
            PartitionScratch.evidence("WALK_LOCKS tenants=" + (tokenTenants + 1) + " leaves=" + leaves
                + " measured=" + measured + " estimate=" + estimate(leaves));
            assertThat(estimate(leaves))
                .as("the preflight's estimate for %d leaves is at least the walk's own count", leaves)
                .isGreaterThanOrEqualTo(measured);
        }
    }

    @Test
    void aLockTableTooSmallForTheWalkRefusesToStart_countingDataOnlyTenants_andLeavesTheStoreUnwalked() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, WALK);
                long capacity = lockTableCapacity(pg);
                // The fewest token tenants whose walk the estimate puts over this server's lock table.
                int tokenTenants = 0;
                while (estimate(leaves(tokenTenants)) <= capacity) {
                    tokenTenants++;
                }
                seedDataOnlyTenants(pg, tokenTenants);
                long leaves = leaves(tokenTenants);

                assertThatThrownBy(() -> SchemaMigrator.migrate(ds))
                    .isInstanceOf(MigrationException.class)
                    .hasMessageContaining("creates " + leaves + " leaf partitions (4 models x " + (tokenTenants + 1)
                        + " tenants x 2 tables)")
                    .hasMessageContaining("about " + estimate(leaves) + " relation locks")
                    .hasMessageContaining("has room for " + capacity)
                    .hasMessageContaining("Set max_locks_per_transaction to at least");

                assertThat(walkApplied(pg)).as("the refused boot left the walk unapplied").isFalse();
                assertThat(forceRls(pg, "chunks")).as("the count's NO FORCE was rolled back").isTrue();
                assertThat(forceRls(pg, "taxonomy_centroids")).as("the count's NO FORCE was rolled back").isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * A store last booted before {@code catalog-036} has {@code nexus.chunks} and no {@code nexus.embedding_models}.
     * The preflight counts the four models {@code catalog-036-2} will seed. Pinned through a refusal, which the
     * fail-open catch cannot turn into a pass: without the stand-in, the models query fails and the check skips.
     */
    @Test
    void aStoreFromBeforeTheEmbeddingModelsTable_isEstimatedAtTheFourSeededModels() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, "catalog-036-1");
                assertThat(relationExists(pg, "nexus.chunks")).isTrue();
                assertThat(relationExists(pg, "nexus.embedding_models")).isFalse();
                long capacity = lockTableCapacity(pg);
                int tokenTenants = 0;
                while (estimate(leaves(tokenTenants)) <= capacity) {
                    tokenTenants++;
                }
                seedTokenTenants(pg, tokenTenants);

                assertThatThrownBy(() -> SchemaMigrator.migrate(ds))
                    .isInstanceOf(MigrationException.class)
                    .hasMessageContaining("(4 models x " + (tokenTenants + 1) + " tenants x 2 tables)");
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * The check is advisory: a query it cannot finish is logged and skipped, and the boot goes on. A session holding
     * nexus.chunks (as a running walk does to its commit) makes the count's ALTER wait out its lock_timeout; the
     * preflight logs the skip and the walk runs once the holder lets go.
     */
    @Test
    void aTenantCountThatCannotTakeItsLock_isSkippedAfterTheLockTimeout_andTheWalkRuns() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
            dev.nexus.service.db.WalkLockPreflight.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg); Connection holder = pg.createConnection("")) {
                migrateUpTo(ds, WALK);
                holder.setAutoCommit(false);
                DSL.using(holder, SQLDialect.POSTGRES).fetchCount(DSL.table(DSL.name("nexus", "chunks")));  // ACCESS SHARE to commit
                Thread release = new Thread(() -> {
                    try {
                        Thread.sleep(8_000);
                        holder.rollback();
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                }, "chunks-holder");
                logger.addAppender(logs);
                release.start();
                try {
                    SchemaMigrator.migrate(ds);
                } finally {
                    logger.detachAppender(logs);
                    release.join(30_000);
                }
                assertThat(logs.list.stream().map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage))
                    .as("the count timed out on the held lock and was skipped, not waited out")
                    .anySatisfy(m -> assertThat(m).contains("event=walk_lock_preflight_skipped")
                        .contains("lock preflight query failed").contains("lock timeout"))
                    .noneSatisfy(m -> assertThat(m).contains("event=walk_lock_preflight_passed"));
                assertThat(walkApplied(pg)).as("the boot went on and walked").isTrue();
                assertThat(forceRls(pg, "taxonomy_centroids")).as("FORCE restored by the rollback").isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    /** A fresh pre-walk store with {@code tokenTenants} token tenants, walked through migrate(ds); the walk's count. */
    private static long walkAndCountLocks(int tokenTenants) throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        List<String> engineLog = new CopyOnWriteArrayList<>();
        java.util.logging.Logger jul = java.util.logging.Logger.getLogger("");
        java.util.logging.Handler capture = new java.util.logging.Handler() {
            @Override public void publish(java.util.logging.LogRecord r) {
                engineLog.add(new java.util.logging.SimpleFormatter().formatMessage(r));
            }
            @Override public void flush() { }
            @Override public void close() { }
        };
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, WALK);
                seedTokenTenants(pg, tokenTenants);
                jul.addHandler(capture);
                try {
                    SchemaMigrator.migrate(ds);
                } finally {
                    jul.removeHandler(capture);
                }
                assertThat(walkApplied(pg)).isTrue();
                Matcher m = WALK_LOCKS.matcher(String.join("\n", engineLog));
                assertThat(m.find()).as("the walk's end-of-walk lock count reached the engine log").isTrue();
                return Long.parseLong(m.group(1));
            }
        } finally {
            pg.stop();
        }
    }

    private static void seedTokenTenants(PostgreSQLContainer<?> pg, int n) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int i = 0; i < n; i++) {
                PgContainerHelper.seedServiceToken(ctx, "tok-p225lock-" + i, "p225lock-" + i, "p225lock-" + i);
            }
        }
    }

    /**
     * {@code n} tenants with no token row, alternately holding one chunk or one centroid in a registered collection.
     * The centroid-only ones are the tenants a plain read misses: taxonomy_centroids has no owner-read policy, so
     * under FORCE ROW LEVEL SECURITY the migration role sees none of its rows.
     */
    private static void seedDataOnlyTenants(PostgreSQLContainer<?> pg, int n) throws Exception {
        float[] f = new float[1024];
        java.util.Arrays.fill(f, 0.1f);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int i = 0; i < n; i++) {
                String tenant = "p225lockdata-" + i;
                String coll = "code__lockdata" + i + "-owner__voyage-code-3__v1";
                PgContainerHelper.insertCollection(ctx, tenant, coll);
                if (i % 2 == 0) {
                    PgContainerHelper.insertChunk1024(ctx, tenant, coll, Chash.ofText("p225lock:" + i).toBytes(), Vector.of(f));
                } else {
                    ctx.insertInto(TAXONOMY_CENTROIDS)
                        .set(TAXONOMY_CENTROIDS.TENANT_ID, tenant).set(TAXONOMY_CENTROIDS.COLLECTION, coll)
                        .set(TAXONOMY_CENTROIDS.TOPIC_ID, 9000L + i).set(TAXONOMY_CENTROIDS.LABEL, "c" + i)
                        .set(TAXONOMY_CENTROIDS.EMBEDDING_1024, Vector.of(f))
                        .execute();
                }
            }
        }
    }

    private static boolean forceRls(PostgreSQLContainer<?> pg, String table) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("relforcerowsecurity"), Boolean.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")))
                .where(DSL.field(DSL.name("oid")).eq(DSL.function("to_regclass", SQLDataType.OTHER, DSL.val("nexus." + table))))
                .fetchOne(0, Boolean.class);
        }
    }

    private static boolean relationExists(PostgreSQLContainer<?> pg, String qualified) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.function("to_regclass", SQLDataType.VARCHAR, DSL.val(qualified)))
                .fetchOne(0, String.class) != null;
        }
    }

    /** max_locks_per_transaction x (max_connections + max_prepared_transactions), read off the server. */
    private static long lockTableCapacity(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            return setting(ctx, "max_locks_per_transaction")
                * (setting(ctx, "max_connections") + setting(ctx, "max_prepared_transactions"));
        }
    }

    private static long setting(DSLContext ctx, String name) {
        return Long.parseLong(ctx.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val(name)))
            .fetchOne(0, String.class));
    }

    private static boolean walkApplied(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .fetchCount(DSL.table(DSL.name("public", "databasechangelog")),
                    DSL.field(DSL.name("id"), String.class).eq(WALK)) > 0;
        }
    }

    private static HikariDataSource pool(PostgreSQLContainer<?> c) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(c.getJdbcUrl());
        cfg.setUsername(ADMIN);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(3);
        return new HikariDataSource(cfg);
    }

    /** Applies the changelog up to, not including, {@code target} (the walk tests' own idiom). */
    private static void migrateUpTo(HikariDataSource ds, String target) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase lb = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = lb.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (target.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as(target + " must be in the master changelog").isGreaterThanOrEqualTo(0);
                lb.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
