// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import static org.jooq.impl.DSL.function;
import static org.jooq.impl.DSL.val;

/**
 * The one lock that orders a collection rename against the write of the gate 3 latch row (RDR-229 P3.5a, linda's ruling D2,
 * bead nexus-p2m1x.23). It is a transaction-scoped advisory lock ({@code pg_advisory_xact_lock_shared} / {@code pg_advisory_xact_lock},
 * two-key form, so it is a lock space of its own beside the single-key locks the repositories take on {@code hashtext(...)}).
 *
 * <ul>
 * <li>A rename takes it SHARED, first in its own transaction ({@link RenameGuard#runRename}), then reads the latch row. Renames do not
 *     exclude each other here.</li>
 * <li>The latch write takes it EXCLUSIVE in the transaction that inserts the row ({@code CompletionCheck#insertLatch}), so it waits for
 *     every rename in flight to commit or roll back, and a rename that starts after it is queued behind it. A rename therefore either
 *     committed before the row was written, or it reads the row (the database runs at READ COMMITTED, which {@code runRename} asserts,
 *     so the read after the lock sees what the writer committed).</li>
 * </ul>
 *
 * <p>This is the one lock of the rename and the latch write (linda, R-A): it lives in the database, not in the process, because the row
 * does, so two engines against one install order the same way; there is no in-process rename lock beside it. <b>Lock order</b>: this
 * lock is taken before any other lock of the rename transaction, and the latch writer takes nothing else, so no cycle can form
 * through it.
 */
public final class RenameLatchLock {

    /** The first key: names this lock space (the bytes of "RNLT"). Positive, so {@code pg_locks.classid} shows it unsigned and unchanged. */
    public static final int NAMESPACE = 0x524E4C54;

    /** The second key: the gate the lock orders renames against (gate 3). */
    public static final int GATE3_KEY = 3;

    private RenameLatchLock() {
    }

    /** What a rename takes: waits for a latch write in flight, and is waited for by the next one. */
    public static void shared(DSLContext ctx) {
        // Typed-DSL function composition (house rule: no raw string-SQL, RawSqlGateTest): SELECT pg_advisory_xact_lock_shared(k1, k2).
        ctx.select(function("pg_advisory_xact_lock_shared", Object.class, val(NAMESPACE), val(GATE3_KEY))).fetch();
    }

    /** What the latch write takes: waits for every rename in flight. */
    public static void exclusive(DSLContext ctx) {
        ctx.select(function("pg_advisory_xact_lock", Object.class, val(NAMESPACE), val(GATE3_KEY))).fetch();
    }
}
