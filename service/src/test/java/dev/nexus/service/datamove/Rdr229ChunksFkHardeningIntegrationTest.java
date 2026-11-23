// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.vectors.PciBuilderSession;
import dev.nexus.service.vectors.Rdr229ChunksFk;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21), round 3: what the adoption move and the validation moves do around their edges. The reaper's ownership check,
 * the stop and cancel paths, the adoption's lock discipline (check first, lock only when every leaf looks ready), a rerun of the swap, the
 * evidence that the worker role's VALIDATE is set-based (with a non-owner control), and a crash between a validation and its claim. See
 * {@link Rdr229ChunksFkIntegrationTest} for the route itself.
 */
class Rdr229ChunksFkHardeningIntegrationTest extends Rdr229SwapMovesFixtures {

    @AfterEach
    void releaseParked() throws Exception {
        releaseWhatTheTestParked();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private Connection open(String url, String user, String password, String application) throws SQLException {
        Properties props = new Properties();
        props.setProperty("user", user);
        props.setProperty("password", password);
        props.setProperty("ApplicationName", application);
        return DriverManager.getConnection(url, props);
    }

    private Connection openAsWorker(String application) throws SQLException {
        return open(adminConnection.url(), adminConnection.user(), adminConnection.password(), application);
    }

    private static OffsetDateTime backendStart(Connection c) {
        Field<OffsetDateTime> start = DSL.field(DSL.name("backend_start"), OffsetDateTime.class);
        return DSL.using(c, SQLDialect.POSTGRES).select(start).from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("pid"), Integer.class).eq(pid(c))).fetchOne(start);
    }

    /** Validates every leaf key by hand, so the adoption sees every leaf ready; the leaf moves stay open. */
    private void validateEveryLeafByHand() throws SQLException {
        for (LeafFk f : leafFks()) {
            runSql("ALTER TABLE nexus." + quote(f.leaf()) + " VALIDATE CONSTRAINT " + quote(f.name()));
        }
    }

    private boolean alive(Connection c) throws SQLException {
        return c.isValid(2);
    }

    // ── the reaper's ownership check ────────────────────────────────────────────────────────────────────────

    /**
     * The boot ends the data-move backends of THIS database and THIS role, and no other: a backend with the same name in another database of the
     * cluster, or run by another role in this one, is not ours and is never signalled (Sam: no process is killed without an ownership check).
     */
    @Test
    void theBootReapEndsOnlyThisDatabasesAndThisRolesDataMoveBackends() throws Exception {
        String suffix = Long.toString(System.nanoTime());
        String otherDb = "p33c_other_db_" + suffix;
        String otherRole = "p33c_other_role_" + suffix;
        runSql("CREATE DATABASE " + otherDb);
        runSql("CREATE ROLE " + otherRole + " LOGIN PASSWORD 'p33c_pw'");
        String otherUrl = adminConnection.url().replaceFirst("(jdbc:postgresql://[^/]+/)[^?]+", "$1" + otherDb);
        try (Connection ours = openAsWorker("nexus-data-move-ours-" + suffix);
             Connection otherDatabase = open(otherUrl, adminConnection.user(), adminConnection.password(), "nexus-data-move-otherdb-" + suffix);
             Connection otherUser = open(adminConnection.url(), otherRole, "p33c_pw", "nexus-data-move-otherrole-" + suffix)) {
            assertThat(alive(ours) && alive(otherDatabase) && alive(otherUser)).as("precondition: three backends with a data-move name").isTrue();

            int ended = DataMoveOrphans.terminateLeftover(adminConnection);

            assertThat(ended).as("it ended at least ours").isGreaterThanOrEqualTo(1);
            assertThat(alive(ours)).as("this database's backend of this role: ended").isFalse();
            assertThat(alive(otherDatabase)).as("the same name in another database of the cluster: left alone").isTrue();
            assertThat(alive(otherUser)).as("the same name, same database, another role: left alone").isTrue();
        } finally {
            runSql("DROP DATABASE IF EXISTS " + otherDb + " WITH (FORCE)");
            runSql("DROP ROLE IF EXISTS " + otherRole);
        }
    }

    /** A cancel checks in the same statement that the pid is still the backend it was (name and start); a reused pid is left alone. */
    @Test
    void aCancelReachesOnlyTheBackendItWasAimedAt() throws Exception {
        String application = "nexus-data-move-cancel-" + System.nanoTime();
        try (Connection victim = openAsWorker(application)) {
            int victimPid = pid(victim);
            OffsetDateTime start = backendStart(victim);
            var sleeper = CompletableFuture.runAsync(() -> {
                try {
                    MoveSql.runOnComplete(victim, "SELECT pg_sleep(30)");
                } catch (SQLException e) {
                    throw new IllegalStateException(sqlState(e), e);
                }
            });
            sleepMs(700);

            DataMoveOrphans.cancelBackend(adminConnection, victimPid, application, start.minusSeconds(60));
            DataMoveOrphans.cancelBackend(adminConnection, victimPid, "nexus-data-move-someone-else", start);
            sleepMs(700);
            assertThat(sleeper.isDone()).as("a start or a name that is not this backend's: nothing was cancelled").isFalse();

            DataMoveOrphans.cancelBackend(adminConnection, victimPid, application, start);
            assertThatThrownBy(() -> sleeper.get(10, TimeUnit.SECONDS)).as("the backend it was aimed at: its statement was cancelled")
                .hasRootCauseInstanceOf(SQLException.class);
        }
    }

    /** Main ends the leftover backends BEFORE the schema migration: the call is in the source, ahead of the migrate call. */
    @Test
    void mainReapsLeftoverBackendsBeforeItMigrates() throws Exception {
        String main = Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java"));
        int reap = main.indexOf("DataMoveOrphans.terminateLeftover(");
        int migrate = main.indexOf("SchemaMigrator.migrate(migrationDs");
        assertThat(reap).as("Main calls the reaper").isPositive();
        assertThat(migrate).as("and the migration").isPositive();
        assertThat(reap).as("the reaper runs first").isLessThan(migrate);
    }

    // ── stop, pause, crash ──────────────────────────────────────────────────────────────────────────────────

    /**
     * A stop that comes after the completion's backend is published and before its statement starts reaches nobody (a cancel between two
     * statements is a no-op), so the finisher reads the stop flag again and does not start the VALIDATE. The seam is that window.
     */
    @Test
    void aStopInTheWindowBeforeTheValidationStartsIsHonoured() throws Exception {
        seed("stopwin");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        String leaf = chunkLeaves().get(0);
        String label = Rdr229ChunksFk.LEAF_LABEL_PREFIX + leaf;
        pause("rdr229-%");
        resume(label);
        var finisher = new VerifyingFinisher();
        var w = productionWorker(finisher);
        finisher.afterPublishHook = w::stop;

        w.runCycle();

        assertThat(leafFks().stream().filter(f -> f.leaf().equals(leaf)).toList()).as("the VALIDATE did not run").singleElement()
            .satisfies(f -> assertThat(f.validated()).isFalse());
        var row = moves(LABEL_F.eq(label)).get(0);
        assertThat(row.get("failed_at")).as("a stop is not a failure").isNull();
        assertThat(row.get("last_transient")).isNull();
        assertThat(row.get("completed_at")).isNull();
    }

    /** A pause set while a validation waits cancels it (the stop path, scoped to the move): it ends in about a watch interval, not the 5 s lock wait. */
    @Test
    void aPauseSetWhileAValidationRunsCancelsIt() throws Exception {
        Held held = holdOneLeaf();
        var w = held.worker();
        var cycle = CompletableFuture.runAsync(w::runCycle);
        assertThat(completionPid(w, held.label(), 3_000)).as("the validation is waiting on the lock").isPositive();

        pause(held.label());
        long pausedAt = System.nanoTime();
        cycle.get(4, TimeUnit.SECONDS);

        assertThat(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - pausedAt)).as("cancelled by the watch, before the lock wait ran out")
            .isLessThan(3_500L);
        var row = moves(LABEL_F.eq(held.label())).get(0);
        assertThat(row.get("failed_at")).as("a pause is not a failure").isNull();
        assertThat(row.get("last_transient")).as("nor a transient").isNull();
        assertThat(row.get("completed_at")).isNull();
        assertThat(row.get("paused_at")).as("it stays paused").isNotNull();
    }

    /**
     * An engine that dies between a validation's commit and its claim leaves the key validated and the move open. The next run finds the
     * key validated (its on_complete validates nothing) and claims: the move completes, with no second scan to pay.
     */
    @Test
    void aCrashBetweenTheValidationAndTheClaimIsResumedByTheNextRun() throws Exception {
        seed("crash");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        String leaf = chunkLeaves().get(0);
        String label = Rdr229ChunksFk.LEAF_LABEL_PREFIX + leaf;
        pause("rdr229-%");
        resume(label);
        var dying = new VerifyingFinisher();
        var first = productionWorker(dying);
        dying.afterValidateHook = first::stop;   // the engine stops after the VALIDATE commits and before the claim

        first.runCycle();

        assertThat(leafFks().stream().filter(f -> f.leaf().equals(leaf)).toList()).as("the VALIDATE had committed").singleElement()
            .satisfies(f -> assertThat(f.validated()).isTrue());
        assertThat(completedAt(label)).as("and the claim never happened").isNull();

        productionWorker(new VerifyingFinisher()).runCycle();

        assertThat(completedAt(label)).as("the next run claims it").isNotNull();
        assertThat(moves(LABEL_F.eq(label)).get(0).get("failed_at")).isNull();
    }

    // ── the adoption's lock discipline ──────────────────────────────────────────────────────────────────────

    /**
     * With a leaf still waiting, the adoption move looks first and does not lock: a writer holding ROW EXCLUSIVE on a leaf would make any
     * SHARE ROW EXCLUSIVE request on chunks wait out its 200 ms and fail with a lock timeout, and the recorded reason never says so; it names the
     * paused leaf. No index builder is ended on a pass that defers.
     */
    @Test
    void severalPollsWithALeafPendingTakeNoLockOnChunksAndEndNoBuilder() throws Exception {
        seed("nolock");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        var fks = leafFks();
        assertThat(fks).as("non-vacuity: several leaves").hasSizeGreaterThanOrEqualTo(2);
        String blocked = fks.get(0).leaf();
        for (LeafFk f : fks.subList(1, fks.size())) {
            runSql("ALTER TABLE nexus." + quote(f.leaf()) + " VALIDATE CONSTRAINT " + quote(f.name()));
        }
        pause("rdr229-%");
        resume(Rdr229ChunksFk.ADOPT_LABEL);
        Connection writer = pg.createConnection("");
        parked.add(writer);
        writer.setAutoCommit(false);
        MoveSql.runOnComplete(writer, "LOCK TABLE nexus." + quote(fks.get(fks.size() - 1).leaf()) + " IN ROW EXCLUSIVE MODE");
        try (Connection builder = openAsWorker(PciBuilderSession.APPLICATION_NAME_PREFIX + "p33c-test")) {
            var w = productionWorker(new VerifyingFinisher());

            for (int i = 0; i < 4; i++) {
                w.runCycle();
                ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("last_transient_at"), OffsetDateTime.class), OffsetDateTime.now().minusHours(2))
                    .where(LABEL_F.eq(Rdr229ChunksFk.ADOPT_LABEL)).execute();   // the backoff has passed: poll again
            }

            var adopt = adoptMoves().get(0);
            assertThat((String) adopt.get("last_transient")).as("the pass deferred on the pending leaf " + blocked + ", not on a lock").contains("paused")
                .doesNotContain("lock timeout");
            assertThat(alive(builder)).as("no index builder was ended by a pass that deferred").isTrue();
            assertThat(compositeOnId()).isEmpty();
        }
    }

    /** With every leaf ready the adoption takes its lock under its own 200 ms wait: a held writer makes it fail 55P03 in well under the move's 700 ms. */
    @Test
    void theAdoptionBehindAHeldWriterFailsOn55P03WithinItsOwnShortLockWait() throws Exception {
        seed("writerwait");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        Connection writer = pg.createConnection("");
        parked.add(writer);
        writer.setAutoCommit(false);
        MoveSql.runOnComplete(writer, "LOCK TABLE nexus." + quote(chunkLeaves().get(0)) + " IN ROW EXCLUSIVE MODE");

        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            MoveSql.runOnComplete(c, "SET LOCAL statement_timeout = 1400");
            MoveSql.runOnComplete(c, "SET LOCAL lock_timeout = '700ms'");
            long start = System.nanoTime();
            assertThatThrownBy(() -> MoveSql.runOnComplete(c, onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL)))
                .as("the lock on chunks is refused").satisfies(t -> assertThat(sqlState(t)).isEqualTo("55P03"));
            long elapsed = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
            c.rollback();
            assertThat(elapsed).as("its own 200 ms wait, not the move's 700 ms").isBetween(150L, 650L);
        }
        assertThat(compositeOnId()).isEmpty();
    }

    /**
     * The adoption that outgrows its tight bound (about 2000 leaves in production; here a 150 ms bound and a held writer, so the statement timer
     * fires inside the lock wait) is a transient: the move stays open, nothing fails, the retry is counted, and the recorded reason names the
     * bound and the leaf count and says what to do, in the form the status shows.
     */
    @Test
    void anAdoptionThatTimesOutUnderItsBoundRecordsTheBoundTheLeafCountAndTheRemedy() throws Exception {
        seed("timeout");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("statement_timeout_ms"), Integer.class), 150)
            .where(LABEL_F.eq(Rdr229ChunksFk.ADOPT_LABEL)).execute();
        pause("rdr229-%");
        resume(Rdr229ChunksFk.ADOPT_LABEL);
        Connection writer = pg.createConnection("");
        parked.add(writer);
        writer.setAutoCommit(false);
        MoveSql.runOnComplete(writer, "LOCK TABLE nexus." + quote(chunkLeaves().get(0)) + " IN ROW EXCLUSIVE MODE");

        productionWorker(new VerifyingFinisher()).runCycle();

        var adopt = adoptMoves().get(0);
        assertThat(adopt.get("failed_at")).as("a timeout is a transient, never a failure").isNull();
        assertThat(adopt.get("completed_at")).as("the move stays open").isNull();
        assertThat((Integer) adopt.get("transient_retries")).as("and the timeout counts toward the needs-an-operator threshold").isGreaterThanOrEqualTo(1);
        String reason = (String) adopt.get("last_transient");
        assertThat(reason).as("the reason names the bound, the leaf count and the remedy")
            .contains("adoption timed out at 150 ms over " + VerifyingFinisher.chunkLeafCount(ctx()) + " leaves")
            .contains("raise statement_timeout_ms on " + Rdr229ChunksFk.ADOPT_LABEL + " for one attempt in a quiet window");
        assertThat(MoveReasons.publicForm(reason)).as("and the status keeps it").contains("adoption timed out at 150 ms over");
        assertThat(compositeOnId()).isEmpty();
    }

    /**
     * A leaf created while the adoption holds its lock waits for it (the lock conflicts with the ACCESS EXCLUSIVE a leaf's creation takes on its
     * model partition), and the adoption then sees it: it queues the new leaf and does not add the composite over it.
     */
    @Test
    void aLeafCreatedWhileTheAdoptionHoldsItsLockWaitsAndIsThenCaught() throws Exception {
        seed("serialize");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        Connection adoption = pg.createConnection("");
        parked.add(adoption);
        adoption.setAutoCommit(false);
        MoveSql.runOnComplete(adoption, "LOCK TABLE nexus.chunks IN SHARE ROW EXCLUSIVE MODE");   // what the adoption holds, from its lock to its commit
        String lateTenant = "p33c-serialize-leaf-" + System.nanoTime();
        tenantsUsed.add(lateTenant);
        var creating = CompletableFuture.runAsync(() -> {
            try {
                createTenantLeaves(lateTenant);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });

        sleepMs(1_500);
        assertThat(creating.isDone()).as("the new leaf waits for the lock").isFalse();
        adoption.commit();
        creating.get(15, TimeUnit.SECONDS);

        runOnCompleteOnOwnSession(onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL));
        assertThat(compositeOnId()).as("the adoption saw the new leaf and added nothing over it").isEmpty();
        assertThat(leafFks().stream().filter(f -> !f.validated()).toList()).as("the new leaves got their keys, NOT VALID").isNotEmpty();
    }

    /** Leaf validations spare the index builders; the adoption ends them only when it is about to add the composite. */
    @Test
    void leafValidationsSpareTheIndexBuildersAndTheAdoptionEndsThemOnlyToAdd() throws Exception {
        seed("builders");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        try (Connection builder = openAsWorker(PciBuilderSession.APPLICATION_NAME_PREFIX + "p33c-test")) {
            pause(Rdr229ChunksFk.ADOPT_LABEL);
            productionWorker(new VerifyingFinisher()).runCycle();   // every leaf validation runs
            assertThat(leafFks()).allSatisfy(f -> assertThat(f.validated()).isTrue());
            assertThat(alive(builder)).as("the leaf validations ended no index builder").isTrue();

            resume(Rdr229ChunksFk.ADOPT_LABEL);
            productionWorker(new VerifyingFinisher()).runCycle();

            assertThat(compositeOnId()).as("the adoption added the composite").hasSize(1);
            assertThat(alive(builder)).as("and ended the builder, which would hold a conflicting lock, to do it").isFalse();
        }
    }

    // ── idempotence ─────────────────────────────────────────────────────────────────────────────────────────

    /** Re-running the swap's on_complete after the composite exists adds nothing and fails nowhere (no 42710 on a leaf key that is attached). */
    @Test
    void reRunningTheSwapAfterTheAdoptionIsANoOp() throws Exception {
        seed("rerun");
        registerSwaps();
        runUntilComposite(new VerifyingFinisher());
        assertThat(compositeOnId()).as("precondition: the composite is there").hasSize(1);
        var before = leafFks();

        runSql(onCompleteOf(CHUNKS_SWAP));

        assertThat(compositeOnId()).hasSize(1);
        assertThat(leafFks()).as("no leaf key was added or changed").isEqualTo(before);
        assertThat(leafFks()).allSatisfy(f -> assertThat(f.parentId()).isNotZero());
    }

    // ── the evidence that the worker role's validation is set-based ─────────────────────────────────────────

    /** The registry's idx_scan during one VALIDATE of {@code table} as {@code user}, read from the transaction's own statistics. */
    private long registryProbes(String user, String password, String table, String constraint) throws Exception {
        try (Connection c = open(adminConnection.url(), user, password, "p33c-probe")) {
            c.setAutoCommit(false);
            MoveSql.runOnComplete(c, "ALTER TABLE nexus." + table + " ADD CONSTRAINT " + constraint + " " + Rdr229ChunksFk.FK_DEFINITION + " NOT VALID");
            MoveSql.runOnComplete(c, "ALTER TABLE nexus." + table + " VALIDATE CONSTRAINT " + constraint);
            Field<Long> idx = DSL.field(DSL.name("idx_scan"), Long.class);
            Long scanned = DSL.using(c, SQLDialect.POSTGRES).select(idx).from(DSL.table(DSL.name("pg_catalog", "pg_stat_xact_user_tables")))
                .where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus")).and(DSL.field(DSL.name("relname"), String.class).eq("catalog_collections"))
                .fetchOne(idx);
            c.rollback();
            return scanned == null ? 0L : scanned;
        }
    }

    /**
     * The registry is row-secured and FORCED. The worker role (the owner of both tables) validates set-based: the registry is probed by index
     * not once. The control is a role that owns the referencing table but not the registry, with SELECT and REFERENCES only: PostgreSQL then
     * falls back to one probe per row (RI_Initial_Check), so the registry's idx_scan is positive for the same VALIDATE. Without the control a
     * zero could be a sequential scan of something else.
     */
    @Test
    void theWorkerRolesValidationIsSetBasedAndANonOwnersIsNot() throws Exception {
        seed("setbased");
        Field<Boolean> rls = DSL.field(DSL.name("c", "relrowsecurity"), Boolean.class);
        Field<Boolean> forced = DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class);
        var flags = ctx().select(rls, forced).from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq("catalog_collections")).fetchOne();
        assertThat(flags).as("catalog_collections is there").isNotNull();
        assertThat(flags.value1()).as("row-level security is enabled on the registry").isTrue();
        assertThat(flags.value2()).as("and forced on its owner").isTrue();

        String suffix = Long.toString(System.nanoTime());
        String role = "p33c_nonowner_" + suffix;
        String ownerTable = "p33c_scratch_owner_" + suffix;
        String otherTable = "p33c_scratch_other_" + suffix;
        runSql("CREATE ROLE " + role + " LOGIN PASSWORD 'p33c_pw'");
        runSql("GRANT USAGE ON SCHEMA nexus TO " + role);
        runSql("GRANT SELECT, REFERENCES ON nexus.catalog_collections TO " + role);
        try {
            for (String t : List.of(ownerTable, otherTable)) {
                runSql("CREATE TABLE nexus." + t + " AS SELECT tenant_id, collection_id, embedding_model FROM nexus.catalog_collections LIMIT 200");
            }
            assertThat(ctx().fetchCount(table("nexus", ownerTable))).as("non-vacuity: rows whose probes a per-row check would make").isPositive();
            runSql("ALTER TABLE nexus." + otherTable + " OWNER TO " + role);
            runSql("ALTER TABLE nexus." + ownerTable + " OWNER TO " + adminConnection.user());

            long ownerProbes = registryProbes(adminConnection.user(), adminConnection.password(), ownerTable, "p33c_owner_fk");
            long nonOwnerProbes = registryProbes(role, "p33c_pw", otherTable, "p33c_other_fk");

            assertThat(ownerProbes).as("the worker role: no probe of the registry by index (set-based)").isZero();
            assertThat(nonOwnerProbes).as("a role that does not own the registry: the per-row fallback probes it by index").isPositive();
        } finally {
            runSql("DROP TABLE IF EXISTS nexus." + ownerTable);
            runSql("DROP TABLE IF EXISTS nexus." + otherTable);
            runSql("REVOKE ALL ON nexus.catalog_collections FROM " + role);
            runSql("REVOKE ALL ON SCHEMA nexus FROM " + role);
            runSql("DROP ROLE IF EXISTS " + role);
        }
    }
}
