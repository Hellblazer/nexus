// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.PciBuilders;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.function.Consumer;

/**
 * What follows the runs (RDR-246, The move worker step 3; nexus-f3yqd.2.14). With every tenant's run complete:
 *
 * <ol>
 *   <li>the move's row is read again, and a pause set meanwhile stops the finish here;</li>
 *   <li>the verification query runs once per listed tenant on a dedicated connection that carries that tenant's setting,
 *       then once table-wide on a separate connection after a check that it carries no {@code nexus.tenant}; each in a
 *       READ ONLY transaction (ACCESS SHARE only) under {@code verify_statement_timeout_ms}, a bound of its own that is
 *       longer than the completion's because this pass is also the warm-up (a plain scan of what the completion reads
 *       again), and every one is run so a failure names every tenant that still holds old-shape rows;</li>
 *   <li>if every run returned no rows, the row is read once more (a pause set now still stops it), and the completion
 *       transaction runs: the UPDATE that sets {@code completed_at}, then the verification again, warm now, table-wide and once per
 *       listed tenant, plus the tenants the catalogs name now (under {@code SET LOCAL nexus.tenant}; a verification that reads a row-security-scoped table is empty
 *       without a tenant, so the tenants' runs are the check), all sharing {@code statement_timeout_ms}, then {@code on_complete} (if any). A write between the two transactions that leaves an
 *       old-shape row is caught by that second run, which rolls the transaction back, so neither {@code completed_at} nor
 *       {@code on_complete} takes effect without the other, or past a non-empty verification.</li>
 * </ol>
 *
 * <p>The UPDATE goes first inside that transaction and carries {@code paused_at IS NULL} and no row that supersedes the
 * move, so it takes the row's lock and rechecks both atomically with the commit; when it matches no row the transaction
 * ends having run nothing. Only after it matched, on a move over the chunk family ({@link ChunkFamily}), are the
 * per-collection index builders ended (best effort, {@link PciBuilders}), so a pause set after the last read ends no build.
 * {@code on_complete} runs under {@code SET LOCAL lock_timeout} below the move's {@code statement_timeout_ms} on every
 * move, so a lock it cannot get fails fast with 55P03 and no reader queues behind it.
 *
 * <p><b>A move with no statement bound</b> ({@link DataMove#unbounded}: a validation that blocks no writer, Sam 2026-11-23) does not run
 * {@code on_complete} in the transaction above. {@link #validateBeforeClaim} runs it first, in a transaction of its own under
 * {@code lock_timeout} 5 s and {@code statement_timeout} 0 (so no network timeout), holding no row of {@code nexus.data_moves} and no other
 * move's state; its backend pid and start go to the reporter (status), a stop cancels the statement ({@link #cancelInFlight}), and a boot ends
 * the backend if the engine died ({@link DataMoveOrphans}). Only then does the claim transaction run: the move's re-check (a registry read for a
 * leaf validation, which also reads its leaf key and refuses to complete unless it is validated; for the other moves the move's own verification,
 * a scan per tenant under the row lock) and the UPDATE of {@code completed_at}, with no {@code on_complete}. {@code on_complete} is idempotent for these moves (it validates what is not validated yet), so a
 * restart between the two finds nothing left to do and claims. A move whose {@code on_complete} puts itself back
 * ({@code completed_at} NULL again, the adoption move) ends as {@link MoveFinisher.Outcome#REOPENED}: no completed event, no listener, no retry reset.
 *
 * <p>A stop ({@link Context#stopped}) is read before the finish, between tenants, before the table-wide run and before the
 * completion transaction, and ends the finish as {@link Outcome#LEFT_OPEN}; a verification already in flight is not
 * cancelled, it is bounded by {@code verify_statement_timeout_ms}.
 *
 * <p>Nothing here decides blocked or failed: a failure goes back as a {@link FailureHandler.Failure} ({@link
 * FailureHandler.Stage#VERIFICATION}, {@link FailureHandler.Stage#VERIFICATION_REFUSED}, {@link
 * FailureHandler.Stage#VERIFICATION_NONEMPTY}, {@link FailureHandler.Stage#RECHECK}, {@link FailureHandler.Stage#ON_COMPLETE}) for the worker to hand to the {@link FailureHandler}.
 * A first pass that timed out or failed is a {@code VERIFICATION} failure (transient on 57014, the existing retry and backoff),
 * never a completion; the re-check in the completion transaction reports a row as the first pass does ({@code VERIFICATION_NONEMPTY},
 * naming each tenant) and any error or timeout as {@code RECHECK}, which is classified like {@code ON_COMPLETE}. A verification text the worker
 * refuses to run ({@link MoveSql#refusal}) is a {@code VERIFICATION_REFUSED} failure with {@code raised} null and the rule in
 * {@code detail}.
 */
final class VerifyingFinisher implements MoveFinisher {

    private static final Logger log = LoggerFactory.getLogger(VerifyingFinisher.class);

    /** A run reads at most this many old-shape rows before it reports "more than" (a count, not the table). */
    static final int COUNT_CAP = 10_000;

    /** The longest {@code lock_timeout} of {@code on_complete}, in milliseconds; a move with a shorter statement bound gets half of it. */
    static final int LOCK_TIMEOUT_CEILING_MS = 5_000;

    /** The re-check's deadline of a move with no statement bound ({@link DataMove#unbounded}): none, each run is set to statement_timeout 0. */
    private static final long NO_DEADLINE = Long.MAX_VALUE;

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));

    /** The {@code lock_timeout} of {@code on_complete}: half the statement bound, at most 5 s, at least 1 ms. */
    static int lockTimeoutMs(int statementTimeoutMs) {
        return Math.max(1, Math.min(LOCK_TIMEOUT_CEILING_MS, statementTimeoutMs / 2));
    }

    /** Test seam: runs after the second read of the row and before the completion transaction, where a pause can slip in. */
    Runnable beforeCompletionHook;

    /** Test seam: runs with the verification connection after each reset, so a test can read what the session carries. */
    Consumer<CallConnection> afterResetHook;

    /** Test seam: runs with the completion transaction's context at the start of the re-check, before its first check of the session. */
    Consumer<DSLContext> recheckHook;

    @Override
    public Result finish(Context context) {
        if (context.stopped().getAsBoolean()) {
            return Result.leftOpen();
        }
        DSLContext admin = context.admin();
        Optional<DataMove> current = DataMoveRegistry.reload(admin, context.move().label());
        if (current.isEmpty()) {
            return Result.gone();
        }
        DataMove move = current.get();
        if (move.paused()) {
            return Result.paused(move);
        }

        Optional<String> refused = MoveSql.refusal(move.verification());
        if (refused.isPresent()) {
            return Result.report(failure(admin, move, FailureHandler.Stage.VERIFICATION_REFUSED, null, null,
                "the verification of " + move.label() + " was not run: " + refused.get()));
        }
        Result verification = verify(context, move);
        if (verification != null) {
            return verification;
        }

        current = DataMoveRegistry.reload(admin, move.label());
        if (current.isEmpty()) {
            return Result.gone();
        }
        move = current.get();
        if (move.paused()) {
            return Result.paused(move);
        }
        Runnable hook = beforeCompletionHook;
        if (hook != null) {
            hook.run();
        }
        if (context.stopped().getAsBoolean()) {
            return Result.leftOpen();
        }
        return complete(admin, move, context);
    }

    // ── verification ───────────────────────────────────────────────────────────────────────────────────────

    /** @return null when every run returned no rows; otherwise the result that ends the finish */
    private Result verify(Context context, DataMove move) {
        Map<String, Long> found = new LinkedHashMap<>();
        for (int pass = 0; pass < 2; pass++) {
            boolean tenantPass = pass == 0;
            if (tenantPass && context.tenants().isEmpty()) {
                continue;
            }
            if (context.stopped().getAsBoolean()) {
                return Result.leftOpen();
            }
            CallConnection conn;
            try {
                conn = CallConnection.open(context.adminConnection(),
                    context.applicationName() + (tenantPass ? "-verify" : "-verify-all"));
            } catch (SQLException e) {
                // as for the CALL connection: the environment's, transient whatever the SQLSTATE
                return Result.report(failure(context.admin(), move, FailureHandler.Stage.CONNECTION, null, e,
                    "the verification connection could not be opened: sqlstate=" + e.getSQLState() + " "
                        + FailureHandler.oneLine(e.getMessage())));
            }
            try {
                Result ended = tenantPass ? verifyTenants(context, move, conn, found)
                    : verifyTableWide(context, move, conn, found);
                if (ended != null) {
                    return ended;
                }
            } finally {
                conn.close();
            }
        }
        if (found.isEmpty()) {
            return null;
        }
        StringBuilder detail = new StringBuilder("verification of ").append(move.label())
            .append(" found old-shape rows in ").append(move.drivingTableLabel()).append(": ");
        List<String> parts = new ArrayList<>();
        found.forEach((who, n) -> parts.add(who + "=" + (n > COUNT_CAP ? COUNT_CAP + "+" : n)));
        detail.append(String.join(", ", parts));
        MoveEvents.verificationFailed(move.label(), String.join(", ", parts));
        return Result.report(failure(context.admin(), move, FailureHandler.Stage.VERIFICATION_NONEMPTY, null, null,
            detail.toString()));
    }

    private Result verifyTenants(Context context, DataMove move, CallConnection conn, Map<String, Long> found) {
        for (String tenant : context.tenants()) {
            if (context.stopped().getAsBoolean()) {
                return Result.leftOpen();
            }
            try {
                conn.useTenant(tenant);
                long rows = readOnlyCount(conn, move, tenant);
                if (rows > 0) {
                    found.put("tenant " + tenant, rows);
                }
                conn.clearTenant();   // a connection that cannot be shown clean never runs another tenant's query
                Consumer<CallConnection> hook = afterResetHook;
                if (hook != null) {
                    hook.accept(conn);
                }
            } catch (SQLException | RuntimeException e) {
                return Result.report(failure(context.admin(), move, stageOf(e), tenant, sqlOf(e),
                    "the verification for tenant " + tenant + " could not run: " + describe(e)));
            }
        }
        return null;
    }

    private Result verifyTableWide(Context context, DataMove move, CallConnection conn, Map<String, Long> found) {
        try {
            String setting = conn.tenantSetting();
            if (setting != null && !setting.isEmpty()) {
                return Result.report(failure(context.admin(), move, FailureHandler.Stage.VERIFICATION, null, null,
                    "the table-wide verification connection carries nexus.tenant=" + setting
                        + ", so it would read one tenant's rows and report the table clean"));
            }
            long rows = readOnlyCount(conn, move, null);
            if (rows > 0) {
                found.put("table-wide", rows);
            }
            return null;
        } catch (SQLException | RuntimeException e) {
            return Result.report(failure(context.admin(), move, stageOf(e), null, sqlOf(e),
                "the table-wide verification could not run: " + describe(e)));
        }
    }

    /** The verification query in a READ ONLY transaction under {@code verify_statement_timeout_ms}; the transaction is always rolled back. */
    private static long readOnlyCount(CallConnection conn, DataMove move, String tenant) throws SQLException {
        Connection c = conn.connection();
        c.setAutoCommit(false);
        c.setReadOnly(true);
        try {
            PgSession.setLocal(conn.ctx(), "statement_timeout", Integer.toString(move.verifyStatementTimeoutMs()));
            return MoveSql.countRows(c, move.verification(), tenant, COUNT_CAP);
        } finally {
            try {
                c.rollback();
                c.setReadOnly(false);
                c.setAutoCommit(true);
                PgSession.restoreNetworkTimeout(c, 0);
            } catch (SQLException e) {
                conn.close();   // a connection that cannot leave the transaction is not used again
            }
        }
    }

    // ── completion ─────────────────────────────────────────────────────────────────────────────────────────

    private Result complete(DSLContext admin, DataMove move, Context context) {
        List<String> tenants = context.tenants();
        String onComplete = move.onComplete();
        if (move.unbounded() && onComplete != null) {
            // a validation with no statement bound runs in a transaction of its own BEFORE the claim (linda, 2026-11-23): the claim holds
            // the move's row lock and a snapshot, and must not hold them for as long as a VALIDATE can take. on_complete is idempotent
            // (it validates only what is not validated yet), so a restart after it and before the claim finds nothing left to do.
            Result failed = validateBeforeClaim(admin, move, context);
            if (failed != null) {
                return failed;
            }
        }
        // a move with no statement bound (a validation that blocks no writer) gets statement_timeout 0, so no network timeout either,
        // and the lock wait the ceiling gives any move
        int statementMs = move.unbounded() ? 0 : move.statementTimeoutMs();
        int lockMs = move.unbounded() ? DataMove.UNBOUNDED_LOCK_TIMEOUT_MS : lockTimeoutMs(statementMs);
        boolean completed;
        try {
            completed = admin.transactionResult(cfg -> {
                DSLContext tx = cfg.dsl();
                PgSession.setStatementAndLockBounds(tx, statementMs, lockMs);
                // the row's lock, and the pause and the supersede read under it, are this transaction's: a pause or a
                // superseding row committed after the last read matches no row here, and then nothing below runs
                var superseding = MOVES.as("s");
                int updated = tx.update(MOVES)
                    .set(DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE),
                        DSL.currentOffsetDateTime())
                    .where(DSL.field(DSL.name("label"), String.class).eq(move.label()))
                    .and(DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE).isNull())
                    .and(DSL.field(DSL.name("failed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE).isNull())
                    .and(DSL.field(DSL.name("paused_at"), SQLDataType.TIMESTAMPWITHTIMEZONE).isNull())
                    .and(DSL.notExists(DSL.selectOne().from(superseding)
                        .where(DSL.field(DSL.name("s", "supersedes"), String.class).eq(move.label()))))
                    .execute();
                if (updated == 0) {
                    return false;
                }
                // the verification again, warm now, under the completion's own bound: a write since the first pass cannot slip
                // past, and a non-empty or failed re-check rolls this transaction back with the UPDATE above
                tx.connection(c -> recheck(tx, c, move, tenants));
                if (move.unbounded() && isLeafValidation(move)) {
                    // the claim of a leaf validation reads its leaf key: missing or NOT VALID is 55P03, the claim does not complete and the move
                    // validates again (the adoption's reconcile is the backstop, not the guarantee; linda, 2026-11-23)
                    tx.connection(c -> MoveSql.runOnComplete(c, dev.nexus.service.vectors.Rdr229ChunksFk.leafClaimCheck(move.label())));
                }
                if (onComplete != null && !move.unbounded()) {
                    if (ChunkFamily.touches(move) && !isAdoption(move)) {   // the adoption ends builders itself, once it holds its lock and is about to add
                        // only now: the move is known to complete, so no pause or supersede ends a build for nothing
                        // best effort: a savepoint keeps a refused signal from aborting the transaction
                        try {
                            tx.transaction(nested -> PciBuilders.terminateOrThrow(nested.dsl(),
                                "reason=data_move_on_complete move=" + move.label(), log));
                        } catch (DataAccessException e) {
                            log.error("event=pci_builders_terminate_failed cause=\"{}\"", FailureHandler.oneLine(e.toString()));
                        }
                    }
                    tx.connection(c -> MoveSql.runOnComplete(c, onComplete));
                }
                return true;
            });
        } catch (RecheckFailed e) {
            return Result.report(failure(admin, move, e.stage, null, e.raised, e.detail));
        } catch (DataAccessException e) {
            SQLException raised = e.getCause(SQLException.class);
            if (isAdoption(move) && raised != null && "57014".equals(raised.getSQLState())) {
                // the parent ADD outgrew its tight bound (about 2000 leaves): a transient like any 57014 from on_complete (the move stays open, writes
                // proceed after each timeout, and the retry count reaches "needs an operator"), with the reason that says what to do
                return Result.report(failure(admin, move, FailureHandler.Stage.ON_COMPLETE, null, raised, adoptionTimeoutReason(admin, move)));
            }
            return Result.report(failure(admin, move, FailureHandler.Stage.ON_COMPLETE, null, raised,
                "on_complete of " + move.label() + " did not complete: "
                    + FailureHandler.oneLine(raised != null ? "sqlstate=" + raised.getSQLState() + " " + raised.getMessage()
                        : e.getMessage())));
        }
        if (!completed) {
            Optional<DataMove> now = DataMoveRegistry.reload(admin, move.label());
            if (now.isEmpty()) {
                return Result.gone();
            }
            return now.get().paused() ? Result.paused(now.get()) : Result.leftOpen();
        }
        if (onComplete != null && reopenedBy(admin, move.label())) {
            // the on_complete put the move back (the adoption move does while a leaf is not validated yet): not a completion, so no
            // event, no listener and no retry reset
            return Result.reopened();
        }
        MoveEvents.completed(move.label(), onComplete != null);
        return Result.completed();
    }

    /** True when the move's own on_complete set {@code completed_at} back to NULL in the transaction that completed it. */
    private static boolean reopenedBy(DSLContext admin, String label) {
        return admin.fetchExists(MOVES, DSL.field(DSL.name("label"), String.class).eq(label),
            DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE).isNull());
    }

    /** A completion in flight: its backend, whose identity (start and name) a cancel checks again, and the admin values to reach it with. */
    private record InFlight(dev.nexus.service.db.AdminConnection admin, int pid, java.time.OffsetDateTime backendStart, String applicationName) { }

    private volatile InFlight inFlight;

    /** Test seam: runs once the completion's backend is published and before the stop flag is read again, where a stop can slip in. */
    Runnable afterPublishHook;

    /** Test seam: runs after a validation has committed and before its claim, where an engine can die. */
    Runnable afterValidateHook;

    /**
     * Cancels the completion in flight with {@code pg_cancel_backend}, after checking in the same statement that the pid is still that backend
     * (same database, same role, same name and start). A cancel, not a terminate: it ends the running statement and leaves the session, so
     * between two statements it does nothing and cannot kill a session that has moved on to another move; the window is closed by reading the
     * stop flag again after publishing, not by a harder signal.
     */
    @Override
    public void cancelInFlight() {
        InFlight running = inFlight;
        if (running != null) {
            DataMoveOrphans.cancelBackend(running.admin(), running.pid(), running.applicationName(), running.backendStart());
        }
    }

    /**
     * A move with no statement bound runs its on_complete here, in a transaction of its own under lock_timeout only and statement_timeout
     * 0, before the claim. The backend's pid and start go to the reporter (status shows how long it has run), a stop cancels the statement,
     * and so does a pause or a supersede set while it runs ({@link CompletionWatch}). PCI builders are ended first for a move over the chunk
     * family, except a per-leaf validation (it only conflicts with a build on its own leaf, and the lock wait answers that with 55P03), and
     * only after the move has been read again for a pause.
     *
     * @return null on success, otherwise the result that ends the finish
     */
    private Result validateBeforeClaim(DSLContext admin, DataMove move, Context context) {
        if (ChunkFamily.touches(move) && !isLeafValidation(move)) {
            Optional<DataMove> now = DataMoveRegistry.reload(admin, move.label());
            if (now.isEmpty()) {
                return Result.gone();
            }
            if (now.get().paused()) {
                return Result.paused(now.get());
            }
            try {
                admin.transaction(cfg -> PciBuilders.terminateOrThrow(cfg.dsl(), "reason=data_move_on_complete move=" + move.label(), log));
            } catch (DataAccessException e) {
                log.error("event=pci_builders_terminate_failed cause=\"{}\"", FailureHandler.oneLine(e.toString()));
            }
        }
        MoveReporter reporter = context.reporter();
        CompletionWatch[] watch = new CompletionWatch[1];
        try {
            boolean[] stoppedBefore = new boolean[1];
            admin.transaction(cfg -> {
                DSLContext tx = cfg.dsl();
                PgSession.setStatementAndLockBounds(tx, 0, DataMove.UNBOUNDED_LOCK_TIMEOUT_MS);
                var start = tx.select(DSL.field(DSL.name("pid"), Integer.class), DSL.field(DSL.name("backend_start"), java.time.OffsetDateTime.class))
                    .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                    .where(DSL.field(DSL.name("pid"), Integer.class).eq(DSL.function("pg_backend_pid", SQLDataType.INTEGER))).fetchOne();
                Integer pid = start == null ? null : start.value1();
                log.info("event=data_move_completion label={} pid={}", move.label(), pid);
                if (reporter != null && pid != null) {
                    reporter.callStarted(move, COMPLETION_RUN, pid);
                }
                if (pid != null) {
                    InFlight flight = new InFlight(context.adminConnection(), pid, start.value2(), context.applicationName());
                    inFlight = flight;
                    watch[0] = CompletionWatch.start(flight.admin(), move.label(), pid, flight.applicationName(), flight.backendStart());
                }
                Runnable seam = afterPublishHook;
                if (seam != null) {
                    seam.run();
                }
                if (context.stopped().getAsBoolean()) {   // a stop that came before the backend was published reached nobody: do not start
                    stoppedBefore[0] = true;
                    return;
                }
                tx.connection(c -> MoveSql.runOnComplete(c, move.onComplete()));
            });
            if (stoppedBefore[0]) {
                return Result.leftOpen();
            }
        } catch (DataAccessException e) {
            SQLException raised = e.getCause(SQLException.class);
            boolean cancelled = raised != null && "57014".equals(raised.getSQLState());
            if (cancelled && context.stopped().getAsBoolean()) {
                return Result.leftOpen();   // the engine stopped it; the next boot validates again
            }
            if (cancelled && watch[0] != null && watch[0].fired()) {
                Optional<DataMove> now = DataMoveRegistry.reload(admin, move.label());
                return now.isPresent() && now.get().paused() ? Result.paused(now.get()) : now.isEmpty() ? Result.gone() : Result.leftOpen();
            }
            return Result.report(failure(admin, move, FailureHandler.Stage.ON_COMPLETE, null, raised,
                "on_complete of " + move.label() + " did not complete: "
                    + FailureHandler.oneLine(raised != null ? "sqlstate=" + raised.getSQLState() + " " + raised.getMessage()
                        : e.getMessage())));
        } finally {
            if (watch[0] != null) {
                watch[0].close();
            }
            inFlight = null;
            if (reporter != null) {
                reporter.callEnded(move);
            }
        }
        Runnable crash = afterValidateHook;
        if (crash != null) {
            crash.run();
        }
        if (context.stopped().getAsBoolean()) {
            return Result.leftOpen();   // validated; the claim is the next pass's, and finds nothing left to validate
        }
        return null;
    }

    /** The run label the reporter shows for a completion statement. */
    static final String COMPLETION_RUN = "completion";

    /**
     * What an operator reads when the adoption's parent ADD times out under its bound: the bound and the leaf count it grew with (numbers only:
     * the status is unauthenticated), and the one thing to do about it. {@link MoveReasons#publicForm} keeps this text as it is.
     */
    static String adoptionTimeoutReason(DSLContext admin, DataMove move) {
        long leaves = chunkLeafCount(admin);
        return "adoption timed out at " + move.statementTimeoutMs() + " ms over " + leaves + " leaves of nexus.chunks; raise statement_timeout_ms on "
            + dev.nexus.service.vectors.Rdr229ChunksFk.ADOPT_LABEL + " for one attempt in a quiet window (it lengthens the write block on chunks and the registry by about "
            + "that much), then set it back";
    }

    /** The leaves of nexus.chunks (a partition with no partitions of its own, named for chunks), counted from the catalog. */
    static long chunkLeafCount(DSLContext ctx) {
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        return ctx.fetchCount(c.join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace")))),
            DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"),
            DSL.field(DSL.name("c", "relkind"), String.class).eq("r"),
            DSL.field(DSL.name("c", "relispartition"), Boolean.class).isTrue(),
            DSL.field(DSL.name("c", "relname"), String.class).like("chunks\\_%", '\\'));
    }

    private static boolean isAdoption(DataMove move) {
        return move.label().equals(dev.nexus.service.vectors.Rdr229ChunksFk.ADOPT_LABEL);
    }

    private static boolean isLeafValidation(DataMove move) {
        return move.label().startsWith(dev.nexus.service.vectors.Rdr229ChunksFk.LEAF_LABEL_PREFIX);
    }

    /** A re-check that ends the completion transaction: carries what {@link #failure} needs. */
    private static final class RecheckFailed extends RuntimeException {
        private static final long serialVersionUID = 1L;

        final transient FailureHandler.Stage stage;
        final transient SQLException raised;
        final String detail;

        RecheckFailed(FailureHandler.Stage stage, SQLException raised, String detail) {
            super(detail, raised);
            this.stage = stage;
            this.raised = raised;
            this.detail = detail;
        }
    }

    /**
     * The verification, inside the completion transaction after the UPDATE and before {@code on_complete}: once table-wide with no
     * {@code nexus.tenant}, then once per tenant, each under {@code SET LOCAL nexus.tenant} (the first pass runs the tenants and
     * then table-wide; the order here does not matter, every run is made). The per-tenant runs are not redundant: a verification that
     * reads a row-security-scoped table or helper sees nothing without a tenant, so the table-wide run is empty whatever the table
     * holds and the tenant's own run is the check. The whole re-check shares {@code statement_timeout_ms}: each run gets what is left
     * of it (so do the catalog reads of the tenant re-read, each bounded by what is left when it starts), and the move's own statement and lock bounds are set again for {@code on_complete}, which gets a fresh
     * {@code statement_timeout_ms} (so the transaction can run up to twice that, plus the UPDATE). The first pass has warmed what
     * the runs read, but only what the verification itself reads: the registrant writes the verification to read what
     * {@code on_complete} reads (the table, and the index or foreign-key side it will scan), or the first pass warms nothing for it.
     *
     * <p><b>What it covers, and what it does not.</b> It is a point-in-time check: it sees the rows committed when each run's
     * snapshot is taken, and a write committed after that can still land before {@code on_complete} runs (nothing in this transaction
     * stops a writer; only {@code on_complete}'s own locks do). What it closes is the window between the first pass and this
     * transaction, which can be hours. The tenant list is the first pass's, plus what the catalogs name now that it was not listed
     * with ({@link MoveTenants#reread}: a tenant with a {@code nexus.service_tokens} row since, and, on a table partitioned one
     * tenant per leaf, a tenant with a leaf since). The remaining limit: a tenant whose first rows appear in a table the probe listed,
     * after the probe, that has neither a token nor a leaf, is not run here; only a rescan of the table could find it.
     *
     * <p><b>Why one shared budget, not one bound per tenant.</b> The completion transaction holds the row lock of the move's
     * registry row (and the transaction its snapshot) from the UPDATE to commit, and the number of tenants is the user count, which
     * the registry does not bound. With a bound per tenant the hold grows as tenants times {@code statement_timeout_ms} (1.4 s for
     * Phase 3: ten tenants could hold it 14 s); with one shared budget it is at most the re-check's {@code statement_timeout_ms} plus
     * {@code on_complete}'s own. The price is that a move with many tenants whose warm runs add up past the bound times out (57014,
     * stage {@code RECHECK}, transient: the move retries with backoff and every retry repeats the first pass, so an under-sized
     * bound loops), so a registrant sets {@code statement_timeout_ms} for the sum of the tenants' warm runs;
     * {@code DataMoveSplitBoundsIntegrationTest} pins both sides (runs whose sum fits complete; runs each under the bound whose sum
     * is over it time out and complete nothing).
     *
     * <p><b>Errors.</b> The re-check runs under the transaction's {@code lock_timeout}, so a wait behind an ACCESS EXCLUSIVE holder
     * raises 55P03, and a deadlock or a serialization failure is possible: these are stage {@code RECHECK}, classified as
     * {@code on_complete}'s are (transient), never {@code VERIFICATION}, where they would be permanent. A row it finds is
     * {@code VERIFICATION_NONEMPTY}, permanent, as in the first pass. An exception that is not the database's (a defect of the
     * worker's own) is stage {@code PASS}, which the classifier treats as permanent with nothing raised: only the check that the
     * session carries no tenant is a {@code RECHECK} with nothing raised, and transient.
     *
     * <p>The session is shown to carry no tenant before the first run, and again after the last tenant's setting is cleared, so a
     * tenant that leaked in (or out) never reaches {@code on_complete}. Every failing run is reported, as the first pass reports.
     *
     * @throws RecheckFailed when the session carries a tenant, a row of the old shape remains, or a run fails or times out
     */
    private void recheck(DSLContext tx, Connection c, DataMove move, List<String> tenants) {
        try {
            Consumer<DSLContext> seam = recheckHook;
            if (seam != null) {
                seam.accept(tx);
            }
            requireNoTenant(tx, move, "before the re-check");
            long deadline = move.unbounded() ? NO_DEADLINE : System.nanoTime() + move.statementTimeoutMs() * 1_000_000L;
            // a move over a table that is not tenant-scoped has no tenant list, and keeps none; the catalog reads of the re-read
            // are bounded by what is left of the budget when they start
            clampToBudget(tx, deadline);
            List<String> listed = tenants.isEmpty() ? tenants
                : MoveTenants.reread(tx, move.drivingSchema(), move.drivingTable(), tenants);
            Map<String, Long> found = new LinkedHashMap<>();
            runWithinBudget(tx, c, move, null, deadline, found);
            for (String tenant : listed) {
                setTenant(tx, tenant);
                runWithinBudget(tx, c, move, tenant, deadline, found);
            }
            if (!listed.isEmpty()) {
                setTenant(tx, "");
            }
            requireNoTenant(tx, move, "after the re-check");
            if (move.unbounded()) {
                PgSession.setStatementAndLockBounds(tx, 0, DataMove.UNBOUNDED_LOCK_TIMEOUT_MS);
            } else {
                PgSession.setStatementAndLockBounds(tx, move.statementTimeoutMs(), lockTimeoutMs(move.statementTimeoutMs()));
            }
            if (!found.isEmpty()) {
                List<String> parts = new ArrayList<>();
                found.forEach((who, n) -> parts.add(who + "=" + (n > COUNT_CAP ? COUNT_CAP + "+" : n)));
                String shown = String.join(", ", parts);
                MoveEvents.verificationFailed(move.label(), shown);
                throw new RecheckFailed(FailureHandler.Stage.VERIFICATION_NONEMPTY, null,
                    "verification of " + move.label() + " in the completion transaction found old-shape rows in "
                        + move.drivingTableLabel() + ": " + shown + " (written after the first pass)");
            }
        } catch (RecheckFailed e) {
            throw e;
        } catch (SQLException | RuntimeException e) {
            // an error from the database is the re-check's (RECHECK); anything else, an NPE or another bug of the worker's own, is
            // not the host's and no retry changes it: PASS, which the classifier treats as permanent when nothing was raised. Only
            // the tenant-leak guard (requireNoTenant, above) is a RECHECK with nothing raised, and transient.
            FailureHandler.Stage stage = e instanceof MoveSql.Refused ? FailureHandler.Stage.VERIFICATION_REFUSED
                : sqlOf(e) != null ? FailureHandler.Stage.RECHECK : FailureHandler.Stage.PASS;
            throw new RecheckFailed(stage, sqlOf(e),
                "the verification in the completion transaction of " + move.label() + " could not run: " + describe(e));
        }
    }

    /** The statement timeout for what follows, the budget that is left (at least 1 ms). */
    private static void clampToBudget(DSLContext tx, long deadline) {
        PgSession.setLocal(tx, "statement_timeout",
            deadline == NO_DEADLINE ? "0" : Long.toString(Math.max(1L, (deadline - System.nanoTime()) / 1_000_000L)));
    }

    /** One re-check run, under what is left of the budget; a tenant of {@code null} is the table-wide run. */
    private static void runWithinBudget(DSLContext tx, Connection c, DataMove move, String tenant, long deadline,
                                        Map<String, Long> found) throws SQLException {
        long leftMs = deadline == NO_DEADLINE ? Long.MAX_VALUE : (deadline - System.nanoTime()) / 1_000_000L;
        if (leftMs <= 0) {
            throw new RecheckFailed(FailureHandler.Stage.RECHECK, new SQLException("statement_timeout_ms spent", "57014"),
                "the verification in the completion transaction of " + move.label() + " used all of statement_timeout_ms="
                    + move.statementTimeoutMs() + " before " + (tenant == null ? "the table-wide run" : "tenant " + tenant));
        }
        PgSession.setLocal(tx, "statement_timeout", deadline == NO_DEADLINE ? "0" : Long.toString(leftMs));
        long rows = MoveSql.countRows(c, move.verification(), tenant, COUNT_CAP);
        if (rows > 0) {
            found.put(tenant == null ? "table-wide" : "tenant " + tenant, rows);
        }
    }

    /** {@code SET LOCAL nexus.tenant} (as {@code set_config(..., true)}), so the setting ends with the transaction at the latest. */
    private static void setTenant(DSLContext tx, String tenant) {
        tx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val(TenantScope.DEFAULT_TENANT_GUC),
            DSL.val(tenant, SQLDataType.VARCHAR), DSL.inline(true))).fetchOne();
    }

    private static void requireNoTenant(DSLContext tx, DataMove move, String when) {
        String setting = tx.select(DSL.function("current_setting", SQLDataType.VARCHAR,
            DSL.val(TenantScope.DEFAULT_TENANT_GUC), DSL.inline(true))).fetchOne(0, String.class);
        if (setting != null && !setting.isEmpty()) {
            throw new RecheckFailed(FailureHandler.Stage.RECHECK, null,
                "the completion transaction of " + move.label() + " carries nexus.tenant=" + setting + " " + when
                    + ", so a run would read one tenant's rows and report the table clean, or on_complete would run as that tenant");
        }
    }

    // ── failures ───────────────────────────────────────────────────────────────────────────────────────────

    private static FailureHandler.Failure failure(DSLContext admin, DataMove move, FailureHandler.Stage stage,
                                                  String tenant, SQLException raised, String detail) {
        return new FailureHandler.Failure(move, stage, null, move.drivingSchema(), move.drivingTable(), tenant, 0, raised,
            detail, admin);
    }

    /** {@code VERIFICATION_REFUSED} for a text the worker will not run, {@code VERIFICATION} for anything that can pass. */
    private static FailureHandler.Stage stageOf(Exception e) {
        return e instanceof MoveSql.Refused ? FailureHandler.Stage.VERIFICATION_REFUSED : FailureHandler.Stage.VERIFICATION;
    }

    private static SQLException sqlOf(Exception e) {
        if (e instanceof SQLException se) {
            return se;
        }
        return e instanceof DataAccessException dae ? dae.getCause(SQLException.class) : null;
    }

    private static String describe(Exception e) {
        if (e instanceof MoveSql.Refused) {
            return FailureHandler.oneLine(e.getMessage());
        }
        SQLException sql = sqlOf(e);
        return FailureHandler.oneLine(sql != null ? "sqlstate=" + sql.getSQLState() + " " + sql.getMessage() : e.toString());
    }
}
