// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciIndexSet.ValidIndex;
import org.jooq.DSLContext;
import org.jooq.Select;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Savepoint;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashSet;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * RDR-227 Step 2 (nexus-43ulx.25): the sampled check that the planner still reaches a collection's own index.
 *
 * <p>A per-collection index is used by an arm only when the planner can prove the arm's {@code collection =
 * ANY(p_collections)} implies the index's predicate, and only under a custom plan (A1). If either stops holding
 * (statistics drift, a path that lost {@code force_custom_plan}) the arm silently walks the leaf's shared index
 * again, which is today's behaviour but without the recall the index was built for. Nothing else would show it, so
 * a sampled indexed HNSW arm runs {@code EXPLAIN} on its own statement, inside its own transaction and
 * under its own settings, and logs {@code event=pci_plan_check used=<bool> index=<name> collection=<name>}.
 * {@code used} is whether the plan text names the index; a {@code false} logs at WARN and adds the plan's top node
 * and the scans the planner chose instead ({@code top=... scans=...}), so the cause is in the line.
 *
 * <p><b>Sampling</b> has three triggers, any one of which samples the arm. (1) The first eligible arm for an index
 * after the router's set gains it ({@link ValidIndex#since}), so a new or recovered index is checked at once and a
 * verification window after a deploy sees a line per index. (2) At most one arm per index per {@code interval}
 * (default {@link #DEFAULT_INTERVAL}), so a quiet index is still checked and a busy one is not checked more often. (3)
 * The counter: the {@code every}-th eligible arm (default {@link #DEFAULT_EVERY}) across all indexes. <b>The counter
 * is process-global</b> (one check serves the whole repository, so a busy collection takes most of its samples and a
 * quiet one almost none); the per-index triggers are what cover the quiet ones. A counter-only check ({@code
 * interval == null}) is what a test uses to predict which arm samples. The caller counts only arms that can use
 * an index (an arm the router sent exact, an arm with no valid index and a multi-collection arm never reach {@link
 * #sample}). Both parameters are constructor arguments; there is no environment variable for them.
 *
 * <p><b>The index is matched by the name the router's set holds</b> ({@link ValidIndex#name}), not by recomputing
 * {@link PciCatalog#indexName}, so a {@code pci_} index the sweep admitted under a different hash is still found in
 * the plan. A set that derives names (a test double) falls back to the computed name.
 *
 * <p><b>A failed EXPLAIN never fails the arm.</b> The EXPLAIN shares the arm's transaction, and a statement error
 * inside a transaction aborts it (SQLSTATE 25P02 on every later statement) unless the error is rolled back to a
 * savepoint. So the EXPLAIN runs between a JDBC savepoint and its release; on failure the savepoint is rolled back
 * to, the failure is logged as {@code event=pci_plan_check_failed}, and the arm's statement runs next as if the
 * check had not been there. The savepoint is taken through {@link DSLContext#connection}, never
 * {@code ctx.transaction(...)}: on a context over a connection whose transaction the caller owns, jOOQ's
 * transaction would COMMIT it.
 *
 * <p><b>The EXPLAIN is jOOQ's typed one</b> ({@link DSLContext#explain}), run with the statement's own bind values
 * ({@code ExplainQuery} prepares {@code explain <statement>} with the same binds), so {@code p_collections} is
 * bound as the arm binds it and the planner folds the bound array into the plan, as it does for the arm.
 */
final class PciPlanCheck {

    private static final Logger log = LoggerFactory.getLogger(PciPlanCheck.class);

    /** One indexed HNSW arm in this many is explained in production, whichever index it uses. */
    static final int DEFAULT_EVERY = 1000;

    /** At most one arm per index in this long is explained in production, besides the first after the index appears. */
    static final Duration DEFAULT_INTERVAL = Duration.ofMinutes(15);

    /** Where the plan text comes from; a test replaces it to read or break the EXPLAIN. */
    @FunctionalInterface
    interface PlanSource {
        /** The plan of {@code statement} on {@code ctx}'s connection, as text. */
        String plan(DSLContext ctx, Select<?> statement);
    }

    private static final int MAX_ERROR_CHARS = 300;

    private final int every;
    private final Duration interval;
    private final Clock clock;
    private final PlanSource source;
    private final AtomicLong armsSeen = new AtomicLong();
    /** Index name to when it was last sampled; bounded by the number of indexes the router has ever listed. */
    private final ConcurrentHashMap<String, Instant> lastSampled = new ConcurrentHashMap<>();

    /**
     * A counter-only check: explain every {@code every}-th arm handed to {@link #sample}, nothing else.
     *
     * @param every  at least 1
     * @param source where the plan text comes from
     */
    PciPlanCheck(int every, PlanSource source) {
        this(every, null, Clock.systemUTC(), source);
    }

    /**
     * @param every    explain every {@code every}-th arm handed to {@link #sample}; at least 1
     * @param interval at most one arm per index per this long, and the first arm after an index appears; {@code
     *                 null} turns both off (counter only); otherwise positive
     * @param clock    the time of the per-index triggers
     * @param source   where the plan text comes from
     */
    PciPlanCheck(int every, Duration interval, Clock clock, PlanSource source) {
        if (every < 1) {
            throw new IllegalArgumentException("every must be at least 1, was " + every);
        }
        if (interval != null && (interval.isZero() || interval.isNegative())) {
            throw new IllegalArgumentException("interval must be positive, was " + interval);
        }
        this.every = every;
        this.interval = interval;
        this.clock = Objects.requireNonNull(clock, "clock");
        this.source = Objects.requireNonNull(source, "source");
    }

    /**
     * The production check: {@link #DEFAULT_EVERY} and {@link #DEFAULT_INTERVAL}, explained by jOOQ. Package-private
     * accessors ({@link #every()}, {@link #interval()}) let a test pin the values the repository ships with.
     */
    static PciPlanCheck production() {
        return new PciPlanCheck(DEFAULT_EVERY, DEFAULT_INTERVAL, Clock.systemUTC(),
            (ctx, statement) -> ctx.explain(statement).plan());
    }

    int every() {
        return every;
    }

    /** The per-index interval, or {@code null} for a counter-only check. */
    Duration interval() {
        return interval;
    }

    /**
     * Count one eligible arm and say whether it is to be sampled; see the class comment for the three triggers. A
     * sampled arm's index is stamped as just sampled, so concurrent arms of the same index do not all sample.
     */
    boolean due(ValidIndex index) {
        boolean byCounter = armsSeen.incrementAndGet() % every == 0;
        if (interval == null) {
            return byCounter;
        }
        Instant now = clock.instant();
        Instant last = lastSampled.get(index.name());
        if (!byCounter && last != null && !last.isBefore(index.since())
            && Duration.between(last, now).compareTo(interval) < 0) {
            return false;
        }
        boolean[] sampled = {false};
        lastSampled.compute(index.name(), (name, previous) -> {
            boolean first = previous == null || previous.isBefore(index.since());
            boolean elapsed = previous != null && Duration.between(previous, now).compareTo(interval) >= 0;
            if (byCounter || first || elapsed) {
                sampled[0] = true;
                return now;
            }
            return previous;
        });
        return sampled[0];
    }

    /**
     * Count one indexed HNSW arm and, when it is due, explain its statement and log whether the plan names the
     * index. Call it inside the arm's transaction, before the arm's own statement. Never throws for a failure of the
     * check itself. The statement is built only for a sampled arm.
     *
     * @param ctx        the arm's context, on the connection that holds the arm's transaction and settings
     * @param statement  builds the arm's statement; called only when the arm is sampled
     * @param index      the index the router's set holds for the arm's collection
     * @param collection the collection the arm searches, for the log line
     * @return whether the arm was sampled, so the caller can leave the sample's time out of the arm's own
     */
    boolean sample(DSLContext ctx, Supplier<Select<?>> statement, ValidIndex index, String collection) {
        if (!due(index)) {
            return false;
        }
        String indexName = index.name();
        Savepoint savepoint = null;
        try {
            Savepoint[] taken = new Savepoint[1];
            ctx.connection(conn -> taken[0] = conn.setSavepoint());
            savepoint = taken[0];
            String plan = source.plan(ctx, statement.get());
            release(ctx, savepoint);
            savepoint = null;
            boolean used = plan != null && plan.contains(indexName);
            if (used) {
                log.info("event=pci_plan_check used=true index={} collection={}", indexName, collection);
            } else {
                log.warn("event=pci_plan_check used=false index={} collection={} {}", indexName, collection,
                    describePlan(plan));
            }
        } catch (RuntimeException e) {
            rollBack(ctx, savepoint);
            log.warn("event=pci_plan_check_failed index={} collection={} error={}", indexName, collection,
                describe(e));
        }
        return true;
    }

    private static final Pattern TOP_NODE = Pattern.compile("^\\s*(?:->\\s*)?(.*?)\\s+\\(cost=");
    private static final Pattern SCAN = Pattern.compile(
        "(?:Bitmap )?(?:Index(?: Only)? Scan|Seq Scan|Bitmap Heap Scan)(?: Backward)?(?: using \\S+)?(?: on \\S+)?");
    private static final int MAX_SCANS = 5;

    /**
     * What a plan that did not name the index did instead: {@code top="<first node>" scans="<scan nodes, ';' joined>"}.
     * The index names the planner chose are in the scan nodes ({@code Index Scan using <name> on <relation>}). Reads
     * only the text, so it cannot fail on a plan shape it does not know: it then says {@code top="" scans=""}.
     */
    static String describePlan(String plan) {
        String top = "";
        Set<String> scans = new LinkedHashSet<>();
        if (plan != null) {
            for (String line : plan.split("\\R")) {
                if (top.isEmpty()) {
                    Matcher m = TOP_NODE.matcher(line);
                    if (m.find()) {
                        top = m.group(1);
                    }
                }
                Matcher s = SCAN.matcher(line);
                if (s.find() && scans.size() < MAX_SCANS) {
                    scans.add(s.group());
                }
            }
        }
        return "top=\"" + top + "\" scans=\"" + String.join("; ", scans) + "\"";
    }

    /** Undo whatever the failed EXPLAIN did to the transaction; a failure here is logged, and the arm decides. */
    private static void rollBack(DSLContext ctx, Savepoint savepoint) {
        if (savepoint == null) {
            return;
        }
        try {
            ctx.connection(conn -> {
                conn.rollback(savepoint);
                conn.releaseSavepoint(savepoint);
            });
        } catch (RuntimeException e) {
            log.warn("event=pci_plan_check_rollback_failed error={}", describe(e));
        }
    }

    private static void release(DSLContext ctx, Savepoint savepoint) {
        try {
            ctx.connection(conn -> conn.releaseSavepoint(savepoint));
        } catch (RuntimeException e) {
            // The savepoint lives until the arm's transaction ends; nothing depends on releasing it.
            log.debug("event=pci_plan_check_release_failed error={}", describe(e));
        }
    }

    private static String describe(RuntimeException e) {
        String message = e.getMessage() == null ? "" : e.getMessage().replace('\n', ' ');
        if (message.length() > MAX_ERROR_CHARS) {
            message = message.substring(0, MAX_ERROR_CHARS) + "...";
        }
        return e.getClass().getSimpleName() + ": " + message;
    }
}
