// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * The registry rows a write transaction has already read {@code FOR KEY SHARE} (RDR-229 P3.5a, linda's option (b)): one read of a
 * collection's row per transaction, and the lock it took is held to the end of the transaction, so a second read adds nothing.
 *
 * <p>The memo lives in the {@code data} map of the transaction's own jOOQ configuration, never in a thread-local and never in a
 * cache: {@code TenantScope} builds a new context for every transaction, so the entry dies with the transaction, a retried
 * transaction (a deadlock retry opens a new one) starts empty, and a background writer with no request behind it has the same
 * memo as a route does. A context derived from the transaction's shares the configuration and so the memo.
 *
 * <p><b>It answers "was this row read under the lock", not "what is the row now".</b> A transaction that retires, re-points or
 * deletes a registry row itself must {@link #forget} afterwards ({@code CollectionTombstones} does at every such statement), or a
 * later read in it would be handed the live row it read before. A savepoint that rolls back a read must {@link #forget} too: the
 * rollback released the lock the memo says is held ({@code PgVectorRepository}'s repair and count savepoints run no registry read
 * today and call it anyway, so a future one cannot leave a stale entry).
 */
final class WriteRegistryMemo {

    /** The key in the configuration's data map. Not a field of a collection type: the map is the configuration's. */
    private static final String KEY = "nexus.collection-registry.write-memo";

    private WriteRegistryMemo() {
    }

    @SuppressWarnings("unchecked")
    private static Map<String, CollectionRow> map(DSLContext ctx) {
        return (Map<String, CollectionRow>) ctx.configuration().data().computeIfAbsent(KEY, k -> new ConcurrentHashMap<String, CollectionRow>());
    }

    private static String key(String tenant, String label) {
        return tenant + '|' + label;
    }

    /** The row read under the lock in this transaction, or null. */
    static CollectionRow get(DSLContext ctx, String tenant, String label) {
        return map(ctx).get(key(tenant, label));
    }

    /** Keeps {@code row}, which this transaction has just read {@code FOR KEY SHARE}. */
    static void put(DSLContext ctx, String tenant, String label, CollectionRow row) {
        map(ctx).put(key(tenant, label), row);
    }

    /**
     * Lock order's first half, checked (RDR-229 P3.5a, linda 6(c)): an advisory lock (a sweep gate, the index-run lock, the taxonomy
     * collection lock, a tuple lock, the re-embed start guard) is taken BEFORE the registry row, never after. A transaction that
     * already holds a registry key share and then waits for an advisory lock can close a cycle with a verb that holds the lock and wants
     * the row. It fails only while the census is on ({@link ExpectedCollectionIds#censusOn}, the engine suites), where it is the
     * assertion that every transaction of every writer honours the order; a production transaction pays nothing.
     */
    static void assertNoneHeld(DSLContext ctx, String lock) {
        if (!ExpectedCollectionIds.censusOn()) {
            return;
        }
        Object m = ctx.configuration().data().get(KEY);
        if (m != null && !((Map<?, ?>) m).isEmpty()) {
            throw new IllegalStateException("lock order: " + lock + " is taken after this transaction read a registry row ("
                + ((Map<?, ?>) m).keySet() + "); advisory locks come first, then the registry row");
        }
    }

    private static final String EXEMPT = "nexus.collection-registry.write-exempt";

    /**
     * Tags this transaction exempt from {@link StrictWriterCensus}: it is a verb that locks the registry row itself, a background writer that
     * waits for no lock, or it wrote the registry row. {@code why} is for the reader of the call site.
     */
    static void exempt(DSLContext ctx, String why) {
        ctx.configuration().data().put(EXEMPT, why);
    }

    /** True when this transaction holds a registry row (the memo has an entry) or is tagged exempt. */
    static boolean heldOrExempt(org.jooq.Configuration configuration) {
        if (configuration.data().containsKey(EXEMPT)) {
            return true;
        }
        Object m = configuration.data().get(KEY);
        return m != null && !((Map<?, ?>) m).isEmpty();
    }

    /** Drops every entry of this transaction: it changed the registry, or rolled back to a savepoint. */
    static void forget(DSLContext ctx) {
        exempt(ctx, "the transaction wrote the registry row");
        Object m = ctx.configuration().data().get(KEY);
        if (m != null) {
            ((Map<?, ?>) m).clear();
        }
    }
}
