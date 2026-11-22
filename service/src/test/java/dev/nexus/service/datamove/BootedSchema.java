// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.DmlUtilsTestFixtures;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.DmlUtilsInstaller;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): a BOOTED install's schema, for the tests that read the live schema and must see what a running
 * engine leaves, not what the Liquibase walk alone leaves. P3.3c's id keys are made by data-move {@code on_complete} statements (the
 * swap moves of {@code nexus.chunks} and {@code nexus.taxonomy_centroids} add the three child id keys and the composite on
 * {@code catalog_collections}), so a database that only ran the walk never has them.
 *
 * <p>{@link #migrateAndBoot} does what the engine does at boot: the walk by the non-superuser migrating role on the pg_background
 * image, then the work the engine's boot starts, driven here by hand until no move is open: the data-move worker with the production
 * failure handler and finisher (the driver {@code Rdr229FkTwinsIntegrationTest} and {@code Rdr229SwapMovesIntegrationTest} run), and
 * P3.3b's {@link LeafIndexPass}, which registers the two swap moves once gate 1 of the completion check holds. The install is empty, so
 * every move over it completes at once; what the drive adds to the schema is what a real boot adds.
 *
 * <p><b>Non-vacuity.</b> Before it returns, the drive asserts that every move a booted schema depends on exists, is complete, and neither
 * failed nor paused, with its label read from the class that owns it (never written here), and that no {@code nexus.data_moves} row is
 * open. A drive that skipped a move, or that waited less than the work took, fails naming the open or missing labels; the wait is
 * bounded ({@link #DRIVE_BOUND}).
 */
public final class BootedSchema {

    /** How long the drive may run before it fails naming what is still open. A boot over an empty install takes seconds. */
    static final Duration DRIVE_BOUND = Duration.ofMinutes(5);

    /** The three child tables whose id key into {@code nexus.chunks} the chunks swap's {@code on_complete} adds and validates. */
    static final List<String> CHUNK_CHILD_TABLES = List.of("catalog_document_chunks", "chunk_orphaned_at", "topic_assignments");

    /** The two tables a swap move is registered for (P3.3b registers, P3.3c completes). */
    static final List<String> SWAP_TABLES = List.of("chunks", "taxonomy_centroids");

    private static final String WORKER_NAME = "nexus-booted-schema";
    private static final String ENGINE_VERSION = "9.9.9-booted-schema";

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> LABEL = DSL.field(DSL.name("label"), String.class);
    private static final Field<String> SUPERSEDES = DSL.field(DSL.name("supersedes"), String.class);
    private static final Field<OffsetDateTime> COMPLETED_AT = DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<OffsetDateTime> FAILED_AT = DSL.field(DSL.name("failed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<OffsetDateTime> PAUSED_AT = DSL.field(DSL.name("paused_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);

    private BootedSchema() { /* static */ }

    /** A container of the image the data-move worker needs (pg_background); the caller stops it. */
    public static PostgreSQLContainer<?> startContainer() {
        return PgContainerHelper.startDataMove();
    }

    /**
     * Migrates the database {@code su} (a superuser connection to a {@link #startContainer} container) as the engine does and boots
     * it: runs the boot work until no move is open, then asserts the moves a booted schema depends on are complete. Leaves {@code su}
     * open, in autocommit, on the booted database.
     *
     * @throws AssertionError naming the labels still open or missing, when the drive does not reach that state within {@link #DRIVE_BOUND}
     */
    public static void migrateAndBoot(Connection su) throws Exception {
        su.setAutoCommit(true);
        DmlUtilsTestFixtures.createAdmin(su);
        PgContainerHelper.createDataMoveExtensions(su);
        DmlUtilsTestFixtures.bootstrapDmlUtils(su);
        String url = su.getMetaData().getURL();

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(url);
        cfg.setUsername(DmlUtilsTestFixtures.ADMIN);
        cfg.setPassword(DmlUtilsTestFixtures.ADMIN_PASSWORD);
        cfg.setMaximumPoolSize(1);
        try (var adminPool = new HikariDataSource(cfg)) {
            var installer = new DmlUtilsInstaller(true);
            SchemaMigrator.migrate(adminPool, installer);
            var admin = new AdminConnection(url, DmlUtilsTestFixtures.ADMIN, DmlUtilsTestFixtures.ADMIN_PASSWORD);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            try (var worker = productionWorker(admin, installer)) {
                var pass = new LeafIndexPass(admin, new BootWorkRegistry(Clock.systemUTC()),
                    c -> CompletionCheck.evaluate(c).gate1(), ENGINE_VERSION);
                drive(ctx, worker, pass);
            }
            assertBooted(ctx);
        }
    }

    /** The worker {@code Rdr229FkTwinsIntegrationTest#productionWorker} builds: the production failure handler and finisher, no sleep between runs. */
    private static DataMoveWorker productionWorker(AdminConnection admin, DmlUtilsInstaller installer) {
        Clock clock = Clock.systemUTC();
        return new DataMoveWorker(admin, installer,
            new DataMoveWorker.Settings(Duration.ofMillis(50), Duration.ofMillis(10), 0),
            WORKER_NAME, clock, new MoveStates(clock), new DataMoveFailureHandler(admin, WORKER_NAME, clock),
            new VerifyingFinisher(), true);
    }

    /**
     * Cycles the worker and the leaf pass until no move is open and both swaps are registered, or the bound runs out. The order is the
     * engine's: the worker completes the stamp and id-twin moves (gate 1), the pass then builds the id unique indexes and registers the
     * swaps, the worker completes those (their {@code on_complete} registers the three validation moves) and then the validations.
     */
    private static void drive(DSLContext ctx, DataMoveWorker worker, LeafIndexPass pass) throws InterruptedException {
        long deadline = System.nanoTime() + DRIVE_BOUND.toNanos();
        while (true) {
            worker.runCycle();
            pass.runOnce();
            Set<String> open = openLabels(ctx);
            Set<String> unregistered = missingLabels(ctx, swapLabels());
            if (open.isEmpty() && unregistered.isEmpty()) {
                return;
            }
            if (System.nanoTime() > deadline) {
                throw new AssertionError("the boot drive did not finish within " + DRIVE_BOUND + ": open moves " + open
                    + ", swap moves never registered " + unregistered);
            }
            Thread.sleep(50);
        }
    }

    // ── non-vacuity ─────────────────────────────────────────────────────────────────────────────────────────

    /**
     * What a booted schema depends on, by label, each read from the class that owns it: P3.1's stamp moves, P3.3a's id-twin validation
     * moves, P3.3b's two swap moves and P3.3c's three child validation moves. A label {@link StampLabels#NOT_APPLICABLE_WHEN_ABSENT}
     * allows to be absent (the retired queue's) is left out while its table or column is absent, as the completion check does.
     */
    static List<String> expectedLabels(DSLContext ctx) {
        Set<String> out = new LinkedHashSet<>(StampLabels.all());
        out.addAll(FkTwins.labels());
        out.removeIf(label -> {
            StampLabels.Applicability only = StampLabels.NOT_APPLICABLE_WHEN_ABSENT.get(label);
            return only != null && !only.holds(ctx, Set.of(only.table()));
        });
        out.addAll(swapLabels());
        for (String child : CHUNK_CHILD_TABLES) {
            out.add(Rdr229SwapMove.validationLabel(child));
        }
        return List.copyOf(out);
    }

    private static List<String> swapLabels() {
        return SWAP_TABLES.stream().map(LeafIndexPass::swapLabel).toList();
    }

    /** Fails unless every expected move exists, is complete and neither failed nor paused, and no move is open. */
    static void assertBooted(DSLContext ctx) {
        List<String> problems = new ArrayList<>();
        for (String label : expectedLabels(ctx)) {
            var row = ctx.select(COMPLETED_AT, FAILED_AT, PAUSED_AT).from(MOVES).where(LABEL.eq(label)).fetchOne();
            if (row == null) {
                problems.add(label + ": no row");
            } else if (row.get(FAILED_AT) != null) {
                problems.add(label + ": failed");
            } else if (row.get(PAUSED_AT) != null) {
                problems.add(label + ": paused");
            } else if (row.get(COMPLETED_AT) == null) {
                problems.add(label + ": not complete");
            }
        }
        Set<String> open = openLabels(ctx);
        if (!open.isEmpty()) {
            problems.add("open moves: " + open);
        }
        Set<String> failed = new TreeSet<>(ctx.select(LABEL).from(MOVES).where(FAILED_AT.isNotNull()).fetch(LABEL));
        if (!failed.isEmpty()) {
            problems.add("failed moves: " + failed);
        }
        if (!problems.isEmpty()) {
            throw new AssertionError("the booted schema's moves are not all complete: " + problems);
        }
    }

    /** The labels of every move the worker still has to work: not complete, not failed and not superseded (the worker's own definition of open). */
    static Set<String> openLabels(DSLContext ctx) {
        var superseded = DSL.select(SUPERSEDES).from(MOVES).where(SUPERSEDES.isNotNull());
        return new TreeSet<>(ctx.select(LABEL).from(MOVES)
            .where(COMPLETED_AT.isNull()).and(FAILED_AT.isNull()).and(LABEL.notIn(superseded))
            .fetch(LABEL));
    }

    private static Set<String> missingLabels(DSLContext ctx, List<String> labels) {
        Set<String> present = new TreeSet<>(ctx.select(LABEL).from(MOVES).where(LABEL.in(labels)).fetch(LABEL));
        Set<String> missing = new TreeSet<>(labels);
        missing.removeAll(present);
        return missing;
    }
}
