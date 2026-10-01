// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.Objects;

/**
 * What a chunk-write route asks of {@link PgVectorRepository}: check that every chash in the
 * request has a live manifest row in the collection, and either refuse ({@link
 * OwnerlessWriteMode#ENFORCE}) or log and count ({@link OwnerlessWriteMode#LOG_ONLY}).
 *
 * <p>The check is requested by the HANDLER, through this guard, and is not part of the
 * repository's write methods: a repository method called without a guard (the contract and
 * fixture tests, the migration ingest) writes ownerless chunks as it always did.
 * {@code OwnershipGuardCoverageScan} scans the main sources so a handler cannot call a guarded
 * repository method with a null guard, or add a new chunk-write route that never builds one. The
 * scan matches direct calls and method references to the guarded methods, as text, so it also reads
 * comments: name a guarded method in prose without the call or the double-colon syntax.
 *
 * @param mode          enforce or log-only
 * @param route         the route name for the error and the log, e.g. {@code upsert-chunks}
 * @param userAgent     the request's {@code User-Agent}, for the log line; may be null
 * @param clientVersion the request's {@code X-Nexus-Client-Version}, for the log line; null or blank
 *                      means the header was absent, which is a client older than the cut that sends it
 */
public record OwnershipGuard(OwnerlessWriteMode mode, String route, String userAgent, String clientVersion) {

    public OwnershipGuard {
        Objects.requireNonNull(mode, "mode");
        Objects.requireNonNull(route, "route");
    }

    /** A guard that names no client (tests, internal callers). */
    public OwnershipGuard(OwnerlessWriteMode mode, String route) {
        this(mode, route, null, null);
    }
}
