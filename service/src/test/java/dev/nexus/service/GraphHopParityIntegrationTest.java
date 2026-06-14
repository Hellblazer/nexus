// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
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
 * RDR-156 P4 follow-on (bead nexus-houg9): corpus-scale function-vs-app-stitch parity for
 * the graph-hop combined query {@code nexus.search_graph_hop_<dim>} (catalog-007).
 *
 * <p>The graph-hop function collapses the {@code query} tool's {@code follow_links} dance
 * into one statement. This suite proves it returns the SAME top-k documents as the
 * multi-step app stitch it retires, where the stitch uses the PRODUCTION
 * {@link CatalogRepository#graphBFS} (the very app-side BFS the function replaces) for the
 * reachable-set leg, then the production {@link PgVectorRepository#search} for the vector
 * leg, then an app-side filter+rerank. So the comparison is genuinely function-vs-stitch,
 * NOT function-vs-itself (the gap a same-engine SQL oracle leaves open — the unit
 * {@link GraphHopParityTest} parity group uses an in-SQL oracle; this closes it against
 * the real Java graphBFS).
 *
 * <p>Built as a focused sibling (not a DualRunHarness extension) for the same reason as
 * {@link CombinedQueryParityIntegrationTest}: the harness's Chroma baseline leg is
 * unrelated to combined-query parity and can't embed the minilm token in a minilm-only
 * router (nexus-i055u). Graph-hop parity is pgvector-only: function vs app-stitch on the
 * same store.
 *
 * <p><strong>Non-vacuity:</strong> the fixture plants a vector-CLOSEST but graph-UNREACHABLE
 * doc and a tombstoned reachable doc whose text matches a probe; both the stitch and the
 * function must exclude them. A function that ignored the graph (ranked the whole
 * collection) or dropped {@code deleted_at IS NULL} would diverge.
 *
 * <p><strong>Scale boundary:</strong> validates correctness at fixture scale (default 60
 * docs); the HNSW recall property (Finding 5b) is conexus xr7.8.9, not here.
 *
 * <p>Real ONNX MiniLM embeddings, deterministic seed. Requires Docker + ONNX MiniLM cache.
 * Run: {@code mvn test -Dtest=GraphHopParityIntegrationTest -Dtest.excluded.groups="" -Dgroups=integration}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GraphHopParityIntegrationTest {

    private static final String TENANT = "ghparity-tenant";
    private static final String COLL   = "knowledge__ghparity__minilm-l6-v2-384__v1";
    private static final String SEED   = "gh-doc-0";
    private static final String LINK   = "cites";
    private static final int DEPTH     = 2;

    private static final int GH_SIZE = Integer.getInteger("nx.ghparity.size", 60);
    private static final int K        = Integer.getInteger("nx.ghparity.k", 10);
    private static final String TOMB_TUMBLER = "gh-doc-tombstoned";
    private static final String UNREACH_TUMBLER = "gh-doc-unreachable";

    private static final List<String> WORD_BANK = List.of(
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
        "yankee", "zulu", "cobalt", "quartz", "falcon", "harbor", "lantern");

    record GhDoc(String tumbler, String chash, String text, boolean tombstoned) {}

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    OnnxEmbedder onnx;
    PgVectorRepository pgRepo;
    CatalogRepository catRepo;

    final List<GhDoc> docs = new ArrayList<>();
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
        catRepo = new CatalogRepository(tenantScope);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (onnx  != null) onnx.close();
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    /**
     * Linear cites chain gh-doc-0 → gh-doc-1 → … (each cites the next), so depth-2 BFS
     * from the seed reaches docs 0,1,2. Plus an UNREACHABLE doc (no edges) whose text ==
     * a probe (vector-closest, graph-excluded) and a TOMBSTONED reachable doc.
     */
    private void seedFixtures() throws Exception {
        Random rnd = new Random(20260614L);
        for (int d = 0; d < GH_SIZE; d++) {
            int len = 8 + rnd.nextInt(5);
            Set<String> words = new LinkedHashSet<>();
            while (words.size() < len) words.add(WORD_BANK.get(rnd.nextInt(WORD_BANK.size())));
            docs.add(new GhDoc("gh-doc-" + d, sha256Hex32("gh-chash-" + d),
                String.join(" ", words) + " ghdoc" + d, false));
        }
        // Probes drawn from the FIRST THREE docs' words (the depth-2 reachable set), so
        // the stitch surfaces reachable docs and parity is meaningful.
        for (int q = 0; q < 3; q++) {
            String[] w = docs.get(q).text().split(" ");
            queries.add(w[0] + " " + w[1]);
        }
        // Unreachable doc: text == queries.get(0) → vector top match, but NO edges, so
        // graphBFS never reaches it. Both stitch and function must exclude it.
        docs.add(new GhDoc(UNREACH_TUMBLER, sha256Hex32("gh-unreach"), queries.get(0), false));
        // Tombstoned reachable doc: linked into the chain (so graph-reachable) but
        // deleted_at set; text == queries.get(0) so it would top-rank if the guard missed.
        docs.add(new GhDoc(TOMB_TUMBLER, sha256Hex32("gh-tomb"), queries.get(0), true));

        pgRepo.upsertChunks(TENANT, COLL,
            docs.stream().map(GhDoc::chash).toList(),
            docs.stream().map(GhDoc::text).toList(),
            docs.stream().map(c -> Map.<String, Object>of()).toList());

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (GhDoc c : docs) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents "
                    + "(tenant_id, tumbler, title, author, content_type, physical_collection, deleted_at) "
                    + "VALUES ('" + TENANT + "', '" + c.tumbler() + "', 'Doc', 'ada', 'paper', '"
                    + COLL + "', " + (c.tombstoned() ? "now()" : "NULL") + ") "
                    + "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, collection) "
                    + "VALUES ('" + TENANT + "', '" + c.tumbler() + "', 0, '" + c.chash()
                    + "', '" + COLL + "') ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
            }
            // cites chain gh-doc-0 → 1 → 2 → 3 … across the whole numbered range.
            for (int d = 0; d < GH_SIZE - 1; d++) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_links "
                    + "(tenant_id, from_tumbler, to_tumbler, link_type, created_by) "
                    + "VALUES ('" + TENANT + "', 'gh-doc-" + d + "', 'gh-doc-" + (d + 1)
                    + "', '" + LINK + "', 'test') ON CONFLICT DO NOTHING");
            }
            // Tombstoned doc is reachable: link gh-doc-1 → tombstoned (1 hop from a
            // depth-1 node → within depth 2).
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_links "
                + "(tenant_id, from_tumbler, to_tumbler, link_type, created_by) "
                + "VALUES ('" + TENANT + "', 'gh-doc-1', '" + TOMB_TUMBLER + "', '" + LINK
                + "', 'test') ON CONFLICT DO NOTHING");
            // UNREACH_TUMBLER intentionally has NO edges.
        }
    }

    @Test
    void graphHop_parityWithAppStitch() {
        Map<String, String> chashToTumbler = docs.stream()
            .collect(Collectors.toMap(GhDoc::chash, GhDoc::tumbler));
        Map<String, String> tumblerToChash = docs.stream()
            .collect(Collectors.toMap(GhDoc::tumbler, GhDoc::chash));

        for (String q : queries) {
            // ── App stitch leg 1: production graphBFS for the reachable doc set. ──
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> nodes = (List<Map<String, Object>>) catRepo
                .graphBFS(TENANT, List.of(SEED), List.of(LINK), "out", DEPTH).get("nodes");
            // Reachable, LIVE tumblers (graphBFS returns docRows incl. tombstoned; the
            // function tombstone-filters, so the oracle must too).
            Set<String> reachableLiveChashes = nodes.stream()
                .map(n -> (String) n.get("tumbler"))
                .filter(t -> tumblerToChash.containsKey(t))
                .filter(t -> docs.stream().noneMatch(d -> d.tumbler().equals(t) && d.tombstoned()))
                .map(tumblerToChash::get)
                .collect(Collectors.toSet());

            // ── App stitch leg 2: production vector search → keep reachable → top-K. ──
            List<String> stitched = new ArrayList<>();
            for (Map<String, Object> r : pgRepo.search(TENANT, q, List.of(COLL), GH_SIZE, null)) {
                if (reachableLiveChashes.contains((String) r.get("id"))) {
                    stitched.add(chashToTumbler.get((String) r.get("id")));
                    if (stitched.size() == K) break;
                }
            }

            // ── Combined: one function call, document-level tumblers, top-K. ──
            List<String> combined = ids(pgRepo.searchGraphHop(
                TENANT, q, List.of(SEED), List.of(COLL), LINK, DEPTH, "out", K));

            assertThat(stitched)
                .as("non-vacuity: the app stitch must surface reachable docs for '%s'", q)
                .isNotEmpty();
            assertThat(combined)
                .as("graph-hop for '%s' must equal the app-stitch (graphBFS + search) "
                    + "top-%d docs. combined=%s stitched=%s", q, K, combined, stitched)
                .containsExactlyInAnyOrderElementsOf(stitched);
            // The graph-excluded vector-closest doc must appear in NEITHER.
            assertThat(combined).as("unreachable doc must be excluded for '%s'", q)
                .doesNotContain(UNREACH_TUMBLER);
        }
    }

    @Test
    void graphHop_excludesUnreachableTopRankedDoc() {
        // UNREACH_TUMBLER's text == queries.get(0) → it is a top vector match, yet it has
        // no edges so graphBFS never reaches it. A function that ranked the whole
        // collection (ignoring the graph) would surface it → divergence.
        String q = queries.get(0);
        String unreachChash = docs.stream().filter(d -> d.tumbler().equals(UNREACH_TUMBLER))
            .findFirst().orElseThrow().chash();
        List<String> rawIds = ids(pgRepo.search(TENANT, q, List.of(COLL), GH_SIZE, null));
        assertThat(rawIds)
            .as("precondition: the unreachable doc IS a top vector match for its own text")
            .contains(unreachChash);

        List<String> combined = ids(pgRepo.searchGraphHop(
            TENANT, q, List.of(SEED), List.of(COLL), LINK, DEPTH, "out", K));
        assertThat(combined)
            .as("graph-hop must EXCLUDE the vector-closest but graph-UNREACHABLE doc — "
                + "proves the traversal gate is load-bearing, not a vector passthrough")
            .doesNotContain(UNREACH_TUMBLER);
    }

    @Test
    void graphHop_excludesTombstonedReachableDoc() {
        // TOMB_TUMBLER is graph-reachable (gh-doc-1 → it) AND a top vector match for
        // queries.get(0), but tombstoned — the deleted_at IS NULL guard must drop it.
        String q = queries.get(0);
        List<String> combined = ids(pgRepo.searchGraphHop(
            TENANT, q, List.of(SEED), List.of(COLL), LINK, DEPTH, "out", K));
        assertThat(combined)
            .as("graph-hop must EXCLUDE the reachable-but-tombstoned doc despite a top "
                + "vector match — proves deleted_at IS NULL fires on the graph path too")
            .doesNotContain(TOMB_TUMBLER);
    }

    @Test
    void graphHop_chashIsMatchedChunkChash() {
        // Audit HIGH: every returned row's chash is the matched chunk's chash (the value
        // rzqto wires into chunk_text_hash), i.e. the chash we seeded for that tumbler.
        Map<String, String> tumblerToChash = docs.stream()
            .collect(Collectors.toMap(GhDoc::tumbler, GhDoc::chash));
        List<Map<String, Object>> rows = pgRepo.searchGraphHop(
            TENANT, queries.get(0), List.of(SEED), List.of(COLL), LINK, DEPTH, "out", K);
        assertThat(rows).isNotEmpty();
        for (Map<String, Object> r : rows) {
            assertThat((String) r.get("chash"))
                .as("row %s chash must be its matched chunk's chash", r.get("id"))
                .isEqualTo(tumblerToChash.get((String) r.get("id")));
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
