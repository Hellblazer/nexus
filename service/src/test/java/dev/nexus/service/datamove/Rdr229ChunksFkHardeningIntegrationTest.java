// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.vectors.PciBuilderSession;
import dev.nexus.service.vectors.Rdr229ChunksFk;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

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
        foreignBackendsSurvive(() -> DataMoveOrphans.terminateLeftover(adminConnection));
    }

    /** The worker's own sweep after the migration ({@code terminateOrphans}) has the same ownership check. */
    @Test
    void theWorkersOrphanSweepEndsOnlyThisDatabasesAndThisRolesDataMoveBackends() throws Exception {
        var worker = productionWorker(new VerifyingFinisher());
        foreignBackendsSurvive(worker::terminateOrphans);
    }

    private void foreignBackendsSurvive(java.util.concurrent.Callable<Integer> sweep) throws Exception {
        String suffix = Long.toString(System.nanoTime());
        String otherDb = "p33c_other_db_" + suffix;
        String otherRole = "p33c_other_role";   // literal and this class's alone (TestRoleUniquenessScanTest); the data-move database is the class's own, and the finally drops it
        PgContainerHelper.createDatabase(su, otherDb);
        PgContainerHelper.createLoginRole(su, otherRole, "p33c_pw", false);
        String otherUrl = adminConnection.url().replaceFirst("(jdbc:postgresql://[^/]+/)[^?]+", "$1" + otherDb);
        try (Connection ours = openAsWorker("nexus-data-move-ours-" + suffix);
             Connection otherDatabase = open(otherUrl, adminConnection.user(), adminConnection.password(), "nexus-data-move-otherdb-" + suffix);
             Connection otherUser = open(adminConnection.url(), otherRole, "p33c_pw", "nexus-data-move-otherrole-" + suffix)) {
            assertThat(alive(ours) && alive(otherDatabase) && alive(otherUser)).as("precondition: three backends with a data-move name").isTrue();

            int ended = sweep.call();

            assertThat(ended).as("it ended at least ours").isGreaterThanOrEqualTo(1);
            assertThat(alive(ours)).as("this database's backend of this role: ended").isFalse();
            assertThat(alive(otherDatabase)).as("the same name in another database of the cluster: left alone").isTrue();
            assertThat(alive(otherUser)).as("the same name, same database, another role: left alone").isTrue();
        } finally {
            PgContainerHelper.dropDatabaseIfExists(su, otherDb);
            PgContainerHelper.dropRoleRevokingGrants(su, otherRole);
        }
    }

    /** A cancel checks in the same statement that the pid is still the backend it was (name and start); a reused pid is left alone. */
    @Test
    void aCancelReachesOnlyTheBackendItWasAimedAt() throws Exception {
        String application = "nexus-data-move-cancel-" + System.nanoTime();
        try (Connection victim = openAsWorker(application)) {
            int victimPid = pid(victim);
            OffsetDateTime start = backendStart(victim);
            var sleeper = CompletableFuture.runAsync(() -> sleep(victim, 30));
            sleepMs(700);

            DataMoveOrphans.cancelBackend(adminConnection, victimPid, application, start.minusSeconds(60));
            DataMoveOrphans.cancelBackend(adminConnection, victimPid, "nexus-data-move-someone-else", start);
            sleepMs(700);
            assertThat(sleeper.isDone()).as("a start or a name that is not this backend's: nothing was cancelled").isFalse();

            DataMoveOrphans.cancelBackend(adminConnection, victimPid, application, start);
            assertThatThrownBy(() -> sleeper.get(10, TimeUnit.SECONDS)).as("the backend it was aimed at: its statement was cancelled (57014)")
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("57014"));
        }
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
            validateLeafByHand(f.leaf());
        }
        pause("rdr229-%");
        resume(Rdr229ChunksFk.ADOPT_LABEL);
        Connection writer = pg.createConnection("");
        parked.add(writer);
        writer.setAutoCommit(false);
        lock(writer, fks.get(fks.size() - 1).leaf(), PgContainerHelper.LockMode.ROW_EXCLUSIVE);
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
        lock(writer, chunkLeaves().get(0), PgContainerHelper.LockMode.ROW_EXCLUSIVE);

        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            setLocal(c, "statement_timeout", 1400);
            setLocal(c, "lock_timeout", "700ms");
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
     * The adoption that outgrows its tight bound (about 2000 leaves in production) is a transient: the move stays open, nothing fails, the retry is
     * counted, and the recorded reason names the bound and the leaf count and says what to do, in the form the status shows. The timeout is forced
     * inside the on_complete, after the re-check: the registered text gets a 2 s sleep ahead of the parent ADD under a 1000 ms bound, which the
     * re-check (a registry read) cannot reach, so the statement timer fires in the adoption's own statement and the reason is the adoption's, never the
     * re-check's. (A lock wait cannot stand in for the slow ADD: the move's lock wait is half its bound, so a lock wait ends in 55P03 first.)
     */
    @Test
    void anAdoptionThatTimesOutUnderItsBoundRecordsTheBoundTheLeafCountAndTheRemedy() throws Exception {
        seed("timeout");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        String text = onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL);
        String addStatement = Rdr229ChunksFk.ADD_PARENT;
        assertThat(text).as("the parent ADD is in the registered text").contains(addStatement);
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("statement_timeout_ms"), Integer.class), 1_000)
            .set(DSL.field(DSL.name("on_complete"), String.class), text.replace(addStatement, "PERFORM pg_catalog.pg_sleep(2);\n    " + addStatement))
            .where(LABEL_F.eq(Rdr229ChunksFk.ADOPT_LABEL)).execute();
        pause("rdr229-%");
        resume(Rdr229ChunksFk.ADOPT_LABEL);

        productionWorker(new VerifyingFinisher()).runCycle();

        var adopt = adoptMoves().get(0);
        assertThat(adopt.get("failed_at")).as("a timeout is a transient, never a failure").isNull();
        assertThat(adopt.get("completed_at")).as("the move stays open").isNull();
        assertThat((Integer) adopt.get("transient_retries")).as("and the timeout counts toward the needs-an-operator threshold").isGreaterThanOrEqualTo(1);
        String reason = (String) adopt.get("last_transient");
        assertThat(reason).as("the reason is the adoption's own, from its on_complete and not from the re-check, and names the bound, the leaf count and the remedy")
            .contains("adoption timed out at 1000 ms over " + chunkLeaves().size() + " leaves")
            .contains("raise statement_timeout_ms on " + Rdr229ChunksFk.ADOPT_LABEL + " for one attempt in a quiet window")
            .doesNotContain("verification in the completion transaction");
        assertThat(MoveReasons.publicForm(reason)).as("and the status keeps it").contains("adoption timed out at 1000 ms over");
        assertThat(compositeOnId()).as("the ADD did not run to completion").isEmpty();
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
        lock(adoption, "chunks", PgContainerHelper.LockMode.SHARE_ROW_EXCLUSIVE);   // what the adoption holds, from its lock to its commit
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

    // ── index builders (linda 2026-11-23, option b) ─────────────────────────────────────────────────────────

    /** A session shaped like a running build: builder-named, in an open transaction holding SHARE UPDATE EXCLUSIVE on {@code leaf}. */
    private Connection realBuilder(String leaf) throws Exception {
        Connection b = openAsWorker(PciBuilderSession.APPLICATION_NAME_PREFIX + "p33c-real-" + System.nanoTime());
        parked.add(b);
        b.setAutoCommit(false);
        lock(b, leaf, PgContainerHelper.LockMode.SHARE_UPDATE_EXCLUSIVE);
        return b;
    }

    private void backoffHasPassed(String label) {
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("last_transient_at"), OffsetDateTime.class), OffsetDateTime.now().minusHours(2))
            .where(LABEL_F.eq(label)).execute();
    }

    private ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> captureAdoptionBuilderLog() {
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(AdoptionBuilders.class);
        var appender = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        appender.start();
        logger.addAppender(appender);
        return appender;
    }

    private void releaseAdoptionBuilderLog(ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> appender) {
        ((ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(AdoptionBuilders.class)).detachAppender(appender);
    }

    private static boolean logged(ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> appender, String text) {
        return appender.list.stream().anyMatch(e -> e.getFormattedMessage().contains(text));
    }

    /**
     * Every leaf is ready and a real build holds SHARE UPDATE EXCLUSIVE on one of them: the first pass times out behind it, ends exactly that builder
     * (and logs which), and fails 55P03; the next pass, after the backoff, takes the lock and adopts.
     */
    @Test
    void aRealBuilderThatBlocksTheLockIsEndedAndTheNextPassAdopts() throws Exception {
        seed("builderkill");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        pause("rdr229-%");
        resume(Rdr229ChunksFk.ADOPT_LABEL);
        var log = captureAdoptionBuilderLog();
        try (Connection builder = realBuilder(chunkLeaves().get(0))) {
            // a clock the test moves: the worker's blocked state keeps its time and the row's backoff is read from the clock too, so "after the backoff"
            // is one advance and no sleep
            var clock = new DataMoveFailureIntegrationTest.MutableClock();
            var w = worker(installed(), 0, new DataMoveFailureHandler(adminConnection, WORKER_NAME, clock), new VerifyingFinisher(), true, clock);

            w.runCycle();

            assertThat(alive(builder)).as("the build that blocked the lock was ended").isFalse();
            assertThat(logged(log, "event=rdr229_adoption_ended_builders ended=1")).as("and the engine log says which, and why").isTrue();
            assertThat(logged(log, "gone=true")).as("the ended builder had left the server when the pass returned, so the next attempt could not meet its lock").isTrue();
            assertThat((String) adoptMoves().get(0).get("last_transient")).as("the pass itself is the 55P03 that backs off")
                .contains(Rdr229ChunksFk.LOCK_FAILED_MARKER);
            assertThat(compositeOnId()).isEmpty();

            // the first failure was recorded at clock time with retries 1, so the retry is due 30 s later; one minute passes, and the single pass that
            // follows takes the move (the worker's blocked state waits only blockedRetry, 10 ms)
            clock.advance(java.time.Duration.ofMinutes(1));
            int worked = w.runCycle();
            var row = adoptMoves().get(0);
            String passes = String.format("%n worked=%d state=%s completed_at=%s registered_at=%s paused_at=%s retries=%s last_transient=%s", worked,
                w.states().state(Rdr229ChunksFk.ADOPT_LABEL).map(st -> st.kind().toString()).orElse("none"), row.get("completed_at"), row.get("registered_at"),
                row.get("paused_at"), row.get("transient_retries"), row.get("last_transient"));
            assertThat(worked).as("the worker claimed the adoption move after the backoff%s", passes).isGreaterThanOrEqualTo(1);
            assertThat(compositeOnId()).as("the next pass took the lock and adopted%s", passes).hasSize(1);
        } finally {
            releaseAdoptionBuilderLog(log);
        }
    }

    /**
     * A builder-NAMED backend of another role that holds the blocking lock is not this engine's builder: the pass that timed out behind it ends
     * nothing and says so. Without the {@code usename} term of AdoptionBuilders the blocker would count as a builder, the pass would log an ended
     * builder (ended=0, since the signal's own role check refuses it) and the first assertion on the log below would fail.
     */
    @Test
    void aBuilderNamedBackendOfAnotherRoleThatBlocksTheLockIsNotEnded() throws Exception {
        seed("builderotherrole");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        pause("rdr229-%");
        resume(Rdr229ChunksFk.ADOPT_LABEL);
        var worker = productionWorker(new VerifyingFinisher());   // before the lock: building one re-runs the migration
        var log = captureAdoptionBuilderLog();
        try (Connection other = open(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), PciBuilderSession.APPLICATION_NAME_PREFIX + "p33c-otherrole-" + System.nanoTime())) {
            parked.add(other);
            other.setAutoCommit(false);
            lock(other, chunkLeaves().get(0), PgContainerHelper.LockMode.SHARE_UPDATE_EXCLUSIVE);

            worker.runCycle();

            assertThat(logged(log, "event=rdr229_adoption_blocked_by_others")).as("the engine log says the blocker is not one of its builders").isTrue();
            assertThat(logged(log, "event=rdr229_adoption_ended_builders")).as("and ends nothing").isFalse();
            assertThat(alive(other)).as("the other role's session is untouched").isTrue();
            assertThat((String) adoptMoves().get(0).get("last_transient")).as("the pass is the plain 55P03 that backs off").contains(Rdr229ChunksFk.LOCK_FAILED_MARKER);
        } finally {
            releaseAdoptionBuilderLog(log);
        }
    }

    /**
     * The signal itself checks ownership, in the statement that sends it: of three builder-named backends, the calling role's in this database is
     * ended; another role's in this database and the same role's in another database are not. Without the {@code usename} term the signal at the
     * other role's backend is refused by the server (an error); without the database term the other database's backend is ended.
     */
    @Test
    void terminatingPidsEndsOnlyBuildersOfThisDatabaseAndThisRole() throws Exception {
        String suffix = Long.toString(System.nanoTime());
        String otherDb = "p33c_builder_db_" + suffix;
        PgContainerHelper.createDatabase(su, otherDb);
        String otherUrl = adminConnection.url().replaceFirst("(jdbc:postgresql://[^/]+/)[^?]+", "$1" + otherDb);
        String name = PciBuilderSession.APPLICATION_NAME_PREFIX + "p33c-term-";
        try (Connection caller = openAsWorker("p33c-term-caller-" + suffix);
             Connection mine = openAsWorker(name + "mine-" + suffix);
             Connection otherRole = open(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), name + "role-" + suffix);
             Connection otherDatabase = open(otherUrl, adminConnection.user(), adminConnection.password(), name + "db-" + suffix)) {
            int ended = dev.nexus.service.db.PciBuilders.terminatePidsOrThrow(DSL.using(caller, SQLDialect.POSTGRES),
                List.of(pid(mine), pid(otherRole), pid(otherDatabase)), "reason=p33c-test", org.slf4j.LoggerFactory.getLogger(Rdr229ChunksFkHardeningIntegrationTest.class));

            assertThat(ended).as("only the calling role's builder of this database was signalled").isEqualTo(1);
            assertThat(waitUntilDead(mine)).as("it is gone").isTrue();
            assertThat(alive(otherRole)).as("another role's builder-named backend in this database: left alone").isTrue();
            assertThat(alive(otherDatabase)).as("the same role's builder-named backend in another database: left alone").isTrue();
        } finally {
            PgContainerHelper.dropDatabaseIfExists(su, otherDb);
        }
    }

    private boolean waitUntilDead(Connection c) throws SQLException {
        long end = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (System.nanoTime() < end) {
            if (!alive(c)) {
                return true;
            }
            sleepMs(50);
        }
        return false;
    }

    /** A pass that defers for a leaf ends no builder, however live: it never reached the lock. */
    @Test
    void aDeferringPassEndsNoBuilder() throws Exception {
        seed("builderdefer");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        var fks = leafFks();
        assertThat(fks).hasSizeGreaterThanOrEqualTo(2);
        for (LeafFk f : fks.subList(1, fks.size())) {
            validateLeafByHand(f.leaf());
        }
        pause("rdr229-%");
        resume(Rdr229ChunksFk.ADOPT_LABEL);
        var log = captureAdoptionBuilderLog();
        try (Connection builder = realBuilder(fks.get(fks.size() - 1).leaf())) {
            productionWorker(new VerifyingFinisher()).runCycle();

            assertThat((String) adoptMoves().get(0).get("last_transient")).as("it deferred on the paused leaf, not on the lock").contains("paused");
            assertThat(alive(builder)).as("and the live build was not touched").isTrue();
            assertThat(log.list).as("nothing was logged about builders").noneMatch(e -> e.getFormattedMessage().contains("rdr229_adoption"));
        } finally {
            releaseAdoptionBuilderLog(log);
        }
    }

    /** A blocker that is not a builder (a plain session holding a lock) is never ended: the pass is a plain 55P03 with its backoff. */
    @Test
    void aBlockerThatIsNotABuilderIsNeverEnded() throws Exception {
        seed("builderplain");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        pause("rdr229-%");
        resume(Rdr229ChunksFk.ADOPT_LABEL);
        var log = captureAdoptionBuilderLog();
        try (Connection plain = openAsWorker("p33c-plain-session")) {
            parked.add(plain);
            plain.setAutoCommit(false);
            lock(plain, chunkLeaves().get(0), PgContainerHelper.LockMode.SHARE_UPDATE_EXCLUSIVE);

            productionWorker(new VerifyingFinisher()).runCycle();

            assertThat(alive(plain)).as("the session that is not a builder is untouched").isTrue();
            assertThat((String) adoptMoves().get(0).get("last_transient")).as("a plain 55P03 on the lock").contains(Rdr229ChunksFk.LOCK_FAILED_MARKER);
            assertThat(logged(log, "event=rdr229_adoption_ended_builders")).as("no builder was ended").isFalse();
            assertThat(logged(log, "event=rdr229_adoption_blocked_by_others")).as("and the log says it was left alone").isTrue();
            assertThat(compositeOnId()).isEmpty();
        } finally {
            releaseAdoptionBuilderLog(log);
        }
    }

    /** Leaf validations spare a live build on another leaf (a build on the leaf itself would make its validation wait out the lock wait instead). */
    @Test
    void leafValidationsSpareALiveBuilderOnAnotherLeaf() throws Exception {
        seed("builderspare");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        var leaves = chunkLeaves();
        assertThat(leaves).hasSizeGreaterThanOrEqualTo(2);
        String built = leaves.get(0);
        pause(Rdr229ChunksFk.ADOPT_LABEL);
        pause("rdr229-fkvalidate-%-chunk-fk");   // the child validations (over other chunk-family tables) end builders by design
        pause(Rdr229ChunksFk.LEAF_LABEL_PREFIX + built);
        try (Connection builder = realBuilder(built)) {
            productionWorker(new VerifyingFinisher()).runCycle();

            assertThat(leafFks().stream().filter(f -> !f.leaf().equals(built))).as("the other leaves validated").allSatisfy(f -> assertThat(f.validated()).isTrue());
            assertThat(alive(builder)).as("and the live build was not ended by their validation").isTrue();
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

    /**
     * Each guard of the rerun alone keeps it quiet, and without both it fails: the skip when the composite exists, and the match that counts a key
     * attached to the parent. Variants of the registered text with one guard, then the other, then both taken out.
     */
    @Test
    void eachRerunGuardAloneKeepsTheSwapQuietAndWithoutBothItFails() throws Exception {
        seed("guards");
        registerSwaps();
        runUntilComposite(new VerifyingFinisher());
        assertThat(compositeOnId()).hasSize(1);
        String text = onCompleteOf(CHUNKS_SWAP);
        String compositeGuard = "IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid = 'nexus.chunks'::pg_catalog.regclass AND conname = '"
            + Rdr229ChunksFk.PARENT_CONSTRAINT + "') THEN";
        String attachedClause = " AND k.confdelsetcols IS NULL";
        assertThat(text).as("the composite guard is in the text").contains(compositeGuard);
        assertThat(text).as("and so is the match clause the attached-key rule sits beside").contains(attachedClause);
        String withoutCompositeGuard = text.replace(compositeGuard, "IF true THEN");
        String withoutAttachedMatch = text.replace(attachedClause, " AND k.conparentid = 0" + attachedClause);
        String withoutEither = withoutAttachedMatch.replace(compositeGuard, "IF true THEN");

        runSql(withoutCompositeGuard);   // the match on an attached key is enough
        runSql(withoutAttachedMatch);    // the composite guard is enough
        assertThatThrownBy(() -> runSql(withoutEither)).as("with neither guard the rerun adds a key that is there (duplicate_object)")
            .satisfies(t -> assertThat(sqlState(t)).isEqualTo("42710"));
    }

    // ── the claim, and the lock wait after the lock ─────────────────────────────────────────────────────────

    /**
     * A leaf's key that is NOT VALID again between its VALIDATE and its claim (an operator, a restore) is not a completed validation: the claim raises
     * 55P03, the move stays open, and the next run validates the key and claims.
     */
    @Test
    void aClaimWhoseLeafKeyWasInvalidatedAfterTheValidationDoesNotCompleteAndTheNextRunValidatesAgain() throws Exception {
        seed("claimcheck");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        String leaf = chunkLeaves().get(0);
        String label = Rdr229ChunksFk.LEAF_LABEL_PREFIX + leaf;
        String key = leaf + Rdr229ChunksFk.LEAF_CONSTRAINT_SUFFIX;
        pause("rdr229-%");
        resume(label);
        var finisher = new VerifyingFinisher();
        var once = new java.util.concurrent.atomic.AtomicBoolean();
        finisher.afterValidateHook = () -> {
            if (once.compareAndSet(false, true)) {
                try {
                    ctx().alterTable(table("nexus", leaf)).dropConstraint(DSL.name(key)).execute();
                    try (Connection c = pg.createConnection("")) {
                        addLeafKeyNotValid(c, leaf, key);
                    }
                } catch (SQLException e) {
                    throw new IllegalStateException(e);
                }
            }
        };

        productionWorker(finisher).runCycle();

        var row = moves(LABEL_F.eq(label)).get(0);
        assertThat(row.get("completed_at")).as("the claim did not complete over a key that is NOT VALID").isNull();
        assertThat(row.get("failed_at")).isNull();
        assertThat((String) row.get("last_transient")).as("the claim raised the 55P03 of the leaf claim check").contains("leaf validation claim").contains("55P03");
        assertThat(leafFks().stream().filter(f -> f.leaf().equals(leaf)).toList()).singleElement().satisfies(f -> assertThat(f.validated()).isFalse());

        backoffHasPassed(label);
        productionWorker(new VerifyingFinisher()).runCycle();

        assertThat(completedAt(label)).as("the next run validated the key again and claimed").isNotNull();
        assertThat(leafFks().stream().filter(f -> f.leaf().equals(leaf)).toList()).singleElement().satisfies(f -> assertThat(f.validated()).isTrue());
    }

    /** Runs the adoption's text under the bounds the finisher gives it (1400 ms, 700 ms) on a session of the test's own. */
    private void runAdoptionUnderProductionBounds(String text) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            try {
                setLocal(c, "statement_timeout", 1400);
                setLocal(c, "lock_timeout", "700ms");
                MoveSql.runOnComplete(c, text);
                c.commit();
            } catch (SQLException | RuntimeException e) {
                c.rollback();
                throw e;
            }
        }
    }

    /** Holds ROW EXCLUSIVE on the registry from a session of the test's own for {@code ms}; the returned future ends when it has been released. */
    private CompletableFuture<Void> holdRegistryFor(long ms) throws SQLException {
        Connection holder = pg.createConnection("");
        parked.add(holder);
        holder.setAutoCommit(false);
        lock(holder, "catalog_collections", PgContainerHelper.LockMode.ROW_EXCLUSIVE);
        return CompletableFuture.runAsync(() -> {
            sleepMs(ms);
            try {
                holder.commit();
            } catch (SQLException e) {
                throw new IllegalStateException(e);
            }
        });
    }

    /**
     * After its lock on chunks the adoption puts its lock wait back to the move's 700 ms, so the ADD can wait out a short reader of the registry (the
     * ADD takes ACCESS EXCLUSIVE there). The control is the same text with the restore taken out: the ADD then waits only the 200 ms of the lock and fails.
     */
    @Test
    void theLockWaitIsRestoredAfterTheLockSoTheAddCanWaitOutAShortRegistryHolder() throws Exception {
        seed("restore");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        String text = onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL);
        String restore = "PERFORM pg_catalog.set_config('lock_timeout', lock_wait, true);";
        assertThat(text).as("the restore is in the text").contains(restore);

        var first = holdRegistryFor(400);
        assertThatThrownBy(() -> runAdoptionUnderProductionBounds(text.replace(restore, "")))
            .as("without the restore the ADD waits only 200 ms").satisfies(t -> assertThat(sqlState(t)).isEqualTo("55P03"));
        first.get(5, TimeUnit.SECONDS);
        assertThat(compositeOnId()).isEmpty();

        var second = holdRegistryFor(400);
        runAdoptionUnderProductionBounds(text);
        second.get(5, TimeUnit.SECONDS);

        assertThat(compositeOnId()).as("with it, the ADD waited the holder out and the composite is there").hasSize(1);
    }

    /** A leaf key that cannot get its lock waits the short 200 ms of the adoption, not the move's 700 ms. */
    @Test
    void aLeafKeyThatCannotGetItsLockFailsInTheShortWait() throws Exception {
        seed("keywait");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        validateEveryLeafByHand();
        String leaf = chunkLeaves().get(0);
        ctx().alterTable(table("nexus", leaf)).dropConstraint(DSL.name(leaf + Rdr229ChunksFk.LEAF_CONSTRAINT_SUFFIX)).execute();
        Connection writer = pg.createConnection("");
        parked.add(writer);
        writer.setAutoCommit(false);
        lock(writer, leaf, PgContainerHelper.LockMode.ROW_EXCLUSIVE);

        long start = System.nanoTime();
        assertThatThrownBy(() -> runAdoptionUnderProductionBounds(onCompleteOf(Rdr229ChunksFk.ADOPT_LABEL)))
            .satisfies(t -> assertThat(sqlState(t)).isEqualTo("55P03"));
        long elapsed = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(elapsed).as("the key's own 200 ms wait, not the move's 700 ms").isBetween(150L, 600L);
    }

    /** A watch that has been closed cancels nothing, even for a move that is paused (the claim that follows a validation must not be cancelled for it). */
    @Test
    void aClosedWatchCancelsNothing() throws Exception {
        seed("watchclose");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        String label = Rdr229ChunksFk.LEAF_LABEL_PREFIX + chunkLeaves().get(0);
        pause(label);
        String application = "nexus-data-move-watchclose-" + System.nanoTime();
        try (Connection victim = openAsWorker(application)) {
            var watch = CompletionWatch.start(adminConnection, label, pid(victim), application, backendStart(victim));
            watch.close();
            sleepMs(2_500);

            assertThat(watch.fired()).as("a closed watch did not fire although its move is paused").isFalse();
            sleep(victim, 0.3);   // would raise 57014 had it been cancelled
        }
    }

    /**
     * A watch closed while it is between reading a paused move and acting on it: the read has told it to cancel, and the close comes after. Nothing is
     * cancelled (the check after the read), and close() returns only once the watch's thread has ended (the join), so the session cannot be cancelled
     * later for a validation that is over. The hook holds the thread in that window for 400 ms, past an interrupt. Without the check after the read the
     * thread cancels the victim when the hook returns: fired() is true and the sleep below raises 57014. Without the join close() returns at once, while
     * the thread is still in the hook: ended() is false.
     */
    @Test
    void aWatchClosedBetweenItsReadAndItsCancelCancelsNothingAndHasEndedWhenCloseReturns() throws Exception {
        seed("watchmid");
        registerSwaps();
        runSwaps(new VerifyingFinisher());
        String label = Rdr229ChunksFk.LEAF_LABEL_PREFIX + chunkLeaves().get(0);
        pause(label);
        String application = "nexus-data-move-watchmid-" + System.nanoTime();
        var read = new java.util.concurrent.CountDownLatch(1);
        try (Connection victim = openAsWorker(application)) {
            var watch = CompletionWatch.start(adminConnection, label, pid(victim), application, backendStart(victim), () -> {
                read.countDown();
                long end = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(400);
                while (System.nanoTime() < end) {
                    try {
                        Thread.sleep(20);
                    } catch (InterruptedException ignored) {
                        // the close interrupts the thread: the window stays open for the whole 400 ms
                    }
                }
            });
            assertThat(read.await(15, TimeUnit.SECONDS)).as("the watch read the paused move").isTrue();

            watch.close();

            assertThat(watch.ended()).as("close() returned only after the watch's thread had ended").isTrue();
            assertThat(watch.fired()).as("the close came after the read and before the cancel: nothing was cancelled").isFalse();
            sleep(victim, 0.3);   // would raise 57014 had it been cancelled
        }
    }

    /**
     * The watch's read of the registry meets ACCESS EXCLUSIVE on nexus.data_moves (an operator's maintenance, a migration changeset). The read has
     * its own 2 s lock wait, so it fails and the watch looks again at its next interval; a close() while the read waits returns within its join
     * (6 s) with the thread ended, and cancels nothing. Without the session's bounds the read waits on the lock until the network timeout and the
     * thread outlives close().
     */
    @Test
    void aWatchWhoseReadIsBlockedOnTheRegistryStillEndsWithinTheJoinOfClose() throws Exception {
        String application = "nexus-data-move-watchlocked-" + System.nanoTime();
        try (Connection victim = openAsWorker(application)) {
            int victimPid = pid(victim);
            OffsetDateTime start = backendStart(victim);
            Connection holder = pg.createConnection("");
            parked.add(holder);
            holder.setAutoCommit(false);
            lock(holder, "data_moves", PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            var watch = CompletionWatch.start(adminConnection, "p33c-watch-locked", victimPid, application, start);
            long seen = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
            while (System.nanoTime() < seen && !watchReadBlocked()) {
                sleepMs(100);
            }
            assertThat(watchReadBlocked()).as("non-vacuity: the watch's read is waiting behind the lock").isTrue();

            long closing = System.nanoTime();
            watch.close();
            long closeMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - closing);

            assertThat(watch.ended()).as("the watch's thread had ended when close() returned (it took %d ms)", closeMs).isTrue();
            assertThat(closeMs).as("within the join").isLessThan(6_000L);
            assertThat(watch.fired()).as("a read that never completed cancelled nothing").isFalse();
            sleep(victim, 0.1);
        }
    }

    /** True when a session named for the watch is waiting on a lock. */
    private boolean watchReadBlocked() {
        return PgContainerHelper.backendsBlockedOnLocks(ctx()) > 0 && ctx().fetchExists(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")),
            DSL.field(DSL.name("application_name"), String.class).eq(DataMoveOrphans.PREFIX + "watch"),
            DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock"));
    }

    // ── the evidence that the worker role's validation is set-based ─────────────────────────────────────────

    /**
     * How many times the registry is scanned (sequentially or by index) during one VALIDATE of {@code table} as {@code user}, read from the
     * transaction's own statistics. A set-based check scans it about once (one anti-join); the per-row fallback probes it once per referencing row,
     * by whichever scan the planner picks (a small registry is read sequentially, so the index count alone is no evidence).
     */
    private long registryScans(String user, String password, String table, String constraint) throws Exception {
        try (Connection c = open(adminConnection.url(), user, password, "p33c-probe")) {
            c.setAutoCommit(false);
            addLeafKeyNotValid(c, table, constraint);
            PgContainerHelper.validateConstraint(c, table("nexus", table), constraint);
            Field<Long> idx = DSL.field(DSL.name("idx_scan"), Long.class);
            Field<Long> seq = DSL.field(DSL.name("seq_scan"), Long.class);
            var scanned = DSL.using(c, SQLDialect.POSTGRES).select(seq, idx).from(DSL.table(DSL.name("pg_catalog", "pg_stat_xact_user_tables")))
                .where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus")).and(DSL.field(DSL.name("relname"), String.class).eq("catalog_collections"))
                .fetchOne();
            c.rollback();
            return scanned == null ? 0L : (scanned.value1() == null ? 0L : scanned.value1()) + (scanned.value2() == null ? 0L : scanned.value2());
        }
    }

    /**
     * The registry is row-secured and FORCED. The worker role (the owner of both tables) validates set-based: the registry is scanned about once. The control is a role that owns the referencing table but not the registry, with SELECT and REFERENCES only: PostgreSQL then
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
        String role = "p33c_nonowner";   // literal and this class's alone (TestRoleUniquenessScanTest); dropped in the finally
        String ownerTable = "p33c_scratch_owner_" + suffix;
        String otherTable = "p33c_scratch_other_" + suffix;
        PgContainerHelper.createLoginRole(su, role, "p33c_pw", false);
        PgContainerHelper.grantSchemaUsage(su, "nexus", role);
        PgContainerHelper.grantSchemaUsage(su, "nexus_test", role);   // the fixtures' functions the probe calls
        Table<?> registry = table("nexus", "catalog_collections");
        ctx().grant(DSL.privilege("select"), DSL.privilege("references")).on(registry).to(DSL.role(DSL.name(role))).execute();
        try {
            for (String t : List.of(ownerTable, otherTable)) {
                // every registry row twenty times over: the per-row fallback probes once per row, the set-based check once in all
                ctx().createTable(table("nexus", t)).as(DSL.select(DSL.field(DSL.name("r", "tenant_id")), DSL.field(DSL.name("r", "collection_id")),
                        DSL.field(DSL.name("r", "embedding_model"))).from(registry.as("r")).crossJoin(DSL.generateSeries(1, 20).as("g"))).execute();
            }
            long rows = ctx().fetchCount(table("nexus", ownerTable));
            assertThat(rows).as("non-vacuity: at least twenty rows, whose probes a per-row check would make").isGreaterThanOrEqualTo(20L);
            PgContainerHelper.alterTableOwner(su, table("nexus", otherTable), role);
            PgContainerHelper.alterTableOwner(su, table("nexus", ownerTable), adminConnection.user());

            long ownerScans = registryScans(adminConnection.user(), adminConnection.password(), ownerTable, "p33c_owner_fk");
            long nonOwnerScans = registryScans(role, "p33c_pw", otherTable, "p33c_other_fk");

            assertThat(ownerScans).as("the worker role: the registry is scanned a handful of times at most for %d rows (one anti-join: set-based)", rows)
                .isLessThanOrEqualTo(3L);
            assertThat(nonOwnerScans).as("a role that does not own the registry: one probe per referencing row (the per-row fallback), %d rows", rows)
                .isGreaterThanOrEqualTo(rows);
        } finally {
            ctx().dropTableIfExists(table("nexus", ownerTable)).execute();
            ctx().dropTableIfExists(table("nexus", otherTable)).execute();
            ctx().revoke(DSL.privilege("select"), DSL.privilege("references")).on(registry).from(DSL.role(DSL.name(role))).execute();
            PgContainerHelper.revokeSchemaUsage(su, "nexus", role);
            PgContainerHelper.revokeSchemaUsage(su, "nexus_test", role);
            PgContainerHelper.dropRoleRevokingGrants(su, role);
        }
    }
}
