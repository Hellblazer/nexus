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
    private static final String TOKEN_2  = "tok-owr2-0123456789abcdef0123456789abcde";
    private static final String TENANT_2 = "owr-tenant-2";
    private static final String COLLECTION_B = "knowledge__owr-owner-b__voyage-context-3__v1";
    // The refusal log is rate limited per (route, tenant, collection), so each log test gets its own collection.
    private static final String COLLECTION_LOG = "knowledge__owr-owner-log__voyage-context-3__v1";
    private static final String COLLECTION_LOOP = "knowledge__owr-owner-loop__voyage-context-3__v1";
    private static final String COLLECTION_CLIP = "knowledge__owr-owner-clip__voyage-context-3__v1";
    /** Registered by BOTH tenants, so the limiter's tenant key can be told from its collection key. */
    private static final String COLLECTION_SHARED_NAME = "knowledge__owr-owner-shared__voyage-context-3__v1";
    private static final String COLLECTION_GATE = "knowledge__owr-owner-gate__voyage-context-3__v1";
    private static final String COLLECTION_FORGE = "knowledge__owr-owner-forge__voyage-context-3__v1";
    private static final String COLLECTION_INTX = "knowledge__owr-owner-intx__voyage-context-3__v1";
    private static final String COLLECTION_INTX_PUT = "knowledge__owr-owner-intxput__voyage-context-3__v1";
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
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN_2, TENANT_2, "owr-test-2");
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
        http = TestHttp.client();

        // Burn the per-tenant ghost sweep before registering the collection (see
        // VectorHandlerDeadlineMappingTest for the measured ordering trap).
        for (String token : List.of(TOKEN, TOKEN_2)) {
            http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
                .header("Authorization", "Bearer " + token).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        }
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION_B);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION_LOG);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION_LOOP);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION_CLIP);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION_SHARED_NAME);
            PgContainerHelper.insertCollection(dsl, TENANT_2, COLLECTION_SHARED_NAME);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION_GATE);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION_FORGE);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION_INTX);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION_INTX_PUT);
            PgContainerHelper.insertCollection(dsl, TENANT_2, COLLECTION);
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
        repo.setAfterNeedEmbedResolvedHookForTests(null);
        OwnerlessWriteActivity.resetForTests();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String path, Object body) throws Exception {
        return post(TOKEN, path, body, Map.of());
    }

    private HttpResponse<String> post(String token, String path, Object body, Map<String, String> headers)
            throws Exception {
        var builder = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("Content-Type", "application/json");
        headers.forEach(builder::header);
        var req = builder
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
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
        // Physical scan, every page: the collection holds more than one page of chunks, and a
        // count that stopped at the first page would pass vacuously for a chash past it.
        int found = 0;
        for (int offset = 0; ; offset += 300) {
            var resp = post("/v1/vectors/get", Map.of("collection", COLLECTION,
                "where", Map.of(), "include_non_live", true, "limit", 300, "offset", offset));
            assertThat(resp.statusCode()).isEqualTo(200);
            @SuppressWarnings("unchecked")
            List<String> ids = (List<String>) json(resp).get("ids");
            found += (int) ids.stream().filter(h::equals).count();
            if (ids.size() < 300) {
                return found;
            }
        }
    }

    private void assertNamesTheCombinedRoutes(HttpResponse<String> resp) throws Exception {
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(422);
        Map<String, Object> body = json(resp);
        assertThat((String) body.get("error"))
            .contains(COMBINED_WRITE)
            .contains(COMBINED_APPEND)
            .as("the sentence an old-client user acts on")
            .contains("upgrade conexus and restart nx-mcp");
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
    void upsertChunks_aRequestLargerThanOneCheckBatch_isCheckedEndToEnd() throws Exception {
        // The ownership query is batched (300 chashes a statement); an unowned chash in the LAST
        // batch must still refuse the whole request, and 301 owned chashes must all be found.
        int n = 305;
        String[] hashes = new String[n];
        for (int i = 0; i < n; i++) hashes[i] = chash("owr-batch-" + i);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertOwnedChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION, 1024, hashes);
        }
        var ids = new ArrayList<>(List.of(hashes));
        var docs = new ArrayList<String>();
        for (int i = 0; i < n; i++) docs.add("seed");
        assertThat(upsert(COLLECTION, ids, docs).statusCode()).as("all 305 owned").isEqualTo(200);

        String orphan = chash("owr-batch-orphan");
        ids.add(orphan);
        docs.add("orphan text");
        var resp = upsert(COLLECTION, ids, docs);
        assertNamesTheCombinedRoutes(resp);
        assertThat(((Number) json(resp).get("unowned_count")).intValue()).isEqualTo(1);
        assertThat(((Number) json(resp).get("requested_count")).intValue()).isEqualTo(n + 1);
        assertThat(storedCount(orphan)).isZero();
    }

    @Test
    void upsertChunks_aDuplicateIdCountsOnceInTheRefusal() throws Exception {
        String h = chash("owr-dup-unowned");
        var resp = upsert(COLLECTION, List.of(h, h), List.of("a", "b"));
        assertNamesTheCombinedRoutes(resp);
        assertThat(((Number) json(resp).get("unowned_count")).intValue()).isEqualTo(1);
        assertThat(((Number) json(resp).get("requested_count")).intValue()).isEqualTo(1);
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
    void order_aLegacy32CharIdIsStill400_aheadOfEverythingTheRepositoryAsks() throws Exception {
        // Chash.requireCanonical runs in the handler, before the repository resolves the collection or
        // asks about ownership. The collection here is UNREGISTERED, so a request that reached the
        // repository would answer 422 'register it first', not 400: this fails if the handler's check goes.
        String legacy = "0123456789abcdef0123456789abcdef";
        var up = upsert(UNREGISTERED, List.of(legacy), List.of("text"));
        assertThat(up.statusCode()).as("upsert-chunks, body: %s", up.body()).isEqualTo(400);
        var sp = post("/v1/vectors/store-put", Map.of(
            "collection", UNREGISTERED, "doc_id", legacy, "content", "text", "metadata", Map.of()));
        assertThat(sp.statusCode()).as("store-put, body: %s", sp.body()).isEqualTo(400);
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
        assertThat(OwnerlessWriteMode.parse(null)).as("unset means log-only: only an explicit enforce enforces")
            .isEqualTo(OwnerlessWriteMode.LOG_ONLY);
        assertThat(OwnerlessWriteMode.parse("")).isEqualTo(OwnerlessWriteMode.LOG_ONLY);
        assertThat(OwnerlessWriteMode.parse("   ")).isEqualTo(OwnerlessWriteMode.LOG_ONLY);
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


    // ── 6. pins that survived mutation in review ─────────────────────────────

    /** Seed an ownerless chunk (stored text, vector and metadata, no manifest row) in COLLECTION. */
    private void seedOwnerless(String hex, String text, Map<String, Object> metadata) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var v = new float[1024];
            v[0] = 1f;
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION,
                List.of(hex), List.of(text), List.of(v), List.of(metadata));
        }
    }

    /** {@code [metadata json, last_written_at]} of the physical chunk row, or null when there is none. */
    private Object[] physicalRow(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ch = dev.nexus.service.vectors.DimTables.CHUNKS.get(1024);
            var rec = DSL.using(su, SQLDialect.POSTGRES)
                .select(ch.metadata(), ch.lastWrittenAt())
                .from(ch.table())
                .where(ch.tenantId().eq(tenant).and(ch.collection().eq(collection)).and(ch.chash().eq(hex)))
                .fetchOne();
            return rec == null ? null : new Object[] {String.valueOf(rec.value1()), rec.value2()};
        }
    }

    @Test
    void checkRunsBeforeTheExistencePartition_aRefusedRequestChangesNoRow() throws Exception {
        // An ownerless chunk that already holds a vector and the SAME text takes the partition's
        // metadata-only UPDATE (committed before embedding). The ownership check must refuse first, so
        // the marker is not applied and last_written_at (the reaper's grace anchor) is not restamped.
        String text = "owr-ownerless-with-vector";
        String h = chash(text);
        seedOwnerless(h, text, Map.of("kept", "yes"));
        Object[] before = physicalRow(TENANT, COLLECTION, h);
        assertThat(before).isNotNull();

        var resp = upsert(COLLECTION, List.of(h), List.of(text),
            Map.of("metadatas", List.of(Map.of("marker", "must-not-land"))));
        assertNamesTheCombinedRoutes(resp);

        Object[] after = physicalRow(TENANT, COLLECTION, h);
        assertThat((String) after[0]).as("no marker applied").doesNotContain("must-not-land").contains("kept");
        assertThat(after[1]).as("last_written_at not restamped").isEqualTo(before[1]);
    }

    @Test
    void ownedInAnotherCollection_doesNotAuthoriseAWriteHere() throws Exception {
        // Chashes are content hashes, identical across collections: a chash owned in COLLECTION must
        // not authorise an ownerless write into COLLECTION_B.
        String h = owned("owr-owned-elsewhere");
        var resp = upsert(COLLECTION_B, List.of(h), List.of("seed"));
        assertNamesTheCombinedRoutes(resp);
    }

    @Test
    void ownedByAnotherTenant_doesNotAuthoriseAWriteHere() throws Exception {
        // Tenant 1 owns the chash in COLLECTION; tenant 2 registered the same collection name and
        // writes the same chash. Tenant 2 has no manifest row of its own, so it is refused.
        String h = owned("owr-owned-by-tenant-1");
        var resp = post(TOKEN_2, "/v1/vectors/upsert-chunks", Map.of(
            "collection", COLLECTION, "ids", List.of(h), "documents", List.of("seed"),
            "metadatas", List.of(Map.of())), Map.of());
        assertNamesTheCombinedRoutes(resp);
    }

    @Test
    void aChashThatLosesItsOwnerAndItsChunkDuringTheEmbed_isRefusedInsideTheWriteTransaction() throws Exception {
        // The pre-embed check passes (the chash is owned). Between it and the write, another client
        // re-indexes the document without the chash and the post-commit sweep deletes the chunk row.
        // Without a second check inside the write transaction the INSERT would create a NEW chunk with
        // no owner. The seam fires after the existence partition and before the embed.
        String h = owned("owr-loses-owner-mid-write");
        repo.setAfterNeedEmbedResolvedHookForTests(() -> {
            try (Connection su = pg.createConnection("")) {
                var dsl = DSL.using(su, SQLDialect.POSTGRES);
                dsl.deleteFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS)
                    .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT))
                    .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.CHASH.eq(
                        Chash.fromHex(h).toBytes()))
                    .execute();
                var ch = dev.nexus.service.vectors.DimTables.CHUNKS.get(1024);
                dsl.deleteFrom(ch.table())
                    .where(ch.tenantId().eq(TENANT).and(ch.collection().eq(COLLECTION)).and(ch.chash().eq(h)))
                    .execute();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        // New text, so the chash takes the need-embed path and reaches the insert.
        var resp = upsert(COLLECTION, List.of(h), List.of("text that differs from the stored seed"));
        assertNamesTheCombinedRoutes(resp);
        assertThat(embedder.calls.get()).as("the pre-embed check passed, so the embedder ran").isEqualTo(1);
        assertThat(physicalRow(TENANT, COLLECTION, h)).as("no chunk row was created for it").isNull();
        assertThat(OwnerlessWriteActivity.refusedTotal()).isEqualTo(1);
    }


    @Test
    void theTenantPredicateOfTheOwnershipRead_isPinnedOnAConnectionThatBypassesRls() throws Exception {
        // On the service's own connections RLS (FORCE) scopes the read to the tenant, so a dropped
        // tenant predicate would still pass the HTTP test above. The superuser connection bypasses
        // RLS: here only the predicate separates the two tenants.
        String h = owned("owr-tenant-predicate");
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgVectorRepository.liveOwnedChashes(dsl, TENANT, COLLECTION, List.of(h)))
                .as("the owning tenant sees its chash").containsExactly(h);
            assertThat(PgVectorRepository.liveOwnedChashes(dsl, TENANT_2, COLLECTION, List.of(h)))
                .as("another tenant, same collection name, same chash: not owned").isEmpty();
        }
    }

    @Test
    void theRecheckInTheWriteTransactionWaitsForAnExclusiveSweepGate() throws Exception {
        // The sweep takes the gate EXCLUSIVE; the recheck must take it SHARED, or the sweep could delete
        // the chunk row between the recheck's read and the insert. Hold the gate exclusive on a separate
        // connection from the moment the write has passed its pre-embed check, release it 1.5 s later, and
        // require the write to have waited for the release. Without acquireSweepGateShared in the recheck
        // nothing waits and the write returns at once.
        String text = "owr-gate-owned";
        String h = chash(text);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertOwnedChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION_GATE, 1024, h);
        }
        long holdMs = 1_500L;
        List<Thread> releasers = new ArrayList<>();
        repo.setAfterNeedEmbedResolvedHookForTests(() -> {
            try {
                Connection holder = pg.createConnection("");
                holder.setAutoCommit(false);
                // The same key CatalogRepository.acquireSweepGateExclusive takes (typed DSL, RawSqlGateTest).
                DSL.using(holder, SQLDialect.POSTGRES)
                    .select(DSL.function("pg_advisory_xact_lock", Object.class,
                        DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + TENANT + "/" + COLLECTION_GATE))))
                    .fetch();
                Thread t = new Thread(() -> {
                    try {
                        Thread.sleep(holdMs);
                        holder.commit();
                        holder.close();
                    } catch (Exception e) {
                        throw new IllegalStateException(e);
                    }
                });
                t.start();
                releasers.add(t);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        long start = System.nanoTime();
        // New text, so the chash takes the need-embed path and reaches the write transaction.
        var resp = upsert(COLLECTION_GATE, List.of(h), List.of("text that differs from the stored seed"));
        long elapsedMs = (System.nanoTime() - start) / 1_000_000L;
        for (Thread t : releasers) {
            t.join();
        }
        assertThat(releasers).as("non-vacuity: the hook ran and took the gate").hasSize(1);
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        assertThat(elapsedMs).as("the write waited for the exclusive holder (held %d ms)", holdMs)
            .isGreaterThan(1_000L);
    }

    @Test
    void aNewHandlerRouteCannotFailOpen_everyCallerPassesAGuard() throws Exception {
        OwnershipGuardCoverageScan.assertEveryGuardedRepositoryCallPassesAGuard();
    }

    @Test
    void noMainSourceOutsideTheRepositoryNamesTheReferenceOnlyWriter() throws Exception {
        OwnershipGuardCoverageScan.assertNoMainSourceOutsideTheRepositoryNamesTheReferenceOnlyWriter();
    }

    // ── 7. the log line ──────────────────────────────────────────────────────

    private List<String> captureRepositoryWarnings(java.util.concurrent.Callable<Void> body) throws Exception {
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(PgVectorRepository.class);
        var appender = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        appender.start();
        logger.addAppender(appender);
        try {
            body.call();
        } finally {
            logger.detachAppender(appender);
        }
        return appender.list.stream()
            .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
            .filter(m -> m.contains("ownerless_chunk_write_"))
            .toList();
    }

    @Test
    void theLogLineNamesTheClient_andAnAbsentVersionHeaderMeansAnOldClient() throws Exception {
        var lines = captureRepositoryWarnings(() -> {
            var body = Map.<String, Object>of("collection", COLLECTION_LOG, "ids", List.of(chash("owr-log-1")),
                "documents", List.of("t"), "metadatas", List.of(Map.of(
                    "source_path", "/p/a.py", "title", "a.py:1-1", "source_agent", "indexer", "source_uri", "file:///secret")));
            post(TOKEN, "/v1/vectors/upsert-chunks", body,
                Map.of("User-Agent", "nx-test-agent/9", "X-Nexus-Client-Version", "7.99.0"));
            return null;
        });
        assertThat(lines).hasSize(1);
        String line = lines.get(0);
        assertThat(line).contains("event=ownerless_chunk_write_refused")
            .contains("route=upsert-chunks").contains("collection=" + COLLECTION_LOG)
            .contains("phase=pre_embed").contains("unowned=1").contains("requested=1")
            .contains("user_agent=\"nx-test-agent/9\"").contains("client_version=\"7.99.0\"")
            .contains("suppressed_since_last=0")
            .contains("source_path=/p/a.py;").contains("title=a.py:1-1;").contains("source_agent=indexer;")
            .doesNotContain("file:///secret");

        // The limiter is keyed on route, tenant and collection: use another route to get a fresh line, header absent.
        var absent = captureRepositoryWarnings(() -> {
            post(TOKEN, "/v1/vectors/store-put", Map.of("collection", COLLECTION_LOG,
                "doc_id", chash("owr-log-2"), "content", "t", "metadata", Map.of()), Map.of("User-Agent", "old/1"));
            return null;
        });
        assertThat(absent).hasSize(1);
        assertThat(absent.get(0)).contains("route=store-put").contains("client_version=\"absent\"").contains("user_agent=\"old/1\"");
    }

    @Test
    void aLoopingClientLogsOncePerMinutePerRouteAndCollection_butEveryRequestIsCounted() throws Exception {
        var lines = captureRepositoryWarnings(() -> {
            for (int i = 0; i < 4; i++) {
                upsert(COLLECTION_LOOP, List.of(chash("owr-loop-" + i)), List.of("t"));
            }
            return null;
        });
        assertThat(lines).as("one WARN for four refused requests").hasSize(1);
        assertThat(OwnerlessWriteActivity.refusedTotal()).as("the counter is not limited").isEqualTo(4);
    }


    @Test
    void twoTenantsWithTheSameCollectionNameEachGetTheirOwnWarnLine() throws Exception {
        var lines = captureRepositoryWarnings(() -> {
            for (String token : List.of(TOKEN, TOKEN_2)) {
                post(token, "/v1/vectors/upsert-chunks", Map.of("collection", COLLECTION_SHARED_NAME,
                    "ids", List.of(chash("owr-shared-" + token)), "documents", List.of("t"),
                    "metadatas", List.of(Map.of())), Map.of());
            }
            return null;
        });
        assertThat(lines).as("one line per tenant, not one for the shared collection name").hasSize(2);
        assertThat(lines.stream().anyMatch(l -> l.contains("tenant=" + TENANT + " "))).isTrue();
        assertThat(lines.stream().anyMatch(l -> l.contains("tenant=" + TENANT_2 + " "))).isTrue();
    }

    @Test
    void theLoggedUserAgentAndClientVersionAreCutTo120Characters() throws Exception {
        String longUa = "ua-" + "u".repeat(300);
        String longVersion = "v-" + "9".repeat(300);
        var lines = captureRepositoryWarnings(() -> {
            post(TOKEN, "/v1/vectors/upsert-chunks", Map.of("collection", COLLECTION_CLIP,
                "ids", List.of(chash("owr-clip")), "documents", List.of("t"), "metadatas", List.of(Map.of())),
                Map.of("User-Agent", longUa, "X-Nexus-Client-Version", longVersion));
            return null;
        });
        assertThat(lines).hasSize(1);
        String line = lines.get(0);
        assertThat(line).contains("user_agent=\"" + longUa.substring(0, 120) + "\"")
            .doesNotContain(longUa.substring(0, 121))
            .contains("client_version=\"" + longVersion.substring(0, 120) + "\" ")
            .doesNotContain(longVersion.substring(0, 121));
    }

    @Test
    void aBadModeValueFailsServiceConstruction() throws Exception {
        OwnerlessWriteMode.setEnvReaderForTests(name -> OwnerlessWriteMode.ENV.equals(name) ? "off" : System.getenv(name));
        try {
            org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class,
                () -> new NexusService(0, TOKEN, svcDs, null, repo));
        } finally {
            OwnerlessWriteMode.setEnvReaderForTests(null);
        }
    }

    @Test
    void anUnsetModeBootsLogOnly_andAnExplicitEnforceBootsEnforce() throws Exception {
        OwnerlessWriteMode.setEnvReaderForTests(name -> null);
        NexusService unset = new NexusService(0, TOKEN, svcDs, null, repo);
        try {
            assertThat(unset.ownerlessWritePolicy().mode()).isEqualTo(OwnerlessWriteMode.LOG_ONLY);
        } finally {
            unset.stop();
            OwnerlessWriteMode.setEnvReaderForTests(null);
        }
        OwnerlessWriteMode.setEnvReaderForTests(name -> OwnerlessWriteMode.ENV.equals(name) ? "enforce" : null);
        NexusService enforce = new NexusService(0, TOKEN, svcDs, null, repo);
        try {
            assertThat(enforce.ownerlessWritePolicy().mode()).isEqualTo(OwnerlessWriteMode.ENFORCE);
        } finally {
            enforce.stop();
            OwnerlessWriteMode.setEnvReaderForTests(null);
        }
    }

    @Test
    void aClientVersionHeaderOrMetadataValueCannotForgeFieldsInTheLogLine() throws Exception {
        // client_version and user_agent are quoted, the metadata sits inside brackets; a value that
        // carries spaces, quotes or a closing bracket must stay inside its own delimiters.
        String version = "1.0\" tenant=evil suppressed_since_last=99 x=\"";
        var lines = captureRepositoryWarnings(() -> {
            post(TOKEN, "/v1/vectors/upsert-chunks", Map.of("collection", COLLECTION_FORGE,
                "ids", List.of(chash("owr-forge")), "documents", List.of("t"),
                "metadatas", List.of(Map.of("title", "x] tenant=evil2 [y", "source_path", "/p q"))),
                Map.of("User-Agent", "ua\" tenant=evil3 \"", "X-Nexus-Client-Version", version));
            return null;
        });
        assertThat(lines).hasSize(1);
        String line = lines.get(0);
        assertThat(line).contains("client_version=\"1.0' tenant=evil suppressed_since_last=99 x='\"");
        String unquoted = line.replaceAll("\"[^\"]*\"", "\"\"").replaceAll("\\[[^\\]]*\\]", "[]");
        assertThat(unquoted.split("tenant=", -1).length - 1)
            .as("the only tenant= field is the engine's own: %s", unquoted).isEqualTo(1);
        assertThat(unquoted.split("suppressed_since_last=", -1).length - 1).isEqualTo(1);
        assertThat(unquoted).contains("suppressed_since_last=0");
    }

    // ── 8. log-only on the paths the enforce tests already pin ───────────────

    /** Seed an OWNED chunk for {@code hex} in {@code collection} (one of the registered ones). */
    private void ownedIn(String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertOwnedChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, collection, 1024, hex);
        }
    }

    /** From the embed seam on: the chash loses its manifest row and its chunk row, as a concurrent re-index plus sweep would. */
    private void loseOwnerAndChunkDuringTheEmbed(String collection, String hex) {
        repo.setAfterNeedEmbedResolvedHookForTests(() -> {
            try (Connection su = pg.createConnection("")) {
                var dsl = DSL.using(su, SQLDialect.POSTGRES);
                dsl.deleteFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS)
                    .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT))
                    .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.CHASH.eq(
                        Chash.fromHex(hex).toBytes()))
                    .execute();
                var ch = dev.nexus.service.vectors.DimTables.CHUNKS.get(1024);
                dsl.deleteFrom(ch.table())
                    .where(ch.tenantId().eq(TENANT).and(ch.collection().eq(collection)).and(ch.chash().eq(hex)))
                    .execute();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
    }

    @Test
    @SuppressWarnings("unchecked")
    void logOnly_aChashThatLosesItsOwnerDuringTheEmbed_isCountedAsInTx_andTheWriteLands() throws Exception {
        service.ownerlessWritePolicy().set(OwnerlessWriteMode.LOG_ONLY);
        String h = chash("owr-logonly-in-tx");
        ownedIn(COLLECTION_INTX, h);
        loseOwnerAndChunkDuringTheEmbed(COLLECTION_INTX, h);
        HttpResponse<String>[] resp = new HttpResponse[1];
        var lines = captureRepositoryWarnings(() -> {
            resp[0] = upsert(COLLECTION_INTX, List.of(h), List.of("text that differs from the stored seed"));
            return null;
        });
        assertThat(resp[0].statusCode()).as("body: %s", resp[0].body()).isEqualTo(200);
        assertThat(embedder.calls.get()).as("the pre-embed check passed, so the embedder ran").isEqualTo(1);
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal()).as("the in-transaction recheck counted it").isEqualTo(1);
        assertThat(OwnerlessWriteActivity.refusedTotal()).isZero();
        assertThat(lines).hasSize(1);
        assertThat(lines.get(0)).contains("event=ownerless_chunk_write_would_refuse").contains("phase=in_tx");
        assertThat(physicalRow(TENANT, COLLECTION_INTX, h)).as("log-only writes it as today").isNotNull();
    }

    @Test
    void storePut_aChashThatLosesItsOwnerDuringTheEmbed_isRefusedInEnforce_andCountedAndLandsInLogOnly() throws Exception {
        // store-put reaches the same write transaction through putWithTokens: its in-tx recheck, both modes.
        String text = "owr-store-put-in-tx";
        String h = chash(text);
        ownedIn(COLLECTION_INTX_PUT, h);
        loseOwnerAndChunkDuringTheEmbed(COLLECTION_INTX_PUT, h);
        var enforce = post("/v1/vectors/store-put", Map.of("collection", COLLECTION_INTX_PUT,
            "doc_id", h, "content", "store-put text that differs from the stored seed", "metadata", Map.of()));
        assertNamesTheCombinedRoutes(enforce);
        assertThat(embedder.calls.get()).isEqualTo(1);
        assertThat(OwnerlessWriteActivity.refusedTotal()).isEqualTo(1);
        assertThat(physicalRow(TENANT, COLLECTION_INTX_PUT, h)).as("enforce: no chunk row was created").isNull();

        // The refusal rolled back; seed the chash owned again, lose it again, now in log-only.
        service.ownerlessWritePolicy().set(OwnerlessWriteMode.LOG_ONLY);
        ownedIn(COLLECTION_INTX_PUT, h);
        loseOwnerAndChunkDuringTheEmbed(COLLECTION_INTX_PUT, h);
        var logOnly = post("/v1/vectors/store-put", Map.of("collection", COLLECTION_INTX_PUT,
            "doc_id", h, "content", "store-put text that differs again", "metadata", Map.of()));
        assertThat(logOnly.statusCode()).as("body: %s", logOnly.body()).isEqualTo(200);
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal()).isEqualTo(1);
        assertThat(OwnerlessWriteActivity.refusedTotal()).as("unchanged since the enforce leg").isEqualTo(1);
        assertThat(physicalRow(TENANT, COLLECTION_INTX_PUT, h)).as("log-only: the write landed").isNotNull();
    }

    @Test
    void logOnly_forceReEmbedDoesNotBypassTheCheck_isCountedOnceAndLands() throws Exception {
        service.ownerlessWritePolicy().set(OwnerlessWriteMode.LOG_ONLY);
        String h = chash("owr-logonly-force");
        var resp = upsert(COLLECTION, List.of(h), List.of("text"), Map.of("force_re_embed", true));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal())
            .as("counted once: the pre-embed report suppresses the in-tx recount").isEqualTo(1);
        assertThat(embedder.calls.get()).as("log-only embeds as today").isEqualTo(1);
        assertThat(storedCount(h)).isEqualTo(1);
    }

    @Test
    void logOnly_suppliedEmbeddingsDoNotBypassTheCheck_areCountedAndLand() throws Exception {
        service.ownerlessWritePolicy().set(OwnerlessWriteMode.LOG_ONLY);
        String h = chash("owr-logonly-vectors");
        float[] v = new float[1024];
        v[2] = 1f;
        var resp = upsert(COLLECTION, List.of(h), List.of("text"), Map.of("embeddings", List.of(v)));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
        assertThat(OwnerlessWriteActivity.wouldRefuseTotal()).isEqualTo(1);
        assertThat(embedder.calls.get()).as("the supplied vector is used").isZero();
        assertThat(storedCount(h)).isEqualTo(1);
    }

    // ── 9. retired routes ────────────────────────────────────────────────────

    @Test
    void theRetiredStagingRoutesAreGone() throws Exception {
        // nexus-z0o2p.27 deleted StagingHandler with its schema. The engine answers an unregistered
        // path 404 whatever the method; a valid token is sent so a 401 cannot stand in for the answer.
        var routes = List.of(
            Map.entry("POST", "/v1/staging/load/chunks"), Map.entry("POST", "/v1/staging/embed_fill"),
            Map.entry("POST", "/v1/staging/promote"), Map.entry("POST", "/v1/staging/finalize"),
            Map.entry("POST", "/v1/staging/clear"), Map.entry("GET", "/v1/staging/counts"));
        for (var route : routes) {
            var builder = TestHttp.request("http://127.0.0.1:" + service.getPort() + route.getValue())
                .header("Authorization", "Bearer " + TOKEN).header("Content-Type", "application/json");
            var req = route.getKey().equals("GET")
                ? builder.GET().build()
                : builder.POST(HttpRequest.BodyPublishers.ofString("{}")).build();
            var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
            assertThat(resp.statusCode()).as("%s %s -> %s", route.getKey(), route.getValue(), resp.body()).isEqualTo(404);
        }
        // Non-vacuity: a live route on the same client, token and service answers.
        assertThat(get("/v1/status").statusCode()).isEqualTo(200);
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
