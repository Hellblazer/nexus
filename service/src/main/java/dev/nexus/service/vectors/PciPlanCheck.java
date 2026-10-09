// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.DSLContext;
import org.jooq.Select;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Savepoint;
import java.util.Objects;
import java.util.concurrent.atomic.AtomicLong;

/**
 * RDR-227 Step 2 (nexus-43ulx.25): the sampled check that the planner still reaches a collection's own index.
 *
 * <p>A per-collection index is used by an arm only when the planner can prove the arm's {@code collection =
 * ANY(p_collections)} implies the index's predicate, and only under a custom plan (A1). If either stops holding
 * (statistics drift, a path that lost {@code force_custom_plan}) the arm silently walks the leaf's shared index
 * again, which is today's behaviour but without the recall the index was built for. Nothing else would show it, so
 * one indexed HNSW arm in {@code every} runs {@code EXPLAIN} on its own statement, inside its own transaction and
 * under its own settings, and logs {@code event=pci_plan_check used=<bool> index=<name> collection=<name>}.
 * {@code used} is whether the plan text names the index; a {@code false} logs at WARN.
 *
 * <p><b>Sampling</b> is a counter, not a coin: the {@code every}-th sampled-eligible arm explains, so a test can
 * predict which arm that is. The caller counts only arms that can use an index (an arm the router sent exact, an
 * arm with no valid index and a multi-collection arm never reach {@link #sample}). The interval is a constructor
 * parameter; production takes {@link #DEFAULT_EVERY} and there is no environment variable for it.
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

    /** One indexed HNSW arm in this many is explained in production. */
    static final int DEFAULT_EVERY = 1000;

    /** Where the plan text comes from; a test replaces it to read or break the EXPLAIN. */
    @FunctionalInterface
    interface PlanSource {
        /** The plan of {@code statement} on {@code ctx}'s connection, as text. */
        String plan(DSLContext ctx, Select<?> statement);
    }

    private static final int MAX_ERROR_CHARS = 300;

    private final int every;
    private final PlanSource source;
    private final AtomicLong armsSeen = new AtomicLong();

    /**
     * @param every  explain every {@code every}-th arm handed to {@link #sample}; at least 1
     * @param source where the plan text comes from
     */
    PciPlanCheck(int every, PlanSource source) {
        if (every < 1) {
            throw new IllegalArgumentException("every must be at least 1, was " + every);
        }
        this.every = every;
        this.source = Objects.requireNonNull(source, "source");
    }

    /** The production check: one arm in {@link #DEFAULT_EVERY}, explained by jOOQ. */
    static PciPlanCheck production() {
        return new PciPlanCheck(DEFAULT_EVERY, (ctx, statement) -> ctx.explain(statement).plan());
    }

    /**
     * Count one indexed HNSW arm and, when it is the {@code every}-th, explain {@code statement} and log whether the
     * plan names {@code indexName}. Call it inside the arm's transaction, before the arm's own statement. Never
     * throws for a failure of the check itself.
     *
     * @param ctx        the arm's context, on the connection that holds the arm's transaction and settings
     * @param statement  the arm's statement
     * @param model      the embedding model of the arm's leaf
     * @param tenant     the arm's tenant
     * @param collection the collection the arm searches, for which the router believes a valid per-collection
     *                   index exists; the index's name is derived only when the arm is sampled
     */
    void sample(DSLContext ctx, Select<?> statement, String model, String tenant, String collection) {
        if (armsSeen.incrementAndGet() % every != 0) {
            return;
        }
        String indexName = PciCatalog.indexName(model, tenant, collection);
        Savepoint savepoint = null;
        try {
            Savepoint[] taken = new Savepoint[1];
            ctx.connection(conn -> taken[0] = conn.setSavepoint());
            savepoint = taken[0];
            String plan = source.plan(ctx, statement);
            release(ctx, savepoint);
            savepoint = null;
            boolean used = plan != null && plan.contains(indexName);
            if (used) {
                log.info("event=pci_plan_check used=true index={} collection={}", indexName, collection);
            } else {
                log.warn("event=pci_plan_check used=false index={} collection={}", indexName, collection);
            }
        } catch (RuntimeException e) {
            rollBack(ctx, savepoint);
            log.warn("event=pci_plan_check_failed index={} collection={} error={}", indexName, collection,
                describe(e));
        }
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
