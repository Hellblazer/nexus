// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.PgContainerHelper;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.sql.Connection;
import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-225 (nexus-3wh8d.10 M3): {@link HttpUtil}'s no-partition mapping against the message PostgreSQL itself
 * produces at each level of a model-then-tenant partition tree.
 *
 * <p>The unit test's fixtures are strings someone wrote; one of them (a model-level failure carrying a tenant)
 * is a shape PostgreSQL cannot produce, because a failure at a level reports that level's key only. This test
 * builds the same two-level layout as {@code nexus.chunks} (LIST by model, each model partition LIST by tenant),
 * makes PostgreSQL refuse a row at each level, and checks what the mapping makes of the real text.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HttpUtilPartitionErrorsPgTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, "CREATE SCHEMA p225_part_errs");
            PgContainerHelper.runSuperuserDdl(su, "CREATE TABLE p225_part_errs.t (embedding_model text NOT NULL,"
                + " tenant_id text NOT NULL, v int) PARTITION BY LIST (embedding_model)");
            PgContainerHelper.runSuperuserDdl(su, "CREATE TABLE p225_part_errs.t_m1 PARTITION OF p225_part_errs.t"
                + " FOR VALUES IN ('m1') PARTITION BY LIST (tenant_id)");
            PgContainerHelper.runSuperuserDdl(su, "CREATE TABLE p225_part_errs.t_m1_a PARTITION OF p225_part_errs.t_m1"
                + " FOR VALUES IN ('acme')");
        }
    }

    @AfterAll
    void stop() throws Exception {
        if (pg != null) {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.runSuperuserDdl(su, "DROP SCHEMA p225_part_errs CASCADE");
            }
            pg.stop();
        }
    }

    /** What PostgreSQL itself says when a row fits no partition: the driver's own exception, unwrapped from jOOQ. */
    private SQLException refused(String model, String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES)
                .insertInto(DSL.table(DSL.name("p225_part_errs", "t")),
                    DSL.field(DSL.name("embedding_model"), String.class), DSL.field(DSL.name("tenant_id"), String.class),
                    DSL.field(DSL.name("v"), Integer.class))
                .values(model, tenant, 1).execute();
        } catch (DataAccessException e) {
            return (SQLException) e.getCause();
        }
        throw new AssertionError("PostgreSQL accepted the row (" + model + ", " + tenant + ")");
    }

    @Test
    void aModelLevelFailure_reportsOnlyTheModel_andIsMappedTo500NamingIt() throws Exception {
        SQLException real = refused("no-such-model", "acme");
        assertThat(real.getSQLState()).isEqualTo("23514");
        assertThat(real.getMessage())
            .as("a failure at the model level reports that level's key only")
            .contains("Partition key of the failing row contains (embedding_model) = (no-such-model)")
            .doesNotContain("tenant_id");

        assertThat(HttpUtil.noPartitionMessage(real)).isNotNull();
        // keyed[0] == null && keyed[1] != null: the branch the hand-written fixture never reached.
        assertThat(HttpUtil.partitionKeyOf(real.getMessage())).isEqualTo(new String[] {null, "no-such-model"});

        var ex = new CapturingExchange("POST", URI.create("/x"), "");
        assertThat(HttpUtil.sendTypedDbError(ex, real, LoggerFactory.getLogger(getClass()), "test", "op=x")).isTrue();
        assertThat(ex.status).isEqualTo(500);
        JsonNode body = MAPPER.readTree(ex.bodyString());
        assertThat(body.get("reason").asText()).isEqualTo("tenant_partition_missing");
        assertThat(body.get("model").asText()).isEqualTo("no-such-model");
        assertThat(body.has("tenant")).as("the server did not report a tenant, so none is named").isFalse();
        assertThat(body.get("error").asText())
            .contains("(not reported by the server)").contains("embedding model 'no-such-model'");
    }

    @Test
    void aLeafLevelFailure_reportsOnlyTheTenant_andIsMappedTo500NamingIt() throws Exception {
        SQLException real = refused("m1", "no-such-tenant");
        assertThat(real.getSQLState()).isEqualTo("23514");
        assertThat(real.getMessage())
            .contains("Partition key of the failing row contains (tenant_id) = (no-such-tenant)")
            .doesNotContain("embedding_model");

        assertThat(HttpUtil.partitionKeyOf(real.getMessage())).isEqualTo(new String[] {"no-such-tenant", null});

        var ex = new CapturingExchange("POST", URI.create("/x"), "");
        assertThat(HttpUtil.sendTypedDbError(ex, real, LoggerFactory.getLogger(getClass()), "test", "op=x")).isTrue();
        assertThat(ex.status).isEqualTo(500);
        JsonNode body = MAPPER.readTree(ex.bodyString());
        assertThat(body.get("tenant").asText()).isEqualTo("no-such-tenant");
        assertThat(body.has("model")).isFalse();
    }
}
