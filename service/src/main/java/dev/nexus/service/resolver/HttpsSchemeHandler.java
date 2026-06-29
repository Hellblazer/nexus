// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * Resolves {@code https://} URIs via HTTP GET.
 *
 * <p>RDR-169 G3 (bead nexus-064jj) — server-reachable scheme.  Fetches the
 * document at the URI and returns its body as text.  This is the server-side
 * complement to the Python bridge's {@code _read_https_uri} reader: the
 * server can reach external URLs; client machines may be behind NAT.
 *
 * <p>Tenant context is accepted but not used for access control (HTTPS URLs
 * are public; tenant scoping for private URL sets is a Phase B concern).
 *
 * <p>Phase A: the handler is registered and unit-tested.  Live /v1
 * reference-only serving is Phase B (bead nexus-dtnpu).
 */
public final class HttpsSchemeHandler implements UriSchemeHandler, AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(HttpsSchemeHandler.class);

    /** Default fetch timeout — kept short since reference resolution is in the hot path. */
    private static final Duration DEFAULT_TIMEOUT = Duration.ofSeconds(15);

    private final HttpClient httpClient;

    /** Production constructor. */
    public HttpsSchemeHandler() {
        this(HttpClient.newBuilder()
                       .connectTimeout(DEFAULT_TIMEOUT)
                       .followRedirects(HttpClient.Redirect.NORMAL)
                       .build());
    }

    /**
     * Injected constructor for tests (pass a stub {@link HttpClient} to avoid
     * live network calls).
     */
    public HttpsSchemeHandler(HttpClient httpClient) {
        if (httpClient == null) {
            throw new IllegalArgumentException("httpClient must not be null");
        }
        this.httpClient = httpClient;
    }

    /**
     * Closes the underlying {@link HttpClient}.
     *
     * <p>{@link HttpClient} implements {@link AutoCloseable} on Java 21+
     * (the service runs GraalVM 25).  Delegating here lets callers manage
     * the handler's lifecycle via try-with-resources or an explicit close
     * call during service shutdown.
     */
    @Override
    public void close() {
        httpClient.close();
    }

    @Override
    public ResolveResult resolve(String uri, String tenant) {
        log.debug("event=https_resolve tenant={} uri={}", tenant, uri);

        // Scheme guard: this handler is registered for https:// only.
        // Fail loud rather than silently forwarding a mis-routed scheme.
        if (uri == null || !uri.startsWith("https://")) {
            return ResolveResult.error("unreachable",
                    "HttpsSchemeHandler received a non-https URI: '" + uri + "'");
        }

        HttpRequest request;
        try {
            request = HttpRequest.newBuilder()
                    .uri(URI.create(uri))
                    .timeout(DEFAULT_TIMEOUT)
                    .GET()
                    .build();
        } catch (IllegalArgumentException e) {
            return ResolveResult.error("unreachable",
                    "malformed https URI '" + uri + "': " + e.getMessage());
        }

        HttpResponse<String> response;
        try {
            response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        } catch (IOException e) {
            return ResolveResult.error("unreachable",
                    "I/O error fetching '" + uri + "': " + e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return ResolveResult.error("unreachable",
                    "fetch interrupted for '" + uri + "'");
        }

        int status = response.statusCode();
        if (status < 200 || status >= 300) {
            return ResolveResult.error("unreachable",
                    "HTTP " + status + " fetching '" + uri + "'");
        }

        String body = response.body();
        if (body == null || body.isBlank()) {
            return ResolveResult.error("empty",
                    "empty body at '" + uri + "' (HTTP " + status + ")");
        }

        return ResolveResult.ok(body, uri);
    }
}
