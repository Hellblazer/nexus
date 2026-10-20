// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Query;
import org.jooq.Record1;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.jooq.tools.jdbc.MockConnection;
import org.jooq.tools.jdbc.MockResult;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * {@link PgContainerHelper#explainPlan} and {@link PgContainerHelper#explainPlanInlined} return every plan row
 * (nexus-cbo4a), with binds bound or inlined respectively. A non-vacuity pin against a jOOQ upgrade: in 3.20
 * {@code Explain.plan()} formats with no row cap (only {@code Result.toString()} stops at 50), so a
 * {@code doesNotContain} over a long plan sees every node. No database: a jOOQ mock connection answers the EXPLAIN
 * with a canned plan of N rows and records the statement it was sent.
 */
class PgContainerHelperExplainPlanTest {

    private static final Field<String> QUERY_PLAN = DSL.field(DSL.name("QUERY PLAN"), SQLDataType.VARCHAR);

    /** The SQL text and bind values of the last statement a {@link #contextAnswering} context received. */
    private static final class Seen {
        String sql;
        Object[] binds;
    }

    /** A context whose every statement answers with an {@code n}-row EXPLAIN result, recording what it was sent. */
    private static DSLContext contextAnswering(int n, Seen seen) {
        DSLContext fixture = DSL.using(SQLDialect.POSTGRES);
        Result<Record1<String>> plan = fixture.newResult(QUERY_PLAN);
        for (int i = 1; i <= n; i++) {
            var row = fixture.newRecord(QUERY_PLAN);
            row.set(QUERY_PLAN, "Node " + i + " (cost=0.00..1.00 rows=1 width=4)");
            plan.add(row);
        }
        return DSL.using(new MockConnection(ctx -> {
            seen.sql = ctx.sql();
            seen.binds = ctx.bindings();
            return new MockResult[] {new MockResult(n, plan)};
        }), SQLDialect.POSTGRES);
    }

    /** A query carrying one bind value, 4242, so bound versus inlined rendering is observable. */
    private static Query boundQuery(DSLContext ctx) {
        return ctx.selectOne().where(DSL.val(4242).eq(DSL.val(4242)));
    }

    private static long planRows(String plan) {
        return plan.lines().filter(l -> l.contains("Node ")).count();
    }

    @Test
    void aPlanPastFiftyRows_isReturnedWhole_withBindsBound() {
        Seen seen = new Seen();
        DSLContext ctx = contextAnswering(120, seen);
        String plan = PgContainerHelper.explainPlan(ctx, boundQuery(ctx));
        assertThat(planRows(plan)).isEqualTo(120);
        assertThat(plan).contains("Node 51 ").contains("Node 120 ");
        assertThat(seen.sql).startsWithIgnoringCase("explain ").contains("?").doesNotContain("4242");
        assertThat(seen.binds).contains(4242);
    }

    @Test
    void theInlinedVariant_returnsTheWholePlan_withBindsInlined() {
        Seen seen = new Seen();
        DSLContext ctx = contextAnswering(120, seen);
        String plan = PgContainerHelper.explainPlanInlined(ctx, boundQuery(ctx));
        assertThat(planRows(plan)).isEqualTo(120);
        assertThat(plan).contains("Node 120 ");
        assertThat(seen.sql).startsWithIgnoringCase("explain ").contains("4242").doesNotContain("?");
        assertThat(seen.binds).isEmpty();
    }

    @Test
    void explainPlanWith_sendsTheInlinedQueryAndFourBooleans_andReturnsEveryRow() {
        Seen seen = new Seen();
        DSLContext ctx = contextAnswering(120, seen);
        String plan = PgContainerHelper.explainPlanWith(ctx, boundQuery(ctx),
            PgContainerHelper.ExplainOptions.ANALYZE_VERBOSE_COSTS_OFF);
        assertThat(planRows(plan)).as("every row, no cap").isEqualTo(120);
        assertThat(seen.sql).contains("nexus_test").contains("explain_plan");
        // statement text first, with its bind inlined (a bind marker inside it would fail in EXECUTE), then the
        // four option booleans in the order analyze, verbose, buffers, costs
        assertThat(seen.binds).hasSize(5);
        assertThat((String) seen.binds[0]).startsWithIgnoringCase("select").contains("4242").doesNotContain("?");
        assertThat(seen.binds).containsSubsequence(true, true, false, false);
    }

    @Test
    void explainPlanWith_buffersOptionsAreTheOtherPreset() {
        Seen seen = new Seen();
        DSLContext ctx = contextAnswering(3, seen);
        PgContainerHelper.explainPlanWith(ctx, boundQuery(ctx), PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS);
        assertThat(seen.binds).containsSubsequence(true, false, true, true);
    }

    @Test
    void explainPlanWith_sendsEachBooleanInItsOwnSlot() {
        // buffers and costs differ in the third case, so their swap fails here; the analyze/costs swap fails in
        // explainPlanWith_sendsTheInlinedQueryAndFourBooleans_andReturnsEveryRow
        Seen seen = new Seen();
        DSLContext ctx = contextAnswering(1, seen);
        PgContainerHelper.explainPlanWith(ctx, boundQuery(ctx), new PgContainerHelper.ExplainOptions(true, false, false, true));
        assertThat(java.util.Arrays.copyOfRange(seen.binds, 1, 5)).containsExactly(true, false, false, true);
        PgContainerHelper.explainPlanWith(ctx, boundQuery(ctx), new PgContainerHelper.ExplainOptions(false, true, true, false));
        assertThat(java.util.Arrays.copyOfRange(seen.binds, 1, 5)).containsExactly(false, true, true, false);
        PgContainerHelper.explainPlanWith(ctx, boundQuery(ctx), new PgContainerHelper.ExplainOptions(false, false, true, false));
        assertThat(java.util.Arrays.copyOfRange(seen.binds, 1, 5)).containsExactly(false, false, true, false);
    }

    @Test
    void aPlanWhoseLateRowsAreWiderThanItsFirstFifty_isRefused_notReturnedCut() {
        // jOOQ's text format sizes the column from the first 50 rows and cuts a wider later row to that width
        Seen seen = new Seen();
        DSLContext fixture = DSL.using(SQLDialect.POSTGRES);
        Result<Record1<String>> plan = fixture.newResult(QUERY_PLAN);
        for (int i = 1; i <= 60; i++) {
            var row = fixture.newRecord(QUERY_PLAN);
            row.set(QUERY_PLAN, i <= 50 ? "Node " + i : "Node " + i + " Filter: (a very long late condition " + "x".repeat(80) + ")");
            plan.add(row);
        }
        DSLContext ctx = DSL.using(new MockConnection(c -> {
            seen.sql = c.sql();
            return new MockResult[] {new MockResult(60, plan)};
        }), SQLDialect.POSTGRES);
        assertThatThrownBy(() -> PgContainerHelper.explainPlan(ctx, boundQuery(ctx)))
            .isInstanceOf(IllegalStateException.class).hasMessageContaining("explainPlanWith");
        assertThatThrownBy(() -> PgContainerHelper.explainPlanInlined(ctx, boundQuery(ctx)))
            .isInstanceOf(IllegalStateException.class).hasMessageContaining("explainPlanWith");
    }
}
