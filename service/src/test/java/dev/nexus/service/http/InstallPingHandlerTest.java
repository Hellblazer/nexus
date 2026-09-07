// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.db.InstallPingRepository;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-h5olw — {@code POST /v1/install-ping}. Hermetic: bare {@link HttpServer}
 * on port 0, a recording sink, no Postgres.
 */
class InstallPingHandlerTest {

    private static final String VALID = """
        {"install_id":"%s","client_version":"7.35.0","mode":"local",
         "os":"darwin","arch":"arm64","python":"3.12"}""";

    private final List<InstallPingRepository.Ping> recorded = new CopyOnWriteArrayList<>();
    private HttpServer server;
    private String url;
    private final HttpClient http = HttpClient.newHttpClient();

    @BeforeEach
    void start() throws Exception {
        start(0);
    }

    private void start(int trustedProxies) throws Exception {
        if (server != null) server.stop(0);
        Clock fixed = Clock.fixed(Instant.parse("2026-09-07T12:00:00Z"), ZoneOffset.UTC);
        // Generous limiter so only the explicit rate-limit tests trip it.
        var limiter = new MintRateLimiter(fixed, 5, 1, 100);
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/install-ping", new InstallPingHandler(recorded::add, limiter, trustedProxies));
        server.start();
        url = "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/install-ping";
    }

    @AfterEach
    void stop() {
        server.stop(0);
    }

    private HttpResponse<String> post(String body) throws Exception {
        return post(body, null);
    }

    private HttpResponse<String> post(String body, String xff) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(url))
                .POST(HttpRequest.BodyPublishers.ofString(body));
        if (xff != null) b.header("X-Forwarded-For", xff);
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void validPing_is202_andRecordedVerbatim() throws Exception {
        UUID id = UUID.randomUUID();
        HttpResponse<String> r = post(VALID.formatted(id));
        assertThat(r.statusCode()).isEqualTo(202);
        assertThat(r.body()).isEqualTo("{\"ok\":true}");
        assertThat(recorded).containsExactly(
                new InstallPingRepository.Ping(id, "7.35.0", "local", "darwin", "arm64", "3.12"));
    }

    @Test
    void get_is405() throws Exception {
        HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(url)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).isEqualTo(405);
        assertThat(recorded).isEmpty();
    }

    @Test
    void malformedJson_is400() throws Exception {
        assertThat(post("{not json").statusCode()).isEqualTo(400);
        assertThat(recorded).isEmpty();
    }

    @Test
    void missingField_is400() throws Exception {
        String body = "{\"install_id\":\"" + UUID.randomUUID() + "\",\"client_version\":\"7.35.0\",\"mode\":\"local\"}";
        assertThat(post(body).statusCode()).isEqualTo(400);
        assertThat(recorded).isEmpty();
    }

    @Test
    void nonUuidInstallId_is400() throws Exception {
        assertThat(post(VALID.formatted("not-a-uuid")).statusCode()).isEqualTo(400);
        assertThat(recorded).isEmpty();
    }

    @Test
    void unknownMode_is400() throws Exception {
        String body = VALID.formatted(UUID.randomUUID()).replace("\"local\"", "\"hybrid\"");
        assertThat(post(body).statusCode()).isEqualTo(400);
        assertThat(recorded).isEmpty();
    }

    @Test
    void overlongField_is400() throws Exception {
        String body = VALID.formatted(UUID.randomUUID()).replace("darwin", "x".repeat(65));
        assertThat(post(body).statusCode()).isEqualTo(400);
        assertThat(recorded).isEmpty();
    }

    @Test
    void oversizedBody_is400() throws Exception {
        String body = VALID.formatted(UUID.randomUUID()).replace("}", ",\"pad\":\"" + "y".repeat(2048) + "\"}");
        assertThat(post(body).statusCode()).isEqualTo(400);
        assertThat(recorded).isEmpty();
    }

    @Test
    void sameInstallIdPastBurst_is429_withRetryAfter() throws Exception {
        UUID id = UUID.randomUUID();
        for (int i = 0; i < 5; i++) {
            assertThat(post(VALID.formatted(id)).statusCode()).as("ping %d", i).isEqualTo(202);
        }
        HttpResponse<String> r = post(VALID.formatted(id));
        assertThat(r.statusCode()).isEqualTo(429);
        assertThat(r.headers().firstValue("Retry-After")).contains("60");
        assertThat(recorded).hasSize(5);
    }

    @Test
    void trustedProxiesZero_ignoresForwardedFor_keysOnSocketPeer() throws Exception {
        // Default posture (bare engine): the header is untrusted. Every request
        // here comes from 127.0.0.1, so varying X-Forwarded-For must not buy
        // fresh per-address budget (100/min shared by one peer).
        for (int i = 0; i < 100; i++) {
            assertThat(post(VALID.formatted(UUID.randomUUID()), "203.0.113." + (i % 250)).statusCode())
                    .as("ping %d", i).isEqualTo(202);
        }
        assertThat(post(VALID.formatted(UUID.randomUUID()), "198.51.100.7").statusCode()).isEqualTo(429);
    }

    @Test
    void trustedProxiesOne_keysOnHopBeforeTheEdge() throws Exception {
        // Managed-edge shape (conexus-n5n8): engine sees "client-ip, edge-ip".
        start(1);
        for (int i = 0; i < 100; i++) {
            assertThat(post(VALID.formatted(UUID.randomUUID()), "203.0.113.5, 10.0.0.1").statusCode())
                    .as("ping %d", i).isEqualTo(202);
        }
        // Same client behind the same edge: budget spent.
        assertThat(post(VALID.formatted(UUID.randomUUID()), "203.0.113.5, 10.0.0.1").statusCode())
                .isEqualTo(429);
        // A different client behind the same edge is a different key.
        assertThat(post(VALID.formatted(UUID.randomUUID()), "203.0.113.6, 10.0.0.1").statusCode())
                .isEqualTo(202);
        // Too few hops for the count: socket peer, never an exception.
        assertThat(post(VALID.formatted(UUID.randomUUID()), "10.0.0.1").statusCode()).isEqualTo(202);
    }

    @Test
    void sinkFailure_is500_notSwallowed() throws Exception {
        server.removeContext("/v1/install-ping");
        var limiter = new MintRateLimiter(Clock.systemUTC(), 5, 1, 100);
        server.createContext("/v1/install-ping", new InstallPingHandler(
                p -> { throw new IllegalStateException("db down"); }, limiter, 0));
        assertThat(post(VALID.formatted(UUID.randomUUID())).statusCode()).isEqualTo(500);
    }
}
