package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record3;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashSet;
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

    // ── Raw DSL field references (no jOOQ codegen for simple tables) ──────────
    // Using DSL.field with type params to avoid raw-type warnings.

    private static final Field<String>         F_TENANT     = DSL.field(DSL.name("chash_index", "tenant_id"),           String.class);
    private static final Field<String>         F_CHASH      = DSL.field(DSL.name("chash_index", "chash"),               String.class);
    private static final Field<String>         F_COLLECTION = DSL.field(DSL.name("chash_index", "physical_collection"), String.class);
    private static final Field<OffsetDateTime> F_CREATED_AT = DSL.field(DSL.name("chash_index", "created_at"),          OffsetDateTime.class);

    private static final org.jooq.Table<?> CHASH_INDEX = DSL.table(DSL.name("nexus", "chash_index"));

    private final TenantScope tenantScope;

    public ChashRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
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
    public void upsert(String tenant, String chash, String collection) {
        if (chash == null || chash.isBlank()) throw new IllegalArgumentException("chash must not be empty");
        if (collection == null || collection.isBlank()) throw new IllegalArgumentException("collection must not be empty");

        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(CHASH_INDEX,
                            F_TENANT, F_CHASH, F_COLLECTION, F_CREATED_AT)
               .values(tenant, chash, collection, now)
               .onConflict(F_TENANT, F_CHASH, F_COLLECTION)
               .doUpdate()
               .set(F_CREATED_AT, DSL.field("EXCLUDED.created_at", OffsetDateTime.class))
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
    public void upsertMany(String tenant, List<String> chashes, String collection) {
        if (collection == null || collection.isBlank()) throw new IllegalArgumentException("collection must not be empty");
        if (chashes == null || chashes.isEmpty()) return;

        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        List<String> valid = chashes.stream()
                .filter(c -> c != null && !c.isBlank())
                .toList();
        if (valid.isEmpty()) return;

        tenantScope.withTenant(tenant, ctx -> {
            var insert = ctx.insertInto(CHASH_INDEX,
                                        F_TENANT, F_CHASH, F_COLLECTION, F_CREATED_AT);
            var step = insert.values(tenant, valid.get(0), collection, now);
            for (int i = 1; i < valid.size(); i++) {
                step = step.values(tenant, valid.get(i), collection, now);
            }
            step.onConflict(F_TENANT, F_CHASH, F_COLLECTION)
                .doUpdate()
                .set(F_CREATED_AT, DSL.field("EXCLUDED.created_at", OffsetDateTime.class))
                .execute();
            return null;
        });
    }

    // ── lookup ─────────────────────────────────────────────────────────────────

    /**
     * Return all {@code (collection, created_at)} rows for {@code chash}.
     *
     * <p>Returns an empty list when {@code chash} is unknown.
     * Mirrors {@code ChashIndex.lookup}.
     */
    public List<Map<String, String>> lookup(String tenant, String chash) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(F_COLLECTION, F_CREATED_AT)
                          .from(CHASH_INDEX)
                          .where(F_CHASH.eq(chash))
                          .fetch();
            List<Map<String, String>> result = new ArrayList<>(rows.size());
            for (var r : rows) {
                OffsetDateTime ts = r.get(F_CREATED_AT);
                String tsStr = ts != null ? UTC_SECOND.format(ts.atZoneSameInstant(ZoneOffset.UTC)) : "";
                result.add(Map.of("collection", r.get(F_COLLECTION), "created_at", tsStr));
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
                   .where(F_COLLECTION.eq(collection))
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
            var rows = ctx.selectDistinct(F_COLLECTION)
                          .from(CHASH_INDEX)
                          .fetch();
            Set<String> result = new HashSet<>(rows.size());
            for (var r : rows) {
                result.add(r.get(F_COLLECTION));
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
        return tenantScope.withTenant(tenant, ctx -> {
            // Drop rows in new that would collide with the rename
            ctx.deleteFrom(CHASH_INDEX)
               .where(F_COLLECTION.eq(newCollection)
                   .and(F_CHASH.in(
                       DSL.select(F_CHASH)
                          .from(CHASH_INDEX)
                          .where(F_COLLECTION.eq(oldCollection))
                   )))
               .execute();
            // Rename
            return ctx.update(CHASH_INDEX)
                      .set(F_COLLECTION, newCollection)
                      .where(F_COLLECTION.eq(oldCollection))
                      .execute();
        });
    }

    // ── delete_stale ───────────────────────────────────────────────────────────

    /**
     * Drop the single row identified by the compound PK {@code (chash, collection)}.
     *
     * <p>Mirrors {@code ChashIndex.delete_stale}. Returns 0 when the PK was absent
     * (idempotent under concurrent self-heal invocations).
     */
    public int deleteStale(String tenant, String chash, String collection) {
        return tenantScope.withTenant(tenant, ctx ->
                ctx.deleteFrom(CHASH_INDEX)
                   .where(F_CHASH.eq(chash).and(F_COLLECTION.eq(collection)))
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
                         .where(F_COLLECTION.eq(collection))
                         .fetchOne();
            return row != null ? row.value1() : 0;
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
    public void doImport(String tenant, String chash, String collection, String createdAtIso) {
        if (chash == null || chash.isBlank()) throw new IllegalArgumentException("chash must not be empty");
        if (collection == null || collection.isBlank()) throw new IllegalArgumentException("collection must not be empty");

        OffsetDateTime createdAt;
        try {
            createdAt = OffsetDateTime.parse(createdAtIso);
        } catch (Exception e) {
            createdAt = OffsetDateTime.now(ZoneOffset.UTC);
            log.warn("event=chash_import_bad_created_at chash={} raw={} fallback=now", chash, createdAtIso);
        }

        final OffsetDateTime ts = createdAt;
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(CHASH_INDEX,
                            F_TENANT, F_CHASH, F_COLLECTION, F_CREATED_AT)
               .values(tenant, chash, collection, ts)
               .onConflict(F_TENANT, F_CHASH, F_COLLECTION)
               .doUpdate()
               .set(F_CREATED_AT, DSL.field("EXCLUDED.created_at", OffsetDateTime.class))
               .execute();
            return null;
        });
    }
}
