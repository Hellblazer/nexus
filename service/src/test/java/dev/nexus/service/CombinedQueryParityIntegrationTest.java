// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.OnnxEmbedder;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-156 P4.1b (bead nexus-qrm3s): corpus-scale function-vs-stitched-path parity for
 * the combined-query primitives (Finding 4).
 *
 * <p>The combined-query SQL functions ({@code nexus.search_metadata_scoped_<dim>} /
 * {@code search_topic_scoped_<dim>}, catalog-006) collapse the app-side catalog dance
 * into one statement. This suite is the cross-layer parity seam the RDR-155 P3.E
 * DualRunHarness was meant to host (Finding 4) — built as a focused sibling because the
 * DualRunHarness's Chroma baseline leg is unrelated to combined-query parity (and
 * currently can't embed the {@code all-minilm-l6-v2} collection token with the
 * minilm-only router; tracked separately). Combined-query parity is pgvector-only:
 * function vs app-stitch, both on the same store.
 *
 * <p><strong>What it proves:</strong> for each probe query, the SINGLE combined-function
 * call returns the SAME top-k documents/chunks as the multi-step app stitch it retires —
 * (1) catalog/topic membership lookup, (2) per-collection vector search via the real
 * {@link PgVectorRepository#search}, (3) app-side filter + re-rank. The stitch runs
 * through the production repository search path, so the comparison is genuinely
 * function-vs-stitch, not function-vs-itself (the gap the unit CombinedQueryParityTest's
 * in-SQL oracle left open, per the joesk review).
 *
 * <p><strong>Scale boundary:</strong> this validates function-vs-stitch CORRECTNESS at
 * fixture scale (default 60 docs; {@code -Dnx.cqparity.size} / {@code -Dnx.cqparity.k}
 * scale it up). At this scale pgvector seq-scans — no HNSW index forms — so the
 * narrow/distant-filter HNSW under-return recall property (Finding 5b, {@code
 * hnsw.iterative_scan} / {@code max_scan_tuples}) is NOT exercised here; that production-
 * scale recall gate stays conexus xr7.8.9 (and nexus-0zcn9). What this proves: the
 * single combined-function call returns the same documents/chunks as the app stitch,
 * including correct content_type/topic filtering and tombstone exclusion.
 *
 * <p>Real ONNX MiniLM embeddings, deterministic seed. Requires Docker (Testcontainers
 * pgvector/pgvector:pg17) and ONNX MiniLM files in the chromadb cache.
 * Run: {@code mvn test -Dtest=CombinedQueryParityIntegrationTest -Dtest.excluded.groups="" -Dgroups=integration}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CombinedQueryParityIntegrationTest {

    private static final String TENANT = "cqparity-tenant";
    private static final String COLL   = "knowledge__cqparity__minilm-l6-v2-384__v1";
    private static final String TOPIC  = "Parity Vectors";

    private static final int CQ_SIZE = Integer.getInteger("nx.cqparity.size", 60);
    private static final int K        = Integer.getInteger("nx.cqparity.k", 10);
    private static final String TOMB_TUMBLER = "cq-doc-tombstoned";

    private static final List<String> WORD_BANK = List.of(
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
        "yankee", "zulu", "cobalt", "quartz", "falcon", "harbor", "lantern");

    record CqDoc(String tumbler, String chash, String text,
                 String contentType, String author, boolean inTopic, boolean tombstoned) {}

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    OnnxEmbedder onnx;
    PgVectorRepository pgRepo;

    final List<CqDoc> docs = new ArrayList<>();
    final List<String> queries = new ArrayList<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') "
                + "THEN CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase lb = new Liquibase("db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                lb.update(new Contexts());
            }
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        onnx = new OnnxEmbedder();
        EmbedderRouter docRouter = new EmbedderRouter(onnx, "document");
        EmbedderRouter queryRouter = new EmbedderRouter(onnx, "query");
        pgRepo = new PgVectorRepository(tenantScope, docRouter, queryRouter);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (onnx  != null) onnx.close();
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    private void seedFixtures() throws Exception {
        Random rnd = new Random(20260614L);
        for (int d = 0; d < CQ_SIZE; d++) {
            int len = 8 + rnd.nextInt(5);
            Set<String> words = new LinkedHashSet<>();
            while (words.size() < len) words.add(WORD_BANK.get(rnd.nextInt(WORD_BANK.size())));
            docs.add(new CqDoc(
                "cq-doc-" + d,
                sha256Hex32("cq-chash-" + d),
                String.join(" ", words) + " cqdoc" + d,
                (d % 2 == 0) ? "paper" : "code",
                (d % 2 == 0) ? "ada" : "bob",
                d % 2 == 0,
                false));
        }
        for (int q = 0; q < docs.size(); q += 7) {
            String[] w = docs.get(q).text().split(" ");
            queries.add(w[0] + " " + w[1]);
        }
        // Tombstone probe: a TOMBSTONED paper doc whose text == the first query, so it
        // would rank at/near the top for that query if the deleted_at guard were missing.
        // Both the live-only app stitch and the combined function must exclude it; a
        // function that dropped `deleted_at IS NULL` would surface it → divergence.
        docs.add(new CqDoc(TOMB_TUMBLER, sha256Hex32("cq-tomb"), queries.get(0),
            "paper", "ada", false, true));

        // Load chunks via the repo (real ONNX embeddings; chash = our 32-char id).
        pgRepo.upsertChunks(TENANT, COLL,
            docs.stream().map(CqDoc::chash).toList(),
            docs.stream().map(CqDoc::text).toList(),
            docs.stream().map(c -> Map.<String, Object>of()).toList());

        // Catalog + manifest + topic via superuser (bypasses RLS for the fixture write).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            long topicId;
            try (var rs = su.createStatement().executeQuery(
                    "INSERT INTO nexus.topics (tenant_id, label, collection, created_at) "
                    + "VALUES ('" + TENANT + "', '" + TOPIC + "', '" + COLL + "', "
                    + "'2026-01-01T00:00:00+00'::timestamptz) RETURNING id")) {
                rs.next();
                topicId = rs.getLong(1);
            }
            for (CqDoc c : docs) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents "
                    + "(tenant_id, tumbler, title, author, content_type, physical_collection, deleted_at) "
                    + "VALUES ('" + TENANT + "', '" + c.tumbler() + "', 'Doc', '" + c.author()
                    + "', '" + c.contentType() + "', '" + COLL + "', "
                    + (c.tombstoned() ? "now()" : "NULL") + ") "
                    + "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, collection) "
                    + "VALUES ('" + TENANT + "', '" + c.tumbler() + "', 0, '" + c.chash()
                    + "', '" + COLL + "') ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
                if (c.inTopic()) {
                    su.createStatement().execute(
                        "INSERT INTO nexus.topic_assignments "
                        + "(tenant_id, doc_id, topic_id, source_collection, assigned_at) "
                        + "VALUES ('" + TENANT + "', '" + c.chash() + "', " + topicId + ", '"
                        + COLL + "', '2026-01-01T00:00:00+00'::timestamptz) "
                        + "ON CONFLICT (tenant_id, doc_id, topic_id) DO NOTHING");
                }
            }
        }
    }

    @Test
    void metadataScoped_parityWithAppStitch() {
        Map<String, String> chashToTumbler = docs.stream()
            .collect(Collectors.toMap(CqDoc::chash, CqDoc::tumbler));
        // LIVE papers only — the combined function tombstone-filters (deleted_at IS
        // NULL), so the oracle must too, or parity would falsely diverge on the
        // tombstoned probe doc.
        Set<String> paperChashes = docs.stream()
            .filter(c -> "paper".equals(c.contentType()) && !c.tombstoned()).map(CqDoc::chash)
            .collect(Collectors.toSet());
        assertThat(queries).isNotEmpty();
        assertThat(paperChashes).isNotEmpty();

        for (String q : queries) {
            // App stitch: full vector search → keep paper chunks → tumbler top-K.
            List<String> stitched = new ArrayList<>();
            for (Map<String, Object> r : pgRepo.search(TENANT, q, List.of(COLL), CQ_SIZE, null)) {
                if (paperChashes.contains((String) r.get("id"))) {
                    stitched.add(chashToTumbler.get((String) r.get("id")));
                    if (stitched.size() == K) break;
                }
            }
            // Combined: one function call, document-level tumblers, top-K.
            List<String> combined = ids(pgRepo.searchMetadataScoped(
                TENANT, q, List.of(COLL), "paper", null, null, null, K));

            assertThat(stitched)
                .as("non-vacuity: the app stitch must surface paper docs for '%s' "
                    + "(queries are built from corpus words) so parity is meaningful", q)
                .isNotEmpty();
            assertThat(combined)
                .as("metadata-scoped(paper) for '%s' must equal the app-stitch top-%d "
                    + "documents. combined=%s stitched=%s", q, K, combined, stitched)
                .containsExactlyInAnyOrderElementsOf(stitched);
        }
    }

    @Test
    void metadataScoped_excludesTombstonedDoc_evenWhenTopRanked() {
        // The tombstoned probe doc's text == queries.get(0), so it is a top vector
        // match — yet the combined function must exclude it (deleted_at IS NULL). This
        // is the non-tautological tombstone check: a function missing the guard would
        // surface a near-rank-1 doc here.
        String q = queries.get(0);
        String tombChash = docs.stream().filter(CqDoc::tombstoned).findFirst()
            .orElseThrow().chash();

        // Precondition: the tombstoned chunk IS vector-reachable (strong match).
        List<String> rawIds = ids(pgRepo.search(TENANT, q, List.of(COLL), CQ_SIZE, null));
        assertThat(rawIds)
            .as("precondition: the tombstoned chunk is a top vector match for its own text")
            .contains(tombChash);

        List<String> combined = ids(pgRepo.searchMetadataScoped(
            TENANT, q, List.of(COLL), "paper", null, null, null, K));
        assertThat(combined)
            .as("metadata-scoped must EXCLUDE the tombstoned paper doc despite it being "
                + "a top vector match — proves the deleted_at IS NULL guard fires")
            .doesNotContain(TOMB_TUMBLER);
    }

    @Test
    void topicScoped_parityWithAppStitch() {
        Set<String> topicChashes = docs.stream()
            .filter(CqDoc::inTopic).map(CqDoc::chash).collect(Collectors.toSet());
        assertThat(topicChashes).isNotEmpty();

        for (String q : queries) {
            // App stitch: full vector search → keep topic-assigned chunks → top-K.
            List<String> stitched = new ArrayList<>();
            for (Map<String, Object> r : pgRepo.search(TENANT, q, List.of(COLL), CQ_SIZE, null)) {
                if (topicChashes.contains((String) r.get("id"))) {
                    stitched.add((String) r.get("id"));
                    if (stitched.size() == K) break;
                }
            }
            // Combined: one function call, chunk-level chashes, top-K.
            List<String> combined = ids(pgRepo.searchTopicScoped(TENANT, q, TOPIC, COLL, K));

            assertThat(stitched)
                .as("non-vacuity: the app stitch must surface topic chunks for '%s' "
                    + "so parity is meaningful", q)
                .isNotEmpty();
            assertThat(combined)
                .as("topic-scoped('%s') for '%s' must equal the app-stitch top-%d chunks. "
                    + "combined=%s stitched=%s", TOPIC, q, K, combined, stitched)
                .containsExactlyInAnyOrderElementsOf(stitched);
        }
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    private static String sha256Hex32(String seed) throws Exception {
        byte[] h = java.security.MessageDigest.getInstance("SHA-256")
            .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder(64);
        for (byte b : h) sb.append(String.format("%02x", b));
        return sb.substring(0, 32);
    }
}
