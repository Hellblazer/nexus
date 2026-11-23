// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import static org.jooq.impl.DSL.function;
import static org.jooq.impl.DSL.val;

/** The per-(tenant, collection) advisory lock of {@link TaxonomyRepository}'s persist paths (moved out of it to keep it under its size ceiling, RDR-229 P3.5c).
 * The lock is keyed by the resolved registry row's collection id (linda 2026-11-23), so a persist under a collection's old label and one under
 * its new label after a one-row rename take the same key and serialize; a label with no registry row keeps the name key. */
final class TaxonomyCollectionLock {

    private TaxonomyCollectionLock() {}

    /**
     * Serialize taxonomy persist operations per (tenant, collection) within the
     * CURRENT transaction (nexus-n2ls1).
     *
     * <p>The persist paths' existing-topics guard is a plain unlocked SELECT
     * COUNT: two concurrent persists for the same (tenant, collection) both
     * counted 0 under READ COMMITTED, both inserted the same root label, and
     * the loser hit the taxonomy-004 partial unique index (SQLSTATE 23505 →
     * HTTP 409, observed live 2026-07-07). {@code pg_advisory_xact_lock}
     * blocks the second persist until the first commits, so the loser then
     * sees the winner's committed rows and takes the guard-skip (discover) or
     * a fresh replace (rebuild). Released automatically at commit/rollback.
     * A {@code hashtext} collision between two different keys merely
     * over-serializes — never a correctness issue.
     */
    static void lockTaxonomyCollection(DSLContext ctx, String tenant, String collection) {
        // Bounded wait FIRST (critique, nexus-n2ls1): without a lock_timeout a
        // stuck holder turns the fast retryable 409 this fix removes into an
        // indefinite in-transaction wait surfacing as an edge 504 — worse
        // observability than the original bug. set_config(..., true) is
        // SET LOCAL (txn-scoped, auto-reverts at commit/rollback); a timed-out
        // acquire raises SQLSTATE 55P03, a clean retryable error. The timeout
        // also bounds this txn's subsequent row-lock waits — acceptable, these
        // persist txns are short writes.
        ctx.select(function("set_config", String.class,
                   val("lock_timeout"), val("5000"), val(true)))
           .fetch();
        // Typed-DSL function composition (house rule: no raw string-SQL —
        // RawSqlGateTest): SELECT pg_advisory_xact_lock(hashtext(:key)).
        // hashtext returns int4, implicitly widened to the bigint overload.
        // The key is the registry row's id, read now in this transaction (live-first for a shared label); a label with no registry row has
        // no id and takes the name key, as it always did.
        Long id = ctx.select(CollectionIds.idOf(tenant, collection)).fetchOne(0, Long.class);
        String key = id != null ? tenant + "#id/" + id : tenant + "/" + collection;
        ctx.select(function("pg_advisory_xact_lock", Object.class,
                   function("hashtext", Integer.class, val(key))))
           .fetch();
    }
}
