// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.RenameReadiness;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.util.Map;
import java.util.Objects;
import java.util.function.Function;

/**
 * The one entry point of every collection rename, RDR-229 P3.5a (bead nexus-p2m1x.23; linda's rulings 3, D2 and R-A): the object
 * {@code CatalogRepository.renameCollectionTxn} and the rename of each per-store repository (chash, taxonomy, telemetry, aspects,
 * highlights) take by constructor, and the only way any of them reaches the rename it runs.
 *
 * <p>{@link #runRename} opens the rename's transaction (the caller's tenant stamp), and as the first statements of it takes
 * {@link RenameLatchLock} shared, asserts the transaction runs at READ COMMITTED, and reads the gate 3 latch row. The row alone
 * decides, and the matching body runs in that same transaction:
 * <ul>
 * <li>no row: the name path ({@code nameBody}, today's rename, which gives the collection a new id);</li>
 * <li>the row and {@link RenameReadiness} complete: the one-row regime ({@code oneRowBody});</li>
 * <li>the row and readiness not complete: {@link RenameRefusedException}, 409 {@code rename_refused} naming the missing condition.</li>
 * </ul>
 * The latch write takes the same lock exclusive ({@code CompletionCheck#insertLatch}), so a rename either committed before the row was
 * written or reads it. There is no check outside the transaction: a decision made before the lock can be stale by the time the rename
 * commits (ruling D2), so none exists. {@link #open()} runs the name body with no lock and no read: what a repository built without a
 * guard gets, for the callers and tests that predate P3.5a.
 *
 * <p>The in-process gate 3 signal plays no part: it is seeded from the row at boot and set after the row commits, so it can only
 * lag the row, and the one-row window opens only once the row is committed.
 *
 * <p>One public constructor, {@code (readiness, oneRowRename)}: P3.7 supplies its catalog rename ({@link OneRowRename}) where
 * {@code NexusService} builds the guard.
 */
public final class RenameGuard {

    private static final RenameGuard OPEN = new RenameGuard(RenameReadiness.ready(), OneRowRename.failClosed(), false);

    private final RenameReadiness readiness;
    private final OneRowRename oneRowRename;
    /** False for {@link #open()}: it never refuses, so its transactions take no lock and read no latch row. */
    private final boolean enforcing;

    public RenameGuard(RenameReadiness readiness, OneRowRename oneRowRename) {
        this(readiness, oneRowRename, true);
    }

    private RenameGuard(RenameReadiness readiness, OneRowRename oneRowRename, boolean enforcing) {
        this.readiness = Objects.requireNonNull(readiness, "readiness");
        this.oneRowRename = Objects.requireNonNull(oneRowRename, "oneRowRename");
        this.enforcing = enforcing;
    }

    /** A guard that never refuses and has no one-row regime. */
    public static RenameGuard open() {
        return OPEN;
    }

    /** Which rename a transaction runs, as decided from the latch row. */
    public enum RenamePath {
        /** No latch row: the name path. */
        NAME_PATH,
        /** The latch row exists and readiness is complete: the one-row regime. */
        ONE_ROW
    }

    /**
     * Runs a rename. {@code transaction} opens the transaction and runs the work it is given in it, with the tenant stamped (for
     * example {@code work -> tenantScope.withTenant(tenant, work)}, or a retrying wrapper around that); each call of it is one
     * attempt, and the decision is made afresh in each.
     *
     * @throws RenameRefusedException when the latch row exists and the one-row rename cannot run yet
     * @throws IllegalStateException  when the transaction is not READ COMMITTED (the read of the row would be stale)
     */
    public <T> T runRename(Function<Function<DSLContext, T>, T> transaction, Function<DSLContext, T> nameBody,
                           Function<DSLContext, T> oneRowBody) {
        if (!enforcing) {
            return transaction.apply(nameBody);
        }
        return transaction.apply(ctx -> {
            RenameLatchLock.shared(ctx);
            requireReadCommitted(ctx);
            boolean latched = CompletionCheck.readLatch(ctx, CompletionCheck.GATE3).isPresent();
            return switch (decide(latched, readiness)) {
                case NAME_PATH -> nameBody.apply(ctx);
                case ONE_ROW -> oneRowBody.apply(ctx);
            };
        });
    }

    /** {@link #runRename(Function, Function, Function)} in a plain tenant transaction of {@code scope}. */
    public <T> T runRename(TenantScope scope, String tenant, Function<DSLContext, T> nameBody, Function<DSLContext, T> oneRowBody) {
        return runRename(work -> scope.withTenant(tenant, work), nameBody, oneRowBody);
    }

    /**
     * The decision, from the latch row and the readiness alone.
     *
     * @throws RenameRefusedException when the row exists and readiness is not complete
     */
    static RenamePath decide(boolean latchRow, RenameReadiness readiness) {
        if (!latchRow) {
            return RenamePath.NAME_PATH;
        }
        RenameReadiness.Missing missing = readiness.missing();
        if (missing != RenameReadiness.Missing.NONE) {
            throw new RenameRefusedException(missing);
        }
        return RenamePath.ONE_ROW;
    }

    /** Fails closed: under REPEATABLE READ the snapshot is taken before the lock wait, so the read of the row could predate the latch write. */
    private static void requireReadCommitted(DSLContext ctx) {
        String level = ctx.select(DSL.function("current_setting", String.class, DSL.val("transaction_isolation"))).fetchOne(0, String.class);
        if (!"read committed".equals(level)) {
            throw new IllegalStateException("a collection rename needs a READ COMMITTED transaction to read the gate 3 latch row after "
                + "the rename lock; this one is '" + level + "'");
        }
    }

    /** The catalog's one-row body: P3.7's {@link OneRowRename}, its epoch recorded for the route, its counts answered. */
    public Map<String, Integer> oneRowCounts(DSLContext ctx, String tenant, String oldLabel, String newLabel, String[] epochOut) {
        RenameOutcome outcome = oneRowRename.rename(ctx, tenant, oldLabel, newLabel);
        epochOut[0] = outcome.catalogEpoch();
        return outcome.counts();
    }

    /**
     * Whether a collection's id survives a rename on this install (R6's {@code ids_stable}, the key P3.5a's rename response
     * carries). Always false in this tree: the rename it runs is the name path, which gives the collection a new id. P3.7's one-row
     * rename keeps the id and is what makes this true; it changes this one method.
     */
    public boolean idsStable() {
        return false;
    }
}
