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
 * <p>Wired live (bead nexus-aphki): {@link dev.nexus.service.NexusService}'s
 * constructor registers one instance of this handler for {@code https://}
 * unconditionally, injects the registry into {@link
 * dev.nexus.service.http.ResolveHandler} at {@code POST /v1/vectors/resolve},
 * and closes it in {@code NexusService.stop()} (see {@link
 * UriSchemeResolverRegistry}'s class javadoc for the full wiring — distinct
 * from RDR-169 Phase B / bead nexus-zw2em, which landed Gap 1's schema
 * column and Gap 4's WRITE route only).
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
        // "unreachable" here is a CALLER-shaped URI problem (RDR-169 G3 fix
        // round 2, T2 fix-check-nexus-aphki-round1-2026-09-12 Critical) --
        // maps to 422 in ResolveHandler, same as a malformed chroma:// URI.
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
            // Also caller-shaped (a syntactically invalid URI) -- "unreachable", 422.
            return ResolveResult.error("unreachable",
                    "malformed https URI '" + uri + "': " + e.getMessage());
        }

        HttpResponse<String> response;
        try {
            response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        } catch (IOException e) {
            // GENUINE fetch failure (fix round 2): distinct from the URI-shape
            // "unreachable" errors above -- a well-formed https:// request that
            // could not be completed (DNS/connect/TLS/read failure). Maps to
            // 502 in ResolveHandler, not 422: the URI itself was fine, the
            // resolver just could not reach it.
            return ResolveResult.error("fetch_failed",
                    "I/O error fetching '" + uri + "': " + e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return ResolveResult.error("fetch_failed",
                    "fetch interrupted for '" + uri + "'");
        }

        int status = response.statusCode();
        if (status < 200 || status >= 300) {
            // A completed fetch that came back non-2xx is also a genuine
            // resolver failure, not a caller-shaped URI problem -- 502.
            return ResolveResult.error("fetch_failed",
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
