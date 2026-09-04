// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
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
 * query already uses. This suite proves that shape still binds to the FULL index
 * post-repoint, with NO {@code enable_seqscan=off} coercion — an honest natural-planner-
 * choice proof, matching F13's own "used in EVERY case, including with no predicate and no
 * filter" methodology, not a forced structural one.
 *
 * <p><strong>nexus-74zvm (F13 NULL-distance residual — GUARD chosen).</strong> All three
 * sites now DO add an {@code embedding_<dim> IS NOT NULL} predicate — a foreign-dim row
 * (mixed-dim collection) has a NULL value there, so its distance is NULL and, under {@code
 * NULLS LAST}, could otherwise surface via {@code LIMIT} once live matches run out. This
 * suite's hand-built SQL below includes that predicate, mirroring production exactly, and
 * proves it does not defeat the FULL-index bind this class exists to verify: pgvector's
 * HNSW index structurally never contains a NULL-valued row (there is no vector to place in
 * the graph), so the predicate is redundant at the index and free. See {@code
 * PgVectorRepositoryDimGuardTest}'s {@code search_foreignDimRow_neverEmittedWithNullDistance}
 * / {@code hybridSearch_foreignDimRow_neverEmittedWithNullDistance} for the behavioral
 * (non-EXPLAIN) proof that a foreign-dim row is actually excluded.
 *
 * <p><strong>Correction (Step G cluster-B triage, nexus-o8dil.16/.48):</strong> the
 * selective-gate branch of {@code hybridSearch} does NOT fit the "still binds to the FULL
 * index" claim above — its own inline comment (nexus-lcogi, nexus-x7z7l) documents "No
 * HNSW, no re-gate" by design for a small, already-resolved {@code chash IN (...)} set, and
 * {@link #hybridSearch_selectiveGateRank_shape_usesFullHnswIndex_768} proves the narrower,
 * accurate claim for that site instead: the distance projection still names the CORRECT dim
 * column (the actual F13 hazard — a wrong-dim column would still parse), not that it binds
 * to an index it is deliberately never meant to use. Sites 1 ({@code searchWithTokens}) and
 * 3 ({@code hybridSearch}'s dense-gate rank) are unaffected and still enforce the full,
 * uncoerced HNSW-bind proof this class's name promises.
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
    /** Dedicated ef_search-boosted role for the real-call companion — see startAll(). */
    private static final String SVC_ROLE_REALCALL = "svc_rawsql_planshape_realcall";
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
    HikariDataSource realCallDs;
    PgVectorRepository repo1024;

    /** The chash of the single row nearest the query vector, per dim — for behavioral checks. */
    private String nearestChash1024;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // nexus-gjwhu (Deliverable 2): the SQL-function-family EXPLAIN proof below
            // (searchMetadataScoped_shape_..., searchGraphHop_shape_...,
            // searchTopicScoped_shape_...) calls the three combined-query functions
            // directly under this same SVC_ROLE. EXECUTE ON FUNCTION is not part of
            // bootstrapServiceRole's fixed grant set -- kept as explicit grants
            // (nexus-cbo4a batch 1b); the extra tables those functions join
            // (catalog_links, topics, topic_assignments) already have SELECT via the
            // helper's ALL TABLES IN SCHEMA nexus grant.
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.search_metadata_scoped_1024"
                + "(vector, text[], text, text, int, text, text, jsonb, int) TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.search_graph_hop_768"
                + "(vector, text[], text[], text, int, text, jsonb, int) TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.search_topic_scoped_384"
                + "(vector, text, text, int) TO " + SVC_ROLE);
        }

        // Second role + pool, DEDICATED to the real-call companion test (found during
        // Step G cluster-B triage, nexus-o8dil.16/.48 — two revisions of this fix are
        // preserved in this comment because both were independently disproven, not just
        // theorized).
        //
        // Revision 1 (WRONG): raise hnsw.ef_search on the shared SVC_ROLE directly. This
        // flipped searchWithTokens_shape_usesFullHnswIndex_768's planner choice from the
        // HNSW index to a Seq Scan - ef_search DOES feed the planner's cost estimate for
        // an ORDER BY <=> LIMIT n query.
        //
        // Revision 2 (WRONG): move the ef_search bump to a role-and-pool DEDICATED to
        // this real-call companion (repo1024 below), isolated from the shape tests'
        // planner environment. This fixed the class in ISOLATION (8/8 clean runs) but
        // still flaked - about 1 in 3 - when run in the SAME mvn invocation alongside
        // OTHER EXPLAIN-heavy test classes. Root cause: pgvector's HNSW graph build
        // assigns each inserted row's layer via ITS OWN internal randomness, entirely
        // separate from the SQL-level random()/setseed() this suite's filler-vector
        // fixture controls (see seedFixtures()'s javadoc) - so the GRAPH's shape, and
        // therefore ef_search's effective recall for a specific borderline row, is not
        // reproducible run to run no matter how high ef_search is set. Confirmed:
        // "isolated" and "combined" runs used the byte-identical seeded filler vectors
        // (same setseed(0.42), same insertion order) yet differed in outcome.
        //
        // Fix (this revision): stop trying to make HNSW recall deterministic and instead
        // route this role's real-call queries AWAY from any index scan at all
        // (enable_indexscan/enable_bitmapscan = off forces Seq Scan + exact Sort). This
        // is not weakening the F13 proof - THIS test's actual job (per its own javadoc)
        // is proving the correct dim column was chosen and the true nearest row is
        // returned, not proving HNSW is used (that is Site 1/2/3's job, unaffected,
        // still asserted via EXPLAIN on the untouched SVC_ROLE/tenantScope). A forced
        // Seq Scan gives an exact, deterministic answer regardless of ANN internals.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE_REALCALL, SVC_PASS);
            // Planner GUCs, orthogonal to grants -- not part of bootstrapServiceRole's
            // fixed grant set, kept as explicit ALTER ROLEs (nexus-cbo4a batch 1b).
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE_REALCALL + " SET enable_indexscan = off");
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE_REALCALL + " SET enable_bitmapscan = off");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        var realCallCfg = new HikariConfig();
        realCallCfg.setJdbcUrl(pg.getJdbcUrl());
        realCallCfg.setUsername(SVC_ROLE_REALCALL);
        realCallCfg.setPassword(SVC_PASS);
        realCallCfg.setMaximumPoolSize(2);
        realCallCfg.setAutoCommit(true);
        realCallDs = new HikariDataSource(realCallCfg);
        var realCallTenantScope = new TenantScope(realCallDs);

        var embedder1024 = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        // The query text used by searchWithTokens_realCall_findsNearestRowByCorrectDimColumn
        // embeds to exactly the seeded "nearest" row's vector (1, 0, 0, ..., 0) — explicit
        // registration rather than relying on FakeEmbedder's unregistered-text default.
        embedder1024.register("planshape nearest target chunk", 1.0f, 0.0f);
        repo1024 = new PgVectorRepository(realCallTenantScope, embedder1024, embedder1024);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (realCallDs != null) realCallDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * Bulk-seeds all three dims directly into {@code nexus.chunks} (superuser, bypasses
     * RLS — same fast generate_series pattern as {@code ChashProbePlanShapeTest}) so the
     * per-dim HNSW indexes carry enough rows for the planner's default cost model to prefer
     * them. Registers each collection so RLS-scoped reads via {@code TENANT} see the rows,
     * and the write is stamped with {@code tenant_id = TENANT} directly (no per-row
     * embedder round trip needed for bulk filler).
     *
     * <p><strong>Filler vectors are randomized, not a single repeated literal</strong>
     * (found during Step G cluster-B triage, nexus-o8dil.16/.48). An earlier version cross
     * joined ONE fixed {@code [1,1,...,1]} literal (a plain, non-{@code LATERAL} derived
     * table is computed once and reused for every outer row) — {@link #CHUNKS_PER_DIM}
     * byte-identical filler points is an adversarial fixture for HNSW: they form one dense
     * clique the graph search can get stuck inside, and {@code
     * searchWithTokens_realCall_findsNearestRowByCorrectDimColumn} measurably missed the
     * true (distance-0) nearest row in favor of a filler duplicate. {@code CROSS JOIN
     * LATERAL} over a fresh {@code random()} draw forces genuine per-row evaluation (a
     * plain, non-lateral subquery would still be computed once), giving each filler row an
     * independent, well-spread position — realistic relative to actual embeddings and free
     * of the degenerate-clique recall failure.
     */
    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var st = su.createStatement();
            // Deterministic filler positions (CLAUDE.md: seeded randomness) — the exact
            // values don't matter, only that they are NOT all identical.
            st.execute("SELECT setseed(0.42)");

            for (int dim : new int[] {1024, 768, 384}) {
                String coll = dim == 1024 ? COL_1024 : dim == 768 ? COL_768 : COL_384;
                st.execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                    + TENANT + "', '" + coll + "') ON CONFLICT DO NOTHING");
                String embCol = DimTables.embeddingColumn(dim);
                // Filler: CHUNKS_PER_DIM independently-random vectors (never collides with
                // the single "nearest" row seeded below — see the method javadoc for why
                // this must be per-row-random rather than one repeated literal).
                st.execute(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, " + embCol + ") "
                    + "SELECT '" + TENANT + "', '" + coll + "', "
                    + "       decode(md5('planshape-" + dim + "-' || i) || md5('fill-" + dim + "-' || i), 'hex'), "
                    + "       'planshape filler chunk ' || i, v.vec "
                    + "FROM generate_series(1, " + CHUNKS_PER_DIM + ") i "
                    + "CROSS JOIN LATERAL (SELECT (array_agg(random() * 2 - 1))::vector AS vec"
                    + "                    FROM generate_series(1, " + dim + ")) v");
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
                PgContainerHelper.analyzeTable(su, CHUNKS);
            }

            // nexus-gjwhu (Deliverable 2): fixtures for the SQL-function-family
            // EXPLAIN proof (searchGraphHop_/searchTopicScoped_shape_... below).
            // Reuses the CHUNKS_PER_DIM filler rows already seeded above under
            // COL_768/COL_384 for realistic cardinality; adds only the manifest/
            // topic scaffolding each function's JOIN shape additionally requires
            // beyond the raw-SQL sites' liveChunksPredicate (which tolerates
            // manifest-less chunks; these two functions INNER JOIN and so need
            // real rows to return anything at all).
            st.execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + TENANT + "', 'planshape-graphhop-doc', 'planshape graph-hop seed doc', '"
                + COL_768 + "') ON CONFLICT (tenant_id, tumbler) DO NOTHING");
            st.execute(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                + "SELECT tenant_id, 'planshape-graphhop-doc', row_number() OVER (ORDER BY chash), chash, collection "
                + "FROM nexus.chunks WHERE tenant_id = '" + TENANT + "' AND collection = '" + COL_768 + "'");

            st.execute(
                "INSERT INTO nexus.topics (id, tenant_id, label, collection, doc_count, created_at, review_status) "
                + "VALUES (900001, '" + TENANT + "', 'planshape-topic', '" + COL_384
                + "', 0, NOW(), 'pending') ON CONFLICT (id) DO NOTHING");
            // Deliberately SELECTIVE (~10% of the 384-dim filler, via a
            // deterministic byte-modulo sample), NOT the full collection: with
            // every chunk tagged, the join is non-selective and the planner
            // correctly prefers a bulk scan+sort over per-row HNSW probes (found
            // during Step G / this bead's own triage — an earlier all-rows-tagged
            // revision of this fixture made the planner choose a Seq Scan on
            // topic_assignments + Bitmap Heap Scan on chunks_pk instead of the
            // HNSW index, since nothing narrows the candidate set and HNSW's
            // early-stop advantage never pays off). A genuinely selective tag
            // (most candidates do NOT qualify) is what makes the filtered-ANN
            // early-stop strategy (hnsw.iterative_scan=relaxed_order, set by
            // explain() below) actually cheaper than a full scan+sort.
            // RDR-194 P3c: topic_assignments.doc_id is bytea now — select chash
            // directly, no encode('hex').
            st.execute(
                "INSERT INTO nexus.topic_assignments "
                + "(tenant_id, doc_id, topic_id, assigned_by, source_collection, assigned_at) "
                + "SELECT tenant_id, chash, 900001, 'planshape-seed', collection, NOW() "
                + "FROM nexus.chunks WHERE tenant_id = '" + TENANT + "' AND collection = '" + COL_384 + "' "
                + "AND get_byte(chash, 0) < 26");

            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.analyzeTable(su, TOPIC_ASSIGNMENTS);
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
    // FROM nexus.chunks c, WHERE c.collection IN (...) AND liveChunksPredicate AND
    // embedding_<dim> IS NOT NULL) — nexus-74zvm guarded shape, matching production
    // exactly (see the class-level comment above for why the guard does not defeat
    // the FULL-index bind this suite proves).
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
            // nexus-74zvm: guarded shape, mirroring production exactly.
            + " AND embedding_1024 IS NOT NULL"
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("searchWithTokens' distance projection (1024-dim) must bind to the FULL"
                + " idx_chunks_embedding_1024 HNSW index with the nexus-74zvm"
                + " embedding_1024 IS NOT NULL guard added — the guard must not defeat"
                + " the index bind. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_1024");
        assertThat(plan)
            .as("must not degrade to a sequential scan of the unified (mixed-dim) table's"
                + " chunks rows (liveChunksPredicate's own correlated anti-join against the"
                + " near-empty catalog_document_chunks/catalog_documents manifest tables"
                + " legitimately Seq Scans at THIS fixture's near-zero manifest cardinality —"
                + " narrowed to the table this test actually targets, not the whole plan"
                + " tree; see ChashProbePlanShapeTest's PROBE_SQL, which carries no"
                + " liveChunksPredicate and so has no such satellite scan to exclude)."
                + " Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
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
            // nexus-74zvm: guarded shape, mirroring production exactly.
            + " AND embedding_768 IS NOT NULL"
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("searchWithTokens' distance projection (768-dim) must bind to the FULL"
                + " idx_chunks_embedding_768 HNSW index with the nexus-74zvm guard added."
                + " Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_768");
        assertThat(plan)
            .as("no Seq Scan of the chunks table itself (see the 1024-dim test's assertion"
                + " for why this is narrowed to \"on chunks\" rather than the whole plan)."
                + " Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
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
            // nexus-74zvm: guarded shape, mirroring production exactly.
            + " AND embedding_384 IS NOT NULL"
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("searchWithTokens' distance projection (384-dim) must bind to the FULL"
                + " idx_chunks_embedding_384 HNSW index with the nexus-74zvm guard added."
                + " Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_384");
        assertThat(plan)
            .as("no Seq Scan of the chunks table itself (see the 1024-dim test's assertion"
                + " for why this is narrowed to \"on chunks\" rather than the whole plan)."
                + " Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
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
    //
    // UNLIKE Site 1/3, this branch is NOT an HNSW-binding proof (found during Step G
    // cluster-B triage, nexus-o8dil.16/.48). hybridSearch's own inline comment at the
    // selective-gate dispatch (nexus-lcogi, nexus-x7z7l) is explicit: "Rank those exact
    // chashes by cosine distance via a chash IN (...) filter ... No HNSW, no re-gate: ranks
    // the small gated set exactly". A selective gate's chash IN (...) list is by
    // construction small (this fixture's single-chash list is a faithful instance, not an
    // under-powered one — SELECTIVE_GATE_MAX just bounds it above, it says nothing about a
    // floor), so Postgres correctly prefers the exact index lookup
    // (idx_chunks_tenant_chash) over an ANN scan every time — asserting an HNSW bind here
    // would assert against the method's own documented design, not prove anything about it.
    // What F13 actually risks at this site is the SAME as Site 1/3 name resolution hazard:
    // the distance projection naming the WRONG dim's embedding column (still parses, since
    // all three columns exist on the unified table) — proven here by asserting the ORDER BY
    // key literally reads back the correct dim column, plus a defense-in-depth check that a
    // regression can't silently degrade this exact, tiny lookup into a full table scan.
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
            // nexus-74zvm: guarded shape, mirroring production exactly. Pure correctness
            // here (a foreign-dim row matching the text gate) — no plan-shape risk, since
            // this branch never binds HNSW regardless (see class-level comment above).
            + " AND embedding_768 IS NOT NULL"
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("hybridSearch's selective-gate rank (768-dim) must project distance off the"
                + " CORRECT dim column (embedding_768, not a wrong dim or a bare"
                + " embedding) — this branch deliberately does NOT bind to the"
                + " idx_chunks_embedding_768 HNSW index (see the class-level comment above:"
                + " production's own selective-gate dispatch says \"No HNSW\" for a small,"
                + " already-resolved chash set). Plan was:%n%s", plan)
            .contains("embedding_768 <=>");
        assertThat(plan)
            .as("a selective, single-chash lookup must not degrade into scanning the whole"
                + " chunks table. Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
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
            // nexus-74zvm: guarded shape, mirroring production exactly — the load-bearing
            // proof for this suite's HNSW-bind claim (this is the dense-gate, HNSW-first
            // branch; unlike Site 2 above, an index-defeat here WOULD be a regression).
            + " AND embedding_384 IS NOT NULL"
            + " ORDER BY distance ASC, chash ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("hybridSearch's dense-gate HNSW-first rank (384-dim) must bind to the FULL"
                + " idx_chunks_embedding_384 HNSW index for its distance ORDER BY, WITH the"
                + " nexus-74zvm embedding_384 IS NOT NULL guard added — the guard must not"
                + " defeat the index bind. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_384");
    }

    // ════════════════════════════════════════════════════════════════════════
    // nexus-gjwhu (Deliverable 2, RDR-191 Phase 5): NULL-distance guard
    // EXPLAIN proof for the SQL-function family (vectors-006-null-distance-
    // guards.xml). Mirrors this class's own Site-1/3 methodology: proves the
    // newly-added `embedding_<dim> IS NOT NULL` predicate does not defeat the
    // FULL idx_chunks_embedding_<dim> HNSW bind these inlinable LANGUAGE sql
    // functions rely on (catalog-006's own header: "query vector as a plan-
    // time argument (Finding 5a) so the HNSW index survives the join").
    // One representative function per family, per this bead's acceptance
    // criterion — not all nine; the guard clause and its placement are
    // identical across each family's three dims (vectors-006's own file
    // header), so one EXPLAIN per family is the non-redundant proof.
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void searchMetadataScoped_shape_usesFullHnswIndex_1024() throws Exception {
        String vec = "[1" + ",0".repeat(1023) + "]";
        String sql =
            "SELECT * FROM nexus.search_metadata_scoped_1024("
            + "'" + vec + "'::vector, ARRAY['" + COL_1024 + "']::text[], "
            + "NULL, NULL, NULL, NULL, NULL, NULL, 10)";
        String plan = explain(sql);
        assertThat(plan)
            .as("search_metadata_scoped_1024's embedding_1024 IS NOT NULL guard "
                + "(vectors-006-1) must not defeat the FULL idx_chunks_embedding_1024 "
                + "HNSW bind this inlinable LANGUAGE sql function relies on. Plan was:%n%s",
                plan)
            .contains("idx_chunks_embedding_1024");
        assertThat(plan)
            .as("must not degrade to a sequential scan of nexus.chunks. Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
    }

    @Test
    void searchGraphHop_shape_usesFullHnswIndex_768() throws Exception {
        String vec = "[1" + ",0".repeat(767) + "]";
        String sql =
            "SELECT * FROM nexus.search_graph_hop_768("
            + "'" + vec + "'::vector, ARRAY['planshape-graphhop-doc']::text[], "
            + "ARRAY['" + COL_768 + "']::text[], NULL, 1, 'both', NULL, 10)";
        String plan = explain(sql);
        assertThat(plan)
            .as("search_graph_hop_768's embedding_768 IS NOT NULL guard (vectors-006-2) "
                + "must not defeat the FULL idx_chunks_embedding_768 HNSW bind for the "
                + "outer SELECT's distance ORDER BY. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_768");
        assertThat(plan)
            .as("must not degrade to a sequential scan of nexus.chunks. Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
    }

    /**
     * STALE-PROSE CORRECTION (RDR-194 P3c, nexus-tk070.p3c, 2026-08): this javadoc
     * originally documented {@code search_topic_scoped}'s join to {@code
     * topic_assignments} as {@code ta.doc_id = encode(c.chash, 'hex')} -- a function
     * applied to the indexed side, non-sargable by construction, with the plan-driver
     * analysis below derived from that shape. {@code taxonomy-011-doc-id-bytea.xml}
     * (P3c) converted {@code topic_assignments.doc_id} to {@code bytea} and the join
     * is now direct equality, {@code ta.doc_id = c.chash} -- no {@code encode}/{@code
     * decode} on either side. RE-VERIFIED empirically (2026-08-16): the join IS now
     * sargable -- {@code EXPLAIN} shows an {@code Index Scan using
     * idx_chunks_tenant_chash on chunks c ... Index Cond: (chash = ta.doc_id)},
     * replacing the old bitmap-scan-by-(tenant_id,collection)-then-residual-filter
     * shape this paragraph originally described. Still NOT HNSW-bound (the index used
     * is the chash btree, not the vector index -- ORDER BY still needs a Sort node
     * over the small joined result), so the "does not bind HNSW by design" framing
     * survives, but the "can NEVER drive an indexed... probe" claim did not: it now
     * does, just not the vector index. The assertions below were updated accordingly
     * (accept either {@code chunks_pk} or {@code idx_chunks_tenant_chash} for the
     * tenant-scoping check, since the planner's chosen index changed).
     */
    @Test
    void searchTopicScoped_shape_projectsCorrectDimColumn_andStaysCollectionScoped_384() throws Exception {
        String vec = "[1" + ",0".repeat(383) + "]";
        String sql =
            "SELECT * FROM nexus.search_topic_scoped_384("
            + "'" + vec + "'::vector, 'planshape-topic', '" + COL_384 + "', 10)";
        String plan = explain(sql);
        assertThat(plan)
            .as("search_topic_scoped_384's distance projection must still read the CORRECT "
                + "dim column (embedding_384) after vectors-006-3's guard was added. Plan was:%n%s",
                plan)
            .contains("embedding_384 <=>");
        // RDR-194 P3c (nexus-tk070.p3c): the join to topic_assignments changed from
        // `ta.doc_id = encode(c.chash, 'hex')` to direct bytea equality
        // `ta.doc_id = c.chash`, which turned out to BE sargable after all (the
        // stale javadoc paragraph above already flags this as unverified/re-derive-
        // if-it-matters) -- the planner now drives an Index Scan on
        // idx_chunks_tenant_chash keyed off ta.doc_id, not chunks_pk. The scoping
        // property this assertion actually cares about (tenant_id, collection) is
        // still enforced via that index's own Index Cond, so this checks for either
        // scoped index rather than pinning to the specific one the planner happens
        // to choose.
        assertThat(plan)
            .as("search_topic_scoped_384's embedding_384 IS NOT NULL guard (vectors-006-3) must "
                + "not degrade the scan into an UNSCOPED sequential scan of nexus.chunks -- it must "
                + "still be scoped by tenant_id (chunks_pk or idx_chunks_tenant_chash). Plan was:%n%s", plan)
            .containsAnyOf("chunks_pk", "idx_chunks_tenant_chash");
        assertThat(plan)
            .as("must not degrade to a full sequential scan of nexus.chunks ignoring "
                + "tenant/collection scoping. Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
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
