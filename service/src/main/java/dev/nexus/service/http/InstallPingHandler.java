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

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.time.Clock;
import java.util.HexFormat;
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
 *
 * <p>nexus-5zv4j: a nullable {@code source_hash} column is computed here and
 * never sent by the client (the payload above is unchanged) — an HMAC-SHA256
 * of the same {@link #remoteAddress} the rate limiter keys on (keyed by
 * {@link #HASH_KEY_ENV}; unset means NULL, never an unkeyed hash of the raw
 * address, which is itself never stored or logged). NAT/CGNAT collapses many
 * distinct installs onto one source_hash, and a single install's address
 * changing (a new DHCP lease, a mobile network handoff) fragments its own
 * history across hashes — this is a correlation signal, not a stable
 * per-install identifier. That identity use also relies on the edge actually
 * collapsing X-Forwarded-For to one vouched hop before this engine sees it:
 * a self-hosted engine that sets {@link #TRUSTED_PROXIES_ENV} without a
 * collapsing proxy in front of it lets a client pick its own X-Forwarded-For
 * value, and therefore its own fingerprint.
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

    /** HMAC-SHA256 key for source_hash. Unset = the column stays NULL, never an unkeyed hash.
     * Rotating this key changes every future source_hash: a hash computed under a new key is
     * NOT comparable to one computed under an earlier key, so rotation silently starts a new
     * correlation epoch rather than continuing the old one. */
    static final String HASH_KEY_ENV = "NX_INSTALL_PING_HASH_KEY";
    /** First N hex chars of the 32-byte HMAC-SHA256 digest kept as source_hash (8 of 32 bytes). */
    static final int SOURCE_HASH_HEX_CHARS = 16;

    private final InstallPingSink sink;
    private final MintRateLimiter rateLimiter;
    private final int trustedProxies;
    private final String hashKey;

    /** Convenience: no source_hash (existing callers that predate nexus-5zv4j). */
    public InstallPingHandler(InstallPingSink sink, MintRateLimiter rateLimiter, int trustedProxies) {
        this(sink, rateLimiter, trustedProxies, null);
    }

    public InstallPingHandler(InstallPingSink sink, MintRateLimiter rateLimiter, int trustedProxies,
                               String hashKey) {
        this.sink = sink;
        this.rateLimiter = rateLimiter;
        this.trustedProxies = Math.max(0, trustedProxies);
        this.hashKey = hashKey;
        if (hashKey == null || hashKey.isBlank()) {
            // Not a failure -- unset is a valid, supported posture (source_hash stays
            // NULL forever) -- but a silent no-op here is easy to mistake for a bug
            // report waiting to happen, so it gets one WARN at construction rather
            // than being discoverable only by noticing every row's source_hash is
            // NULL. No /version field: this is an operator-visible boot log, not a
            // wire-contract capability.
            log.warn("event=install_ping_hash_key_unset detail=\"NX_INSTALL_PING_HASH_KEY not set; "
                    + "source_hash will be NULL for every ping\"");
        }
    }

    /** Production wiring: env-tuned limiter (same knobs as the mint route), proxy count,
     * and HMAC key. */
    public static InstallPingHandler fromEnv(InstallPingSink sink, Clock clock) {
        return new InstallPingHandler(sink, MintRateLimiter.fromEnv(clock), trustedProxiesFromEnv(),
                System.getenv(HASH_KEY_ENV));
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
            String sourceHash = sourceHash(hashKey, remote);
            sink.record(new InstallPingRepository.Ping(
                    installId, version, mode, os, arch, python, sourceHash));
        } catch (RuntimeException e) {
            // sourceHash() is inside this same try (nexus-5zv4j round-2 fix): an
            // IllegalStateException from a missing HmacSHA256 provider must degrade
            // to the same logged 500 a sink failure gets, never escape handle()
            // uncaught.
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

    /**
     * First {@link #SOURCE_HASH_HEX_CHARS} hex chars of HMAC-SHA256({@code key},
     * {@code address}), or {@code null} when {@code key} is unset — never an
     * unkeyed hash of the address (nexus-5zv4j). {@code address} is the SAME
     * value {@link #remoteAddress} derived for the rate limiter; it is never
     * itself stored or logged, only this digest.
     */
    static String sourceHash(String key, String address) {
        if (key == null || key.isBlank()) {
            return null;
        }
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            byte[] digest = mac.doFinal(address.getBytes(StandardCharsets.UTF_8));
            String hex = HexFormat.of().formatHex(digest);
            return hex.substring(0, SOURCE_HASH_HEX_CHARS);
        } catch (GeneralSecurityException e) {
            // HmacSHA256 is a mandatory JCE algorithm on every JVM this engine ships
            // on; a missing provider is a build/runtime defect, not a request-time
            // condition to swallow into a silent NULL.
            throw new IllegalStateException("HmacSHA256 unavailable", e);
        }
    }
}
