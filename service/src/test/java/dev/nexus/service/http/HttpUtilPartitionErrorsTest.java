// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.db.CollectionModelMismatchException;
import dev.nexus.service.db.ModelPartitions;
import dev.nexus.service.db.TenantCreationBusyException;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-225 (nexus-3wh8d.13): the typed responses of {@link HttpUtil#sendTypedDbError} for the partitioned-chunks
 * refusals. PostgreSQL's "no partition of relation found for row" shares SQLSTATE 23514 with the dimension CHECK,
 * so the mapping keys on the message: the first is an engine invariant (500 naming the tenant), the second a caller
 * error (409). A lock wait during token issuance is a retryable 503.
 */
class HttpUtilPartitionErrorsTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String NO_LEAF =
        "ERROR: no partition of relation \"chunks_m0a90d9bc\" found for row\n  Detail: Partition key of the failing row contains (tenant_id) = (acme).";
    private static final String NO_MODEL_PARTITION =
        "ERROR: no partition of relation \"chunks\" found for row\n  Detail: Partition key of the failing row contains (embedding_model, tenant_id) = (new-model, acme, corp).";
    private static final String DIMENSION_CHECK =
        "ERROR: new row for relation \"chunks_m0a90d9bc_t_7ec7c1dcb9736273\" violates check constraint \"chunks_m0a90d9bc_dimension_chk\"";

    private static CapturingExchange send(Throwable t) throws Exception {
        var ex = new CapturingExchange();
        boolean typed = HttpUtil.sendTypedDbError(ex, t, LoggerFactory.getLogger(HttpUtilPartitionErrorsTest.class), "test", "op=x");
        assertThat(typed).isTrue();
        return ex;
    }

    @Test
    void aLeafMissingForATenant_is500_namingTheTenant_notTheCaller409() throws Exception {
        var ex = send(new RuntimeException("write failed", new SQLException(NO_LEAF, "23514")));
        assertThat(ex.status).isEqualTo(500);
        JsonNode body = MAPPER.readTree(ex.body());
        assertThat(body.get("reason").asText()).isEqualTo("tenant_partition_missing");
        assertThat(body.get("tenant").asText()).isEqualTo("acme");
        assertThat(body.has("model")).isFalse();
        assertThat(body.get("error").asText()).contains("'acme'");
    }

    @Test
    void aModelPartitionMissing_is500_namingTheModelAndTheTenant_evenWhenAValueHasACommaInIt() throws Exception {
        var ex = send(new SQLException(NO_MODEL_PARTITION, "23514"));
        assertThat(ex.status).isEqualTo(500);
        JsonNode body = MAPPER.readTree(ex.body());
        assertThat(body.get("model").asText()).isEqualTo("new-model");
        assertThat(body.get("tenant").asText()).isEqualTo("acme, corp");
    }

    @Test
    void theDimensionCheck_staysTheCallerError409_withItsConstraintName() throws Exception {
        var ex = send(new SQLException(DIMENSION_CHECK, "23514"));
        assertThat(ex.status).isEqualTo(409);
        assertThat(ex.body()).contains("23514").contains("integrity constraint violation");
    }

    @Test
    void noPartitionMessage_isKeyedOnTheMessage_notOnTheSqlstateAlone() {
        assertThat(HttpUtil.noPartitionMessage(new SQLException(NO_LEAF, "23514"))).isNotNull();
        assertThat(HttpUtil.noPartitionMessage(new SQLException(DIMENSION_CHECK, "23514"))).isNull();
        assertThat(HttpUtil.noPartitionMessage(new SQLException(NO_LEAF, "23503"))).as("only 23514").isNull();
        assertThat(HttpUtil.partitionKeyOf("no detail here")).isEqualTo(new String[] {null, null});
    }

    @Test
    void aCrossModelRehome_is409_namingBothModelsAndBothCollections() throws Exception {
        var ex = send(new CollectionModelMismatchException("code__a__voyage-code-3__v1", "voyage-code-3",
            "docs__a__bge-base-en-v15-768__v1", "bge-base-en-v15-768"));
        assertThat(ex.status).isEqualTo(409);
        JsonNode body = MAPPER.readTree(ex.body());
        assertThat(body.get("reason").asText()).isEqualTo("collection_model_mismatch");
        assertThat(body.get("source_model").asText()).isEqualTo("voyage-code-3");
        assertThat(body.get("target_model").asText()).isEqualTo("bge-base-en-v15-768");
        assertThat(body.get("source_collection").asText()).isEqualTo("code__a__voyage-code-3__v1");
        assertThat(body.get("target_collection").asText()).isEqualTo("docs__a__bge-base-en-v15-768__v1");
        assertThat(body.get("error").asText()).contains("voyage-code-3").contains("bge-base-en-v15-768");
    }

    @Test
    void aLockWaitOnTokenIssuance_is503_withRetryAfter() throws Exception {
        var ex = send(new RuntimeException("insert failed", new TenantCreationBusyException("acme", 3,
            new SQLException("canceling statement due to statement timeout", "57014"))));
        assertThat(ex.status).isEqualTo(503);
        assertThat(ex.responseHeaders.getFirst("Retry-After")).isNotBlank();
        JsonNode body = MAPPER.readTree(ex.body());
        assertThat(body.get("reason").asText()).isEqualTo("tenant_creation_busy");
        assertThat(body.get("retry_after_seconds").asInt()).isPositive();
        assertThat(body.get("error").asText()).contains("acme");
    }

    @Test
    void aRegisteredModelWithNoPartition_is500_andAnUnregisteredOne_is422_bothNamingTheModel() throws Exception {
        // The exception's constructor is package-private to db; reach it through the public refusal path.
        var registered = refusal(true);
        var ex = send(registered);
        assertThat(ex.status).isEqualTo(500);
        JsonNode body = MAPPER.readTree(ex.body());
        assertThat(body.get("reason").asText()).isEqualTo("model_partition_missing");
        assertThat(body.get("model").asText()).isEqualTo("m-added-by-changeset");
        assertThat(body.get("error").asText()).contains("m-added-by-changeset").contains("create_model_partition");

        var unregistered = send(refusal(false));
        assertThat(unregistered.status).isEqualTo(422);
        JsonNode ub = MAPPER.readTree(unregistered.body());
        assertThat(ub.get("reason").asText()).isEqualTo("unregistered_embedding_model");
        assertThat(ub.get("model").asText()).isEqualTo("m-added-by-changeset");
    }

    /** A {@link ModelPartitions.ModelPartitionMissingException} for each case, built the way {@code ModelPartitions} builds it. */
    private static RuntimeException refusal(boolean registered) throws Exception {
        var ctor = ModelPartitions.ModelPartitionMissingException.class
            .getDeclaredConstructor(String.class, String.class, boolean.class);
        ctor.setAccessible(true);
        return ctor.newInstance("chunks", "m-added-by-changeset", registered);
    }

    private static final class CapturingExchange extends HttpExchange {
        final Headers responseHeaders = new Headers();
        final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        String body() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return URI.create("/x"); }
        @Override public String getRequestMethod() { return "POST"; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return new ByteArrayInputStream(new byte[0]); }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}
