package dev.nexus.service.db;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.db.JsonbSupport.jsonbRequired;
import static dev.nexus.service.jooq.nexus.Tables.*;
import static org.jooq.impl.DSL.*;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the read-only analytical queries over the taxonomy (the ICF map, the hubs, the per-collection audit and the link
 * drift), moved out of {@link TaxonomyRepository} so that file stays under its size ceiling; {@link TaxonomyRepository} keeps a delegating method
 * for each, so no caller changes. Reads only: no write, no registry row, no lock.
 */
final class TaxonomyAnalytics {

    private final TenantScope tenantScope;

    TaxonomyAnalytics(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /**
     * Compute ICF map atomically: returns [{topic_id, n_effective, df}] in one transaction.
     * Callers compute log2(n_effective / df) in Python.
     */
    public Map<String, Object> computeIcfMapAtomic(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            int nEffective = ctx.select(countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION))
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                    .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
                .fetchOne(0, Integer.class);

            List<Map<String, Object>> rows = new ArrayList<>();
            if (nEffective >= 2) {
                var icfRows = ctx.select(
                        TOPIC_ASSIGNMENTS.TOPIC_ID,
                        countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).as("df"))
                    .from(TOPIC_ASSIGNMENTS)
                    .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                        .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
                    .groupBy(TOPIC_ASSIGNMENTS.TOPIC_ID)
                    .having(countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).gt(0))
                    .fetch();
                for (var r : icfRows) {
                    var m = new LinkedHashMap<String, Object>();
                    m.put("topic_id", r.get(TOPIC_ASSIGNMENTS.TOPIC_ID));
                    m.put("df", r.get("df", Integer.class));
                    rows.add(m);
                }
            }
            var result = new LinkedHashMap<String, Object>();
            result.put("n_effective", nEffective);
            result.put("rows", rows);
            return result;
        });
    }

    /**
     * Hub detection data: returns per-topic DF + total_chunks + label + collection + source set,
     * plus the staleness aggregates behind {@code --warn-stale} (nexus-onjvy).
     * Python-side computes ICF, stopword matching, and score.
     *
     * <p>STALENESS (RDR-077 C-2 semantics, previously SQLite-only). Per hub, over the
     * source collections actually contributing to it:
     * <ul>
     *   <li>{@code max_last_discover_at} — MAX(taxonomy_meta.last_discover_at), NULLs
     *       excluded by MAX, exactly as the retired SQLite oracle did.</li>
     *   <li>{@code never_discovered_count} — contributing collections with a NULL
     *       last_discover_at PLUS those with no taxonomy_meta row at all. Both are
     *       "never discovered" from the command's point of view.</li>
     *   <li>{@code is_stale} — a contributing collection was discovered after the hub's
     *       latest assignment (discover ran, projection did not), or any contributing
     *       collection was never discovered. RDR-077 Phase 5 sense, Sam's ruling
     *       2026-10-26 (nexus-9kp10); it read the reverse until then.</li>
     * </ul>
     *
     * <p>{@code is_stale} is compared on real TIMESTAMPTZ values here. Clients from
     * nexus-9kp10 on re-derive it from {@code last_assigned_at},
     * {@code max_last_discover_at} and {@code never_discovered_count} (parsed, not
     * compared as strings), because engines before this change send the reverse
     * sense; this verdict serves older clients, which pass it through.
     *
     * <p>One extra query, not N+1: taxonomy_meta is fetched once for every collection
     * referenced by any hub, then folded per topic in memory.
     *
     * @param minCollections minimum distinct source_collections (DF threshold)
     */
    public List<Map<String, Object>> detectHubsData(String tenant, int minCollections) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    TOPICS.ID.as("topic_id"),
                    TOPICS.LABEL,
                    TOPICS.COLLECTION,
                    countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).as("df"),
                    count().as("total_chunks"),
                    max(TOPIC_ASSIGNMENTS.ASSIGNED_AT).as("last_assigned_at"))
               .from(TOPIC_ASSIGNMENTS)
               .join(TOPICS).on(TOPICS.ID.eq(TOPIC_ASSIGNMENTS.TOPIC_ID))
               .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                   .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
               .groupBy(TOPICS.ID, TOPICS.LABEL, TOPICS.COLLECTION)
               .having(countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).ge(minCollections))
               .orderBy(count().desc())
               .fetch();

            // Per-hub source collection sets
            var allSources = ctx.select(TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                    .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
                .orderBy(TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
                .fetch();

            // Build topic_id -> [source_collection, ...] map
            java.util.Map<Long, List<String>> sourcesMap = new java.util.HashMap<>();
            for (var r : allSources) {
                long tid = r.get(TOPIC_ASSIGNMENTS.TOPIC_ID);
                String sc = r.get(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION);
                sourcesMap.computeIfAbsent(tid, k -> new ArrayList<>()).add(sc);
            }

            // taxonomy_meta for every collection any hub draws from, in one query.
            // Only non-null last_discover_at values are kept: a collection with a NULL
            // timestamp and one with no taxonomy_meta row at all are both "never
            // discovered" here, and the oracle counted them together too (its
            // never_count summed the NULLs and the missing rows). So a single
            // "absent from this map" test covers both states.
            var contributing = sourcesMap.values().stream()
                .flatMap(List::stream).distinct().toList();
            java.util.Map<String, OffsetDateTime> discoverAt = new java.util.HashMap<>();
            if (!contributing.isEmpty()) {
                for (var r : ctx.select(TAXONOMY_META.COLLECTION, TAXONOMY_META.LAST_DISCOVER_AT)
                        .from(TAXONOMY_META)
                        .where(TAXONOMY_META.COLLECTION.in(contributing))
                        .fetch()) {
                    OffsetDateTime at = r.get(TAXONOMY_META.LAST_DISCOVER_AT);
                    if (at != null) discoverAt.put(r.get(TAXONOMY_META.COLLECTION), at);
                }
            }

            List<Map<String, Object>> result = new ArrayList<>();
            for (var r : rows) {
                long tid = r.get("topic_id", Long.class);
                var m = new LinkedHashMap<String, Object>();
                m.put("topic_id", tid);
                m.put("label", r.get(TOPICS.LABEL));
                m.put("collection", r.get(TOPICS.COLLECTION));
                m.put("df", r.get("df", Integer.class));
                m.put("total_chunks", r.get("total_chunks", Integer.class));
                Object lastAt = r.get("last_assigned_at");
                m.put("last_assigned_at", TaxonomyRepository.utcIso(lastAt));
                List<String> sources = sourcesMap.getOrDefault(tid, List.of());
                m.put("source_collections", sources);

                OffsetDateTime maxDiscover = null;
                int neverCount = 0;
                for (String coll : new java.util.LinkedHashSet<>(sources)) {
                    OffsetDateTime at = discoverAt.get(coll);
                    if (at == null) {
                        // NULL last_discover_at, or no taxonomy_meta row at all.
                        neverCount++;
                        continue;
                    }
                    if (maxDiscover == null || at.isAfter(maxDiscover)) maxDiscover = at;
                }

                boolean isStale = neverCount > 0;
                if (!isStale && maxDiscover != null
                        && lastAt instanceof OffsetDateTime assignedAt) {
                    isStale = maxDiscover.isAfter(assignedAt);
                }
                m.put("max_last_discover_at", TaxonomyRepository.utcIso(maxDiscover));
                m.put("never_discovered_count", neverCount);
                m.put("is_stale", isStale);
                result.add(m);
            }
            return result;
        });
    }

    /**
     * Audit collection: returns similarity distribution data and top receiving hub topics.
     * Python-side computes quantiles; we return sorted similarities + hub rows.
     */
    public Map<String, Object> auditCollectionData(String tenant, String collection, int topN) {
        return tenantScope.withTenant(tenant, ctx -> {
            var simRows = ctx.select(TOPIC_ASSIGNMENTS.SIMILARITY)
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                    .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(collection))
                    .and(TOPIC_ASSIGNMENTS.SIMILARITY.isNotNull()))
                .orderBy(TOPIC_ASSIGNMENTS.SIMILARITY.asc())
                .fetch();

            List<Double> sims = new ArrayList<>();
            for (var r : simRows) {
                sims.add(r.get(TOPIC_ASSIGNMENTS.SIMILARITY));
            }

            var hubRows = ctx.select(
                    TOPIC_ASSIGNMENTS.TOPIC_ID,
                    TOPICS.LABEL,
                    count().as("chunks"))
               .from(TOPIC_ASSIGNMENTS)
               .join(TOPICS).on(TOPICS.ID.eq(TOPIC_ASSIGNMENTS.TOPIC_ID))
               .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                   .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(collection)))
               .groupBy(TOPIC_ASSIGNMENTS.TOPIC_ID, TOPICS.LABEL)
               .orderBy(count().desc())
               .limit(topN)
               .fetch();

            List<Map<String, Object>> hubs = new ArrayList<>();
            for (var r : hubRows) {
                var m = new LinkedHashMap<String, Object>();
                m.put("topic_id", r.get(TOPIC_ASSIGNMENTS.TOPIC_ID));
                m.put("label", r.get(TOPICS.LABEL));
                m.put("chunk_count", r.get("chunks", Integer.class));
                hubs.add(m);
            }

            var result = new LinkedHashMap<String, Object>();
            result.put("collection", collection);
            result.put("similarities", sims);
            result.put("hub_rows", hubs);
            return result;
        });
    }

    /**
     * Topics carrying projection assignments that have no {@code topic_links}
     * row, restricted to those a link is structurally POSSIBLE for.
     *
     * <p>The predicate mirrors {@link TaxonomyRepository#refreshProjectionLinks} exactly, and that
     * is the whole point: a drift audit whose notion of "linkable" differs from
     * the materializer's reports fiction. {@code refreshProjectionLinks} pairs a
     * projection TARGET with a NON-projection SOURCE, so a topic is only drifted
     * if some doc gives it a non-projection partner. Two projection assignments
     * on one doc produce no link and are not drift; a lone assignment cannot
     * pair at all.
     *
     * <p>This route exists because the client-side check could not ask the
     * engine and audited the frozen SQLite migration source instead
     * (nexus-ypori) — reporting stale relic rows as live faults while the real
     * store went unexamined. Computing it here is also the only affordable
     * shape: reconstructing it client-side costs one round trip per topic plus
     * a bulk assignment read.
     *
     * @param limit max drift rows returned; the total count is exact regardless
     * @return {@code {projection_total, drift_count, rows:[{topic_id,label,collection}]}}
     */
    public Map<String, Object> linkDrift(String tenant, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            var tgt = TOPIC_ASSIGNMENTS.as("ta");
            var src = TOPIC_ASSIGNMENTS.as("ta2");

            var linkable = tgt.ASSIGNED_BY.eq("projection")
                .and(notExists(
                    ctx.selectOne().from(TOPIC_LINKS)
                       .where(TOPIC_LINKS.FROM_TOPIC_ID.eq(tgt.TOPIC_ID)
                           .or(TOPIC_LINKS.TO_TOPIC_ID.eq(tgt.TOPIC_ID)))))
                .and(exists(
                    ctx.selectOne().from(src)
                       .where(src.DOC_ID.eq(tgt.DOC_ID)
                           .and(src.TOPIC_ID.ne(tgt.TOPIC_ID))
                           .and(src.ASSIGNED_BY.ne("projection")))));

            int projectionTotal = ctx.select(countDistinct(TOPIC_ASSIGNMENTS.TOPIC_ID))
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection"))
                .fetchOne(0, int.class);

            int driftCount = ctx.select(countDistinct(tgt.TOPIC_ID))
                .from(tgt).where(linkable)
                .fetchOne(0, int.class);

            var rows = ctx.selectDistinct(tgt.TOPIC_ID, TOPICS.LABEL, tgt.SOURCE_COLLECTION)
                .from(tgt)
                .leftJoin(TOPICS).on(TOPICS.ID.eq(tgt.TOPIC_ID))
                .where(linkable)
                .orderBy(tgt.TOPIC_ID)
                .limit(Math.max(0, limit))
                .fetch();

            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) {
                var m = new LinkedHashMap<String, Object>();
                m.put("topic_id", r.get(tgt.TOPIC_ID));
                m.put("label", r.get(TOPICS.LABEL));
                m.put("collection", r.get(tgt.SOURCE_COLLECTION));
                out.add(m);
            }
            var result = new LinkedHashMap<String, Object>();
            result.put("projection_total", projectionTotal);
            result.put("drift_count", driftCount);
            result.put("rows", out);
            return result;
        });
    }

}
