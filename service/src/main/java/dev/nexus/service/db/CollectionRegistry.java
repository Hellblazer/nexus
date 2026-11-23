// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;

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
 * there) and need a TTL or shared invalidation (LISTEN/NOTIFY) first. The assumption is enforced since
 * nexus-5tykh: {@link EngineSingletonLock} takes a session advisory lock at boot, so a second engine on the same
 * database refuses to start, and an engine that loses the lock and finds another holder exits. An engine that
 * loses it and takes it back calls {@link #evictAll}, because another engine may have run in the gap.
 *
 * <p><b>RDR-229 P3.5a (bead nexus-p2m1x.23): keyed by id with a label index.</b> A row is cached under its label and under
 * its id; one resolution by either key fills both, an evict by label drops both, and a fill that raced an evict (the
 * nexus-hkh1y guard) is dropped for both. The process-wide cache is one {@link #shared()} registry, which every static method
 * below serves and the route boundary uses; a registry built with {@code new} owns a cache of its own (the isolated registry
 * of a test). Each registry takes {@link Gate3} by constructor and reads it per call ({@link #readerKey}), so the switch from
 * the name to the id needs no restart. Until gate 3 the readers still key on the label.
 */

public final class CollectionRegistry {

    /** The process-wide label index; {@link #SHARED} aliases it, and the static methods are {@code SHARED}'s. */
    private static final Map<String, CollectionRow> KNOWN = new ConcurrentHashMap<>();

    /**
     * RDR-229 P3.5a: the process-wide id index, {@code tenant|collection_id} to the same row {@link #KNOWN} holds under its label.
     * Filled and dropped with {@link #KNOWN} under {@link #FILL_LOCK}. A row that carries no id (the short {@link CollectionRow}
     * constructors) is in the label index only.
     */
    private static final Map<String, CollectionRow> KNOWN_BY_ID = new ConcurrentHashMap<>();

    /**
     * nexus-hkh1y: a {@link #require} miss reads the row and then caches it, and an evict can land
     * between the two (a supersede or rename commits and evicts while the read is in flight). Caching
     * that read would put back the live row the evict just removed, with no TTL to age it out. Every
     * evict bumps the eviction count under {@link #FILL_LOCK}; a miss notes the count before its read
     * and caches only if it is unchanged, under the same lock, so a read that started before an evict
     * never re-caches. Taken on misses and evicts only, never on a cache hit.
     */
    private static final Object FILL_LOCK = new Object();
    private static final long[] EVICTIONS = new long[1];

    /** Test seam (nexus-hkh1y): runs between a miss's read and its cache fill. */
    static volatile Runnable afterMissReadForTests;

    /** The registry over the process-wide cache. Declared after the two indexes it aliases. */
    private static final CollectionRegistry SHARED =
        new CollectionRegistry(Gate3.never(), KNOWN, KNOWN_BY_ID, FILL_LOCK, EVICTIONS);

    private final Gate3 gate3;
    private final Map<String, CollectionRow> known;
    private final Map<String, CollectionRow> knownById;
    private final Object fillLock;
    private final long[] evictions;

    private CollectionRegistry(Gate3 gate3, Map<String, CollectionRow> known, Map<String, CollectionRow> knownById,
                               Object fillLock, long[] evictions) {
        this.gate3 = java.util.Objects.requireNonNull(gate3, "gate3");
        this.known = known;
        this.knownById = knownById;
        this.fillLock = fillLock;
        this.evictions = evictions;
    }

    /**
     * A registry with a cache of its own (empty, and no other registry's), reading {@code gate3} per call. What a test builds;
     * production code uses {@link #shared(Gate3)}, so an evict by a repository reaches the cache the route boundary reads.
     */
    public CollectionRegistry(Gate3 gate3) {
        this(gate3, new ConcurrentHashMap<>(), new ConcurrentHashMap<>(), new Object(), new long[1]);
    }

    /** The process-wide registry, gate 3 never holding: what the static methods below are. */
    public static CollectionRegistry shared() {
        return SHARED;
    }

    /** A registry over the process-wide cache that reads {@code gate3} per call: the route boundary's. */
    public static CollectionRegistry shared(Gate3 gate3) {
        return new CollectionRegistry(gate3, KNOWN, KNOWN_BY_ID, FILL_LOCK, EVICTIONS);
    }

    // ---- the id-keyed API (RDR-229 P3.5a) -------------------------------------------------------------------------------

    /** {@link #require} by label, on this registry's cache. */
    public CollectionRow resolveLabel(DSLContext ctx, String tenant, String label) {
        Optional<CollectionRow> hit = cachedByLabel(tenant, label);
        if (hit.isPresent()) {
            return hit.get();
        }
        return fill(ctx, tenant, CATALOG_COLLECTIONS.NAME.eq(label),
            () -> new UnregisteredCollectionException(tenant, label));
    }

    /**
     * The row of {@code collectionId} in {@code tenant}: the cached one, else read by id and cached under both keys unless an
     * evict ran between the read and the fill (nexus-hkh1y). A retired row is returned (it names its successor) and cached
     * under neither key, as {@link #markKnown} does for a label.
     *
     * @throws UnknownCollectionIdException if the tenant has no row with that id (never an
     *         {@link UnregisteredCollectionException}, which a label with no row raises)
     */
    public CollectionRow resolveId(DSLContext ctx, String tenant, long collectionId) {
        Optional<CollectionRow> hit = cachedById(tenant, collectionId);
        if (hit.isPresent()) {
            return hit.get();
        }
        return fill(ctx, tenant, CATALOG_COLLECTIONS.COLLECTION_ID.eq(collectionId),
            () -> new UnknownCollectionIdException(tenant, collectionId));
    }

    /** {@link #resolveId}, for a caller outside an open transaction: a cache hit opens none, a miss opens one short read. */
    public CollectionRow lookupId(TenantScope scope, String tenant, long collectionId) {
        Optional<CollectionRow> hit = cachedById(tenant, collectionId);
        if (hit.isPresent()) {
            return hit.get();
        }
        return scope.withTenant(tenant, ctx -> resolveId(ctx, tenant, collectionId));
    }

    /** The cached row of {@code label}, if any; never touches the database. */
    public Optional<CollectionRow> cachedByLabel(String tenant, String label) {
        return Optional.ofNullable(known.get(key(tenant, label)));
    }

    /** The cached row of {@code collectionId}, if any; never touches the database. */
    public Optional<CollectionRow> cachedById(String tenant, long collectionId) {
        return Optional.ofNullable(knownById.get(idKey(tenant, collectionId)));
    }

    /** {@link #evict} by label, on this registry's cache; it drops the id entry of the same row too. */
    public void evictLabel(String tenant, String label) {
        synchronized (fillLock) {
            evictions[0]++;
            dropLabel(tenant, label);
        }
    }

    /**
     * Forget every row cached for {@code tenant}, under both keys (RDR-204 bead nexus-ft04v.6). Called post-commit after an
     * {@code embedding_profile} write for that tenant: defense-in-depth, not a correctness requirement, since a profile write never
     * touches an existing {@code catalog_collections} row, so no cached entry is actually stale after one; it makes a
     * registration for that tenant re-verify against the database rather than trust in-process state that predates the profile
     * change. Also the {@code CollectionRegistry} evictor of the test-only tenant reset route (bead nexus-0bl77): after the reset
     * deletes the tenant's rows, a cached row would be a stale positive. Bumps the eviction count, so a miss that began before it
     * caches nothing.
     *
     * @return the number of rows removed, whatever the number of index entries each has
     */
    public int evictTenant(String tenant) {
        String prefix = tenant + '|';
        int[] removed = {0};
        synchronized (fillLock) {
            evictions[0]++;
            known.keySet().removeIf(k -> {
                boolean mine = k.startsWith(prefix);
                if (mine) {
                    removed[0]++;
                }
                return mine;
            });
            knownById.keySet().removeIf(k -> k.startsWith(prefix));
        }
        return removed[0];
    }

    /**
     * What a reader keys its query on: the label while {@link Gate3} does not hold (the name columns, as before P3.5), the id
     * ({@link CollectionRow#collectionId()}) once it does. Decided per call from the injected signal, so the switch needs no
     * restart and drops back if gate 3 does.
     */
    public Object readerKey(CollectionRow row) {
        return gate3.holds() ? (Object) row.collectionId() : row.label();
    }

    private static String key(String tenant, String collection) {
        // Printable delimiter on purpose (wave review): the original '\0' made the version-control tool
        // treat this FILE as binary, so every diff rendered as "Bin N -> M bytes"
        // and logic changes were invisible to text review. '|' is not legal in
        // tenant ids or conformant collection names, so keys cannot collide.
        return tenant + '|' + collection;
    }

    private static String idKey(String tenant, long collectionId) {
        return tenant + '|' + collectionId;
    }

    /** Caches {@code row} under {@code collection} and, when it has an id, under that; see {@link #markKnown}. */
    private void mark(String tenant, String collection, CollectionRow row) {
        synchronized (fillLock) {
            if (row.isSuperseded()) {
                // nexus-mp8ys: a retired name is never cached. The cache answers "may this write
                // proceed without a read", and a revive (POST /collections/upsert) clears
                // superseded_by without touching the cache, so a cached tombstone would go on
                // refusing a name that was revived. Uncached, every read of a tombstone re-reads it.
                dropLabel(tenant, collection);
                return;
            }
            CollectionRow previous = known.put(key(tenant, collection), row);
            if (previous != null && previous.collectionId() > 0 && previous.collectionId() != row.collectionId()) {
                // the label now names another row (a delete and a re-registration): the old id must not resolve
                knownById.remove(idKey(tenant, previous.collectionId()));
            }
            if (row.collectionId() > 0) {
                knownById.put(idKey(tenant, row.collectionId()), row);
            }
        }
    }

    /** Drops the label entry and the id entry of the row it holds. The caller holds {@link #fillLock}. */
    private void dropLabel(String tenant, String collection) {
        CollectionRow gone = known.remove(key(tenant, collection));
        if (gone != null && gone.collectionId() > 0) {
            knownById.remove(idKey(tenant, gone.collectionId()));
        }
    }

    /**
     * The miss path of {@link #resolveLabel} and {@link #resolveId}: read the one row {@code where} selects (by label or by
     * id), build its {@link CollectionRow}, and cache it under both keys unless an evict ran between the read and the fill
     * (nexus-hkh1y), in which case the row is returned and not cached.
     */
    private CollectionRow fill(DSLContext ctx, String tenant, Condition where,
                               java.util.function.Supplier<RuntimeException> missing) {
        long evictionsBeforeRead;
        synchronized (fillLock) {
            evictionsBeforeRead = evictions[0];
        }
        Field<Integer> modelDimension = EMBEDDING_MODELS.DIMENSION.as("model_dimension");
        Record r = ctx.select(
                CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE, modelDimension,
                CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .leftJoin(EMBEDDING_MODELS)
                .on(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(where))
            .fetchOne();
        if (r == null) {
            throw missing.get();
        }
        String label = r.get(CATALOG_COLLECTIONS.NAME);
        // RDR-204 Phase 2 follow-up (bead nexus-ft04v.16, hygiene-002 finding):
        // catalog_collections.dimension is NULL BY DESIGN for a walked zero-chunk row
        // and for a dormant/disputed row with no single agreed dimension --
        // embedding_model is never NULL (NOT NULL + FK to embedding_models since
        // hygiene-002-1), so COALESCE to the model's own seeded dimension rather than
        // ever caching a fabricated sentinel a caller could silently misread as real.
        Integer catalogDim = r.get(CATALOG_COLLECTIONS.DIMENSION);
        Integer dimension = catalogDim != null ? catalogDim : r.get(modelDimension);
        if (dimension == null) {
            throw new IllegalStateException(
                "collection '" + label + "' for tenant '" + tenant + "' has no resolvable "
                + "dimension: catalog_collections.dimension is NULL and its embedding_model '"
                + r.get(CATALOG_COLLECTIONS.EMBEDDING_MODEL) + "' has no nexus.embedding_models row to fall back to");
        }
        Long id = r.get(CATALOG_COLLECTIONS.COLLECTION_ID);
        CollectionRow row = new CollectionRow(r.get(CATALOG_COLLECTIONS.CONTENT_TYPE), r.get(CATALOG_COLLECTIONS.OWNER_ID),
            r.get(CATALOG_COLLECTIONS.EMBEDDING_MODEL), dimension, r.get(CATALOG_COLLECTIONS.LIFECYCLE_STATE),
            r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY), id == null ? 0L : id, label);
        Runnable seam = afterMissReadForTests;
        if (seam != null) {
            seam.run();
        }
        synchronized (fillLock) {
            if (evictions[0] == evictionsBeforeRead) {
                mark(tenant, label, row);
            }
        }
        return row;
    }

    // ---- the process-wide registry, as static methods (every caller that predates the id) ---------------------------------

    /** True when {@code (tenant, collection)} is known-registered — safe to skip the INSERT. */
    public static boolean isKnown(String tenant, String collection) {
        return cached(tenant, collection).isPresent();
    }

    /**
     * The cached row for {@code (tenant, collection)}, if any is known-registered.
     * Never touches the database — a pure in-process read.
     */
    public static Optional<CollectionRow> cached(String tenant, String collection) {
        return SHARED.cachedByLabel(tenant, collection);
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
     * as the seeding seam tests in other packages use. A retired row is not cached (nexus-mp8ys).
     */
    public static void markKnown(String tenant, String collection, CollectionRow row) {
        SHARED.mark(tenant, collection, row);
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
        SHARED.evictLabel(tenant, collection);
    }

    /**
     * Forget every cached row, for every tenant (nexus-5tykh). {@link EngineSingletonLock} calls it after the engine
     * lost its one-engine-per-database lock and took it back: another engine may have served the database in the
     * gap and changed or superseded rows this cache still holds. Bumps the eviction count, so a miss whose read
     * began before this call caches nothing.
     *
     * @return the number of cached rows removed
     */
    public static int evictAll() {
        synchronized (FILL_LOCK) {
            EVICTIONS[0]++;
            int removed = KNOWN.size();
            KNOWN.clear();
            KNOWN_BY_ID.clear();
            return removed;
        }
    }

    /** Test-only: clears all cached entries. */
    static void clearForTests() {
        synchronized (FILL_LOCK) {
            EVICTIONS[0]++;
            KNOWN.clear();
            KNOWN_BY_ID.clear();
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
        return SHARED.resolveLabel(ctx, tenant, collection);
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

    /**
     * The {@code lifecycle_state} of the {@code (tenant, collection)} row, or empty when the pair has no row
     * (RDR-229 P2.5). The guards on an existing collection ({@link QuarantineOrigin}, the vector routes, the
     * reaper's split) ask this instead of testing the {@code quarantine-} name prefix. Unlike {@link #require} it
     * never throws for a missing row: the caller decides what a row-less name means (the guards judge it by its
     * prefix, as they always did).
     *
     * <p>Source of the answer: the in-process cache when the row is cached (every live, non-retired row a write or
     * a {@link #lookup} has touched), else ONE single-column {@code SELECT} on the caller's own transaction. A miss
     * is not cached here: {@link #require} does the guarded fill and needs the dimension; this is a read of one
     * column and fills nothing, so it adds no cache state and no invalidation duty. A retired (superseded) row is
     * never cached ({@link #markKnown}), so it is always read.
     *
     * <p>Why the cache cannot make the quarantine answer stale: {@code 'quarantine'} is written only at INSERT
     * (registration, the SQL movers), so a cached answer to "is it quarantine" cannot go stale. The one Java writer
     * that changes {@code lifecycle_state} in place is the ghost sweep, and it moves a row between {@code live}
     * and {@code dormant}, which does not change that answer. A future writer that flips {@code lifecycle_state}
     * to or from {@code 'quarantine'} after INSERT would make this cache wrong and MUST invalidate it
     * ({@link #evict}).
     */
    public static Optional<String> lifecycleState(DSLContext ctx, String tenant, String collection) {
        if (collection == null) {
            return Optional.empty();
        }
        Optional<CollectionRow> hit = cached(tenant, collection);
        if (hit.isPresent()) {
            return Optional.ofNullable(hit.get().lifecycleState());
        }
        return Optional.ofNullable(ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(collection)))
            .fetchOne(CATALOG_COLLECTIONS.LIFECYCLE_STATE));
    }

    /**
     * {@link #lifecycleState(DSLContext, String, String)} for callers OUTSIDE an open transaction; a cache hit
     * opens none, a miss opens one short read transaction through {@code scope}.
     */
    public static Optional<String> lifecycleState(TenantScope scope, String tenant, String collection) {
        if (collection == null) {
            return Optional.empty();
        }
        Optional<CollectionRow> hit = cached(tenant, collection);
        if (hit.isPresent()) {
            return Optional.ofNullable(hit.get().lifecycleState());
        }
        return scope.withTenant(tenant, ctx -> lifecycleState(ctx, tenant, collection));
    }

    private static CollectionRow refuseIfSuperseded(String tenant, String collection, CollectionRow row) {
        if (row.isSuperseded()) {
            throw new CollectionSupersededException(tenant, collection, row.supersededBy());
        }
        return row;
    }
}
