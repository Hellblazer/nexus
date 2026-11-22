// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.PciBuilders;
import dev.nexus.service.db.PgSession;
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
 *       READ ONLY transaction under {@code statement_timeout_ms}, and every one is run so a failure names every tenant
 *       that still holds old-shape rows;</li>
 *   <li>if every run returned no rows, the row is read once more (a pause set now still stops it), and {@code on_complete}
 *       (if any) and the UPDATE that sets {@code completed_at} run in one transaction on the pass's admin connection, so
 *       neither takes effect without the other.</li>
 * </ol>
 *
 * <p>The UPDATE goes first inside that transaction and carries {@code paused_at IS NULL} and no row that supersedes the
 * move, so it takes the row's lock and rechecks both atomically with the commit; when it matches no row the transaction
 * ends having run nothing. Only after it matched, on a move over the chunk family ({@link ChunkFamily}), are the
 * per-collection index builders ended (best effort, {@link PciBuilders}), so a pause set after the last read ends no build.
 * {@code on_complete} runs under {@code SET LOCAL lock_timeout} below the move's {@code statement_timeout_ms} on every
 * move, so a lock it cannot get fails fast with 55P03 and no reader queues behind it.
 *
 * <p>A stop ({@link Context#stopped}) is read before the finish, between tenants, before the table-wide run and before the
 * completion transaction, and ends the finish as {@link Outcome#LEFT_OPEN}; a verification already in flight is not
 * cancelled, it is bounded by {@code statement_timeout_ms}.
 *
 * <p>Nothing here decides blocked or failed: a failure goes back as a {@link FailureHandler.Failure} ({@link
 * FailureHandler.Stage#VERIFICATION}, {@link FailureHandler.Stage#VERIFICATION_REFUSED}, {@link
 * FailureHandler.Stage#VERIFICATION_NONEMPTY}, {@link FailureHandler.Stage#ON_COMPLETE}) for the worker to hand to the {@link FailureHandler}. A verification text the worker
 * refuses to run ({@link MoveSql#refusal}) is a {@code VERIFICATION_REFUSED} failure with {@code raised} null and the rule in
 * {@code detail}.
 */
final class VerifyingFinisher implements MoveFinisher {

    private static final Logger log = LoggerFactory.getLogger(VerifyingFinisher.class);

    /** A run reads at most this many old-shape rows before it reports "more than" (a count, not the table). */
    static final int COUNT_CAP = 10_000;

    /** The longest {@code lock_timeout} of {@code on_complete}, in milliseconds; a move with a shorter statement bound gets half of it. */
    static final int LOCK_TIMEOUT_CEILING_MS = 5_000;

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));

    /** The {@code lock_timeout} of {@code on_complete}: half the statement bound, at most 5 s, at least 1 ms. */
    static int lockTimeoutMs(int statementTimeoutMs) {
        return Math.max(1, Math.min(LOCK_TIMEOUT_CEILING_MS, statementTimeoutMs / 2));
    }

    /** Test seam: runs after the second read of the row and before the completion transaction, where a pause can slip in. */
    Runnable beforeCompletionHook;

    /** Test seam: runs with the verification connection after each reset, so a test can read what the session carries. */
    Consumer<CallConnection> afterResetHook;

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
        return complete(admin, move);
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

    /** The verification query in a READ ONLY transaction under {@code statement_timeout_ms}; the transaction is always rolled back. */
    private static long readOnlyCount(CallConnection conn, DataMove move, String tenant) throws SQLException {
        Connection c = conn.connection();
        c.setAutoCommit(false);
        c.setReadOnly(true);
        try {
            PgSession.setLocal(conn.ctx(), "statement_timeout", Integer.toString(move.statementTimeoutMs()));
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

    private Result complete(DSLContext admin, DataMove move) {
        String onComplete = move.onComplete();
        int statementMs = move.statementTimeoutMs();
        int lockMs = lockTimeoutMs(statementMs);
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
                if (onComplete != null) {
                    if (ChunkFamily.touches(move)) {
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
        } catch (DataAccessException e) {
            SQLException raised = e.getCause(SQLException.class);
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
        MoveEvents.completed(move.label(), onComplete != null);
        return Result.completed();
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
