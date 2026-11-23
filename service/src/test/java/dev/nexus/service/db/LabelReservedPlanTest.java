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
    void everyNameKeyedTableOfTheArbiterListIsProbedOrIsAHistoryTableNamedWithItsReason() {
        Set<String> probed = new HashSet<>();
        LabelReserved.probes().forEach(p -> probed.add(p.table()));
        Set<String> all = new HashSet<>();
        ArbiterTwins.ALL.forEach(a -> all.add(a.table()));
        Set<String> accounted = new HashSet<>(probed);
        accounted.addAll(LabelReserved.HISTORY_NOT_PROBED.keySet());
        assertThat(accounted).as("every ArbiterTwins entry is probed or listed in HISTORY_NOT_PROBED with a reason").isEqualTo(all);
        assertThat(probed).as("non-vacuity: the partitioned name keys, pdf_pipeline (linda's ruling) and chash_remap (census pass 2: its name key survives the "
            + "name-key drop) are probed").contains("chunks", "taxonomy_centroids", "pdf_pipeline", "chash_remap");
        assertThat(probed).as("reembed_jobs is never probed: an active job blocks the rename itself").doesNotContain("reembed_jobs");
        assertThat(LabelReserved.HISTORY_NOT_PROBED.keySet()).as("the tables left out, each with its reason").containsExactly("reembed_jobs");
        LabelReserved.HISTORY_NOT_PROBED.values().forEach(reason -> assertThat(reason).isNotBlank());
    }

    /**
     * What the check needs of the schema: for every probed table an index (the name key's own) whose leading columns are the tenant and the name column, so
     * the EXISTS is answered by an index probe and no index is added (linda 2026-11-23, addendum 8a). Read from the catalog, not from a plan: on an empty
     * table the planner's choice says nothing about the index (a plan with {@code enable_seqscan = off} still chose a sequential scan of two empty
     * tables here), so the plan is logged for the record and the assertion is the catalog's.
     */
    @Test
    void everyProbedTableHasAnIndexLedByTheTenantAndTheNameColumn_noIndexIsAdded() throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            ctx.set(DSL.name("enable_seqscan"), DSL.inline("off"));
            log.info("event=label_reserved_plan plan=\n{}", ctx.explain(LabelReserved.query(ctx, "tenant", "label", null)).plan());
            var schema = DSL.field(DSL.name("schemaname"), String.class);
            var table = DSL.field(DSL.name("tablename"), String.class);
            var def = DSL.field(DSL.name("indexdef"), String.class);
            for (var p : LabelReserved.probes()) {
                var defs = ctx.select(def).from(DSL.table(DSL.name("pg_catalog", "pg_indexes"))).where(schema.eq("nexus").and(table.eq(p.table()))).fetch(def);
                if (p.table().equals("pdf_pipeline")) {
                    // linda 2026-11-23, option (a): pdf_pipeline has no index led by (tenant_id, collection); the probe runs only at a
                    // registration, which is rare, and a tenant-led index bounds it to one tenant's rows. No index is added for it.
                    assertThat(defs).as("pdf_pipeline: the probe is bounded by a tenant-led index; its indexes: " + defs)
                        .anyMatch(d -> d.contains("(tenant_id,"));
                    continue;
                }
                assertThat(defs).as(p.table() + ": an index led by (tenant_id, " + p.nameColumn() + "); its indexes: " + defs)
                    .anyMatch(d -> d.contains("(tenant_id, " + p.nameColumn() + ",") || d.contains("(tenant_id, " + p.nameColumn() + ")"));
            }
        }
    }
}
