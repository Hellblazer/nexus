// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SortField;
import org.jooq.impl.DSL;

import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.OptionalLong;
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
 * of a test). The registry has no gate-3 dependency (linda 2026-11-23): which key a reader queries on, the label or the id, is
 * the reader's own choice from the one {@code Gate3} its repository is given, and the registry only serves both.
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

    private static final org.slf4j.Logger log = org.slf4j.LoggerFactory.getLogger(CollectionRegistry.class);

    /**
     * A write's registry row read is {@code FOR KEY SHARE} with no timeout of its own: an ingest writer waits as long as a retiring verb holds the row
     * (an operator's gc route is bounded instead, {@link GcCollectionReads}: it can be retried, while an ingest writer should not fail behind a
     * rare rename). The wait stays unbounded but is not silent: a read that took this long or longer is logged once it returns,
     * {@code event=registry_wait_slow}, with the collection, its id, the wait in ms and the route.
     */
    private static volatile long slowWaitMs = 1_000;

    /** Test-only: the wait, in ms, from which a registry read is logged; restore 1000. Never set by production code. */
    static void setSlowWaitMsForTests(long ms) {
        slowWaitMs = ms;
    }

    private static void noteWait(long startNanos, String tenant, java.util.function.Supplier<Object> collection,
                                 java.util.function.Supplier<Object> collectionId) {
        long ms = (System.nanoTime() - startNanos) / 1_000_000L;
        if (ms >= slowWaitMs) {
            log.warn("event=registry_wait_slow tenant={} collection={} collection_id={} wait_ms={} route={}", tenant,
                String.valueOf(collection.get()).replaceAll("\\p{Cntrl}", "?"), collectionId.get(), ms,
                ExpectedCollectionIds.route().replaceAll("\\p{Cntrl}", "?"));
        }
    }
    private static final long[] EVICTIONS = new long[1];

    /** Test seam (nexus-hkh1y): runs between a miss's read and its cache fill. */
    static volatile Runnable afterMissReadForTests;

    /** The registry over the process-wide cache. Declared after the two indexes it aliases. */
    private static final CollectionRegistry SHARED =
        new CollectionRegistry(KNOWN, KNOWN_BY_ID, FILL_LOCK, EVICTIONS);

    private final Map<String, CollectionRow> known;
    private final Map<String, CollectionRow> knownById;
    private final Object fillLock;
    private final long[] evictions;

    private CollectionRegistry(Map<String, CollectionRow> known, Map<String, CollectionRow> knownById,
                               Object fillLock, long[] evictions) {
        this.known = known;
        this.knownById = knownById;
        this.fillLock = fillLock;
        this.evictions = evictions;
    }

    /**
     * A registry with a cache of its own (empty, and no other registry's). What a test builds;
     * production code uses {@link #shared()}, so an evict by a repository reaches the cache the route boundary reads.
     */
    public CollectionRegistry() {
        this(new ConcurrentHashMap<>(), new ConcurrentHashMap<>(), new Object(), new long[1]);
    }

    /** The process-wide registry: what the static methods below are, and what the route boundary and every evict use. */
    public static CollectionRegistry shared() {
        return SHARED;
    }

    /**
     * The order in which a label held by several rows resolves to one: the live row first, then the NEWEST tombstone (linda's
     * corrected Q3 ruling, 2026-11-23: no resolver filters strictly to live in Phase 3). "Live" means not superseded,
     * {@code superseded_by = ''} and {@code superseded_by_id IS NULL}, and not {@code lifecycle_state = 'live'}: a quarantined or
     * trashed row that is not superseded still holds its label. Every label read that must pick one row ({@link #resolveLabel},
     * {@link #lifecycleState}, {@code CollectionPurgeScope#resolve}) orders by this and takes the first row, so the rule is
     * written once and a change to what "live" means is a change here. A by-id read selects one row by its key and needs no order.
     */
    public static List<SortField<?>> labelResolutionOrder() {
        return List.of(DSL.when(notSuperseded(), 1).otherwise(0).desc(), CATALOG_COLLECTIONS.COLLECTION_ID.desc());
    }

    /** The one definition of "live" for a label read: the row names no successor ({@link #labelResolutionOrder}). */
    public static Condition notSuperseded() {
        return CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("").and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNull());
    }

    // ---- the id-keyed API (RDR-229 P3.5a) -------------------------------------------------------------------------------

    /**
     * {@link #require} by label, on this registry's cache. While the request named this label by {@code collection_id}
     * ({@link ExpectedCollectionIds}), it resolves THAT id instead, in the caller's transaction and {@code FOR KEY SHARE}
     * ({@link #readRequested}), never the label.
     */
    public CollectionRow resolveLabel(DSLContext ctx, String tenant, String label) {
        OptionalLong requested = ExpectedCollectionIds.of(label);
        if (requested.isPresent()) {
            return readRequested(ctx, tenant, label, requested.getAsLong(), true);
        }
        Optional<CollectionRow> hit = cachedByLabel(tenant, label);
        if (hit.isPresent()) {
            return hit.get();
        }
        return fill(ctx, tenant, CATALOG_COLLECTIONS.NAME.eq(label), labelResolutionOrder(), false,
            () -> new UnregisteredCollectionException(tenant, label));
    }

    /**
     * The row a request named by {@code collection_id} (and resolved to {@code label} at the route boundary), read in the
     * caller's transaction with {@code FOR KEY SHARE} (a delete of the row waits for that transaction, so the id cannot be freed
     * and its label taken again underneath the write). Never from the cache, which cannot hold a lock, and never by the label. No
     * row is {@link UnknownCollectionIdException}; a retired row is {@link CollectionRetiredIdException} naming its successor by
     * {@code superseded_by_id}; a row of another id is {@link CollectionIdMismatchException}; a live row is returned, under its
     * CURRENT label, whatever label the route boundary resolved: the id is the identity (linda's ruling), so a one-row rename that
     * landed between the boundary and this read changes nothing about where the write goes, and the write stamps the row's current
     * label ({@link CollectionRow#label()}). The one refusal on a label is for a request that carried a label of its own beside the id
     * ({@link ExpectedCollectionIds#clientNamed}) which the row no longer bears: {@link CollectionIdMismatchException}, 409.
     *
     * <p>The one place every in-transaction by-id read goes through ({@link #resolveLabel}, {@link CollectionIds#require}). A
     * second read of the same label in one transaction answers from the transaction's memo: the lock is already held.
     *
     * @param consume true for a read in the WRITE's own transaction, which marks the id consumed ({@link ExpectedCollectionIds});
     *        false for an outside-a-transaction lookup's short read, which must not count
     * @throws CollectionIdMismatchException if the row the read returns is not the row of that id (a registry defect, 500), or the
     *         request carried a label of its own that the row no longer bears (409)
     */
    CollectionRow readRequested(DSLContext ctx, String tenant, String label, long collectionId, boolean consume) {
        CollectionRow memo = WriteRegistryMemo.get(ctx, tenant, label);
        if (memo != null && memo.collectionId() == collectionId) {
            if (consume) {
                ExpectedCollectionIds.consumed(ctx, label);
            }
            return memo;
        }
        if (consume) {
            ExpectedCollectionIds.beforeTransactionRead();
            noteWriteRead(tenant, label);
        }
        // a write's own read locks the row (FOR KEY SHARE); an outside-a-transaction lookup's short read holds no lock past its
        // own commit, so it takes none: the lock would be useless, and its row lock assigns the short transaction an xid, which
        // makes it a "write" to the per-transaction census
        CollectionRow row = fill(ctx, tenant, CATALOG_COLLECTIONS.COLLECTION_ID.eq(collectionId), List.of(), consume,
            () -> new UnknownCollectionIdException(tenant, collectionId));
        if (row.collectionId() != collectionId) {
            throw new CollectionIdMismatchException(collectionId, row.collectionId());
        }
        if (!row.label().equals(label) && ExpectedCollectionIds.clientNamed(label)) {
            throw new CollectionIdMismatchException(collectionId, label, row.label());
        }
        if (row.isSuperseded()) {
            throw new CollectionRetiredIdException(tenant, collectionId, row.label(),
                CollectionSuccessor.of(ctx, tenant, collectionId));
        }
        WriteRegistryMemo.put(ctx, tenant, label, row);
        WriteRegistryMemo.put(ctx, tenant, row.label(), row);   // a writer that stamps the current label asks for it by that name
        if (consume) {
            ExpectedCollectionIds.consumed(ctx, label);
            ExpectedCollectionIds.afterTransactionRead();
        }
        return row;
    }

    /**
     * The registry check of a WRITE transaction (RDR-229 P3.5a, linda's option (b)): the row of {@code label} read {@code FOR KEY
     * SHARE} in {@code ctx}'s own transaction, once, whatever the request names. A request by {@code collection_id} reads that id
     * ({@link #readRequested}); a request by label reads the row the label resolves to ({@link #labelResolutionOrder}, the live row
     * first), and a read of the same label later in the transaction answers from {@link WriteRegistryMemo}. The lock is what makes
     * the write live at commit: a verb that retires the row ({@code CollectionTombstones#lockRows}, {@code FOR UPDATE}) waits for
     * every writer holding it, and a writer that arrives after the verb reads the retired row. It is the same lock the foreign key
     * takes on the first child insert, taken first, so it stays ahead of the write's child-row locks in every writer.
     *
     * <p>A reader (a transaction that writes nothing under the collection) keeps {@link #resolveLabel}, which answers from the cache
     * and takes no lock. A write that runs outside any transaction of its own has no registry check to make here.
     *
     * @throws UnregisteredCollectionException if no row has the label (a request by label); the id errors of {@link #readRequested}
     *         for a request by id
     */
    public CollectionRow requireForWriteRow(DSLContext ctx, String tenant, String label) {
        return lockForWrite(ctx, tenant, label).orElseThrow(() -> new UnregisteredCollectionException(tenant, label));
    }

    /**
     * {@link #requireForWriteRow} for a write that never refused an unregistered label (the deletes: a label no row has removes
     * nothing and answers 200): empty when a request by label finds no row, with nothing to lock. A request by id still answers its
     * typed 404 or 409. A retired row of a label is returned, as {@link #resolveLabel} returns it: only a request by its id is refused.
     */
    public Optional<CollectionRow> requireForWriteIfPresentRow(DSLContext ctx, String tenant, String label) {
        return lockForWrite(ctx, tenant, label);
    }

    private Optional<CollectionRow> lockForWrite(DSLContext ctx, String tenant, String label) {
        OptionalLong requested = ExpectedCollectionIds.of(label);
        if (requested.isPresent()) {
            return Optional.of(readRequested(ctx, tenant, label, requested.getAsLong(), true));
        }
        CollectionRow memo = WriteRegistryMemo.get(ctx, tenant, label);
        if (memo != null) {
            return Optional.of(memo);
        }
        noteWriteRead(tenant, label);
        CollectionRow row = fillOrNull(ctx, tenant, CATALOG_COLLECTIONS.NAME.eq(label), labelResolutionOrder(), true, false);
        if (row != null) {
            WriteRegistryMemo.put(ctx, tenant, label, row);
        }
        return Optional.ofNullable(row);
    }

    /**
     * The registry check of a write transaction that touches SEVERAL collections (a remap batch's sources and targets, an assign
     * batch's source collections): one statement reads every row it names {@code FOR KEY SHARE} in {@code collection_id} order, the
     * order a retiring verb locks its rows in ({@code CollectionTombstones#lockRows}), and memoises each, so the per-label checks that
     * follow answer from the memo. Locking one row after another in the request's order instead would let a writer holding the key
     * share of the higher id and waiting for the lower one meet a verb that holds the lower and waits for the higher: a cycle. Names
     * a request gave by id read that id ({@link #readRequested}'s refusals: no row 404, a retired row 409, a row renamed under the request
     * {@link CollectionIdMismatchException} for a label the client sent); a label no row has is simply not memoised, and the per-label check that follows raises
     * its own answer. Call it before any row work of the transaction.
     */
    void lockAllForWrite(DSLContext ctx, String tenant, java.util.Collection<String> labels) {
        java.util.Set<String> todo = new java.util.LinkedHashSet<>();
        for (String label : labels) {
            if (label == null || label.isEmpty()) {
                continue;
            }
            if (WriteRegistryMemo.get(ctx, tenant, label) != null) {
                if (ExpectedCollectionIds.of(label).isPresent()) {
                    ExpectedCollectionIds.consumed(ctx, label);
                }
                continue;
            }
            todo.add(label);
        }
        if (todo.size() < 2) {
            todo.forEach(label -> lockForWrite(ctx, tenant, label));
            return;
        }
        List<Long> namedIds = new java.util.ArrayList<>();
        List<String> unnamed = new java.util.ArrayList<>();
        for (String label : todo) {
            OptionalLong id = ExpectedCollectionIds.of(label);
            if (id.isPresent()) {
                namedIds.add(id.getAsLong());
            } else {
                unnamed.add(label);
            }
        }
        if (!namedIds.isEmpty()) {
            ExpectedCollectionIds.beforeTransactionRead();
        }
        noteWriteRead(tenant, String.join(",", todo));
        Condition match = DSL.falseCondition();
        if (!namedIds.isEmpty()) {
            match = match.or(CATALOG_COLLECTIONS.COLLECTION_ID.in(namedIds));
        }
        if (!unnamed.isEmpty()) {
            match = match.or(CATALOG_COLLECTIONS.NAME.in(unnamed));
        }
        Field<Integer> modelDimension = EMBEDDING_MODELS.DIMENSION.as("model_dimension");
        long waitStarted = System.nanoTime();
        List<CollectionRow> rows = ctx.select(
                CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE, modelDimension,
                CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .leftJoin(EMBEDDING_MODELS)
                .on(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(match))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID)
            .forKeyShare().of(CATALOG_COLLECTIONS)
            .fetch(r -> rowOf(r, modelDimension, tenant));
        noteWait(waitStarted, tenant, () -> rows.stream().map(CollectionRow::label).toList(), () -> rows.stream().map(CollectionRow::collectionId).toList());
        for (String label : todo) {
            OptionalLong named = ExpectedCollectionIds.of(label);
            CollectionRow chosen = null;
            if (named.isPresent()) {
                long id = named.getAsLong();
                chosen = rows.stream().filter(r -> r.collectionId() == id).findFirst()
                    .orElseThrow(() -> new UnknownCollectionIdException(tenant, id));
                if (!chosen.label().equals(label) && ExpectedCollectionIds.clientNamed(label)) {
                    throw new CollectionIdMismatchException(id, label, chosen.label());
                }
                if (chosen.isSuperseded()) {
                    throw new CollectionRetiredIdException(tenant, id, chosen.label(), CollectionSuccessor.of(ctx, tenant, id));
                }
            } else {
                // the live row of the label first, else the newest tombstone: labelResolutionOrder
                for (CollectionRow candidate : rows) {
                    if (!candidate.label().equals(label)) {
                        continue;
                    }
                    if (chosen == null || (chosen.isSuperseded() && !candidate.isSuperseded())
                        || (chosen.isSuperseded() == candidate.isSuperseded() && candidate.collectionId() > chosen.collectionId())) {
                        chosen = candidate;
                    }
                }
            }
            if (chosen != null) {
                WriteRegistryMemo.put(ctx, tenant, label, chosen);
                if (named.isPresent()) {
                    WriteRegistryMemo.put(ctx, tenant, chosen.label(), chosen);
                    ExpectedCollectionIds.consumed(ctx, label);
                }
            }
        }
        if (!namedIds.isEmpty()) {
            ExpectedCollectionIds.afterTransactionRead();
        }
    }

    /**
     * A document batch's registry check: the rows of every collection id in {@code ids} (named in the BODY, {@code physical_collection_id}) and
     * every label in {@code labels}, {@code FOR KEY SHARE} in ONE statement in {@code collection_id} order, so a batch that mixes ids and labels
     * cannot take two runs in two orders against a verb that locks in id order. Each id is memoised under its {@code #<id>} key (what
     * {@link #lockIdForWrite} answers from) and its current label; a label is memoised under itself. The refusals are
     * {@link #lockIdForWrite}'s for an id (no row {@link UnknownCollectionIdException}, a retired row {@link CollectionRetiredIdException});
     * a label no row has is not refused here.
     */
    void lockIdsAndLabelsForWrite(DSLContext ctx, String tenant, java.util.Collection<Long> ids, java.util.Collection<String> labels) {
        List<Long> idTodo = new java.util.ArrayList<>();
        for (Long id : ids) {
            if (id != null && WriteRegistryMemo.get(ctx, tenant, "#" + id) == null) {
                idTodo.add(id);
            }
        }
        List<String> labelTodo = new java.util.ArrayList<>();
        for (String label : labels) {
            if (label != null && !label.isEmpty() && WriteRegistryMemo.get(ctx, tenant, label) == null) {
                labelTodo.add(label);
            }
        }
        if (idTodo.isEmpty() && labelTodo.isEmpty()) {
            return;
        }
        if (idTodo.size() + labelTodo.size() == 1 && idTodo.size() == 1) {
            lockIdForWrite(ctx, tenant, idTodo.get(0));
            return;
        }
        if (idTodo.size() + labelTodo.size() == 1) {
            lockForWrite(ctx, tenant, labelTodo.get(0));
            return;
        }
        if (!idTodo.isEmpty()) {
            ExpectedCollectionIds.beforeTransactionRead();
        }
        noteWriteRead(tenant, String.join(",", labelTodo) + idTodo);
        Condition match = DSL.falseCondition();
        if (!idTodo.isEmpty()) {
            match = match.or(CATALOG_COLLECTIONS.COLLECTION_ID.in(idTodo));
        }
        if (!labelTodo.isEmpty()) {
            match = match.or(CATALOG_COLLECTIONS.NAME.in(labelTodo));
        }
        Field<Integer> modelDimension = EMBEDDING_MODELS.DIMENSION.as("model_dimension");
        long waitStarted = System.nanoTime();
        List<CollectionRow> rows = ctx.select(
                CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE, modelDimension,
                CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .leftJoin(EMBEDDING_MODELS)
                .on(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(match))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID)
            .forKeyShare().of(CATALOG_COLLECTIONS)
            .fetch(r -> rowOf(r, modelDimension, tenant));
        noteWait(waitStarted, tenant, () -> rows.stream().map(CollectionRow::label).toList(), () -> rows.stream().map(CollectionRow::collectionId).toList());
        for (long id : idTodo) {
            CollectionRow row = rows.stream().filter(r -> r.collectionId() == id).findFirst()
                .orElseThrow(() -> new UnknownCollectionIdException(tenant, id));
            if (row.isSuperseded()) {
                throw new CollectionRetiredIdException(tenant, id, row.label(), CollectionSuccessor.of(ctx, tenant, id));
            }
            WriteRegistryMemo.put(ctx, tenant, "#" + id, row);
            WriteRegistryMemo.put(ctx, tenant, row.label(), row);
            ExpectedCollectionIds.consumed(ctx, "#" + id);
        }
        for (String label : labelTodo) {
            CollectionRow chosen = null;
            for (CollectionRow candidate : rows) {
                if (!candidate.label().equals(label)) {
                    continue;
                }
                if (chosen == null || (chosen.isSuperseded() && !candidate.isSuperseded())
                    || (chosen.isSuperseded() == candidate.isSuperseded() && candidate.collectionId() > chosen.collectionId())) {
                    chosen = candidate;
                }
            }
            if (chosen != null) {
                WriteRegistryMemo.put(ctx, tenant, label, chosen);
            }
        }
        if (!idTodo.isEmpty()) {
            ExpectedCollectionIds.afterTransactionRead();
        }
    }

    /** {@link #lockIdsAndLabelsForWrite} on the process-wide registry. */
    public static void requireIdsAndLabelsForWrite(DSLContext ctx, String tenant, java.util.Collection<Long> ids,
                                                   java.util.Collection<String> labels) {
        SHARED.lockIdsAndLabelsForWrite(ctx, tenant, ids, labels);
    }

    /**
     * linda's ruling (RDR-229 P3.5a): a writer reads its collection's row {@code FOR KEY SHARE} first, with nothing to write too, so a by-id
     * call answers the same whatever its batch size: a 200 holds only for a live row, and a freed id is 404. A write with an empty batch calls
     * this where it used to return at once; it opens a write transaction only when the request named the collection by id (a request by label
     * names no row to guard, and keeps its no-transaction answer).
     */
    public static void readRowForEmptyWrite(TenantScope scope, String tenant, String label) {
        if (!ExpectedCollectionIds.named(label)) {
            return;
        }
        scope.withTenant(tenant, ctx -> {
            requireForWrite(ctx, tenant, label);
            return null;
        });
    }

    /**
     * The registry rows of {@code labels} that exist, read {@code FOR SHARE} (stronger than the key share every writer takes: a concurrent
     * retype of a row waits too) in {@code collection_id} order, and recorded in the transaction's memo like any registry read, so the writer
     * census sees them. For the writers that decide from a non-key column of the row (the stale-note repair: is it still a knowledge collection)
     * and must hold it unchanged until they commit. A label no row has is simply absent from the answer.
     */
    public static List<CollectionRow> readSharedForWrite(DSLContext ctx, String tenant, java.util.Collection<String> labels) {
        if (labels.isEmpty()) {
            return List.of();
        }
        return readLocked(ctx, tenant, CATALOG_COLLECTIONS.NAME.in(labels), true, labels);
    }

    /**
     * The registry rows of {@code ids} (and the rows of {@code labels}, for rows the P3.1 stamp has not reached), in ANY state, read
     * {@code FOR KEY SHARE} in {@code collection_id} order and recorded in the transaction's memo. For a cleanup that must run whatever the
     * collection's state (a purge of a document's manifest rows clears a retired collection's rows too, so a retired row is not refused as
     * {@link #lockIdForWrite} refuses it) but still takes its place in the canonical order. An id or label with no row is skipped.
     */
    public static void lockForCleanup(DSLContext ctx, String tenant, java.util.Collection<Long> ids, java.util.Collection<String> labels) {
        Condition match = DSL.falseCondition();
        if (!ids.isEmpty()) {
            match = match.or(CATALOG_COLLECTIONS.COLLECTION_ID.in(ids));
        }
        if (!labels.isEmpty()) {
            match = match.or(CATALOG_COLLECTIONS.NAME.in(labels));
        }
        if (ids.isEmpty() && labels.isEmpty()) {
            return;
        }
        readLocked(ctx, tenant, match, false, labels);
    }

    private static List<CollectionRow> readLocked(DSLContext ctx, String tenant, Condition match, boolean fullShare,
                                                  java.util.Collection<String> labels) {
        noteWriteRead(tenant, String.join(",", labels));
        Field<Integer> modelDimension = EMBEDDING_MODELS.DIMENSION.as("model_dimension");
        var read = ctx.select(
                CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE, modelDimension,
                CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .leftJoin(EMBEDDING_MODELS)
                .on(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(match))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID);
        List<CollectionRow> rows = (fullShare ? read.forShare() : read.forKeyShare()).of(CATALOG_COLLECTIONS)
            .fetch(r -> rowOf(r, modelDimension, tenant));
        for (CollectionRow row : rows) {
            WriteRegistryMemo.put(ctx, tenant, "#" + row.collectionId(), row);
        }
        for (String label : labels) {
            CollectionRow chosen = null;
            for (CollectionRow candidate : rows) {   // the live row of the label first, else the newest tombstone (labelResolutionOrder)
                if (!candidate.label().equals(label)) {
                    continue;
                }
                if (chosen == null || (chosen.isSuperseded() && !candidate.isSuperseded())
                    || (chosen.isSuperseded() == candidate.isSuperseded() && candidate.collectionId() > chosen.collectionId())) {
                    chosen = candidate;
                }
            }
            if (chosen != null) {
                WriteRegistryMemo.put(ctx, tenant, label, chosen);
            }
        }
        return rows;
    }

    /** {@link #lockAllForWrite} on the process-wide registry. */
    public static void requireAllForWrite(DSLContext ctx, String tenant, java.util.Collection<String> labels) {
        SHARED.lockAllForWrite(ctx, tenant, labels);
    }

    /**
     * RDR-229 P3.5a: the registry rows of every collection a batch names, read FOR KEY SHARE in one statement in {@code collection_id}
     * order (a retiring verb's order) before the batch registers the ones that are missing and writes. A name no row has is skipped here:
     * the registration below creates it.
     */
    static void lockAllInIdOrder(DSLContext ctx, String tenant, java.util.Collection<String> collections) {
        WriteRegistryMemo.assertNoneHeld(ctx, "a multi-collection batch");   // the batch is the transaction's first registry read
        var named = collections.stream().filter(c -> c != null && !c.isEmpty()).toList();
        if (named.size() > 1) {
            try {
                requireAllForWrite(ctx, tenant, named);
            } catch (UnregisteredCollectionException unknown) {
                // registered by the loop that follows
            }
        }
    }

    /**
     * The registry row of {@code collectionId}, read {@code FOR KEY SHARE} in {@code ctx}'s write transaction, for a write whose request
     * names the collection by id in its BODY and not at the route boundary (the document routes: {@code physical_collection_id}, in
     * the transaction that writes). The same read, lock, refusals and memo as a request by id at the boundary: no row is
     * {@link UnknownCollectionIdException}, a retired row {@link CollectionRetiredIdException}, a row of another id
     * {@link CollectionIdMismatchException}; a live row is memoised under its current label and returned, and the id counts as
     * consumed for the per-transaction census ({@code #<id>}, the key the filter named it by). Never from the cache, which holds no
     * lock: a row the cache calls live can be retired by a supersede that commits before the write does.
     */
    public static CollectionRow requireIdForWrite(DSLContext ctx, String tenant, long collectionId) {
        return SHARED.lockIdForWrite(ctx, tenant, collectionId);
    }

    CollectionRow lockIdForWrite(DSLContext ctx, String tenant, long collectionId) {
        String key = "#" + collectionId;
        CollectionRow held = WriteRegistryMemo.get(ctx, tenant, key);
        if (held != null && held.collectionId() == collectionId) {
            ExpectedCollectionIds.consumed(ctx, key);   // read already, in this transaction, with the batch's other rows (lockIdsAndLabelsForWrite)
            return held;
        }
        noteWriteRead(tenant, key);
        ExpectedCollectionIds.beforeTransactionRead();   // test seams, as for a boundary-named id (no-ops in production)
        CollectionRow row = fillOrNull(ctx, tenant, CATALOG_COLLECTIONS.COLLECTION_ID.eq(collectionId), List.of(), true, false);
        if (row == null) {
            throw new UnknownCollectionIdException(tenant, collectionId);
        }
        if (row.collectionId() != collectionId) {
            throw new CollectionIdMismatchException(collectionId, row.collectionId());
        }
        if (row.isSuperseded()) {
            throw new CollectionRetiredIdException(tenant, collectionId, row.label(), CollectionSuccessor.of(ctx, tenant, collectionId));
        }
        WriteRegistryMemo.put(ctx, tenant, row.label(), row);
        ExpectedCollectionIds.consumed(ctx, key);
        ExpectedCollectionIds.afterTransactionRead();
        return row;
    }

    /** Test seam: called with {@code tenant|label} before every {@code FOR KEY SHARE} read of a write transaction; null in production. */
    private static volatile java.util.function.Consumer<String> writeReadHookForTests;

    /** Test-only: install (or clear with {@code null}) the hook run before each write-transaction registry read, to count them. */
    public static void setWriteReadHookForTests(java.util.function.Consumer<String> hook) {
        writeReadHookForTests = hook;
    }

    private static void noteWriteRead(String tenant, String label) {
        java.util.function.Consumer<String> hook = writeReadHookForTests;
        if (hook != null) {
            hook.accept(tenant + '|' + label);
        }
    }

    /** Tags this transaction exempt from the writer census ({@code StrictWriterCensus}): a verb, a background writer that waits for no lock. */
    public static void exemptFromWriterCensus(DSLContext ctx, String why) {
        WriteRegistryMemo.exempt(ctx, why);
    }

    /**
     * Drops the transaction's memo of registry rows it read {@code FOR KEY SHARE}: a rollback to a savepoint releases any lock taken
     * inside it while the memo says it is held, so a savepoint that contains a registry read must call this after the rollback. No
     * savepoint in the engine contains one today (the two savepoints of the search path's short-page count and re-run are read-only
     * and never read the registry, so their rollbacks no longer call it); {@code WriteRegistryMemoTest} pins the behaviour.
     */
    public static void forgetWriteMemo(DSLContext ctx) {
        WriteRegistryMemo.forget(ctx);
    }

    /** {@link #requireForWriteRow} on the process-wide registry. */
    public static CollectionRow requireForWrite(DSLContext ctx, String tenant, String label) {
        return SHARED.requireForWriteRow(ctx, tenant, label);
    }

    /** True when this transaction has already read {@code label}'s registry row {@code FOR KEY SHARE} (it holds the lock). */
    public static boolean holdsRow(DSLContext ctx, String tenant, String label) {
        return WriteRegistryMemo.get(ctx, tenant, label) != null;
    }

    /** {@link #requireForWriteIfPresentRow} on the process-wide registry. */
    public static Optional<CollectionRow> requireForWriteIfPresent(DSLContext ctx, String tenant, String label) {
        return SHARED.requireForWriteIfPresentRow(ctx, tenant, label);
    }

    /**
     * The row of {@code collectionId} in {@code tenant}: the cached one, else read by id and cached under both keys unless an
     * evict ran between the read and the fill (nexus-hkh1y). A retired row is returned (it names its successor) and cached
     * under neither key, as {@link #markKnown} does for a label.
     *
     * @throws UnknownCollectionIdException if the tenant has no row with that id (never an
     *         {@link UnregisteredCollectionException}, which a label with no row raises)
     */
    CollectionRow resolveId(DSLContext ctx, String tenant, long collectionId) {
        Optional<CollectionRow> hit = cachedById(tenant, collectionId);
        if (hit.isPresent()) {
            return hit.get();
        }
        return fill(ctx, tenant, CATALOG_COLLECTIONS.COLLECTION_ID.eq(collectionId), List.of(), false,
            () -> new UnknownCollectionIdException(tenant, collectionId));
    }

    /**
     * {@link #resolveId}, for a caller outside an open transaction: a cache hit opens none, a miss opens one short read. A READ with no lock: the
     * route boundary's id-to-label resolution ({@code CollectionSelectorFilter}) is its one caller outside this package, pinned by
     * {@code LookupIdCallersTest}; a write that names an id reads it {@code FOR KEY SHARE} in its own transaction instead.
     */
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
    Optional<CollectionRow> cachedById(String tenant, long collectionId) {
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
                // superseded_by; upsertCollection evicts after it commits (nexus-afkjy), but a cached
                // tombstone could still refuse the name between that commit and the evict.
                // Uncached, every read of a tombstone re-reads it.
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
    private CollectionRow fill(DSLContext ctx, String tenant, Condition where, List<SortField<?>> order, boolean keyShare,
                               java.util.function.Supplier<RuntimeException> missing) {
        CollectionRow row = fillOrNull(ctx, tenant, where, order, keyShare, true);
        if (row == null) {
            throw missing.get();
        }
        return row;
    }

    /**
     * {@link #fill}, answering null for no row instead of throwing. {@code cache} false is a write transaction's own read
     * ({@link #lockForWrite}): it neither runs the miss seam nor caches the row. The row it reads may be one THIS transaction just
     * inserted or changed (a rename inserts the new row, then reads it for its id), which would put a row nobody committed into a
     * cache that has no TTL, and a rollback would leave it there; the seam (a test's supersede) would run again inside the
     * transaction of the verb it started.
     */
    private CollectionRow fillOrNull(DSLContext ctx, String tenant, Condition where, List<SortField<?>> order, boolean keyShare,
                                     boolean cache) {
        long evictionsBeforeRead;
        synchronized (fillLock) {
            evictionsBeforeRead = evictions[0];
        }
        Field<Integer> modelDimension = EMBEDDING_MODELS.DIMENSION.as("model_dimension");
        var read = ctx.select(
                CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE, modelDimension,
                CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .leftJoin(EMBEDDING_MODELS)
                .on(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(where))
            .orderBy(order)
            .limit(1);
        // FOR KEY SHARE names the registry row alone: PostgreSQL refuses a row lock on the nullable side of the outer join
        long waitStarted = System.nanoTime();
        Record r = keyShare ? read.forKeyShare().of(CATALOG_COLLECTIONS).fetchOne() : read.fetchOne();
        if (keyShare) {
            noteWait(waitStarted, tenant, () -> r == null ? "" : r.get(CATALOG_COLLECTIONS.NAME),
                () -> r == null ? "" : r.get(CATALOG_COLLECTIONS.COLLECTION_ID));
        }
        if (r == null) {
            return null;
        }
        CollectionRow row = rowOf(r, modelDimension, tenant);
        String label = row.label();
        if (!cache) {
            return row;
        }
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

    /** The {@link CollectionRow} of one registry record, with the model's dimension where the row's own is NULL. */
    static CollectionRow rowOf(Record r, Field<Integer> modelDimension, String tenant) {
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
        return new CollectionRow(r.get(CATALOG_COLLECTIONS.CONTENT_TYPE), r.get(CATALOG_COLLECTIONS.OWNER_ID),
            r.get(CATALOG_COLLECTIONS.EMBEDDING_MODEL), dimension, r.get(CATALOG_COLLECTIONS.LIFECYCLE_STATE),
            r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY), id == null ? 0L : id, label);
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
        // a label the request named by id is never answered from the label cache (ExpectedCollectionIds)
        return ExpectedCollectionIds.of(collection).isPresent() ? Optional.empty() : SHARED.cachedByLabel(tenant, collection);
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
     * .renameCollection}, which deletes the OLD row) or changes a column a cached
     * {@link CollectionRow} holds ({@code supersedeCollection}; {@code upsertCollection},
     * whose conflict arm can change content_type, a placeholder owner and the tombstone,
     * nexus-afkjy). A stale entry would make
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
     * Row 15 addendum: runs {@code transaction}; when it throws and {@code evict} holds, the tenant's cached registry rows go before the
     * exception does. A COMMIT that reached the caller as an exception may still have been applied, and a sweep that threw has no list of
     * the rows it changed, so the tenant's cache is emptied (always safe: the next read refills it).
     */
    public static <T> T evictingTenantIfItThrows(String tenant, boolean evict, java.util.function.Supplier<T> transaction) {
        try {
            return transaction.get();
        } catch (RuntimeException | Error e) {
            if (evict) {
                shared().evictTenant(tenant);
            }
            throw e;
        }
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
        OptionalLong requested = ExpectedCollectionIds.of(collection);
        if (requested.isPresent()) {
            // a short read of the id, never the label; it is not the write's own transaction, so it does not count as consuming
            return scope.withTenant(tenant, ctx -> SHARED.readRequested(ctx, tenant, collection, requested.getAsLong(), false));
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
     * <p>The registry check of a write transaction: the row is read {@code FOR KEY SHARE}, once per transaction, for a label request
     * as for an id request ({@link #requireForWrite}).
     *
     * @throws UnregisteredCollectionException if no row exists for the pair
     * @throws CollectionSupersededException if the row names a successor
     */
    public static CollectionRow requireWritable(DSLContext ctx, String tenant, String collection) {
        return refuseIfSuperseded(tenant, collection, requireForWrite(ctx, tenant, collection));
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
        OptionalLong requested = ExpectedCollectionIds.of(collection);
        if (requested.isPresent()) {
            // a label the request named by id: the row of that id, never the label's
            return Optional.ofNullable(ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(requested.getAsLong()))
                .fetchOne(CATALOG_COLLECTIONS.LIFECYCLE_STATE));
        }
        return Optional.ofNullable(ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(collection)))
            .orderBy(labelResolutionOrder())
            .limit(1)
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
