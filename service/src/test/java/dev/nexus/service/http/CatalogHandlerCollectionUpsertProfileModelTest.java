// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 1 (bead nexus-ft04v.8) — {@code
 * CatalogHandler#handleCollectionUpsert} takes a NEW collection's {@code
 * embedding_model} from the tenant's current {@code nexus.embedding_profile}
 * row for the request's {@code content_type} (Technical Design step 2): no
 * model named gets the profile's model, the profile's model named is
 * accepted, a DIFFERENT model named is refused with a 422 naming the
 * profile's value. An EXISTING row is never re-pointed by the profile
 * (Technical Design 1a): a differing model on an existing row is refused the
 * same way, but naming the ROW's own value instead. New rows also get {@code
 * lifecycle_state} ({@code live} unless the {@code quarantine-} prefix
 * convention applies) and {@code dimension} from {@code
 * nexus.embedding_models}; {@code model_version} keeps round-tripping
 * exactly as sent.
 *
 * <p>Same HTTP-level idiom as {@code
 * CatalogHandlerCollectionUpsertProfileSeedTest}: driven directly via {@link
 * CatalogHandler#handle} with a capturing {@link HttpExchange}, a
 * voyage-mode {@link EmbedderRouter} (dummy key — no embed call happens on
 * this path) wired through a real {@link CombinedWriteService}.
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerCollectionUpsertProfileModelTest {

    private static final String SVC_ROLE = "svc_profile_model_http_test";
    private static final String SVC_PASS = "svc_profile_model_http_test_pass";

    /** A stand-in for Bge768Embedder that needs no model file (mirrors EmbedderRouterEmbeddingProfileSeedTest's FakeBge). */
    private static final class FakeBge implements Embedder {
        @Override public List<float[]> embed(List<String> texts) {
            return texts.stream().map(t -> new float[768]).toList();
        }
        @Override public String modelToken() {
            return "bge-base-en-v15-768";
        }
    }

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler handler;
    CatalogHandler onnxHandler;
    com.zaxxer.hikari.HikariDataSource svcDs;

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

        // Voyage-mode, no local ONNX -- the production cloud shape
        // (nexus-0n7uc). Dummy key: this path never calls embed(), only
        // modelToken() reads.
        EmbedderRouter router = new EmbedderRouter("dummy-key", "document");
        var combinedWriteService = new CombinedWriteService(tenantScope, repo, router);
        handler = new CatalogHandler(repo, combinedWriteService);

        // ONNX-local mode -- the other live mode (no Voyage key). Both modes
        // share the same repo/tenantScope (RLS keeps every test's tenant
        // isolated); only the router differs.
        EmbedderRouter onnxRouter = new EmbedderRouter(new FakeBge(), "document");
        var onnxCombinedWriteService = new CombinedWriteService(tenantScope, repo, onnxRouter);
        onnxHandler = new CatalogHandler(repo, onnxCombinedWriteService);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private record CollectionRow(String embeddingModel, Integer dimension, String lifecycleState,
                                  String modelVersion) {}

    private CollectionRow collectionRow(String tenant, String name) {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var r = ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                                CATALOG_COLLECTIONS.LIFECYCLE_STATE, CATALOG_COLLECTIONS.MODEL_VERSION)
                    .from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                    .and(CATALOG_COLLECTIONS.NAME.eq(name))
                    .fetchOne();
            if (r == null) return null;
            return new CollectionRow(r.value1(), r.value2(), r.value3(), r.value4());
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private boolean profileRowExists(String tenant, String contentType) {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            return ctx.fetchExists(ctx.selectFrom(EMBEDDING_PROFILE)
                    .where(EMBEDDING_PROFILE.TENANT_ID.eq(tenant))
                    .and(EMBEDDING_PROFILE.CONTENT_TYPE.eq(contentType)));
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    // ── (b) + tenant-with-no-profile-row: seeded first, then registration proceeds ──

    @Test
    void newCollection_noModelNamed_getsProfileModelDimensionAndLiveState() throws Exception {
        String tenant = "profile-model-tenant-1";
        String name = "code__" + tenant + "__voyage-code-3__v1";
        assertThat(profileRowExists(tenant, "code"))
            .as("guard: no profile row exists yet for this fresh tenant").isFalse();

        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"model_version\":\"v1\"}");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);

        assertThat(profileRowExists(tenant, "code"))
            .as("the profile row must be seeded lazily before registration proceeds").isTrue();

        var row = collectionRow(tenant, name);
        assertThat(row).isNotNull();
        assertThat(row.embeddingModel()).as("no model named -> the profile's model").isEqualTo("voyage-code-3");
        assertThat(row.dimension()).isEqualTo(1024);
        assertThat(row.lifecycleState()).isEqualTo("live");
        assertThat(row.modelVersion()).as("model_version round-trips exactly as sent").isEqualTo("v1");
    }

    // ── (c) naming the profile's model is accepted ──────────────────────────

    @Test
    void newCollection_namingProfileModel_accepted() throws Exception {
        String tenant = "profile-model-tenant-2";
        String name = "code__" + tenant + "__voyage-code-3__v1";

        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-code-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);

        var row = collectionRow(tenant, name);
        assertThat(row).isNotNull();
        assertThat(row.embeddingModel()).isEqualTo("voyage-code-3");
        assertThat(row.dimension()).isEqualTo(1024);
        assertThat(row.lifecycleState()).isEqualTo("live");
    }

    // ── (d) naming a DIFFERENT model is refused: 422 naming the profile's value ──

    @Test
    void newCollection_namingADifferentModel_refused422NamingProfileValue() throws Exception {
        String tenant = "profile-model-tenant-3";
        String name = "code__" + tenant + "__voyage-context-3__v1";

        // The seed fires from THIS request's own content_type ("code") before the
        // registration decision, regardless of what embedding_model the request
        // itself names -- the router's own mapping seeds "code" -> voyage-code-3.
        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-context-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(ex, tenant);

        assertThat(ex.status).isEqualTo(422);
        assertThat(ex.bodyString())
            .as("the 422 body must name BOTH the profile's value and the value that was sent")
            .contains("voyage-code-3")
            .contains("voyage-context-3")
            .contains("code");

        assertThat(collectionRow(tenant, name))
            .as("a refused registration must not leave a row behind").isNull();
    }

    // ── (e) an EXISTING row is never re-pointed by the profile ──────────────

    @Test
    void existingRow_reRegisterSameModel_accepted() throws Exception {
        String tenant = "profile-model-tenant-4";
        String name = "code__" + tenant + "__voyage-code-3__v1";

        CapturingExchange first = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-code-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(first, tenant);
        assertThat(first.status).isEqualTo(200);

        CapturingExchange second = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-code-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(second, tenant);
        assertThat(second.status).as(second.bodyString()).isEqualTo(200);

        var row = collectionRow(tenant, name);
        assertThat(row.embeddingModel()).isEqualTo("voyage-code-3");
    }

    @Test
    void existingRow_reRegisterDifferentModel_refused422NamingTheRowsOwnValue() throws Exception {
        String tenant = "profile-model-tenant-5";
        String name = "code__" + tenant + "__voyage-code-3__v1";

        CapturingExchange first = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-code-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(first, tenant);
        assertThat(first.status).isEqualTo(200);

        // The profile for "code" is voyage-code-3 too (same router), so this proves
        // the refusal names the EXISTING ROW's value, not merely the profile's --
        // both happen to agree here, so the distinguishing assertion is the message
        // shape ("already registered") rather than the specific model token. A
        // second scenario below drives the profile and the row apart to make the
        // distinction unambiguous.
        CapturingExchange second = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-context-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(second, tenant);

        assertThat(second.status).isEqualTo(422);
        assertThat(second.bodyString())
            .as("must name the row's own model and the rejected value")
            .contains("already registered")
            .contains("voyage-code-3")
            .contains("voyage-context-3");

        assertThat(collectionRow(tenant, name).embeddingModel())
            .as("the existing row's model must be untouched by the refused re-registration")
            .isEqualTo("voyage-code-3");
    }

    @Test
    void existingRow_neverRepointedEvenAfterProfileChanges() throws Exception {
        // Drives the profile and an existing row's model APART: register under the
        // CURRENT profile, then directly overwrite the profile row (simulating a
        // later `nx config set` + restart re-seed, GH #1461), then re-register the
        // SAME collection naming the OLD (row's own) model again -- must still be
        // ACCEPTED (RDR-204 1a: existing rows are never re-pointed), even though it
        // now disagrees with the (changed) profile.
        String tenant = "profile-model-tenant-6";
        String name = "code__" + tenant + "__voyage-code-3__v1";

        CapturingExchange first = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"model_version\":\"v1\"}");
        handleWithTenant(first, tenant);
        assertThat(first.status).isEqualTo(200);
        assertThat(collectionRow(tenant, name).embeddingModel()).isEqualTo("voyage-code-3");

        // Simulate the profile moving on (a later mode/config switch) directly --
        // this bead does not touch the profile-write path (bead .6), only reads it.
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.update(EMBEDDING_PROFILE)
               .set(EMBEDDING_PROFILE.EMBEDDING_MODEL, "voyage-context-3")
               .set(EMBEDDING_PROFILE.DIMENSION, 1024)
               .where(EMBEDDING_PROFILE.TENANT_ID.eq(tenant))
               .and(EMBEDDING_PROFILE.CONTENT_TYPE.eq("code"))
               .execute();
        }

        // Re-registering the SAME name with its OWN (now stale-vs-profile) model
        // must still succeed -- an existing row is authoritative over the profile.
        CapturingExchange second = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-code-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(second, tenant);
        assertThat(second.status).as(second.bodyString()).isEqualTo(200);
        assertThat(collectionRow(tenant, name).embeddingModel())
            .as("existing row keeps its own model even after the profile moved on")
            .isEqualTo("voyage-code-3");
    }

    // ── (f) quarantine-prefixed name -> lifecycle_state='quarantine' ────────

    @Test
    void newCollection_quarantinePrefixedName_getsQuarantineLifecycleState() throws Exception {
        String tenant = "profile-model-tenant-7";
        String name = "quarantine-code__" + tenant + "__voyage-code-3__v1";

        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"model_version\":\"v1\"}");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);

        var row = collectionRow(tenant, name);
        assertThat(row).isNotNull();
        assertThat(row.lifecycleState()).isEqualTo("quarantine");
        // Still gets a real model from the profile -- quarantine is orthogonal to
        // model resolution.
        assertThat(row.embeddingModel()).isEqualTo("voyage-code-3");
    }

    @Test
    void newCollection_ordinaryName_getsLiveLifecycleState() throws Exception {
        String tenant = "profile-model-tenant-8";
        String name = "code__" + tenant + "__voyage-code-3__v1";

        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"model_version\":\"v1\"}");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).isEqualTo(200);

        assertThat(collectionRow(tenant, name).lifecycleState()).isEqualTo("live");
    }

    // ── (g) model_version round-trips exactly as sent ───────────────────────

    @Test
    void modelVersion_roundTripsExactlyAsSent() throws Exception {
        String tenant = "profile-model-tenant-9";
        String name = "code__" + tenant + "__voyage-code-3__v7";

        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"model_version\":\"v7\"}");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).isEqualTo(200);

        assertThat(collectionRow(tenant, name).modelVersion()).isEqualTo("v7");
    }

    // ── unmapped content_type: free-form on the client, NEVER a 400 ─────────
    //
    // Coordinator-reported regression (2026-09-07), found by the primary's
    // Python suite against .2+.6+.3: an unmapped content_type used to make
    // EmbedderRouter#seedEmbeddingProfileForContentType throw
    // IllegalArgumentException, which — once the seed moved ahead of the
    // registration decision — turned into a 400 for a request that used to
    // succeed. Content types are free-form on the client (tests register
    // "prose"; quarantine-<ct> and others exist in production), so an
    // unmapped content type must instead fall back to the mode's CCE bucket
    // token (the same token "unknown" gets) and registration must proceed —
    // the 422 exists ONLY for a request naming a model that disagrees with
    // the (possibly-fallback) profile, never for the content_type itself.
    // Exercised in BOTH modes, per the coordinator's ask — a run that
    // covered only one mode would be a vacuous pass.

    @Test
    void unmappedContentType_prose_getsCCEFallbackModel_profileSeeded_voyageMode() throws Exception {
        String tenant = "profile-model-tenant-11-voyage";
        String name = "prose__" + tenant + "__voyage-context-3__v1";
        assertThat(profileRowExists(tenant, "prose")).as("guard: no profile row yet").isFalse();

        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"prose\","
            + "\"owner_id\":\"" + tenant + "\",\"model_version\":\"v1\"}");
        handleWithTenant(handler, ex, tenant);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);

        assertThat(profileRowExists(tenant, "prose"))
            .as("an unmapped content type still gets a seeded profile row (the CCE fallback)")
            .isTrue();

        var row = collectionRow(tenant, name);
        assertThat(row).isNotNull();
        assertThat(row.embeddingModel())
            .as("no model named -- the CCE fallback token, voyage-context-3 in Voyage mode")
            .isEqualTo("voyage-context-3");
        assertThat(row.dimension()).isEqualTo(1024);
        assertThat(row.lifecycleState())
            .as("\"prose\" does not start with quarantine- -> live")
            .isEqualTo("live");
    }

    @Test
    void unmappedContentType_prose_getsCCEFallbackModel_profileSeeded_onnxMode() throws Exception {
        String tenant = "profile-model-tenant-11-onnx";
        String name = "prose__" + tenant + "__bge-base-en-v15-768__v1";
        assertThat(profileRowExists(tenant, "prose")).as("guard: no profile row yet").isFalse();

        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"" + name + "\",\"content_type\":\"prose\","
            + "\"owner_id\":\"" + tenant + "\",\"model_version\":\"v1\"}");
        handleWithTenant(onnxHandler, ex, tenant);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);

        assertThat(profileRowExists(tenant, "prose"))
            .as("an unmapped content type still gets a seeded profile row (the CCE fallback)")
            .isTrue();

        var row = collectionRow(tenant, name);
        assertThat(row).isNotNull();
        assertThat(row.embeddingModel())
            .as("no model named -- the CCE fallback token, bge-768 in ONNX mode")
            .isEqualTo("bge-base-en-v15-768");
        assertThat(row.dimension()).isEqualTo(768);
        assertThat(row.lifecycleState())
            .as("\"prose\" does not start with quarantine- -> live")
            .isEqualTo("live");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void handleWithTenant(CapturingExchange ex, String tenant) throws Exception {
        handleWithTenant(handler, ex, tenant);
    }

    private void handleWithTenant(CatalogHandler h, CapturingExchange ex, String tenant) throws Exception {
        RequestContext.set(new RequestContext.Principal(tenant, null, false, false, "tenant", "test-credential-hash"));
        try {
            h.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private static CapturingExchange post(String path, String jsonBody) {
        return new CapturingExchange("POST", URI.create(path), jsonBody);
    }

    /** Minimal {@link HttpExchange} that captures the response status + body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return requestBody; }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public com.sun.net.httpserver.HttpPrincipal getPrincipal() { return null; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
    }
}
