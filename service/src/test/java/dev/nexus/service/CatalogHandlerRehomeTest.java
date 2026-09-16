// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wsx4l — HTTP coverage for {@code POST /v1/catalog/collections/rehome}
 * ({@link dev.nexus.service.http.CatalogHandler#handleCollectionRehome}).
 *
 * <p>The transaction semantics and the merge-and-report behaviour are covered at the
 * repository level by {@link CollectionRehomeTest}; this exercises only the HTTP glue
 * that test cannot reach: the required-key guards, the 405s, the {@link
 * dev.nexus.service.db.CatalogRepository.RehomeRefused} to 409 mapping, the field
 * names a caller reads, and the disconnect behaviour the whole interface rests on.
 *
 * <p>The response-shape test matters more than it looks. {@code remaining_rows} is
 * what a caller polls and {@code done} is its convenience form; if either goes missing
 * or gets renamed, a caller either never finishes or believes it finished with rows
 * still in the source, and neither failure is visible in a 200.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerRehomeTest {

    private static final String TOKEN = "catalog-rehome-handler-token-ghi789";
    private static final String SVC_ROLE = "svc_cat_rehome_handler";
    private static final String SVC_PASS = "svc_cat_rehome_handler_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    private static final String SRC = "knowledge__hrehome-src__minilm-l6-v2-384__v1";
    private static final String DST = "knowledge__hrehome-dst__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;
    dev.nexus.service.db.TenantScope tenantScope;
    dev.nexus.service.db.CatalogRepository catalogRepo;
    dev.nexus.service.vectors.PgVectorRepository vecRepo;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new dev.nexus.service.db.TenantScope(svcDs);
        catalogRepo = new dev.nexus.service.db.CatalogRepository(tenantScope);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new dev.nexus.service.vectors.PgVectorRepository(tenantScope, embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        // TestHttp, never a bare JDK client builder: one with no connectTimeout can park
        // forever on a socket read while HOLDING the shared build lease, which has wedged
        // this box's engine builds for half an hour at a time (nexus-9meyc). Both
        // ratchets in test_engine_test_http_timeout_lint.py are reduce-only, so the
        // sibling handler tests written before that rule are grandfathered under the
        // ceiling and are not the pattern to copy. (Naming the bare factory method here,
        // even to warn against it, makes this comment itself count as a call site -- that
        // lint matches source text, not the AST.)
        http = TestHttp.client();

        // Burn the once-per-process ghost sweep BEFORE registering anything, exactly
        // as CatalogHandlerRenameTest does and for the same measured reason: the
        // sweep deletes registered-but-chunkless collections, and our target is
        // chunkless by construction -- registering first would let the warmup delete
        // the very row this suite re-homes onto.
        var warmup = TestHttp
            .request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN)
            .GET().build();
        http.send(warmup, HttpResponse.BodyHandlers.ofString());

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, SRC);
            PgContainerHelper.insertCollection(dsl, TENANT, DST);
        }
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void post_emptySource_returns200WithTheLoopContract() throws Exception {
        var resp = post("/v1/catalog/collections/rehome",
            "{\"source\":\"" + SRC + "\",\"target\":\"" + DST + "\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        // Every field a caller's loop reads, by the exact name it reads.
        assertThat(body).containsKeys(
            "moved_chunks", "moved_documents", "moved_by_table", "left_behind_by_table",
            "remaining_documents", "remaining_chunks", "remaining_rows",
            "remaining_by_table", "done");
        assertThat(((Number) body.get("remaining_rows")).intValue())
            .as("an empty source has nothing left, so remaining_rows -- the terminator -- is 0")
            .isZero();
        assertThat(body.get("done"))
            .as("and done is its convenience form, so it agrees")
            .isEqualTo(Boolean.TRUE);
    }

    @Test
    void post_missingSourceOrTarget_returns400() throws Exception {
        assertThat(post("/v1/catalog/collections/rehome", "{}").statusCode()).isEqualTo(400);
        assertThat(post("/v1/catalog/collections/rehome",
            "{\"source\":\"" + SRC + "\"}").statusCode()).isEqualTo(400);
    }

    /** The status route answers the same question from a separate, cheap call. */
    @Test
    void statusRoute_returnsTheRemainderWithoutWriting() throws Exception {
        var resp = post("/v1/catalog/collections/rehome/status",
            "{\"source\":\"" + SRC + "\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKeys(
            "remaining_chunks", "remaining_documents", "remaining_rows",
            "remaining_by_table", "done");
    }

    @Test
    void statusRoute_missingSource_returns400() throws Exception {
        assertThat(post("/v1/catalog/collections/rehome/status", "{}").statusCode())
            .isEqualTo(400);
    }

    @Test
    void post_sameSourceAndTarget_returns409WithTheReason() throws Exception {
        var resp = post("/v1/catalog/collections/rehome",
            "{\"source\":\"" + SRC + "\",\"target\":\"" + SRC + "\"}");
        assertThat(resp.statusCode())
            .as("RehomeRefused is a refusal, so it maps to 409 -- never the generic 500 "
                + "that would discard the message naming the reason")
            .isEqualTo(409);
        assertThat(resp.body()).contains("same collection");
    }

    @Test
    void post_unregisteredSource_returns409_notASilentAllZeroNoOp() throws Exception {
        var resp = post("/v1/catalog/collections/rehome",
            "{\"source\":\"knowledge__hrehome-absent__minilm-l6-v2-384__v1\","
            + "\"target\":\"" + DST + "\"}");
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("source collection");
    }

    @Test
    void get_returns405() throws Exception {
        var req = TestHttp
            .request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/rehome")
            .header("Authorization", "Bearer " + TOKEN)
            .GET().build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode())
            .isEqualTo(405);
    }

    /**
     * nexus-wsx4l, THE LOAD-BEARING QUESTION for a submit-then-poll interface: when the
     * caller's connection dies mid-request, does the handler's transaction still commit?
     *
     * <p>Why it decides the design. A measured re-home of the real owner-1.1 source takes
     * 169 s (46,034 code chunks at 128 s, 6,309 docs chunks at 39 s — the cost is the FK
     * cascade firing for ~95k dependent rows), against an edge read deadline of about
     * 30 s. So EVERY caller is cut long before the work ends, and a return value cannot
     * carry the result. That is only survivable if the engine finishes the job anyway. If
     * instead the pooled connection is returned and rolled back when the client vanishes,
     * a caller cut at 30 s leaves a transaction that aborts at second 31, a poll that
     * reports nothing moved, and an operator who retries forever.
     *
     * <p>There is an adjacent production OBSERVATION that says it survives (the 2026-09-16
     * code__1-1 quarantine: transaction open at 00:13:12.349Z, edge logged 499 at
     * 00:13:42Z, rows still invisible at 00:14:10Z — 28 s after the client left — and all
     * 41,032 rows committed by 00:18:53Z). That is a different handler on the same engine
     * and pool, which makes it evidence rather than proof. This test asks the question of
     * THIS handler.
     *
     * <p>NON-VACUITY, which this test needs more than most: if the move finished before
     * the client timeout fired, the disconnect proved nothing and the test must FAIL as
     * inconclusive rather than pass. So it asserts the target was still EMPTY at the
     * moment the client gave up — the transaction had not committed yet — and only then
     * polls for it to land. A version that merely polled for the rows would go green
     * whether or not a disconnect was ever survived.
     */
    @Test
    void clientCutMidRequest_transactionStillCommits() throws Exception {
        var p = seededPair("cut", 1500);

        // Cut the client deliberately and early. TestHttp.request keeps the connect
        // timeout; the per-request override is what makes the client give up while the
        // engine is still working.
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort()
                + "/v1/catalog/collections/rehome")
            .timeout(java.time.Duration.ofMillis(150))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(
                "{\"source\":\"" + p.src() + "\",\"target\":\"" + p.dst()
                + "\"}"))
            .build();

        boolean cut = false;
        try {
            http.send(req, HttpResponse.BodyHandlers.ofString());
        } catch (java.net.http.HttpTimeoutException e) {
            cut = true;
        }
        assertThat(cut)
            .as("the client must actually have been cut for this test to mean anything; if "
                + "the engine answered inside 150ms, raise the seed size rather than "
                + "accepting the green")
            .isTrue();

        // NON-VACUITY: nothing may be visible yet. If the move already committed, the
        // disconnect happened after the work and this test proved nothing.
        assertThat(chunkCount(p.dst()))
            .as("INCONCLUSIVE, not a pass: the re-home had already committed by the time "
                + "the client gave up, so no transaction outlived a disconnect here. "
                + "Raise the seed size so the work outlasts the client timeout.")
            .isZero();

        // THE QUESTION: with no client attached, does it finish?
        long deadline = System.nanoTime() + java.time.Duration.ofSeconds(60).toNanos();
        int seen = -1;
        while (System.nanoTime() < deadline) {
            seen = chunkCount(p.dst());
            if (seen == 1500) break;
            Thread.sleep(100);
        }
        assertThat(seen)
            .as("the handler's transaction must commit even though the caller is long "
                + "gone -- this is what makes a submit-then-poll interface possible at "
                + "all. A 0 here means the connection was rolled back on disconnect and "
                + "the whole design has to change.")
            .isEqualTo(1500);
        assertThat(chunkCount(p.src()))
            .as("and the source is emptied of chunks by the same committed transaction")
            .isZero();
    }

    private record Pair(String src, String dst) {}

    /**
     * A fresh live source/target pair with {@code chunks} chunks and one document per 50
     * chunks in the source. Sized by the caller so the re-home outlasts a short client
     * timeout; the FK cascade to the manifest is what makes the work non-trivial.
     */
    private Pair seededPair(String slug, int chunks) throws Exception {
        String src = "knowledge__hrehome-" + slug + "-src__minilm-l6-v2-384__v1";
        String dst = "knowledge__hrehome-" + slug + "-dst__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, src);
            PgContainerHelper.insertCollection(dsl, TENANT, dst);
        }
        // CHUNKS FIRST. fk_catalog_chunks_chunk refuses a manifest row naming a
        // (collection, chash) with no nexus.chunks row, so seeding in the other order
        // fails at the first manifest write. MAX_RECORDS_PER_WRITE is 300, so page it.
        for (int start = 0; start < chunks; start += 300) {
            int end = Math.min(start + 300, chunks);
            java.util.List<String> hashes = new java.util.ArrayList<>();
            java.util.List<String> texts = new java.util.ArrayList<>();
            java.util.List<java.util.Map<String, Object>> metas = new java.util.ArrayList<>();
            for (int i = start; i < end; i++) {
                hashes.add(dev.nexus.service.db.Chash.ofText(slug + "-text-" + i).toHex());
                texts.add(slug + "-text-" + i);
                metas.add(java.util.Map.of());
            }
            vecRepo.upsertChunks(TENANT, src, hashes, texts, metas);
        }
        int perDoc = 50;
        for (int base = 0; base < chunks; base += perDoc) {
            String doc = slug + "-doc-" + (base / perDoc);
            catalogRepo.upsertDocument(TENANT, java.util.Map.of(
                "tumbler", doc, "title", doc, "content_type", "paper",
                "corpus", "knowledge", "physical_collection", src));
            java.util.List<java.util.Map<String, Object>> manifest = new java.util.ArrayList<>();
            for (int i = base; i < Math.min(base + perDoc, chunks); i++) {
                manifest.add(java.util.Map.of("position", i - base,
                    "chash", dev.nexus.service.db.Chash.ofText(slug + "-text-" + i).toHex()));
            }
            catalogRepo.writeManifest(TENANT, doc, src, manifest);
        }
        return new Pair(src, dst);
    }

    private int chunkCount(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchCount(
            DSL.table(DSL.name("nexus", "chunks")),
            DSL.field("collection", String.class).eq(collection)));
    }

    private HttpResponse<String> post(String path, String json) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
