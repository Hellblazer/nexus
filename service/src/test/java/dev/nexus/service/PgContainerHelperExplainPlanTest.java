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
}
