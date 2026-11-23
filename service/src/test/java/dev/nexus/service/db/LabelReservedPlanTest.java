// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.datamove.ArbiterTwins;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HashSet;
import java.util.Set;

/**
 * RDR-229 P3.7 (linda 2026-11-23, addendum 8a): {@link LabelReserved}'s check uses the name keys' existing indexes: no index and no changeset is added.
 * The plan of each table's EXISTS is read with sequential scans disabled (the tables are empty, so the planner would otherwise choose a scan of nothing):
 * what it must show is that an index on the name key's leading columns can answer it. The plans are logged ({@code event=label_reserved_plan}). The
 * list of probed tables is pinned against {@link ArbiterTwins#ALL}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class LabelReservedPlanTest {

    private static final Logger log = LoggerFactory.getLogger(LabelReservedPlanTest.class);

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stop() {
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void everyNameKeyedTableOfTheArbiterListIsProbedOrNamedWithItsReason() {
        Set<String> probed = new HashSet<>();
        LabelReserved.probes().forEach(p -> probed.add(p.table()));
        Set<String> all = new HashSet<>();
        ArbiterTwins.ALL.forEach(a -> all.add(a.table()));
        Set<String> accounted = new HashSet<>(probed);
        accounted.addAll(LabelReserved.NOT_CHECKED.keySet());
        assertThat(accounted).as("every ArbiterTwins entry is probed or listed in NOT_CHECKED with a reason").isEqualTo(all);
        assertThat(probed).as("non-vacuity: the two partitioned name keys are probed").contains("chunks", "taxonomy_centroids");
        LabelReserved.NOT_CHECKED.values().forEach(reason -> assertThat(reason).isNotBlank());
    }

    @Test
    void theExistsOfEachTableUsesAnIndexOnTheNameKeysLeadingColumns() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            ctx.set(DSL.name("enable_seqscan"), DSL.inline("off"));
            for (var p : LabelReserved.probes()) {
                var table = DSL.table(DSL.name("nexus", p.table()));
                var query = ctx.selectOne().from(table)
                    .where(DSL.field(DSL.name("tenant_id"), String.class).eq("tenant"))
                    .and(DSL.field(DSL.name(p.nameColumn()), String.class).eq("label"))
                    .and(p.predicate())
                    .and(DSL.field(DSL.name(p.idColumn()), Long.class).isDistinctFrom(7L));
                String plan = ctx.explain(query).plan();
                log.info("event=label_reserved_plan table={} plan=\n{}", p.table(), plan);
                assertThat(plan).as(p.table() + " is answered by an index, not a scan of the table:\n" + plan).containsIgnoringCase("Index");
                assertThat(plan).as(p.table() + " takes no sequential scan:\n" + plan).doesNotContain("Seq Scan");
            }
        }
    }

    @Test
    void theStatementItselfPlansWithoutASequentialScanOfAnyNameKeyedTable() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            ctx.set(DSL.name("enable_seqscan"), DSL.inline("off"));
            String plan = ctx.explain(LabelReserved.query(ctx, "tenant", "label", null)).plan();
            log.info("event=label_reserved_plan table=* plan=\n{}", plan);
            assertThat(plan).as("the one statement the check sends").doesNotContain("Seq Scan on chunks").doesNotContain("Seq Scan on taxonomy_centroids");
            assertThat(plan).contains("Index");
        }
    }
}
