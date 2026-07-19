// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHASH_REMAP;
import static dev.nexus.service.jooq.nexus.Tables.REMAP_MEMBERSHIP;

/**
 * RDR-186 bead nexus-146xx.4 — jOOQ-based chash_remap repository.
 *
 * <p>The engine-side write path for the wire re-identification map
 * ({@code nexus.chash_remap}, remap-001-baseline.xml), replacing the client's
 * local {@code chash_remap.db} write path ({@code ChashRemapStore.record_batch},
 * {@code src/nexus/migration/wire_reid.py}). All access routes through
 * {@link TenantScope#withTenant} so every row is stamped with the tenant GUC
 * and enforced by FORCE RLS.
 *
 * <p><strong>RF-186-1:</strong> this repository exposes raw-fact operations
 * only — record, clear, and the live membership counts. There is no verdict
 * read or write surface, and none may ever be added (Gap-4 pin; see the
 * remap-001 changelog header).
 *
 * <p>Batch semantics mirror the SQLite store: {@link #recordBatch} is ONE
 * transaction (the RDR-185 r2 ordering unit — the map batch commits
 * atomically-with-or-before the data it describes), upserting on the
 * {@code (tenant_id, source_collection, old_id)} natural key so resume
 * re-derivation is idempotent.
 */
public final class RemapRepository {

    private static final Logger log = LoggerFactory.getLogger(RemapRepository.class);

    /**
     * Maximum entries per {@link #recordBatch} call — the chroma_quotas
     * MAX_RECORDS_PER_WRITE (300) heritage cap the bead mandates: the client
     * already pages its writes at this bound, so the endpoint enforces the
     * same contract rather than accepting unbounded bodies.
     */
    public static final int MAX_BATCH = 300;

    /** One old-id → new-chash fact (mirrors the Python {@code RemapEntry}). */
    public record RemapEntry(
            String sourceCollection,
            String oldId,
            String newChash,
            String targetCollection,
            String provenance) {}

    private final TenantScope tenantScope;

    public RemapRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /**
     * Persist one map batch in ONE transaction (the r2 ordering unit).
     *
     * <p>Upserts: re-recording the same {@code (tenant, source_collection,
     * old_id)} replaces the fact — resume re-derivation is deterministic, so
     * this is idempotent in practice (mirrors {@code ChashRemapStore
     * .record_batch}'s ON CONFLICT DO UPDATE).
     *
     * <p>Within-batch duplicates of the same {@code (source_collection,
     * old_id)} key are collapsed keeping the LAST occurrence (matching
     * {@code executemany}'s sequential overwrite) — a multi-VALUES INSERT
     * cannot affect the same row twice. Entries are sorted by the conflict
     * key for a global lock order (the nexus-ps9wb deadlock class).
     *
     * @return number of rows written (after within-batch dedup)
     * @throws IllegalArgumentException on empty/oversized batch or invalid entry
     */
    public int recordBatch(String tenant, List<RemapEntry> entries) {
        if (entries == null || entries.isEmpty()) return 0;
        if (entries.size() > MAX_BATCH) {
            throw new IllegalArgumentException(
                "batch too large: " + entries.size() + " entries (max " + MAX_BATCH
                + " — page the batch, chroma_quotas MAX_RECORDS_PER_WRITE heritage)");
        }
        for (RemapEntry e : entries) {
            requireNonBlank(e.sourceCollection(), "source_collection");
            requireNonBlank(e.oldId(), "old_id");
            requireNonBlank(e.targetCollection(), "target_collection");
            requireNonBlank(e.provenance(), "provenance");
            // RDR-180 (nexus-jxizy.7): 64-hex is the canonical fact width;
            // 32-hex era facts remain readable (DB CHECK length IN (32,64)).
            // The HTTP boundary (RemapHandler.normalizeChash) already
            // enforces 64 for NEW facts; this repo-level guard mirrors the
            // DB CHECK as belt-and-suspenders.
            int len = e.newChash() == null ? -1 : e.newChash().length();
            if (len != 32 && len != 64) {
                throw new IllegalArgumentException(
                    "new_chash must be 64 hex chars (or a 32-hex era fact), got: "
                    + (e.newChash() == null ? "null" : len + " chars"));
            }
        }

        // LAST occurrence wins (executemany overwrite semantics), then sort by
        // the conflict key for one global lock order. Keyed by the two-element
        // list, not a delimited string — old_id is an open-ended TEXT field
        // (any pre-remap id), so no delimiter choice is collision-safe.
        Map<List<String>, RemapEntry> byKey = new LinkedHashMap<>();
        for (RemapEntry e : entries) {
            byKey.put(List.of(e.sourceCollection(), e.oldId()), e);
        }
        List<RemapEntry> deduped = byKey.values().stream()
                .sorted(Comparator.comparing(RemapEntry::sourceCollection)
                                  .thenComparing(RemapEntry::oldId))
                .toList();

        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        tenantScope.withTenant(tenant, ctx -> {
            var insert = ctx.insertInto(CHASH_REMAP,
                    CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION,
                    CHASH_REMAP.OLD_ID, CHASH_REMAP.NEW_CHASH,
                    CHASH_REMAP.TARGET_COLLECTION, CHASH_REMAP.CREATED_AT,
                    CHASH_REMAP.PROVENANCE);
            var step = insert.values(tenant,
                    deduped.get(0).sourceCollection(), deduped.get(0).oldId(),
                    deduped.get(0).newChash(), deduped.get(0).targetCollection(),
                    now, deduped.get(0).provenance());
            for (int i = 1; i < deduped.size(); i++) {
                RemapEntry e = deduped.get(i);
                step = step.values(tenant, e.sourceCollection(), e.oldId(),
                        e.newChash(), e.targetCollection(), now, e.provenance());
            }
            step.onConflict(CHASH_REMAP.TENANT_ID, CHASH_REMAP.SOURCE_COLLECTION, CHASH_REMAP.OLD_ID)
                .doUpdate()
                .set(CHASH_REMAP.NEW_CHASH, DSL.field("EXCLUDED.new_chash", String.class))
                .set(CHASH_REMAP.TARGET_COLLECTION, DSL.field("EXCLUDED.target_collection", String.class))
                .set(CHASH_REMAP.CREATED_AT, DSL.field("EXCLUDED.created_at", OffsetDateTime.class))
                .set(CHASH_REMAP.PROVENANCE, DSL.field("EXCLUDED.provenance", String.class))
                .execute();
            return null;
        });
        log.info("event=remap_record_batch tenant={} entries={} deduped={}",
                tenant, entries.size(), deduped.size());
        return deduped.size();
    }

    /**
     * Clear one leg's map rows — the rollback absence-encoding (RDR-186 D2).
     *
     * <p>The CALLER (client bead .8, {@code rollback_collections}) owns the
     * ordering discipline: this is invoked ONLY after the whole leg's
     * {@code target_after} verification — never eagerly, never per-page.
     * This method just deletes.
     *
     * <p>{@code targetCollection} is REQUIRED (critic-146xx-4-5): a leg is the
     * {@code (source, target)} PAIR — the granularity {@code target_collection}
     * was added to the schema for (RDR-185 .13 r2/C2 co-residency). A
     * whole-source wide clear would silently delete a co-resident sibling
     * leg's still-valid claims when rolling back one leg; if a genuine
     * all-legs bulk clear is ever needed it gets its own distinctly-named
     * operation so intent is visible at the call site.
     *
     * @return rows deleted
     */
    public int clearLeg(String tenant, String sourceCollection, String targetCollection) {
        requireNonBlank(sourceCollection, "source_collection");
        requireNonBlank(targetCollection, "target_collection");
        int deleted = tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(CHASH_REMAP)
               .where(CHASH_REMAP.TENANT_ID.eq(tenant)
                       .and(CHASH_REMAP.SOURCE_COLLECTION.eq(sourceCollection))
                       .and(CHASH_REMAP.TARGET_COLLECTION.eq(targetCollection)))
               .execute());
        log.info("event=remap_clear_leg tenant={} source={} target={} deleted={}",
                tenant, sourceCollection, targetCollection, deleted);
        return deleted;
    }

    /**
     * The LIVE leg membership counts — delegates to
     * {@code nexus.remap_membership()} (remap-002, bead .5). Computed fresh on
     * every call; never cached, never persisted (RF-186-1).
     *
     * @return {@code [mapped_total, present_count]}; converged iff equal
     *         (including 0 == 0 — nothing owed)
     */
    public long[] membership(String tenant, String sourceCollection, String targetCollection) {
        requireNonBlank(sourceCollection, "source_collection");
        requireNonBlank(targetCollection, "target_collection");
        return tenantScope.withTenant(tenant, ctx -> {
            var row = ctx.selectFrom(REMAP_MEMBERSHIP.call(sourceCollection, targetCollection))
                         .fetchOne();
            if (row == null) {
                throw new IllegalStateException("remap_membership returned no row");
            }
            return new long[]{row.getMappedTotal(), row.getPresentCount()};
        });
    }

    /**
     * One source collection's facts: {@code [old_id, new_chash,
     * target_collection]} rows — the client {@code entries_for_collection} /
     * {@code entries_with_targets} read shape (rollback + cascade, bead .6/.8).
     */
    public List<Map<String, String>> entriesForCollection(String tenant, String sourceCollection) {
        requireNonBlank(sourceCollection, "source_collection");
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(CHASH_REMAP.OLD_ID, CHASH_REMAP.NEW_CHASH, CHASH_REMAP.TARGET_COLLECTION)
               .from(CHASH_REMAP)
               .where(CHASH_REMAP.TENANT_ID.eq(tenant)
                       .and(CHASH_REMAP.SOURCE_COLLECTION.eq(sourceCollection)))
               .orderBy(CHASH_REMAP.OLD_ID)
               .fetch(r -> Map.of(
                       "old_id", r.value1(),
                       "new_chash", r.value2(),
                       "target_collection", r.value3())));
    }

    /** Page-size ceiling for {@link #pairs} (mirrors the store-list paging shape). */
    public static final int MAX_PAGE = 1000;

    /**
     * Paged global {@code (old_id, new_chash)} view — the remap cascade's
     * input ({@code all_pairs}). Deterministically ordered by
     * {@code (source_collection, old_id)} so OFFSET pagination is stable.
     */
    public List<List<String>> pairs(String tenant, int limit, int offset) {
        if (limit < 1 || limit > MAX_PAGE) {
            throw new IllegalArgumentException("limit must be 1.." + MAX_PAGE + ", got " + limit);
        }
        if (offset < 0) {
            throw new IllegalArgumentException("offset must be >= 0, got " + offset);
        }
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(CHASH_REMAP.OLD_ID, CHASH_REMAP.NEW_CHASH)
               .from(CHASH_REMAP)
               .where(CHASH_REMAP.TENANT_ID.eq(tenant))
               .orderBy(CHASH_REMAP.SOURCE_COLLECTION, CHASH_REMAP.OLD_ID)
               .limit(limit)
               .offset(offset)
               .fetch(r -> List.of(r.value1(), r.value2())));
    }

    /**
     * Total fact-row count for the tenant (optionally one source collection) —
     * ONE cheap round trip serving both .6 design inputs: the
     * probe-before-fetch short-circuit (skip the paged /pairs scan when the
     * count is unchanged since the last clean check, the nexus-vgtff pattern)
     * and the paged-read torn-read reconcile (count before and after paging;
     * mismatch = could-not-tell, never silently accepted).
     */
    public long count(String tenant, String sourceCollection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var where = CHASH_REMAP.TENANT_ID.eq(tenant);
            if (sourceCollection != null && !sourceCollection.isBlank()) {
                where = where.and(CHASH_REMAP.SOURCE_COLLECTION.eq(sourceCollection));
            }
            return (long) ctx.fetchCount(CHASH_REMAP, where);
        });
    }

    /** Distinct source collections — the prior-collections (source-gone) probe input. */
    public List<String> sourceCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectDistinct(CHASH_REMAP.SOURCE_COLLECTION)
               .from(CHASH_REMAP)
               .where(CHASH_REMAP.TENANT_ID.eq(tenant))
               .orderBy(CHASH_REMAP.SOURCE_COLLECTION)
               .fetch(CHASH_REMAP.SOURCE_COLLECTION));
    }

    private static void requireNonBlank(String value, String field) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("'" + field + "' must not be blank");
        }
    }
}
