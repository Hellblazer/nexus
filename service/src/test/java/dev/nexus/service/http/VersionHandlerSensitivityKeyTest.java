// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.EmbeddingProfile;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.lang.reflect.Proxy;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.SQLException;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-rfvyy.6.5 (RDR-243): {@code GET /version} advertises {@code catalog_sensitivity_supported: true}.
 *
 * <p>Two pins, because a helper that nothing calls would satisfy the first alone: the fragment the helper appends
 * (the exact string, the {@code vector_search_chash_allow_supported} precedent in
 * {@code VersionHandlerReleaseVersionTest}), and the key on the body the REAL handler serves, which pins the call
 * site. Hermetic: a bare {@link HttpServer} bound to the handler, no Postgres; the handler's schema read goes to a
 * pool that refuses, which this class does not assert on.
 */
class VersionHandlerSensitivityKeyTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private HttpServer server;
    private EmbedderRouter router;

    @AfterEach
    void stop() {
        if (server != null) server.stop(0);
        if (router != null) router.close();
    }

    @Test
    void theHelperAppendsTheExactFragment() {
        var body = new StringBuilder();
        VersionHandler.appendCatalogSensitivityCapabilityField(body);
        assertThat(body.toString()).isEqualTo(",\"catalog_sensitivity_supported\":true");
    }

    @Test
    void theRealHandlerServesTheKeyAsABooleanTrue() throws Exception {
        Embedder local = new Embedder() {
            @Override public String modelToken() { return "bge-base-en-v15-768"; }
            @Override public List<float[]> embed(List<String> texts) { return List.of(); }
        };
        router = new EmbedderRouter(local, "pa-key", "document", EmbeddingProfile.VOYAGE);
        DataSource refusingPool = (DataSource) Proxy.newProxyInstance(
            VersionHandlerSensitivityKeyTest.class.getClassLoader(), new Class<?>[] {DataSource.class},
            (proxy, method, args) -> {
                if (method.getName().equals("getConnection")) throw new SQLException("no pool in this test");
                throw new UnsupportedOperationException(method.getName());
            });
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/version", new VersionHandler(refusingPool, router));
        server.start();
        HttpResponse<String> resp = HttpClient.newHttpClient().send(HttpRequest.newBuilder(
            URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/version")).GET().build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        JsonNode body = MAPPER.readTree(resp.body());
        assertThat(body.has("catalog_sensitivity_supported")).as("the key is present: %s", resp.body()).isTrue();
        assertThat(body.get("catalog_sensitivity_supported").isBoolean()).isTrue();
        assertThat(body.get("catalog_sensitivity_supported").asBoolean()).isTrue();
        assertThat(body.get("vector_search_chash_allow_supported").asBoolean())
            .as("its sibling is still served beside it").isTrue();
    }
}
