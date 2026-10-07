// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.SchemaMigrator.MigrationException;
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

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.OptionalLong;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-225 P1.4 (nexus-3wh8d.9): the local disk preflight. Before the Liquibase walk, while
 * {@code vectors-030-1} is pending, the engine refuses to start unless the data directory's filesystem has
 * at least 2.2x (chunks + taxonomy_centroids, indexes and TOAST included) free. Free space arrives through an
 * injected supplier, so no test fills a disk. The expected requirement is written down by hand here
 * ({@code ceil(total * 22 / 10)}, total read from the live store), not read back from the engine.
 */
class P225DiskPreflightIntegrationTest {

    private static final String ADMIN = "nexus_admin_p225disk";
    private static final String ADMIN_PASS = "nexus_admin_p225disk_pass";
    private static final String MASTER = "db/changelog/db.changelog-master.xml";
    private static final String WALK = "vectors-030-1";

    @Test
    void lowFreeSpaceRefusesToStart_namesRequiredFreeAndShortfall_andLeavesTheStoreUnwalked() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, WALK);
                long required = requiredBytes(pg);
                long free = required - 1;      // one byte short: the boundary

                assertThatThrownBy(() -> SchemaMigrator.migrate(ds, () -> { }, () -> OptionalLong.of(free)))
                    .isInstanceOf(MigrationException.class)
                    .hasMessageContaining("required " + required + " bytes")
                    .hasMessageContaining("free " + free + " bytes")
                    .hasMessageContaining("shortfall 1 bytes");

                assertThat(walkApplied(pg)).as("the refused boot left the walk unapplied").isFalse();
                assertThat(chunksIsPlainTable(pg)).as("nexus.chunks is still the old layout").isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void exactlyEnoughFreeSpaceLetsTheWalkRun_andOnceApplied_theCheckDoesNotRunAgain() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, WALK);
                long required = requiredBytes(pg);
                AtomicInteger firstBootReads = new AtomicInteger();

                SchemaMigrator.migrate(ds, () -> { }, () -> {
                    firstBootReads.incrementAndGet();
                    return OptionalLong.of(required);   // exactly the requirement: not a refusal
                });
                assertThat(firstBootReads.get()).as("pending walk: the free-space source was read").isEqualTo(1);
                assertThat(walkApplied(pg)).isTrue();
                assertThat(chunksIsPlainTable(pg)).as("nexus.chunks is now partitioned").isFalse();

                // Second boot: the changeset is in databasechangelog. A source reporting zero free bytes would
                // refuse if the check ran; it must not even be read.
                AtomicInteger secondBootReads = new AtomicInteger();
                SchemaMigrator.migrate(ds, () -> { }, () -> {
                    secondBootReads.incrementAndGet();
                    return OptionalLong.of(0);
                });
                assertThat(secondBootReads.get()).as("applied walk: the check does not run").isZero();
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void whenTheEngineCannotSeeTheDataDirectory_theCheckIsSkippedAndTheWalkRuns() throws Exception {
        // The managed/remote branch: the source reports "no local data directory" (the production source does
        // so when NX_PG_DATA_DIR is unset or names nothing the engine can see). A 1-byte-of-everything store
        // would refuse if a number were available; an empty answer must skip, not refuse.
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, WALK);
                AtomicInteger reads = new AtomicInteger();
                SchemaMigrator.migrate(ds, () -> { }, () -> {
                    reads.incrementAndGet();
                    return OptionalLong.empty();
                });
                assertThat(reads.get()).isEqualTo(1);
                assertThat(walkApplied(pg)).as("skipped, so the walk ran").isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void aFreshInstallHasNothingToCopy_soTheFreeSpaceSourceIsNeverRead() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                AtomicInteger reads = new AtomicInteger();
                SchemaMigrator.migrate(ds, () -> { }, () -> {
                    reads.incrementAndGet();
                    return OptionalLong.of(0);
                });
                assertThat(reads.get()).as("no nexus.chunks yet: nothing to measure").isZero();
                assertThat(walkApplied(pg)).isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * The wiring gap (nexus-3wh8d.19, DP3): every test above injects its own free-space supplier through the
     * three-argument overload, so none would notice {@code migrate(ds)}, the single entry point {@code Main}
     * calls, passing an empty supplier and switching the preflight off. A real child JVM runs that entry point
     * with {@code NX_PG_DATA_DIR} naming a real directory; the preflight must read that directory's filesystem
     * and report it. With an empty supplier the check returns silently and the event never appears.
     *
     * <p>The environment cannot be set in-process, hence the child. A refusal cannot be provoked from here (it
     * needs a filesystem with less than 2.2x the store's size free), so the pin is on the free figure the
     * preflight logs, which only the real supplier can produce.
     */
    @Test
    void migrateDataSource_readsTheFreeSpaceOfNxPgDataDir() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        Path dataDir = Files.createTempDirectory("p225-disk-wiring");
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, WALK);
                assertThat(requiredBytes(pg)).as("a walk is pending with data to copy").isPositive();

                var pb = new ProcessBuilder(
                    Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                    "-cp", System.getProperty("java.class.path"),
                    MigrateEntryPoint.class.getName())
                    .redirectErrorStream(true);
                pb.environment().put("NX_DB_URL", pg.getJdbcUrl());
                pb.environment().put("NX_DB_USER", ADMIN);
                pb.environment().put("NX_DB_PASS", ADMIN_PASS);
                pb.environment().put(dev.nexus.service.db.LocalDiskPreflight.DATA_DIR_ENV, dataDir.toString());
                Process p = pb.start();
                var out = new CopyOnWriteArrayList<String>();
                try {
                    Thread reader = new Thread(() -> {
                        try (var r = new BufferedReader(
                                new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                            String line;
                            while ((line = r.readLine()) != null) out.add(line);
                        } catch (java.io.IOException ignored) {
                            // torn down mid-read
                        }
                    }, "migrate-entry-reader");
                    reader.setDaemon(true);
                    reader.start();
                    boolean exited = p.waitFor(240, TimeUnit.SECONDS);
                    reader.join(5_000);
                    String log = String.join("\n", out);
                    assertThat(exited).as("the child exits by itself; output:%n%s", log).isTrue();
                    assertThat(p.exitValue()).as("the walk ran; output:%n%s", log).isZero();

                    Matcher m = Pattern.compile("event=disk_preflight_passed required_bytes=(\\d+) free_bytes=(\\d+)")
                        .matcher(log);
                    assertThat(m.find())
                        .as("migrate(ds) ran the disk preflight against the NX_PG_DATA_DIR filesystem; output:%n%s", log)
                        .isTrue();
                    long reported = Long.parseLong(m.group(2));
                    long actual = Files.getFileStore(dataDir).getUsableSpace();
                    assertThat(reported).as("free_bytes is the data directory's filesystem, not a stub")
                        .isBetween(Math.max(0, actual - (256L << 20)), actual + (256L << 20));
                    assertThat(walkApplied(pg)).isTrue();
                } finally {
                    p.destroyForcibly();
                }
            }
        } finally {
            Files.deleteIfExists(dataDir);
            pg.stop();
        }
    }

    /** The production entry point, in a child JVM: exactly the call {@code Main} makes, with the env it was given. */
    public static final class MigrateEntryPoint {
        public static void main(String[] args) {
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(System.getenv("NX_DB_URL"));
            cfg.setUsername(System.getenv("NX_DB_USER"));
            cfg.setPassword(System.getenv("NX_DB_PASS"));
            cfg.setMaximumPoolSize(2);
            try (HikariDataSource ds = new HikariDataSource(cfg)) {
                SchemaMigrator.migrate(ds);
            }
            System.exit(0);
        }
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    /** ceil(2.2 * (chunks + taxonomy_centroids, each with indexes and TOAST)), by hand, off the live store. */
    private static long requiredBytes(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            long total = totalBytes(ctx, "nexus.chunks") + totalBytes(ctx, "nexus.taxonomy_centroids");
            assertThat(total).as("a pre-walk store has measurable tables").isPositive();
            return (total * 22 + 9) / 10;
        }
    }

    private static long totalBytes(DSLContext ctx, String qualified) {
        return ctx.select(DSL.function("pg_total_relation_size", SQLDataType.BIGINT,
                DSL.function("to_regclass", SQLDataType.OTHER, DSL.val(qualified))))
            .fetchOne(0, Long.class);
    }

    private static boolean walkApplied(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .fetchCount(DSL.table(DSL.name("public", "databasechangelog")),
                    DSL.field(DSL.name("id"), String.class).eq(WALK)) > 0;
        }
    }

    /** True while nexus.chunks is the pre-walk ordinary table: a partitioned parent has model partitions. */
    private static boolean chunksIsPlainTable(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return PartitionScratch.children(DSL.using(su, SQLDialect.POSTGRES), "chunks").isEmpty();
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
