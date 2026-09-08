// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * Bead nexus-h8rf6.2 — in-process cache of {@code (tenant, collection)} pairs KNOWN
 * to already have a row in {@code nexus.catalog_collections}.
 *
 * <p><strong>Why this exists.</strong> Both {@code ChashRepository
 * .ensureCollectionRegistered} and {@code PgVectorRepository.upsertChunksInternal}
 * issue {@code INSERT INTO nexus.catalog_collections ... ON CONFLICT (tenant_id,
 * name) DO NOTHING} on EVERY batch write, so the collection stub exists before the
 * chash/chunk row lands. PostgreSQL's {@code ON CONFLICT} clause takes a value lock
 * on the conflicting unique-index entry to safely decide whether to apply DO
 * NOTHING; when two concurrent transactions target the SAME {@code (tenant_id,
 * name)} row — the ordinary case for one repo's indexing run, which writes chash
 * and vector batches for ONE physical collection over and over — the second
 * transaction BLOCKS until the first commits or rolls back, even though the
 * eventual outcome is a no-op. Because the winning transaction typically keeps
 * its connection open for the rest of its own batch (chunk/chash writes), every
 * other concurrent writer to that collection sits blocked HOLDING its own pooled
 * connection for that whole window. Under sustained concurrent indexing this
 * convoy exhausts the shared HikariCP pool: unrelated requests then fail
 * {@code dataSource.getConnection()} with {@code SQLTransientConnectionException}
 * ("Connection is not available, request timed out"), which surfaces as an
 * opaque 500 (reproduced by {@code ChashVectorConcurrencyTest}).
 *
 * <p><strong>Fix shape.</strong> Once a {@code (tenant, collection)} pair is known
 * to be registered, every subsequent write skips the redundant
 * {@code INSERT ... ON CONFLICT} entirely — eliminating the repeated same-row
 * lock-wait for the remainder of the process's lifetime. Only the FIRST
 * registration (racing among whichever concurrent requests reach it first) still
 * pays the lock-wait cost; that window is small and bounded by however many
 * requests happen to race at that exact instant, not by the length of the run.
 *
 * <p><strong>Correctness: mark-known only after commit.</strong> Callers MUST call
 * {@link #markKnown} only after the transaction that performed the (possibly
 * skipped) registration has committed successfully — never from inside the
 * {@code TenantScope.withTenant} lambda itself. If the enclosing transaction later
 * rolls back (e.g. an unrelated error later in the same batch), the
 * {@code catalog_collections} row was never actually persisted; marking the cache
 * eagerly would cause every subsequent writer to skip re-registration forever,
 * silently starving the row. Marking post-commit means a rollback simply leaves
 * the pair unmarked, so the next writer retries registration as usual.
 *
 * <p>Process-local and unbounded by design: {@code (tenant, collection)} pairs are
 * low-cardinality relative to write volume (one entry per physical collection ever
 * touched by this process), so an unbounded {@link ConcurrentHashMap}-backed set is
 * the right trade-off — no eviction complexity, no cache-miss storms.
 */
public final class CollectionRegistry {

    private static final Set<String> KNOWN = ConcurrentHashMap.newKeySet();

    private CollectionRegistry() {}

    private static String key(String tenant, String collection) {
        // Printable delimiter on purpose (wave review): the original '\0' made git
        // treat this FILE as binary, so every diff rendered as "Bin N -> M bytes"
        // and logic changes were invisible to text review. '|' is not legal in
        // tenant ids or conformant collection names, so keys cannot collide.
        return tenant + '|' + collection;
    }

    /** True when {@code (tenant, collection)} is known-registered — safe to skip the INSERT. */
    public static boolean isKnown(String tenant, String collection) {
        return KNOWN.contains(key(tenant, collection));
    }

    /**
     * Record {@code (tenant, collection)} as registered. Callers MUST only invoke
     * this AFTER the enclosing transaction has committed (see class doc).
     */
    public static void markKnown(String tenant, String collection) {
        KNOWN.add(key(tenant, collection));
    }

    /**
     * Forget {@code (tenant, collection)} — MUST be called after any commit that
     * DELETEs the {@code catalog_collections} row ({@code CatalogRepository
     * .deleteCollection}; the canonical branch of {@code CatalogRepository
     * .renameCollection}, which deletes the OLD row). A stale entry would make
     * every subsequent writer skip re-registration for a later same-named
     * collection, landing chash/chunk rows with no registry stub — the exact
     * silent-skip failure the fail-loud registration design exists to prevent.
     *
     * <p>Same post-commit discipline as {@link #markKnown}: evict only after the
     * deleting transaction has committed. The small window between commit and
     * eviction is fail-loud, not silent — a concurrent writer that skips
     * registration in that window hits the {@code ON DELETE RESTRICT} FK and
     * errors rather than writing orphaned rows.
     */
    public static void evict(String tenant, String collection) {
        KNOWN.remove(key(tenant, collection));
    }

    /**
     * Forget every {@code (tenant, collection)} pair cached for {@code tenant}
     * (RDR-204 bead nexus-ft04v.6). Called post-commit after an
     * {@code embedding_profile} write for that tenant — defense-in-depth, not a
     * correctness requirement: a profile write never touches an existing
     * {@code catalog_collections} row (the row records the model its vectors
     * were already embedded with; the profile only governs what NEW
     * registrations get), so no cached entry is actually stale after one. This
     * exists so a subsequent registration for that tenant always re-verifies
     * against the database rather than trusting in-process state that predates
     * the profile change, mirroring the post-commit discipline of {@link
     * #evict}.
     *
     * @param tenant the tenant whose cached entries should be forgotten
     */
    public static void evictTenant(String tenant) {
        String prefix = tenant + '|';
        KNOWN.removeIf(k -> k.startsWith(prefix));
    }

    /** Test-only: clears all cached entries. */
    static void clearForTests() {
        KNOWN.clear();
    }

    /**
     * Fail loud when {@code (tenant, collection)} has no row in {@code
     * nexus.catalog_collections} (RDR-204 Phase 1, bead nexus-ft04v.7).
     *
     * <p>Replaces the seven {@code ensureCollectionRegistered}-shaped stub inserts
     * this bead retires: those wrote a blank-attribute row on first write so their
     * own {@code (tenant, collection)} FK could never fail; this checks existence
     * instead and never writes anything. Checks the in-process cache first (skips
     * a round trip once a pair is known-registered by a real registration —
     * {@code CatalogRepository.upsertCollection} and siblings mark it via {@link
     * #markKnown} post-commit); on a cache miss, asks the database directly and
     * marks the cache on a hit so repeat writes to the same collection skip the
     * SELECT too.
     *
     * @throws UnregisteredCollectionException if no row exists for the pair
     */
    public static void requireRegistered(DSLContext ctx, String tenant, String collection) {
        if (isKnown(tenant, collection)) {
            return;
        }
        boolean exists = ctx.fetchExists(
            ctx.selectOne()
               .from(CATALOG_COLLECTIONS)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                   .and(CATALOG_COLLECTIONS.NAME.eq(collection))));
        if (!exists) {
            throw new UnregisteredCollectionException(tenant, collection);
        }
        markKnown(tenant, collection);
    }
}
