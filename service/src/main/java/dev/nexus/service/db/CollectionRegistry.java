// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Record7;

import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;

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
 * <p><strong>Correctness: mark-known only after commit.</strong> The only production
 * fill is {@link #require}'s own (reached through {@link #lookup} too), guarded by the
 * eviction count (nexus-hkh1y); {@link #markKnown} stays public as a test seeding seam.
 * The rule below is why that fill may run mid-transaction and why a writer must not
 * cache its own write: a row may be cached only once a committed transaction holds it,
 * never from inside the {@code TenantScope.withTenant} lambda that wrote it ({@link
 * #require} caches a row it just read, whose existence predates the reading
 * transaction, so this transaction's own rollback cannot undo it). If the
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
 *
 * <p><b>One engine per database</b> (nexus-hkh1y). The cache is evicted only in the JVM that made
 * the change, so it is correct only while one engine serves a database. That holds today: production
 * runs exactly one engine JVM for every tenant, on one host behind one edge, and a redeploy stops the
 * old engine before the new one starts (RDR-007's single-host design, per conexus, 2026-10-22); a
 * rehearsal fork runs its own engine against its own database. Replicas or a blue/green overlap on
 * one database would leave the other JVM serving a stale row (a renamed-away name still writable
 * there) and need a TTL or shared invalidation (LISTEN/NOTIFY) first.
 */
public final class CollectionRegistry {

    private static final Map<String, CollectionRow> KNOWN = new ConcurrentHashMap<>();

    /**
     * nexus-hkh1y: a {@link #require} miss reads the row and then caches it, and an evict can land
     * between the two (a supersede or rename commits and evicts while the read is in flight). Caching
     * that read would put back the live row the evict just removed, with no TTL to age it out. Every
     * evict bumps {@link #evictions} under {@link #FILL_LOCK}; a miss notes the count before its read
     * and caches only if it is unchanged, under the same lock, so a read that started before an evict
     * never re-caches. Taken on misses and evicts only, never on a cache hit.
     */
    private static final Object FILL_LOCK = new Object();
    private static long evictions;

    /** Test seam (nexus-hkh1y): runs between a miss's read and its cache fill. */
    static volatile Runnable afterMissReadForTests;

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
     *
     * <p>No production code outside this class calls it (nexus-hkh1y): an unguarded put of a
     * row read before a concurrent evict re-caches the stale row. Production fills go through
     * {@link #require}/{@link #lookup}, whose fill checks the eviction count; this stays public
     * as the seeding seam tests in other packages use.
     */
    public static void markKnown(String tenant, String collection, CollectionRow row) {
        if (row.isSuperseded()) {
            // nexus-mp8ys: a retired name is never cached. The cache answers "may this write
            // proceed without a read", and a revive (POST /collections/upsert) clears
            // superseded_by without touching the cache, so a cached tombstone would go on
            // refusing a name that was revived. Uncached, every read of a tombstone re-reads it.
            KNOWN.remove(key(tenant, collection));
            return;
        }
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
        synchronized (FILL_LOCK) {
            evictions++;
            KNOWN.remove(key(tenant, collection));
        }
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
     * <p>Also the {@code CollectionRegistry} evictor of the test-only tenant reset
     * route (bead nexus-0bl77): after the reset deletes the tenant's
     * {@code catalog_collections} rows, a cached pair would be a stale positive.
     *
     * @param tenant the tenant whose cached entries should be forgotten
     * @return the number of cached pairs removed
     */
    public static int evictTenant(String tenant) {
        String prefix = tenant + '|';
        int[] removed = {0};
        synchronized (FILL_LOCK) {
            evictions++;
            KNOWN.keySet().removeIf(k -> {
                boolean mine = k.startsWith(prefix);
                if (mine) {
                    removed[0]++;
                }
                return mine;
            });
        }
        return removed[0];
    }

    /** Test-only: clears all cached entries. */
    static void clearForTests() {
        synchronized (FILL_LOCK) {
            evictions++;
            KNOWN.clear();
        }
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
     * a round trip once a pair is known-registered); on a cache miss, reads the row
     * from the database directly and caches it so repeat writes to the same
     * collection skip the SELECT too, unless an evict ran between the read and the
     * fill (nexus-hkh1y), in which case the row is returned but not cached.
     *
     * @throws UnregisteredCollectionException if no row exists for the pair
     * @throws IllegalStateException if the row's own {@code dimension} is {@code NULL}
     *         (a walked zero-chunk row, or a dormant/disputed row — {@code
     *         hygiene-002-collection-attributes-walk.xml}) AND its {@code
     *         embedding_model} — NOT NULL, FK-backed to {@code nexus.embedding_models}
     *         since that same changeset — somehow has no {@code embedding_models} row
     *         either (should be unreachable given the FK, but this method never hands
     *         a caller a fabricated dimension; see {@link CollectionRow}'s javadoc)
     */
    public static CollectionRow require(DSLContext ctx, String tenant, String collection) {
        Optional<CollectionRow> hit = cached(tenant, collection);
        if (hit.isPresent()) {
            return hit.get();
        }
        long evictionsBeforeRead;
        synchronized (FILL_LOCK) {
            evictionsBeforeRead = evictions;
        }
        Record7<String, String, String, Integer, String, Integer, String> r = ctx.select(
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE, EMBEDDING_MODELS.DIMENSION,
                CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .leftJoin(EMBEDDING_MODELS)
                .on(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                .and(CATALOG_COLLECTIONS.NAME.eq(collection)))
            .fetchOne();
        if (r == null) {
            throw new UnregisteredCollectionException(tenant, collection);
        }
        // RDR-204 Phase 2 follow-up (bead nexus-ft04v.16, hygiene-002 finding):
        // catalog_collections.dimension is NULL BY DESIGN for a walked zero-chunk row
        // and for a dormant/disputed row with no single agreed dimension --
        // embedding_model is never NULL (NOT NULL + FK to embedding_models since
        // hygiene-002-1), so COALESCE to the model's own seeded dimension rather than
        // ever caching a fabricated sentinel a caller could silently misread as real.
        Integer catalogDim = r.value4();
        Integer modelDim = r.value6();
        Integer dimension = catalogDim != null ? catalogDim : modelDim;
        if (dimension == null) {
            throw new IllegalStateException(
                "collection '" + collection + "' for tenant '" + tenant + "' has no resolvable "
                + "dimension: catalog_collections.dimension is NULL and its embedding_model '"
                + r.value3() + "' has no nexus.embedding_models row to fall back to");
        }
        CollectionRow row = new CollectionRow(r.value1(), r.value2(), r.value3(), dimension, r.value5(), r.value7());
        Runnable seam = afterMissReadForTests;
        if (seam != null) {
            seam.run();
        }
        synchronized (FILL_LOCK) {
            if (evictions == evictionsBeforeRead) {
                markKnown(tenant, collection, row);
            }
        }
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

    /**
     * {@link #require}, for a caller that is about to WRITE chunks into {@code collection}
     * (nexus-mp8ys): refuses a retired name instead of returning it. Reads, listings and the
     * fidelity-preserving collection import ({@code UPSERT_IMPORT}, which legitimately carries
     * tombstoned collections) keep using {@link #require} / {@link #lookup}, which hand a tombstone
     * back with {@link CollectionRow#supersededBy()} set. The quarantine restore writes chunks back
     * into its origin and so refuses a retired origin too ({@code PgVectorRepository#checkRestoreOrigin}).
     * Callers: the manifest row writers (plain and append modes) and the taxonomy centroid upsert.
     *
     * @throws UnregisteredCollectionException if no row exists for the pair
     * @throws CollectionSupersededException if the row names a successor
     */
    public static CollectionRow requireWritable(DSLContext ctx, String tenant, String collection) {
        return refuseIfSuperseded(tenant, collection, require(ctx, tenant, collection));
    }

    /**
     * {@link #requireWritable}, for callers OUTSIDE an open transaction; the write-path twin of
     * {@link #lookup}. A cache hit is always a live row (see {@link #markKnown}), so the hot path
     * costs what {@link #lookup} costs.
     */
    public static CollectionRow lookupWritable(TenantScope scope, String tenant, String collection) {
        return refuseIfSuperseded(tenant, collection, lookup(scope, tenant, collection));
    }

    private static CollectionRow refuseIfSuperseded(String tenant, String collection, CollectionRow row) {
        if (row.isSuperseded()) {
            throw new CollectionSupersededException(tenant, collection, row.supersededBy());
        }
        return row;
    }
}
