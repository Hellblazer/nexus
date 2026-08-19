// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

/**
 * RDR-195 — Voyage AI rejected a batch as exceeding the per-request token
 * ceiling (HTTP 400, {@code error_code: "TOO_MANY_TOKENS_IN_BATCH"}).
 *
 * <p>Distinct from every other {@code callApi} failure: it is precisely
 * described, machine-readable, and actionable — the batch, not the
 * credentials or the upstream service, is at fault. {@link VoyageEmbedder}'s
 * sub-batch caller catches this, halves the offending sub-batch, and retries
 * the halves (RDR-195 Technical Design — the split lives ABOVE {@code
 * callApi} so this typed error and the transient 429/5xx retry path stay
 * orthogonal). It escapes to a caller only in two cases, both genuine and
 * both meant to fail loudly rather than silently drop data:
 * <ul>
 *   <li>a single text alone still trips the ceiling (an un-splittable
 *       input — an upstream contract change, or a model whose context
 *       exceeds its own batch ceiling);</li>
 *   <li>the adaptive-split budget ({@code MAX_SUB_REQUESTS_PER_BATCH}) is
 *       exhausted before the batch converges.</li>
 * </ul>
 *
 * <p>Parallel to {@link UpstreamAuthException}'s typed-exception convention,
 * but not currently given a bespoke {@code VectorHandler} status mapping —
 * both escape cases are rare-and-defensive by design (Technical Design's
 * termination argument), so the existing generic 500 arm is an acceptably
 * loud outcome; only the message detail matters here.
 */
public class VoyageTooManyTokensException extends RuntimeException {
    public VoyageTooManyTokensException(String message) {
        super(message);
    }
}
