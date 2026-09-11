// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-169 Phase B fix round 1, item 4 (T2 test-validation-nexus-22vvy-rdr169-
 * phase-b-2026-09-11 coverage gap 1): HTTP-boundary contract for
 * {@code POST /v1/vectors/upsert-reference-only} -- {@link
 * dev.nexus.service.http.VectorHandler#handleUpsertReferenceOnlyChunk}. No
 * test anywhere in the tree previously drove this route over real HTTP
 * (only the repository-level {@link ReferenceOnlyChunkUpsertTest} called
 * {@code PgVectorRepository#upsertReferenceOnlyChunk} directly).
 *
 * <p>Mirrors {@code VectorHandlerDeadlineMappingTest}'s converted bootstrap
 * (Testcontainers PG, {@link PgContainerHelper#applyProductSchema} + {@link
 * PgContainerHelper#bootstrapServiceRole} + {@link
 * PgContainerHelper#seedServiceToken} -- Sam's no-raw-SQL-strings-in-Java
 * directive, nexus-zrcj7/nexus-cbo4a), {@code PgVectorRepository} injected via
 * the 5-arg {@link NexusService} overload, port 0, {@code PER_CLASS}. Two
 * tenants (two bearer tokens, one shared service role/datasource -- RLS,
 * not connection separation, is what is under test for isolation).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerUpsertReferenceOnlyTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN_A  = "tok-uro-tenant-a-0123456789abcdef000000";
    private static final String TOKEN_B  = "tok-uro-tenant-b-0123456789abcdef000000";
    private static final String SVC_ROLE = "svc_uro";
    private static final String SVC_PASS = "svc_uro_pass";
    private static final String TENANT_A = "uro-tenant-a";
    private static final String TENANT_B = "uro-tenant-b";
    private static final String COLLECTION = "knowledge__uro-owner__voyage-context-3__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    PgVectorRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN_A, TENANT_A, "uro-test-a");
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN_B, TENANT_B, "uro-test-b");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        FakeEmbedder embedder = new FakeEmbedder(1024);
        repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);

        service = new NexusService(0, TOKEN_A, svcDs, null, repo);
        service.start();
        http = HttpClient.newHttpClient();

        // RDR-204 Phase 1 (bead nexus-ft04v.3): burn the per-tenant ghost sweep on
        // each tenant's FIRST request before registering any collection (measured
        // ordering trap, see VectorHandlerDeadlineMappingTest's identical comment).
        for (String token : List.of(TOKEN_A, TOKEN_B)) {
            var warmup = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list"))
                .header("Authorization", "Bearer " + token)
                .GET().build();
            http.send(warmup, HttpResponse.BodyHandlers.ofString());
        }

        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLLECTION);
            PgContainerHelper.insertCollection(ctx, TENANT_B, COLLECTION);
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    private HttpResponse<String> post(String token, String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonBody(HttpResponse<String> resp) throws Exception {
        return MAPPER.readValue(resp.body(), Map.class);
    }

    // -------------------------------------------------------------------------
    // Malformed / missing embedding -> 400 naming the field
    // -------------------------------------------------------------------------

    @Test
    void missingEmbedding_returns400_namingTheField() throws Exception {
        String chash = Chash.ofText("uro-missing-embedding").toHex();
        var resp = post(TOKEN_A, "/v1/vectors/upsert-reference-only", Map.of(
            "collection", COLLECTION,
            "chash",      chash));
        // no "embedding" key at all

        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat((String) jsonBody(resp).get("error"))
            .as("the 400 body must name the missing field")
            .contains("embedding");
    }

    @Test
    void nonNumericEmbedding_returns400_namingTheField() throws Exception {
        String chash = Chash.ofText("uro-nonnumeric-embedding").toHex();
        var resp = post(TOKEN_A, "/v1/vectors/upsert-reference-only", Map.of(
            "collection", COLLECTION,
            "chash",      chash,
            "embedding",  List.of("not", "a", "number")));

        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat((String) jsonBody(resp).get("error"))
            .as("the 400 body must name the malformed field")
            .contains("embedding");
    }

    // -------------------------------------------------------------------------
    // full -> reference-only rejection, over real HTTP
    // -------------------------------------------------------------------------

    @Test
    void fullToReferenceOnly_overHttp_maps422_namingTheChash() throws Exception {
        String chash = Chash.ofText("uro-full-content").toHex();
        repo.upsertChunks(TENANT_A, COLLECTION,
            List.of(chash), List.of("full content seeded for the HTTP guard test"),
            List.of(Map.of()));

        var resp = post(TOKEN_A, "/v1/vectors/upsert-reference-only", Map.of(
            "collection", COLLECTION,
            "chash",      chash,
            "embedding",  floatList(FakeEmbedder.unitVector(1024, 1.0f, 0.0f))));

        assertThat(resp.statusCode())
            .as("the full->reference-only guard's IllegalStateException maps through "
                + "VectorHandler's shared 'well-formed but rejected' arm to 422, not a "
                + "generic 500 (got body: %s)", resp.body())
            .isEqualTo(422);
        assertThat((String) jsonBody(resp).get("error"))
            .as("the 422 body must name the chash the guard rejected")
            .contains(chash)
            .contains("full→reference-only transition is prohibited");
    }

    // -------------------------------------------------------------------------
    // Tenant isolation: tenant A cannot see or clobber tenant B's row
    // -------------------------------------------------------------------------

    @Test
    void tenantA_cannotTouchTenantBsRow() throws Exception {
        String sharedChash = Chash.ofText("uro-cross-tenant-chash").toHex();

        // Tenant B writes FULL content at this (collection, chash).
        repo.upsertChunks(TENANT_B, COLLECTION,
            List.of(sharedChash), List.of("tenant B's private full content"),
            List.of(Map.of()));

        // Tenant A submits a reference-only write at the SAME (collection, chash).
        // RLS scopes the guard SELECT to tenant A alone -- tenant B's row must be
        // invisible to it, so tenant A's write succeeds (no false-positive
        // full->reference-only rejection borrowed from another tenant's data).
        var resp = post(TOKEN_A, "/v1/vectors/upsert-reference-only", Map.of(
            "collection", COLLECTION,
            "chash",      sharedChash,
            "embedding",  floatList(FakeEmbedder.unitVector(1024, 0.0f, 1.0f))));

        assertThat(resp.statusCode())
            .as("tenant A's write must succeed -- RLS must not let tenant B's full "
                + "row leak into tenant A's guard SELECT (got body: %s)", resp.body())
            .isEqualTo(200);

        // Tenant B's own row must be completely untouched by tenant A's write.
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var ch = dev.nexus.service.vectors.DimTables.CHUNKS.get(1024);
            String tenantBContent = ctx.select(ch.chunkText()).from(ch.table())
                .where(ch.tenantId().eq(TENANT_B).and(ch.chash().eq(sharedChash)))
                .fetchOne(ch.chunkText());
            assertThat(tenantBContent)
                .as("tenant B's full content must be completely unaffected by "
                    + "tenant A's reference-only write to the same (collection, chash)")
                .isEqualTo("tenant B's private full content");
        }
    }

    private static List<Double> floatList(float[] vec) {
        List<Double> out = new java.util.ArrayList<>(vec.length);
        for (float f : vec) out.add((double) f);
        return out;
    }
}
