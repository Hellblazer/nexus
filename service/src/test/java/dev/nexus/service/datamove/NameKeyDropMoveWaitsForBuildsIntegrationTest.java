// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.db.PciBuilders;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;

import java.sql.Connection;
import java.sql.DriverManager;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.7 row 23 (nexus-p2m1x.27), Sam 2026-11-23 about 23:50 PST (T2
 * {@code nexus/sam-ruling-rdr229-drop-move-waits-for-id-builds-2026-11-24}, "Fix: wait, don't end"): the REAL name-key drop move
 * ({@link NameKeyDropMove}, registered by its own {@code registerIfAbsent}) through the REAL {@link VerifyingFinisher} on the product schema.
 * A completion attempt that finds an index build in flight defers: it reports a not-now, writes no failure and no retry count, leaves the
 * move open and ends nothing, so the build runs to completion. With no build in flight the drop completes and the keys are gone.
 *
 * <p>A build in flight is a real {@code CREATE INDEX CONCURRENTLY} held mid-build ({@link InFlightIndexBuild}): that is what the drop
 * waits for, and what {@code PciBuilders.building} finds (linda 2026-11-24: a builder-named session that is not building does not defer
 * the drop). A builder-named session holding ROW SHARE on {@code nexus.chunks} is the counter-case: it blocks the drop's ALTER (so a lock
 * wait times out, 55P03) but is no build, so that stays an ordinary failure. The tests run in order because the completing test drops the
 * keys of the container the class shares (the deferring tests leave them where they are, and say so by comparing the count before and
 * after).
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class NameKeyDropMoveWaitsForBuildsIntegrationTest extends DataMoveIntegrationBase {

    private static final String VERSION = "9.9.9-row23";
    private static final String BUILDER = "nexus-pci-builder-row23";
    /** The start of the reason a deferral carries (the class's own constant is not used: the test pins the words). */
    private static final String WAITING = "waiting for in-flight index builds";
    private static final String BEFORE_LOCKS = "seen before the drop asked for its locks";
    private static final String LOCK_WAIT = "the drop's lock wait timed out behind a build that began after the first read";
    private static final org.slf4j.Logger LOG = org.slf4j.LoggerFactory.getLogger("row23-test");
    private static final String SESSION_TABLE = "tenant_plain";   // the finisher reads the move from the registry; the session's table is not touched
    private static final Field<OffsetDateTime> FAILED_AT = DSL.field(DSL.name("failed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<String> FAILURE = DSL.field(DSL.name("failure"), String.class);
    private static final Field<String> LAST_TRANSIENT = DSL.field(DSL.name("last_transient"), String.class);

    private static CompletionCheck.Result latched() {
        return new CompletionCheck.Result(true, true, true, List.of(), List.of(), true, 0L, List.of(), List.of(), "",
            CompletionCheck.Dangling.UNREAD, false, true, false, false, CompletionCheck.RenameState.NOT_READ);
    }

    private void registerTheDropMove() {
        assertThat(NameKeyDropMove.registerIfAbsent(latched(), ctx(), VERSION)).as("the move is registered by its own pass's call").isTrue();
    }

    /** How many of the eight name keys (seven, and the queue's where its table exists) are still there. */
    private long keysPresent() {
        List<NameKeyDropMove.NameKey> all = new ArrayList<>(NameKeyDropMove.NAME_KEYS);
        all.add(NameKeyDropMove.QUEUE_KEY);
        return all.stream().filter(k -> PgCatalogProbes.constraintExists(ctx(), k.constraint())).count();
    }

    private Connection builderHoldingChunks(String applicationName) throws Exception {
        var props = new Properties();
        props.setProperty("user", ADMIN);
        props.setProperty("password", ADMIN_PASSWORD);
        props.setProperty("ApplicationName", applicationName);
        Connection c = DriverManager.getConnection(pg.getJdbcUrl(), props);
        c.setAutoCommit(false);
        DSL.using(c, SQLDialect.POSTGRES).select(DSL.one()).from(table("nexus", "chunks")).forShare().fetch();
        return c;
    }

    private void assertNothingWasWritten(String label) {
        var row = ctx().select(FAILED_AT, FAILURE, LAST_TRANSIENT, COMPLETED_AT).from(table("nexus", "data_moves")).where(LABEL.eq(label)).fetchOne();
        assertThat(row.get(FAILED_AT)).as("not failed").isNull();
        assertThat(row.get(FAILURE)).as("no failure text").isNull();
        assertThat(row.get(LAST_TRANSIENT)).as("no transient failure recorded").isNull();
        assertThat(row.get(COMPLETED_AT)).as("the move is still incomplete").isNull();
        var move = DataMoveRegistry.reload(ctx(), label).orElseThrow();
        assertThat(move.transientRetries()).as("no failure count").isZero();
    }

    /** The finisher's reason is a not-now with nothing for the failure handler. */
    private static void assertDeferral(MoveFinisher.Result result, String path) {
        assertThat(result.outcome()).as("a deferral, not a completion and not a failure").isEqualTo(MoveFinisher.Outcome.BLOCKED);
        assertThat(result.failure()).as("nothing for the failure handler: no count, no page").isNull();
        assertThat(result.reason()).startsWith(WAITING).contains("running=1").contains(path);
    }

    // ── (a) a build in flight: a deferral, read before the transaction ───────────────────────────────────

    @Test
    @Order(1)
    void aBuildInFlight_defersTheCompletion_noFailureNoCount_theMoveStaysOpen_andTheBuildIsNotEnded() throws Exception {
        registerTheDropMove();
        long keysBefore = keysPresent();
        assertThat(keysBefore).as("non-vacuity: the keys the move drops are there to be dropped").isPositive();
        try (InFlightIndexBuild build = InFlightIndexBuild.start(this, "row23-pre", BUILDER, "row23_pre_idx")) {
            assertThat(sessionsNamed(BUILDER)).as("the build is running").isEqualTo(1);
            assertThat(PciBuilders.countBuilding(ctx(), LOG)).as("non-vacuity: the detector sees one CREATE INDEX").isEqualTo(1);

            var result = finish(NameKeyDropMove.LABEL, SESSION_TABLE, List.of());

            assertDeferral(result, BEFORE_LOCKS);
            assertNothingWasWritten(NameKeyDropMove.LABEL);
            assertThat(keysPresent()).as("on_complete did not run: no key was dropped").isEqualTo(keysBefore);
            assertThat(sessionsNamed(BUILDER)).as("the build was not ended").isEqualTo(1);
        }   // close: the blocker lets go and the build finishes; a build that had been ended would throw here
        awaitNoSessions(BUILDER);
    }

    // ── a build that begins after the first read: the same deferral, from the lock wait ──────────────────

    @Test
    @Order(2)
    void aBuildThatBeginsAfterTheFirstRead_isAlsoADeferral_theLockWaitTimesOutAndNothingIsEnded() throws Exception {
        registerTheDropMove();
        long keysBefore = keysPresent();
        InFlightIndexBuild[] late = new InFlightIndexBuild[1];
        VerifyingFinisher finisher = new VerifyingFinisher();
        // the seam runs in the completion transaction after the first read and before on_complete: the build begins in that gap
        finisher.recheckHook = tx -> {
            try {
                late[0] = InFlightIndexBuild.start(this, "row23-late", BUILDER + "-late", "row23_late_idx");
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        };
        try {
            var result = finish(finisher, NameKeyDropMove.LABEL, SESSION_TABLE, List.of());

            assertThat(late[0]).as("non-vacuity: the build began inside the completion transaction").isNotNull();
            assertDeferral(result, LOCK_WAIT);
            assertNothingWasWritten(NameKeyDropMove.LABEL);
            assertThat(keysPresent()).isEqualTo(keysBefore);
            assertThat(sessionsNamed(BUILDER + "-late")).as("the late build was not ended").isEqualTo(1);
        } finally {
            if (late[0] != null) {
                late[0].close();
            }
        }
        awaitNoSessions(BUILDER + "-late");
    }

    // ── an unreadable detector is not "a build is there" ─────────────────────────────────────────────────

    @Test
    @Order(3)
    void anUnreadableDetectorBeforeTheTransaction_isAnOrdinaryCountedTransient_notADeferral_andNothingRuns() {
        registerTheDropMove();
        long keysBefore = keysPresent();
        VerifyingFinisher finisher = new VerifyingFinisher();
        finisher.buildWait.detector = ctx -> PciBuilders.Building.unreadable();

        var result = finish(finisher, NameKeyDropMove.LABEL, SESSION_TABLE, List.of());

        assertThat(result.outcome()).as("handed to the failure handler, which counts it").isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().raised() == null).as("nothing raised: a transient by the classifier's rule").isTrue();
        assertThat(DataMoveFailureClassifier.classify(result.failure().stage(), null, List.of(), result.failure().detail()).permanent())
            .as("a read that fails is retried, as a transient").isFalse();
        assertThat(result.failure().detail()).contains("could not be read");
        assertThat(keysPresent()).as("the drop did not run blind").isEqualTo(keysBefore);
        assertThat(completedAt(NameKeyDropMove.LABEL)).isNull();
    }

    @Test
    @Order(4)
    void anUnreadableDetectorAfterTheLockTimeout_keepsTheFailureItWas_itIsNotADeferral() throws Exception {
        registerTheDropMove();
        VerifyingFinisher finisher = new VerifyingFinisher();
        AtomicInteger reads = new AtomicInteger();
        // the first read (before the transaction) finds nothing; the read after the lock timeout cannot be made
        finisher.buildWait.detector = ctx -> reads.getAndIncrement() == 0
            ? new PciBuilders.Building(0, List.of()) : PciBuilders.Building.unreadable();
        try (Connection holder = builderHoldingChunks(BUILDER + "-holder")) {
            var result = finish(finisher, NameKeyDropMove.LABEL, SESSION_TABLE, List.of());

            assertThat(reads.get()).as("non-vacuity: the detector was read twice, the second time unreadable").isEqualTo(2);
            assertOnCompleteLockTimeout(result);
            holder.rollback();
        }
        awaitNoSessions(BUILDER + "-holder");
    }

    // ── a 55P03 with no build running stays a counted failure ────────────────────────────────────────────

    @Test
    @Order(5)
    void aLockTimeoutWithABuilderSessionThatIsNotBuilding_staysAnOrdinaryFailure_notADeferral() throws Exception {
        registerTheDropMove();
        long keysBefore = keysPresent();
        try (Connection holder = builderHoldingChunks(BUILDER + "-holder")) {
            assertThat(PciBuilders.count(ctx(), LOG)).as("non-vacuity: the wide count sees the session").isEqualTo(1);
            assertThat(PciBuilders.countBuilding(ctx(), LOG)).as("and the narrow one does not: it is no CREATE INDEX").isZero();

            var result = finish(NameKeyDropMove.LABEL, SESSION_TABLE, List.of());

            assertOnCompleteLockTimeout(result);
            assertThat(keysPresent()).isEqualTo(keysBefore);
            holder.rollback();
        }
        awaitNoSessions(BUILDER + "-holder");
    }

    private static void assertOnCompleteLockTimeout(MoveFinisher.Result result) {
        assertThat(result.outcome()).as("not a deferral: the failure handler counts it").isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.ON_COMPLETE);
        assertThat(result.failure().raised().getSQLState()).isEqualTo("55P03");
    }

    // ── (b) no build in flight: the drop completes, an idle builder session notwithstanding ──────────────

    @Test
    @Order(6)
    void noBuildInFlight_theDropCompletes_andTheKeysAreDropped_evenWithAnIdleBuilderSession() throws Exception {
        registerTheDropMove();
        try (Connection idle = idleBuilderSession(BUILDER + "-idle")) {
            assertThat(PciBuilders.count(ctx(), LOG)).as("non-vacuity: a builder-named session exists (the wide count sees it)").isEqualTo(1);
            assertThat(PciBuilders.countBuilding(ctx(), LOG)).as("it is building nothing").isZero();

            var finisher = new VerifyingFinisher();
            var result = finish(finisher, NameKeyDropMove.LABEL, SESSION_TABLE, List.of());

            assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
            assertThat(completedAt(NameKeyDropMove.LABEL)).isNotNull();
            assertThat(keysPresent()).as("every name key is gone").isZero();
            assertThat(finisher.buildWait.age(NameKeyDropMove.LABEL)).as("no deferral run is left open").isNull();
        }
        awaitNoSessions(BUILDER + "-idle");
    }

    private Connection idleBuilderSession(String applicationName) throws Exception {
        var props = new Properties();
        props.setProperty("user", ADMIN);
        props.setProperty("password", ADMIN_PASSWORD);
        props.setProperty("ApplicationName", applicationName);
        Connection c = DriverManager.getConnection(pg.getJdbcUrl(), props);
        DSL.using(c, SQLDialect.POSTGRES).select(DSL.one()).fetch();
        return c;
    }
}
