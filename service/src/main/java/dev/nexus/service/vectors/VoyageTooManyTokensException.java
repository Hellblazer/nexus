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
 * <p><b>Gate remediation (nexus-kmtlp.11 fix 2, 2026-08-19).</b> Parallel to
 * {@link UpstreamAuthException}'s typed-exception convention, and — unlike
 * the original design note this javadoc used to carry — {@code
 * VectorHandler} DOES give this a bespoke status mapping: <strong>422</strong>
 * (well-formed request, unservable as submitted), carrying {@link
 * #errorCode()}/{@link #getMessage()}/{@link #subRequests()}/{@link
 * #batchSize()}/{@link #model()} in the response body. The substantive-critic
 * (T2 {@code substantive-critique-rdr195-phase2-da9c61781-2026-08-19} finding
 * 2) verified that landing on the generic 500 arm laundered this exception's
 * detail exactly like the pre-RDR-195 opaque 500 it exists to replace — the
 * detail reached only the engine log, never the caller. The structured
 * fields below exist so {@code VectorHandler} does not have to parse them
 * back out of the free-text message.
 */
public class VoyageTooManyTokensException extends RuntimeException {

    private final String errorCode;
    private final String model;
    private final int    batchSize;
    private final int    subRequests;

    /**
     * @param message     human-readable detail (may include the raw upstream body)
     * @param errorCode   always {@code "TOO_MANY_TOKENS_IN_BATCH"} today — carried as a
     *                    field (not re-derived from {@code message}) so a future second
     *                    discriminator does not require a message-parsing change downstream
     * @param model       the {@link VoyageEmbedder} instance's model, e.g. {@code "voyage-code-3"}
     * @param batchSize   the offending sub-batch's text count (1 for the un-splittable-text
     *                    escape; the sub-batch size at exhaustion for the budget escape)
     * @param subRequests sub-requests already attempted for this TOP-LEVEL {@code embed}/
     *                    {@code embedWithUsage}/{@code embedDouble} call when this exception
     *                    was raised (see {@link VoyageEmbedder#MAX_SUB_REQUESTS_PER_BATCH}'s
     *                    javadoc for the per-planned-batch scope this counts within)
     */
    public VoyageTooManyTokensException(String message, String errorCode, String model,
                                         int batchSize, int subRequests) {
        super(message);
        this.errorCode  = errorCode;
        this.model      = model;
        this.batchSize  = batchSize;
        this.subRequests = subRequests;
    }

    public String errorCode() {
        return errorCode;
    }

    public String model() {
        return model;
    }

    public int batchSize() {
        return batchSize;
    }

    public int subRequests() {
        return subRequests;
    }
}
