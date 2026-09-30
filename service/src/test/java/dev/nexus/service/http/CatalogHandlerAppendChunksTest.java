// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 P1.1 (bead nexus-z0o2p.2) -- the HTTP face of {@code POST
 * /v1/catalog/manifest/append} with an inline {@code chunks} array: request
 * validation, the {@code X-Nexus-Usage-Tokens} header, the response fields a
 * client uses to detect an old engine that ignores {@code chunks}, and the
 * unchanged no-chunks path. Drives {@link CatalogHandler#handle} directly with a
 * capturing exchange, like {@code CatalogHandlerManifestFkTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerAppendChunksTest {

    private static final String SVC_ROLE = "svc_append_http_test";
    private static final String SVC_PASS = "svc_append_http_test_pass";
    private static final String TENANT   = "append-http-tenant";
    private static final String COLLECTION = "code__aphttp__minilm-l6-v2-384__v1";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource svcDs;
    private TenantScope tenantScope;
    private CatalogRepository repo;
    private CatalogHandler handler;
    private CatalogHandler handlerWithoutService;
    private final AtomicInteger embeds = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
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
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        Embedder fake = new Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                embeds.addAndGet(texts.size());
                List<float[]> out = new ArrayList<>();
                for (String t : texts) {
                    float[] v = new float[384];
                    v[Math.floorMod(t.hashCode(), 384)] = 1.0f;
                    out.add(v);
                }
                return out;
            }
            @Override public EmbedResult embedWithUsage(List<String> texts) {
                return new EmbedResult(embed(texts), 7L * texts.size());
            }
            @Override public String modelToken() { return "minilm-l6-v2-384"; }
        };
        handler = new CatalogHandler(repo,
            new CombinedWriteService(tenantScope, repo, new EmbedderRouter(fake, "document")));
        handlerWithoutService = new CatalogHandler(repo);
        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void registerDoc(String tumbler) {
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "append-http-" + tumbler,
            "content_type", "code", "corpus", "code",
            "physical_collection", COLLECTION, "chunk_count", 0));
    }

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    // ── the no-chunks path is unchanged ─────────────────────────────────────────

    @Test
    void append_withoutChunks_isByteForByteTheOldResponse_andWritesTheRows() throws Exception {
        registerDoc("aph.1");
        // A chunk the row can reference, written through the combined route first.
        String c = ch("aph1-c");
        CapturingExchange seed = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + COLLECTION + "\",\"docs\":[{\"doc_id\":\"aph.1\",\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}]}],"
            + "\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"aph1 c\"}]}");
        handle(handler, seed);
        assertThat(seed.status).isEqualTo(200);

        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.1\",\"collection\":\"" + COLLECTION + "\",\"rows\":[{\"position\":1,\"chash\":\"" + c + "\"}]}");
        handle(handler, ex);

        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).isEqualTo("{\"ok\":true,\"count\":1}");
        assertThat(ex.responseHeaders.containsKey(VectorHandler.USAGE_TOKENS_HEADER)).isFalse();
        assertThat(repo.getManifest(TENANT, "aph.1")).hasSize(2);
    }

    // ── append with chunks ──────────────────────────────────────────────────────

    @Test
    void append_withChunks_landsChunkAndRowAndReportsCounts() throws Exception {
        registerDoc("aph.2");
        String c = ch("aph2-c");
        int before = embeds.get();

        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.2\",\"collection\":\"" + COLLECTION + "\","
            + "\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"aph2 c\",\"metadata\":{\"k\":\"v\"}}]}");
        handle(handler, ex);

        assertThat(ex.status).isEqualTo(200);
        String body = ex.bodyString();
        assertThat(body).contains("\"ok\":true").contains("\"count\":1")
            .contains("\"chunks_written\":1").contains("\"chunks_deduped\":1")
            .contains("\"embed_skipped\":0").contains("\"embed_embedded\":1");
        assertThat(ex.responseHeaders.getFirst(VectorHandler.USAGE_TOKENS_HEADER))
            .as("embed token usage rides the response header, as write_many does").isEqualTo("7");
        assertThat(embeds.get() - before).isEqualTo(1);
        assertThat(repo.getManifest(TENANT, "aph.2")).hasSize(1);
    }

    // ── validation ──────────────────────────────────────────────────────────────

    @Test
    void append_chunksNotAList_400() throws Exception {
        registerDoc("aph.3");
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.3\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],\"chunks\":\"nope\"}");
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("'chunks' must be a list");
    }

    @Test
    void append_chunkMissingText_400() throws Exception {
        registerDoc("aph.4");
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.4\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],"
            + "\"chunks\":[{\"chash\":\"" + ch("aph4") + "\"}]}");
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("chunks[0]").contains("'text' required");
    }

    @Test
    void append_chunkWithLegacy32CharChash_400() throws Exception {
        registerDoc("aph.5");
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.5\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],"
            + "\"chunks\":[{\"chash\":\"" + "a".repeat(32) + "\",\"text\":\"t\"}]}");
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("chunks[0]").contains("legacy 32-hex");
    }

    @Test
    void append_chunksWithNoCombinedWriteService_503() throws Exception {
        registerDoc("aph.6");
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.6\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],\"chunks\":[]}");
        handle(handlerWithoutService, ex);
        assertThat(ex.status).isEqualTo(503);
    }

    @Test
    void append_chunksForMissingDocument_409_andNothingInserted() throws Exception {
        String c = ch("aph7-c");
        int before = embeds.get();
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.no-such\",\"collection\":\"" + COLLECTION + "\","
            + "\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"orphan\"}]}");
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(409);
        assertThat(ex.bodyString()).contains("document not registered: aph.no-such");
        assertThat(embeds.get() - before).as("no embed for a document that does not exist").isZero();
    }

    // ── sweep_chashes (RDR-223 P1.3, bead nexus-z0o2p.4) ────────────────────────

    /** Writes a document owning {@code chash}, then empties it (sweep off): {@code chash} is left ownerless. */
    private void leaveOwnerless(String docId, String chash) throws Exception {
        registerDoc(docId);
        CapturingExchange seed = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + COLLECTION + "\",\"docs\":[{\"doc_id\":\"" + docId + "\",\"rows\":[{\"position\":0,\"chash\":\"" + chash + "\"}]}],"
            + "\"chunks\":[{\"chash\":\"" + chash + "\",\"text\":\"ownerless " + docId + "\"}]}");
        handle(handler, seed);
        assertThat(seed.status).isEqualTo(200);
        CapturingExchange empty = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + COLLECTION + "\",\"docs\":[{\"doc_id\":\"" + docId + "\",\"rows\":[]}]}");
        handle(handler, empty);
        assertThat(empty.status).isEqualTo(200);
        assertThat(empty.bodyString()).contains("\"dropped_chashes\":{\"" + docId + "\":[\"" + chash + "\"]}");
    }

    @Test
    void append_sweepOnly_withoutChunks_sweepsAfterCommit_andReportsIt() throws Exception {
        String x = ch("aph8-x");
        leaveOwnerless("aph.8.holder", x);
        registerDoc("aph.8");

        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.8\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],"
            + "\"sweep_chashes\":[\"" + x + "\"]}");
        handle(handlerWithoutService, ex);   // no chunks: no CombinedWriteService needed

        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"ok\":true").contains("\"count\":0")
            .contains("\"swept\":1").contains("\"sweep_skipped\":0")
            .contains("\"sweep_detail\":[{\"doc_id\":\"aph.8\",\"dropped\":1,\"swept\":1,\"kept\":0,\"errored\":false}]");
    }

    @Test
    void append_withChunksAndSweepChashes_landsThenSweeps() throws Exception {
        String x = ch("aph9-x"), fresh = ch("aph9-fresh");
        leaveOwnerless("aph.9.holder", x);
        registerDoc("aph.9");

        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.9\",\"collection\":\"" + COLLECTION + "\","
            + "\"rows\":[{\"position\":0,\"chash\":\"" + fresh + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + fresh + "\",\"text\":\"aph9 fresh\"}],"
            + "\"sweep_chashes\":[\"" + x + "\"]}");
        handle(handler, ex);

        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"chunks_written\":1").contains("\"swept\":1");
        assertThat(repo.getManifest(TENANT, "aph.9")).hasSize(1);
    }

    @Test
    void append_sweepChashesOverTheCap_400NamingTheCap_andNothingIsWritten() throws Exception {
        registerDoc("aph.10");
        String c = ch("aph10-c");
        StringBuilder sweep = new StringBuilder();
        for (int i = 0; i < 301; i++) {
            if (i > 0) sweep.append(',');
            sweep.append('"').append(ch("aph10-sweep-" + i)).append('"');
        }
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.10\",\"collection\":\"" + COLLECTION + "\","
            + "\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"aph10 c\"}],"
            + "\"sweep_chashes\":[" + sweep + "]}");
        int before = embeds.get();
        handle(handler, ex);

        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("300").contains("sweep_chashes");
        assertThat(repo.getManifest(TENANT, "aph.10"))
            .as("the refusal comes before any transaction: the rows did not commit").isEmpty();
        assertThat(embeds.get() - before).as("and before the embed").isZero();
    }

    @Test
    void append_sweepChashesAtExactlyTheCap_isAccepted() throws Exception {
        registerDoc("aph.11");
        StringBuilder sweep = new StringBuilder();
        for (int i = 0; i < 300; i++) {
            if (i > 0) sweep.append(',');
            sweep.append('"').append(ch("aph11-sweep-" + i)).append('"');
        }
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.11\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],"
            + "\"sweep_chashes\":[" + sweep + "]}");
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"dropped\":300").contains("\"swept\":0");
    }

    @Test
    void append_malformedSweepChashes_400() throws Exception {
        registerDoc("aph.12");
        CapturingExchange notList = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.12\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],\"sweep_chashes\":\"x\"}");
        handle(handler, notList);
        assertThat(notList.status).isEqualTo(400);
        assertThat(notList.bodyString()).contains("'sweep_chashes' must be a list");

        CapturingExchange notChash = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.12\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],"
            + "\"sweep_chashes\":[\"" + "a".repeat(32) + "\"]}");
        handle(handler, notChash);
        assertThat(notChash.status).isEqualTo(400);
        assertThat(notChash.bodyString()).contains("sweep_chashes[0]").contains("legacy 32-hex");

        CapturingExchange notString = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.12\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],\"sweep_chashes\":[7]}");
        handle(handler, notString);
        assertThat(notString.status).isEqualTo(400);
        assertThat(notString.bodyString()).contains("sweep_chashes[0]").contains("must be a string");
    }

    @Test
    void append_sweepOnlyOnATombstonedDocument_409() throws Exception {
        registerDoc("aph.tomb");
        assertThat(repo.deleteDocument(TENANT, "aph.tomb")).isEqualTo(1);
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.tomb\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],"
            + "\"sweep_chashes\":[\"" + ch("aph-tomb-x") + "\"]}");
        handle(handlerWithoutService, ex);
        assertThat(ex.status).isEqualTo(409);
        assertThat(ex.bodyString()).contains("tombstoned");
    }

    @Test
    void append_sweepChashesForMissingDocument_409() throws Exception {
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.no-such-2\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],"
            + "\"sweep_chashes\":[\"" + ch("aph13-x") + "\"]}");
        handle(handlerWithoutService, ex);
        assertThat(ex.status).isEqualTo(409);
    }

    // ── append_many (RDR-223 P1.4, bead nexus-z0o2p.5) ──────────────────────────

    private static String manyBody(String docsJson, String extra) {
        return "{\"collection\":\"" + COLLECTION + "\",\"docs\":" + docsJson + extra + "}";
    }

    @Test
    void appendMany_withChunks_landsEveryDocument_reportsPerDocumentResultsAndTokens() throws Exception {
        registerDoc("aph.m1");
        registerDoc("aph.m2");
        String a = ch("aphm-a"), b = ch("aphm-b");
        CapturingExchange ex = post("/v1/catalog/manifest/append_many", manyBody(
            "[{\"doc_id\":\"aph.m1\",\"rows\":[{\"position\":0,\"chash\":\"" + a + "\"}]},"
            + "{\"doc_id\":\"aph.m2\",\"rows\":[{\"position\":0,\"chash\":\"" + b + "\"},"
            + "{\"position\":1,\"chash\":\"" + a + "\"}]}]",
            ",\"chunks\":[{\"chash\":\"" + a + "\",\"text\":\"aphm a\"},{\"chash\":\"" + b + "\",\"text\":\"aphm b\"}]"));
        handle(handler, ex);

        assertThat(ex.status).isEqualTo(200);
        String body = ex.bodyString();
        assertThat(body).contains("\"docs\":2").contains("\"rows\":3").contains("\"failed_doc_ids\":[]")
            .contains("\"chunks_written\":3").contains("\"chunks_deduped\":2").contains("\"embed_embedded\":2")
            .contains("\"results\":[{\"doc_id\":\"aph.m1\",\"ok\":true,\"count\":1,\"chunks_written\":1},"
                + "{\"doc_id\":\"aph.m2\",\"ok\":true,\"count\":2,\"chunks_written\":2}]");
        assertThat(ex.responseHeaders.getFirst(VectorHandler.USAGE_TOKENS_HEADER)).isEqualTo("14");
        assertThat(repo.getManifest(TENANT, "aph.m2")).hasSize(2);
    }

    @Test
    void appendMany_withoutChunks_appendsRowsThatReferenceStoredChunks_noServiceNeeded() throws Exception {
        registerDoc("aph.m3");
        registerDoc("aph.m4");
        String c = ch("aphm3-c");
        CapturingExchange seed = post("/v1/catalog/manifest/append", "{\"doc_id\":\"aph.m3\",\"collection\":\"" + COLLECTION + "\","
            + "\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"aphm3 c\"}]}");
        handle(handler, seed);
        assertThat(seed.status).isEqualTo(200);

        CapturingExchange ex = post("/v1/catalog/manifest/append_many", manyBody(
            "[{\"doc_id\":\"aph.m4\",\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}]}]", ""));
        handle(handlerWithoutService, ex);

        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"docs\":1").contains("\"chunks_written\":0")
            .doesNotContain("chunks_deduped");
        assertThat(repo.getManifest(TENANT, "aph.m4")).hasSize(1);
    }

    @Test
    void appendMany_aMissingDocument_isReportedInPlace_200() throws Exception {
        registerDoc("aph.m5");
        String d = ch("aphm5-d");
        CapturingExchange ex = post("/v1/catalog/manifest/append_many", manyBody(
            "[{\"doc_id\":\"aph.m-missing\",\"rows\":[{\"position\":0,\"chash\":\"" + d + "\"}]},"
            + "{\"doc_id\":\"aph.m5\",\"rows\":[{\"position\":0,\"chash\":\"" + d + "\"}]}]",
            ",\"chunks\":[{\"chash\":\"" + d + "\",\"text\":\"aphm5 d\"}]"));
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"docs\":1").contains("\"failed_doc_ids\":[\"aph.m-missing\"]")
            .contains("{\"doc_id\":\"aph.m-missing\",\"ok\":false,\"reason\":\"manifest write refused: document not registered: aph.m-missing\"}");
    }

    @Test
    void appendMany_validation_400s_beforeAnyTransaction() throws Exception {
        registerDoc("aph.m6");
        String c = ch("aphm6-c");
        String good = "{\"doc_id\":\"aph.m6\",\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}]}";

        CapturingExchange notList = post("/v1/catalog/manifest/append_many",
            "{\"collection\":\"" + COLLECTION + "\",\"docs\":\"x\"}");
        handle(handler, notList);
        assertThat(notList.status).isEqualTo(400);
        assertThat(notList.bodyString()).contains("'docs' must be a list");

        CapturingExchange noCollection = post("/v1/catalog/manifest/append_many", "{\"docs\":[" + good + "]}");
        handle(handler, noCollection);
        assertThat(noCollection.status).isEqualTo(400);
        assertThat(noCollection.bodyString()).contains("'collection' required");

        CapturingExchange noDocId = post("/v1/catalog/manifest/append_many", manyBody("[" + good + ",{\"rows\":[]}]", ""));
        handle(handler, noDocId);
        assertThat(noDocId.status).isEqualTo(400);
        assertThat(noDocId.bodyString()).contains("docs[1]").contains("'doc_id' required");

        CapturingExchange badChash = post("/v1/catalog/manifest/append_many", manyBody(
            "[" + good + ",{\"doc_id\":\"aph.m6\",\"rows\":[{\"position\":0,\"chash\":\"" + "a".repeat(32) + "\"}]}]", ""));
        handle(handler, badChash);
        assertThat(badChash.status).isEqualTo(400);
        assertThat(badChash.bodyString()).contains("docs[1]").contains("rows[0]").contains("legacy 32-hex");
        assertThat(repo.getManifest(TENANT, "aph.m6")).as("the valid first document was not written").isEmpty();

        StringBuilder sweep = new StringBuilder();
        for (int i = 0; i < 301; i++) {
            if (i > 0) sweep.append(',');
            sweep.append('"').append(ch("aphm6-sweep-" + i)).append('"');
        }
        CapturingExchange overCap = post("/v1/catalog/manifest/append_many", manyBody(
            "[{\"doc_id\":\"aph.m6\",\"rows\":[],\"sweep_chashes\":[" + sweep + "]}]", ""));
        handle(handler, overCap);
        assertThat(overCap.status).isEqualTo(400);
        assertThat(overCap.bodyString()).contains("docs[0]").contains("300");
    }

    @Test
    void appendMany_caps_docsAndChunks() throws Exception {
        StringBuilder docs = new StringBuilder("[");
        for (int i = 0; i < 1001; i++) {
            if (i > 0) docs.append(',');
            docs.append("{\"doc_id\":\"aph.cap.").append(i).append("\",\"rows\":[]}");
        }
        docs.append(']');
        CapturingExchange tooManyDocs = post("/v1/catalog/manifest/append_many", manyBody(docs.toString(), ""));
        handle(handler, tooManyDocs);
        assertThat(tooManyDocs.status).isEqualTo(400);
        assertThat(tooManyDocs.bodyString()).contains("too many docs (max 1000)");

        StringBuilder chunks = new StringBuilder(",\"chunks\":[");
        for (int i = 0; i < 301; i++) {
            if (i > 0) chunks.append(',');
            chunks.append("{\"chash\":\"").append(ch("aph-cap-chunk-" + i)).append("\",\"text\":\"t\"}");
        }
        chunks.append(']');
        CapturingExchange tooManyChunks = post("/v1/catalog/manifest/append_many", manyBody("[]", chunks.toString()));
        handle(handler, tooManyChunks);
        assertThat(tooManyChunks.status).isEqualTo(400);
        assertThat(tooManyChunks.bodyString()).contains("too many chunks (max 300)");
    }

    @Test
    void appendMany_chunksWithNoCombinedWriteService_503() throws Exception {
        CapturingExchange ex = post("/v1/catalog/manifest/append_many", manyBody("[]", ",\"chunks\":[]"));
        handle(handlerWithoutService, ex);
        assertThat(ex.status).isEqualTo(503);
    }

    @Test
    void append_chunksOverTheCap_400NamingTheCap_beforeAnyEmbedOrTransaction_andAtTheCapIsAccepted() throws Exception {
        registerDoc("aph.cap");
        StringBuilder over = new StringBuilder();
        StringBuilder at = new StringBuilder();
        for (int i = 0; i < 301; i++) {
            String piece = (i > 0 ? "," : "") + "{\"chash\":\"" + ch("aphcap-" + i) + "\",\"text\":\"t" + i + "\"}";
            over.append(piece);
            if (i < 300) at.append(piece);
        }
        int before = embeds.get();
        CapturingExchange tooMany = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.cap\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],\"chunks\":[" + over + "]}");
        handle(handler, tooMany);
        assertThat(tooMany.status).isEqualTo(400);
        assertThat(tooMany.bodyString()).contains("too many chunks (max 300)");
        assertThat(embeds.get() - before).isZero();

        CapturingExchange atCap = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.cap\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],\"chunks\":[" + at + "]}");
        handle(handler, atCap);
        assertThat(atCap.status).isEqualTo(200);
        assertThat(atCap.bodyString()).contains("\"chunks_unreferenced\":300");
    }

    // ── client-supplied vectors (RDR-223 P1.5, bead nexus-z0o2p.6) ──────────────

    private static String vectorJson(int dim, double base) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < dim; i++) {
            if (i > 0) sb.append(',');
            sb.append(base + i * 0.001);
        }
        return sb.append(']').toString();
    }

    @Test
    void vectors_onAppend_areStoredWithoutAnEmbed_andRefusedOnAWrongModelOrDimension() throws Exception {
        registerDoc("aph.v1");
        String c = ch("aphv1-c");
        int before = embeds.get();

        CapturingExchange ok = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.v1\",\"collection\":\"" + COLLECTION + "\",\"embedding_model\":\"minilm-l6-v2-384\","
            + "\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"aphv1 c\",\"embedding\":" + vectorJson(384, 0.5) + "}]}");
        handle(handler, ok);
        assertThat(ok.status).isEqualTo(200);
        assertThat(ok.bodyString()).contains("\"vectors_supplied\":1").contains("\"embed_embedded\":0")
            .contains("\"chunks_written\":1");
        assertThat(embeds.get() - before).as("no embedder call for a supplied vector").isZero();
        assertThat(ok.responseHeaders.containsKey(VectorHandler.USAGE_TOKENS_HEADER)).isFalse();

        String d = ch("aphv1-d");
        CapturingExchange wrongModel = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.v1\",\"collection\":\"" + COLLECTION + "\",\"embedding_model\":\"voyage-code-3\","
            + "\"rows\":[{\"position\":1,\"chash\":\"" + d + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + d + "\",\"text\":\"d\",\"embedding\":" + vectorJson(384, 0.1) + "}]}");
        handle(handler, wrongModel);
        assertThat(wrongModel.status).isEqualTo(400);
        assertThat(wrongModel.bodyString()).contains("voyage-code-3").contains("minilm-l6-v2-384");

        CapturingExchange wrongDim = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.v1\",\"collection\":\"" + COLLECTION + "\",\"embedding_model\":\"minilm-l6-v2-384\","
            + "\"rows\":[{\"position\":1,\"chash\":\"" + d + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + d + "\",\"text\":\"d\",\"embedding\":" + vectorJson(383, 0.1) + "}]}");
        handle(handler, wrongDim);
        assertThat(wrongDim.status).isEqualTo(400);
        assertThat(wrongDim.bodyString()).contains("chunks[0]").contains("383").contains("384");

        CapturingExchange noModel = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.v1\",\"collection\":\"" + COLLECTION + "\","
            + "\"rows\":[{\"position\":1,\"chash\":\"" + d + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + d + "\",\"text\":\"d\",\"embedding\":" + vectorJson(384, 0.1) + "}]}");
        handle(handler, noModel);
        assertThat(noModel.status).isEqualTo(400);
        assertThat(noModel.bodyString()).contains("embedding_model");
        assertThat(repo.getManifest(TENANT, "aph.v1")).as("only the accepted append committed").hasSize(1);
    }

    @Test
    void vectors_onWriteManyAndAppendMany_areAccepted() throws Exception {
        registerDoc("aph.v2");
        registerDoc("aph.v3");
        String a = ch("aphv2-a"), b = ch("aphv3-b");
        int before = embeds.get();

        CapturingExchange wm = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + COLLECTION + "\",\"embedding_model\":\"minilm-l6-v2-384\","
            + "\"docs\":[{\"doc_id\":\"aph.v2\",\"rows\":[{\"position\":0,\"chash\":\"" + a + "\"}]}],"
            + "\"chunks\":[{\"chash\":\"" + a + "\",\"text\":\"a\",\"embedding\":" + vectorJson(384, 0.2) + "}]}");
        handle(handler, wm);
        assertThat(wm.status).isEqualTo(200);
        assertThat(wm.bodyString()).contains("\"vectors_supplied\":1").contains("\"chunks_written\":1");

        CapturingExchange am = post("/v1/catalog/manifest/append_many",
            "{\"collection\":\"" + COLLECTION + "\",\"embedding_model\":\"minilm-l6-v2-384\","
            + "\"docs\":[{\"doc_id\":\"aph.v3\",\"rows\":[{\"position\":0,\"chash\":\"" + b + "\"}]}],"
            + "\"chunks\":[{\"chash\":\"" + b + "\",\"text\":\"b\",\"embedding\":" + vectorJson(384, 0.3) + "}]}");
        handle(handler, am);
        assertThat(am.status).isEqualTo(200);
        assertThat(am.bodyString()).contains("\"vectors_supplied\":1").contains("\"docs\":1");
        assertThat(embeds.get() - before).isZero();
    }

    @Test
    void vectors_malformedShapes_400() throws Exception {
        registerDoc("aph.v4");
        String c = ch("aphv4-c");
        CapturingExchange notArray = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.v4\",\"collection\":\"" + COLLECTION + "\",\"embedding_model\":\"minilm-l6-v2-384\","
            + "\"rows\":[],\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"c\",\"embedding\":\"nope\"}]}");
        handle(handler, notArray);
        assertThat(notArray.status).isEqualTo(400);
        assertThat(notArray.bodyString()).contains("chunks[0]").contains("'embedding' must be an array of numbers");

        CapturingExchange nonNumeric = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.v4\",\"collection\":\"" + COLLECTION + "\",\"embedding_model\":\"minilm-l6-v2-384\","
            + "\"rows\":[],\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"c\",\"embedding\":[0.1,\"x\"]}]}");
        handle(handler, nonNumeric);
        assertThat(nonNumeric.status).isEqualTo(400);
        assertThat(nonNumeric.bodyString()).contains("non-numeric");

        CapturingExchange badModelType = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.v4\",\"collection\":\"" + COLLECTION + "\",\"embedding_model\":7,"
            + "\"rows\":[],\"chunks\":[]}");
        handle(handler, badModelType);
        assertThat(badModelType.status).isEqualTo(400);
        assertThat(badModelType.bodyString()).contains("'embedding_model' must be a string");
    }

    // ── helpers ──────────────────────────────────────────────────────────────────

    private void handle(CatalogHandler h, CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            h.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private static CapturingExchange post(String path, String jsonBody) {
        return new CapturingExchange("POST", URI.create(path), jsonBody);
    }
}
