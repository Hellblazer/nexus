// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.OwnerlessWriteActivity;
import dev.nexus.service.vectors.OwnerlessWriteMode;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): {@code upsert-chunks} and {@code store-put} refuse a
 * write whose chashes are not all owned, i.e. whose chashes do not each have a live manifest
 * row in the collection. The check is in the HANDLERS (a guard passed to the repository), so a
 * repository method called directly stays unguarded.
 *
 * <p>The tests drive the real HTTP routes against a Testcontainers Postgres. Every group pins one
 * clause of the bead's test list: the refusal (including a request that names a document, and
 * the {@code force_re_embed} and supplied-embeddings branches that never look at the existence
 * partition), the acceptance of owned chashes, the ORDER of the three 4xx checks, the log-only mode,
 * and the counters {@code /v1/status} serves.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerlessWriteRefusalTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN    = "tok-owr-0123456789abcdef0123456789abcdef";
    private static final String SVC_ROLE = "svc_owr";
    private static final String SVC_PASS = "svc_owr_pass";
    private static final String TENANT   = "owr-tenant";
    private static final String COLLECTION = "knowledge__owr-owner__voyage-context-3__v1";
    private static final String UNREGISTERED = "knowledge__owr-never-registered__voyage-context-3__v1";

    private static final String COMBINED_WRITE  = "/v1/catalog/manifest/write_many";
    private static final String COMBINED_APPEND = "/v1/catalog/manifest/append";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    PgVectorRepository repo;
    ProbeEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "owr-test");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        embedder = new ProbeEmbedder();
        repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs, null, repo);
        service.start();
        http = HttpClient.newHttpClient();

        // Burn the per-tenant ghost sweep before registering the collection (see
        // VectorHandlerDeadlineMappingTest for the measured ordering trap).
        http.send(HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list"))
            .header("Authorization", "Bearer " + TOKEN).GET().build(),
            HttpResponse.BodyHandlers.ofString());
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    @BeforeEach
    void reset() {
        service.ownerlessWritePolicy().set(OwnerlessWriteMode.ENFORCE);
        embedder.calls.set(0);
        embedder.failWith = null;
        OwnerlessWriteActivity.resetForTests();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        return http.send(HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN).GET().build(),
            HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> json(HttpResponse<String> resp) throws Exception {
        return MAPPER.readValue(resp.body(), Map.class);
    }

    private HttpResponse<String> upsert(String collection, List<String> ids, List<String> docs,
                                        Map<String, Object> extra) throws Exception {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection", collection);
        body.put("ids", ids);
        body.put("documents", docs);
        body.put("metadatas", ids.stream().map(i -> Map.of()).toList());
        body.putAll(extra);
        return post("/v1/vectors/upsert-chunks", body);
    }

    private HttpResponse<String> upsert(String collection, List<String> ids, List<String> docs) throws Exception {
        return upsert(collection, ids, docs, Map.of());
    }

    private static String chash(String seed) {
        return Chash.ofText(seed).toHex();
    }

    /** Seed one OWNED chunk (stored vector, a live manifest row) in the shared collection. */
    private String owned(String seed) throws Exception {
        String h = chash(seed);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertOwnedChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION, 1024, h);
        }
        return h;
    }

    private int storedCount(String h) throws Exception {
        var resp = post("/v1/vectors/get", Map.of("collection", COLLECTION,
            "where", Map.of(), "include_non_live", true, "limit", 300));
        assertThat(resp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) json(resp).get("ids");
        return (int) ids.stream().filter(h::equals).count();
    }

    private void assertNamesTheCombinedRoutes(HttpResponse<String> resp) throws Exception {
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(422);
        Map<String, Object> body = json(resp);
        assertThat((String) body.get("error"))
            .contains(COMBINED_WRITE)
            .contains(COMBINED_APPEND);
        assertThat(body.get("reason")).as("the typed discriminator a client keys on")
            .isEqualTo("ownerless_chunk_write");
        assertThat(((Number) body.get("unowned_count")).intValue()).isPositive();
    }

    // ── 1. the refusal ───────────────────────────────────────────────────────

    @Test
    void upsertChunks_aChashWithNoLiveManifestRow_isRefused422NamingTheCombinedRoutes() throws Exception {
        String h = chash("owr-ownerless-1");
        var resp = upsert(COLLECTION, List.of(h), List.of("ownerless chunk text"));
        assertNamesTheCombinedRoutes(resp);
        assertThat(storedCount(h)).as("a refused write stores nothing").isZero();
        assertThat(OwnerlessWriteActivity.refusedTotal()).isEqualTo(1);
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal()).isZero();
    }

    @Test
    void upsertChunks_aRequestThatNamesADocumentIsStillRefused() throws Exception {
        // The routes never write manifest rows, so a field naming an owner satisfies nothing.
        String h = chash("owr-ownerless-names-doc");
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection", COLLECTION);
        body.put("ids", List.of(h));
        body.put("documents", List.of("text"));
        body.put("metadatas", List.of(Map.of("doc_id", "1.2.3", "catalog_doc_id", "1.2.3", "owner", "1.2")));
        body.put("doc_id", "1.2.3");
        body.put("document", "1.2.3");
        body.put("owner", "1.2");
        body.put("tumbler", "1.2.3");
        assertNamesTheCombinedRoutes(post("/v1/vectors/upsert-chunks", body));
        assertThat(storedCount(h)).isZero();
    }

    @Test
    void upsertChunks_forceReEmbedDoesNotBypassTheCheck() throws Exception {
        String h = chash("owr-ownerless-force");
        var resp = upsert(COLLECTION, List.of(h), List.of("text"), Map.of("force_re_embed", true));
        assertNamesTheCombinedRoutes(resp);
        assertThat(embedder.calls.get()).as("a refused write never pays the embedder").isZero();
        assertThat(storedCount(h)).isZero();
    }

    @Test
    void upsertChunks_suppliedEmbeddingsDoNotBypassTheCheck() throws Exception {
        String h = chash("owr-ownerless-vectors");
        float[] v = new float[1024];
        v[0] = 1f;
        var resp = upsert(COLLECTION, List.of(h), List.of("text"), Map.of("embeddings", List.of(v)));
        assertNamesTheCombinedRoutes(resp);
        assertThat(storedCount(h)).isZero();
    }

    @Test
    void upsertChunks_oneUnownedChashRefusesTheWholeRequest() throws Exception {
        String ownedHash = owned("owr-mixed-owned");
        String orphan = chash("owr-mixed-orphan");
        var resp = upsert(COLLECTION, List.of(ownedHash, orphan), List.of("refreshed text", "orphan text"),
            Map.of("metadatas", List.of(Map.of("marker", "must-not-land"), Map.of())));
        assertNamesTheCombinedRoutes(resp);
        assertThat(storedCount(orphan)).isZero();
        var got = post("/v1/vectors/get", Map.of("collection", COLLECTION, "where", Map.of("marker", "must-not-land")));
        @SuppressWarnings("unchecked")
        List<String> hits = (List<String>) json(got).get("ids");
        assertThat(hits).as("the owned chash's metadata refresh rides the same refused request").isEmpty();
    }

    @Test
    void upsertChunks_aChashOwnedOnlyByATombstonedDocumentIsNotOwned() throws Exception {
        String h = owned("owr-tombstoned-owner");
        setOwnerTombstoned(true);
        try {
            assertNamesTheCombinedRoutes(upsert(COLLECTION, List.of(h), List.of("text")));
        } finally {
            setOwnerTombstoned(false);
        }
    }

    /** Tombstone (or revive) the document {@link #owned} hangs its manifest rows on. */
    private void setOwnerTombstoned(boolean tombstoned) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, tombstoned ? DSL.currentOffsetDateTime() : null)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT))
                .and(CATALOG_DOCUMENTS.TUMBLER.eq("own-" + COLLECTION))
                .execute();
        }
    }

    @Test
    void storePut_aChashWithNoLiveManifestRow_isRefused422NamingTheCombinedRoutes() throws Exception {
        String text = "owr-store-put-ownerless-text";
        var resp = post("/v1/vectors/store-put", Map.of(
            "collection", COLLECTION, "doc_id", chash(text), "content", text, "metadata", Map.of()));
        assertNamesTheCombinedRoutes(resp);
        assertThat(storedCount(chash(text))).isZero();
    }

    // ── 2. owned writes are accepted as today ────────────────────────────────

    @Test
    void upsertChunks_ownedChashes_areAcceptedAndRefreshMetadata() throws Exception {
        String h = owned("owr-owned-refresh");
        var resp = upsert(COLLECTION, List.of(h), List.of("seed"),
            Map.of("metadatas", List.of(Map.of("refreshed", "yes"))));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        assertThat(embedder.calls.get()).as("an owned chash with a stored vector skips the embedder").isZero();
        var got = post("/v1/vectors/get", Map.of("collection", COLLECTION, "where", Map.of("refreshed", "yes")));
        @SuppressWarnings("unchecked")
        List<String> hits = (List<String>) json(got).get("ids");
        assertThat(hits).containsExactly(h);
        assertThat(OwnerlessWriteActivity.refusedTotal()).isZero();
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal()).isZero();
    }

    @Test
    void upsertChunks_ownedChashes_withForceReEmbed_areAccepted() throws Exception {
        // The collection re-embed shape: owned chashes, force_re_embed, the embedder runs.
        String h = owned("owr-owned-force");
        var resp = upsert(COLLECTION, List.of(h), List.of("re-embedded text"), Map.of("force_re_embed", true));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        assertThat(embedder.calls.get()).isEqualTo(1);
    }

    @Test
    void upsertChunks_ownedChashes_withSuppliedEmbeddings_areAccepted() throws Exception {
        String h = owned("owr-owned-vectors");
        float[] v = new float[1024];
        v[1] = 1f;
        var resp = upsert(COLLECTION, List.of(h), List.of("seed"), Map.of("embeddings", List.of(v)));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
    }

    @Test
    void storePut_anOwnedChash_isAccepted() throws Exception {
        String text = "seed";   // insertOwnedChunks seeds the text "seed"; the chash is the caller's identity
        String h = owned("owr-owned-store-put");
        var resp = post("/v1/vectors/store-put", Map.of(
            "collection", COLLECTION, "doc_id", h, "content", text, "metadata", Map.of("k", "v")));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
    }

    // ── 3. the order of the three 4xx checks ─────────────────────────────────

    @Test
    void order_aLegacy32CharIdIsStill400_aheadOfTheOwnershipCheck() throws Exception {
        // Chash.requireCanonical runs first, in the handler, before the repository is reached.
        var resp = upsert(COLLECTION, List.of("0123456789abcdef0123456789abcdef"), List.of("text"));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(400);
        assertThat(OwnerlessWriteActivity.refusedTotal()).isZero();
    }

    @Test
    void order_anUnregisteredCollectionSaysRegisterItFirst_aheadOfTheOwnershipRefusal() throws Exception {
        // The repository resolves the collection (dimForCollection -> 'register it first')
        // before it asks about ownership, so an ownerless write to an unregistered collection
        // gets the registration remedy, not the combined-route one.
        var resp = upsert(UNREGISTERED, List.of(chash("owr-unregistered")), List.of("text"));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(422);
        assertThat((String) json(resp).get("error"))
            .contains("register it first")
            .doesNotContain(COMBINED_WRITE);
        assertThat(OwnerlessWriteActivity.refusedTotal()).isZero();
    }

    @Test
    void order_anOwnerlessWriteWithAThrowingEmbedderIs422Not503() throws Exception {
        // Ownership is checked BEFORE embedding, so a refused write never reaches the (failing)
        // embedder: the caller learns the real reason, not a retryable upstream error.
        embedder.failWith = new dev.nexus.service.vectors.RequestDeadlineExceededException(
            "owr-simulated-deadline", 5L, dev.nexus.service.vectors.RequestDeadlineExceededException.Outcome.ABORTED);
        var resp = upsert(COLLECTION, List.of(chash("owr-throwing-embedder")), List.of("text"));
        assertNamesTheCombinedRoutes(resp);
        assertThat(embedder.calls.get()).isZero();
    }

    // ── 4. log-only mode ─────────────────────────────────────────────────────

    @Test
    void logOnly_anOwnerlessWriteIsAcceptedAndCounted_notRefused() throws Exception {
        service.ownerlessWritePolicy().set(OwnerlessWriteMode.LOG_ONLY);
        String h = chash("owr-log-only");
        var resp = upsert(COLLECTION, List.of(h), List.of("log-only text"));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        assertThat(storedCount(h)).as("log-only writes it as today").isEqualTo(1);
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal()).isEqualTo(1);
        assertThat(OwnerlessWriteActivity.refusedTotal()).isZero();
    }

    @Test
    void logOnly_storePutIsCountedToo_andAnOwnedWriteIsNot() throws Exception {
        service.ownerlessWritePolicy().set(OwnerlessWriteMode.LOG_ONLY);
        String text = "owr-log-only-store-put";
        assertThat(post("/v1/vectors/store-put", Map.of(
            "collection", COLLECTION, "doc_id", chash(text), "content", text, "metadata", Map.of()))
            .statusCode()).isEqualTo(200);
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal()).isEqualTo(1);

        String h = owned("owr-log-only-owned");
        assertThat(upsert(COLLECTION, List.of(h), List.of("seed")).statusCode()).isEqualTo(200);
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal()).as("an owned write is not counted").isEqualTo(1);
    }

    @Test
    void theModeParsesFromTheSettingValue() {
        assertThat(OwnerlessWriteMode.parse(null)).as("the default for the final cut").isEqualTo(OwnerlessWriteMode.ENFORCE);
        assertThat(OwnerlessWriteMode.parse("")).isEqualTo(OwnerlessWriteMode.ENFORCE);
        assertThat(OwnerlessWriteMode.parse("enforce")).isEqualTo(OwnerlessWriteMode.ENFORCE);
        assertThat(OwnerlessWriteMode.parse("log-only")).isEqualTo(OwnerlessWriteMode.LOG_ONLY);
        assertThat(OwnerlessWriteMode.parse(" LOG-ONLY ")).isEqualTo(OwnerlessWriteMode.LOG_ONLY);
        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class,
            () -> OwnerlessWriteMode.parse("off"));
    }

    // ── 5. the counters on /v1/status ────────────────────────────────────────

    @Test
    void status_servesBothCounters() throws Exception {
        upsert(COLLECTION, List.of(chash("owr-status-refused")), List.of("text"));   // refused
        service.ownerlessWritePolicy().set(OwnerlessWriteMode.LOG_ONLY);
        upsert(COLLECTION, List.of(chash("owr-status-would")), List.of("text"));     // would-refuse
        var resp = get("/v1/status");
        assertThat(resp.statusCode()).isEqualTo(200);
        Map<String, Object> body = json(resp);
        assertThat(((Number) body.get("ownerless_writes_refused_total")).longValue()).isEqualTo(1);
        assertThat(((Number) body.get("ownerless_writes_would_refuse_total")).longValue()).isEqualTo(1);
        assertThat(body.get("ownerless_write_mode")).isEqualTo("log-only");
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    /** A real vector per text, a call counter, and an optional failure to throw. */
    private static final class ProbeEmbedder implements Embedder {
        final AtomicInteger calls = new AtomicInteger();
        volatile RuntimeException failWith;

        @Override
        public String modelToken() {
            return "voyage-context-3";
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            calls.incrementAndGet();
            if (failWith != null) throw failWith;
            List<float[]> out = new ArrayList<>(texts.size());
            for (String ignored : texts) {
                float[] v = new float[1024];
                v[0] = 1f;
                out.add(v);
            }
            return out;
        }

        @Override
        public void close() {
        }
    }
}
