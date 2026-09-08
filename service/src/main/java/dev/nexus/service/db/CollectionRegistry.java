// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Record5;

import java.util.Map;
import java.util.Optional;
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
 * <p><strong>RDR-204 Phase 2 (bead nexus-ft04v.14): the cache holds the ROW, not
 * presence.</strong> A caller that already has the {@code (tenant, collection)}
 * pair confirmed registered increasingly also needs to know WHAT it says —
 * {@code content_type}/{@code owner_id}/{@code embedding_model}/{@code dimension}/
 * {@code lifecycle_state} — to route a write or pick an embedder without a second
 * round trip. Presence was a strict subset of this: {@code isKnown} is now exactly
 * {@code cached(...).isPresent()}. See {@link CollectionRow} for the field
 * semantics.
 *
 * <p><strong>Correctness: mark-known only after commit.</strong> Callers MUST call
 * {@link #markKnown} only after the transaction that performed the (possibly
 * skipped) registration has committed successfully — never from inside the
 * {@code TenantScope.withTenant} lambda itself, UNLESS the row being marked was
 * already committed by an EARLIER transaction (e.g. {@link #require} caching a row
 * it just read mid-transaction — that row's existence predates this transaction,
 * so marking it known cannot be undone by this transaction's own rollback). If the
 * enclosing transaction later rolls back (e.g. an unrelated error later in the
 * same batch), a row THIS transaction wrote was never actually persisted; marking
 * the cache eagerly for THAT row would cause every subsequent writer to skip
 * re-registration forever, silently starving the row. Marking post-commit means a
 * rollback simply leaves the pair unmarked, so the next writer retries
 * registration as usual.
 *
 * <p>Process-local and unbounded by design: {@code (tenant, collection)} pairs are
 * low-cardinality relative to write volume (one entry per physical collection ever
 * touched by this process — production tenant {@code nexus} carries 229 rows), so
 * an unbounded {@link ConcurrentHashMap}-backed map is the right trade-off — no
 * eviction complexity, no cache-miss storms. Caching the row instead of a boolean
 * multiplies the per-entry footprint (three {@code String} references, an
 * {@code int}, and one more {@code String} versus a single set membership) but not
 * the CARDINALITY: the number of distinct keys is unchanged, so the map stays
 * bounded by the same "collections this process has actually touched" argument —
 * at real-world scale (hundreds of collections per tenant, low hundreds of
 * tenants per process) the whole cache remains a few megabytes at most.
 */
public final class CollectionRegistry {

    private static final Map<String, CollectionRow> KNOWN = new ConcurrentHashMap<>();

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
        return cached(tenant, collection).isPresent();
    }

    /**
     * The cached row for {@code (tenant, collection)}, if any is known-registered.
     * Never touches the database — a pure in-process read.
     */
    public static Optional<CollectionRow> cached(String tenant, String collection) {
        return Optional.ofNullable(KNOWN.get(key(tenant, collection)));
    }

    /**
     * Record {@code (tenant, collection)} as registered, caching {@code row}.
     * Callers MUST only invoke this AFTER the enclosing transaction has committed
     * (see class doc) — unless {@code row} reflects a pair confirmed registered by
     * an EARLIER, already-committed transaction (as {@link #require} does on a
     * cache miss).
     */
    public static void markKnown(String tenant, String collection, CollectionRow row) {
        KNOWN.put(key(tenant, collection), row);
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
        KNOWN.keySet().removeIf(k -> k.startsWith(prefix));
    }

    /** Test-only: clears all cached entries. */
    static void clearForTests() {
        KNOWN.clear();
    }

    /**
     * Fail loud when {@code (tenant, collection)} has no row in {@code
     * nexus.catalog_collections} (RDR-204 Phase 1, bead nexus-ft04v.7); return its
     * cached or freshly-read row otherwise (RDR-204 Phase 2, bead nexus-ft04v.14).
     *
     * <p>Replaces the seven {@code ensureCollectionRegistered}-shaped stub inserts
     * this bead retires: those wrote a blank-attribute row on first write so their
     * own {@code (tenant, collection)} FK could never fail; this checks existence
     * instead and never writes anything. Checks the in-process cache first (skips
     * a round trip once a pair is known-registered — {@code CatalogRepository
     * .upsertCollection} and siblings mark it via {@link #markKnown} post-commit);
     * on a cache miss, reads the row from the database directly and caches it so
     * repeat writes to the same collection skip the SELECT too.
     *
     * @throws UnregisteredCollectionException if no row exists for the pair
     */
    public static CollectionRow require(DSLContext ctx, String tenant, String collection) {
        Optional<CollectionRow> hit = cached(tenant, collection);
        if (hit.isPresent()) {
            return hit.get();
        }
        Record5<String, String, String, Integer, String> r = ctx.select(
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                .and(CATALOG_COLLECTIONS.NAME.eq(collection)))
            .fetchOne();
        if (r == null) {
            throw new UnregisteredCollectionException(tenant, collection);
        }
        Integer dimension = r.value4();
        CollectionRow row = new CollectionRow(
            r.value1(), r.value2(), r.value3(), dimension == null ? 0 : dimension, r.value5());
        markKnown(tenant, collection, row);
        return row;
    }

    /**
     * Wrapper over {@link #require} for callers that only need the fail-loud
     * existence check and do not (yet) consume the row. Kept so the seven
     * pre-existing call sites (RDR-204 Phase 1, bead nexus-ft04v.7) keep compiling
     * unchanged.
     *
     * @throws UnregisteredCollectionException if no row exists for the pair
     */
    public static void requireRegistered(DSLContext ctx, String tenant, String collection) {
        require(ctx, tenant, collection);
    }

    /**
     * {@link #require}, for callers OUTSIDE an open transaction (RDR-204 Phase 2,
     * bead nexus-ft04v.14) — e.g. a post-commit re-cache after a write that did not
     * itself carry the row's final attributes forward in Java. On a cache miss,
     * opens its own short read transaction via {@code scope}.
     *
     * @throws UnregisteredCollectionException if no row exists for the pair
     */
    public static CollectionRow lookup(TenantScope scope, String tenant, String collection) {
        Optional<CollectionRow> hit = cached(tenant, collection);
        if (hit.isPresent()) {
            return hit.get();
        }
        return scope.withTenant(tenant, ctx -> require(ctx, tenant, collection));
    }
}
