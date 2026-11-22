// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.Field;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * nexus-f3yqd.2.14 (RDR-246 Step 4, The move worker step 3, Writers during a move) on the pg_background image: the
 * verification half of the finish. Per-tenant checks under that tenant's setting, the table-wide check on a separate
 * connection that carries none, both READ ONLY and bounded; a non-empty one reports every tenant's count and leaves
 * {@code completed_at} NULL. The {@code on_complete} half is {@link DataMoveOnCompleteIntegrationTest}.
 *
 * <p>The worker runs the passes. Where a scenario needs the runs done and then a different state (an old-shape row put
 * back, a tenant dropped from the list), a worker with {@link MoveFinisher#RUNS_ONLY} does the runs and the finisher is
 * called on its own, on a connection opened the way the worker opens it.
 */
class DataMoveVerificationIntegrationTest extends DataMoveIntegrationBase {

    private static final Field<Long> ID = DSL.field(DSL.name("id"), Long.class);
    private static final Field<String> PAYLOAD = DSL.field(DSL.name("payload"), String.class);

    /** What the failure-class bead will do with these stages; here, so a non-empty verification reads {@code failed:}. */
    private static FailureHandler handler(List<FailureHandler.Failure> seen) {
        return f -> {
            seen.add(f);
            return f.stage() == FailureHandler.Stage.VERIFICATION_NONEMPTY
                ? FailureHandler.Disposition.failed(f.detail())
                : FailureHandler.Disposition.blocked(f.detail());
        };
    }

    private void insertOldShapeRow(String tenant, long id) {
        ctx().insertInto(table("dm_test", "tenant_rows"))
            .columns(TENANT_ID, ID, PAYLOAD)
            .values(tenant, id, "old-" + id)
            .execute();
    }

    // ── a verified move completes ────────────────────────────────────────────────────────────────────────

    @Test
    void everyRunCompleteAndEveryVerificationEmpty_completesTheMove_andLeavesNoVerificationConnection() throws Exception {
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();
        register("v-ok", "tenant_rows", 50, 0);
        var w = worker(installed(), null, handler(seen), new VerifyingFinisher());

        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(completedAt("v-ok")).isNotNull();
        assertThat(seen).isEmpty();
        assertThat(w.states().state("v-ok")).as("a completed move is no longer reported").isEmpty();
        assertEveryRowMovedOnce("tenant_rows", 1000);
        awaitNoSessions(WORKER_NAME + "-verify");
        awaitNoSessions(WORKER_NAME + "-verify-all");
        assertThat(w.runCycle()).as("a completed move is not worked again").isZero();
    }

    @Test
    void aMoveOverALeafedTable_isVerifiedThroughItsParent_perTenantAndTableWide() {
        register("v-leafed", "leafed", 50, 0, null, "stamp-template.sql", "verification-leafed.sql", 60_000, null);
        var w = worker(installed(), null, handler(new CopyOnWriteArrayList<>()), new VerifyingFinisher());

        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(completedAt("v-leafed")).isNotNull();
        assertEveryRowMovedOnce("leafed", 900);
        assertThat(partitionWiseRuns("v-leafed")).isZero();
    }

    // ── writers during a move ────────────────────────────────────────────────────────────────────────────

    @Test
    void writersDuringAMove_leaveEverySurvivingRowInTheNewShape_andNewRowsTheirValues() throws Exception {
        register("v-writers", "tenant_rows", 20, 30, null, "idempotent-template.sql", "verification.sql", 60_000, null);
        var w = worker(installed(), null, handler(new CopyOnWriteArrayList<>()), new VerifyingFinisher());
        var stop = new AtomicBoolean();
        var inserted = new AtomicInteger();
        var deleted = new AtomicInteger();
        var updated = new AtomicInteger();
        Connection writerConnection = pg.createConnection("");
        var writer = async(() -> {
            var db = DSL.using(writerConnection, org.jooq.SQLDialect.POSTGRES);
            var rows = table("dm_test", "tenant_rows");
            int i = 0;
            while (!stop.get()) {
                // a new-shape row (n = 1), the shape writers produce from the registering release on
                db.insertInto(rows).columns(TENANT_ID, ID, PAYLOAD, N)
                    .values("acme", 1000L + i, "new-" + i, 1).execute();
                inserted.incrementAndGet();
                // an update of a row of either shape: whether the move has reached it depends on the moment
                db.update(rows).set(PAYLOAD, "updated-" + i)
                    .where(TENANT_ID.eq("globex")).and(ID.eq((long) (i % 200) + 1)).execute();
                updated.incrementAndGet();
                // a delete of a row of either shape, one tenant's first rows
                if (i < 40) {
                    db.deleteFrom(rows).where(TENANT_ID.eq("orphan")).and(ID.eq((long) i + 1)).execute();
                    deleted.incrementAndGet();
                }
                i++;
                Thread.sleep(15);
            }
            return i;
        });

        int worked;
        try {
            worked = w.runCycle();
        } finally {
            stop.set(true);
            writer.get();
            writerConnection.close();
        }

        assertThat(worked).isEqualTo(1);
        assertThat(inserted.get()).as("the writer ran while the move did").isGreaterThan(10);
        assertThat(completedAt("v-writers")).as("the verification was empty, so the move completed").isNotNull();
        assertThat(countWhere("tenant_rows", N.eq(0))).as("no old-shape row survives").isZero();
        assertThat(countWhere("tenant_rows", ID.ge(1000L).and(PAYLOAD.like("new-%")).and(N.eq(1))))
            .as("new rows keep their values").isEqualTo(inserted.get());
        assertThat(countWhere("tenant_rows", ID.ge(1000L).and(TOUCHED_BY.isNotNull())))
            .as("the template leaves a new-shape row unchanged").isZero();
        assertThat(countWhere("tenant_rows", TENANT_ID.eq("orphan").and(ID.le((long) deleted.get()))))
            .as("deleted rows stay deleted").isZero();
        assertThat(countWhere("tenant_rows", PAYLOAD.like("updated-%"))).as("updates of both shapes landed").isPositive();
    }

    @Test
    void aWriterThatPutsAnOldShapeRowInAChunkAlreadyDone_failsTheMove_withTheCountPerTenant_andNoCompletion() {
        register("v-late", "tenant_rows", 50, 0);
        runsOnly("v-late");
        insertOldShapeRow("acme", 5000);
        insertOldShapeRow("globex", 5001);
        insertOldShapeRow("globex", 5002);
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();
        var w = worker(installed(), null, handler(seen), new VerifyingFinisher());

        assertThat(w.runCycle()).isEqualTo(1);

        var state = w.states().state("v-late").orElseThrow();
        assertThat(state.kind()).isEqualTo(MoveStates.Kind.FAILED);
        // each tenant counts only its own rows, though the admin role reads every tenant's (the owner-read policy)
        assertThat(state.reason()).contains("tenant acme=1").contains("tenant globex=2").contains("table-wide=3")
            .doesNotContain("tenant default").doesNotContain("tenant orphan");
        assertThat(completedAt("v-late")).as("completed_at stays NULL").isNull();
        assertThat(seen).extracting(FailureHandler.Failure::stage).containsExactly(FailureHandler.Stage.VERIFICATION_NONEMPTY);
        assertThat((Throwable) seen.get(0).raised()).isNull();
        assertThat(seen.get(0).admin()).isNotNull();
    }

    // ── a tenant the runs missed ─────────────────────────────────────────────────────────────────────────

    @Test
    void aTenantLeftOutOfThePerTenantRuns_failsTheMoveAtTheTableWideVerification() {
        register("v-left-out", "tenant_rows", 50, 0);
        runsOnly("v-left-out");
        insertOldShapeRow("orphan", 5000);
        List<String> all = List.of("default", "acme", "globex", "orphan", "sl/ash");
        List<String> withoutOrphan = List.of("default", "acme", "globex", "sl/ash");

        var missed = finish("v-left-out", "tenant_rows", withoutOrphan);
        var listed = finish("v-left-out", "tenant_rows", all);

        assertThat(missed.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(missed.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION_NONEMPTY);
        assertThat(missed.reason()).as("only the table-wide run sees it").contains("table-wide=1")
            .doesNotContain("tenant ");
        assertThat(listed.reason()).as("control: listed, the tenant's own run counts it too").contains("tenant orphan=1")
            .contains("table-wide=1");
        assertThat(completedAt("v-left-out")).isNull();
    }

    // ── the tenant on each connection ────────────────────────────────────────────────────────────────────

    @Test
    void eachPerTenantRunCarriesItsTenant_theTableWideRunCarriesNone_andTheConnectionIsCleanAfterReset() throws Exception {
        register("v-setting", "tenant_rows", 50, 0, null, "stamp-template.sql", "verification-tenant-setting.sql", 60_000, null);
        runsOnly("v-setting");
        String probe = DataMoveTestFixtures.resource("verification-tenant-setting.sql");

        var result = finish("v-setting", "tenant_rows", List.of("default", "acme", "globex", "orphan", "sl/ash"));
        assertThat(result.outcome()).as("the probe finds a tenant on no run").isEqualTo(MoveFinisher.Outcome.COMPLETED);

        // controls: the probe does return a row when a run carries the wrong setting
        try (var conn = CallConnection.open(adminConnection, WORKER_NAME + "-control")) {
            assertThat(conn.tenantSetting()).as("a fresh admin connection carries none").isNullOrEmpty();
            conn.useTenant("acme");
            assertThat(MoveSql.countRows(conn.connection(), probe, null, 10))
                .as("table-wide run on a connection that carries a tenant").isEqualTo(1);
            assertThat(MoveSql.countRows(conn.connection(), probe, "globex", 10))
                .as("per-tenant run on another tenant's setting").isEqualTo(1);
            assertThat(MoveSql.countRows(conn.connection(), probe, "acme", 10)).as("its own setting").isZero();
            conn.clearTenant();
            assertThat(conn.tenantSetting()).as("none after RESET").isNullOrEmpty();
            assertThat(MoveSql.countRows(conn.connection(), probe, null, 10)).isZero();
        }
    }

    @Test
    void aTableWideConnectionThatCarriesATenant_isRefused_notReadAsAnEmptyTable() throws Exception {
        register("v-role", "tenant_rows", 50, 0);
        runsOnly("v-role");
        insertOldShapeRow("globex", 5000);

        DataMoveTestFixtures.setAdminTenant(su);
        MoveFinisher.Result result;
        try {
            result = finish("v-role", "tenant_rows", List.of());
        } finally {
            DataMoveTestFixtures.resetAdminTenant(su);
        }

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION);
        assertThat(result.reason()).contains("carries nexus.tenant=acme");
        assertThat(completedAt("v-role")).isNull();
    }

    // ── read only, and bounded ───────────────────────────────────────────────────────────────────────────

    @Test
    void aVerificationThatWrites_isRefusedByItsReadOnlyTransaction_andChangesNothing() {
        register("v-writes", "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-writes.sql", 60_000, null);
        runsOnly("v-writes");

        var result = finish("v-writes", "tenant_plain", List.of());

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION);
        assertThat((Throwable) result.failure().raised()).isNotNull();
        assertThat(result.failure().raised().getSQLState()).as("read_only_sql_transaction").isEqualTo("25006");
        assertEveryRowMovedOnce("tenant_plain", 150);
        assertThat(completedAt("v-writes")).isNull();
    }

    @Test
    void aVerificationThatOutlastsStatementTimeoutMs_isReportedAsTheTimeout_andTheMoveIsNotCompleted() throws Exception {
        register("v-slow", "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-sleep.sql", 1_000, null);
        runsOnly("v-slow");

        long started = System.nanoTime();
        var result = finish("v-slow", "tenant_plain", List.of());
        long elapsedSeconds = (System.nanoTime() - started) / 1_000_000_000L;

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION);
        assertThat(result.failure().raised().getSQLState()).as("query_canceled: statement_timeout").isEqualTo("57014");
        assertThat(elapsedSeconds).as("the bound, not the 30 s sleep").isLessThan(15);
        assertThat(completedAt("v-slow")).isNull();
        awaitNoSessions(WORKER_NAME + "-verify-all");   // the connection is closed
    }

    // ── a verification text the worker will not run ─────────────────────────────────────────────────────

    @Test
    void aVerificationWithAQuestionMark_isRefusedBeforeItRuns_asAVerificationFailureNamingTheRule() throws Exception {
        register("v-qmark", "tenant_rows", 50, 0, null, "stamp-template.sql", "verification-question-mark.sql", 60_000, null);
        runsOnly("v-qmark");

        var result = finish("v-qmark", "tenant_rows", List.of("acme"));

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION_REFUSED);
        assertThat((Throwable) result.failure().raised()).isNull();
        assertThat(result.reason()).contains("contains a '?'").contains("was not run");
        assertThat(completedAt("v-qmark")).isNull();
        awaitNoSessions(WORKER_NAME + "-verify");
    }

    @Test
    void aDollarOneInsideAStringLiteral_isRefusedByTheDriversParameterCount_notRewrittenIntoTheLiteral() {
        register("v-literal", "tenant_rows", 50, 0, null, "stamp-template.sql", "verification-dollar-literal.sql", 60_000, null);
        runsOnly("v-literal");

        var result = finish("v-literal", "tenant_rows", List.of("acme"));

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION_REFUSED);
        assertThat((Throwable) result.failure().raised()).isNull();
        assertThat(result.reason()).contains("has 3 $1 but the driver sees 2 bind parameters");
        assertThat(completedAt("v-literal")).isNull();
    }

    @Test
    void aValidVerification_bindsTheTenantToEveryOccurrenceThroughTheDriver() throws Exception {
        register("v-bind", "tenant_rows", 50, 0);
        runsOnly("v-bind");
        insertOldShapeRow("acme", 5000);
        String text = DataMoveTestFixtures.resource("verification.sql");

        try (var conn = CallConnection.open(adminConnection, WORKER_NAME + "-bind")) {
            assertThat(MoveSql.countRows(conn.connection(), text, "acme", 10)).as("its own row").isEqualTo(1);
            assertThat(MoveSql.countRows(conn.connection(), text, "globex", 10)).as("another tenant's: none").isZero();
            assertThat(MoveSql.countRows(conn.connection(), text, null, 10)).as("null binds the OR $1 IS NULL half").isEqualTo(1);
        }
    }

    // ── a verification connection that cannot be opened ──────────────────────────────────────────────────

    @Test
    void aVerificationConnectionThatCannotBeOpened_isAConnectionFailure_andTransientWhateverItsSqlstate() {
        register("v-noconn", "tenant_rows", 50, 0);
        runsOnly("v-noconn");
        DataMove move = DataMoveRegistry.reload(ctx(), "v-noconn").orElseThrow();

        MoveFinisher.Result result;
        try (var session = adminSession("tenant_rows")) {
            // a role that does not exist: a login failure (class 28), which no SQLSTATE list calls transient, trust auth or not
            var badLogin = new dev.nexus.service.db.AdminConnection(pg.getJdbcUrl(), "no_such_admin_role", ADMIN_PASSWORD);
            result = new VerifyingFinisher().finish(
                new MoveFinisher.Context(move, List.of("acme"), session.ctx(), badLogin, WORKER_NAME, () -> false));
        }

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        var failure = result.failure();
        assertThat(failure.stage()).isEqualTo(FailureHandler.Stage.CONNECTION);
        assertThat(failure.raised().getSQLState()).as("the login failure the finisher saw").startsWith("28");
        assertThat(DataMoveFailureClassifier.classify(failure.stage(), failure.raised(), List.of(), failure.detail())
            .permanent()).as("an open failure is the environment's, so transient").isFalse();
        assertThat(completedAt("v-noconn")).isNull();
    }

    // ── a stop ───────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aStopBeforeTheFinish_startsNothing() throws Exception {
        register("v-stop0", "tenant_rows", 50, 0);
        runsOnly("v-stop0");

        var result = finish(new VerifyingFinisher(), "v-stop0", "tenant_rows", List.of("acme", "globex"), () -> true);

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.LEFT_OPEN);
        assertThat(completedAt("v-stop0")).isNull();
        assertThat(sessionsNamed(WORKER_NAME + "-verify")).isZero();
    }

    @Test
    void aStopBetweenTenants_endsTheFinishThere_andCompletesNothing() {
        register("v-stop1", "tenant_rows", 50, 0);
        runsOnly("v-stop1");
        var calls = new AtomicInteger();
        var verified = new AtomicInteger();
        var finisher = new VerifyingFinisher();
        finisher.afterResetHook = conn -> verified.incrementAndGet();
        // reads: the finish, the tenant pass, the first tenant (false); the second tenant (true)
        var result = finish(finisher, "v-stop1", "tenant_rows", List.of("default", "acme", "globex", "orphan", "sl/ash"),
            () -> calls.incrementAndGet() >= 4);

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.LEFT_OPEN);
        assertThat(verified.get()).as("one tenant was verified before the stop").isEqualTo(1);
        assertThat(completedAt("v-stop1")).isNull();
    }

    @Test
    void aStopAfterTheVerification_endsTheFinishBeforeTheCompletionTransaction() {
        register("v-stop2", "tenant_rows", 50, 0);
        runsOnly("v-stop2");
        var stopped = new AtomicBoolean();
        var finisher = new VerifyingFinisher();
        finisher.beforeCompletionHook = () -> stopped.set(true);

        var result = finish(finisher, "v-stop2", "tenant_rows", List.of("acme"), stopped::get);

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.LEFT_OPEN);
        assertThat(completedAt("v-stop2")).as("the verification was empty and the move would have completed").isNull();
    }

    // ── the connection between tenants ───────────────────────────────────────────────────────────────────

    @Test
    void theVerificationConnectionCarriesNoTenant_afterEachReset() {
        register("v-reset", "tenant_rows", 50, 0);
        runsOnly("v-reset");
        var carried = new CopyOnWriteArrayList<String>();
        var finisher = new VerifyingFinisher();
        finisher.afterResetHook = conn -> carried.add(String.valueOf(conn.tenantSetting()));

        var result = finish(finisher, "v-reset", "tenant_rows", List.of("default", "acme", "globex"));

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(carried).as("one reading per tenant, each of an empty setting").hasSize(3)
            .allSatisfy(setting -> assertThat(setting).isIn("", "null"));
    }

    // ── a pause ──────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aPauseSetBeforeTheVerification_stopsTheFinish_beforeAnyVerificationRuns() {
        register("v-pause", "tenant_rows", 50, 0);
        runsOnly("v-pause");
        DataMove listed = DataMoveRegistry.reload(ctx(), "v-pause").orElseThrow();
        DataMoveTestFixtures.pause(ctx(), "v-pause", true);

        MoveFinisher.Result result;
        try (var session = adminSession("tenant_rows")) {
            result = new VerifyingFinisher().finish(
                new MoveFinisher.Context(listed, List.of("acme"), session.ctx(), adminConnection, WORKER_NAME));
        }

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.PAUSED);
        assertThat(result.move().paused()).isTrue();
        assertThat(completedAt("v-pause")).isNull();
    }
}
