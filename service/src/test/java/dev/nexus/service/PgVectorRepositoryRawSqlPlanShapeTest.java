// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-191 Phase 4 (bead nexus-o8dil.16, F13 hazard) — EXPLAIN-based plan-shape proof for
 * the three raw-SQL bare-{@code embedding} sites {@link PgVectorRepository} fixed under the
 * chunks_384/768/1024 -&gt; {@code nexus.chunks} unification: {@link
 * PgVectorRepository#searchWithTokens} ({@code embedding <=> ?::vector} at the old line
 * ~993), and both branches of {@link PgVectorRepository#hybridSearch} — the selective-gate
 * rank (old line ~1324) and the dense-gate HNSW-first rank (old line ~1337).
 *
 * <p><strong>What this proves and why it matters (F13).</strong> {@code nexus.chunks} now
 * carries THREE nullable {@code embedding_384}/{@code embedding_768}/{@code embedding_1024}
 * columns (one non-null per row, DB CHECK-enforced) instead of one {@code embedding} column
 * per dim-sharded table. A bare {@code embedding} no longer resolves at all post-repoint —
 * but naming the WRONG dim's column would still PARSE (all three columns exist on the one
 * table) while silently seq-scanning instead of using that dim's FULL HNSW index
 * ({@code idx_chunks_embedding_<dim>}), because the wrong column has ~0 selectivity
 * correlation with the query vector and, more importantly, is simply the WRONG index
 * target. RDR-191's own F13 finding (docs/rdr/rdr-191-unify-chunk-tables-enable-manifest-fk.md)
 * measured this class of defect at ~250x when a PARTIAL index's predicate was omitted from
 * the query; the schema decision that followed (three FULL indexes, never partial — F13,
 * Decision item 1) means the omitted-predicate shape is exactly the one every production
 * query already uses (none of the three sites here add an {@code embedding_<dim> IS NOT
 * NULL} predicate — see each site's own code and javadoc). This suite proves that shape
 * still binds to the FULL index post-repoint, with NO {@code enable_seqscan=off} coercion —
 * an honest natural-planner-choice proof, matching F13's own "used in EVERY case, including
 * with no predicate and no filter" methodology, not a forced structural one.
 *
 * <p>Each collection's dim is seeded at a few thousand rows specifically so the planner's
 * default cost model (not a hint) prefers the HNSW index for an {@code ORDER BY <=> LIMIT n}
 * query over a Seq Scan + Sort — at trivial cardinality (tens of rows) a seq scan can be
 * genuinely cheaper and the planner's choice would say nothing about the repoint. All THREE
 * dims are seeded into the SAME {@code nexus.chunks} table (the post-repoint reality) so
 * these tests also incidentally prove the unified table does not confuse column selection
 * across dims — a wrong-dim column reference against this fixture would either error (NULL
 * arithmetic through {@code <=>}) or scan the wrong index, either of which fails these
 * assertions.
 *
 * <p>Behavioral companion: {@link #searchWithTokens_realCall_findsNearestRowByCorrectDimColumn}
 * calls the actual production method (not a hand-reconstructed query) end-to-end, so a wrong
 * column name that happens to still produce a valid plan shape (unlikely, but not provable
 * from EXPLAIN text alone) would still be caught by a wrong or empty result.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, PER_CLASS lifecycle.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorRepositoryRawSqlPlanShapeTest {

    private static final String SVC_ROLE = "svc_rawsql_planshape_test";
    private static final String SVC_PASS = "svc_rawsql_planshape_pass";
    private static final String TENANT = "rawsql-planshape-tenant";

    private static final String COL_1024 = "code__planshape__voyage-code-3__v1";
    private static final String COL_768  = "docs__planshape__bge-base-en-v15-768__v1";
    private static final String COL_384  = "knowledge__planshape__minilm-l6-v2-384__v1";

    // Modest but non-trivial per-dim cardinality: large enough that the planner's default
    // cost model naturally prefers the HNSW index over Seq Scan + Sort for an
    // ORDER BY <=> LIMIT n query (the whole point of this suite), small enough to build
    // fast under Testcontainers.
    private static final int CHUNKS_PER_DIM = 4_000;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    HikariDataSource svcDs;
    PgVectorRepository repo1024;

    /** The chash of the single row nearest the query vector, per dim — for behavioral checks. */
    private String nearestChash1024;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE
                + "') THEN CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS
                + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') "
                + "THEN CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                          new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chunks TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            // liveChunksPredicate (inlined into every site under test) reads these two
            // tables via a correlated NOT EXISTS/EXISTS — grant so the plan resolves under
            // the plain-LOGIN role, same as production (nexus-3ck2g class).
            su.createStatement().execute(
                "GRANT SELECT ON nexus.catalog_document_chunks, nexus.catalog_documents TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        var embedder1024 = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        // The query text used by searchWithTokens_realCall_findsNearestRowByCorrectDimColumn
        // embeds to exactly the seeded "nearest" row's vector (1, 0, 0, ..., 0) — explicit
        // registration rather than relying on FakeEmbedder's unregistered-text default.
        embedder1024.register("planshape nearest target chunk", 1.0f, 0.0f);
        repo1024 = new PgVectorRepository(tenantScope, embedder1024, embedder1024);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * Bulk-seeds all three dims directly into {@code nexus.chunks} (superuser, bypasses
     * RLS — same fast generate_series pattern as {@code ChashProbePlanShapeTest}) so the
     * per-dim HNSW indexes carry enough rows for the planner's default cost model to prefer
     * them. Registers each collection so RLS-scoped reads via {@code TENANT} see the rows,
     * and the write is stamped with {@code tenant_id = TENANT} directly (no per-row
     * embedder round trip needed for bulk filler).
     */
    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var st = su.createStatement();

            for (int dim : new int[] {1024, 768, 384}) {
                String coll = dim == 1024 ? COL_1024 : dim == 768 ? COL_768 : COL_384;
                st.execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                    + TENANT + "', '" + coll + "') ON CONFLICT DO NOTHING");
                String embCol = DimTables.embeddingColumn(dim);
                // Filler: a vector far from the query point (all-ones direction), so it
                // never collides with the single "nearest" row seeded below.
                st.execute(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, " + embCol + ") "
                    + "SELECT '" + TENANT + "', '" + coll + "', "
                    + "       decode(md5('planshape-" + dim + "-' || i) || md5('fill-" + dim + "-' || i), 'hex'), "
                    + "       'planshape filler chunk ' || i, v.vec "
                    + "FROM generate_series(1, " + CHUNKS_PER_DIM + ") i "
                    + "CROSS JOIN (SELECT ('[1' || repeat(',1', " + (dim - 1) + ") || ']')::vector AS vec) v");
                // The single nearest row: unit vector along the first axis (distance 0 from
                // a query vector pointed the same way) — the target every behavioral
                // assertion below checks for.
                String nearestChash = md5x2("planshape-near-" + dim, "target-" + dim);
                st.execute(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, " + embCol + ") "
                    + "VALUES ('" + TENANT + "', '" + coll + "', decode('" + nearestChash + "', 'hex'), "
                    + "'planshape nearest target chunk', "
                    + "('[1' || repeat(',0', " + (dim - 1) + ") || ']')::vector)");
                if (dim == 1024) {
                    nearestChash1024 = nearestChash;
                }
                st.execute("ANALYZE nexus.chunks");
            }
        }
    }

    private static String md5x2(String a, String b) {
        return md5Hex(a) + md5Hex(b);
    }

    private static String md5Hex(String s) {
        try {
            var md = java.security.MessageDigest.getInstance("MD5");
            StringBuilder sb = new StringBuilder();
            for (byte x : md.digest(s.getBytes(java.nio.charset.StandardCharsets.UTF_8))) {
                sb.append(String.format("%02x", x));
            }
            return sb.toString();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    /**
     * Verbatim mirror of {@link PgVectorRepository}'s private {@code liveChunksPredicate}
     * (RDR-156 Decision 6) — inlined here (rather than reflectively invoked) because the
     * production method is {@code private static}. MAINTENANCE: if
     * {@code liveChunksPredicate}'s SQL text changes, update this constant to match, or this
     * suite's plan shape silently stops mirroring production. It is RDR-191 DELIBERATELY
     * UNCHANGED (see that method's own javadoc), so this text should be stable across the
     * repoint.
     */
    private static String liveChunksPredicate(String alias) {
        return "(NOT EXISTS (SELECT 1 FROM nexus.catalog_document_chunks m"
            + " JOIN nexus.catalog_documents d ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id"
            + " WHERE m.tenant_id = " + alias + ".tenant_id AND m.chash = " + alias + ".chash"
            + " AND d.deleted_at IS NOT NULL"
            + " AND NOT EXISTS (SELECT 1 FROM nexus.catalog_document_chunks m2"
            + " JOIN nexus.catalog_documents d2 ON d2.tenant_id = m2.tenant_id AND d2.tumbler = m2.doc_id"
            + " WHERE m2.tenant_id = m.tenant_id AND m2.chash = m.chash"
            + " AND d2.deleted_at IS NULL)))";
    }

    /**
     * Runs {@code EXPLAIN} under the SAME session setting production applies before every
     * {@code searchWithTokens}/{@code hybridSearch} raw fetch ({@code SET LOCAL
     * hnsw.iterative_scan = 'relaxed_order'}, via {@link dev.nexus.service.db.PgSession
     * #setLocal}) — filtered-ANN recall keeps HNSW scanning past {@code ef_search} when RLS
     * + collection + metadata predicates narrow the candidate set. Omitting this here would
     * make the dense-gate plan (a filter that matches nearly every seeded row) an unfaithful
     * reproduction of the site under test.
     */
    private String explain(String sql) throws Exception {
        return tenantScope.withTenant(TENANT, ctx -> {
            dev.nexus.service.db.PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            StringBuilder sb = new StringBuilder();
            for (var r : ctx.resultQuery("EXPLAIN " + sql).fetch()) {
                sb.append(r.get(0, String.class)).append('\n');
            }
            return sb.toString();
        });
    }

    // ════════════════════════════════════════════════════════════════════════
    // Site 1: PgVectorRepository#searchWithTokens (embedding_<dim> <=> ?::vector,
    // FROM nexus.chunks c, WHERE c.collection IN (...) AND liveChunksPredicate) —
    // no embedding_<dim> IS NOT NULL predicate, matching production exactly.
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void searchWithTokens_shape_usesFullHnswIndex_1024() throws Exception {
        String vec = "[1" + ",0".repeat(1023) + "]";
        String sql =
            "SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata::text AS metadata_json,"
            + " (embedding_1024 <=> '" + vec + "'::vector) AS distance"
            + " FROM nexus.chunks c"
            + " WHERE c.collection IN ('" + COL_1024 + "')"
            + " AND " + liveChunksPredicate("c")
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("searchWithTokens' distance projection (1024-dim) must bind to the FULL"
                + " idx_chunks_embedding_1024 HNSW index with NO IS NOT NULL predicate added."
                + " Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_1024");
        assertThat(plan)
            .as("must not degrade to a sequential scan of the unified (mixed-dim) table."
                + " Plan was:%n%s", plan)
            .doesNotContain("Seq Scan");
    }

    @Test
    void searchWithTokens_shape_usesFullHnswIndex_768() throws Exception {
        String vec = "[1" + ",0".repeat(767) + "]";
        String sql =
            "SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata::text AS metadata_json,"
            + " (embedding_768 <=> '" + vec + "'::vector) AS distance"
            + " FROM nexus.chunks c"
            + " WHERE c.collection IN ('" + COL_768 + "')"
            + " AND " + liveChunksPredicate("c")
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("searchWithTokens' distance projection (768-dim) must bind to the FULL"
                + " idx_chunks_embedding_768 HNSW index. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_768");
        assertThat(plan).as("no Seq Scan. Plan was:%n%s", plan).doesNotContain("Seq Scan");
    }

    @Test
    void searchWithTokens_shape_usesFullHnswIndex_384() throws Exception {
        String vec = "[1" + ",0".repeat(383) + "]";
        String sql =
            "SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata::text AS metadata_json,"
            + " (embedding_384 <=> '" + vec + "'::vector) AS distance"
            + " FROM nexus.chunks c"
            + " WHERE c.collection IN ('" + COL_384 + "')"
            + " AND " + liveChunksPredicate("c")
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("searchWithTokens' distance projection (384-dim) must bind to the FULL"
                + " idx_chunks_embedding_384 HNSW index. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_384");
        assertThat(plan).as("no Seq Scan. Plan was:%n%s", plan).doesNotContain("Seq Scan");
    }

    /**
     * Behavioral companion: the REAL {@link PgVectorRepository#searchWithTokens} call,
     * end to end, must find the seeded nearest-row target — proves the fixed column choice
     * is not merely plan-shape-correct but semantically correct (a plausible-but-wrong
     * column swap between two dims of the same width class would still parse and might even
     * hit an index, but would never rank the true nearest row first).
     */
    @Test
    void searchWithTokens_realCall_findsNearestRowByCorrectDimColumn() {
        var result = repo1024.searchWithTokens(
            TENANT, "planshape nearest target chunk", List.of(COL_1024), 1, null, false);
        assertThat(result.value()).hasSize(1);
        assertThat(result.value().get(0).get("id")).isEqualTo(nearestChash1024);
    }

    // ════════════════════════════════════════════════════════════════════════
    // Site 2: hybridSearch selective-gate rank (embedding_<dim> <=> ?::vector, FROM
    // nexus.chunks c, WHERE collection IN (...) AND liveChunksPredicate AND chash IN (...))
    // — reconstructs the rank query hybridSearch builds once the bounded gate fetch
    // resolves a SELECTIVE (small) gate; see that method's javadoc.
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void hybridSearch_selectiveGateRank_shape_usesFullHnswIndex_768() throws Exception {
        String vec = "[1" + ",0".repeat(767) + "]";
        // A small, explicit chash IN (...) list mirrors the selective branch's bounded gate
        // result — any handful of live chashes from the 768-dim fixture works, the plan
        // shape under test does not depend on which chashes are named.
        String chash = md5x2("planshape-near-768", "target-768");
        String sql =
            "SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata::text AS metadata_json,"
            + " (embedding_768 <=> '" + vec + "'::vector) AS distance"
            + " FROM nexus.chunks c"
            + " WHERE collection IN ('" + COL_768 + "')"
            + " AND " + liveChunksPredicate("c")
            + " AND chash IN (decode('" + chash + "', 'hex'))"
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("hybridSearch's selective-gate rank (768-dim) must bind to the FULL"
                + " idx_chunks_embedding_768 HNSW index for its distance ORDER BY, with NO"
                + " embedding_768 IS NOT NULL predicate added. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_768");
    }

    // ════════════════════════════════════════════════════════════════════════
    // Site 3: hybridSearch dense-gate HNSW-first rank (embedding_<dim> <=> ?::vector, FROM
    // nexus.chunks c, WHERE collection IN (...) AND liveChunksPredicate AND the FTS/trigram
    // text gate) — the branch taken when the bounded gate fetch hits SELECTIVE_GATE_MAX,
    // ranking directly off the FULL HNSW index with the text gate as a co-filter.
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void hybridSearch_denseGateRank_shape_usesFullHnswIndex_384() throws Exception {
        String vec = "[1" + ",0".repeat(383) + "]";
        String sql =
            "SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata::text AS metadata_json,"
            + " (embedding_384 <=> '" + vec + "'::vector) AS distance"
            + " FROM nexus.chunks c"
            + " WHERE collection IN ('" + COL_384 + "')"
            + " AND " + liveChunksPredicate("c")
            + " AND (chunk_tsv @@ plainto_tsquery('english', 'planshape') OR 'planshape' <% chunk_text)"
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("hybridSearch's dense-gate HNSW-first rank (384-dim) must bind to the FULL"
                + " idx_chunks_embedding_384 HNSW index for its distance ORDER BY, with NO"
                + " embedding_384 IS NOT NULL predicate added. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_384");
    }

    @Test
    void seededCardinalityIsReal() throws Exception {
        try (Connection su = pg.createConnection("");
             var rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.chunks WHERE tenant_id = '" + TENANT + "'")) {
            rs.next();
            assertThat(rs.getLong(1))
                .as("the plan-shape claim is only meaningful at non-trivial cardinality")
                .isEqualTo(3L * CHUNKS_PER_DIM + 3L);
        }
    }
}
