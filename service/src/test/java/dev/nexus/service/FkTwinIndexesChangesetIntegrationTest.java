// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22): {@code vectors-076}, the supporting indexes of P3.3a's id twins. vectors-076-1..6 are shaped like
 * vectors-074-1 (ChunkOrphanedAtIndexChangesetIntegrationTest holds that shape's lock-wait path); this class shows each builds on a fresh
 * walk with the definition the census reads, and that an INVALID leftover (what a killed build leaves) heals on the next walk. vectors-076-7,
 * the retired queue's, is recorded on a fresh walk where the table is gone, builds where the table is back with its id column, and is
 * skipped unrecorded where the table is back without it.
 *
 * <p>One container, run in order, as the production migration role's shape (a non-superuser owner).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class FkTwinIndexesChangesetIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_twinidx";
    private static final String ADMIN_PASS = "nexus_admin_twinidx_pass";
    private static final String CHANGELOG = "db/changelog/vectors-076-collection-id-fk-twin-indexes.xml";
    /** The self-retrying changeset a fresh install always carries (it waits for a data move): the count the walk reports apart. */
    private static final int WAITING_ON_ITS_OWN = 1;

    /** Changeset id, then its index and the definition's column list. */
    private static final Map<String, String[]> CONCURRENT = new LinkedHashMap<>();
    static {
        CONCURRENT.put("vectors-076-1", new String[] {"idx_ta_tenant_source_collection_id", "topic_assignments", "(tenant_id, source_collection_id)"});
        CONCURRENT.put("vectors-076-2", new String[] {"idx_doc_aspects_collection_id", "document_aspects", "(tenant_id, collection_id)"});
        CONCURRENT.put("vectors-076-3", new String[] {"idx_topics_tenant_collection_id", "topics", "(tenant_id, collection_id)"});
        CONCURRENT.put("vectors-076-4", new String[] {"idx_taxonomy_meta_tenant_collection_id", "taxonomy_meta", "(tenant_id, collection_id)"});
        CONCURRENT.put("vectors-076-5", new String[] {"idx_doc_highlights_collection_id", "document_highlights", "(tenant_id, collection_id)"});
        CONCURRENT.put("vectors-076-6", new String[] {"idx_canary_query_ring_collection_id", "canary_query_ring", "(tenant_id, collection_id)"});
    }
    private static final String QUEUE_CHANGESET = "vectors-076-7";
    private static final String QUEUE_INDEX = "idx_aspect_queue_collection_id";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource adminDs;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.startEmptyDatabase();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-twinidx");
        adminDs = new HikariDataSource(cfg);
    }

    @AfterAll
    void stop() {
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    private <T> T su(java.util.function.Function<DSLContext, T> body) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return body.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private String text(java.util.function.Function<DSLContext, String> body) throws Exception {
        return su(body);
    }

    /** {@code indisvalid} of the index, or null when there is none. */
    private Boolean indexValid(String index) throws Exception {
        return su(ctx -> {
            var i = DSL.table(DSL.name("pg_catalog", "pg_index")).as("i");
            var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
            var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
            Field<Boolean> valid = DSL.field(DSL.name("i", "indisvalid"), Boolean.class);
            return ctx.select(valid).from(i)
                .join(c).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("i", "indexrelid"))))
                .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq(index))
                .fetchOne(valid);
        });
    }

    private boolean recorded(String changeset) throws Exception {
        return su(ctx -> ctx.fetchExists(DSL.selectOne().from(DSL.table(DSL.name("public", "databasechangelog")))
            .where(DSL.field(DSL.name("id"), String.class).eq(changeset))));
    }

    private void forget(String changeset) throws Exception {
        su(ctx -> ctx.deleteFrom(DSL.table(DSL.name("public", "databasechangelog")))
            .where(DSL.field(DSL.name("id"), String.class).eq(changeset)).execute());
    }

    // ── 1: a fresh walk ─────────────────────────────────────────────────────────────────────────────────────

    @Test
    @Order(1)
    void aFreshWalkBuildsEveryTwinIndexValidAndRecordsAllSeven() throws Exception {
        var first = SchemaMigrator.migrate(adminDs);

        assertThat(first.skippedRetryingChangesets()).as("only the changeset that waits for its data move is skipped").isEqualTo(WAITING_ON_ITS_OWN);
        for (var e : CONCURRENT.entrySet()) {
            String index = e.getValue()[0];
            assertThat(indexValid(index)).as(index + " exists and is VALID").isTrue();
            assertThat(recorded(e.getKey())).as(e.getKey() + " ran, so it is recorded").isTrue();
            assertThat(text(ctx -> PgCatalogProbes.indexDef(ctx, "nexus", index)))
                .as(index).contains("ON nexus." + e.getValue()[1] + " ").contains(e.getValue()[2]);
        }
        assertThat(text(ctx -> (String) ctx.fetchOne("SELECT to_regclass('nexus.aspect_extraction_queue')::text").get(0)))
            .as("a fresh walk retires the queue table (aspects-005)").isNull();
        assertThat(indexValid(QUEUE_INDEX)).as("so it has no index").isNull();
        assertThat(recorded(QUEUE_CHANGESET)).as("and the queue's changeset is recorded, never retried").isTrue();
        assertThat(SchemaMigrator.migrate(adminDs).skippedRetryingChangesets()).as("a second walk: nothing new to retry").isEqualTo(WAITING_ON_ITS_OWN);
    }

    // ── 2: an INVALID leftover heals ────────────────────────────────────────────────────────────────────────

    /**
     * The state a killed CREATE INDEX CONCURRENTLY leaves, an INVALID index of the name and no record, made directly (pg_index.indisvalid set false
     * as the superuser) on topic_assignments, the large table of the seven: the next walk drops it and builds a VALID one and records the changeset.
     */
    @Test
    @Order(2)
    void anInvalidLeftoverIsDroppedAndRebuiltOnTheNextWalk() throws Exception {
        String index = CONCURRENT.get("vectors-076-1")[0];
        forget("vectors-076-1");
        su(ctx -> ctx.execute("UPDATE pg_catalog.pg_index SET indisvalid = false WHERE indexrelid = 'nexus." + index + "'::regclass"));
        assertThat(indexValid(index)).as("precondition: an INVALID leftover").isFalse();

        var healed = SchemaMigrator.migrate(adminDs);

        assertThat(indexValid(index)).as("dropped and built again, VALID").isTrue();
        assertThat(recorded("vectors-076-1")).as("and recorded").isTrue();
        assertThat(healed.skippedRetryingChangesets()).isEqualTo(WAITING_ON_ITS_OWN);
    }

    // ── 3: the retired queue ────────────────────────────────────────────────────────────────────────────────

    /**
     * An install that kept the queue: the table is back without its collection_id column, so the precondition skips the changeset unrecorded (a
     * visible retrying member); once the column is there the walk builds the index and records it.
     */
    @Test
    @Order(3)
    void theQueueIndexWaitsForTheIdColumnThenBuilds() throws Exception {
        forget(QUEUE_CHANGESET);
        su(ctx -> ctx.execute("CREATE TABLE nexus.aspect_extraction_queue (tenant_id text NOT NULL, collection text NOT NULL)"));
        su(ctx -> ctx.execute("ALTER TABLE nexus.aspect_extraction_queue OWNER TO " + ADMIN_ROLE));
        try {
            var waiting = SchemaMigrator.migrate(adminDs);
            assertThat(recorded(QUEUE_CHANGESET)).as("skipped while the table lacks collection_id").isFalse();
            assertThat(waiting.skippedRetryingChangesets()).as("a visible retrying member").isEqualTo(WAITING_ON_ITS_OWN + 1);
            assertThat(indexValid(QUEUE_INDEX)).isNull();

            su(ctx -> ctx.execute("ALTER TABLE nexus.aspect_extraction_queue ADD COLUMN collection_id bigint"));
            var built = SchemaMigrator.migrate(adminDs);
            assertThat(indexValid(QUEUE_INDEX)).as("built once the column is there").isTrue();
            assertThat(recorded(QUEUE_CHANGESET)).as("and recorded").isTrue();
            assertThat(built.skippedRetryingChangesets()).isEqualTo(WAITING_ON_ITS_OWN);
            assertThat(text(ctx -> PgCatalogProbes.indexDef(ctx, "nexus", QUEUE_INDEX))).contains("(tenant_id, collection_id)");
        } finally {
            su(ctx -> ctx.execute("DROP TABLE IF EXISTS nexus.aspect_extraction_queue"));
        }
    }
}
