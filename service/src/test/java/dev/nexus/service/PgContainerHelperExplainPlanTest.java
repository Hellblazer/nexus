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
 * {@link PgContainerHelper#explainPlan} and {@link PgContainerHelper#explainPlanInlined} refuse a plan of more than
 * 50 rows (nexus-cbo4a batch 9): jOOQ's {@code Explain.plan()} sizes its table from the first 50 rows and abbreviates
 * the rest, so a {@code doesNotContain} over a longer plan would pass vacuously. No database: a jOOQ mock connection
 * answers the EXPLAIN with a canned plan of N rows, so the 50/51 boundary is exercised exactly.
 */
class PgContainerHelperExplainPlanTest {

    private static final Field<String> QUERY_PLAN = DSL.field(DSL.name("QUERY PLAN"), SQLDataType.VARCHAR);

    /** A context whose every statement answers with an {@code n}-row EXPLAIN result. */
    private static DSLContext contextAnswering(int n) {
        DSLContext fixture = DSL.using(SQLDialect.POSTGRES);
        Result<Record1<String>> plan = fixture.newResult(QUERY_PLAN);
        for (int i = 1; i <= n; i++) {
            var row = fixture.newRecord(QUERY_PLAN);
            row.set(QUERY_PLAN, "Node " + i + " (cost=0.00..1.00 rows=1 width=4)");
            plan.add(row);
        }
        return DSL.using(new MockConnection(ctx -> new MockResult[] {new MockResult(n, plan)}), SQLDialect.POSTGRES);
    }

    private static Query anyQuery(DSLContext ctx) {
        return ctx.selectOne();
    }

    @Test
    void aPlanOfExactlyFiftyRows_isReturnedWhole() {
        DSLContext ctx = contextAnswering(50);
        String plan = PgContainerHelper.explainPlan(ctx, anyQuery(ctx));
        assertThat(plan).contains("Node 1 ").contains("Node 50 ");
    }

    @Test
    void aPlanOfFiftyOneRows_isRefused() {
        DSLContext ctx = contextAnswering(51);
        assertThatThrownBy(() -> PgContainerHelper.explainPlan(ctx, anyQuery(ctx)))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("51 rows");
    }

    @Test
    void theInlinedVariant_holdsTheSameBoundary() {
        DSLContext ok = contextAnswering(50);
        assertThat(PgContainerHelper.explainPlanInlined(ok, anyQuery(ok))).contains("Node 50 ");
        DSLContext over = contextAnswering(51);
        assertThatThrownBy(() -> PgContainerHelper.explainPlanInlined(over, anyQuery(over)))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("51 rows");
    }
}
