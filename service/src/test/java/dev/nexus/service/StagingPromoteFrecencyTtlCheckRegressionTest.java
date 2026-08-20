// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
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
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tk070.p6b fix-pass (code-review IMPORTANT finding, 2026-08-20) —
 * regression test for {@code StagingPromoteOps.finalizeTenant}'s frecency
 * merge (~1030-1076): {@code GREATEST(FRECENCY.TTL_DAYS, gTd)} against an
 * EXISTING {@code nexus.frecency} row whose {@code ttl_days} is {@code NULL}
 * (the post-migration permanent sentinel, RDR-194 D5) merged with a STAGED
 * {@code staging.frecency} row carrying an explicit {@code ttl_days=0}
 * (staging carries no CHECK — 0 remains representable there even though
 * {@code nexus.frecency} retires it).
 *
 * <p>Postgres's {@code GREATEST} ignores {@code NULL} unless every argument
 * is {@code NULL} — so {@code GREATEST(NULL, 0) = 0}, and the subsequent
 * {@code UPDATE ... SET ttl_days = 0} attempt violates
 * {@code frecency_ttl_days_positive_chk}. This is SAFE (fails loud, not
 * silent corruption): the reviewer verified {@link
 * dev.nexus.service.http.StagingHandler}'s existing
 * {@code catch(Exception)->HttpUtil.sendTypedDbError} already turns this
 * into a 409 with the {@code frecency_ttl_days_positive_chk} remedy text
 * (the wildcard {@code %_ttl_days_positive_chk} match — see {@code
 * HttpUtil.ttlDaysCheckRemedy}'s own javadoc) — but that behavior had ZERO
 * test coverage before this pass. Pre-migration this combination was
 * {@code GREATEST(0, 0) = 0}, which silently succeeded (no CHECK existed
 * then) — this IS a genuine behavior change (silent-success ->
 * fail-loud-409), even though a desirable one; see the GREATEST call
 * site's own doc comment in {@code StagingPromoteOps.java} for the
 * asymmetry stated explicitly.
 *
 * <p>Standalone, dedicated container (not threaded into {@link
 * StagingHandlerJourneyTest}'s ordered, shared-state journey) — this
 * scenario needs only two seeded rows and one {@code /v1/staging/finalize}
 * call, no promote/embed_fill choreography.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class StagingPromoteFrecencyTtlCheckRegressionTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private static final String TOKEN  = "staging-frecency-check-token-0123456789ab";
    private static final String TENANT = "staging-frecency-check-tenant";
    private static final String CHUNK_ID = "ab".repeat(32);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    TenantScope scope;

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
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)))
                .update(new Contexts());
        }
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, 'staging-frecency-check') ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            ps.setString(1, TokenHashing.sha256Hex(TOKEN));
            ps.setString(2, TENANT);
            ps.executeUpdate();
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);

        // No live collection traffic in this scenario — a minimal fake
        // embedder satisfies EmbedderRouter/PgVectorRepository construction
        // without ever being invoked.
        var embedder = new dev.nexus.service.vectors.Embedder() {
            @Override public java.util.List<float[]> embed(java.util.List<String> texts) {
                throw new UnsupportedOperationException("not exercised by this scenario");
            }
            @Override public String modelToken() { return "voyage-context-3"; }
        };
        var router = new EmbedderRouter(embedder, "document");
        var pgRepo = new PgVectorRepository(scope, embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs, router, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();
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
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void finalize_existingNullMergedAgainstStagedZero_returns409WithRemedy() throws Exception {
        // Existing nexus.frecency row: ttl_days = NULL (post-migration
        // permanent sentinel — as if telemetry-006-1 already ran).
        scope.withTenant(TENANT, ctx -> {
            ctx.execute(
                "INSERT INTO nexus.frecency (tenant_id, chunk_id, ttl_days) VALUES (?, ?, NULL)",
                TENANT, CHUNK_ID);
            return null;
        });
        // Staged row, SAME chunk_id: ttl_days = 0 (staging carries no CHECK
        // — still representable there, the exact legacy shape this
        // scenario models).
        scope.withTenant(TENANT, ctx -> {
            ctx.execute(
                "INSERT INTO staging.frecency (tenant_id, chunk_id, ttl_days) VALUES (?, ?, 0)",
                TENANT, CHUNK_ID);
            return null;
        });

        var resp = post("/v1/staging/finalize", Map.of("orphan_policy", "drop"));

        assertThat(resp.statusCode())
            .as("GREATEST(NULL, 0) = 0 must violate frecency_ttl_days_positive_chk, "
                + "surfaced as a typed 409 (StagingHandler's existing sendTypedDbError "
                + "wildcard-remedy path) — never a silent write, never a bare 500. Body: %s",
                resp.body())
            .isEqualTo(409);

        Map<String, Object> body = MAPPER.readValue(resp.body(), MAP_TYPE);
        assertThat(body.get("constraint"))
            .as("the constraint name must be the frecency CHECK, not some other 23514")
            .isEqualTo("frecency_ttl_days_positive_chk");
        assertThat(String.valueOf(body.get("remedy")))
            .as("the remedy text must name the fix (HttpUtil.ttlDaysCheckRemedy's "
                + "wildcard match on the constraint name)")
            .contains("null")
            .contains("permanent");

        // Ground truth: the pre-existing NULL row must survive UNCHANGED —
        // a failed transaction must not leave a partial write behind.
        int stillNull = scope.withTenant(TENANT, ctx -> ctx.fetchOne(
            "SELECT count(*) FROM nexus.frecency WHERE tenant_id = '" + TENANT
            + "' AND chunk_id = '" + CHUNK_ID + "' AND ttl_days IS NULL")
            .get(0, Integer.class));
        assertThat(stillNull)
            .as("the failed merge must not have partially applied — the existing "
                + "row's NULL ttl_days must be untouched")
            .isEqualTo(1);
    }
}
