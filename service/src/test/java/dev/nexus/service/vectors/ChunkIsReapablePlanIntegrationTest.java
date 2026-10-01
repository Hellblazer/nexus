// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 7 (bead nexus-wbfpw.15): the plan {@code nexus.chunk_is_reapable} gets under
 * the REAL RLS-subject role, and the HOT-update cost of the one index that would serve its
 * grace column.
 *
 * <p>Measured through {@code nexus_svc}-shaped access (a NOSUPERUSER NOBYPASSRLS role with the
 * tenant GUC stamped transaction-locally), never a superuser connection, which skips the
 * security barrier that decides which quals may become index conditions. The fixture follows
 * the shape of the production collections this predicate runs over: one collection with
 * 30,000 chunks of which 10,000 are orphans, 20,000 manifest rows, 1,000 documents of which
 * 20 are mid-index, and a second collection that must not be touched.
 *
 * <p>What this pins, each with the number that justified it (T2 {@code nexus/rdr-192-continuation}
 * carries the full plans):
 * <ul>
 *   <li>the function inlines: no function scan and no function name in any plan;</li>
 *   <li>the in-flight index pin is a PRIMARY-KEY probe on {@code catalog_documents}, not a bitmap
 *       scan of {@code idx_catalog_documents_index_state} per candidate chunk;</li>
 *   <li>the manifest probe is {@code idx_catalog_chunks_chash};</li>
 *   <li>the candidate scan is the {@code chunks_pk} range over (tenant, collection), so no index
 *       on {@code last_written_at} is needed;</li>
 *   <li>and the reason not to add one: with an index on {@code last_written_at}, a refresh of
 *       that column cannot be a HOT update.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkIsReapablePlanIntegrationTest {

    private static final String SVC_ROLE = "svc_reapable_plan";
    private static final String SVC_PASS = "svc_reapable_plan_pass";
    private static final String TENANT = "reap-plan-t";
    private static final String COL = "knowledge__reap-plan-a__minilm-l6-v2-384__v1";
    private static final String OTHER = "knowledge__reap-plan-b__minilm-l6-v2-384__v1";

    /** The exact call shape every consumer uses. */
    private static final String PREDICATE =
        "EXISTS (SELECT 1 FROM nexus.chunk_is_reapable(c.tenant_id, c.collection, c.chash,"
        + " c.last_written_at, c.metadata, NULL, NULL))";

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void seed() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            var ctx = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, COL);
            PgContainerHelper.insertCollection(ctx, TENANT, OTHER);
        }
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            // Free space on each page stands in for a vacuumed table's steady state: HOT needs room in the
            // updated row's own page, and a freshly loaded table at the default fillfactor has none.
            st.execute("ALTER TABLE nexus.chunks SET (fillfactor = 40)");
            st.execute("INSERT INTO nexus.chunks(tenant_id, collection, chash, chunk_text, embedding_384, metadata,"
                + " created_at, last_written_at) SELECT '" + TENANT + "', '" + COL + "', sha256(('a' || g)::bytea),"
                + " 'x', array_fill(0.0::real, ARRAY[384])::nexus.vector(384),"
                + " jsonb_build_object('catalog_doc_id', 'doc' || (g % 1000)),"
                + " now() - interval '40 days', now() - interval '40 days' FROM generate_series(1, 30000) g");
            st.execute("INSERT INTO nexus.chunks(tenant_id, collection, chash, chunk_text, embedding_384, metadata,"
                + " created_at, last_written_at) SELECT '" + TENANT + "', '" + OTHER + "', sha256(('b' || g)::bytea),"
                + " 'x', array_fill(0.0::real, ARRAY[384])::nexus.vector(384), '{}'::jsonb,"
                + " now() - interval '40 days', now() - interval '40 days' FROM generate_series(1, 15000) g");
            st.execute("INSERT INTO nexus.catalog_documents(tenant_id, tumbler, title, physical_collection,"
                + " index_state, index_started_at) SELECT '" + TENANT + "', 'doc' || g, 't', '" + COL + "',"
                + " CASE WHEN g % 50 = 0 THEN 'indexing' ELSE 'complete' END, now() - interval '1 hour'"
                + " FROM generate_series(0, 999) g");
            // 20,000 of the 30,000 chunks are owned: the first two thirds, by chash seed.
            st.execute("INSERT INTO nexus.catalog_document_chunks(tenant_id, doc_id, position, chash, collection)"
                + " SELECT '" + TENANT + "', 'doc' || (g % 1000), g, sha256(('a' || g)::bytea), '" + COL + "'"
                + " FROM generate_series(1, 20000) g");
            st.execute("ANALYZE nexus.chunks");
            st.execute("ANALYZE nexus.catalog_document_chunks");
            st.execute("ANALYZE nexus.catalog_documents");
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    /** EXPLAIN (ANALYZE) as the RLS-subject role; a DML statement is rolled back. */
    private String explain(String sql) throws Exception {
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS)) {
            c.setAutoCommit(false);
            try (Statement st = c.createStatement()) {
                st.execute("SELECT set_config('nexus.tenant', '" + TENANT + "', true)");
                StringBuilder sb = new StringBuilder();
                try (ResultSet rs = st.executeQuery("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF) " + sql)) {
                    while (rs.next()) sb.append(rs.getString(1)).append('\n');
                }
                return sb.toString();
            } finally {
                c.rollback();
            }
        }
    }

    private static final String LISTING =
        "SELECT c.chash FROM nexus.chunks c WHERE c.tenant_id = '" + TENANT + "' AND c.collection = '" + COL
        + "' AND " + PREDICATE + " ORDER BY c.chash LIMIT 300";

    private static final String REAPER_DELETE =
        "DELETE FROM nexus.chunks c WHERE c.tenant_id = '" + TENANT + "' AND c.collection = '" + COL
        + "' AND " + PREDICATE;

    @Test
    void listingPlan_inlines_andProbesBothTablesThroughTheirIndexes() throws Exception {
        String plan = explain(LISTING);
        System.out.println("\n=== reapable listing, nexus_svc\n" + plan);

        assertThat(plan).doesNotContain("chunk_is_reapable").doesNotContain("Function Scan");
        assertThat(plan).as("candidates come from the (tenant, collection) primary-key range")
            .contains("chunks_pk");
        assertThat(plan).as("manifest probe").contains("idx_catalog_chunks_chash");
        assertThat(plan).as("the pin is a primary-key probe, not a per-chunk bitmap scan of the 'indexing' index:\n"
            + plan).contains("catalog_documents_pk").doesNotContain("idx_catalog_documents_index_state");
    }

    @Test
    void deletePlan_hasTheGraceQualOnTheTargetRow_soAReadCommittedRecheckSeesARacingRefresh() throws Exception {
        String plan = explain(REAPER_DELETE);
        System.out.println("\n=== reapable DELETE, nexus_svc\n" + plan);

        assertThat(plan).doesNotContain("chunk_is_reapable").doesNotContain("Function Scan");
        assertThat(plan).as("the grace comparison is evaluated on the DELETE's own rows").contains("last_written_at");
        assertThat(plan).contains("catalog_documents_pk").doesNotContain("idx_catalog_documents_index_state");
        assertThat(plan).as("every orphan of the collection is a candidate, none of the other collection")
            .contains("Delete on chunks");
    }

    @Test
    void anIndexOnLastWrittenAt_wouldStopHotUpdates_soNoneIsAdded() throws Exception {
        long withoutIndex = hotRatioPercent(false);
        long withIndex = hotRatioPercent(true);
        System.out.println("\n=== HOT update ratio of a last_written_at refresh: no index " + withoutIndex
            + "%, with an index on last_written_at " + withIndex + "%");

        assertThat(withoutIndex).as("without an index a refresh is HOT").isGreaterThan(50);
        assertThat(withIndex).as("with an index on the column every refresh needs a new index entry").isZero();
    }

    /** Refreshes last_written_at on 3,000 rows and returns the HOT share of those updates. */
    private long hotRatioPercent(boolean withIndex) throws Exception {
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            if (withIndex) {
                st.execute("CREATE INDEX tmp_chunks_last_written_at ON nexus.chunks (last_written_at)");
            }
            st.execute("SELECT pg_stat_reset_single_table_counters('nexus.chunks'::regclass)");
            st.execute("UPDATE nexus.chunks SET last_written_at = now() WHERE tenant_id = '" + TENANT
                + "' AND collection = '" + OTHER + "' AND chash IN (SELECT chash FROM nexus.chunks"
                + " WHERE tenant_id = '" + TENANT + "' AND collection = '" + OTHER + "' LIMIT 3000)");
            st.execute("SELECT pg_stat_force_next_flush()");
            long upd;
            long hot;
            try (ResultSet rs = st.executeQuery("SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables"
                + " WHERE schemaname = 'nexus' AND relname = 'chunks'")) {
                rs.next();
                upd = rs.getLong(1);
                hot = rs.getLong(2);
            }
            if (withIndex) {
                st.execute("DROP INDEX nexus.tmp_chunks_last_written_at");
            }
            assertThat(upd).as("the refresh touched the rows").isEqualTo(3000);
            return hot * 100 / upd;
        }
    }
}
