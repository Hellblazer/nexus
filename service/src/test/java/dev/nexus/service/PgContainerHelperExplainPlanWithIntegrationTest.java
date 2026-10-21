// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Record1;
import org.jooq.Select;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * {@code nexus_test.explain_plan} against a real server (nexus-cbo4a batch C). The mock-connection
 * PgContainerHelperExplainPlanTest pins what the wrapper sends; this pins what the function does with it. Each option
 * is shown to reach EXPLAIN (a mock could not see that format() rendered booleans as t/f, which EXPLAIN refuses),
 * a plan past 50 rows comes back whole, a NULL argument is refused, and the function runs in the caller's
 * transaction, so a SET LOCAL planner pin changes the plan it reports.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgContainerHelperExplainPlanWithIntegrationTest {

    private static final PgContainerHelper.ExplainOptions PLAIN = new PgContainerHelper.ExplainOptions(false, false, false, true);

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    /** A count over pg_class: a catalog read, so ANALYZE and BUFFERS always have work to report. */
    private static Select<?> catalogCount(DSLContext ctx) {
        return ctx.selectCount().from(DSL.table(DSL.name("pg_catalog", "pg_class")));
    }

    @Test
    void eachOption_reachesExplain() throws Exception {
        try (Connection c = pg.createConnection("")) {
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            String plain = PgContainerHelper.explainPlanWith(ctx, catalogCount(ctx), PLAIN);
            assertThat(plain).contains("cost=").doesNotContain("actual time=").doesNotContain("Output:");

            String analyzed = PgContainerHelper.explainPlanWith(ctx, catalogCount(ctx),
                new PgContainerHelper.ExplainOptions(true, false, false, true));
            assertThat(analyzed).contains("actual time=").contains("Execution Time:");

            assertThat(PgContainerHelper.explainPlanWith(ctx, catalogCount(ctx),
                new PgContainerHelper.ExplainOptions(false, false, false, false))).doesNotContain("cost=");
            assertThat(PgContainerHelper.explainPlanWith(ctx, catalogCount(ctx),
                new PgContainerHelper.ExplainOptions(false, true, false, true))).contains("Output:");
            assertThat(PgContainerHelper.explainPlanWith(ctx, catalogCount(ctx),
                PgContainerHelper.ExplainOptions.ANALYZE_BUFFERS)).contains("Buffers:");
        }
    }

    @Test
    void aPlanPastFiftyRows_comesBackWhole() throws Exception {
        try (Connection c = pg.createConnection("")) {
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            Select<Record1<Integer>> wide = ctx.select(DSL.val(0));
            for (int i = 1; i < 60; i++) {
                wide = wide.unionAll(ctx.select(DSL.val(i)));
            }
            String plan = PgContainerHelper.explainPlanWith(ctx, wide, PLAIN);
            assertThat(plan.lines().filter(l -> l.contains("Result")).count())
                .as("one Result node per union arm, all 60, past jOOQ's 50-row format cap. Plan:%n%s", plan)
                .isEqualTo(60);
        }
    }

    @Test
    void aNullArgument_isRefused() throws Exception {
        try (Connection c = pg.createConnection("")) {
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            assertThatThrownBy(() -> ctx.select(DSL.function(DSL.name("nexus_test", "explain_plan"), SQLDataType.CLOB,
                    DSL.val(ctx.renderInlined(catalogCount(ctx))), DSL.val((Boolean) null), DSL.val(false),
                    DSL.val(false), DSL.val(true))).fetch())
                .hasMessageContaining("no argument may be NULL");
        }
    }

    @Test
    void itRunsInTheCallersTransaction_soASetLocalPinChangesThePlan() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            Select<?> byOid = ctx.selectCount().from(DSL.table(DSL.name("pg_catalog", "pg_class")))
                .where(DSL.field(DSL.name("oid"), SQLDataType.INTEGER).eq(1259));
            String before = PgContainerHelper.explainPlanWith(ctx, byOid, PLAIN);
            ctx.setLocal(DSL.name("enable_indexscan"), DSL.inline(false)).execute();
            ctx.setLocal(DSL.name("enable_indexonlyscan"), DSL.inline(false)).execute();
            ctx.setLocal(DSL.name("enable_bitmapscan"), DSL.inline(false)).execute();
            String pinned = PgContainerHelper.explainPlanWith(ctx, byOid, PLAIN);
            c.rollback();
            assertThat(before).as("unpinned, the oid lookup uses pg_class's oid index. Plan:%n%s", before)
                .containsPattern("Index (Only )?Scan");
            assertThat(pinned).as("SET LOCAL in the caller's transaction reaches the function. Plan:%n%s", pinned)
                .contains("Seq Scan").doesNotContainPattern("Index (Only )?Scan");
        }
    }
}
