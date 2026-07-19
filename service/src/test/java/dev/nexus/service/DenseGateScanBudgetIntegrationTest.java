/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * BUG-0148 (conexus-xpg7, 2026-07-19) CONTRACT PIN — this test never
 * reproduced the bug; its GREEN runs are what constrained the diagnosis.
 *
 * <p>The failing cloud queries carried a text gate matching ~329 rows —
 * above SELECTIVE_GATE_MAX (128), the dense HNSW-first branch — and shed
 * from 10 rows to 0/1/4. Root cause (confirmed by conexus): the RDR-180
 * ALTER TYPE boot conversion REWROTE the chunks tables, silently resetting
 * planner statistics ({@code last_analyze} never, autoanalyze 12 days
 * stale), and the stale-stats planner flipped sparse-gate hybrid queries
 * onto a plan that exhausted its scan budget. A manual {@code ANALYZE}
 * fixed the live store; changeset {@code rdr180-16-analyze-rewritten-tables}
 * now ANALYZEs the rewritten tables in the same changelog pass (pinned by
 * {@code SchemaMigratorIntegrationTest#rdr180Rewrite_leavesPlannerStatsFresh}).
 *
 * <p>What THIS test pins: with FRESH statistics, the dense branch does not
 * starve at the live-shaped ratio — 350 gate-matching rows dispersed through
 * ~50k noise rows, match vectors far from the query, a freshly REBUILT graph
 * (the post-rewrite state), and the scan budget calibrated to the cloud's
 * cliff regime. All chashes are canonical 64-hex, so the healthy result is
 * also width-independent (the rekey window was exonerated for this bug). A
 * regression here is a NEW dense-branch fill defect, not a BUG-0148
 * recurrence — BUG-0148's trigger (stale stats) is closed off upstream by
 * rdr180-16.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DenseGateScanBudgetIntegrationTest {

    private static final String SVC_ROLE = "svc_dense_budget_test";
    private static final String SVC_PASS = "svc_dense_budget_test_pass";
    private static final String TENANT = "t-dense-budget";

    private static final String COL_TARGET = "rdr__budget__minilm-l6-v2-384__v1";
    private static final String COL_NOISE  = "docs__noise__minilm-l6-v2-384__v1";

    private static final String QUERY = "gpu batch scheduling";
    private static final int MATCHING = 350;   // > SELECTIVE_GATE_MAX (128): dense branch
    private static final int NOISE = 50_000;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    PgVectorRepository repo;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String role : new String[] {SVC_ROLE, "nexus_svc"}) {
                su.createStatement().execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"
                    + role + "') THEN CREATE ROLE " + role + " LOGIN PASSWORD '"
                    + (role.equals(SVC_ROLE) ? SVC_PASS : "nexus_svc_pass")
                    + "'; END IF; END $$");
            }
        }
        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)))
                .update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        scope = new TenantScope(svcDs);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1.0f, 0.0f);
        repo = new PgVectorRepository(scope, embedder, embedder);

        seedLiveShapedStore();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * The live shape, mechanically: drop the HNSW index, bulk-load ~50k noise
     * rows (random unit-ish vectors, non-matching text) + 350 gate-matching
     * rows whose vectors are FAR from the query embedding, REBUILD the index
     * (exactly what the cloud's ALTER TYPE rewrite did), ANALYZE.
     *
     * <p>Query embeds to (1, 0, 0, ...). Matching rows sit near (0, ..., 1)
     * — the gate passes on TEXT, the vectors are the far side of the space,
     * so the graph scan must wade through noise to reach them. All chashes
     * canonical 64-hex: width plays no part.
     */
    private void seedLiveShapedStore() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String col : new String[] {COL_TARGET, COL_NOISE}) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type) "
                    + "VALUES ('" + TENANT + "', '" + col + "', 'rdr') ON CONFLICT DO NOTHING");
            }
            su.createStatement().execute("DROP INDEX nexus.idx_chunks_384_embedding");
            // Noise: random vectors (normalized-ish is irrelevant for cosine
            // ordering realism at this granularity), text that can NEVER pass
            // the gate for QUERY.
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) "
                + "SELECT '" + TENANT + "', '" + COL_NOISE + "', "
                + "  sha256(convert_to('noise doc ' || g, 'UTF8')), "
                + "  'filler noise document number ' || g, "
                + "  ('[' || (SELECT string_agg(random()::text, ',') "
                + "            FROM generate_series(1, 384 + (g - g))) || ']')::vector "
                + "FROM generate_series(1, " + NOISE + ") g");
            // Gate-matching rows: text passes the tsv gate; vectors
            // DISPERSED at random through the same space as the noise —
            // real corpus matches are scattered, and an identical-vector
            // cluster would defeat the reproduction (HNSW yields a whole
            // mutual-neighbor cluster the moment the scan touches it).
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) "
                + "SELECT '" + TENANT + "', '" + COL_TARGET + "', "
                + "  sha256(convert_to('gpu batch row ' || g, 'UTF8')), "
                + "  'gpu batch scheduling design row ' || g, "
                + "  ('[' || (SELECT string_agg(random()::text, ',') "
                + "            FROM generate_series(1, 384 + (g - g))) || ']')::vector "
                + "FROM generate_series(1, " + MATCHING + ") g");
            // Calibrate the scan budget to the CLOUD's ratio at test scale:
            // their failing queries sit at ~329 gate matches in a table where
            // the default 20k-tuple budget yields ~9 expected hits (the exact
            // 10 -> 0/1/4 cliff regime). 512 tuples over 50k rows with 350
            // matches reproduces that ratio honestly (~3.5 expected hits)
            // without a 20-minute 700k seed. DB-level so the repo's own
            // session inherits it — hybridSearch pins iterative_scan but
            // deliberately NOT the budget.
            su.createStatement().execute(
                "ALTER DATABASE \"" + pg.getDatabaseName()
                + "\" SET hnsw.max_scan_tuples = 512");
            // The cloud's post-conversion state: a freshly REBUILT graph.
            su.createStatement().execute(
                "CREATE INDEX idx_chunks_384_embedding ON nexus.chunks_384 "
                + "USING hnsw (embedding vector_cosine_ops)");
            su.createStatement().execute("ANALYZE nexus.chunks_384");
        }
    }

    @Test
    void denseGate_sparseInLargeIndex_mustStillFillNResults() {
        // The pool predates the ALTER DATABASE — session GUCs snapshot at
        // connect, so evict and let fresh connections inherit the budget.
        svcDs.getHikariPoolMXBean().softEvictConnections();
        String budget = scope.withTenant(TENANT, ctx ->
            ctx.fetchOne("SHOW hnsw.max_scan_tuples").get(0, String.class));
        assertThat(budget)
            .as("the calibrated scan budget must actually govern the repo's session")
            .isEqualTo("512");

        // Sanity: the gate really is dense-branch territory (> 128 matches).
        Integer gateMatches = scope.withTenant(TENANT, ctx -> ctx.fetchOne(
            "SELECT count(*) FROM nexus.chunks_384 "
            + "WHERE collection = '" + COL_TARGET + "' "
            + "AND (chunk_tsv @@ plainto_tsquery('english', '" + QUERY + "') "
            + "     OR '" + QUERY + "' <% chunk_text)").get(0, Integer.class));
        assertThat(gateMatches).isGreaterThanOrEqualTo(MATCHING);

        List<Map<String, Object>> rows =
            repo.hybridSearch(TENANT, QUERY, List.of(COL_TARGET), 10, null);

        // THE CONTRACT: >= n_results gate-passing rows exist, so the hybrid
        // MUST return n_results. This held through every BUG-0148 run —
        // with fresh statistics the dense branch fills even at this sparse,
        // vector-far shape. Shedding here is a new fill defect.
        assertThat(rows)
            .as("dense-branch hybrid must not starve when the gate holds "
                + gateMatches + " rows vector-far from the query "
                + "(BUG-0148 contract pin)")
            .hasSize(10);
    }
}
