// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-156 P4.1 (bead nexus-70r3c.14) — TDD-RED suite for the Phase-4 combined-query
 * deliverable (Decision 5, the unification of the two highest-traffic cross-store
 * stitches into single planner-optimizable statements).
 *
 * <p><strong>Two shapes pinned here</strong> (the {@code query} MCP tool's catalog
 * dance and topic-scoped search), each as a per-dim set-returning SQL function so the
 * planner can inline it and keep the query vector a plan-time constant (Finding 5a):
 * <ul>
 *   <li>{@code nexus.search_metadata_scoped_<dim>(p_query vector, p_collections text[],
 *       p_content_type text, p_author text, p_year int, p_corpus text, p_n int)} —
 *       joins {@code chunks_<dim> ⋈ catalog_document_chunks ⋈ catalog_documents},
 *       filters by the catalog metadata dimensions the {@code query} tool routes on
 *       (NULL arg = no filter on that dimension), ranks by cosine distance ascending.</li>
 *   <li>{@code nexus.search_topic_scoped_<dim>(p_query vector, p_topic_label text,
 *       p_collection text, p_n int)} — chunk-level: joins {@code chunks_<dim> ⋈
 *       topic_assignments} on {@code chash = topic_assignments.doc_id} (topic membership
 *       is chunk-keyed, nexus-sa14p) {@code ⋈ topics}, scoped to the named topic label
 *       within a collection, live-filtered (live_chunks predicate), ranks by cosine
 *       distance ascending, returns the chunk chash as id.</li>
 * </ul>
 *
 * <p><strong>The four mandatory encodings (bead nexus-70r3c.14)</strong>, mapped to
 * test groups:
 * <ol>
 *   <li><strong>Query-vector-as-argument (Finding 5a)</strong> — GROUP 1 / GROUP 2:
 *       every function takes the probe vector as its first ARGUMENT (asserted via
 *       {@code pg_proc} signature introspection and by calling with a literal vector).
 *       A join-sourced vector is a hard FAIL (it produces a 340 ms Seq Scan instead of
 *       the 2 ms HNSW Index Scan).</li>
 *   <li><strong>EXPLAIN plan check — HNSW survives the join</strong> — GROUP 3: the
 *       plan for the combined query uses {@code idx_chunks_<dim>_embedding} (the HNSW
 *       index), never a Seq Scan on the chunks table. A {@code Function Scan} node
 *       (i.e. a non-inlinable plpgsql body) also fails this group — only an inlinable
 *       LANGUAGE sql function exposes the underlying index scan to EXPLAIN.</li>
 *   <li><strong>Narrow-collection exact-recall {@code == N}</strong> — GROUP 4: a
 *       narrow collection (size &lt; {@code hnsw.ef_search}) with a semantically distant
 *       query vector, asserted with an EXACT count ({@code == N}, never
 *       {@code >= threshold}). At container scale this is the correctness pin (exact
 *       recall, no silent under-return); the production-scale {@code max_scan_tuples}
 *       ceiling reproduction stayed conexus xr7.8.9 / the Chroma-era DualRunHarness
 *       (deleted at RDR-155 P4b),
 *       same scope split as RDR-155 P3.E.</li>
 *   <li><strong>Parity vs the stitched path</strong> — GROUP 5: each combined query
 *       returns IDENTICAL results to the app-side stitch it retires (catalog filter →
 *       per-collection vector search → re-rank), computed here as an in-test SQL oracle.</li>
 * </ol>
 *
 * <p><strong>Non-vacuity</strong> (conexus xr7.8.7 / hybrid-test pattern): the topic
 * fixture places a vector-CLOSER row under a DIFFERENT topic so a non-filtering
 * implementation cannot pass GROUP 2; the metadata fixture's filtered orderings differ
 * from the unfiltered ordering so a no-op filter cannot pass GROUP 1; tombstone tests
 * (GROUP 6) assert BOTH directions; RLS tests (GROUP 7) assert a superuser CONTROL
 * proving foreign rows exist underneath.
 *
 * <p><strong>Expected RED before catalog-006 lands</strong>: every functional group
 * errors with "function ... does not exist" until bead nexus-70r3c.15 (P4.2) adds the
 * per-dim functions. CONTROL paths (raw fixture inserts, raw vector order-by oracle)
 * are GREEN always.
 *
 * <p>Mirrors conventions from CollectionVectorStatsTest / PgVectorHybridSearchContractTest:
 * PgContainerHelper, Liquibase master, PER_CLASS, {@link Order}, superuser fixtures,
 * svc-role + GUC for RLS, 32-char chashes, registered collections (fk-002), exact
 * assertions ({@code containsExactly} / {@code isEqualTo}, never inequalities), and the
 * 2-D unit-vector trick (only the first two components are non-zero, so cosine distance
 * to the query {@code (1,0)} is fully determined and exact).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CombinedQueryParityTest {

    // ── Tenant IDs ────────────────────────────────────────────────────────────
    private static final String TENANT_A = "cqp-tenant-a";
    private static final String TENANT_B = "cqp-tenant-b";

    // ── Svc role (NOSUPERUSER, NOBYPASSRLS — subject to FORCE RLS) ───────────
    private static final String SVC_ROLE = "svc_cqp_test";
    private static final String SVC_PASS = "svc_cqp_test_pass";

    // ── Combined-query function names (catalog-006 must create these) ─────────
    private static final String FN_META_1024  = "nexus.search_metadata_scoped_1024";
    private static final String FN_META_384   = "nexus.search_metadata_scoped_384";
    private static final String FN_TOPIC_1024 = "nexus.search_topic_scoped_1024";
    private static final String FN_TOPIC_384  = "nexus.search_topic_scoped_384";
    // RDR-156 Decision 5 (bead nexus-ubnwk): the fourth combined-query shape.
    private static final String FN_ASPECT_1024 = "nexus.search_aspect_scoped_1024";

    // ── Collections (conformant <type>__<owner>__<model>__<version>) ─────────
    private static final String COLL_M     = "knowledge__cqp-meta__voyage-context-3__v1";       // 1024
    private static final String COLL_T     = "knowledge__cqp-topic__voyage-context-3__v1";      // 1024
    private static final String COLL_NARROW= "knowledge__cqp-narrow__voyage-context-3__v1";     // 1024
    private static final String COLL_M_384 = "knowledge__cqp-meta384__minilm-l6-v2-384__v1";    // 384
    private static final String COLL_T_384 = "knowledge__cqp-topic384__minilm-l6-v2-384__v1";   // 384
    private static final String COLL_B     = "knowledge__cqp-b__voyage-context-3__v1";          // 1024, tenant B
    // Large (non-selective) fixture: lets the planner pick the HNSW index over a
    // filter-first sort, so the EXPLAIN encoding (GROUP 3) is meaningful. At the tiny
    // GROUP-1 scale a selective filter correctly wins (the selectivity switch) and HNSW
    // is rightly NOT chosen — that is not what GROUP 3 is asserting.
    private static final String COLL_EXPLAIN = "knowledge__cqp-explain__voyage-context-3__v1";  // 1024
    private static final int    EXPLAIN_ROWS = 500;
    // catalog-008 (nexus-889ff): subtree (dotted tumblers) + where (chunk metadata) fixtures.
    private static final String COLL_SUB   = "knowledge__cqp-sub__voyage-context-3__v1";       // 1024
    private static final String COLL_WHERE = "knowledge__cqp-where__voyage-context-3__v1";     // 1024
    // RDR-156 Decision 5 (bead nexus-ubnwk): aspect-scoped fixtures.
    private static final String COLL_ASPECT        = "knowledge__cqp-aspect__voyage-context-3__v1";        // 1024
    private static final String COLL_ASPECT_NARROW = "knowledge__cqp-aspect-narrow__voyage-context-3__v1"; // 1024
    private static final String COLL_ASPECT_EXPLAIN = "knowledge__cqp-aspect-explain__voyage-context-3__v1"; // 1024

    // ── Topic labels ─────────────────────────────────────────────────────────
    private static final String TOPIC_VEC   = "Vector Search";
    private static final String TOPIC_OTHER = "Cooking";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;

    // ══════════════════════════════════════════════════════════════════════════
    // LIFECYCLE
    // ══════════════════════════════════════════════════════════════════════════

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Phase 1: roles (CREATE ROLE cannot run inside a transaction).
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    /**
     * Seed all fixtures once via superuser (bypasses FORCE RLS). Distances are exact:
     * each chunk's embedding is a 2-D unit direction (x,y) padded to {@code dim}; cosine
     * distance to the probe vector {@code (1,0)} is {@code 1 - x} for a unit (x,y).
     *
     * <p>Metadata fixture COLL_M — one chunk per doc, doc carries the metadata:
     * <pre>
     *   doc  content_type author year corpus   dir       dist  query=(1,0)
     *   m1   paper        alice  2024 research (1.0,0.0)  0.0
     *   m2   paper        bob    2023 research (0.8,0.6)  0.2
     *   m3   code         alice  2024 research (0.6,0.8)  0.4   (excluded by type=paper)
     *   m4   paper        alice  2022 archive  (0.0,1.0)  1.0
     *   m5   paper        carol  2024 research (-1.0,0.0) 2.0
     * </pre>
     * type=paper                 → [m1,m2,m4,m5]   (m3 dropped)
     * type=paper AND author=alice→ [m1,m4]
     * author=alice (type NULL)   → [m1,m3,m4]      (ordering differs from type=paper → filter is real)
     * year=2024                  → [m1,m3,m5]
     *
     * <p>Topic fixture COLL_T — TOPIC_VEC has {t1,t2}; t3 is vector-CLOSER but under
     * TOPIC_OTHER, so a non-filtering impl would wrongly surface it:
     * <pre>
     *   doc  topic       dir       dist
     *   t1   Vector...   (1.0,0.0) 0.0
     *   t2   Vector...   (0.6,0.8) 0.4
     *   t3   Cooking     (0.8,0.6) 0.2   (closer than t2, MUST be excluded)
     * </pre>
     * topic=Vector Search → [t1,t2]   (t3 excluded despite dist 0.2 &lt; 0.4 → topic gate real)
     *
     * <p>Narrow fixture COLL_NARROW — 3 chunks, query vector semantically distant
     * (orthogonal-ish); exact-recall must return all 3 (==N), no silent under-return.
     */
    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // ----- metadata fixture (1024) -----
            insertCollection(su, TENANT_A, COLL_M);
            seedMetaDoc(su, 1024, COLL_M, "m1", "paper", "alice", 2024, "research", 1.0, 0.0);
            seedMetaDoc(su, 1024, COLL_M, "m2", "paper", "bob",   2023, "research", 0.8, 0.6);
            seedMetaDoc(su, 1024, COLL_M, "m3", "code",  "alice", 2024, "research", 0.6, 0.8);
            seedMetaDoc(su, 1024, COLL_M, "m4", "paper", "alice", 2022, "archive",  0.0, 1.0);
            seedMetaDoc(su, 1024, COLL_M, "m5", "paper", "carol", 2024, "research", -1.0, 0.0);

            // ----- metadata fixture (384) for per-dim dispatch -----
            insertCollection(su, TENANT_A, COLL_M_384);
            seedMetaDoc(su, 384, COLL_M_384, "m384a", "paper", "alice", 2024, "research", 1.0, 0.0);
            seedMetaDoc(su, 384, COLL_M_384, "m384b", "code",  "alice", 2024, "research", 0.8, 0.6);

            // ----- topic fixture (1024) -----
            insertCollection(su, TENANT_A, COLL_T);
            long topicVec   = insertTopic(su, TENANT_A, TOPIC_VEC,   COLL_T);
            long topicOther = insertTopic(su, TENANT_A, TOPIC_OTHER, COLL_T);
            seedTopicDoc(su, 1024, COLL_T, "t1", topicVec,   1.0, 0.0);
            seedTopicDoc(su, 1024, COLL_T, "t2", topicVec,   0.6, 0.8);
            seedTopicDoc(su, 1024, COLL_T, "t3", topicOther, 0.8, 0.6);

            // ----- topic fixture (384) for per-dim dispatch -----
            insertCollection(su, TENANT_A, COLL_T_384);
            long topicVec384 = insertTopic(su, TENANT_A, TOPIC_VEC, COLL_T_384);
            seedTopicDoc(su, 384, COLL_T_384, "t384a", topicVec384, 1.0, 0.0);

            // ----- narrow fixture (1024): 3 docs, distant query -----
            insertCollection(su, TENANT_A, COLL_NARROW);
            seedMetaDoc(su, 1024, COLL_NARROW, "n1", "paper", "ned", 2024, "research", 0.0, 1.0);
            seedMetaDoc(su, 1024, COLL_NARROW, "n2", "paper", "ned", 2024, "research", -0.2, 0.9797959);
            seedMetaDoc(su, 1024, COLL_NARROW, "n3", "paper", "ned", 2024, "research", 0.2, 0.9797959);

            // ----- tenant-B fixture (1024) for RLS group -----
            insertCollection(su, TENANT_B, COLL_B);
            long topicVecB = insertTopic(su, TENANT_B, TOPIC_VEC, COLL_B);
            seedMetaDoc(su, 1024, COLL_B, "b1", "paper", "zed", 2024, "research", 1.0, 0.0);
            seedTopicDoc(su, 1024, COLL_B, "b2", topicVecB, 1.0, 0.0);

            // ----- subtree fixture (1024): dotted tumblers for the prefix predicate -----
            // subtree="1.2" must match {1.2, 1.2.3, 1.2.4} (self + descendants) but NOT
            // 1.3.1 (sibling subtree) and NOT 1.20 (prefix-string-but-not-tumbler-child).
            insertCollection(su, TENANT_A, COLL_SUB);
            seedMetaDoc(su, 1024, COLL_SUB, "1.2",   "paper", "sa", 2024, "research", 1.0, 0.0);  // 0.0
            seedMetaDoc(su, 1024, COLL_SUB, "1.2.3", "paper", "sa", 2024, "research", 0.8, 0.6);  // 0.2
            seedMetaDoc(su, 1024, COLL_SUB, "1.2.4", "paper", "sa", 2024, "research", 0.6, 0.8);  // 0.4
            seedMetaDoc(su, 1024, COLL_SUB, "1.3.1", "paper", "sa", 2024, "research", 0.0, 1.0);  // 1.0 (sibling)
            seedMetaDoc(su, 1024, COLL_SUB, "1.20",  "paper", "sa", 2024, "research", -1.0, 0.0); // 2.0 (string-prefix trap)

            // ----- where fixture (1024): chunks carry metadata for the @> containment gate -----
            insertCollection(su, TENANT_A, COLL_WHERE);
            seedMetaDocMeta(su, COLL_WHERE, "w1", 1.0, 0.0, "{\"lang\": \"java\"}");   // 0.0
            seedMetaDocMeta(su, COLL_WHERE, "w2", 0.8, 0.6, "{\"lang\": \"py\"}");     // 0.2
            seedMetaDocMeta(su, COLL_WHERE, "w3", 0.6, 0.8, "{\"lang\": \"java\"}");   // 0.4

            // ----- aspect-scoped fixture (1024): RDR-156 Decision 5 -----
            seedAspectFixture(su);

            // ----- large fixture for the EXPLAIN group (GROUP 3) -----
            seedExplainFixture(su);

            // Real row-count statistics so the planner does not under-estimate the
            // bulk-loaded tables to rows=1 and wrongly prefer a filter-first sort over
            // the HNSW index in GROUP 3.
            for (String tbl : List.of("chunks", "catalog_documents",
                    "catalog_document_chunks", "topic_assignments", "topics",
                    "document_aspects")) {
                su.createStatement().execute("ANALYZE nexus." + tbl);
            }
        }
    }

    /**
     * Bulk-seed {@link #EXPLAIN_ROWS} paper documents in COLL_EXPLAIN, all assigned to
     * TOPIC_VEC, server-side (one statement per table, no JDBC round-trip per row). The
     * volume + non-selective predicate is what makes the planner choose the HNSW index
     * over a filter-first sort, so GROUP 3 asserts a real "HNSW survives the join" plan.
     */
    private void seedExplainFixture(Connection su) throws Exception {
        insertCollection(su, TENANT_A, COLL_EXPLAIN);
        long topicId = insertTopic(su, TENANT_A, TOPIC_VEC, COLL_EXPLAIN);
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents " +
            "  (tenant_id, tumbler, title, author, year, content_type, corpus, physical_collection) " +
            "SELECT '" + TENANT_A + "', 'ex'||g, 'Doc '||g, 'exauthor', 2024, 'paper', 'research', '" +
            COLL_EXPLAIN + "' FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
        // RDR-180: chash is bytea(32) now — the lpad'd decimal string is still valid
        // hex (digits 0-9 only), padded to the full 64-hex canonical width and
        // decoded at the seam. topic_assignments.doc_id stays TEXT but is a chunk
        // chash end to end (RDR-180 Item6/Item6a, never a memory-note title and not
        // a mixed identity space — nexus-79box correction), padded to the SAME
        // 64-hex width so it matches encode(c.chash,'hex') at topic-scoped join time.
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires the
        // nexus.chunks row before the manifest row below -- chunk insert first.
        // Embedding: 2-D direction (g%100/100, 1) padded to 1024 — varied enough that
        // HNSW is exercised, dense in the (x,1) plane.
        su.createStatement().execute(
            "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ") " +
            "SELECT '" + TENANT_A + "', '" + COLL_EXPLAIN + "', decode(lpad(g::text, 64, '0'), 'hex'), 'ex'||g, " +
            "('[' || ((g % 100)::float8 / 100.0) || ',1' || repeat(',0', 1022) || ']')::vector " +
            "FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) " +
            "SELECT '" + TENANT_A + "', 'ex'||g, 0, decode(lpad(g::text, 64, '0'), 'hex'), '" + COLL_EXPLAIN + "' " +
            "FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
        // RDR-194 P3c: topic_assignments.doc_id is bytea now — decode(lpad(...), 'hex').
        su.createStatement().execute(
            "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, source_collection, assigned_at) " +
            "SELECT '" + TENANT_A + "', decode(lpad(g::text, 64, '0'), 'hex'), " + topicId + ", '"
            + COLL_EXPLAIN + "', " +
            "'2026-01-01T00:00:00+00'::timestamptz FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
    }

    /**
     * Aspect-scoped fixture COLL_ASPECT (RDR-156 Decision 5, bead nexus-ubnwk) — four
     * docs, each seeded via {@link #seedMetaDoc} (catalog doc + chunk + manifest):
     * <pre>
     *   doc   proposed_method            confidence  doc_id     dir        dist
     *   asp1  "Uses gradient descent"    0.9         stamped    (1.0,0.0)  0.0
     *   asp2  "Uses gradient boosting"   0.2         stamped    (0.8,0.6)  0.2
     *   asp3  (no document_aspects row)  —           —          (0.6,0.8)  0.4
     *   asp4  "Uses gradient descent"    0.9         stamped    (0.0,1.0)  1.0
     * </pre>
     * field=proposed_method, pattern="gradient" → matches asp1,asp2,asp4 (asp3 has no
     * aspects row — INNER JOIN excludes it).
     * pattern="descent" → matches asp1,asp4 only (asp2 says "boosting"); asp2 is
     * nearer than asp4, so a top-K=2 vector-then-filter two-step misses asp4 entirely
     * — the GROUP 10 recall-superset probe.
     *
     * <p>A fifth doc ("asp5", NULL doc_id, content otherwise identical to asp1/asp4)
     * used to sit here to prove the join's {@code a.doc_id = d.tumbler} condition
     * excludes a NULL-doc_id row even when its content matches. document_aspects.doc_id
     * is NOT NULL now (hygiene-001-1, nexus-tk070.p6a follow-on) — such a row can no
     * longer be constructed at all, so that scenario is gone from this fixture; the
     * (now strictly stronger) replacement assertion — the INSERT itself is rejected —
     * lives in {@link #aspectDocId_nowRequired_insertWithNullDocIdRejected()}.
     */
    private void seedAspectFixture(Connection su) throws Exception {
        insertCollection(su, TENANT_A, COLL_ASPECT);
        seedMetaDoc(su, 1024, COLL_ASPECT, "asp1", "paper", "asp", 2024, "research", 1.0, 0.0);
        insertAspectRow(su, TENANT_A, COLL_ASPECT, "asp1", "asp1",
                        "Uses gradient descent", 0.9);
        seedMetaDoc(su, 1024, COLL_ASPECT, "asp2", "paper", "asp", 2024, "research", 0.8, 0.6);
        insertAspectRow(su, TENANT_A, COLL_ASPECT, "asp2", "asp2",
                        "Uses gradient boosting", 0.2);
        seedMetaDoc(su, 1024, COLL_ASPECT, "asp3", "paper", "asp", 2024, "research", 0.6, 0.8);
        // asp3 deliberately has NO document_aspects row at all.
        seedMetaDoc(su, 1024, COLL_ASPECT, "asp4", "paper", "asp", 2024, "research", 0.0, 1.0);
        insertAspectRow(su, TENANT_A, COLL_ASPECT, "asp4", "asp4",
                        "Uses gradient descent", 0.9);

        // Narrow fixture (GROUP 9-style exact recall): 3 chunks, all matching the
        // aspect predicate, distant query vector.
        insertCollection(su, TENANT_A, COLL_ASPECT_NARROW);
        seedMetaDoc(su, 1024, COLL_ASPECT_NARROW, "asn1", "paper", "asn", 2024, "research", 0.0, 1.0);
        insertAspectRow(su, TENANT_A, COLL_ASPECT_NARROW, "asn1", "asn1", "Uses gradient descent", 0.9);
        seedMetaDoc(su, 1024, COLL_ASPECT_NARROW, "asn2", "paper", "asn", 2024, "research", -0.2, 0.9797959);
        insertAspectRow(su, TENANT_A, COLL_ASPECT_NARROW, "asn2", "asn2", "Uses gradient descent", 0.9);
        seedMetaDoc(su, 1024, COLL_ASPECT_NARROW, "asn3", "paper", "asn", 2024, "research", 0.2, 0.9797959);
        insertAspectRow(su, TENANT_A, COLL_ASPECT_NARROW, "asn3", "asn3", "Uses gradient descent", 0.9);

        // Bulk, non-selective fixture for the EXPLAIN group: every doc has a matching
        // aspects row, so the planner cannot use aspect selectivity to avoid the HNSW
        // index — the same "HNSW survives the join" property GROUP 3 pins for the
        // metadata/topic joins.
        seedAspectExplainFixture(su);
    }

    private static void insertAspectRow(Connection su, String tenantId, String collection,
                                        String sourcePath, String docId,
                                        String proposedMethod, double confidence)
            throws Exception {
        // source_uri: document_aspects.source_uri is NOT NULL (hygiene-001-1,
        // nexus-tk070.p6a follow-on). No assertion in this class reads the
        // column, so a synthetic per-row value satisfies the constraint
        // without changing any assertion surface.
        su.createStatement().execute(
            "INSERT INTO nexus.document_aspects " +
            "  (tenant_id, collection, source_path, proposed_method, confidence, " +
            "   extracted_at, model_version, extractor_name, doc_id, source_uri) " +
            "VALUES ('" + tenantId + "', '" + collection + "', '" + sourcePath + "', " +
            sqlText(proposedMethod) + ", " + confidence + ", " +
            "'2026-01-01T00:00:00+00'::timestamptz, 'cqp-test-model', 'cqp-test-extractor', " +
            (docId == null ? "NULL" : sqlText(docId)) + ", " +
            sqlText("cqp://" + collection + "/" + sourcePath) + ") " +
            "ON CONFLICT (tenant_id, collection, source_path) DO UPDATE SET " +
            "  proposed_method = EXCLUDED.proposed_method, " +
            "  confidence = EXCLUDED.confidence, doc_id = EXCLUDED.doc_id");
    }

    /**
     * Bulk-seed {@link #EXPLAIN_ROWS} paper documents in COLL_ASPECT_EXPLAIN, each
     * with a matching {@code document_aspects} row (proposed_method contains
     * "gradient", doc_id pre-stamped — this fixture models the POST-backfill steady
     * state, not the backfill itself). Mirrors {@link #seedExplainFixture}'s volume
     * and non-selectivity rationale.
     */
    private void seedAspectExplainFixture(Connection su) throws Exception {
        insertCollection(su, TENANT_A, COLL_ASPECT_EXPLAIN);
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents " +
            "  (tenant_id, tumbler, title, author, year, content_type, corpus, physical_collection) " +
            "SELECT '" + TENANT_A + "', 'asx'||g, 'Doc '||g, 'asxauthor', 2024, 'paper', 'research', '" +
            COLL_ASPECT_EXPLAIN + "' FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
        su.createStatement().execute(
            "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ") " +
            "SELECT '" + TENANT_A + "', '" + COLL_ASPECT_EXPLAIN + "', decode(lpad((g+1000000)::text, 64, '0'), 'hex'), 'asx'||g, " +
            "('[' || ((g % 100)::float8 / 100.0) || ',1' || repeat(',0', 1022) || ']')::vector " +
            "FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) " +
            "SELECT '" + TENANT_A + "', 'asx'||g, 0, decode(lpad((g+1000000)::text, 64, '0'), 'hex'), '" + COLL_ASPECT_EXPLAIN + "' " +
            "FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
        su.createStatement().execute(
            // source_uri: document_aspects.source_uri is NOT NULL
            // (hygiene-001-1) -- synthetic per-row value, no assertion reads it.
            "INSERT INTO nexus.document_aspects " +
            "  (tenant_id, collection, source_path, proposed_method, confidence, " +
            "   extracted_at, model_version, extractor_name, doc_id, source_uri) " +
            "SELECT '" + TENANT_A + "', '" + COLL_ASPECT_EXPLAIN + "', 'asx'||g, " +
            "'Uses gradient descent', 0.9, '2026-01-01T00:00:00+00'::timestamptz, " +
            "'cqp-test-model', 'cqp-test-extractor', 'asx'||g, 'cqp://' || '" + COLL_ASPECT_EXPLAIN + "' || '/asx'||g " +
            "FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 1 — metadata-scoped: query-vector-as-argument + exact filtered ranking
    //
    // EXPECTED RED: nexus.search_metadata_scoped_1024 absent until catalog-006.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void metadata_queryVectorIsFirstArgument_signaturePinned() throws Exception {
        // The probe vector MUST be a function ARGUMENT (Finding 5a). Introspect the
        // declared signature: first arg is a `vector`, and the seven-arg shape is the
        // pinned contract catalog-006 must honor.
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT pg_catalog.pg_get_function_arguments(p.oid) AS args " +
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace " +
                " WHERE n.nspname = 'nexus' AND p.proname = 'search_metadata_scoped_1024'");
            assertThat(rs.next())
                .as("nexus.search_metadata_scoped_1024 must exist (catalog-006)").isTrue();
            String args = rs.getString("args");
            assertThat(args)
                .as("query vector must be the FIRST argument, typed `vector` — never "
                    + "join-sourced (Finding 5a: a join-sourced vector forces a 340ms "
                    + "Seq Scan instead of the 2ms HNSW Index Scan)")
                .startsWith("p_query vector");
            assertThat(args)
                .as("metadata-scoped signature pins the catalog dimensions the query "
                    + "tool routes on: collections[], content_type, author, year, corpus, "
                    + "subtree, where, n (catalog-008 added subtree + where(jsonb))")
                .contains("p_collections text[]")
                .contains("p_content_type text")
                .contains("p_author text")
                .contains("p_year integer")
                .contains("p_corpus text")
                .contains("p_subtree text")
                .contains("p_where jsonb")
                .contains("p_n integer");
        }
    }

    @Test @Order(11)
    void metadata_contentTypeFilter_exactRankedByDistance() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callMeta(su, 1024, COLL_M, "paper", null, null, null, 10))
                .as("content_type=paper → {m1,m2,m4,m5} ranked by cosine distance "
                    + "(0.0,0.2,1.0,2.0); m3 (code) excluded")
                .containsExactly("m1", "m2", "m4", "m5");
        }
    }

    @Test @Order(12)
    void metadata_typeAndAuthor_compose_exact() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callMeta(su, 1024, COLL_M, "paper", "alice", null, null, 10))
                .as("type=paper AND author=alice → {m1,m4} ranked by distance (0.0,1.0)")
                .containsExactly("m1", "m4");
        }
    }

    @Test @Order(13)
    void metadata_authorOnly_orderingDiffersFromTypeFilter_filterIsReal() throws Exception {
        try (Connection su = pg.createConnection("")) {
            List<String> authorOnly = callMeta(su, 1024, COLL_M, null, "alice", null, null, 10);
            assertThat(authorOnly)
                .as("author=alice (type NULL=no filter) → {m1,m3,m4} by distance "
                    + "(0.0,0.4,1.0) — includes m3 (code), excluded under type=paper")
                .containsExactly("m1", "m3", "m4");
            assertThat(authorOnly)
                .as("author-only ordering must DIFFER from the type=paper ordering — "
                    + "otherwise a NULL arg is not actually skipping the filter (vacuous)")
                .isNotEqualTo(callMeta(su, 1024, COLL_M, "paper", null, null, null, 10));
        }
    }

    @Test @Order(14)
    void metadata_yearFilter_exact() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callMeta(su, 1024, COLL_M, null, null, 2024, null, 10))
                .as("year=2024 → {m1,m3,m5} by distance (0.0,0.4,2.0)")
                .containsExactly("m1", "m3", "m5");
        }
    }

    @Test @Order(15)
    void metadata_nResultsLimit_truncatesRankedList() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callMeta(su, 1024, COLL_M, "paper", null, null, null, 2))
                .as("n=2 truncates the ranked list to the two nearest papers [m1,m2]")
                .containsExactly("m1", "m2");
        }
    }

    @Test @Order(16)
    void metadata_noFilters_allChunksRanked() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callMeta(su, 1024, COLL_M, null, null, null, null, 10))
                .as("all NULL filters → every chunk in COLL_M ranked by distance "
                    + "[m1,m2,m3,m4,m5] (0.0,0.2,0.4,1.0,2.0)")
                .containsExactly("m1", "m2", "m3", "m4", "m5");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — topic-scoped: query-vector-as-argument + topic gate is load-bearing
    //
    // EXPECTED RED: nexus.search_topic_scoped_1024 absent until catalog-006.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void topic_queryVectorIsFirstArgument_signaturePinned() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT pg_catalog.pg_get_function_arguments(p.oid) AS args " +
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace " +
                " WHERE n.nspname = 'nexus' AND p.proname = 'search_topic_scoped_1024'");
            assertThat(rs.next())
                .as("nexus.search_topic_scoped_1024 must exist (catalog-006)").isTrue();
            String args = rs.getString("args");
            assertThat(args)
                .as("topic-scoped query vector must be the FIRST argument, typed `vector`")
                .startsWith("p_query vector");
            assertThat(args)
                .as("topic-scoped signature: topic_label, collection, n")
                .contains("p_topic_label text")
                .contains("p_collection text")
                .contains("p_n integer");
        }
    }

    @Test @Order(21)
    void topic_scopedToLabel_excludesVectorCloserOtherTopic() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callTopic(su, 1024, COLL_T, TOPIC_VEC, 10))
                .as("topic='Vector Search' → chunks {t1,t2} by distance (0.0,0.4). t3 is "
                    + "vector-CLOSER (0.2) but under topic 'Cooking' — its exclusion "
                    + "proves the topic gate is load-bearing, not a vector passthrough")
                .containsExactly(chashOf("t1"), chashOf("t2"));
        }
    }

    @Test @Order(22)
    void topic_otherLabel_returnsOnlyItsMembers() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callTopic(su, 1024, COLL_T, TOPIC_OTHER, 10))
                .as("topic='Cooking' → chunk {t3} only")
                .containsExactly(chashOf("t3"));
        }
    }

    @Test @Order(23)
    void topic_unknownLabel_returnsEmpty() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callTopic(su, 1024, COLL_T, "No Such Topic", 10))
                .as("an unknown topic label returns empty, never a silent vector fallback")
                .isEmpty();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 3 — EXPLAIN: the HNSW index scan survives the join (Finding 5a/5b)
    //
    // EXPECTED RED: function absent until catalog-006.
    //
    // enable_seqscan=off forces the planner to reach for the index; with a literal
    // vector argument the HNSW index idx_chunks_embedding_1024 IS usable for the
    // ORDER BY embedding <=> p_query. The assertion proves:
    //   (a) the function is INLINABLE (a plpgsql body shows "Function Scan" and no
    //       inner index node → fails — only LANGUAGE sql exposes the index scan);
    //   (b) the vector is a plan-time constant (a join-sourced vector cannot use the
    //       index at all → Seq Scan → fails);
    //   (c) the metadata join does not force a Seq Scan on the chunks table.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void explain_metadataScoped_usesHnswIndex_notSeqScan() throws Exception {
        // Large, NON-selectively-filtered fixture: the vector ANN is the driving access
        // path, so a correct impl uses the HNSW index. (A selective filter at small
        // scale rightly wins with a filter-first sort — that is GROUP 1's regime, not
        // this one.)
        String plan = explainMetaPlan(1024, COLL_EXPLAIN, null);
        assertThat(plan)
            .as("combined metadata query must use the HNSW index "
                + "idx_chunks_embedding_1024 for the ANN ordering — the vector is a "
                + "plan-time argument and the function inlines. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_1024");
        assertThat(plan)
            .as("the metadata join must NOT defeat the index into a Seq Scan on "
                + "nexus.chunks (a filter that defeats the index is a regression, not a "
                + "simplification — Decision 5). Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
        assertThat(plan)
            .as("a Function Scan node means the function is not inlinable (plpgsql) — "
                + "EXPLAIN cannot then see the index scan; catalog-006 must use an "
                + "inlinable LANGUAGE sql function. Plan was:%n%s", plan)
            .doesNotContain("Function Scan");
    }

    @Test @Order(31)
    void explain_topicScoped_usesHnswIndex_notSeqScan() throws Exception {
        String plan = explainTopicPlan(1024, COLL_EXPLAIN, TOPIC_VEC);
        assertThat(plan)
            .as("topic-scoped query must keep the HNSW index scan through the "
                + "topic_assignments join. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_1024");
        assertThat(plan)
            .as("topic join must not force a Seq Scan on nexus.chunks. Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — narrow-collection EXACT-recall == N (Finding 5b, MANDATORY)
    //
    // EXPECTED RED: function absent until catalog-006.
    //
    // COLL_NARROW has exactly 3 chunks; the query vector (1,0) is semantically distant
    // from all three (they cluster near (0,1)). The combined query must return ALL 3
    // (== N exact, never a >= threshold). At container scale this is the correctness
    // pin; the production max_scan_tuples ceiling reproduction is conexus xr7.8.9. This
    // does NOT prove a selectivity-strategy switch fires — the inlinable SQL functions
    // have no switch; the planner picks filter-first vs HNSW by cost. The production
    // max_scan_tuples defense (tuned hnsw.iterative_scan at the repoint) is tracked
    // separately. This test pins exact-recall correctness only.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void narrowCollection_exactRecall_returnsAllN() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // CONTROL: the collection holds exactly 3 chunks.
            assertThat(rawChunkCount(su, 1024, TENANT_A, COLL_NARROW))
                .as("CONTROL: COLL_NARROW must hold exactly 3 chunks").isEqualTo(3);

            List<String> got = callMeta(su, 1024, COLL_NARROW, null, null, null, null, 10);
            assertThat(got)
                .as("narrow collection (size 3 < ef_search), distant query vector: the "
                    + "combined query must return EXACTLY 3 rows (== N) — a silent "
                    + "under-return at the scan ceiling is the precise Finding-5b hazard")
                .hasSize(3)
                .containsExactlyInAnyOrder("n1", "n2", "n3");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 5 — parity vs the stitched path the combined query retires
    //
    // EXPECTED RED: function absent until catalog-006.
    //
    // The stitched path = (1) catalog filter to candidate doc tumblers, (2) map to
    // their chashes via the manifest, (3) vector-rank those chashes. Computed here as
    // a single SQL oracle (catalog filter + raw ORDER BY embedding <=> q over the
    // candidate chashes). The combined query must equal it EXACTLY, ordering included.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void parity_metadataScoped_equalsStitchedOracle() throws Exception {
        try (Connection su = pg.createConnection("")) {
            List<String> stitched = stitchedMetaOracle(su, 1024, COLL_M, "paper", "alice");
            // CONTROL: the oracle itself is non-empty and discriminating.
            assertThat(stitched)
                .as("CONTROL: stitched oracle for type=paper,author=alice is [m1,m4]")
                .containsExactly("m1", "m4");
            assertThat(callMeta(su, 1024, COLL_M, "paper", "alice", null, null, 10))
                .as("combined metadata query must return EXACTLY the stitched path's "
                    + "result (the app-side catalog-dance it retires), ordering included")
                .containsExactlyElementsOf(stitched);
        }
    }

    @Test @Order(51)
    void parity_topicScoped_equalsStitchedOracle() throws Exception {
        try (Connection su = pg.createConnection("")) {
            List<String> stitched = stitchedTopicOracle(su, 1024, COLL_T, TOPIC_VEC);
            assertThat(stitched)
                .as("CONTROL: stitched topic oracle for 'Vector Search' is chunks [t1,t2]")
                .containsExactly(chashOf("t1"), chashOf("t2"));
            assertThat(callTopic(su, 1024, COLL_T, TOPIC_VEC, 10))
                .as("combined topic query must return EXACTLY the stitched path's result")
                .containsExactlyElementsOf(stitched);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 6 — tombstone filtering, both directions (non-vacuous)
    //
    // EXPECTED RED: function absent until catalog-006.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void metadata_tombstone_dropsAndReturns() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Direction 1: m2 present.
            assertThat(callMeta(su, 1024, COLL_M, "paper", "bob", null, null, 10))
                .as("baseline: type=paper,author=bob → [m2]").containsExactly("m2");

            // Direction 2: tombstone m2's doc → excluded.
            setDeleted(su, TENANT_A, "m2", true);
            assertThat(callMeta(su, 1024, COLL_M, "paper", "bob", null, null, 10))
                .as("tombstoned doc m2 must drop out of combined results").isEmpty();

            // Direction 3: restore → returns.
            setDeleted(su, TENANT_A, "m2", false);
            assertThat(callMeta(su, 1024, COLL_M, "paper", "bob", null, null, 10))
                .as("restored doc m2 must return").containsExactly("m2");
        }
    }

    @Test @Order(61)
    void topic_tombstone_dropsAndReturns() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThat(callTopic(su, 1024, COLL_T, TOPIC_VEC, 10))
                .as("baseline topic 'Vector Search' → chunks [t1,t2]")
                .containsExactly(chashOf("t1"), chashOf("t2"));

            setDeleted(su, TENANT_A, "t2", true);
            assertThat(callTopic(su, 1024, COLL_T, TOPIC_VEC, 10))
                .as("tombstoned doc t2 must drop from topic-scoped results "
                    + "(its chunk's only manifest row points to a tombstoned doc)")
                .containsExactly(chashOf("t1"));

            setDeleted(su, TENANT_A, "t2", false);
            assertThat(callTopic(su, 1024, COLL_T, TOPIC_VEC, 10))
                .as("restored doc t2 must return")
                .containsExactly(chashOf("t1"), chashOf("t2"));
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 7 — RLS isolation: the functions are SECURITY INVOKER (caller RLS)
    //
    // EXPECTED RED: function absent until catalog-006.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(70)
    void rls_metadataScoped_tenantSeesOnlyOwnRows() throws Exception {
        // CONTROL: superuser (BYPASSRLS) sees tenant-B's row through the function —
        // proves foreign rows exist underneath, so the svc assertions are non-vacuous.
        try (Connection su = pg.createConnection("")) {
            assertThat(callMeta(su, 1024, COLL_B, "paper", null, null, null, 10))
                .as("CONTROL: superuser sees tenant-B papers b1,b2 (foreign rows exist, "
                    + "so the svc-role assertions below are non-vacuous)")
                .containsExactlyInAnyOrder("b1", "b2");
        }
        // svc + GUC=A: cannot see tenant-B's collection at all.
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            assertThat(callMeta(svc, 1024, COLL_B, "paper", null, null, null, 10))
                .as("svc GUC=A must see ZERO tenant-B rows (function is SECURITY "
                    + "INVOKER — caller RLS on nexus.chunks/catalog applies)").isEmpty();
            assertThat(callMeta(svc, 1024, COLL_M, "paper", null, null, null, 10))
                .as("svc GUC=A must still see its OWN tenant-A papers")
                .containsExactly("m1", "m2", "m4", "m5");
        }
        // svc + no GUC: nothing.
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, null, false);
            assertThat(callMeta(svc, 1024, COLL_M, "paper", null, null, null, 10))
                .as("svc with no nexus.tenant GUC sees nothing (RLS matches NULL)")
                .isEmpty();
        }
    }

    @Test @Order(71)
    void rls_topicScoped_tenantSeesOnlyOwnRows() throws Exception {
        // CONTROL: superuser (BYPASSRLS) sees tenant-B's topic chunk — proves the
        // foreign row exists, so the svc assertion below is non-vacuous.
        try (Connection su = pg.createConnection("")) {
            assertThat(callTopic(su, 1024, COLL_B, TOPIC_VEC, 10))
                .as("CONTROL: superuser sees tenant-B topic chunk b2 (foreign rows exist)")
                .containsExactly(chashOf("b2"));
        }
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            assertThat(callTopic(svc, 1024, COLL_B, TOPIC_VEC, 10))
                .as("svc GUC=A must see ZERO tenant-B topic rows (SECURITY INVOKER — "
                    + "caller RLS on nexus.chunks applies)").isEmpty();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 8 — per-dim dispatch (the functions exist and behave on nexus.chunks dim=384)
    //
    // EXPECTED RED: 384 functions absent until catalog-006.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(80)
    void perDim_metadataScoped_384_behavesIdentically() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callMeta(su, 384, COLL_M_384, "paper", null, null, null, 10))
                .as("search_metadata_scoped_384 must exist and behave identically — "
                    + "only m384a is type=paper")
                .containsExactly("m384a");
        }
    }

    @Test @Order(81)
    void perDim_topicScoped_384_behavesIdentically() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callTopic(su, 384, COLL_T_384, TOPIC_VEC, 10))
                .as("search_topic_scoped_384 must exist and behave identically (chunk-level)")
                .containsExactly(chashOf("t384a"));
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 9 — catalog-008 (nexus-889ff): subtree + where + author ILIKE + chash
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(90)
    void author_ilikeSubstring_caseInsensitive() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // query() uses author.lower() IN (substring) — NOT exact. A substring of the
            // author and a different case must still match.
            assertThat(callMeta(su, 1024, COLL_M, null, "lic", null, null, 10))
                .as("author='lic' (substring of 'alice') → {m1,m3,m4} by distance — ILIKE "
                    + "substring, not exact equality (exact would return nothing)")
                .containsExactly("m1", "m3", "m4");
            assertThat(callMeta(su, 1024, COLL_M, null, "ALICE", null, null, 10))
                .as("author='ALICE' (wrong case) → same {m1,m3,m4} — ILIKE is case-insensitive")
                .containsExactly("m1", "m3", "m4");
        }
    }

    @Test @Order(91)
    void subtree_matchesDescendants_notRootSiblingsOrStringPrefix() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // ROOT-EXCLUSIVE, matching query()'s cat.descendants() (LIKE 'prefix.%'): the
            // subtree root 1.2 itself is NOT returned — only descendants. Including the root
            // would diverge from query() and silently change the repoint's results.
            assertThat(callMetaFull(su, 1024, COLL_SUB, null, null, null, null, "1.2", null, 10))
                .as("subtree='1.2' → descendants {1.2.3,1.2.4} ranked by distance; root 1.2 "
                    + "EXCLUDED (matches cat.descendants), sibling 1.3.1 and string-prefix-trap "
                    + "1.20 EXCLUDED")
                .containsExactly("1.2.3", "1.2.4");
        }
    }

    @Test @Order(92)
    void where_jsonbContainment_filtersChunkMetadata() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callMetaFull(su, 1024, COLL_WHERE, null, null, null, null, null,
                                    "{\"lang\": \"java\"}", 10))
                .as("where={lang:java} → {w1,w3} by distance (0.0,0.4); w2 (py) excluded")
                .containsExactly("w1", "w3");
            assertThat(callMetaFull(su, 1024, COLL_WHERE, null, null, null, null, null,
                                    "{\"lang\": \"py\"}", 10))
                .as("where={lang:py} → {w2} only")
                .containsExactly("w2");
        }
    }

    @Test @Order(93)
    void chashColumn_isMatchedChunkChash() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT id, chash FROM nexus.search_metadata_scoped_1024(" +
                queryVecLiteral(1024) + ", ARRAY['" + COLL_M + "']::text[], 'paper', " +
                "NULL, NULL::int, NULL, NULL::text, NULL::jsonb, 10) ORDER BY distance");
            int seen = 0;
            while (rs.next()) {
                assertThat(rs.getString("chash"))
                    .as("chash column must be the matched chunk's chash for doc " + rs.getString("id"))
                    .isEqualTo(validChash(rs.getString("id")));
                seen++;
            }
            assertThat(seen).as("type=paper → m1,m2,m4,m5").isEqualTo(4);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 10 — aspect-scoped (RDR-156 Decision 5, bead nexus-ubnwk): combined
    // vector + document_aspects predicate query.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(100)
    void aspect_queryVectorIsFirstArgument_signaturePinned() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT pg_catalog.pg_get_function_arguments(p.oid) AS args " +
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace " +
                " WHERE n.nspname = 'nexus' AND p.proname = 'search_aspect_scoped_1024'");
            assertThat(rs.next())
                .as("nexus.search_aspect_scoped_1024 must exist (vectors-008)").isTrue();
            String args = rs.getString("args");
            assertThat(args)
                .as("aspect-scoped query vector must be the FIRST argument, typed `vector`")
                .startsWith("p_query vector");
            assertThat(args)
                .as("aspect-scoped signature: collections[], field, pattern, "
                    + "min_confidence, where, n")
                .contains("p_collections text[]")
                .contains("p_field text")
                .contains("p_pattern text")
                .contains("p_min_confidence double precision")
                .contains("p_where jsonb")
                .contains("p_n integer");
        }
    }

    @Test @Order(101)
    void aspect_fieldPatternFilter_exactRankedByDistance_excludesNoRow() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callAspect(su, 1024, COLL_ASPECT, "proposed_method", "gradient", null, 10))
                .as("field=proposed_method,pattern=gradient → {asp1,asp2,asp4} ranked by "
                    + "distance (0.0,0.2,1.0); asp3 (no document_aspects row) is excluded — "
                    + "the INNER JOIN to document_aspects")
                .containsExactly("asp1", "asp2", "asp4");
        }
    }

    @Test @Order(102)
    void aspect_minConfidenceFilter_exact() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callAspect(su, 1024, COLL_ASPECT, null, null, 0.5, 10))
                .as("min_confidence=0.5 → {asp1,asp4} (confidence 0.9); asp2 (0.2) excluded; "
                    + "asp3 (no row) excluded regardless")
                .containsExactly("asp1", "asp4");
        }
    }

    @Test @Order(103)
    void aspect_unknownField_returnsEmpty() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callAspect(su, 1024, COLL_ASPECT, "no_such_field", "gradient", null, 10))
                .as("an unrecognized p_field falls through the SQL CASE to NULL — "
                    + "NULL ILIKE ... is NULL (never TRUE) — so it matches NOTHING, "
                    + "never every row")
                .isEmpty();
        }
    }

    @Test @Order(104)
    void aspectDocId_nowRequired_insertWithNullDocIdRejected() throws Exception {
        // hygiene-001-1 (nexus-tk070.p6a follow-on) made document_aspects.doc_id
        // NOT NULL: the old asp5 fixture (a row with fully-matching content but
        // doc_id IS NULL, proving the join's a.doc_id = d.tumbler condition
        // excludes it) can no longer be constructed at all -- inverted here into
        // the stronger guarantee that the INSERT itself is rejected, not merely
        // that a NULL-doc_id row never surfaces in a query.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThatThrownBy(() ->
                insertAspectRow(su, TENANT_A, COLL_ASPECT, "asp-null-doc-id-rejected", null,
                                "Uses gradient descent", 0.9))
                .as("document_aspects.doc_id must reject NULL (hygiene-001-1 SET NOT NULL)")
                .isInstanceOf(Exception.class)
                .hasMessageContaining("null value in column \"doc_id\"");
        }
    }

    @Test @Order(105)
    void aspect_tombstone_dropsAndReturns() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThat(callAspect(su, 1024, COLL_ASPECT, "proposed_method", "gradient", null, 10))
                .as("baseline: {asp1,asp2,asp4}")
                .containsExactly("asp1", "asp2", "asp4");

            setDeleted(su, TENANT_A, "asp2", true);
            assertThat(callAspect(su, 1024, COLL_ASPECT, "proposed_method", "gradient", null, 10))
                .as("tombstoned doc asp2 must drop out of aspect-scoped results")
                .containsExactly("asp1", "asp4");

            setDeleted(su, TENANT_A, "asp2", false);
            assertThat(callAspect(su, 1024, COLL_ASPECT, "proposed_method", "gradient", null, 10))
                .as("restored doc asp2 must return")
                .containsExactly("asp1", "asp2", "asp4");
        }
    }

    @Test @Order(106)
    void aspect_narrowCollection_exactRecall_returnsAllN() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(rawChunkCount(su, 1024, TENANT_A, COLL_ASPECT_NARROW))
                .as("CONTROL: COLL_ASPECT_NARROW must hold exactly 3 chunks").isEqualTo(3);

            List<String> got = callAspect(su, 1024, COLL_ASPECT_NARROW,
                                          "proposed_method", "gradient", null, 10);
            assertThat(got)
                .as("narrow collection (size 3 < ef_search), distant query vector, "
                    + "non-selective aspect predicate (all 3 match): the combined query "
                    + "must return EXACTLY 3 rows (== N) — a silent under-return at the "
                    + "scan ceiling is the precise Finding-5b hazard, now for the "
                    + "aspects join too")
                .hasSize(3)
                .containsExactlyInAnyOrder("asn1", "asn2", "asn3");
        }
    }

    @Test @Order(107)
    void explain_aspectScoped_usesHnswIndex_notSeqScan() throws Exception {
        // Large, NON-selectively-filtered fixture (every doc matches "gradient"): the
        // vector ANN is the driving access path, so a correct impl uses the HNSW
        // index even through the added document_aspects join.
        String plan = explainAspectPlan(1024, COLL_ASPECT_EXPLAIN, "proposed_method", "gradient");
        assertThat(plan)
            .as("combined aspect-scoped query must use the HNSW index "
                + "idx_chunks_embedding_1024 for the ANN ordering. Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_1024");
        assertThat(plan)
            .as("the document_aspects join must NOT defeat the index into a Seq Scan "
                + "on nexus.chunks. Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks");
        assertThat(plan)
            .as("a Function Scan node means the function is not inlinable (plpgsql) — "
                + "vectors-008 must use an inlinable LANGUAGE sql function. Plan was:%n%s",
                plan)
            .doesNotContain("Function Scan");
    }

    @Test @Order(108)
    void parity_aspectScoped_equalsTwoStepOracle_whenNonSelective() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // topK covers every fixture chunk in COLL_ASPECT (4) — no truncation loss,
            // so the two-step path and the combined query must agree exactly.
            List<String> twoStep = twoStepAspectOracle(su, 1024, COLL_ASPECT, "gradient", 10);
            assertThat(twoStep)
                .as("CONTROL: two-step oracle (top-10, non-truncating) for pattern="
                    + "gradient is [asp1,asp2,asp4]")
                .containsExactly("asp1", "asp2", "asp4");
            assertThat(callAspect(su, 1024, COLL_ASPECT, "proposed_method", "gradient", null, 10))
                .as("combined aspect-scoped query must return EXACTLY the two-step "
                    + "oracle's result when the vector window is non-truncating "
                    + "(ordering included)")
                .containsExactlyElementsOf(twoStep);
        }
    }

    @Test @Order(109)
    void recall_aspectScoped_strictlySupersetsTwoStepOracle_whenSelective() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // Two-step: vector top-2 (nearest are asp1 dist=0.0, asp2 dist=0.2) THEN
            // filter by pattern=descent — asp2 says "boosting", fails the text test —
            // two-step result = [asp1] only. asp4 (dist=1.0, "descent") is OUTSIDE the
            // top-2 vector window and is never even considered.
            List<String> twoStep = twoStepAspectOracle(su, 1024, COLL_ASPECT, "descent", 2);
            assertThat(twoStep)
                .as("CONTROL: two-step oracle (top-2, truncating) for pattern=descent "
                    + "is [asp1] only — asp4 is outside the top-2 vector window")
                .containsExactly("asp1");

            List<String> combined = callAspect(su, 1024, COLL_ASPECT, "proposed_method",
                                               "descent", null, 10);
            assertThat(combined)
                .as("combined aspect-scoped query applies the predicate BEFORE the "
                    + "vector limit — it must find asp4 too, which the two-step path "
                    + "missed entirely")
                .containsExactly("asp1", "asp4");
            assertThat(combined)
                .as("combined result must be a STRICT superset of the two-step oracle "
                    + "(recall win exactly when the predicate is selective and the "
                    + "vector window is small)")
                .containsAll(twoStep)
                .hasSizeGreaterThan(twoStep.size());
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // CALL HELPERS (combined-query functions)
    // ══════════════════════════════════════════════════════════════════════════

    /** Invoke search_metadata_scoped_&lt;dim&gt;; returns ids in returned order. */
    private List<String> callMeta(Connection conn, int dim, String collection,
                                  String contentType, String author, Integer year,
                                  String corpus, int n) throws Exception {
        return callMetaFull(conn, dim, collection, contentType, author, year, corpus,
                            null, null, n);
    }

    /** Full metadata-scoped call with subtree + where (catalog-008). whereJson is a raw
     *  JSON object literal (e.g. {@code "{\"tags\":\"arch\"}"}) or null. */
    private List<String> callMetaFull(Connection conn, int dim, String collection,
                                      String contentType, String author, Integer year,
                                      String corpus, String subtree, String whereJson,
                                      int n) throws Exception {
        String sql =
            "SELECT id FROM nexus.search_metadata_scoped_" + dim + "(" +
            queryVecLiteral(dim) + ", " +
            "ARRAY['" + collection + "']::text[], " +
            sqlText(contentType) + ", " +
            sqlText(author) + ", " +
            (year == null ? "NULL::int" : year.toString()) + ", " +
            sqlText(corpus) + ", " +
            sqlText(subtree) + ", " +
            (whereJson == null ? "NULL::jsonb" : "'" + whereJson.replace("'", "''") + "'::jsonb") + ", " +
            n + ")";
        return runIds(conn, sql);
    }

    /** Invoke search_topic_scoped_&lt;dim&gt;; returns ids in returned order. */
    private List<String> callTopic(Connection conn, int dim, String collection,
                                   String topicLabel, int n) throws Exception {
        String sql =
            "SELECT id FROM nexus.search_topic_scoped_" + dim + "(" +
            queryVecLiteral(dim) + ", " +
            sqlText(topicLabel) + ", " +
            sqlText(collection) + ", " +
            n + ")";
        return runIds(conn, sql);
    }

    /** EXPLAIN (no ANALYZE) the metadata function with seqscan disabled. */
    private String explainMetaPlan(int dim, String collection, String contentType)
            throws Exception {
        String inner =
            "SELECT id FROM nexus.search_metadata_scoped_" + dim + "(" +
            queryVecLiteral(dim) + ", ARRAY['" + collection + "']::text[], " +
            sqlText(contentType) + ", NULL, NULL::int, NULL, NULL::text, NULL::jsonb, 10)";
        return explain(inner);
    }

    /** Invoke search_aspect_scoped_&lt;dim&gt;; returns ids in returned order. */
    private List<String> callAspect(Connection conn, int dim, String collection,
                                    String field, String pattern, Double minConfidence,
                                    int n) throws Exception {
        String sql =
            "SELECT id FROM nexus.search_aspect_scoped_" + dim + "(" +
            queryVecLiteral(dim) + ", " +
            "ARRAY['" + collection + "']::text[], " +
            sqlText(field) + ", " +
            sqlText(pattern) + ", " +
            (minConfidence == null ? "NULL::float8" : minConfidence.toString()) + ", " +
            "NULL::jsonb, " +
            n + ")";
        return runIds(conn, sql);
    }

    /** EXPLAIN (no ANALYZE) the aspect-scoped function with seqscan disabled. */
    private String explainAspectPlan(int dim, String collection, String field, String pattern)
            throws Exception {
        String inner =
            "SELECT id FROM nexus.search_aspect_scoped_" + dim + "(" +
            queryVecLiteral(dim) + ", ARRAY['" + collection + "']::text[], " +
            sqlText(field) + ", " + sqlText(pattern) + ", NULL::float8, NULL::jsonb, 10)";
        return explain(inner);
    }

    /** EXPLAIN (no ANALYZE) the topic function with seqscan disabled. */
    private String explainTopicPlan(int dim, String collection, String topicLabel)
            throws Exception {
        String inner =
            "SELECT id FROM nexus.search_topic_scoped_" + dim + "(" +
            queryVecLiteral(dim) + ", " + sqlText(topicLabel) + ", " +
            sqlText(collection) + ", 10)";
        return explain(inner);
    }

    private String explain(String inner) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Penalize every NON-index way to satisfy ORDER BY embedding <=> <const>
            // LIMIT. At unit scale the cost crossover that makes HNSW the *natural*
            // plan (Finding 5: ~2ms vs 340ms at 98k rows) is not reproducible, so we
            // assert REACHABILITY: with sort/seq/bitmap/hash penalized, the cheapest
            // remaining plan is the HNSW Index Scan through the join — and a join-sourced
            // vector (the Finding-5a regression) could NOT use the index even then, so
            // the assertion still distinguishes a correct impl from the regression.
            // NOTE: enable_nestloop stays ON — the HNSW-ordered chunk scan joins to the
            // catalog/topic tables via nested loop; disabling it would defeat the very
            // plan this test asserts.
            for (String guc : List.of("enable_seqscan", "enable_bitmapscan",
                    "enable_sort", "enable_hashjoin")) {
                su.createStatement().execute("SET " + guc + " = off");
            }
            ResultSet rs = su.createStatement().executeQuery("EXPLAIN " + inner);
            StringBuilder sb = new StringBuilder();
            while (rs.next()) sb.append(rs.getString(1)).append('\n');
            return sb.toString();
        }
    }

    private static List<String> runIds(Connection conn, String sql) throws Exception {
        List<String> out = new ArrayList<>();
        try (var st = conn.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            while (rs.next()) out.add(rs.getString(1));
        }
        return out;
    }

    // ══════════════════════════════════════════════════════════════════════════
    // STITCHED-PATH ORACLES (the app-side dance the combined query retires)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Stitched metadata path: catalog filter → candidate chashes → vector rank.
     * One SQL statement, but structured as the explicit join the Python query tool
     * performs across stores today. Tombstone-filtered (deleted_at IS NULL).
     */
    private List<String> stitchedMetaOracle(Connection conn, int dim, String collection,
                                            String contentType, String author)
            throws Exception {
        String sql =
            "SELECT d.tumbler AS id " +
            "  FROM nexus.catalog_documents d " +
            "  JOIN nexus.catalog_document_chunks m " +
            "    ON m.tenant_id = d.tenant_id AND m.doc_id = d.tumbler " +
            "  JOIN " + DimTables.CHUNKS_TABLE_NAME + " c " +
            "    ON c.tenant_id = m.tenant_id AND c.collection = m.collection " +
            "   AND c.chash = m.chash " +
            " WHERE m.collection = '" + collection + "' " +
            "   AND d.deleted_at IS NULL " +
            (contentType == null ? "" : "   AND d.content_type = " + sqlText(contentType) + " ") +
            (author == null ? "" : "   AND d.author ILIKE '%' || " + sqlText(author) + " || '%' ") +
            " ORDER BY c." + DimTables.embeddingColumn(dim) + " <=> " + queryVecLiteral(dim) + " ASC";
        return runIds(conn, sql);
    }

    /** Stitched topic path: topics ⋈ topic_assignments ⋈ manifest ⋈ chunks, vector rank. */
    private List<String> stitchedTopicOracle(Connection conn, int dim, String collection,
                                             String topicLabel) throws Exception {
        String sql =
            "SELECT encode(c.chash, 'hex') AS id " +
            "  FROM " + DimTables.CHUNKS_TABLE_NAME + " c " +
            "  JOIN nexus.topic_assignments ta " +
            // RDR-194 P3c: doc_id is bytea now -- direct equality, no encode(),
            // matching search_topic_scoped_<dim>'s own post-P3c join predicate.
            "    ON ta.tenant_id = c.tenant_id AND ta.doc_id = c.chash " +
            "  JOIN nexus.topics t " +
            "    ON t.tenant_id = ta.tenant_id AND t.id = ta.topic_id " +
            " WHERE c.collection = '" + collection + "' " +
            "   AND t.collection = '" + collection + "' " +
            "   AND t.label = " + sqlText(topicLabel) + " " +
            "   AND (NOT EXISTS (SELECT 1 FROM nexus.catalog_document_chunks m " +
            "                     WHERE m.tenant_id = c.tenant_id AND m.chash = c.chash) " +
            "        OR EXISTS (SELECT 1 FROM nexus.catalog_document_chunks m " +
            "                     JOIN nexus.catalog_documents d " +
            "                       ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id " +
            "                    WHERE m.tenant_id = c.tenant_id AND m.chash = c.chash " +
            "                      AND d.deleted_at IS NULL)) " +
            " ORDER BY c." + DimTables.embeddingColumn(dim) + " <=> " + queryVecLiteral(dim) + " ASC";
        return runIds(conn, sql);
    }

    /**
     * Two-step oracle the aspect-scoped function retires: (1) raw vector rank, TOP
     * {@code topK} only (models the {@code search} tool's top-N truncation), then
     * (2) app-side filter of THOSE candidates by their document_aspects row's
     * proposed_method (ILIKE pattern) — a candidate outside the top-{@code topK}
     * vector window is never considered, regardless of whether it matches the
     * pattern. This is the recall hazard: a selective aspect predicate cannot widen
     * a vector window that already truncated it away.
     */
    private List<String> twoStepAspectOracle(Connection conn, int dim, String collection,
                                             String pattern, int topK) throws Exception {
        String topKSql =
            "SELECT d.tumbler AS id " +
            "  FROM nexus.catalog_documents d " +
            "  JOIN nexus.catalog_document_chunks m " +
            "    ON m.tenant_id = d.tenant_id AND m.doc_id = d.tumbler " +
            "  JOIN " + DimTables.CHUNKS_TABLE_NAME + " c " +
            "    ON c.tenant_id = m.tenant_id AND c.collection = m.collection " +
            "   AND c.chash = m.chash " +
            " WHERE m.collection = '" + collection + "' " +
            "   AND d.deleted_at IS NULL " +
            " ORDER BY c." + DimTables.embeddingColumn(dim) + " <=> " + queryVecLiteral(dim) + " ASC " +
            " LIMIT " + topK;
        List<String> topKIds = runIds(conn, topKSql);
        List<String> matched = new ArrayList<>();
        for (String id : topKIds) {
            String sql =
                "SELECT 1 FROM nexus.document_aspects a " +
                " WHERE a.tenant_id = '" + TENANT_A + "' AND a.doc_id = '" + id + "' " +
                "   AND a.proposed_method ILIKE '%" + pattern + "%'";
            try (var st = conn.createStatement(); ResultSet rs = st.executeQuery(sql)) {
                if (rs.next()) matched.add(id);
            }
        }
        return matched;
    }

    // ══════════════════════════════════════════════════════════════════════════
    // FIXTURE HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Seed a metadata doc: catalog_documents row (with metadata) + manifest row + one
     * chunk whose embedding is the 2-D unit direction (x,y). doc tumbler == chunk id
     * (== the value the functions return).
     */
    private void seedMetaDoc(Connection su, int dim, String collection, String tumbler,
                             String contentType, String author, int year, String corpus,
                             double x, double y) throws Exception {
        String tenant = tenantFor(collection);
        String chash = validChash(tumbler);
        insertCatalogDocumentFull(su, tenant, tumbler, collection, contentType, author,
                                  year, corpus);
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires the
        // chunk row to land BEFORE the manifest row (previously order-independent).
        insertChunk(su, dim, tenant, collection, chash, tumbler, x, y);
        insertManifestRow(su, tenant, tumbler, 0, chash, collection);
    }

    /**
     * Seed a 1024-dim doc whose CHUNK carries {@code metaJson} JSONB metadata — for the
     * catalog-008 {@code where} containment gate (c.metadata @&gt; p_where).
     */
    private void seedMetaDocMeta(Connection su, String collection, String tumbler,
                                 double x, double y, String metaJson) throws Exception {
        String tenant = tenantFor(collection);
        String chash = validChash(tumbler);
        insertCatalogDocumentFull(su, tenant, tumbler, collection, "paper", "wa", 2024, "research");
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires the
        // chunk row to land BEFORE the manifest row (previously order-independent).
        su.createStatement().execute(
            "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ", metadata)"
            + " VALUES ('" + tenant + "', '" + collection + "', decode('" + chash + "', 'hex'), '" + tumbler + "', "
            + vec2(1024, x, y) + "::vector, '" + metaJson.replace("'", "''") + "'::jsonb)"
            + " ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
        insertManifestRow(su, tenant, tumbler, 0, chash, collection);
    }

    /**
     * Seed a topic doc: catalog_documents row + topic_assignments row + manifest +
     * chunk. Minimal metadata (the topic path filters on topic membership, not fields).
     */
    private void seedTopicDoc(Connection su, int dim, String collection, String tumbler,
                              long topicId, double x, double y) throws Exception {
        String chash = validChash(tumbler);
        String tenant = tenantFor(collection);
        insertCatalogDocumentFull(su, tenant, tumbler, collection,
                                  "paper", "topicauthor", 2024, "research");
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires the
        // chunk row to land BEFORE the manifest row (previously order-independent).
        insertChunk(su, dim, tenant, collection, chash, tumbler, x, y);
        insertManifestRow(su, tenant, tumbler, 0, chash, collection);
        // Topic membership is CHUNK-level: topic_assignments.doc_id is the chunk chash
        // (nexus-sa14p), NOT the document tumbler.
        insertTopicAssignment(su, tenant, chash, topicId, collection);
    }

    /** COLL_B belongs to tenant B; everything else to tenant A. */
    private static String tenantFor(String collection) {
        return COLL_B.equals(collection) ? TENANT_B : TENANT_A;
    }

    private static void insertCollection(Connection su, String tenantId, String name)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" +
            tenantId + "', '" + name + "') ON CONFLICT (tenant_id, name) DO NOTHING");
    }

    private static void insertCatalogDocumentFull(Connection su, String tenantId, String tumbler,
                                                  String collection, String contentType,
                                                  String author, int year, String corpus)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents " +
            "  (tenant_id, tumbler, title, author, year, content_type, corpus, physical_collection) " +
            "VALUES ('" + tenantId + "', '" + tumbler + "', 'Doc " + tumbler + "', " +
            sqlText(author) + ", " + year + ", " + sqlText(contentType) + ", " +
            sqlText(corpus) + ", '" + collection + "') " +
            "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
    }

    private static void insertManifestRow(Connection su, String tenantId, String docId,
                                          int position, String chash, String collection)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks " +
            "  (tenant_id, doc_id, position, chash, collection) " +
            "VALUES ('" + tenantId + "', '" + docId + "', " + position + ", decode('" + chash + "', 'hex'), '" +
            collection + "') ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
    }

    /** Insert a topic; returns its generated id. */
    private static long insertTopic(Connection su, String tenantId, String label,
                                    String collection) throws Exception {
        try (var st = su.createStatement();
             ResultSet rs = st.executeQuery(
                "INSERT INTO nexus.topics (tenant_id, label, collection, created_at) " +
                "VALUES ('" + tenantId + "', " + sqlText(label) + ", '" + collection + "', " +
                "'2026-01-01T00:00:00+00'::timestamptz) RETURNING id")) {
            rs.next();
            return rs.getLong(1);
        }
    }

    private static void insertTopicAssignment(Connection su, String tenantId, String docId,
                                              long topicId, String collection) throws Exception {
        // RDR-194 P3c: topic_assignments.doc_id is bytea now — decode('hex').
        su.createStatement().execute(
            "INSERT INTO nexus.topic_assignments " +
            "  (tenant_id, doc_id, topic_id, source_collection, assigned_at) " +
            "VALUES ('" + tenantId + "', decode('" + docId + "', 'hex'), " + topicId + ", '" + collection + "', " +
            "'2026-01-01T00:00:00+00'::timestamptz) " +
            "ON CONFLICT (tenant_id, doc_id, topic_id) DO NOTHING");
    }

    /** Insert a nexus.chunks row (RDR-191 unified; formerly chunks_&lt;dim&gt;) with a 2-D unit direction embedding. */
    private static void insertChunk(Connection su, int dim, String tenantId, String collection,
                                    String chash, String chunkText, double x, double y)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME +
            " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(dim) + ") VALUES ('" +
            tenantId + "', '" + collection + "', decode('" + chash + "', 'hex'), '" +
            chunkText.replace("'", "''") + "', " + vec2(dim, x, y) + "::vector) " +
            "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }

    private static void setDeleted(Connection su, String tenantId, String tumbler, boolean deleted)
            throws Exception {
        su.createStatement().execute(
            "UPDATE nexus.catalog_documents SET deleted_at = " +
            (deleted ? "now()" : "NULL") +
            " WHERE tenant_id = '" + tenantId + "' AND tumbler = '" + tumbler + "'");
    }

    private static long rawChunkCount(Connection conn, int dim, String tenantId,
                                      String collection) throws Exception {
        try (var st = conn.createStatement();
             ResultSet rs = st.executeQuery(
                "SELECT count(*) FROM " + DimTables.CHUNKS_TABLE_NAME +
                " WHERE tenant_id = '" + tenantId + "' AND collection = '" + collection + "'" +
                " AND " + DimTables.embeddingColumn(dim) + " IS NOT NULL")) {
            rs.next();
            return rs.getLong(1);
        }
    }

    // ── value helpers ────────────────────────────────────────────────────────

    /** The probe query vector literal: 2-D direction (1,0) padded to dim, ::vector. */
    private static String queryVecLiteral(int dim) {
        return vec2(dim, 1.0, 0.0) + "::vector";
    }

    /** A length-{@code dim} pgvector literal with first two components (x,y), rest 0. */
    private static String vec2(int dim, double x, double y) {
        StringBuilder sb = new StringBuilder("'[");
        sb.append(fmt(x)).append(',').append(fmt(y));
        for (int i = 2; i < dim; i++) sb.append(",0");
        return sb.append("]'").toString();
    }

    private static String fmt(double v) {
        if (v == Math.rint(v)) return Integer.toString((int) v);
        return Double.toString(v);
    }

    /** SQL string literal or NULL token for a nullable text value. */
    private static String sqlText(String v) {
        return v == null ? "NULL" : "'" + v.replace("'", "''") + "'";
    }

    /**
     * Full 64-lowercase-hex chash deterministically derived from seed (RDR-180:
     * the full sha256 digest is the canonical chash — the pre-flip [:32] half-
     * digest truncation is retired). SHA-256-based so distinct tumblers get
     * distinct chashes — a naive char-substitution derivation collapses e.g.
     * "m2" and "t2" to the same value, and because chunks are content-addressed
     * (the live-chunk predicate is chash-scoped, NOT collection-scoped), a
     * collision lets a live doc in one collection keep a tombstoned doc's
     * shared chash alive in another.
     */
    private static String validChash(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    /**
     * The chunk chash for a fixture tumbler — same derivation seedMetaDoc/seedTopicDoc
     * use. Topic-scoped search is chunk-level (topic_assignments.doc_id is a chash,
     * nexus-sa14p), so topic assertions match on chash, not tumbler.
     */
    private static String chashOf(String tumbler) {
        return validChash(tumbler);
    }
}
