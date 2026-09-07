// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.InstallPingRepository;
import dev.nexus.service.db.InstallPingSink;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * {@code POST /v1/install-ping} — nexus-h5olw. The client's once-a-day
 * anonymous "I exist" beacon, the only signal that counts local-mode installs.
 *
 * <p>Unauthenticated, same posture as {@link StatusHandler}: local installs
 * have no tenant and no token, and the payload carries nothing tenant-owned.
 * Registered OUTSIDE the {@code /v1/*} auth-filter block in {@code NexusService}.
 *
 * <p>Body (all six required, every one bounded):
 * <pre>{"install_id":"<uuid>","client_version":"7.35.0","mode":"local|cloud",
 *  "os":"darwin","arch":"arm64","python":"3.12"}</pre>
 *
 * <p>Replies {@code 202 {"ok":true}}. Deliberately says nothing else: the
 * update-notice half (latest published version in the reply) is deferred.
 * 400 on a missing, over-long, or malformed field; 405 on non-POST; 429 when
 * the {@link MintRateLimiter} refuses (keyed by remote address, then by
 * install id — a runaway client can neither flood the table nor burn the
 * per-address budget of everyone behind one NAT; the address comes from
 * {@link #remoteAddress}, see its trusted-proxy rule). A DB failure is 500 and
 * logged; the client swallows every outcome, so nothing here is retried.
 *
 * <p>Additive wire change: a NEW route; see docs/wire-contract-pending.md.
 */
public final class InstallPingHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(InstallPingHandler.class);

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    static final int MAX_BODY_BYTES = 1024;
    static final int MAX_FIELD_CHARS = 64;
    static final Set<String> MODES = Set.of("local", "cloud");
    /** Version/os/arch/python: printable token characters only; no whitespace, no JSON noise. */
    private static final Pattern TOKEN = Pattern.compile("[A-Za-z0-9._+-]{1," + MAX_FIELD_CHARS + "}");

    /** Trailing X-Forwarded-For hops appended by proxies this engine trusts; 0 = key on the socket peer. */
    static final String TRUSTED_PROXIES_ENV = "NX_INSTALL_PING_TRUSTED_PROXIES";

    private final InstallPingSink sink;
    private final MintRateLimiter rateLimiter;
    private final int trustedProxies;

    public InstallPingHandler(InstallPingSink sink, MintRateLimiter rateLimiter, int trustedProxies) {
        this.sink = sink;
        this.rateLimiter = rateLimiter;
        this.trustedProxies = Math.max(0, trustedProxies);
    }

    /** Production wiring: env-tuned limiter (same knobs as the mint route) and proxy count. */
    public static InstallPingHandler fromEnv(InstallPingSink sink, Clock clock) {
        return new InstallPingHandler(sink, MintRateLimiter.fromEnv(clock), trustedProxiesFromEnv());
    }

    @Override
    public void handle(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        Map<String, Object> body;
        try (InputStream is = ex.getRequestBody()) {
            byte[] raw = is.readNBytes(MAX_BODY_BYTES + 1);
            if (raw.length > MAX_BODY_BYTES) {
                HttpUtil.send(ex, 400, "{\"error\":\"body too large\"}");
                return;
            }
            try {
                body = MAPPER.readValue(new String(raw, StandardCharsets.UTF_8), MAP_TYPE);
            } catch (IOException e) {
                HttpUtil.send(ex, 400, "{\"error\":\"malformed json\"}");
                return;
            }
        }
        if (body == null) {
            HttpUtil.send(ex, 400, "{\"error\":\"empty body\"}");
            return;
        }

        String installIdRaw = token(body, "install_id");
        String version = token(body, "client_version");
        String mode = token(body, "mode");
        String os = token(body, "os");
        String arch = token(body, "arch");
        String python = token(body, "python");
        if (installIdRaw == null || version == null || mode == null
                || os == null || arch == null || python == null) {
            HttpUtil.send(ex, 400, "{\"error\":\"install_id, client_version, mode, os, arch, python: each required, 1-"
                    + MAX_FIELD_CHARS + " token chars\"}");
            return;
        }
        UUID installId;
        try {
            installId = UUID.fromString(installIdRaw);
        } catch (IllegalArgumentException e) {
            HttpUtil.send(ex, 400, "{\"error\":\"install_id must be a UUID\"}");
            return;
        }
        if (!MODES.contains(mode)) {
            HttpUtil.send(ex, 400, "{\"error\":\"mode must be local or cloud\"}");
            return;
        }

        String remote = remoteAddress(ex, trustedProxies);
        if (!rateLimiter.tryAcquire(remote, installId.toString())) {
            ex.getResponseHeaders().set("Retry-After", "60");
            HttpUtil.send(ex, 429, "{\"error\":\"rate limit exceeded, retry later\"}");
            return;
        }

        try {
            sink.record(new InstallPingRepository.Ping(installId, version, mode, os, arch, python));
        } catch (RuntimeException e) {
            log.warn("install_ping_record_failed", e);
            HttpUtil.send(ex, 500, "{\"error\":\"record failed\"}");
            return;
        }
        HttpUtil.send(ex, 202, "{\"ok\":true}");
    }

    private static String token(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String s)) return null;
        return TOKEN.matcher(s).matches() ? s : null;
    }

    /**
     * The client address the rate limiter keys on, derived from
     * X-Forwarded-For by a TRUSTED-PROXY COUNT rather than by picking an end
     * of the list: either end is wrong in one deployment. The trailing
     * {@code trustedProxies} hops were appended by proxies this engine
     * trusts; the hop immediately before them is the client. With the count
     * at 0 (the default, a bare local engine) the header is entirely
     * untrusted and the socket peer is the key. On the managed edge the
     * control plane collapses the header to exactly the ALB-vouched client
     * IP and the TLS sidecar appends the control plane's address, so the
     * engine sees {@code "client-ip, edge-ip"} and the count is 1
     * ({@code NX_INSTALL_PING_TRUSTED_PROXIES=1}; conexus-n5n8's edge test
     * asserts the same shape). A list shorter than the count falls back to
     * the socket peer.
     */
    static String remoteAddress(HttpExchange ex, int trustedProxies) {
        String peer = ex.getRemoteAddress().getAddress().getHostAddress();
        if (trustedProxies <= 0) return peer;
        String xff = ex.getRequestHeaders().getFirst("X-Forwarded-For");
        if (xff == null || xff.isBlank()) return peer;
        String[] hops = xff.split(",");
        int idx = hops.length - trustedProxies - 1;
        if (idx < 0) return peer;
        String hop = hops[idx].trim();
        return hop.isEmpty() ? peer : hop;
    }

    static int trustedProxiesFromEnv() {
        String raw = System.getenv(TRUSTED_PROXIES_ENV);
        if (raw == null || raw.isBlank()) return 0;
        try {
            return Math.max(0, Integer.parseInt(raw.trim()));
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(TRUSTED_PROXIES_ENV + " must be an integer, got: " + raw, e);
        }
    }
}
