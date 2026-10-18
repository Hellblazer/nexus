// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.RequestLedger;
import dev.nexus.service.vectors.PgVectorRepository.LedgerMarker;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Function;
import java.util.regex.Pattern;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.5): the request-identity protocol of the three keyed sweep forms,
 * shared by their handlers. A keyed request is {@link #run claimed} through {@link RequestLedger}, runs its
 * sweep with a {@link LedgerMarker} that stores the response inside the sweep's own transaction, and answers
 * from the ledger when the key has been seen.
 *
 * <p>Response codes (the wire contract is {@code docs/wire-contract-pending.md}): 400
 * {@code idempotency_key_malformed} and {@code idempotency_key_unsupported} (nothing ran, no ledger row);
 * 422 {@code idempotency_key_reused}; 409 {@code request_in_flight} with {@code Retry-After}; a replay is the
 * stored status and body with {@code Idempotency-Replayed: true}; 503 {@code sweep_gate_busy} with
 * {@code Retry-After} on the quarantine forms. Every response for a request the ledger recorded echoes
 * {@code Idempotency-Key}; the echo is telemetry, the client never gates on it.
 */
final class KeyedSweep {

    private static final Logger log = LoggerFactory.getLogger(KeyedSweep.class);

    static final String KEY_HEADER = "Idempotency-Key";
    static final String REPLAYED_HEADER = "Idempotency-Replayed";

    /** Largest stored response body (Sam, 2026-10-17): above it the stored copy's sample is cut. */
    static final int STORED_RESPONSE_CAP_BYTES = 65_536;

    /** {@code Retry-After} on {@code sweep_gate_busy}: the gate wait is 2 s and a manifest writer holds it. */
    static final int SWEEP_GATE_BUSY_RETRY_AFTER_SECONDS = 5;

    /** Canonical 8-4-4-4-12 hex only; {@link UUID#fromString} alone accepts short forms like 1-1-1-1-1. */
    private static final Pattern CANONICAL_UUID = Pattern.compile(
        "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}");

    private final RequestLedger ledger;

    KeyedSweep(RequestLedger ledger) {
        this.ledger = ledger;
    }

    /** The key as a UUID, or null when {@code raw} is not a canonical UUID. */
    static UUID parseKey(String raw) {
        return raw != null && CANONICAL_UUID.matcher(raw).matches() ? UUID.fromString(raw) : null;
    }

    /** 400 with {@code code} as the error, for a key the engine cannot use. Nothing runs and no row is written. */
    static void refuse(HttpExchange ex, String code) throws IOException {
        HttpUtil.send(ex, 400, "{\"error\":\"" + code + "\"}");
    }

    /**
     * Claim, run and answer one keyed sweep.
     *
     * @param route        the ledger route (the request path)
     * @param raw          the raw request body bytes; their SHA-256 binds the key to this request
     * @param attemptBound the form's statement bound; the ledger adds the pool waits and slack to it
     * @param gateBusy     true for the two quarantine forms, which can meet the RDR-191 sweep gate; restore
     *                     never takes the gate and keeps its row-lock timeout error
     * @param sweep        runs the repository sweep with the marker and returns the outcome
     * @param bodyOf       the response body of an outcome, the same builder the unkeyed call uses
     */
    <O> void run(HttpExchange ex, String tenant, UUID requestId, String keyHeader, String route, byte[] raw,
                 long attemptBound, boolean gateBusy, Function<LedgerMarker<O>, O> sweep,
                 Function<O, Map<String, Object>> bodyOf) throws IOException {
        RequestLedger.Claim claim = ledger.claim(tenant, requestId, route, sha256(raw), attemptBound);
        ex.getResponseHeaders().set(KEY_HEADER, keyHeader);
        switch (claim) {
            case RequestLedger.KeyReused ignored ->
                HttpUtil.send(ex, 422, "{\"error\":\"idempotency_key_reused\"}");
            case RequestLedger.Replay replay -> {
                ex.getResponseHeaders().set(REPLAYED_HEADER, "true");
                HttpUtil.send(ex, replay.status(), replay.responseJson() == null ? "{}" : replay.responseJson());
            }
            case RequestLedger.InFlight inFlight -> {
                ex.getResponseHeaders().set("Retry-After", Long.toString(retryAfterSeconds(inFlight.leaseUntil())));
                var body = new LinkedHashMap<String, Object>();
                body.put("error", "request_in_flight");
                body.put("request_id", inFlight.requestId().toString());
                body.put("route", inFlight.route());
                body.put("started_at", inFlight.startedAt().toString());
                body.put("lease_until", inFlight.leaseUntil().toString());
                HttpUtil.send(ex, 409, toJson(body));
            }
            case RequestLedger.Claimed claimed -> runClaimed(ex, tenant, claimed, route, gateBusy, sweep, bodyOf);
        }
    }

    private <O> void runClaimed(HttpExchange ex, String tenant, RequestLedger.Claimed claimed, String route,
                                boolean gateBusy, Function<LedgerMarker<O>, O> sweep,
                                Function<O, Map<String, Object>> bodyOf) throws IOException {
        UUID requestId = claimed.requestId();
        String[] live = new String[1];
        try {
            sweep.apply((ctx, outcome) -> {
                Map<String, Object> body = bodyOf.apply(outcome);
                live[0] = toJson(body);
                ledger.commitMarker(ctx, tenant, requestId, claimed.nonce(), 200, storedResponse(body, live[0]));
            });
        } catch (RuntimeException e) {
            // Only a failure before the sweep transaction committed lands here: the exception left the
            // repository call, so the transaction rolled back. A failure after it (writing the response below)
            // never reaches the ledger row. The marker matches this attempt's nonce, so it cannot touch a
            // committed row or a newer holder's.
            boolean busy = gateBusy && isSweepGateBusy(e);
            ledger.failMarker(tenant, requestId, claimed.nonce(), busy ? 503 : failureStatus(e));
            if (!busy) {
                throw e;
            }
            log.warn("event=keyed_sweep_gate_busy tenant={} request_id={} route={}", tenant, requestId, route);
            ex.getResponseHeaders().set("Retry-After", Integer.toString(SWEEP_GATE_BUSY_RETRY_AFTER_SECONDS));
            HttpUtil.send(ex, 503, "{\"error\":\"sweep_gate_busy\"}");
            return;
        }
        HttpUtil.send(ex, 200, live[0]);
    }

    /**
     * The copy kept in the ledger: the live body, or, when it serializes to more than
     * {@link #STORED_RESPONSE_CAP_BYTES}, the body with its {@code sample} cut to the longest prefix that fits
     * and {@code "sample_truncated": true} added. The live response to the first caller is never cut, and the
     * stored copy names no audit id: the full list stays in {@code gc_audit}.
     */
    String storedResponse(Map<String, Object> body, String liveJson) {
        if (utf8Length(liveJson) <= STORED_RESPONSE_CAP_BYTES) {
            return liveJson;
        }
        List<?> sample = body.get("sample") instanceof List<?> l ? l : List.of();
        int lo = 0;
        int hi = sample.size();
        while (lo < hi) {
            int mid = (lo + hi + 1) >>> 1;
            if (utf8Length(truncatedJson(body, sample, mid)) <= STORED_RESPONSE_CAP_BYTES) {
                lo = mid;
            } else {
                hi = mid - 1;
            }
        }
        return truncatedJson(body, sample, lo);
    }

    private static String truncatedJson(Map<String, Object> body, List<?> sample, int keep) {
        var copy = new LinkedHashMap<String, Object>(body);
        copy.put("sample", sample.subList(0, keep));
        copy.put("sample_truncated", true);
        return toJson(copy);
    }

    private static String toJson(Object value) {
        try {
            return VectorHandler.MAPPER.writeValueAsString(value);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException("response body is not serialisable", e);
        }
    }

    /**
     * True for SQLSTATE 55P03 raised by a sweep function's gate wait: the message carries the RDR-191 sweep
     * gate marker. A row-lock timeout is also 55P03 but its message does not, and it keeps today's error.
     */
    static boolean isSweepGateBusy(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof java.sql.SQLException se && "55P03".equals(se.getSQLState())
                    && se.getMessage() != null && se.getMessage().contains(PgSession.SWEEP_GATE_MESSAGE_MARKER)) {
                return true;
            }
        }
        return false;
    }

    /** The status the failure marker records: what the handler's ladder will answer, as far as it is knowable. */
    private static int failureStatus(RuntimeException e) {
        if (e instanceof IllegalArgumentException) return 400;
        if (e instanceof IllegalStateException) return 422;
        return 500;
    }

    /** Whole seconds until the lease ends, never below 1. */
    private static long retryAfterSeconds(OffsetDateTime leaseUntil) {
        long millis = Duration.between(OffsetDateTime.now(), leaseUntil).toMillis();
        return Math.max(1L, (millis + 999L) / 1000L);
    }

    private static int utf8Length(String s) {
        return s.getBytes(StandardCharsets.UTF_8).length;
    }

    private static byte[] sha256(byte[] raw) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(raw);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is required of every JVM", e);
        }
    }
}
