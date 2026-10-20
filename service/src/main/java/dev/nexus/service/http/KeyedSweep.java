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
 * {@code Retry-After} on the quarantine forms; 503 {@code request_claim_contended} with {@code Retry-After} when the
 * ledger's bounded claim rounds could not settle the key (nothing was written, resend with the same key). Every
 * response for a request the ledger recorded echoes {@code Idempotency-Key}; the echo is telemetry, the client
 * never gates on it.
 *
 * <p>The failure marker is best effort and never holds up the error response: skipped when the failure is pool
 * exhaustion, otherwise waited for at most {@link #FAIL_MARKER_WAIT_MS}. A row it did not mark reads
 * {@code in_flight} until its lease lapses, then {@code rolled_back}; what {@code rolled_back} does and does not promise is in
 * {@link RequestLedger}.
 */
final class KeyedSweep {

    private static final Logger log = LoggerFactory.getLogger(KeyedSweep.class);

    static final String KEY_HEADER = "Idempotency-Key";
    static final String REPLAYED_HEADER = "Idempotency-Replayed";

    /** Largest stored response body (Sam, 2026-10-17): above it the stored copy's sample is cut. */
    static final int STORED_RESPONSE_CAP_BYTES = 65_536;

    /** {@code Retry-After} on {@code sweep_gate_busy}: the gate wait is 2 s and a manifest writer holds it. */
    static final int SWEEP_GATE_BUSY_RETRY_AFTER_SECONDS = 5;

    /**
     * The longest the failure marker may hold up the error response (nexus-yye8l.18). The marker is an
     * optimisation, not a correctness step: the RDR's backstop for a missed marker is the lease expiry, so the
     * row simply reads {@code in_flight} until the lease lapses and then {@code rolled_back}. A healthy marker
     * takes milliseconds; one that has to wait for an admission permit or a connection (up to two pool waits,
     * ~60 s, behind an exhausted pool) is abandoned after this, and the error response goes out without it.
     */
    static final long FAIL_MARKER_WAIT_MS = 2_000L;

    /** Wire code and {@code Retry-After} of the 503 for a claim that could not settle (nexus-yye8l.20). */
    static final String CLAIM_CONTENDED_ERROR = "request_claim_contended";
    static final int CLAIM_CONTENDED_RETRY_AFTER_SECONDS = 1;

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
            case RequestLedger.KeyReused ignored -> sendKeyReused(ex);
            case RequestLedger.Replay replay -> sendReplay(ex, replay);
            case RequestLedger.InFlight inFlight -> {
                ex.getResponseHeaders().set("Retry-After", Long.toString(retryAfterSeconds(inFlight.observedAt(), inFlight.leaseUntil())));
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

    /**
     * For a keyed request that cannot run because its collection is not registered: answer from the ledger if the
     * key already COMMITTED (the stored response, or 422 {@code idempotency_key_reused} for another body), exactly
     * as {@link #run} would have. Returns false, having written nothing, when the key has no committed row; the
     * caller then gives today's error, and no ledger row is created for a key that never ran.
     */
    boolean answerCommitted(HttpExchange ex, String tenant, UUID requestId, String keyHeader, String route,
                            byte[] raw) throws IOException {
        RequestLedger.Claim claim = ledger.peekCommitted(tenant, requestId, route, sha256(raw));
        if (claim == null) {
            return false;
        }
        ex.getResponseHeaders().set(KEY_HEADER, keyHeader);
        if (claim instanceof RequestLedger.Replay replay) {
            sendReplay(ex, replay);
        } else {
            sendKeyReused(ex);
        }
        return true;
    }

    private static void sendKeyReused(HttpExchange ex) throws IOException {
        HttpUtil.send(ex, 422, "{\"error\":\"idempotency_key_reused\"}");
    }

    private static void sendReplay(HttpExchange ex, RequestLedger.Replay replay) throws IOException {
        ex.getResponseHeaders().set(REPLAYED_HEADER, "true");
        HttpUtil.send(ex, replay.status(), replay.responseJson() == null ? "{}" : replay.responseJson());
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
            // never reaches the ledger row.
            //
            // The failure marker is safe against an ambiguous commit (the server committed, the ack was lost,
            // and the driver threw) by its SQL guard, not by this code's shape: it updates only a row whose
            // holder is this attempt's nonce AND whose state is still in_flight. The commit marker, in the
            // sweep transaction, set state = committed and cleared the holder, so if that transaction did
            // commit the failure marker matches nothing and the committed row stands; and a newer holder's
            // row carries a different nonce.
            //
            // The status the marker records is the one the handler's exception ladder sends, read at the
            // moment HttpUtil.send puts it on the wire (HttpUtil.armBeforeSend, per exchange), so there is no second
            // hand-kept copy of the ladder to drift from it. The marker still runs before the response is
            // written (within the budget below), so a client that retries at once finds the row failed, not in
            // flight.
            //
            // nexus-yye8l.18: the marker is skipped when the failure IS pool exhaustion or an admission
            // timeout. It would queue on the same exhausted pool, behind the connection waits that just failed,
            // to record what the lease expiry records anyway, and the 503 is what the client is waiting for. Any
            // other failure arms it, but the response waits at most FAIL_MARKER_WAIT_MS for it.
            boolean busy = gateBusy && isSweepGateBusy(e);
            if (HttpUtil.isPoolExhausted(e)) {
                log.warn("event=request_ledger_fail_marker_skipped tenant={} request_id={} reason=pool_exhausted",
                    tenant, requestId);
            } else {
                HttpUtil.armBeforeSend(ex,
                    status -> failMarkerWithinBudget(tenant, requestId, claimed.nonce(), status));
            }
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
     * Run {@link RequestLedger#failMarker} on a thread of its own and wait at most {@link #FAIL_MARKER_WAIT_MS}
     * for it. After the budget the response goes out and the marker keeps trying in the background: if it
     * lands later it only moves a row this attempt still holds from {@code in_flight} to {@code failed}, which
     * its own SQL guard allows at any time; if it never does, the lease expiry covers the row.
     */
    private void failMarkerWithinBudget(String tenant, UUID requestId, UUID nonce, int status) {
        var done = new java.util.concurrent.CountDownLatch(1);
        Thread.ofVirtual().name("ledger-fail-marker").start(() -> {
            try {
                ledger.failMarker(tenant, requestId, nonce, status);
            } finally {
                done.countDown();
            }
        });
        try {
            if (!done.await(FAIL_MARKER_WAIT_MS, java.util.concurrent.TimeUnit.MILLISECONDS)) {
                log.warn("event=request_ledger_fail_marker_deferred tenant={} request_id={} waited_ms={}",
                    tenant, requestId, FAIL_MARKER_WAIT_MS);
            }
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
        }
    }

    /**
     * The copy kept in the ledger: the live body, or, when it is larger than {@link #STORED_RESPONSE_CAP_BYTES},
     * the body with its {@code sample} cut to the longest prefix that fits and {@code "sample_truncated": true}
     * added. The live response to the first caller is never cut, and the stored copy names no audit id: the full
     * list stays in {@code gc_audit}.
     *
     * <p>The cap is on the bytes a replay or the status route returns, and that is the text Postgres renders
     * from the jsonb column, not the compact text stored: Postgres writes a space after every {@code ':'} and
     * {@code ','}, which adds several KB to a body near the cap. So the size is taken with {@link #pgJsonbBytes}.
     */
    String storedResponse(Map<String, Object> body, String liveJson) {
        if (pgJsonbBytes(body) <= STORED_RESPONSE_CAP_BYTES) {
            return liveJson;
        }
        List<?> sample = body.get("sample") instanceof List<?> l ? l : List.of();
        int lo = 0;
        int hi = sample.size();
        while (lo < hi) {
            int mid = (lo + hi + 1) >>> 1;
            if (pgJsonbBytes(truncated(body, sample, mid)) <= STORED_RESPONSE_CAP_BYTES) {
                lo = mid;
            } else {
                hi = mid - 1;
            }
        }
        return toJson(truncated(body, sample, lo));
    }

    private static Map<String, Object> truncated(Map<String, Object> body, List<?> sample, int keep) {
        var copy = new LinkedHashMap<String, Object>(body);
        copy.put("sample", sample.subList(0, keep));
        copy.put("sample_truncated", true);
        return copy;
    }

    /** UTF-8 length of {@code value} as Postgres renders it back from a jsonb column. */
    static int pgJsonbBytes(Object value) {
        var sb = new StringBuilder();
        renderLikePostgres(VectorHandler.MAPPER.valueToTree(value), sb);
        return utf8Length(sb.toString());
    }

    /**
     * Postgres's jsonb text form: {@code ": "} after a key, {@code ", "} between members and elements, no other
     * whitespace, {@code {}} and {@code []} when empty. Strings and keys are escaped alike by Jackson and
     * Postgres (and non-ASCII stays literal in both); a number prints as the plain decimal Postgres keeps (a
     * {@code numeric}), so {@code 1.0E-4} is {@code 0.00010}.
     */
    private static void renderLikePostgres(com.fasterxml.jackson.databind.JsonNode node, StringBuilder out) {
        if (node.isObject()) {
            out.append('{');
            boolean first = true;
            for (var it = node.fields(); it.hasNext(); ) {
                var field = it.next();
                if (!first) out.append(", ");
                first = false;
                out.append(toJson(field.getKey())).append(": ");
                renderLikePostgres(field.getValue(), out);
            }
            out.append('}');
        } else if (node.isArray()) {
            out.append('[');
            boolean first = true;
            for (var element : node) {
                if (!first) out.append(", ");
                first = false;
                renderLikePostgres(element, out);
            }
            out.append(']');
        } else if (node.isNumber()) {
            out.append(node.decimalValue().toPlainString());
        } else {
            out.append(node.toString());
        }
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

    /**
     * Whole seconds until the lease ends, never below 1. Both instants are the database's: {@code observedAt} is
     * its clock at the read, so a skewed JVM clock cannot stretch or cut the wait (nexus-yye8l.20).
     */
    private static long retryAfterSeconds(OffsetDateTime observedAt, OffsetDateTime leaseUntil) {
        long millis = Duration.between(observedAt, leaseUntil).toMillis();
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
