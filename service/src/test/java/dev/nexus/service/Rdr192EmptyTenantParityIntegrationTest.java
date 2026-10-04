// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.InputStream;
import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wbfpw.76 -- the engine half of the empty-tenant parity pin.
 *
 * <p>{@link ReaperRepository#holdsNothing} is the engine's empty-tenant test behind
 * {@code Rdr192BackfillGate} (nexus-wbfpw.73). The client rung's convergence test
 * ({@code _default_census} plus {@code _cross_check_empty_listing}) is the other definition of
 * "nothing to do", and the engine's must never call a tenant empty where the client's census would
 * not converge. The probe cannot be reached per tenant from Python ({@code reaper.last_pass.tenants_empty}
 * counts every tenant a pass visits), so each side is pinned to ONE shape table,
 * {@code parity/rdr192_empty_tenant_shapes.json}: this test seeds each shape into real Postgres and
 * asserts the table's {@code engine_empty}; {@code tests/upgrade/test_rdr192_empty_tenant_parity.py}
 * seeds the same shapes through the engine substrate and asserts {@code client_census_clean}, and
 * checks the invariant {@code engine_empty => client_census_clean} over the table.
 *
 * <p>The table's {@code documents} column matters to the client side only: the probe reads
 * {@code nexus.chunks}, and a manifest row needs its chunk through the validated foreign key. This
 * side seeds {@code owned-with-manifest} with an owned chunk anyway so the shape is the one the
 * client sees.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr192EmptyTenantParityIntegrationTest {

    private static final String SVC_ROLE = "svc_rdr192_parity_test";
    private static final String SVC_PASS = "svc_rdr192_parity_test_pass";
    private static final Duration BOUND = Duration.ofSeconds(25);
    private static final String TABLE = "/parity/rdr192_empty_tenant_shapes.json";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ReaperRepository reaperStore;

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
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        reaperStore = new ReaperRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static JsonNode shapes() throws Exception {
        try (InputStream in = Rdr192EmptyTenantParityIntegrationTest.class.getResourceAsStream(TABLE)) {
            assertThat(in).as("the shared shape table " + TABLE).isNotNull();
            return new ObjectMapper().readTree(in).get("shapes");
        }
    }

    /** Seed one shape under its own tenant, as the superuser, and return the tenant. */
    private String seed(JsonNode shape) throws Exception {
        String name = shape.get("name").asText();
        String tenant = "rdr192-parity-" + name;
        String documents = shape.get("documents").asText();
        int i = 0;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (JsonNode c : shape.get("collections")) {
                String prefix = c.get("state").asText().equals("quarantine") ? "quarantine-" : "";
                String collection = prefix + "knowledge__" + tenant + i++ + "__minilm-l6-v2-384__v1";
                PgContainerHelper.insertCollection(ctx, tenant, collection);
                int chunks = c.get("chunks").asInt();
                List<String> hex = new ArrayList<>();
                for (int k = 0; k < chunks; k++) hex.add(Chash.ofText(collection + "/" + k).toHex());
                if (chunks == 0) continue;
                if (documents.equals("owned-with-manifest")) {
                    PgContainerHelper.insertOwnedChunks(ctx, tenant, collection, 384, hex.toArray(new String[0]));
                } else {
                    PgContainerHelper.insertChunks(ctx, tenant, collection, hex,
                        hex.stream().map(h -> "text").toList(),
                        hex.stream().map(h -> new float[384]).toList(),
                        hex.stream().map(h -> Map.<String, Object>of()).toList());
                }
            }
        }
        return tenant;
    }

    @Test
    void theTableCoversTheShapesTheBeadNames() throws Exception {
        List<String> names = new ArrayList<>();
        shapes().forEach(s -> names.add(s.get("name").asText()));
        assertThat(names).contains("empty", "chunk-only", "quarantine-only",
            "registered-collection-no-chunks", "content-owned-with-manifest");
    }

    @TestFactory
    Stream<DynamicTest> theEngineProbeReadsEachShapeAsTheTableSays() throws Exception {
        List<DynamicTest> tests = new ArrayList<>();
        for (JsonNode shape : shapes()) {
            tests.add(DynamicTest.dynamicTest(shape.get("name").asText(), () -> {
                String tenant = seed(shape);
                boolean expected = shape.get("engine_empty").asBoolean();
                assertThat(reaperStore.holdsNothing(tenant, BOUND))
                    .as("holdsNothing(%s)", shape.get("name").asText())
                    .isEqualTo(expected);
                // The parity direction: an engine "empty" must be a client-clean census.
                if (expected) {
                    assertThat(shape.get("client_census_clean").asBoolean())
                        .as("engine calls %s empty, so the client census must converge on it", shape.get("name").asText())
                        .isTrue();
                }
            }));
        }
        return tests.stream();
    }
}
