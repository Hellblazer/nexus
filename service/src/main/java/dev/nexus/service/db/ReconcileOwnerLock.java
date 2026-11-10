// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;

/**
 * RDR-193 (nexus-u5m5g.5): the transaction-scoped advisory lock that serializes the reconcile commits of one owner
 * across engines. {@code EngineJobs} refuses a second run of an owner only inside one engine process (its scope is
 * tenant + owner in that process); a second engine replica knows nothing of it, and {@code catalog_reconcile_commit}
 * locks per run, so two runs of one owner could commit at once. The commit takes this lock first, in the same
 * transaction as the function call, and the database releases it at commit or rollback.
 *
 * <p>The key follows the convention of the engine's other transaction locks ({@code pg_advisory_xact_lock(hashtext(
 * '<name>:' || tenant || '/' || ...))}, an int4 widened to bigint). A {@code hashtext} collision between two owners only
 * over-serializes them. The wait is bounded by the {@code statement_timeout} the caller set before the lock.
 */
final class ReconcileOwnerLock {

    private ReconcileOwnerLock() {}

    /** The string {@code hashtext} turns into the lock key: one key per (tenant, owner). */
    static String key(String tenant, String owner) {
        return "reconcilecommit:" + tenant + "/" + owner;
    }

    /** Blocks until no other transaction holds the owner's lock, then holds it until this transaction ends. */
    static void acquire(DSLContext ctx, String tenant, String owner) {
        ctx.select(DSL.function("pg_advisory_xact_lock", Object.class,
                DSL.function("hashtext", Integer.class, DSL.val(key(tenant, owner)))))
           .fetch();
    }
}
