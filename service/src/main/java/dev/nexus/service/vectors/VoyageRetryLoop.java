// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Random;

/**
 * The ONE Voyage AI HTTP retry loop (nexus-1vpal), consolidating the three
 * near-identical {@code callApi} loops that {@link CceEmbedder},
 * {@link VoyageEmbedder}, and {@link VoyageReranker} each inlined privately.
 * The nexus-99r7y machinery — request-scoped 429 budget, {@code Retry-After}
 * honouring, equal-jitter backoff, typed {@link UpstreamRateLimitedException}
 * — shipped scoped to {@code CceEmbedder} (the measured 2026-08-15 incident
 * path); the two siblings kept the OLD un-jittered, {@code MAX_RETRIES}-
 * bounded loops, and all three share the same account-wide Voyage RPM
 * ceiling, so the identical incident shape stayed reproducible via bulk
 * {@code code__} indexing or rerank storms.
 *
 * <p>Semantics (the 99r7y contract, now for every caller):
 * <ul>
 *   <li><b>429 is budget-bounded, not attempt-bounded</b> — throttling is
 *       pacing, not failure. The caller mints ONE deadline per logical
 *       request ({@link #newDeadlineNanos()}) and passes it to every
 *       {@link #send} that request makes; when the next sleep would leave
 *       less than {@link #MIN_CALL_ALLOWANCE_MS} of budget, the loop fails
 *       fast with a typed {@link UpstreamRateLimitedException} carrying an
 *       honest Retry-After — never an opaque edge-timeout 5xx.</li>
 *   <li><b>Retry-After is honoured</b>: the sleep is
 *       {@code max(equalJitterBackoff, header)}.</li>
 *   <li><b>5xx and network failures keep the old {@link #MAX_RETRIES}
 *       attempt-bounded semantics</b>, now with equal-jitter backoff
 *       (see {@link #backoffDelayMs}). The two counters are INDEPENDENT —
 *       a deliberate divergence from the pre-consolidation
 *       VoyageEmbedder/VoyageReranker loops, whose single shared 3-attempt
 *       counter let 429s exhaust the 5xx allowance and vice versa: here an
 *       interleaved 500,429,500,429,500 sequence makes all five calls
 *       (429s pace against the budget, 5xx counts to 3) where the old
 *       loops stopped at three.</li>
 *   <li><b>401/403 raise {@link UpstreamAuthException}</b> immediately
 *       (nexus-pmhpc), shared verbatim across all three callers.</li>
 *   <li>Every other terminal shape is delegated to the caller's
 *       {@link Failures} so each class keeps its own exception vocabulary
 *       ({@link VoyageTooManyTokensException} discrimination for the
 *       embedder's 400, {@link RerankUpstreamException} wrapping for the
 *       reranker) without owning a private copy of the loop.</li>
 * </ul>
 *
 * <p>The caller owns the {@link HttpRequest} (URL, headers, per-class
 * timeout) and the {@link HttpClient}; this class owns only the retry
 * choreography. Thread-safe: no mutable state beyond the injected
 * {@link Random}, whose methods are internally synchronized.
 */
final class VoyageRetryLoop {

    private static final Logger log = LoggerFactory.getLogger(VoyageRetryLoop.class);

    static final int MAX_RETRIES = 3;

    /**
     * Headroom a retry must leave inside the 429 budget for the HTTP call
     * itself — sleeping right up to the deadline just moves the timeout
     * into the request (nexus-99r7y).
     */
    static final long MIN_CALL_ALLOWANCE_MS = 1_000L;

    /** Per-caller terminal-failure vocabulary; see the class doc. */
    interface Failures {
        /**
         * Exception for a non-retryable or retry-exhausted HTTP status.
         * Never called for 401/403 (auth is shared) or for a
         * budget-exhausted 429 (typed rate-limit is shared).
         */
        RuntimeException status(int status, String body);

        /** Wrapper for network-failure exhaustion and interrupts. */
        RuntimeException wrap(String message, Throwable cause);
    }

    private final String eventRetry;
    private final String eventRateLimited;
    private final String opLabel;
    private final long retryBaseMs;
    private final long rateLimitBudgetMs;
    private final Random jitterRandom;
    private final Runnable onPost;

    /**
     * @param logPrefix         structured-log event prefix — {@code "cce"},
     *                          {@code "voyage"}, {@code "voyage_rerank"} —
     *                          preserving each class's pre-consolidation
     *                          event names verbatim
     * @param opLabel           human label for wrap messages, e.g.
     *                          {@code "CCE embed"} (preserves each class's
     *                          pre-consolidation message shapes)
     * @param retryBaseMs       backoff base (test-injectable, fast in tests)
     * @param rateLimitBudgetMs total 429-absorption budget per logical
     *                          request (see {@link #newDeadlineNanos()})
     * @param jitterRandom      jitter source — seeded in formula tests
     * @param onPost            hook fired before every real upstream POST
     *                          ({@link VoyageEmbedder}'s request counter);
     *                          never null — pass a no-op
     */
    VoyageRetryLoop(String logPrefix, String opLabel, long retryBaseMs,
                    long rateLimitBudgetMs, Random jitterRandom, Runnable onPost) {
        this.eventRetry = logPrefix + "_retry";
        this.eventRateLimited = logPrefix + "_rate_limited";
        this.opLabel = opLabel;
        this.retryBaseMs = retryBaseMs;
        this.rateLimitBudgetMs = rateLimitBudgetMs;
        this.jitterRandom = jitterRandom;
        this.onPost = onPost;
    }

    /**
     * Mint the shared 429 deadline for ONE logical request. Request-scoped,
     * not per-call (the nexus-99r7y substantive-critic ship-blocker): a
     * caller that fans one logical request into many upstream calls —
     * {@link CceEmbedder}'s per-text parallel fan-out, {@link
     * VoyageEmbedder}'s planned sub-batches and adaptive halving — mints
     * this ONCE at the top and threads it into every {@link #send}, so the
     * whole request either completes or answers an honest 429 inside the
     * edge bound, instead of re-arming a fresh budget per upstream call.
     */
    long newDeadlineNanos() {
        return System.nanoTime() + rateLimitBudgetMs * 1_000_000L;
    }

    /**
     * Equal-Jitter backoff delay (nexus-9okyk critic fix 1; AWS "Equal
     * Jitter" convention). {@code cap = retryBaseMs * 2^(attempt-1)} is the
     * exponential envelope; the first half is a floor every retry always
     * waits, the second half uniform random jitter — what decorrelates
     * concurrently-retrying workers that would otherwise wake in lockstep.
     * Exponent clamped (nexus-99r7y): 429 attempts are budget-bounded, so
     * {@code attempt} can exceed the old 1..3 range — an unclamped shift
     * would overflow past attempt 63 and the envelope should stop growing
     * once it is already budget-scale.
     */
    long backoffDelayMs(int attempt) {
        long cap = retryBaseMs * (1L << Math.min(attempt - 1, 5));
        long floor = cap / 2;
        long jitterSpan = cap - floor;  // remaining half; handles odd cap without losing a unit
        long jitter = jitterSpan > 0 ? (long) (jitterRandom.nextDouble() * jitterSpan) : 0;
        return floor + jitter;
    }

    /**
     * Parse a {@code Retry-After} header value as milliseconds.
     *
     * <p>Voyage sends delay-seconds (integer or decimal). The HTTP-date form
     * and any unparsable value yield {@code null} — treated as "no header",
     * falling back to the computed backoff, never failing the request over a
     * malformed advisory header.
     */
    static Long parseRetryAfterMs(String value) {
        if (value == null || value.isBlank()) return null;
        try {
            double seconds = Double.parseDouble(value.trim());
            if (seconds < 0) return null;
            return (long) (seconds * 1000.0);
        } catch (NumberFormatException e) {
            return null;  // HTTP-date form or garbage: advisory only, ignore
        }
    }

    /**
     * Send {@code req}, retrying per the class contract, and return the 200
     * body.
     *
     * @param http          the caller's client (per-class proxy/timeout wiring)
     * @param req           the built request — immutable, reused across attempts
     * @param deadlineNanos the shared 429 deadline from {@link #newDeadlineNanos()},
     *                      minted once per logical request by the caller
     * @param failures      per-call terminal-failure vocabulary (per-call, not
     *                      constructor state, because {@link VoyageEmbedder}'s
     *                      400 discrimination needs per-call batch context)
     */
    String send(HttpClient http, HttpRequest req, long deadlineNanos, Failures failures) {
        int transientFailures = 0;
        int consecutive429s = 0;
        for (int attempt = 1; ; attempt++) {
            try {
                onPost.run();
                HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
                int status = resp.statusCode();

                if (status == 200) return resp.body();

                if (status == 429) {
                    consecutive429s++;
                    // Honour Voyage's own Retry-After when present; otherwise
                    // the equal-jitter envelope paces the herd.
                    Long retryAfterMs = parseRetryAfterMs(
                            resp.headers().firstValue("Retry-After").orElse(null));
                    long delay = Math.max(backoffDelayMs(attempt),
                                          retryAfterMs != null ? retryAfterMs : 0L);
                    long remainingMs = (deadlineNanos - System.nanoTime()) / 1_000_000L;
                    if (delay + MIN_CALL_ALLOWANCE_MS > remainingMs) {
                        // Fail fast and HONEST, with margin under the edge's
                        // 30s bound: an engine-authored 429 + Retry-After
                        // beats an opaque edge-timeout 5xx (the 2026-08-15
                        // incident shape), and arms the client-side rate
                        // brake (conexus-cy9u7) that keys on the header.
                        long retryAfterS = Math.max(1L, (long) Math.ceil(delay / 1000.0));
                        // No floor on remainingMs — a negative remainder means
                        // the loop ran PAST the budget (e.g. a slow in-flight
                        // call), and the log must say so rather than
                        // under-report elapsed as exactly the budget.
                        long elapsedMs = rateLimitBudgetMs - remainingMs;
                        log.warn("event={} attempts={} consecutive_429s={} elapsed_ms={} retry_after_s={}",
                                 eventRateLimited, attempt, consecutive429s, elapsedMs, retryAfterS);
                        throw new UpstreamRateLimitedException(
                                "Voyage AI is rate limiting (HTTP 429 x" + consecutive429s
                                + " within the " + rateLimitBudgetMs + "ms budget); failing"
                                + " fast before the edge timeout. Retry after ~" + retryAfterS
                                + "s. body=" + resp.body(), retryAfterS);
                    }
                    log.warn("event={} attempt={} status=429 delay_ms={} retry_after_header_ms={}",
                             eventRetry, attempt, delay, retryAfterMs);
                    Thread.sleep(delay);
                    continue;
                }
                consecutive429s = 0;
                if (status >= 500) {
                    transientFailures++;
                    if (transientFailures < MAX_RETRIES) {
                        long delay = backoffDelayMs(transientFailures);
                        log.warn("event={} attempt={} status={} delay_ms={}",
                                 eventRetry, attempt, status, delay);
                        Thread.sleep(delay);
                        continue;
                    }
                }
                if (status == 401 || status == 403) {
                    // nexus-pmhpc: credentials problem, not an engine defect —
                    // typed so handlers return 502-with-detail, not 500.
                    throw new UpstreamAuthException(
                            "Voyage AI rejected the service's API key (HTTP " + status
                            + "): the key is invalid, expired, or lacks scope. Rotate the"
                            + " service's Voyage key and restart. body=" + resp.body());
                }
                throw failures.status(status, resp.body());

            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw failures.wrap(opLabel + " interrupted", e);
            } catch (RuntimeException e) {
                throw e;
            } catch (Exception e) {
                transientFailures++;
                if (transientFailures >= MAX_RETRIES) {
                    throw failures.wrap(opLabel + " failed after " + MAX_RETRIES + " attempts", e);
                }
                try { Thread.sleep(backoffDelayMs(transientFailures)); }
                catch (InterruptedException ix) {
                    Thread.currentThread().interrupt();
                    throw failures.wrap(opLabel + " interrupted", ix);
                }
            }
        }
    }
}
