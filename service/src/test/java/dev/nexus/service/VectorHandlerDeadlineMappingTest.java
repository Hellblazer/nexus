// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.RequestDeadlineExceededException;
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
 * nexus-8hdg9 phase 2 (write-path cancellation on client disconnect) --
 * HTTP-boundary contract for {@link RequestDeadlineExceededException}: it
 * must map to 503, not the generic 500 arm, and -- unlike {@code
 * VoyageTooManyTokensException} (422, deliberately OUTSIDE the client's
 * gateway-retry ladder) -- 503 IS one of the client's {@code
 * _GATEWAY_RETRY_CODES} {502,503,504}, since a request that ran out of its
 * own deadline is an honest slow-server signal the client should retry, not
 * a guaranteed-to-fail-again oversize body. A {@code Retry-After} header +
 * {@code retry_after_seconds} body field (critique remediation, T2 {@code
 * critique-nexus-8hdg9-p2-5ce59b36d} [24651] finding 1) mirror {@code
 * VectorHandlerUpstreamRateLimitedTest}'s 429 arm exactly, so the client's
 * gateway retry paces itself instead of firing three more full-cost embeds.
 *
 * <p>Mirrors {@code CatalogHandlerCollectionCountsTest}'s converted bootstrap
 * (Testcontainers PG, {@link PgContainerHelper#applyProductSchema} +
 * {@link PgContainerHelper#bootstrapServiceRole} + {@link
 * PgContainerHelper#seedServiceToken} -- Sam's no-raw-SQL-strings-in-Java
 * directive, nexus-zrcj7/nexus-cbo4a) with a stub {@link Embedder} that
 * throws the typed exception directly, {@code PgVectorRepository} injected
 * via the 5-arg {@link NexusService} overload, port 0, {@code PER_CLASS} --
 * this phase adds only the exception and its mapping, no embed-loop check
 * point raises it yet (phases 3/4, nexus-8hdg9.3/.4), so a stub embedder
 * throwing directly is the smallest fixture that exercises the real {@code
 * VectorHandler.handle} catch chain end-to-end over HTTP.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerDeadlineMappingTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN    = "tok-rdln-test-0123456789abcdef00000000";
    private static final String SVC_ROLE = "svc_rdln";
    private static final String SVC_PASS = "svc_rdln_pass";
    private static final String TENANT   = "rdln-tenant";
    private static final String COLLECTION = "code__rdln__voyage-code-3__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "rdln-test");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        // Stub embedder: always throws the typed exception directly -- VectorHandler's
        // catch chain is under test, not a real embed-loop check point (those are
        // phases 3/4, out of scope for this phase).
        var embedder = new ThrowingEmbedder();
        var pgRepo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);

        service = new NexusService(0, TOKEN, svcDs, null, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();

        // RDR-204 Phase 1 (bead nexus-ft04v.3): AuthFilter runs the per-tenant,
        // once-per-process ghost sweep on whichever request is TENANT's first
        // against this service instance, and DELETES any collection it finds
        // registered-but-chunkless at that moment. The warmup MUST run before the
        // collection is registered at all (measured, nexus-ft04v Phase 1 round 4:
        // registering first and warming up second let the warmup ITSELF trigger
        // the sweep and delete the row it had just registered). Burn the sweep
        // here first, THEN register (RDR-204 Phase 2, bead nexus-ft04v.16:
        // dimForCollection now requires a real row before the stub embedder is
        // ever reached).
        var warmup = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list"))
            .header("Authorization", "Bearer " + TOKEN)
            .GET().build();
        http.send(warmup, HttpResponse.BodyHandlers.ofString());

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, COLLECTION);
            // RDR-223 P3.1 (nexus-z0o2p.23): the engine refuses an ownerless write on
            // upsert-chunks from Phase 3, and this class tests the embed-failure mapping, not
            // the ownership rule. Each chash the tests POST is therefore owned up front, and the
            // POSTs carry force_re_embed so the (throwing) embedder is still reached for it.
            PgContainerHelper.insertOwnedChunks(dsl, TENANT, COLLECTION, 1024,
                Chash.ofText("rdln-c1").toHex(), Chash.ofText("rdln-c2").toHex(),
                Chash.ofText("rdln-c3").toHex());
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    private HttpResponse<String> post(String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static final long SIMULATED_RETRY_AFTER_SECONDS = 5L;

    @Test
    void upsertChunks_requestDeadlineExceeded_maps503_neverOpaque500() throws Exception {
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", COLLECTION,
            "ids",        List.of(Chash.ofText("rdln-c1").toHex()),
            "documents",  List.of("chunk that will simulate an expired write-path deadline"),
            "metadatas",  List.of(Map.of()),
            "force_re_embed", true));

        assertThat(resp.statusCode())
            .as("must be 503 (an honest slow-server signal, one of the client's"
                + " _GATEWAY_RETRY_CODES) -- never the generic 500 arm (got body: %s)",
                resp.body())
            .isEqualTo(503);
        assertThat(resp.headers().firstValue("Retry-After"))
            .as("Retry-After paces the client's gateway retry instead of letting it fire"
                + " three more full-cost embeds against an already-loaded server")
            .contains(Long.toString(SIMULATED_RETRY_AFTER_SECONDS));

        @SuppressWarnings("unchecked")
        Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
        assertThat((String) body.get("error"))
            .as("the typed detail must reach the caller, not just the engine log")
            .contains("rdln-simulated-deadline-exceeded");
        assertThat(((Number) body.get("retry_after_seconds")).longValue())
            .isEqualTo(SIMULATED_RETRY_AFTER_SECONDS);
        // nexus-qajw7: the default outcome is aborted, which the client does not widen for.
        assertThat(resp.headers().firstValue("X-Nexus-Deadline-Outcome")).contains("aborted");
        assertThat(body.get("deadline_outcome")).isEqualTo("aborted");
    }

    @Test
    void anAdmissionRefusalSaysSoOnTheWire() throws Exception {
        // nexus-qajw7: a refusal (nothing embedded) is marked so the client can keep the
        // wider retry budget for it; an abort (embedded work discarded) is not.
        ThrowingEmbedder.outcome = RequestDeadlineExceededException.Outcome.REFUSED;
        try {
            var resp = post("/v1/vectors/upsert-chunks", Map.of(
                "collection", COLLECTION,
                "ids",        List.of(Chash.ofText("rdln-c2").toHex()),
                "documents",  List.of("chunk refused before any embedding"),
                "metadatas",  List.of(Map.of()),
                "force_re_embed", true));
            assertThat(resp.statusCode()).isEqualTo(503);
            assertThat(resp.headers().firstValue("X-Nexus-Deadline-Outcome")).contains("refused");
            @SuppressWarnings("unchecked")
            Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
            assertThat(body.get("deadline_outcome")).isEqualTo("refused");
        } finally {
            ThrowingEmbedder.outcome = RequestDeadlineExceededException.Outcome.ABORTED;
        }
    }

    @Test
    void aShutdownRefusalMaps503_notTheIllegalState422() throws Exception {
        // nexus-o5xyx.3: the ORT gate refuses a run once process exit has begun. The
        // exception extends IllegalStateException, whose arm answers 422, which the
        // client does not retry; the refusal must reach the wire as a retryable 503.
        ThrowingEmbedder.override = new dev.nexus.service.vectors.OrtInitGate.ShutdownInProgressException(
            "refusing to start native model init 'bge768-run': shutdown in progress");
        try {
            var resp = post("/v1/vectors/upsert-chunks", Map.of(
                "collection", COLLECTION,
                "ids",        List.of(Chash.ofText("rdln-c3").toHex()),
                "documents",  List.of("chunk refused because the engine is exiting"),
                "metadatas",  List.of(Map.of()),
                "force_re_embed", true));
            assertThat(resp.statusCode())
                .as("shutdown refusal must be 503, not 422 (body: %s)", resp.body())
                .isEqualTo(503);
            @SuppressWarnings("unchecked")
            Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
            assertThat((String) body.get("error")).contains("shutdown in progress");
        } finally {
            ThrowingEmbedder.override = null;
        }
    }

    @Test
    void everyEmbeddingHandlerMapsAShutdownRefusalTo503AheadOfItsGenericArms() throws Exception {
        // nexus-o5xyx.3 review: write_many (CatalogHandler, via CombinedWriteService)
        // embeds too (embed_fill's StagingHandler was retired at nexus-z0o2p.27, so
        // its row left this list with it). Static pin, as for the 429/deadline arms
        // (VectorHandlerUpstreamRateLimitedTest's proportionality argument); the live-HTTP
        // proof of the shape is the VectorHandler test above.
        String arm = "catch (dev.nexus.service.vectors.OrtInitGate.ShutdownInProgressException";
        for (String handler : List.of("VectorHandler", "CatalogHandler")) {
            String src = java.nio.file.Files.readString(java.nio.file.Path.of(
                "src", "main", "java", "dev", "nexus", "service", "http", handler + ".java"));
            int armIdx = src.indexOf(arm);
            assertThat(armIdx).as("%s must catch ShutdownInProgressException", handler).isPositive();
            assertThat(src.substring(armIdx, Math.min(src.length(), armIdx + 700)))
                .as("%s's arm must answer 503", handler).contains("503");
            int genericIdx = src.indexOf("catch (Exception e)", armIdx);
            assertThat(genericIdx).as("%s: arm ahead of the generic 500 arm", handler).isGreaterThan(armIdx);
            int iseIdx = src.indexOf("catch (IllegalStateException");
            if (iseIdx >= 0) {
                assertThat(armIdx).as("%s: arm ahead of the IllegalStateException arm it would fall into", handler)
                    .isLessThan(iseIdx);
            }
        }
    }

    /** Always throws the typed exception, simulating an expired write-path deadline. */
    private static final class ThrowingEmbedder implements Embedder {
        static volatile RequestDeadlineExceededException.Outcome outcome =
            RequestDeadlineExceededException.Outcome.ABORTED;
        /** When set, thrown instead of the deadline exception. */
        static volatile RuntimeException override;

        @Override
        public String modelToken() {
            return "voyage-code-3";
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            throw simulated();
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            throw simulated();
        }

        private static RuntimeException simulated() {
            RuntimeException o = override;
            if (o != null) return o;
            return new RequestDeadlineExceededException(
                "rdln-simulated-deadline-exceeded: write-path deadline elapsed mid-embed",
                SIMULATED_RETRY_AFTER_SECONDS, outcome);
        }
    }
}
