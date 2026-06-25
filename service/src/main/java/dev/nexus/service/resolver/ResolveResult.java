// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Typed result from {@link UriSchemeResolverRegistry#resolve}.
 *
 * <p>Two shapes: {@link #ok} (content retrieved) and {@link #error} (content
 * unavailable).  No null returns — callers check {@link #isOk()} before
 * accessing {@link #text()}.
 *
 * <p>RDR-169 G3: server-reachable side of the split-by-reachability
 * dispatcher described in the {@link UriSchemeResolverRegistry} javadoc.
 */
public final class ResolveResult {

    private static final Logger log = LoggerFactory.getLogger(ResolveResult.class);

    private final String text;
    private final String sourceUri;
    private final String errorReason;
    private final String errorDetail;

    private ResolveResult(String text, String sourceUri,
                          String errorReason, String errorDetail) {
        this.text        = text;
        this.sourceUri   = sourceUri;
        this.errorReason = errorReason;
        this.errorDetail = errorDetail;
    }

    /** Successful resolution: content was retrieved. */
    public static ResolveResult ok(String text, String sourceUri) {
        if (text == null) {
            throw new IllegalArgumentException("ok result must carry non-null text");
        }
        return new ResolveResult(text, sourceUri, null, null);
    }

    /** Failed resolution: content could not be retrieved.
     *
     * @param reason short token (e.g. {@code "unreachable"}, {@code "reference_only"})
     * @param detail human-readable message; {@code null} is tolerated and treated as
     *               empty string so callers can safely call {@code errorDetail().contains(...)}
     *               without a null check
     */
    public static ResolveResult error(String reason, String detail) {
        String safeDetail = detail != null ? detail : "";
        log.debug("event=resolve_fail reason={} detail={}", reason, safeDetail);
        return new ResolveResult(null, null, reason, safeDetail);
    }

    /** True when the resolution succeeded and {@link #text()} is non-null. */
    public boolean isOk() {
        return text != null;
    }

    /** Retrieved text, or {@code null} on failure. Check {@link #isOk()} first. */
    public String text() {
        return text;
    }

    /** Original source URI on success; {@code null} on failure. */
    public String sourceUri() {
        return sourceUri;
    }

    /** Short error reason token (e.g. {@code "scheme_unknown"}, {@code "unreachable"});
     *  {@code null} on success. */
    public String errorReason() {
        return errorReason;
    }

    /** Human-readable detail; {@code null} on success, never {@code null} on failure
     *  (empty string is substituted when the caller passes {@code null}). */
    public String errorDetail() {
        return errorDetail;
    }

    @Override
    public String toString() {
        return isOk()
            ? "ResolveResult{ok, uri=" + sourceUri + "}"
            : "ResolveResult{error, reason=" + errorReason + ", detail=" + errorDetail + "}";
    }
}
