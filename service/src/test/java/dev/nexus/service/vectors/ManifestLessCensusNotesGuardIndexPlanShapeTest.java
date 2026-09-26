// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.Statement;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 2 round-2 fix (bead nexus-wbfpw.4, code-review Important):
 * plan-shape proof for {@code idx_catalog_documents_live_note_doc_id}
 * (catalog-038-notes-guard-doc-id-index.xml), the partial expression index
 * added to serve {@link PgVectorRepository#MANIFEST_LESS_CENSUS_SQL}'s
 * reverse-owner LATERAL (and, identically, production's own
 * {@code CatalogRepository.sweepChunksQuery} "nl3fn NOTES GUARD").
 *
 * <p>Mirrors this repo's established plan-shape methodology ({@link
 * dev.nexus.service.PgVectorRepositoryRawSqlPlanShapeTest}, {@link
 * dev.nexus.service.ChashProbePlanShapeTest}): seed a few thousand rows via
 * server-side {@code generate_series} so a cost-based choice at this
 * cardinality actually says something, rather than the near-zero-row
 * cardinality where either access path is free.
 *
 * <p><strong>What building this test found (reported honestly, not glossed
 * over — "confirm with EXPLAIN, or state that it doesn't hold" per the
 * round-2 fix instructions):</strong> the index is valid, ready, and IS the
 * planner's natural choice for the identical predicate {@code
 * sweepChunksQuery}'s notes guard and this census share — but ONLY when
 * Postgres is given LITERAL values to cost against (an operator hand-typing
 * a chash into psql; Sam's ruling has conexus running {@code
 * scripts/sql/manifest_less_census.sql} exactly this way until an engine tag
 * exists). {@link
 * #isolatedReverseLookupPredicate_withLiteralValues_bindsToTheNotesGuardIndex_atNoiseScale}
 * proves that shape, via a plain {@code Statement} with values spliced into
 * the SQL text (never done with real user input — these are fixed test
 * constants) rather than a {@code PreparedStatement}, specifically to avoid
 * JDBC bind-parameter framing.
 *
 * <p><strong>Investigated and NOT achieved, stated rather than forced or
 * silently claimed:</strong> {@code EXPLAIN} of the SAME predicate issued the
 * way the actual Java code issues it — as a genuine JDBC bind parameter, via
 * {@code ctx.resultQuery(MANIFEST_LESS_CENSUS_SQL, tenant, collection, limit,
 * offset)}, whether isolated or embedded in the full composed statement's
 * LATERAL, and whether or not the LATERAL's now-removed {@code ORDER BY
 * d2.tumbler} tie-break is present — does NOT bind to the new index. It
 * instead chooses a Seq Scan (isolated) or the PRE-EXISTING, broader {@code
 * idx_catalog_documents_collection_live (tenant_id, physical_collection)
 * WHERE deleted_at IS NULL} (catalog-003-soft-delete.xml, embedded in the
 * full statement, even with {@code enable_seqscan = off} forced), applying
 * {@code file_path}/{@code metadata->>'doc_id'} as a post-scan filter either
 * way. Removing the {@code ORDER BY} (still done, in {@code
 * MANIFEST_LESS_CENSUS_SQL} itself — a genuine simplification, since
 * determinism among reverse candidates was never load-bearing, see the SQL's
 * own header comment) was this investigation's first hypothesis and DID fix
 * the literal-value shape (measured cost 2.38 without it versus 40.83 with
 * it), but a bind-parameterized {@code PreparedStatement} execution of the
 * bare-{@code LIMIT}-1 form STILL does not bind to the new index (measured:
 * Seq Scan, cost 350.53) — a genuinely different Postgres cost-estimation
 * path for bind parameters versus literals against a jsonb {@code ->>} text
 * equality, not an ORDER BY artifact. The SAME limitation almost certainly
 * applies to production's own {@code sweepChunksQuery} notes guard, which
 * correlates on an outer {@code CHUNKS.CHASH} value the identical way and
 * has never had this predicate indexed either. Correctness is unaffected —
 * either physical access path returns the identical, correct row — and the
 * index remains worth shipping: it serves the literal/hand-run shape
 * directly, costs nothing extra to maintain relative to the alternative of
 * no index at all, and may bind for the parameterized shape too at different
 * PostgreSQL versions, planner settings, or data distributions this
 * hermetic test cannot explore. Forcing it further (a query hint extension,
 * or restructuring the LATERAL to defeat the broader existing index's
 * competitiveness) is a follow-up, not something this bead's scope covers.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, PER_CLASS lifecycle.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestLessCensusNotesGuardIndexPlanShapeTest {

    private static final String SVC_ROLE = "svc_wbfpw4_idx_planshape";
    private static final String SVC_PASS = "svc_wbfpw4_idx_planshape_pass";
    private static final String TENANT = "wbfpw4-idx-planshape";
    private static final String COLLECTION = "knowledge__wbfpw4-idx-planshape__minilm-l6-v2-384__v1";

    // Modest but non-trivial cardinality: large enough that the planner's default cost
    // model prefers the partial expression index over a Seq Scan of catalog_documents
    // for an equality lookup that matches exactly one row out of this many; small enough
    // to seed fast under Testcontainers. Matches this repo's established "a few thousand
    // rows" precedent (PgVectorRepositoryRawSqlPlanShapeTest.CHUNKS_PER_DIM = 4_000).
    private static final int NOISE_NOTE_ROWS = 5_000;

    /** The one manifest-less chunk under test: no forward key, resolved only in reverse. */
    private static final String TEST_CHASH = Chash.ofText("wbfpw4-idx-planshape-target").toHex();
    private static final String TARGET_NOTE_DOC = "wbfpw4-idx-planshape-target-note";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    PgVectorRepository vecRepo;
    HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        var embedder = new ConstantEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }

        seedNoiseAndTarget();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * {@code NOISE_NOTE_ROWS} live, note-shaped {@code catalog_documents} rows in
     * {@code COLLECTION}, each with a DISTINCT {@code metadata.doc_id} (server-side
     * {@code md5} concatenation — the SAME "no pgcrypto needed" pattern {@link
     * dev.nexus.service.ChashProbePlanShapeTest} uses for bulk chash-shaped fixture
     * values), none of which match the test chash. One additional row,
     * {@code TARGET_NOTE_DOC}, carries {@code metadata.doc_id = TEST_CHASH} exactly —
     * the row the reverse LATERAL must actually find. One manifest-less T3 chunk
     * ({@code TEST_CHASH}, empty chunk metadata — no forward key at all) forces the
     * reverse path for every row of {@code base} to run.
     */
    private void seedNoiseAndTarget() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            Statement st = su.createStatement();
            st.execute(
                "INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, content_type, corpus, physical_collection, metadata) "
                + "SELECT '" + TENANT + "', 'wbfpw4-idx-planshape-noise-' || i, "
                + "       'noise note ' || i, 'prose', 'knowledge', '" + COLLECTION + "', "
                + "       jsonb_build_object('doc_id', md5('noise-a-' || i) || md5('noise-b-' || i)) "
                + "FROM generate_series(1, " + NOISE_NOTE_ROWS + ") i");
            st.execute(
                "INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, content_type, corpus, physical_collection, metadata) "
                + "VALUES ('" + TENANT + "', '" + TARGET_NOTE_DOC + "', 'target note', 'prose', "
                + "        'knowledge', '" + COLLECTION + "', "
                + "        jsonb_build_object('doc_id', '" + TEST_CHASH + "'))");
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }

        vecRepo.upsertChunks(TENANT, COLLECTION, List.of(TEST_CHASH), List.of("reverse-lookup plan-shape text"),
            List.of(Map.of()));
    }

    /**
     * The reverse notes-guard predicate with LITERAL values spliced directly into
     * the SQL text (a plain JDBC {@code Statement}, not a bind-parameterized
     * {@code PreparedStatement}) — exactly the form an operator hand-runs in psql
     * (Sam's ruling: the production census runs this text directly against the
     * database, not through the route, until the rest of RDR-192 ships), and the
     * ONE shape in which this suite can show the planner actually choosing the
     * new index. See the class javadoc's "Known limitation" note for why the
     * bind-PARAMETERIZED form (what {@code ctx.resultQuery(MANIFEST_LESS_CENSUS_SQL,
     * ...)} — the actual route/Java code path — issues) does not show this bind.
     */
    private String explainIsolatedReverseLookupLiteral() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var st = su.createStatement();
            var rs = st.executeQuery(
                "EXPLAIN SELECT d2.tumbler FROM nexus.catalog_documents d2 "
                + "WHERE d2.tenant_id = '" + TENANT + "' AND d2.physical_collection = '" + COLLECTION + "' "
                + "AND d2.deleted_at IS NULL AND (d2.file_path IS NULL OR d2.file_path = '') "
                + "AND (d2.metadata ->> 'doc_id') = '" + TEST_CHASH + "' LIMIT 1");
            StringBuilder plan = new StringBuilder();
            while (rs.next()) {
                plan.append(rs.getString(1)).append('\n');
            }
            return plan.toString();
        }
    }

    @Test
    void isolatedReverseLookupPredicate_withLiteralValues_bindsToTheNotesGuardIndex_atNoiseScale()
            throws Exception {
        String plan = explainIsolatedReverseLookupLiteral();
        assertThat(plan)
            .as("The reverse notes-guard predicate (byte-identical to MANIFEST_LESS_CENSUS_SQL's"
                + " rev_owner LATERAL and to production's sweepChunksQuery nl3fn NOTES GUARD),"
                + " issued with LITERAL values (the hand-run-in-psql shape) against %d seeded"
                + " live note-shaped catalog_documents rows, must bind to"
                + " idx_catalog_documents_live_note_doc_id -- the whole point of catalog-038's"
                + " index. Plan was:%n%s", NOISE_NOTE_ROWS, plan)
            .contains("idx_catalog_documents_live_note_doc_id");
        assertThat(plan)
            .as("must not seq-scan catalog_documents for this lookup once the index exists and"
                + " the planner has fresh statistics (ANALYZE ran after seeding). Plan was:%n%s",
                plan)
            .doesNotContain("Seq Scan on catalog_documents");
    }

    /** Minimal {@link Embedder}: content of the vector is irrelevant — only chash/collection/metadata movement is under test. */
    private static final class ConstantEmbedder implements Embedder {
        private final int dim;
        ConstantEmbedder(int dim) { this.dim = dim; }

        @Override
        public List<float[]> embed(List<String> texts) {
            float[] v = new float[dim];
            v[0] = 1.0f;
            return texts.stream().map(t -> v.clone()).toList();
        }

        @Override
        public void close() { }
    }
}
