// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TaxonomyCoverage;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PgVectorRepository.ArmRun;
import dev.nexus.service.vectors.PgVectorRepository.Candidate;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.AnnHit;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.Semaphore;

import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SET_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SET_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SET_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SET_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SET_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SET_768;

/**
 * RDR-134 Step 3 (bead nexus-7fku2.1.3): the routed half of one fan-out arm of {@code searchPerCollection}.
 *
 * <p>For a collection the request routes: (1) the coverage reading ({@link TopicRoute#coverage}, the Step 2 cache) must
 * say the collection has centroids, chunks, and coverage of at least the route's {@link TopicRoute#minCoverage()}
 * ({@link TaxonomyCoverage#ROUTE_MIN_COVERAGE}, 0.9, unless the request sets {@code topic_route.min_coverage}), else
 * {@code no_topics}; (2) the centroid ANN ({@link TaxonomyCentroidRepository#annQuery}, the 10 nearest) ranks the
 * collection's topics against the request's ONE query vector, which is passed in and never re-embedded; (3) the nearest
 * topic ({@code top1}) or every topic within {@code width} of it ({@code band}) is kept, similarity compared within this
 * collection only; (4) {@code nexus.search_topic_set_<dim>} searches the collection for the chunks that carry any kept
 * topic, with the arm's k and sensitivity exclusion and the function's own live-owner filter; (5) zero rows means
 * {@code fallback}, rows mean {@code routed}.
 *
 * <p><b>What degrades, what is isolated, what fails the request.</b> Three outcomes, by what went wrong.
 * <ul>
 *   <li><b>Degrades:</b> the collection is served unscoped ({@link Attempt#rows} is null and the caller runs its plain
 *       arm) and {@link Attempt#error} says in short text why. A coverage lookup that throws or answers nothing, a
 *       coverage count or a centroid ranking cut by its own bound, or a centroid ranking that fails otherwise, is
 *       {@code no_topics} (no topic set could be established). A topic search that fails, a statement timeout under the
 *       SEARCH bound included, is {@code fallback} (a topic set was established and the routed search did not deliver).</li>
 *   <li><b>Isolated to the collection, as an unrouted arm's would be:</b> the fan-out budget spent before a stage
 *       starts, or a topic-search statement cut by the fan-out bound. The collection reports
 *       {@code fanout_budget_exhausted} and no route status; it is not retried unscoped.</li>
 *   <li><b>Fails the whole request:</b> the request budget spent before a stage starts or while the topic search ran
 *       (a statement cut by the REQUEST bound), pool or admission exhaustion
 *       ({@link java.sql.SQLTransientConnectionException}), and a transient database failure (SQLSTATE class 08, 53,
 *       57P, 40001, 40P01, 55P03). The route's 503 semantics do not change.</li>
 * </ul>
 * {@link #propagates} is the one place this split is decided.
 *
 * <p><b>Bounds, and the clamp decision (S3).</b> One arm-gate slot is held for the three statements of the attempt and
 * released before the caller's unscoped re-run takes its own. The arm's request and fan-out budgets are checked before
 * each stage ({@link PgVectorRepository#armBound}) and bound the topic search's statement. The two routing reads before
 * it, the coverage count (cold) and the centroid ranking, are CLAMPED to half of what the arm has left
 * ({@link #stageShareMs}), so a slow cold read cannot use up the budget the unscoped arm needs: a read cut by its
 * share degrades the collection to {@code no_topics} (with {@code topic_route_error}) instead of turning a collection
 * the unrouted request would have served into a budget error. The alternative, reading coverage before taking the slot,
 * was not chosen: it would leave the read unbounded by the arm's budget. A collection whose cold count never fits its
 * share is never cached and so never routed; Step 5a measures the count's time.
 *
 * <p><b>Rows.</b> The topic function returns no chunk metadata, so a routed row is built with the five columns it has;
 * {@link #attachMetadata} then puts the stored metadata on the rows the merge kept, so a routed row is the row an
 * unscoped search returns for the same chunk. That read is bounded like a search statement.
 *
 * <p><b>Router levers not adopted (Step 1, critique S1).</b> None of Step 1's router levers is adopted here: not the
 * exact scan for small sets, not the widest {@code ef_search} without {@code pci_}, not the exact re-run of an empty
 * result. The zero-row fallback covers the empty case, and Step 5a measures short pages.
 *
 * <p><b>The threshold stays in Java.</b> The function takes none (vectors-062 decision 1); the fan-out merge applies the
 * client's to routed and unscoped candidates alike and counts {@code rawCount} and {@code dropped} on the arm's top-k.
 * <b>The zero-row fallback is judged BEFORE that threshold (critique S2, kept as it is).</b> A routed search that
 * returns rows is {@code routed} even when the client's threshold then drops every one of them: the collection then
 * contributes no rows, reports {@code routed} with {@code dropped == raw_count}, and is not retried unscoped (its
 * unscoped rows might have passed). The alternative, deciding {@code fallback} on the rows that survive the threshold,
 * would make the status depend on a client value and run a second search to learn it; a caller that needs the
 * difference reads {@code raw_count} and {@code dropped}, which the {@code search_topic_route} event carries per
 * collection. A routed collection's {@code min_raw_distance} is therefore the nearest distance within its routed subset, not the
 * collection's nearest, and a routed arm does not take part in choosing the recall canary's query-ring arm.
 */
final class TopicRouteArm {

    private static final Logger log = LoggerFactory.getLogger(TopicRouteArm.class);

    /** Centroids ranked per collection (RDR-134 Technical Design step 3). */
    static final int CENTROIDS_RANKED = 10;

    /**
     * What a routed arm decided. {@code rows} is the routed arm's candidates for {@link TopicRoute.Status#ROUTED}, and
     * {@code null} for every other status: the caller then runs the collection's unscoped arm. {@code error} is a short
     * text on the degrade paths (coverage, centroid ranking or topic search failed), else null. {@code coverage} is the
     * collection's coverage ratio where the reading measured one (centroids and chunks present), {@code topics} the
     * number of topics kept where the centroid ranking ran; null where not.
     */
    record Attempt(TopicRoute.Status status, List<Candidate> rows, String error, Double coverage, Integer topics) {
        /** A collection the route does not name (or a request it does not route): searched unscoped, nothing read. */
        static final Attempt SKIPPED = new Attempt(TopicRoute.Status.SKIPPED, null, null, null, null);
    }

    private final TenantScope tenantScope;
    private final TaxonomyCentroidRepository centroids;
    /** RDR-229 P3.5c: gate 3, read per statement (never cached); from gate 3 the topic-set search calls its {@code _by_id} twin. */
    private final Gate3 gate3;

    TopicRouteArm(TenantScope tenantScope) {
        this(tenantScope, Gate3.never());
    }

    TopicRouteArm(TenantScope tenantScope, Gate3 gate3) {
        this.tenantScope = tenantScope;
        this.gate3 = java.util.Objects.requireNonNull(gate3, "gate3");
        this.centroids = new TaxonomyCentroidRepository(tenantScope, gate3);
    }

    /**
     * Run the routed half of one collection's arm.
     *
     * @param qv the request's query vector, the one {@code queryVec} was built from (reused, never re-embedded)
     * @param k  the arm's per-collection k
     */
    Attempt attempt(String tenant, String model, String collection, int dim, float[] qv, Vector queryVec, int k,
                    boolean excludeSensitive, TopicRoute route, long requestDeadlineNanos, long fanoutDeadlineNanos,
                    int searchBoundMs, Semaphore armGate, ArmRun run) {
        PgVectorRepository.acquireArmSlot(armGate, requestDeadlineNanos, fanoutDeadlineNanos);
        try {
            // 1. coverage, clamped to a share of what the arm has left
            checkBudget(run, requestDeadlineNanos, fanoutDeadlineNanos, searchBoundMs);
            Optional<TaxonomyCoverage.Counts> counts;
            try {
                counts = route.coverage().lookup(tenant, collection, stageShareMs(run));
            } catch (RuntimeException e) {
                if (propagates(e, run, true)) {
                    throw e;
                }
                log.warn("event=topic_route_coverage_failed collection={} error={}", collection, e.toString());
                return new Attempt(TopicRoute.Status.NO_TOPICS, null, errorText("coverage lookup failed", e), null, null);
            }
            if (counts.isEmpty()) {
                log.warn("event=topic_route_coverage_unavailable collection={}", collection);
                return new Attempt(TopicRoute.Status.NO_TOPICS, null, "coverage unavailable", null, null);
            }
            TaxonomyCoverage.Counts read = counts.get();
            // chunks and unassigned are not measured without centroids, so no ratio is reported for such a collection
            Double coverage = read.hasTaxonomy() && read.chunks() > 0 ? read.coverage() : null;
            if (!read.routable(route.minCoverage())) {
                return new Attempt(TopicRoute.Status.NO_TOPICS, null, null, coverage, null);
            }
            // 2. the collection's 10 nearest centroids, from the request's vector, clamped as the coverage read was
            checkBudget(run, requestDeadlineNanos, fanoutDeadlineNanos, searchBoundMs);
            List<AnnHit> hits;
            try {
                hits = centroids.annQuery(tenant, qv, collection, false, CENTROIDS_RANKED, stageShareMs(run));
            } catch (RuntimeException e) {
                if (propagates(e, run, true)) {
                    throw e;
                }
                log.warn("event=topic_route_centroid_ranking_failed collection={} error={}", collection, e.toString());
                return new Attempt(TopicRoute.Status.NO_TOPICS, null, errorText("centroid ranking failed", e),
                                   coverage, null);
            }
            // 3. the topics kept
            Long[] topics = keep(hits, route);
            if (topics.length == 0) {
                return new Attempt(TopicRoute.Status.NO_TOPICS, null, null, coverage, 0);
            }
            // 4. the topic-set search; the kept set is never empty here, so the function is never walked for nothing
            try {
                List<Candidate> rows = topicSearch(tenant, model, collection, dim, queryVec, topics, k, excludeSensitive,
                                                   run, requestDeadlineNanos, fanoutDeadlineNanos, searchBoundMs);
                return rows.isEmpty() ? new Attempt(TopicRoute.Status.FALLBACK, null, null, coverage, topics.length)
                                      : new Attempt(TopicRoute.Status.ROUTED, rows, null, coverage, topics.length);
            } catch (RuntimeException e) {
                if (propagates(e, run, false)) {
                    throw e;
                }
                log.warn("event=topic_route_search_failed collection={} topics={} error={}", collection, topics.length,
                         e.toString());
                return new Attempt(TopicRoute.Status.FALLBACK, null, errorText("topic search failed", e), coverage,
                                   topics.length);
            }
        } finally {
            armGate.release();
        }
    }

    /** The topic ids kept from the centroid ranking, nearest first: the nearest alone, or all within the band. */
    static Long[] keep(List<AnnHit> hits, TopicRoute route) {
        if (hits.isEmpty()) {
            return new Long[0];
        }
        double best = Double.NEGATIVE_INFINITY;
        for (AnnHit h : hits) {
            best = Math.max(best, h.similarity());
        }
        List<Long> kept = new ArrayList<>();
        for (AnnHit h : hits) {
            boolean keepIt = switch (route.mode()) {
                case TOP1 -> kept.isEmpty() && h.similarity() == best;
                case BAND -> h.similarity() >= best - route.width();
            };
            if (keepIt) {
                kept.add(h.topicId());
            }
        }
        return kept.toArray(new Long[0]);
    }

    /** Refuse a spent request or fan-out budget before a stage starts; the throw is the arm's existing failure. */
    private static void checkBudget(ArmRun run, long requestDeadlineNanos, long fanoutDeadlineNanos,
                                    int searchBoundMs) {
        PgVectorRepository.ArmBound bound = PgVectorRepository.armBound(requestDeadlineNanos, fanoutDeadlineNanos,
                                                                        System.nanoTime(), searchBoundMs);
        run.limiter = bound.limiter();
        run.boundMs = bound.timeoutMs();
    }

    /**
     * True when {@code e} must keep its existing handling instead of degrading to the unscoped arm: the request
     * budget, the fan-out budget, pool or admission exhaustion, a transient database failure, and a statement cut by the
     * REQUEST or FAN-OUT bound ({@code run.limiter}, set at the stage's budget check). The two routing reads run under
     * their own clamp ({@code stageBounded}), a share of the arm's bound: a statement timeout there only says the read
     * was too slow, so it degrades whichever limiter the arm has (a budget really spent then fails the unscoped arm's
     * own budget check, which is that arm's existing handling).
     */
    static boolean propagates(RuntimeException e, ArmRun run, boolean stageBounded) {
        int depth = 0;
        for (Throwable c = e; c != null && depth < 32; c = c.getCause(), depth++) {
            if (c instanceof RequestDeadlineExceededException || c instanceof PgVectorRepository.FanoutBudgetSpentException
                    || c instanceof java.sql.SQLTransientConnectionException) {
                return true;
            }
        }
        String state = PgVectorRepository.firstSqlState(e);
        if ("57014".equals(state)) {
            return !stageBounded && run.limiter != PgVectorRepository.Limiter.SEARCH;
        }
        return PgVectorRepository.isTransientSqlState(state);
    }

    /** Half of what the arm's bound has left, for a routing read; at least 1 ms (Postgres reads 0 as disabled). */
    private static int stageShareMs(ArmRun run) {
        return Math.max(1, run.boundMs / 2);
    }

    /** A short wire text for a degraded stage: what failed, the SQLSTATE if any, and the root cause's class. No SQL, no message. */
    private static String errorText(String what, Throwable e) {
        Throwable root = e;
        for (int depth = 0; root.getCause() != null && root.getCause() != root && depth < 32; depth++) {
            root = root.getCause();
        }
        String state = PgVectorRepository.firstSqlState(e);
        return what + " (" + (state == null ? "" : "SQLSTATE " + state + ", ") + root.getClass().getSimpleName() + ")";
    }

    private List<Candidate> topicSearch(String tenant, String model, String collection, int dim, Vector queryVec,
                                        Long[] topics, int k, boolean excludeSensitive, ArmRun run,
                                        long requestDeadlineNanos, long fanoutDeadlineNanos, int searchBoundMs) {
        // RDR-229 P3.5c: gate 3 is read once per statement. From gate 3 the collection is resolved to its id in the statement's
        // transaction and search_topic_set_<dim>_by_id runs (it answers the registry's label); a label that resolves to no id
        // has no rows ("a NULL id is no collection"), which the caller reads as the unscoped fallback. The twins' sensitive anti
        // join fails open on an unstamped row, so they are called only after the latch.
        final boolean byId = gate3.holds();
        Result<? extends Record> result = tenantScope.withTenant(tenant, ctx -> {
            org.jooq.Table<?> fn;
            if (byId) {
                Long id = CollectionSelector.of(gate3, ctx, tenant, collection).idOf(collection);
                if (id == null) {
                    return null;
                }
                fn = switch (dim) {
                    case 384 -> SEARCH_TOPIC_SET_384_BY_ID.call(queryVec, topics, id, k, model, tenant, excludeSensitive);
                    case 768 -> SEARCH_TOPIC_SET_768_BY_ID.call(queryVec, topics, id, k, model, tenant, excludeSensitive);
                    case 1024 -> SEARCH_TOPIC_SET_1024_BY_ID.call(queryVec, topics, id, k, model, tenant, excludeSensitive);
                    default -> throw new IllegalArgumentException("unsupported dim " + dim);
                };
            } else {
                fn = switch (dim) {
                    case 384 -> SEARCH_TOPIC_SET_384.call(queryVec, topics, collection, k, model, tenant, excludeSensitive);
                    case 768 -> SEARCH_TOPIC_SET_768.call(queryVec, topics, collection, k, model, tenant, excludeSensitive);
                    case 1024 -> SEARCH_TOPIC_SET_1024.call(queryVec, topics, collection, k, model, tenant, excludeSensitive);
                    default -> throw new IllegalArgumentException("unsupported dim " + dim);
                };
            }
            PgSession.setSearchStatementTimeout(ctx, boundMs(run, requestDeadlineNanos, fanoutDeadlineNanos, searchBoundMs));
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            PgSession.setHnswEfSearch(ctx, k);
            PgSession.setHnswScanBudget(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            return ctx.selectFrom(fn).fetch();
        });
        if (result == null) {
            return List.of();
        }
        List<Candidate> out = new ArrayList<>(result.size());
        for (Record rec : result) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("id", rec.get("id", String.class));
            row.put("content", rec.get("content", String.class));
            row.put("distance", rec.get("distance", Double.class));
            row.put("collection", rec.get("collection", String.class));
            row.put("retention", rec.get("retention", String.class));
            out.add(new Candidate(rec.get("distance", Double.class), rec.get("id", String.class),
                                  rec.get("collection", String.class), row));
        }
        return out;
    }

    private static int boundMs(ArmRun run, long requestDeadlineNanos, long fanoutDeadlineNanos, int searchBoundMs) {
        checkBudget(run, requestDeadlineNanos, fanoutDeadlineNanos, searchBoundMs);
        return run.boundMs;
    }

    /**
     * Put the stored chunk metadata, flattened as {@code searchRow} does, on the merged rows of the collections whose
     * arm was routed. One by-id statement over the survivors (at most {@code limit} rows), in the request's one
     * (model, tenant) leaf, bounded by the search bound and what the request budget has left. A row whose chunk is gone
     * since the search keeps no metadata, as a row the plain search returned would not have been returned. A failure
     * fails the request, as the enrichment that follows would.
     */
    void attachMetadata(String tenant, String model, int dim, List<Map<String, Object>> rows, Set<String> routed,
                        int searchBoundMs, long requestDeadlineNanos) {
        Set<String> collections = new LinkedHashSet<>();
        Set<String> chashes = new LinkedHashSet<>();
        for (Map<String, Object> row : rows) {
            if (row.get("collection") instanceof String c && routed.contains(c) && row.get("id") instanceof String id) {
                collections.add(c);
                chashes.add(id);
            }
        }
        if (collections.isEmpty()) {
            return;
        }
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        int boundMs = searchBoundMs;
        if (requestDeadlineNanos != RequestDeadlineProbe.NONE) {
            long remaining = requestDeadlineNanos - System.nanoTime();
            if (remaining <= 0L) {
                throw new RequestDeadlineExceededException(
                    "request budget spent before the routed rows' metadata could be read",
                    RequestDeadlineExceededException.DEFAULT_RETRY_AFTER_SECONDS);
            }
            boundMs = (int) Math.max(1L, Math.min(boundMs, (remaining + 999_999L) / 1_000_000L));
        }
        final int statementBoundMs = boundMs;
        // RDR-229 P3.5c: gate 3 read once. From gate 3 the chunk rows are found by the registry id the routed labels resolve to,
        // and keyed by it (the row's own name is stale after a one-row rename; the row the search returned carries the label).
        final CollectionSelector sel = CollectionSelector.of(gate3, tenantScope, tenant, collections);
        var found = tenantScope.withTenant(tenant, ctx -> {
            PgSession.setSearchStatementTimeout(ctx, statementBoundMs);
            org.jooq.Field<String> key = sel.byId() ? DSL.cast(ch.collectionId(), String.class) : ch.collection();
            return ctx.select(key, ch.chash(), ch.metadata())
                      .from(ch.table())
                      .where(ch.embeddingModel().eq(model))
                      .and(ch.tenantId().eq(tenant))
                      .and(sel.in(ch.collection(), ch.collectionId()))
                      .and(ch.chash().in(chashes))
                      .fetch();
        });
        Map<String, Map<String, Object>> metadata = new HashMap<>(found.size() * 2);
        for (var rec : found) {
            if (rec.value3() != null) {
                metadata.put(rec.value1() + "\u0000" + rec.value2(), PgVectorRepository.fromJson(rec.value3().data()));
            }
        }
        for (Map<String, Object> row : rows) {
            if (row.get("collection") instanceof String c && routed.contains(c)) {
                Object rowKey = sel.byId() ? sel.idOf(c) : c;
                Map<String, Object> meta = rowKey == null ? null : metadata.get(rowKey + "\u0000" + row.get("id"));
                if (meta != null) {
                    row.putAll(meta);
                }
            }
        }
    }
}
