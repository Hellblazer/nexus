package dev.nexus.service.db;

import org.jooq.DSLContext;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_INDEX;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-152 bead nexus-gmiaf.16 — jOOQ-based chash_index repository.
 *
 * <p>Mirrors {@code ChashIndex} (SQLite) for the Postgres service tier.
 * All methods route through {@link TenantScope#withTenant} so every row
 * access is stamped with the tenant GUC and enforced by RLS.
 *
 * <p>The {@code chash_index} table is a content-addressed routing table:
 * given a {@code chash:<hex>} citation, it answers "which physical
 * collections hold this chunk?" The compound PK is
 * {@code (tenant_id, chash, physical_collection)} because the same chunk
 * text (same SHA-256) can legitimately live in multiple collections.
 *
 * <p>NO FTS — this store is an exact-lookup / batch-write table; no text
 * search is required (per the parity contract, Store 7).
 *
 * <p>Thread safety: all writes go through TenantScope which uses a
 * connection pool; each withTenant call gets its own connection.
 *
 * <p>RDR-180 (nexus-jxizy.7): {@code chash} is stored as 32 raw bytes
 * ({@code bytea}); this repository takes {@link Chash} values (already
 * boundary-validated by the handler) and binds {@code toBytes()}, and
 * every read encodes back to the 64-hex interchange form.
 */
public final class ChashRepository {

    private static final Logger log = LoggerFactory.getLogger(ChashRepository.class);

    /**
     * UTC second-precision ISO-8601 formatter matching Python's
     * {@code datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}.
     */
    public static final DateTimeFormatter UTC_SECOND =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                             .withZone(ZoneOffset.UTC);

    private final TenantScope tenantScope;

    public ChashRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /**
     * RDR-156 P0.2: ensure catalog_collections has a stub row for the given collection
     * before any chash_index write that carries physical_collection.
     * Idempotent — ON CONFLICT DO NOTHING.
     *
     * <p>physical_collection is NOT NULL in chash_index, so a blank/null value is a caller
     * error — fail loud rather than silently skipping the registration step and letting the
     * subsequent INSERT fail with a cryptic FK violation.
     *
     * <p>Bead nexus-h8rf6.2 (contention relief): skips the INSERT entirely when
     * {@link CollectionRegistry} already knows this {@code (tenant, collection)} pair is
     * registered. See {@link CollectionRegistry} class doc for why the repeated
     * {@code ON CONFLICT DO NOTHING} was a same-row lock-wait convoy under concurrent
     * indexing, and why callers (not this method) are responsible for marking the cache
     * only after the enclosing transaction commits.
     *
     * @throws IllegalArgumentException if collection is null or blank
     */
    private static void ensureCollectionRegistered(DSLContext ctx, String tenant, String collection) {
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("physical_collection must not be blank");
        }
        if (CollectionRegistry.isKnown(tenant, collection)) {
            return;
        }
        ctx.insertInto(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .values(tenant, collection)
           .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .doNothing()
           .execute();
    }

    /**
     * Register {@code collection} in its OWN short committed transaction, then mark
     * the {@link CollectionRegistry} cache.
     *
     * <p>Bounded first-burst convoy (v0.1.21, ChashVectorConcurrencyTest full-suite
     * failure): with registration inside the batch transaction, the FIRST writer to
     * a brand-new collection holds the {@code catalog_collections} ON CONFLICT value
     * lock for its ENTIRE batch — every concurrent racer blocks for the winner's
     * whole batch duration, and on a loaded host that exceeds the pool's
     * connectionTimeout, surfacing typed 503s the cache was built to prevent. The
     * CollectionRegistry cache bounds convoy COUNT (one per process lifetime);
     * this bounds convoy DURATION (a single-statement micro-transaction, committed
     * before the batch begins).
     *
     * <p>Trade-off, accepted: if the subsequent batch rolls back, the stub row
     * persists — a zero-chunk registry stub is benign (collection existence is
     * live-chunk-count-based everywhere: {@code collection_exists}, {@code /stats};
     * {@code deleteCollection} removes stubs) and the pre-registration is exactly
     * what any retry would recreate. Batch paths use this; {@code renameCollection}
     * keeps in-transaction {@link #ensureCollectionRegistered} because its
     * registration must be atomic with the re-point.
     */
    private void registerCollectionShortTxn(String tenant, String collection) {
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("physical_collection must not be blank");
        }
        if (CollectionRegistry.isKnown(tenant, collection)) {
            return;
        }
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(CATALOG_COLLECTIONS,
                            CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .values(tenant, collection)
               .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .doNothing()
               .execute();
            return null;
        });
        // Post-commit discipline per CollectionRegistry class doc: the registration
        // transaction above has committed by the time withTenant returns.
        CollectionRegistry.markKnown(tenant, collection);
    }

    // ── upsert ─────────────────────────────────────────────────────────────────

    /**
     * Register {@code chash} as living in {@code collection}.
     *
     * <p>INSERT ... ON CONFLICT (tenant_id, chash, physical_collection) DO UPDATE SET
     * created_at = EXCLUDED.created_at — re-indexing the same chunk refreshes
     * {@code created_at}, matching SQLite {@code INSERT OR REPLACE} semantics.
     *
     * @throws IllegalArgumentException if chash or collection is blank
     */
    public void upsert(String tenant, Chash chash, String collection) {
        if (chash == null) throw new IllegalArgumentException("chash must not be null");
        if (collection == null || collection.isBlank()) throw new IllegalArgumentException("collection must not be empty");

        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        // Own short committed txn — bounds the first-registration convoy DURATION
        // (see registerCollectionShortTxn doc); also handles markKnown post-commit.
        registerCollectionShortTxn(tenant, collection);
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(CHASH_INDEX,
                            CHASH_INDEX.TENANT_ID, CHASH_INDEX.CHASH, CHASH_INDEX.PHYSICAL_COLLECTION, CHASH_INDEX.CREATED_AT)
               .values(tenant, chash.toBytes(), collection, now)
               .onConflict(CHASH_INDEX.TENANT_ID, CHASH_INDEX.CHASH, CHASH_INDEX.PHYSICAL_COLLECTION)
               .doUpdate()
               .set(CHASH_INDEX.CREATED_AT, DSL.field("EXCLUDED.created_at", OffsetDateTime.class))
               .execute();
            return null;
        });
    }

    /**
     * Register many {@code chashes} in one {@code collection} in a single batch.
     *
     * <p>Mirrors {@code ChashIndex.upsert_many}: collapses a batch to one round-trip.
     * Blank/null entries in {@code chashes} are skipped. Empty collection raises
     * {@link IllegalArgumentException}. An empty (or all-blank) chashes list is a no-op.
     */
    public void upsertMany(String tenant, List<Chash> chashes, String collection) {
        if (collection == null || collection.isBlank()) throw new IllegalArgumentException("collection must not be empty");
        if (chashes == null || chashes.isEmpty()) return;

        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        // distinct(): a multi-VALUES INSERT .. ON CONFLICT DO UPDATE raises
        // "cannot affect row a second time" (-> HTTP 500) when the same chash
        // appears twice in one statement, and real files emit duplicate chunk
        // text (nexus-85z0y). Dedup is semantics-free: a chash is a content
        // hash, so every occurrence is identical.
        // sorted(): the multi-row INSERT locks CHASH_INDEX rows in values() order.
        // tenant + collection are constant within this call, so chash is the only
        // varying part of the (tenant, chash, physical_collection) conflict key —
        // sorting by chash gives every concurrent batch one global lock order and
        // removes the cross-batch deadlock (SQLSTATE 40P01, nexus-ps9wb; same class as
        // PgVectorRepository.upsertChunks). distinct(): a multi-VALUES INSERT ..
        // ON CONFLICT DO UPDATE raises "cannot affect row a second time" (-> HTTP 500)
        // when the same chash appears twice in one statement, and real files emit
        // duplicate chunk text (nexus-85z0y). Dedup is semantics-free: a chash is a
        // content hash, so every occurrence is identical.
        // Sorting by hex preserves the global lock order (lowercase-hex
        // lexicographic order == unsigned byte order of the stored key).
        List<Chash> valid = chashes.stream()
                .filter(java.util.Objects::nonNull)
                .distinct()
                .sorted(java.util.Comparator.comparing(Chash::toHex))
                .toList();
        if (valid.isEmpty()) return;

        // Own short committed txn — bounds the first-registration convoy DURATION
        // (see registerCollectionShortTxn doc); also handles markKnown post-commit.
        registerCollectionShortTxn(tenant, collection);
        // nexus-ps9wb belt: retry a residual cross-path deadlock (the sort removes the
        // same-batch cycle; a concurrent writer on a different lock order can still
        // deadlock). Idempotent ON CONFLICT batch, victim already rolled back → safe.
        DeadlockRetry.run(collection, () -> tenantScope.withTenant(tenant, ctx -> {
            var insert = ctx.insertInto(CHASH_INDEX,
                                        CHASH_INDEX.TENANT_ID, CHASH_INDEX.CHASH, CHASH_INDEX.PHYSICAL_COLLECTION, CHASH_INDEX.CREATED_AT);
            var step = insert.values(tenant, valid.get(0).toBytes(), collection, now);
            for (int i = 1; i < valid.size(); i++) {
                step = step.values(tenant, valid.get(i).toBytes(), collection, now);
            }
            step.onConflict(CHASH_INDEX.TENANT_ID, CHASH_INDEX.CHASH, CHASH_INDEX.PHYSICAL_COLLECTION)
                .doUpdate()
                .set(CHASH_INDEX.CREATED_AT, DSL.field("EXCLUDED.created_at", OffsetDateTime.class))
                .execute();
            return null;
        }));
    }

    // ── legacy-reference resolution (RDR-180 Item3 read seam) ─────────────────

    /**
     * Resolve a LEGACY reference (pre-RDR-180 32-hex chunk id, or an ETL-era
     * external id) to its canonical chash via the permanent
     * ``nexus.chash_alias`` map (nexus-jxizy.6). Returns null when the map
     * holds no fact for *oldRef* — the caller treats that as chash-not-found
     * (empty rows), never an error: the alias map is the collision-free
     * resolver, and an unmapped legacy reference is simply dangling.
     */
    public Chash resolveLegacyRef(String tenant, String oldRef) {
        if (oldRef == null || oldRef.isBlank()) return null;
        return tenantScope.withTenant(tenant, ctx -> {
            var row = ctx.select(dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS.NEW_CHASH)
                         .from(dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS)
                         .where(dev.nexus.service.jooq.nexus.Tables.CHASH_ALIAS.OLD_REF.eq(oldRef))
                         .fetchOne();
            if (row == null || row.value1() == null) return null;
            return Chash.fromSha256Bytes(row.value1());
        });
    }

    // ── lookup ─────────────────────────────────────────────────────────────────

    /**
     * Return all {@code (collection, created_at)} rows for {@code chash}.
     *
     * <p>Returns an empty list when {@code chash} is unknown.
     * Mirrors {@code ChashIndex.lookup}.
     */
    public List<Map<String, String>> lookup(String tenant, Chash chash) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(CHASH_INDEX.PHYSICAL_COLLECTION, CHASH_INDEX.CREATED_AT)
                          .from(CHASH_INDEX)
                          .where(CHASH_INDEX.CHASH.eq(chash.toBytes()))
                          .fetch();
            List<Map<String, String>> result = new ArrayList<>(rows.size());
            for (var r : rows) {
                OffsetDateTime ts = r.get(CHASH_INDEX.CREATED_AT);
                String tsStr = ts != null ? UTC_SECOND.format(ts.atZoneSameInstant(ZoneOffset.UTC)) : "";
                result.add(Map.of("collection", r.get(CHASH_INDEX.PHYSICAL_COLLECTION), "created_at", tsStr));
            }
            return result;
        });
    }

    // ── delete_collection ──────────────────────────────────────────────────────

    /**
     * Drop all rows for {@code collection}. Returns deleted row count.
     *
     * <p>Mirrors {@code ChashIndex.delete_collection}. Uses the
     * {@code idx_chash_index_collection} index for an index seek (not a table scan).
     * Idempotent: absent collection yields 0.
     */
    public int deleteCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx ->
                ctx.deleteFrom(CHASH_INDEX)
                   .where(CHASH_INDEX.PHYSICAL_COLLECTION.eq(collection))
                   .execute());
    }

    // ── distinct_collections ───────────────────────────────────────────────────

    /**
     * Return every distinct {@code physical_collection} value for this tenant.
     *
     * <p>Mirrors {@code ChashIndex.distinct_collections}. Used by
     * {@code nx catalog chash-reconcile} to identify ghost collections.
     */
    public Set<String> distinctCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.selectDistinct(CHASH_INDEX.PHYSICAL_COLLECTION)
                          .from(CHASH_INDEX)
                          .fetch();
            Set<String> result = new HashSet<>(rows.size());
            for (var r : rows) {
                result.add(r.get(CHASH_INDEX.PHYSICAL_COLLECTION));
            }
            return result;
        });
    }

    // ── rename_collection ──────────────────────────────────────────────────────

    /**
     * Re-point every row from {@code oldCollection} to {@code newCollection}.
     * Returns the count of rows updated.
     *
     * <p>Mirrors {@code ChashIndex.rename_collection}: first deletes any
     * pre-existing rows for {@code newCollection} that would collide with
     * the rename (same chash), then updates. Runs in a single transaction
     * via {@code withTenant}.
     */
    public int renameCollection(String tenant, String oldCollection, String newCollection) {
        int updated = tenantScope.withTenant(tenant, ctx -> {
            // RDR-156 P0.2: ensure the new collection is registered before renaming.
            ensureCollectionRegistered(ctx, tenant, newCollection);
            // Drop rows in new that would collide with the rename
            ctx.deleteFrom(CHASH_INDEX)
               .where(CHASH_INDEX.PHYSICAL_COLLECTION.eq(newCollection)
                   .and(CHASH_INDEX.CHASH.in(
                       DSL.select(CHASH_INDEX.CHASH)
                          .from(CHASH_INDEX)
                          .where(CHASH_INDEX.PHYSICAL_COLLECTION.eq(oldCollection))
                   )))
               .execute();
            // Rename
            return ctx.update(CHASH_INDEX)
                      .set(CHASH_INDEX.PHYSICAL_COLLECTION, newCollection)
                      .where(CHASH_INDEX.PHYSICAL_COLLECTION.eq(oldCollection))
                      .execute();
        });
        // Post-commit (nexus-h8rf6.2): see upsert()'s comment / CollectionRegistry doc.
        CollectionRegistry.markKnown(tenant, newCollection);
        return updated;
    }

    // ── delete_stale ───────────────────────────────────────────────────────────

    /**
     * Drop the single row identified by the compound PK {@code (chash, collection)}.
     *
     * <p>Mirrors {@code ChashIndex.delete_stale}. Returns 0 when the PK was absent
     * (idempotent under concurrent self-heal invocations).
     */
    public int deleteStale(String tenant, Chash chash, String collection) {
        return tenantScope.withTenant(tenant, ctx ->
                ctx.deleteFrom(CHASH_INDEX)
                   .where(CHASH_INDEX.CHASH.eq(chash.toBytes()).and(CHASH_INDEX.PHYSICAL_COLLECTION.eq(collection)))
                   .execute());
    }

    // ── is_empty ───────────────────────────────────────────────────────────────

    /**
     * True when no rows exist for this tenant — the "fresh install" guard.
     *
     * <p>Mirrors {@code ChashIndex.is_empty}. Used by {@code nx doc cite}
     * short-circuit.
     */
    public boolean isEmpty(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
                !ctx.fetchExists(ctx.select(DSL.val(1)).from(CHASH_INDEX)));
    }

    // ── count_for_collection ───────────────────────────────────────────────────

    /**
     * Return the row count for {@code collection}.
     *
     * <p>Mirrors {@code ChashIndex.count_for_collection}. Returns 0 for an
     * unknown collection.
     */
    public int countForCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var row = ctx.select(DSL.count())
                         .from(CHASH_INDEX)
                         .where(CHASH_INDEX.PHYSICAL_COLLECTION.eq(collection))
                         .fetchOne();
            return row != null ? row.value1() : 0;
        });
    }

    // ── registered_chashes_for_collection ─────────────────────────────────────

    /**
     * Return every distinct registered chash for {@code collection}, hex-encoded.
     *
     * <p>RDR-180: the natural chunk ID is the FULL digest, so the pre-flip
     * {@code substr(chash, 1, 32)} compensation is retired — values encode to
     * 64-hex (canonical rows) while not-yet-rekeyed legacy rows naturally
     * encode to their shorter legacy hex, which callers treat as legacy
     * references.
     *
     * <p>Used by the collection-audit coverage probe
     * ({@code collection_audit.py}): one set-difference against T3 chunk IDs
     * replaces the per-page IN-list query.
     *
     * @param tenant     tenant principal (sets RLS GUC)
     * @param collection physical collection name to query
     * @return set of hex-encoded chashes; empty when collection is unknown
     */
    public Set<String> registeredChashesForCollection(String tenant, String collection) {
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("collection must not be empty");
        }
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.selectDistinct(CHASH_INDEX.CHASH)
                          .from(CHASH_INDEX)
                          .where(CHASH_INDEX.PHYSICAL_COLLECTION.eq(collection))
                          .fetch();
            Set<String> result = new HashSet<>(rows.size());
            for (var r : rows) {
                byte[] ch = r.value1();
                if (ch != null && ch.length > 0) {
                    result.add(HexFormat.of().formatHex(ch));
                }
            }
            return result;
        });
    }

    // ── import (fidelity-preserving ETL) ──────────────────────────────────────

    /**
     * Fidelity-preserving import of a single row.
     *
     * <p>ON CONFLICT (tenant_id, chash, physical_collection) DO UPDATE SET
     * created_at = EXCLUDED.created_at. Chash entries are content-addressed
     * and immutable; EXCLUDED verbatim is correct (no GREATEST needed —
     * there is no mutable monotonic counter to protect). Idempotent re-runs
     * are safe.
     *
     * @param createdAtIso UTC ISO-8601 string (e.g. "2025-06-01T10:30:00Z")
     */
    public void doImport(String tenant, Chash chash, String collection, String createdAtIso) {
        if (chash == null) throw new IllegalArgumentException("chash must not be null");
        if (collection == null || collection.isBlank()) throw new IllegalArgumentException("collection must not be empty");

        OffsetDateTime createdAt;
        try {
            createdAt = OffsetDateTime.parse(createdAtIso);
        } catch (Exception e) {
            createdAt = OffsetDateTime.now(ZoneOffset.UTC);
            log.warn("event=chash_import_bad_created_at chash={} raw={} fallback=now", chash, createdAtIso);
        }

        final OffsetDateTime ts = createdAt;
        // Own short committed txn — bounds the first-registration convoy DURATION
        // (see registerCollectionShortTxn doc); also handles markKnown post-commit.
        registerCollectionShortTxn(tenant, collection);
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(CHASH_INDEX,
                            CHASH_INDEX.TENANT_ID, CHASH_INDEX.CHASH, CHASH_INDEX.PHYSICAL_COLLECTION, CHASH_INDEX.CREATED_AT)
               .values(tenant, chash.toBytes(), collection, ts)
               .onConflict(CHASH_INDEX.TENANT_ID, CHASH_INDEX.CHASH, CHASH_INDEX.PHYSICAL_COLLECTION)
               .doUpdate()
               .set(CHASH_INDEX.CREATED_AT, DSL.field("EXCLUDED.created_at", OffsetDateTime.class))
               .execute();
            return null;
        });
    }

    /** One row of a batched ETL import (see {@link #doImportBatch}). */
    public record ImportRow(Chash chash, String collection, String createdAtIso) {}

    /**
     * Batched ETL import: land the WHOLE batch in ONE multi-row
     * {@code INSERT ... ON CONFLICT} statement (nexus-1usso).
     *
     * <p>The pre-fix path looped {@link #doImport} per row — for a 200-row
     * client batch that meant 200 × (collection-stub check + INSERT) ≈ 600
     * sequential Postgres round-trips per HTTP request, ~0.9s server-side,
     * which was the measured 1-request/s (~34 KB/s) migration throughput
     * ceiling. Here each DISTINCT collection is registered once and all rows
     * ride a single statement: two-ish round-trips per request instead of 600.
     *
     * <p>Rows are deduped on {@code (chash, collection)} within the batch —
     * last occurrence wins — because a single multi-row INSERT cannot touch
     * the same conflict target twice (PG: "cannot affect row a second time").
     * The ETL source's PK makes intra-batch duplicates impossible in
     * practice; the dedupe is defensive.
     *
     * <p>Same fidelity semantics as {@link #doImport}: {@code created_at}
     * transfers verbatim (EXCLUDED on conflict), unparseable timestamps fall
     * back to now with a warning. Idempotent re-runs are safe.
     *
     * @return the number of unique rows landed
     */
    public int doImportBatch(String tenant, List<ImportRow> rows) {
        if (rows == null || rows.isEmpty()) return 0;

        // Dedupe on (chash, collection), last wins. LinkedHashMap keeps batch order.
        var unique = new java.util.LinkedHashMap<String, ImportRow>(rows.size());
        for (ImportRow r : rows) {
            if (r.chash() == null
                    || r.collection() == null || r.collection().isBlank()) {
                throw new IllegalArgumentException("chash and collection must not be empty");
            }
            unique.put(r.chash().toHex() + "::" + r.collection(), r);
        }
        final List<ImportRow> deduped = List.copyOf(unique.values());

        Set<String> collections = new java.util.LinkedHashSet<>();
        for (ImportRow r : deduped) collections.add(r.collection());

        // Own short committed txns — bounds the first-registration convoy DURATION
        // (see registerCollectionShortTxn doc); also handles markKnown post-commit.
        for (String c : collections) registerCollectionShortTxn(tenant, c);
        int landed = tenantScope.withTenant(tenant, ctx -> {
            var insert = ctx.insertInto(CHASH_INDEX,
                    CHASH_INDEX.TENANT_ID, CHASH_INDEX.CHASH, CHASH_INDEX.PHYSICAL_COLLECTION, CHASH_INDEX.CREATED_AT);
            for (ImportRow r : deduped) {
                OffsetDateTime ts;
                try {
                    ts = OffsetDateTime.parse(r.createdAtIso());
                } catch (Exception e) {
                    ts = OffsetDateTime.now(ZoneOffset.UTC);
                    log.warn("event=chash_import_bad_created_at chash={} raw={} fallback=now",
                             r.chash(), r.createdAtIso());
                }
                insert = insert.values(tenant, r.chash().toBytes(), r.collection(), ts);
            }
            insert.onConflict(CHASH_INDEX.TENANT_ID, CHASH_INDEX.CHASH, CHASH_INDEX.PHYSICAL_COLLECTION)
                  .doUpdate()
                  .set(CHASH_INDEX.CREATED_AT, DSL.field("EXCLUDED.created_at", OffsetDateTime.class))
                  .execute();
            return deduped.size();
        });
        return landed;
    }
}
