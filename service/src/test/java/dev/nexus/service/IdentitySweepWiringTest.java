// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.tuples.TemplateRegistry;
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
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_TENANTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): identity retention runs in the engine's scheduled tuple sweep, the one task the
 * RDR names for it. An ephemeral identity that ended 31 days ago is tombstoned by {@code runScheduledTupleSweep};
 * a live one and a seat are left alone.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentitySweepWiringTest {

    private static final String BOOT = "boot-identity-sweep-test";
    private static final String TENANT_TOKEN = "identity-sweep-tenant-token";
    private static final String TENANT = "identity-sweep-tenant";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;
    int port;
    final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, BOOT, TenantConstants.DEFAULT_TENANT,
                TokenStore.ROOT_TOKEN_LABEL, TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, TENANT_TOKEN, TENANT, "tenant", TokenStore.SCOPE_TENANT, null, null);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        service = new NexusService(0, BOOT, ds, null, null, null, null, registry);
        service.start();
        port = service.getPort();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> call(String method, String path, Object body, int expect) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + port + "/v1/identity" + path)
            .header("Authorization", "Bearer " + TENANT_TOKEN).header("Content-Type", "application/json");
        var req = "GET".equals(method) ? b.GET().build()
            : b.method(method, HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build();
        HttpResponse<String> r = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).as("%s %s: %s", method, path, r.body()).isEqualTo(expect);
        return MAPPER.readValue(r.body(), Map.class);
    }

    private static Map<String, Object> bind(String session, int pid) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("session_id", session);
        m.put("host", "h1");
        m.put("pid", pid);
        m.put("started_at", 5000L + pid);
        m.put("proc_start", "2026-11-16T10:00:00Z");
        return m;
    }

    @Test
    void theScheduledTupleSweepRunsIdentityRetentionForTheTenantsItVisits() throws Exception {
        String old = (String) call("POST", "", Map.of("kind", "ephemeral", "created_by", "session",
            "bind", bind("old-1", 1)), 200).get("id");
        String live = (String) call("POST", "", Map.of("kind", "ephemeral", "created_by", "session",
            "bind", bind("live-1", 2)), 200).get("id");
        String seat = (String) call("POST", "", Map.of("kind", "seat", "name", "sweepseat", "created_by", "sam"), 200).get("id");
        call("POST", "/" + old + "/bind", Map.of("event", "end", "expected_generation", 1), 200);

        // The ended one finished 31 days ago; the seat ended 400 days ago, which a seat's permanence ignores.
        TenantScope scope = new TenantScope(ds);
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        scope.withTenant(TENANT, ctx -> ctx.update(IDENTITY_RECORDS).set(IDENTITY_RECORDS.ENDED_AT, now.minusDays(31))
            .where(IDENTITY_RECORDS.TENANT_ID.eq(TENANT)).and(IDENTITY_RECORDS.ID.eq(old)).execute());
        scope.withTenant(TENANT, ctx -> ctx.update(IDENTITY_RECORDS).set(IDENTITY_RECORDS.STATE, "ended")
            .set(IDENTITY_RECORDS.ENDED_AT, now.minusDays(400))
            .where(IDENTITY_RECORDS.TENANT_ID.eq(TENANT)).and(IDENTITY_RECORDS.ID.eq(seat)).execute());
        // The sweep visits the tenants of nexus.tuple_tenants.
        DSL.using(ds, SQLDialect.POSTGRES).insertInto(TUPLE_TENANTS)
            .set(TUPLE_TENANTS.TENANT_ID, TENANT).set(TUPLE_TENANTS.FIRST_SEEN, now).set(TUPLE_TENANTS.LAST_SEEN, now)
            .onConflictDoNothing().execute();

        NexusService.TupleSweepRunResult result = service.runScheduledTupleSweep(now);
        assertThat(result.tenantsVisited()).as("non-vacuity: the sweep visited the tenant").isGreaterThanOrEqualTo(1);
        assertThat(result.incompleteCause()).isEqualTo(NexusService.TupleSweepIncompleteCause.NONE);

        call("GET", "/" + old, null, 404);
        call("GET", "/" + live, null, 200);
        assertThat(call("GET", "/" + seat, null, 200)).containsEntry("kind", "seat");
    }
}
