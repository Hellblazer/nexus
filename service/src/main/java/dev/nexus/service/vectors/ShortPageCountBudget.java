// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.concurrent.atomic.AtomicLong;
import java.util.function.IntSupplier;
import java.util.function.IntUnaryOperator;

/**
 * The time budget of a short page's manifest count (nexus-h7kw2).
 *
 * <p>The count ({@code manifest_qualifying_hits}) decides whether a short page is re-run. It is meant to be cheap,
 * and on an analyzed table it usually is (milliseconds). On a table the planner has not analyzed, with the gate's
 * matches at the end of the manifest, it walks every manifest row through the text gate and took 13 s at 220,000
 * chunks before the re-run it was meant to spare. Each statement of the count runs under its own
 * {@code statement_timeout}: the budget, unless a smaller bound applies (the bound in force, or in a fan-out arm the
 * arm's remaining budget). A route (a) count whose first read holds repeated chunks reads a second time, so a count
 * can spend the budget twice. A count the budget cancels answers "unknown", which re-runs the page: the decision the
 * page would have taken with no count at all, so the budget can cost a needless re-run and never a skipped one.
 *
 * <p>After a count that succeeded or answered "unknown", the bound in force before it is set again. That also rebinds
 * the connection's network timeout, which {@link PgSession#setLocal} binds with the GUC and which a rolled-back
 * savepoint does not undo.
 */
public final class ShortPageCountBudget {

    private static final Logger log = LoggerFactory.getLogger(ShortPageCountBudget.class);

    /** The variable that overrides {@link #DEFAULT_MS}; 1 to the search bound's maximum, validated at boot. */
    public static final String ENV = "NX_SHORT_PAGE_COUNT_BUDGET_MS";

    /**
     * The default budget in milliseconds. At 220,000 chunks the count measured 2 to 37 ms in most shapes, about 0.5 s
     * for the analyzed Hash Join, and 610 ms when the filter admits a few chunks, the case where the count saves a
     * 3.6 to 4.5 s re-run. The count reads the manifest, so these grow with its size: a larger tenant, or a loaded
     * host, can need a larger budget ({@link #ENV}).
     */
    static final int DEFAULT_MS = 1_000;

    private static final int CONFIGURED_MS = PgSession.boundedTimeoutMs(ENV, System.getenv(ENV), DEFAULT_MS);

    private static final String QUERY_CANCELED = "57014";

    private static volatile int budgetMs = CONFIGURED_MS;

    private static final AtomicLong SPENT = new AtomicLong();

    private ShortPageCountBudget() {
    }

    /** Boot-time touch for {@link #ENV}: a bad value fails the boot instead of the first short page. */
    public static int startupBudgetMs() {
        return CONFIGURED_MS;
    }

    /**
     * The bound set for one count.
     *
     * @param priorMs     {@code statement_timeout} before the count, in ms ({@code 0} for none)
     * @param budgetBinds the budget is the strictly smallest bound, so a cancel during the count is the budget's own
     *                    and never an arm's deadline
     */
    record Bound(long priorMs, boolean budgetBinds) {
    }

    /**
     * Set the count's bound. Calls {@code rebindTimeoutMs} once when it is given, so a spent fan-out budget is refused
     * here exactly as it was before the count had a budget of its own.
     */
    static Bound apply(DSLContext ctx, IntSupplier rebindTimeoutMs) {
        long prior = Long.parseLong(ctx.select(DSL.field(DSL.name("setting"), String.class))
                                       .from(DSL.table(DSL.name("pg_catalog", "pg_settings")))
                                       .where(DSL.field(DSL.name("name"), String.class).eq("statement_timeout"))
                                       .fetchSingle().value1());
        long other = rebindTimeoutMs != null ? rebindTimeoutMs.getAsInt() : prior;
        int budget = budgetMs;
        boolean budgetBinds = other <= 0 || budget < other;
        PgSession.setLocal(ctx, "statement_timeout", Long.toString(budgetBinds ? budget : other));
        return new Bound(prior, budgetBinds);
    }

    /** Set the bound in force before {@link #apply} again. */
    static void restore(DSLContext ctx, Bound bound) {
        PgSession.setLocal(ctx, "statement_timeout", Long.toString(bound.priorMs()));
    }

    /**
     * The count, counting and logging a cancel the budget caused. The failure still propagates, so the caller's
     * handling (roll back, answer "unknown") is unchanged.
     */
    static IntUnaryOperator watch(Bound bound, IntUnaryOperator count, String tenant, String collection) {
        return cap -> {
            try {
                return count.applyAsInt(cap);
            } catch (RuntimeException e) {
                if (bound.budgetBinds() && canceled(e)) {
                    SPENT.incrementAndGet();
                    log.info("event=vector_short_page_count_budget_spent tenant={} collection={} cap={} budget_ms={}",
                             tenant, collection, cap, budgetMs);
                }
                throw e;
            }
        };
    }

    private static boolean canceled(Throwable t) {
        int depth = 0;
        for (Throwable c = t; c != null && depth < 32; c = c.getCause(), depth++) {
            if (c instanceof java.sql.SQLException sql && QUERY_CANCELED.equals(sql.getSQLState())) {
                return true;
            }
        }
        return false;
    }

    /** Counts the budget cancelled; same shape as the repository's other short-page counters. */
    static long spentCount() {
        return SPENT.get();
    }

    /** Test seam: set the budget; {@code 0} restores the configured value. */
    static void overrideBudgetMsForTests(int ms) {
        budgetMs = ms == 0 ? CONFIGURED_MS : ms;
    }
}
