// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository.WherePlan;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.Table;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Semaphore;

import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_BY_COLLECTION_1024_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_BY_COLLECTION_384_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_BY_COLLECTION_768_BY_ID;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_768;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_BY_COLLECTION_1024;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_BY_COLLECTION_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATE_PROBE_BY_COLLECTION_768;

/**
 * The text gate's probe statements and the request-wide probe of a {@code searchPerCollection} lexical leg
 * (nexus-bbtc2), moved out of {@link PgVectorRepository} (hot rule: move the area first). Stateless: the shared answer
 * lives in {@link SharedLexicalProbe}, one per request.
 *
 * <p>{@code HnswServingGucParityTest} scans this file as it scans the repository: {@link #runShared} bounds the
 * statement and pins the plan mode before it fetches.
 */
final class LexicalGateProbe {

    private static final Logger log = LoggerFactory.getLogger(LexicalGateProbe.class);

    /**
     * Engine switch for the parallel workers the shared probe's step 1 may use ({@code SET LOCAL
     * max_parallel_workers_per_gather} for that statement's transaction, nexus-bbtc2): a non-negative integer, {@code 0}
     * for serial. The workers come from the cluster-wide {@code max_parallel_workers} pool, which the engine's arm
     * permits do not count, so the default is small and a cloud engine should measure before it raises it.
     */
    static final String PROBE_WORKERS_ENV = "NX_SEARCH_ROUTE_PROBE_WORKERS";

    static final int DEFAULT_PROBE_WORKERS = 2;

    /** An engine-side ceiling on the setting, so a typo cannot ask Postgres for hundreds of workers a request. */
    static final int MAX_PROBE_WORKERS = 16;

    /** SQLSTATE the by-collection function raises when its gate admits more rows than it will gather (vectors-060). */
    static final String GATE_CEILING_SQLSTATE = "54000";

    private LexicalGateProbe() {
    }

    /**
     * The probe cap for a lexical arm's gate: one more than {@code selectiveGateMax}, so that a collection whose gate
     * admits more than {@code selectiveGateMax} rows is the one whose probe comes back with {@code selectiveGateMax + 1}.
     * Both the per-arm probe ({@code hybridRank}) and the shared one use it.
     */
    static int probeCap(int selectiveGateMax) {
        return selectiveGateMax + 1;
    }

    /** {@link #PROBE_WORKERS_ENV} read from {@code raw}: {@code 0..MAX_PROBE_WORKERS}, else the default (logged). */
    static int probeWorkers(String raw) {
        if (raw == null || raw.isBlank()) {
            return DEFAULT_PROBE_WORKERS;
        }
        try {
            int v = Integer.parseInt(raw.trim());
            if (v >= 0) {
                return Math.min(v, MAX_PROBE_WORKERS);
            }
        } catch (NumberFormatException ignored) {
            // falls through to the warning
        }
        log.warn("event=search_fanout_setting_invalid name={} raw={} using={} expected=non_negative_integer",
                 PROBE_WORKERS_ENV, raw, DEFAULT_PROBE_WORKERS);
        return DEFAULT_PROBE_WORKERS;
    }

    /**
     * The shared probe's statement bound: at most half of what the fan-out and request budgets have left, and at most
     * the search bound, never below 1 ms. A probe that runs out is cancelled and the arms run their own probes, so the
     * other half is theirs.
     *
     * @throws PgVectorRepository.FanoutBudgetSpentException or {@code RequestDeadlineExceededException} as
     *         {@link PgVectorRepository#armBound} does, when a budget is already spent
     */
    static int probeBoundMs(long requestDeadlineNanos, long fanoutDeadlineNanos, long nowNanos, int searchBoundMs) {
        int remainingMs = PgVectorRepository.armBound(requestDeadlineNanos, fanoutDeadlineNanos, nowNanos,
            Integer.MAX_VALUE).timeoutMs();
        return Math.max(1, Math.min(searchBoundMs, remainingMs / 2));
    }

    /**
     * The trigram settings every gated statement of a lexical arm runs under, set once per transaction: the trigram
     * threshold and which form of the trigram half the gate uses ({@code trgmMode}). The statement bound and the
     * custom-plan pin stay at each caller, where {@code HnswServingGucParityTest} reads them.
     */
    static void setGateGucs(DSLContext ctx, String trgmMode) {
        // Trigram gate calibration (contract anchor): word_similarity >= 0.6, pg_trgm's
        // default - typo-probe candidates sit at ~0.9 and pass, no-signal rows at ~0.1
        // do not. Pinned per-transaction so the gate is independent of cluster config --
        // every schema function below is LANGUAGE sql and cannot SET LOCAL its own GUC.
        PgSession.setLocal(ctx, "pg_trgm.word_similarity_threshold", "0.6");
        // nexus-seo4k / nexus-bbtc2: which form of the trigram half every gated statement below uses: 'off' (more than
        // LEXICAL_TRGM_MAX_WORDS words) drops it, 'substring' (one token) replaces it with a substring match, 'on' keeps
        // word_similarity. The probe (vectors-058-1) reads all three; the row-gating functions (vectors-058-2: the
        // chash_allow rank, the manifest paths, HNSW-first) read 'substring' only. They run in the chash_allow branch
        // too, so it is set before that branch, and set either way so
        // a second hybridRank in one transaction never inherits the first one's value (transaction-local: a pooled
        // connection never carries it further).
        PgSession.setLocal(ctx, PgVectorRepository.LEXICAL_TRGM_GUC, trgmMode);
    }

    /** The text that reaches the gate: a one-token text ({@code substring} mode) is the token itself, trimmed. */
    static String gateQueryText(String rawGateText, String trgmMode) {
        return PgVectorRepository.LEXICAL_TRGM_SUBSTRING.equals(trgmMode) ? rawGateText.strip() : rawGateText;
    }

    /** {@code text_gate_probe_<dim>}: the chashes the text gate admits in {@code colls}, at most {@code cap}. */
    static Table<?> textGateProbeFn(int dim, String gateQueryText, CollectionSelector sel, WherePlan wherePlan, int cap,
                                    String model, String tenant) {
        if (sel.byId()) {
            // RDR-229 P3.5c: gate 3 holds; the twin matches by the resolved ids (vectors-077-100..102).
            Long[] ids = sel.idArray();
            return switch (dim) {
                case 384  -> TEXT_GATE_PROBE_384_BY_ID.call(
                    gateQueryText, ids, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
                case 768  -> TEXT_GATE_PROBE_768_BY_ID.call(
                    gateQueryText, ids, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
                case 1024 -> TEXT_GATE_PROBE_1024_BY_ID.call(
                    gateQueryText, ids, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
                default   -> throw new IllegalArgumentException("unsupported dim " + dim);
            };
        }
        String[] colls = sel.nameArray();
        return switch (dim) {
            case 384  -> TEXT_GATE_PROBE_384.call(
                gateQueryText, colls, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
            case 768  -> TEXT_GATE_PROBE_768.call(
                gateQueryText, colls, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
            case 1024 -> TEXT_GATE_PROBE_1024.call(
                gateQueryText, colls, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    /**
     * {@code text_gate_probe_by_collection_<dim>} (vectors-060): {@code (collection, chash)} for the rows the gate admits
     * in {@code colls}, capped per collection; a collection that reached {@code cap} rows is one {@code (collection,
     * NULL)} row.
     */
    static Table<?> textGateProbeByCollectionFn(int dim, String gateQueryText, CollectionSelector sel, WherePlan wherePlan,
                                                int cap, String model, String tenant) {
        if (sel.byId()) {
            Long[] ids = sel.idArray();
            return switch (dim) {
                case 384  -> TEXT_GATE_PROBE_BY_COLLECTION_384_BY_ID.call(
                    gateQueryText, ids, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
                case 768  -> TEXT_GATE_PROBE_BY_COLLECTION_768_BY_ID.call(
                    gateQueryText, ids, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
                case 1024 -> TEXT_GATE_PROBE_BY_COLLECTION_1024_BY_ID.call(
                    gateQueryText, ids, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
                default   -> throw new IllegalArgumentException("unsupported dim " + dim);
            };
        }
        String[] colls = sel.nameArray();
        return switch (dim) {
            case 384  -> TEXT_GATE_PROBE_BY_COLLECTION_384.call(
                gateQueryText, colls, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
            case 768  -> TEXT_GATE_PROBE_BY_COLLECTION_768.call(
                gateQueryText, colls, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
            case 1024 -> TEXT_GATE_PROBE_BY_COLLECTION_1024.call(
                gateQueryText, colls, wherePlan.containment(), wherePlan.jsonPath(), cap, model, tenant);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

    /**
     * The one text-gate probe of a request's lexical leg, run as the first fan-out task on a worker, under an arm slot
     * like any arm, and bounded by {@link #probeBoundMs}. It never throws and always completes {@code share}: any
     * failure means the arms run their own probes, which is what they did before.
     *
     * <p>One {@code text_gate_probe_by_collection_<dim>} statement (vectors-060) with the cap an arm's own probe uses
     * ({@link #probeCap}) says, per collection, which chashes it has or that it is dense. There is no capped statement
     * over every collection first: on a leaf where the trigram half admits many candidates (D, {@code advisory lock},
     * about 40,000 rechecked for 7,900 gated) that statement alone cost 5.1 s and never decides anything the
     * per-collection statement does not (T2 nexus/bbtc2-dense-leaf-bound-2026-11-14).
     *
     * <p>If the probe runs past its bound, or the gate admits more rows than the function will gather (SQLSTATE
     * {@value #GATE_CEILING_SQLSTATE}), the answer is the fallback and each arm runs its own probe with what the budget
     * has left; the reason is logged.
     *
     * @param workers {@code max_parallel_workers_per_gather} for the statement's transaction ({@link #probeWorkers})
     */
    static void runShared(TenantScope tenantScope, SharedLexicalProbe share, String tenant, String model, int dim,
                           String gateText, CollectionSelector sel,
                           WherePlan wherePlan, int selectiveGateMax, int workers, long requestDeadlineNanos,
                           long fanoutDeadlineNanos, int searchBoundMs, Semaphore armGate) {
        final long startNanos = System.nanoTime();
        try {
            PgVectorRepository.acquireArmSlot(armGate, requestDeadlineNanos, fanoutDeadlineNanos);
            try {
                java.util.function.IntSupplier bound = () ->
                    probeBoundMs(requestDeadlineNanos, fanoutDeadlineNanos, System.nanoTime(), searchBoundMs);
                final String trgmMode = PgVectorRepository.lexicalTrgmMode(gateText);
                final String gateQueryText = gateQueryText(gateText, trgmMode);
                final int cap = probeCap(selectiveGateMax);
                Runnable complete = tenantScope.withTenant(tenant, ctx -> {
                    PgSession.setSearchStatementTimeout(ctx, bound.getAsInt());
                    setGateGucs(ctx, trgmMode);
                    PgSession.setSearchPlanCacheMode(ctx);
                    // The workers a statement may use are the engine's to set, not the function's: see vectors-060.
                    PgSession.setLocal(ctx, "max_parallel_workers_per_gather", Integer.toString(workers));
                    Table<?> fn = textGateProbeByCollectionFn(dim, gateQueryText, sel, wherePlan, cap, model, tenant);
                    Result<? extends Record> per = ctx.selectFrom(fn).fetch();
                    Map<String, List<byte[]>> rowsOf = new HashMap<>();
                    Set<String> dense = new HashSet<>();
                    for (Record r : per) {
                        String collection = r.get(0, String.class);
                        byte[] chash = r.get(1, byte[].class);
                        if (chash == null) {
                            dense.add(collection);
                        } else {
                            rowsOf.computeIfAbsent(collection, c -> new ArrayList<>()).add(chash);
                        }
                    }
                    Map<String, byte[][]> chashesOf = new HashMap<>();
                    rowsOf.forEach((collection, rows) -> chashesOf.put(collection, SharedLexicalProbe.distinct(rows)));
                    return () -> share.completePerCollection(chashesOf, dense, System.nanoTime() - startNanos);
                });
                complete.run();
            } finally {
                armGate.release();
            }
        } catch (RuntimeException e) {
            String state = sqlState(e);
            log.info("event=search_per_collection_shared_probe_fallback reason={} sqlstate={} error_class={} error={}",
                     reason(e, state), state, e.getClass().getSimpleName(), e.getMessage());
        } finally {
            share.completeFallback(System.nanoTime() - startNanos);   // a no-op once one of the answers above landed
        }
    }

    private static String sqlState(Throwable e) {
        for (Throwable t = e; t != null; t = t.getCause()) {
            if (t instanceof SQLException se && se.getSQLState() != null) {
                return se.getSQLState();
            }
        }
        return "";
    }

    private static String reason(Throwable e, String sqlState) {
        if (GATE_CEILING_SQLSTATE.equals(sqlState)) {
            return "gate_ceiling";
        }
        if ("57014".equals(sqlState)) {
            return "statement_timeout";
        }
        if (e instanceof PgVectorRepository.FanoutBudgetSpentException || e instanceof RequestDeadlineExceededException) {
            return "budget_spent";
        }
        return "error";
    }
}
