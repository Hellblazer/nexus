// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpsConfigurator;
import com.sun.net.httpserver.HttpsServer;
import dev.nexus.service.resolver.HttpsSchemeHandler;
import dev.nexus.service.resolver.UriSchemeResolverRegistry;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLEngine;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509ExtendedTrustManager;
import java.io.FileInputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-169 G3 fix round 2 (bead nexus-aphki, T2 fix-check-nexus-aphki-round1-2026-09-12
 * Critical) — end-to-end proof that the REAL {@link HttpsSchemeHandler}, on a genuine
 * network failure, emits {@code "fetch_failed"} and {@link ResolveHandler} maps that to
 * 502, going through a real {@link UriSchemeResolverRegistry} and a real HTTP round trip
 * (not the hand-rolled stub {@code ResolveHandlerReasonMappingTest} uses for the same
 * status code — that suite proves the MAPPING; this one proves the REAL handler actually
 * produces the token the mapping expects).
 *
 * <p>Two genuinely different network failures, both real (no mocked {@link HttpClient}):
 * <ol>
 *   <li>A closed port (nothing listening) — {@code httpClient.send} throws a connection-
 *       refused {@link java.io.IOException} before any TLS handshake starts.</li>
 *   <li>A real TLS server (self-signed cert, generated via {@code keytool} into a JUnit
 *       {@link TempDir} — no external dependency beyond the JDK's own tool) that completes
 *       the handshake and returns HTTP 500 — exercises {@link HttpsSchemeHandler}'s non-2xx
 *       branch specifically, the one fetch-failure site the closed-port case cannot reach.</li>
 * </ol>
 *
 * <p>The client-side {@link HttpClient} trusts the test server's self-signed cert via a
 * permissive {@link X509ExtendedTrustManager} — a test-only artifact, never production code;
 * production {@link HttpsSchemeHandler#HttpsSchemeHandler()} uses the platform's real trust
 * store.
 */
class ResolveHandlerHttpsFetchFailureTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TENANT = "https-fetch-failure-tenant";

    private HttpServer resolveServer;
    private HttpsServer targetHttpsServer;
    private HttpsSchemeHandler httpsSchemeHandler;
    private final HttpClient http = HttpClient.newHttpClient();

    @AfterEach
    void stop() {
        if (resolveServer != null) resolveServer.stop(0);
        if (targetHttpsServer != null) targetHttpsServer.stop(0);
        if (httpsSchemeHandler != null) httpsSchemeHandler.close();
    }

    /** Starts the bare resolve-endpoint server, same tenant-stamping wrapper as
     *  {@code ResolveHandlerReasonMappingTest}. */
    private void startResolveServer(UriSchemeResolverRegistry registry) throws Exception {
        ResolveHandler resolveHandler = new ResolveHandler(registry, null);
        resolveServer = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        resolveServer.createContext("/v1/vectors/resolve", exchange -> {
            RequestContext.set(new RequestContext.Principal(
                    TENANT, "https-fetch-failure-session", true, false, "tenant", "test-hash"));
            try {
                resolveHandler.handle(exchange);
            } finally {
                RequestContext.clear();
            }
        });
        resolveServer.start();
    }

    private HttpResponse<String> postResolve(Object body) throws Exception {
        var req = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + resolveServer.getAddress().getPort()
                        + "/v1/vectors/resolve"))
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
    // Closed port -- connection refused before any TLS handshake
    // -------------------------------------------------------------------------

    @Test
    void closedPort_realHttpsSchemeHandler_fetchFailed_maps502ThroughResolveHandler() throws Exception {
        int freePort;
        try (ServerSocket probe = new ServerSocket(0)) {
            freePort = probe.getLocalPort();
        }
        // probe is closed here -- nothing listens on freePort.
        String uri = "https://127.0.0.1:" + freePort + "/";

        httpsSchemeHandler = new HttpsSchemeHandler(); // real default HttpClient
        var registry = new UriSchemeResolverRegistry();
        registry.register("https", httpsSchemeHandler);
        startResolveServer(registry);

        var resp = postResolve(Map.of("source_uri", uri));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(502);
        Map<String, Object> body = jsonBody(resp);
        assertThat(body.get("error")).isEqualTo("fetch_failed");
        assertThat((String) body.get("detail")).contains("I/O error");
    }

    // -------------------------------------------------------------------------
    // Real TLS server returning HTTP 500 -- exercises the non-2xx-status branch
    // -------------------------------------------------------------------------

    @Test
    void serverReturns500_realHttpsSchemeHandler_fetchFailed_maps502ThroughResolveHandler(
            @TempDir Path tempDir) throws Exception {
        Path keystore = tempDir.resolve("resolve-fetch-failure-test.p12");
        char[] password = "changeit".toCharArray();
        KeytoolSelfSignedCert.generate(keystore, password);

        SSLContext serverSslContext = buildServerSslContext(keystore, password);
        targetHttpsServer = HttpsServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        targetHttpsServer.setHttpsConfigurator(new HttpsConfigurator(serverSslContext));
        targetHttpsServer.createContext("/", exchange -> {
            byte[] msg = "boom".getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(500, msg.length);
            try (var os = exchange.getResponseBody()) {
                os.write(msg);
            }
        });
        targetHttpsServer.start();
        String uri = "https://127.0.0.1:" + targetHttpsServer.getAddress().getPort() + "/";

        // Test-only trust-all client so the self-signed cert above completes a real
        // TLS handshake -- production HttpsSchemeHandler() uses the platform's real
        // trust store instead; this is never wired into anything but this test.
        HttpClient trustingClient = HttpClient.newBuilder()
                .sslContext(trustAllSslContext())
                .build();
        httpsSchemeHandler = new HttpsSchemeHandler(trustingClient);

        var registry = new UriSchemeResolverRegistry();
        registry.register("https", httpsSchemeHandler);
        startResolveServer(registry);

        var resp = postResolve(Map.of("source_uri", uri));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(502);
        Map<String, Object> body = jsonBody(resp);
        assertThat(body.get("error")).isEqualTo("fetch_failed");
        assertThat((String) body.get("detail")).contains("HTTP 500");
    }

    // ── TLS test infrastructure (test-only; never production code) ─────────────

    private static SSLContext buildServerSslContext(Path keystorePath, char[] password) throws Exception {
        KeyStore ks = KeyStore.getInstance("PKCS12");
        try (var in = new FileInputStream(keystorePath.toFile())) {
            ks.load(in, password);
        }
        KeyManagerFactory kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        kmf.init(ks, password);
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(kmf.getKeyManagers(), null, new SecureRandom());
        return ctx;
    }

    /** Trusts any certificate -- ONLY ever used as the CLIENT context in this test file,
     *  talking to a self-signed server this same test process just generated. */
    private static SSLContext trustAllSslContext() throws Exception {
        TrustManager[] trustAll = { new X509ExtendedTrustManager() {
            @Override public void checkClientTrusted(X509Certificate[] chain, String authType) { }
            @Override public void checkServerTrusted(X509Certificate[] chain, String authType) { }
            @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            @Override public void checkClientTrusted(X509Certificate[] chain, String authType, Socket socket) { }
            @Override public void checkServerTrusted(X509Certificate[] chain, String authType, Socket socket) { }
            @Override public void checkClientTrusted(X509Certificate[] chain, String authType, SSLEngine engine) { }
            @Override public void checkServerTrusted(X509Certificate[] chain, String authType, SSLEngine engine) { }
        } };
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, trustAll, new SecureRandom());
        return ctx;
    }

    /** Shells out to the running JDK's own {@code keytool} to mint a throwaway
     *  self-signed PKCS12 keystore -- no external dependency (Bouncy Castle etc.)
     *  beyond the JDK this test already runs on. */
    private static final class KeytoolSelfSignedCert {
        static void generate(Path keystorePath, char[] password) throws Exception {
            String keytool = System.getProperty("java.home") + "/bin/keytool";
            String pass = new String(password);
            Process p = new ProcessBuilder(
                    keytool, "-genkeypair",
                    "-alias", "resolve-fetch-failure-test",
                    "-keyalg", "RSA", "-keysize", "2048",
                    "-validity", "2",
                    "-keystore", keystorePath.toString(),
                    "-storetype", "PKCS12",
                    "-storepass", pass, "-keypass", pass,
                    "-dname", "CN=127.0.0.1, OU=nexus-test, O=nexus-test, L=test, ST=test, C=US",
                    // A Subject Alternative Name is REQUIRED: java.net.http.HttpClient
                    // enables HTTPS endpoint identification by default (RFC 6125), which
                    // checks SAN entries, not the CN fallback -- an ip SAN matching
                    // 127.0.0.1 is what makes the handshake below actually complete.
                    "-ext", "SAN=ip:127.0.0.1")
                    .redirectErrorStream(true)
                    .start();
            String output = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
            int exit = p.waitFor();
            if (exit != 0) {
                throw new IllegalStateException(
                        "keytool -genkeypair failed (exit " + exit + "): " + output);
            }
        }
    }
}
