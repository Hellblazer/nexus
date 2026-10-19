/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service;

import org.jooq.impl.SQLDataType;
import org.jooq.Field;
import org.jooq.DSLContext;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
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
    /** The gate cutoff this test pins. Production's PgVectorRepository.SELECTIVE_GATE_MAX is
     *  5000 now (it was 128 when this test was written), which would send a 350-row gate down
     *  the SELECTIVE text-first branch and leave the dense HNSW-first branch unexercised; the
     *  6-arg hybridSearch overload pins the dispatch at the original 128 instead. */
    private static final int DENSE_GATE_MAX = 128;
    private static final int MATCHING = 350;   // > DENSE_GATE_MAX: dense branch
    private static final int NOISE = 50_000;

    /** The scan cap the calibration below pins (see the calibration comment). */
    private static final int CALIBRATED_SCAN_CAP = 512;

    private final ScanBudgetProbe probe = new ScanBudgetProbe();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    PgVectorRepository repo;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        scope = new TenantScope(probe.wrap(svcDs));
        // nexus-wbfpw.47: every search now SET LOCALs its own scan budget, which overrides
        // a DB-level ALTER DATABASE ... SET hnsw.max_scan_tuples, so the calibrated cap goes
        // through PgSession's test seam and is asserted INSIDE the search below.
        PgSession.overrideScanBudgetForTests(CALIBRATED_SCAN_CAP, 1);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1.0f, 0.0f);
        repo = new PgVectorRepository(scope, embedder, embedder);

        seedLiveShapedStore();
    }

    @AfterAll
    void stopAll() {
        PgSession.resetScanBudgetForTests();
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
            // RDR-204 nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection,
            // which derives content_type from each RDR-103-conformant name.
            for (String col : new String[] {COL_TARGET, COL_NOISE}) {
                PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, col, "minilm-l6-v2-384");
            }
            // RDR-191 Phase 4: idx_chunks_384_embedding -> idx_chunks_embedding_384 on
            // the unified nexus.chunks table.
            su.createStatement().execute("DROP INDEX nexus.idx_chunks_embedding_384");
            // Noise: random vectors (normalized-ish is irrelevant for cosine
            // ordering realism at this granularity), text that can NEVER pass
            // the gate for QUERY.
            insertRandomChunks(DSL.using(su, SQLDialect.POSTGRES), COL_NOISE, "noise doc ",
                "filler noise document number ", NOISE);
            // Gate-matching rows: text passes the tsv gate; vectors
            // DISPERSED at random through the same space as the noise —
            // real corpus matches are scattered, and an identical-vector
            // cluster would defeat the reproduction (HNSW yields a whole
            // mutual-neighbor cluster the moment the scan touches it).
            insertRandomChunks(DSL.using(su, SQLDialect.POSTGRES), COL_TARGET, "gpu batch row ",
                "gpu batch scheduling design row ", MATCHING);
            // RDR-192 Step 5 (nexus-wbfpw.10): hybridSearch now requires a live
            // own-collection manifest owner -- own every one of the MATCHING rows in
            // bulk (the noise rows never pass the text gate regardless of ownership,
            // so they are deliberately left manifest-less: nothing under test reads
            // them). One owning document, one manifest row per matching chash,
            // computed in Java with the SAME expression as the INSERT above,
            // sha256 of the UTF-8 text.
            String[] matchingChashes = new String[MATCHING];
            for (int g = 1; g <= MATCHING; g++) {
                matchingChashes[g - 1] = sha256Hex("gpu batch row " + g);
            }
            PgContainerHelper.ownChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, COL_TARGET,
                matchingChashes);
            // The scan budget is calibrated to the CLOUD's ratio at test scale, through
            // PgSession's test seam (CALIBRATED_SCAN_CAP, set in startAll): their failing
            // queries sit at ~329 gate matches in a table where the default 20k-tuple
            // budget yields ~9 expected hits (the exact 10 -> 0/1/4 cliff regime). 512
            // tuples over 50k rows with 350 matches reproduces that ratio honestly (~3.5
            // expected hits) without a 20-minute 700k seed. It is NOT a DB-level setting:
            // hybridSearch's HNSW-first branch SET LOCALs the serving budget, which would
            // override one (nexus-wbfpw.47).
            // The cloud's post-conversion state: a freshly REBUILT graph.
            // nexus-zpd9o: build it serially. A parallel pgvector HNSW build holds the
            // graph (maintenance_work_mem, 64 MB) in dynamic shared memory, which does not
            // fit a container's default 64 MB /dev/shm ("could not resize shared memory
            // segment ... No space left on device" on chas). Session-level, this connection only.
            DSL.using(su, SQLDialect.POSTGRES)
                .set(DSL.name("max_parallel_maintenance_workers"), DSL.inline(0)).execute();
            su.createStatement().execute(
                "CREATE INDEX idx_chunks_embedding_384 ON " + DimTables.CHUNKS_TABLE_NAME + " "
                + "USING hnsw (" + DimTables.embeddingColumn(384) + " nexus.vector_cosine_ops)");
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
    }

    @Test
    void denseGate_sparseInLargeIndex_mustStillFillNResults() {
        // Sanity: the gate really is dense-branch territory (> 128 matches).
        Integer gateMatches = scope.withTenant(TENANT, ctx -> ctx.fetchOne(
            "SELECT count(*) FROM " + DimTables.CHUNKS_TABLE_NAME + " "
            + "WHERE collection = '" + COL_TARGET + "' "
            + "AND (chunk_tsv @@ plainto_tsquery('english', '" + QUERY + "') "
            + "     OR '" + QUERY + "' OPERATOR(nexus.<%) chunk_text)").get(0, Integer.class));
        assertThat(gateMatches).isGreaterThanOrEqualTo(MATCHING);

        probe.arm("dense");
        List<Map<String, Object>> rows;
        try {
            rows = repo.hybridSearch(TENANT, QUERY, List.of(COL_TARGET), 10, null, DENSE_GATE_MAX);
        } finally {
            probe.disarm();
        }

        // The calibrated premise must hold INSIDE the search: the HNSW-first statement ran
        // under the pinned cap. Without this a raised serving budget would silently turn
        // the cliff-ratio pin into a test of a different (200k-cap) regime.
        var hnsw = probe.hnswStatements("dense");
        assertThat(hnsw).as("the dense gate must take the HNSW-first branch; saw %s", probe.seen())
            .isNotEmpty();
        assertThat(hnsw).allSatisfy(seen -> assertThat(seen.maxScanTuples())
            .as("the calibrated scan budget must govern the search statement itself")
            .isEqualTo(Integer.toString(CALIBRATED_SCAN_CAP)));

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

    /** sha256 of {@code text}'s UTF-8 bytes as lowercase hex, the same value the
     *  seed INSERT's {@code sha256(convert_to(text, 'UTF8'))} stores. */
    private static String sha256Hex(String text) {
        try {
            return java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256")
                .digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * {@code count} 384-dim chunks under {@code collection}: chash = sha256 of {@code hashPrefix + g}, text =
     * {@code textPrefix + g}, and a vector of 384 independent {@code random()} components per row (the
     * correlated {@code string_agg} subquery, with {@code 384 + (g - g)} to force per-row evaluation). Built
     * as pgvector text and cast to the generated field's own vector type.
     */
    private static void insertRandomChunks(DSLContext ctx, String collection, String hashPrefix,
                                           String textPrefix, int count) {
        var series = DSL.generateSeries(1, count).as("s", "g");
        Field<Integer> g = series.field("g", Integer.class);
        Field<String> components = DSL.field(ctx.select(DSL.groupConcat(DSL.cast(DSL.rand(), SQLDataType.VARCHAR))
                .separator(","))
            .from(DSL.generateSeries(DSL.inline(1), DSL.inline(384).plus(g.minus(g)))));
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
            .select(ctx.select(DSL.val(TENANT, CHUNKS.TENANT_ID), DSL.val(collection, CHUNKS.COLLECTION),
                    DSL.function("sha256", SQLDataType.BLOB,
                        DSL.function("convert_to", SQLDataType.BLOB,
                            DSL.concat(DSL.val(hashPrefix, CHUNKS.CHUNK_TEXT), DSL.cast(g, SQLDataType.VARCHAR)),
                            DSL.inline("UTF8"))),
                    DSL.val("minilm-l6-v2-384", CHUNKS.EMBEDDING_MODEL),
                    DSL.concat(DSL.val(textPrefix, CHUNKS.CHUNK_TEXT), DSL.cast(g, SQLDataType.VARCHAR)),
                    DSL.cast(DSL.concat(DSL.inline("["), components, DSL.inline("]")),
                        CHUNKS.EMBEDDING_384.getDataType()))
                .from(series))
            .execute();
    }
}
