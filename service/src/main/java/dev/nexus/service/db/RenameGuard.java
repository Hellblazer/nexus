// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.RenameReadiness;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Map;
import java.util.Objects;
import java.util.function.Function;

/**
 * The one entry point of every collection rename, RDR-229 P3.5a (bead nexus-p2m1x.23; linda's rulings 3, D2 and R-A): the object
 * {@code CatalogRepository.renameCollectionTxn} and the rename of each per-store repository (chash, taxonomy, telemetry, aspects,
 * highlights) take by constructor, and the only way any of them reaches the rename it runs.
 *
 * <p>Readiness is a dependency of this decision with a contract: {@link RenameReadiness#missing()} is total, cheap and fail-closed. It
 * never throws, and any read failure or timeout answers a value other than {@code NONE} that names the missing condition, so the
 * guard never reads a failure as "complete". That behaviour is P3.7's to supply; until then the stub answers the drop move is missing.
 * The drop move is gated on the latched gate 3, so readiness {@code NONE} implies the latch row exists: "no row with readiness
 * complete" is a real inconsistency (a partial restore), never a timing window.
 *
 * <p>{@link #runRename} opens the rename's transaction (the caller's tenant stamp), and as the first statements of it takes
 * {@link RenameLatchLock} shared, asserts the transaction runs at READ COMMITTED, and reads the gate 3 latch row. The row alone
 * decides, and the matching body runs in that same transaction:
 * <ul>
 * <li>no row and readiness not complete (before the latch): the name path ({@code nameBody}, today's rename, which gives the collection
 *     a new id);</li>
 * <li>no row and readiness complete: an inconsistency (the move is done on an install with no latch, as after a partial restore):
 *     {@link RenameRefusedException} with {@code missing} {@code latch_row_missing}, logged at ERROR, never the name path, which would
 *     mint a new id;</li>
 * <li>the row and {@link RenameReadiness} complete: the one-row regime ({@code oneRowBody});</li>
 * <li>the row and readiness not complete: {@link RenameRefusedException}, 409 {@code rename_refused} naming the missing condition.</li>
 * </ul>
 * The latch write takes the same lock exclusive ({@code CompletionCheck#insertLatch}), so a rename either committed before the row was
 * written or reads it. There is no check outside the transaction: a decision made before the lock can be stale by the time the rename
 * commits (ruling D2), so none exists. {@link #open()} runs the name body with no lock and no read: what a repository built without a
 * guard gets, for the callers and tests that predate P3.5a.
 *
 * <p><b>The canonical lock order</b> (linda, "live at commit"), for every verb that retires or revives a registry row and every writer
 * that files rows under one; this is the one statement of it:
 * <ol>
 * <li>{@link RenameLatchLock}, shared: verbs only (a writer never takes it);</li>
 * <li>the sweep gate(s), shared, one per label the acquirer covers, in sorted order;</li>
 * <li>the collection tuple lock(s) ({@code CollectionTupleLock}, taken only while the live-tuple unique index is absent), in their
 *     existing order;</li>
 * <li>the registry rows, in {@code collection_id} order: {@code FOR UPDATE} for a verb ({@code CollectionTombstones#lockRows}, called
 *     in {@code CollectionRename#retireAndRegister} right after the tuple locks and before the first row write),
 *     {@code FOR KEY SHARE} for a writer: the registry check every write transaction runs first takes it (by id through the request
 *     carrier, by label through the memoised in-transaction read; the label read lands with slice A at assembly), and the foreign key
 *     takes it again on the first child insert;</li>
 * <li>only then the chunk, manifest and child-row work.</li>
 * </ol>
 * The writers that take advisory locks follow it: a manifest or chunk writer takes its sweep gate shared and then, on insert, its key
 * share; a registration or mint takes the tuple lock and then writes the registry row (the catalog-051 triggers take the same tuple
 * lock); none takes a tuple lock and then a gate.
 * {@code FOR UPDATE} conflicts with {@code FOR KEY SHARE}, so a verb waits for the writers in flight on its rows and its re-home
 * includes their rows, and a writer that arrives after it waits, then reads the retired row (409 {@code collection_superseded}) instead of
 * filing rows into it: the bar is "live at commit" for EVERY writer, each of which takes the registry key share first, in its own
 * transaction, before any child write. The verb's wait is bounded ({@link RenameBusyException}, 503 {@code rename_busy}); a writer's is
 * not, and a wait over a second is logged either way ({@link RegistryLockWait}). Both sides take the gate before the row, so a sweep queued for the exclusive gate waits on both shared holders and
 * neither waits on it once it holds its gate: no cycle. The supersede takes no sweep gate, so it takes steps 1 and 3 only.
 *
 * <p>The latch row decides, except that no row with the readiness complete is {@code latch_row_missing}. The in-process gate 3 signal
 * plays no part: it is seeded from the row at boot and set after the row commits, so it can only lag the row, and the one-row window
 * opens only once the row is committed.
 *
 * <p>One public constructor, {@code (readiness, oneRowRename)}: P3.7 supplies its catalog rename ({@link OneRowRename}) where
 * {@code NexusService} builds the guard.
 */
public final class RenameGuard {

    private static final Logger log = LoggerFactory.getLogger(RenameGuard.class);

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
     * @throws RenameRefusedException when the latch row exists and the one-row rename cannot run yet, or when there is no row and the
     *                                readiness is complete
     * @throws IllegalStateException  when {@code transaction} does not run the work in one transaction (the rename lock is not held
     *                                after taking it), or the transaction is not READ COMMITTED (the read of the row would be stale)
     */
    public <T> T runRename(Function<Function<DSLContext, T>, T> transaction, Function<DSLContext, T> nameBody,
                           Function<DSLContext, T> oneRowBody) {
        Objects.requireNonNull(transaction, "transaction");
        Objects.requireNonNull(nameBody, "nameBody");
        Objects.requireNonNull(oneRowBody, "oneRowBody: every rename must say what it does in the one-row regime, even if only to refuse");
        if (!enforcing) {
            return transaction.apply(nameBody);
        }
        return transaction.apply(ctx -> {
            RenameLatchLock.shared(ctx);
            requireInTransaction(ctx);
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
     * @throws RenameRefusedException when the row exists and readiness is not complete, or when there is no row and readiness is
     *                                complete ({@code latch_row_missing})
     */
    static RenamePath decide(boolean latchRow, RenameReadiness readiness) {
        RenameReadiness.Missing missing = readiness.missing();
        if (!latchRow) {
            if (missing == RenameReadiness.Missing.NONE) {
                log.error("event=rename_refused_latch_row_missing the rename readiness says the name-key drop move is complete but this "
                    + "install has no gate 3 latch row; this is an inconsistency (as after a partial restore); a name-path rename would "
                    + "mint a new id, so it is refused");
                throw new RenameRefusedException(RenameReadiness.Missing.LATCH_ROW_MISSING);
            }
            return RenamePath.NAME_PATH;
        }
        if (missing != RenameReadiness.Missing.NONE) {
            throw new RenameRefusedException(missing);
        }
        return RenamePath.ONE_ROW;
    }

    /**
     * Fails closed unless this backend holds the rename lock now: the lock is transaction-scoped, so a caller whose {@code transaction}
     * argument ran the work on an autocommit connection would have released it at the end of the statement above, and the decision
     * would no longer be atomic with the write.
     */
    private static void requireInTransaction(DSLContext ctx) {
        int held = ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")), DSL.field(DSL.name("locktype"), String.class).eq("advisory")
            .and(DSL.field(DSL.name("granted"), Boolean.class).isTrue())
            .and(DSL.field(DSL.name("pid"), Integer.class).eq(DSL.function("pg_backend_pid", Integer.class)))
            .and(DSL.field(DSL.name("classid")).cast(org.jooq.impl.SQLDataType.BIGINT).eq((long) RenameLatchLock.NAMESPACE))
            .and(DSL.field(DSL.name("objid")).cast(org.jooq.impl.SQLDataType.BIGINT).eq((long) RenameLatchLock.GATE3_KEY)));
        if (held == 0) {
            throw new IllegalStateException("a collection rename must run inside one transaction: the rename lock is not held after taking it");
        }
    }

    /** Fails closed: under REPEATABLE READ the snapshot is taken before the lock wait, so the read of the row could predate the latch write. */
    private static void requireReadCommitted(DSLContext ctx) {
        String level = ctx.select(DSL.function("current_setting", String.class, DSL.val("transaction_isolation"))).fetchOne(0, String.class);
        if (!"read committed".equals(level)) {
            throw new IllegalStateException("a collection rename needs a READ COMMITTED transaction to read the gate 3 latch row after "
                + "the rename lock; this one is '" + level + "'");
        }
    }

    /** The catalog's one-row body: P3.7's {@link OneRowRename} runs, and its whole outcome is kept for the caller. */
    Map<String, Integer> oneRow(DSLContext ctx, String tenant, String oldLabel, String newLabel, RenameOutcome[] outcomeOut) {
        outcomeOut[0] = oneRowRename.rename(ctx, tenant, oldLabel, newLabel);
        return outcomeOut[0].counts();
    }

    /**
     * After the commit of a one-row catalog rename: both labels leave the registry cache (the old label's row is retired or gone and the
     * new one's attributes may have changed, so a stale entry would let a later write pass the registration check and strand rows), and
     * the outcome P3.7's body answered is the answer. Nothing is refilled: a miss reads the registry on the next use.
     */
    RenameOutcome committedOneRow(String tenant, String oldLabel, String newLabel, RenameOutcome outcome) {
        CollectionRegistry.evict(tenant, oldLabel);
        CollectionRegistry.evict(tenant, newLabel);
        return outcome;
    }

    /**
     * The premise of a per-store no-op (linda's Q-A ruling): the registry rename has moved the label, so the NEW label is a live
     * registry row (not superseded) and the OLD one is not. Read in the rename's own transaction, under the shared rename lock.
     *
     * @throws RenameNotInRegistryException when it is not so
     */
    static void requireRegistryRenamed(DSLContext ctx, String tenant, String oldLabel, String newLabel) {
        var live = ctx.select(DSL.field(DSL.name("name"), String.class))
            .from(DSL.table(DSL.name("nexus", "catalog_collections")))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant))
            .and(DSL.field(DSL.name("name"), String.class).in(oldLabel, newLabel))
            .and(DSL.field(DSL.name("superseded_by"), String.class).eq(""))
            .fetch(DSL.field(DSL.name("name"), String.class));
        if (!live.contains(newLabel) || live.contains(oldLabel)) {
            throw new RenameNotInRegistryException();
        }
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
