// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgCatalogProbes;
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

/**
 * RDR-229 P3.7 row 23 (nexus-p2m1x.27), Sam 2026-11-23 about 23:50 PST (T2
 * {@code nexus/sam-ruling-rdr229-drop-move-waits-for-id-builds-2026-11-24}, "Fix: wait, don't end"): the REAL name-key drop move
 * ({@link NameKeyDropMove}, registered by its own {@code registerIfAbsent}) through the REAL {@link VerifyingFinisher} on the product schema.
 * A completion attempt that finds an index build in flight defers: it reports a not-now, writes no failure and no retry count, leaves the
 * move open and ends nothing, so the build runs to completion. With no build in flight the drop completes and the keys are gone.
 *
 * <p>An in-flight build is simulated the way {@code DataMoveOnCompleteIntegrationTest} does: a session named under
 * {@code nexus-pci-builder-} holding ROW SHARE on {@code nexus.chunks}, which is what {@code PciBuilders} finds and what the drop's ALTER
 * queues behind. The tests run in order because the completing test drops the keys of the container the class shares (the deferring
 * tests leave them where they are, and say so by comparing the count before and after).
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class NameKeyDropMoveWaitsForBuildsIntegrationTest extends DataMoveIntegrationBase {

    private static final String VERSION = "9.9.9-row23";
    private static final String BUILDER = "nexus-pci-builder-row23";
    /** The start of the reason a deferral carries (the finisher's own constant is not used: the test pins the words). */
    private static final String WAITING = "waiting for in-flight index builds";
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

    /** The build is not ended: its session answers, then it ends its own transaction the way a finished build does. */
    private static void assertTheBuildRunsToCompletion(Connection builder) throws Exception {
        assertThat(builder.isValid(2)).as("the build's session is alive: nothing ended it").isTrue();
        DSL.using(builder, SQLDialect.POSTGRES).select(DSL.one()).from(table("nexus", "chunks")).forShare().fetch();   // still usable: not terminated
        builder.commit();
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

    // ── (a) a build in flight: a deferral ────────────────────────────────────────────────────────────────

    @Test
    @Order(1)
    void aBuildInFlight_defersTheCompletion_noFailureNoCount_theMoveStaysOpen_andTheBuildIsNotEnded() throws Exception {
        registerTheDropMove();
        long keysBefore = keysPresent();
        assertThat(keysBefore).as("non-vacuity: the keys the move drops are there to be dropped").isPositive();
        try (Connection builder = builderHoldingChunks(BUILDER)) {
            assertThat(sessionsNamed(BUILDER)).as("the build is running, holding a lock on nexus.chunks").isEqualTo(1);

            var result = finish(NameKeyDropMove.LABEL, SESSION_TABLE, List.of());

            assertThat(result.outcome()).as("a deferral, not a completion and not a failure").isEqualTo(MoveFinisher.Outcome.BLOCKED);
            assertThat(result.failure()).as("nothing for the failure handler: no count, no page").isNull();
            assertThat(result.reason()).startsWith(WAITING).contains("1 running");
            assertNothingWasWritten(NameKeyDropMove.LABEL);
            assertThat(keysPresent()).as("on_complete did not run: no key was dropped").isEqualTo(keysBefore);
            assertThat(sessionsNamed(BUILDER)).as("the build was not ended").isEqualTo(1);
            assertTheBuildRunsToCompletion(builder);
        }
        awaitNoSessions(BUILDER);
    }

    // ── a build that begins after the check: the same deferral, from the lock wait ───────────────────────

    @Test
    @Order(2)
    void aBuildThatBeginsAfterTheCheck_isAlsoADeferral_theLockWaitTimesOutAndNothingIsEnded() throws Exception {
        registerTheDropMove();
        long keysBefore = keysPresent();
        Connection[] late = new Connection[1];
        VerifyingFinisher finisher = new VerifyingFinisher();
        // the seam runs in the completion transaction after the check and before on_complete: the build begins in that gap
        finisher.recheckHook = tx -> {
            try {
                late[0] = builderHoldingChunks(BUILDER + "-late");
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        };
        try {
            var result = finish(finisher, NameKeyDropMove.LABEL, SESSION_TABLE, List.of());

            assertThat(late[0]).as("non-vacuity: the build began inside the completion transaction").isNotNull();
            assertThat(result.outcome()).as("on_complete's lock wait timed out behind the build: a deferral, not a failure")
                .isEqualTo(MoveFinisher.Outcome.BLOCKED);
            assertThat(result.failure()).isNull();
            assertThat(result.reason()).startsWith(WAITING);
            assertNothingWasWritten(NameKeyDropMove.LABEL);
            assertThat(keysPresent()).isEqualTo(keysBefore);
            assertThat(sessionsNamed(BUILDER + "-late")).as("the late build was not ended").isEqualTo(1);
            assertTheBuildRunsToCompletion(late[0]);
        } finally {
            if (late[0] != null) {
                late[0].close();
            }
        }
        awaitNoSessions(BUILDER + "-late");
    }

    // ── (b) no build in flight: the drop completes ───────────────────────────────────────────────────────

    @Test
    @Order(3)
    void noBuildInFlight_theDropCompletes_andTheKeysAreDropped() {
        registerTheDropMove();
        assertThat(sessionsNamed(BUILDER)).as("non-vacuity: no build runs").isZero();

        var result = finish(NameKeyDropMove.LABEL, SESSION_TABLE, List.of());

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(completedAt(NameKeyDropMove.LABEL)).isNotNull();
        assertThat(keysPresent()).as("every name key is gone").isZero();
    }
}
