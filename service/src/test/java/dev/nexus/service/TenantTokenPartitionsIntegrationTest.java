// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantCreationBusyException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Clock;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.PartitionScratch.expectedName;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 Phase 2 Step 1 (nexus-3wh8d.13): a tenant exists from the moment its first token row is written, and
 * that INSERT creates the tenant's partition leaves through the {@code service_tokens} trigger. Every path that
 * writes a token row is exercised through its real entry point, on the production role ({@code nexus_svc}):
 * {@code POST /v1/tenants/create}, {@code POST /v1/service-tokens/issue}, {@code POST /v1/data-tokens/mint}, the
 * boot {@code ensureBootstrapToken} and a rotation. For each, the leaves exist under every model partition of both
 * parents, a write by the same role lands in the tenant's leaf, and a second token for the same tenant creates
 * nothing. The bound on a creation that waits on a lock (a retryable 503 after bounded attempts) is measured here
 * and its numbers go to {@code target/p225-evidence.txt} for the RDR.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TenantTokenPartitionsIntegrationTest {

    private static final String BOOT = "boot-p225-token-test";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String CODE_COLLECTION = "code__p225tok__voyage-code-3__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;
    int port;
    final HttpClient http = TestHttp.client();
    TenantScope tenantScope;
    PgVectorRepository vectors;
    CatalogRepository catalog;

    /** Deterministic 1024-wide vectors: the leaf test needs a real write, not a real embedding. */
    static final class FixedEmbedder implements Embedder {
        private final int dim;

        FixedEmbedder(int dim) {
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>();
            for (String t : texts) {
                float[] v = new float[dim];
                v[0] = 1.0f;
                v[1] = (t.hashCode() & 0xff) / 255f;
                out.add(v);
            }
            return out;
        }

        @Override
        public void close() {
        }
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), BOOT,
                TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL, TokenStore.SCOPE_ROOT, null, null);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        tenantScope = new TenantScope(ds);
        var embedder = new FixedEmbedder(1024);
        vectors = new PgVectorRepository(tenantScope, embedder, embedder);
        catalog = new CatalogRepository(tenantScope);
        service = new NexusService(0, BOOT, ds);
        service.start();
        port = service.getPort();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static DSLContext dsl(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    private HttpResponse<String> post(String bearer, String path, String body) throws Exception {
        HttpRequest req = TestHttp.request("http://127.0.0.1:" + port + path)
            .header("Authorization", "Bearer " + bearer)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode postOk(String bearer, String path, String body) throws Exception {
        var resp = post(bearer, path, body);
        assertThat(resp.statusCode()).as("POST %s -> %s", path, resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    /** The leaf names that exist for {@code tenant}, per parent and model partition; the expected set is every model. */
    private void assertLeavesExist(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            List<String> models = ctx.select(EMBEDDING_MODELS.EMBEDDING_MODEL).from(EMBEDDING_MODELS).fetch(0, String.class);
            assertThat(models).as("the four seeded models").hasSize(4);
            for (String parent : List.of("chunks", "taxonomy_centroids")) {
                for (String model : models) {
                    List<String> leaves = PartitionScratch.children(ctx, expectedName(parent, model, null)).stream()
                        .map(PartitionScratch.Child::name).toList();
                    assertThat(leaves).as("%s / %s leaves for %s", parent, model, tenant)
                        .contains(expectedName(parent, model, tenant));
                }
            }
        }
    }

    private TreeSet<String> allLeafNames() throws Exception {
        TreeSet<String> all = new TreeSet<>();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = dsl(su);
            for (String parent : List.of("chunks", "taxonomy_centroids")) {
                for (var mp : PartitionScratch.children(ctx, parent)) {
                    PartitionScratch.children(ctx, mp.name()).forEach(l -> all.add(l.name()));
                }
            }
        }
        return all;
    }

    /** A write as the production role into the tenant's leaf: register a collection, upsert a chunk, find where it landed. */
    private void assertWriteLandsInItsLeaf(String tenant, String suffix) throws Exception {
        String collection = "code__p225tok" + suffix + "__voyage-code-3__v1";
        catalog.upsertCollection(tenant, Map.of("name", collection, "content_type", "code",
            "owner_id", "p225", "embedding_model", "voyage-code-3"));
        String chash = String.format("%064x", Math.abs((tenant + suffix).hashCode()) + 1L);
        vectors.upsertChunks(tenant, collection, List.of(chash), List.of("text of " + tenant), List.of(Map.of()));
        try (Connection su = pg.createConnection("")) {
            var leaf = DSL.field(DSL.name("tableoid")).cast(org.jooq.impl.DefaultDataType.getDefaultDataType("regclass"))
                .cast(String.class);
            var row = dsl(su).select(CHUNKS.EMBEDDING_MODEL, leaf).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection)).fetchOne();
            assertThat(row).as("the chunk written for %s", tenant).isNotNull();
            assertThat(row.value1()).isEqualTo("voyage-code-3");
            assertThat(row.value2()).endsWith(expectedName("chunks", "voyage-code-3", tenant));
        }
    }

    // ── the three HTTP entry points ──────────────────────────────────────────

    @Test
    @Order(1)
    void tenantsCreate_createsTheLeaves_theWriteLands_andASecondTokenIsANoOp() throws Exception {
        String tenant = "p225-tok-create";
        assertThat(allLeafNames()).noneMatch(n -> n.endsWith(expectedName("chunks", "voyage-code-3", tenant)));
        postOk(BOOT, "/v1/tenants/create", "{\"name\":\"" + tenant + "\"}");
        assertLeavesExist(tenant);
        assertWriteLandsInItsLeaf(tenant, "a");

        TreeSet<String> before = allLeafNames();
        postOk(BOOT, "/v1/service-tokens/issue", "{\"tenant\":\"" + tenant + "\",\"label\":\"second\"}");
        assertThat(allLeafNames()).as("a second token for the same tenant creates nothing").isEqualTo(before);
    }

    @Test
    @Order(2)
    void serviceTokensIssue_createsTheLeavesOfANewTenant_andTheWriteLands() throws Exception {
        String tenant = "p225-tok-issue";
        postOk(BOOT, "/v1/service-tokens/issue", "{\"tenant\":\"" + tenant + "\"}");
        assertLeavesExist(tenant);
        assertWriteLandsInItsLeaf(tenant, "b");
    }

    @Test
    @Order(3)
    void dataTokensMint_createsTheLeavesOfANewTenant_andTheWriteLands() throws Exception {
        JsonNode mint = postOk(BOOT, "/v1/service-tokens/issue",
            "{\"tenant\":\"p225-tok-edge\",\"label\":\"edge\",\"scope\":\"mint\"}");
        String mintToken = mint.get("token").asText();
        assertLeavesExist("p225-tok-edge");

        String tenant = "p225-tok-mint";
        assertThat(allLeafNames()).noneMatch(n -> n.endsWith(expectedName("chunks", "voyage-code-3", tenant)));
        var resp = post(mintToken, "/v1/data-tokens/mint", "{\"tenant\":\"" + tenant + "\"}");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        assertLeavesExist(tenant);
        assertWriteLandsInItsLeaf(tenant, "c");

        TreeSet<String> before = allLeafNames();
        assertThat(post(mintToken, "/v1/data-tokens/mint", "{\"tenant\":\"" + tenant + "\"}").statusCode()).isEqualTo(200);
        assertThat(allLeafNames()).as("a second data token for the same tenant creates nothing").isEqualTo(before);
    }

    // ── rotation ─────────────────────────────────────────────────────────────

    @Test
    @Order(4)
    void rotation_ofAnExistingTenant_createsNothing_andOfANewOne_createsItsLeaves() throws Exception {
        postOk(BOOT, "/v1/service-tokens/issue", "{\"tenant\":\"p225-tok-rot\"}");
        TreeSet<String> before = allLeafNames();
        postOk(BOOT, "/v1/service-tokens/rotate", "{\"tenant\":\"p225-tok-rot\",\"grace_seconds\":300}");
        assertThat(allLeafNames()).as("rotating a tenant that has its leaves creates nothing").isEqualTo(before);
        assertWriteLandsInItsLeaf("p225-tok-rot", "d");

        // A rotation of a tenant that holds no token row inserts the replacement as that tenant's first one.
        String fresh = "p225-tok-rot-new";
        assertThat(allLeafNames()).noneMatch(n -> n.endsWith(expectedName("chunks", "voyage-code-3", fresh)));
        postOk(BOOT, "/v1/service-tokens/rotate", "{\"tenant\":\"" + fresh + "\",\"grace_seconds\":300}");
        assertLeavesExist(fresh);
        assertWriteLandsInItsLeaf(fresh, "e");
    }

    // ── healthy creation latency (measured for the RDR) ──────────────────────

    @Test
    @Order(5)
    void healthyTenantCreation_isFarInsideTheBound_measured() throws Exception {
        var store = new TokenStore(ds, Clock.systemUTC());
        List<Long> micros = new ArrayList<>();
        for (int i = 0; i < 40; i++) {
            long t0 = System.nanoTime();
            store.issueToken("p225-tok-lat-" + i, "lat", null);
            micros.add((System.nanoTime() - t0) / 1000);
        }
        Collections.sort(micros);
        long p50 = micros.get(micros.size() / 2) / 1000;
        long p95 = micros.get((int) (micros.size() * 0.95)) / 1000;
        long max = micros.get(micros.size() - 1) / 1000;
        PartitionScratch.evidence("TOKEN-INSERT healthy creation through TokenStore.issueToken on nexus_svc, 4 models (8 leaves per tenant), 40 new tenants after "
            + allLeafNames().size() / 8 + " tenants: p50 " + p50 + " ms, p95 " + p95 + " ms, max " + max + " ms");
        System.out.println("TOKEN_INSERT_LATENCY_MS p50=" + p50 + " p95=" + p95 + " max=" + max);
        assertThat(p50).as("a healthy creation takes tens of milliseconds").isLessThan(500);
        for (int i = 0; i < 40; i++) {
            assertLeavesExist("p225-tok-lat-" + i);
        }
    }

    // ── fresh install ────────────────────────────────────────────────────────

    @Test
    @Order(6)
    void freshInstall_theDefaultTenantsFirstWriteSucceeds_withoutAnyTokenCreatingLeaves() throws Exception {
        PostgreSQLContainer<?> c2 = PgContainerHelper.start();
        try {
            try (Connection su = c2.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(c2.getJdbcUrl());
            cfg.setUsername(PgContainerHelper.SVC_USERNAME);
            cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
            cfg.setMaximumPoolSize(4);
            cfg.setAutoCommit(true);
            try (HikariDataSource ds2 = new HikariDataSource(cfg)) {
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = dsl(su);
                    assertThat(ctx.fetchCount(SERVICE_TOKENS)).as("a fresh install has no token row yet").isZero();
                    for (String parent : List.of("chunks", "taxonomy_centroids")) {
                        for (var mp : PartitionScratch.children(ctx, parent)) {
                            assertThat(PartitionScratch.children(ctx, mp.name()).stream().map(PartitionScratch.Child::name))
                                .as("the migration made the default tenant's leaf under %s", mp.name())
                                .contains(expectedName(parent, modelOf(mp.bound()), "default"));
                        }
                    }
                }
                var scope = new TenantScope(ds2);
                var embedder = new FixedEmbedder(1024);
                new CatalogRepository(scope).upsertCollection("default", Map.of("name", CODE_COLLECTION,
                    "content_type", "code", "owner_id", "p225", "embedding_model", "voyage-code-3"));
                new PgVectorRepository(scope, embedder, embedder).upsertChunks("default", CODE_COLLECTION,
                    List.of("a".repeat(64)), List.of("first write"), List.of(Map.of()));
                try (Connection su = c2.createConnection("")) {
                    assertThat(dsl(su).fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq("default"))).isEqualTo(1);
                }
                // The boot path: ensureBootstrapToken for the default tenant inserts the root row and creates nothing.
                TreeSet<String> before = new TreeSet<>();
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = dsl(su);
                    PartitionScratch.children(ctx, "chunks").forEach(mp ->
                        PartitionScratch.children(ctx, mp.name()).forEach(l -> before.add(l.name())));
                }
                var store = new TokenStore(ds2, Clock.systemUTC());
                store.ensureBootstrapToken("boot-v1", TenantConstants.DEFAULT_TENANT);
                store.ensureBootstrapToken("boot-v2", TenantConstants.DEFAULT_TENANT);   // rotation: the UPDATE rebinding to default
                TreeSet<String> after = new TreeSet<>();
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = dsl(su);
                    PartitionScratch.children(ctx, "chunks").forEach(mp ->
                        PartitionScratch.children(ctx, mp.name()).forEach(l -> after.add(l.name())));
                    assertThat(dsl(su).fetchCount(SERVICE_TOKENS, SERVICE_TOKENS.LABEL.eq(TokenStore.ROOT_TOKEN_LABEL))).isEqualTo(1);
                }
                assertThat(after).as("the root token for the default tenant, issued and rotated, creates nothing").isEqualTo(before);
                // And the same call for a tenant with no leaves yet is a first token: it creates them.
                String fresh = "p225-boot-fresh";
                try (Connection su = c2.createConnection("")) {
                    dsl(su).deleteFrom(SERVICE_TOKENS).where(SERVICE_TOKENS.LABEL.eq(TokenStore.ROOT_TOKEN_LABEL)).execute();
                }
                store.ensureBootstrapToken("boot-v3", fresh);
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = dsl(su);
                    for (var mp : PartitionScratch.children(ctx, "chunks")) {
                        assertThat(PartitionScratch.children(ctx, mp.name()).stream().map(PartitionScratch.Child::name))
                            .contains(expectedName("chunks", modelOf(mp.bound()), fresh));
                    }
                }
            }
        } finally {
            c2.stop();
        }
    }

    private static String modelOf(String bound) {
        return bound.replaceAll("^FOR VALUES IN \\('(.*)'\\)$", "$1");
    }

    // ── a creation behind a lock: bounded, retried, then a retryable 503 (measured for the RDR) ──

    @Test
    @Order(7)
    void aCreationBehindALock_endsAsARetryable503_afterBoundedAttempts_holdsWritersNoLongerThanOneAttempt_andLeavesNothing() throws Exception {
        String tenant = "p225-tok-busy";
        String writerTenant = "p225-tok-writer";
        // The creation takes ACCESS EXCLUSIVE on each model partition in relname order. An open reader on the first
        // one (a transaction that has read it) makes the creation wait there. The writer below writes to that same
        // model partition, so it is the model of the first one by relname.
        String firstModelPartition;
        String firstModel;
        int firstDim;
        try (Connection su = pg.createConnection("")) {
            var first = PartitionScratch.children(dsl(su), "chunks").stream()
                .min(java.util.Comparator.comparing(PartitionScratch.Child::name)).orElseThrow();
            firstModelPartition = first.name();
            firstModel = modelOf(first.bound());
            firstDim = dsl(su).select(EMBEDDING_MODELS.DIMENSION).from(EMBEDDING_MODELS)
                .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(firstModel)).fetchOne(0, Integer.class);
        }
        // The writer's tenant has its leaves and a registered collection under that model before the blocker goes up.
        postOk(BOOT, "/v1/tenants/create", "{\"name\":\"" + writerTenant + "\"}");
        String writerCollection = "docs__p225writer__" + firstModel + "__v1";
        catalog.upsertCollection(writerTenant, Map.of("name", writerCollection, "content_type", "docs",
            "owner_id", "p225", "embedding_model", firstModel));
        var writerEmbedder = new FixedEmbedder(firstDim);
        var writerRepo = new PgVectorRepository(tenantScope, writerEmbedder, writerEmbedder);
        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            dsl(blocker).fetchCount(DSL.table(DSL.name("nexus", firstModelPartition)));   // ACCESS SHARE, held to the end of the transaction

            long t0 = System.nanoTime();
            CompletableFuture<HttpResponse<String>> creation = CompletableFuture.supplyAsync(() -> {
                try {
                    return post(BOOT, "/v1/tenants/create", "{\"name\":\"" + tenant + "\"}");
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            });

            // A writer to the SAME model partition, for another tenant, arriving while the creation is queued for its
            // ACCESS EXCLUSIVE lock behind the blocker: it queues behind the creation, and is released when that
            // attempt gives up (1 s), not at the 2 s lock timeout per acquisition.
            Thread.sleep(300);
            long w0 = System.nanoTime();
            String chash = "b".repeat(64);
            writerRepo.upsertChunks(writerTenant, writerCollection, List.of(chash), List.of("writer"), List.of(Map.of()));
            long writerMs = (System.nanoTime() - w0) / 1_000_000;

            HttpResponse<String> resp = creation.get(60, TimeUnit.SECONDS);
            long totalMs = (System.nanoTime() - t0) / 1_000_000;

            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(503);
            JsonNode body = MAPPER.readTree(resp.body());
            assertThat(body.get("reason").asText()).isEqualTo("tenant_creation_busy");
            assertThat(body.get("retry_after_seconds").asInt()).isPositive();
            assertThat(resp.headers().firstValue("Retry-After")).isPresent();
            assertThat(body.get("error").asText()).contains(tenant);

            PartitionScratch.evidence("TOKEN-INSERT bound (1 s statement_timeout, 3 attempts, 200 ms + jitter pause): a creation parked behind one open reader on the "
                + "first model partition ended as a 503 after " + totalMs + " ms; a writer to that model partition arriving 300 ms in waited " + writerMs + " ms");
            System.out.println("TOKEN_INSERT_BOUND total_ms=" + totalMs + " writer_wait_ms=" + writerMs);
            assertThat(totalMs).as("three 1 s attempts and two pauses").isBetween(2800L, 9000L);
            assertThat(writerMs).as("the writer is let through when the first attempt gives up, not held for the whole creation")
                .isLessThan(2500L);

            try (Connection su = pg.createConnection("")) {
                assertThat(dsl(su).fetchCount(SERVICE_TOKENS, SERVICE_TOKENS.TENANT_ID.eq(tenant)))
                    .as("nothing was issued").isZero();
                assertThat(allLeafNames()).noneMatch(n -> n.endsWith(expectedName("chunks", "voyage-code-3", tenant)));
            }
            blocker.rollback();
        }
        // With the blocker gone the same request succeeds.
        postOk(BOOT, "/v1/tenants/create", "{\"name\":\"" + tenant + "\"}");
        assertLeavesExist(tenant);
    }

    @Test
    @Order(8)
    void theStoreThrowsTenantCreationBusy_whenEveryAttemptWaits_andACustomBoundIsHonoured() throws Exception {
        String tenant = "p225-tok-busy-store";
        String firstModelPartition;
        try (Connection su = pg.createConnection("")) {
            firstModelPartition = PartitionScratch.children(dsl(su), "chunks").stream()
                .map(PartitionScratch.Child::name).sorted().findFirst().orElseThrow();
        }
        var store = new TokenStore(ds, Clock.systemUTC(), new TokenStore.TenantCreationBound(
            java.time.Duration.ofMillis(250), 2, java.time.Duration.ofMillis(10)));
        try (Connection blocker = pg.createConnection("")) {
            blocker.setAutoCommit(false);
            dsl(blocker).fetchCount(DSL.table(DSL.name("nexus", firstModelPartition)));
            long t0 = System.nanoTime();
            assertThatThrownBy(() -> store.issueToken(tenant, "x", null))
                .isInstanceOfSatisfying(TenantCreationBusyException.class, e -> {
                    assertThat(e.tenant()).isEqualTo(tenant);
                    assertThat(e.attempts()).isEqualTo(2);
                    assertThat(e.getCause()).hasStackTraceContaining("statement timeout");
                });
            long ms = (System.nanoTime() - t0) / 1_000_000;
            assertThat(ms).as("two 250 ms attempts and a short pause").isBetween(450L, 3000L);
            blocker.rollback();
        }
        // The same store succeeds once the lock is gone.
        store.issueToken(tenant, "x", null);
        assertLeavesExist(tenant);
    }
}
