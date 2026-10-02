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
 * {@code OwnershipGuardCoverageScan} scans the main sources, by name, for the four guarded
 * repository methods ({@code upsertChunksWithTokens}, {@code upsertChunksWithVectors},
 * {@code putWithTokens}, {@code upsertChunks}), so a handler cannot call one of them with a null
 * guard. It matches direct calls and method references as text, so it also reads comments: name a
 * guarded method in prose without the call or the double-colon syntax. It does not see a chunk
 * write that goes through another method or through direct SQL. Known writers outside the guard are
 * the SQL functions whose live definition inserts into {@code nexus.chunks} (the last definition of
 * each function in the changelog's include order, rollback blocks excluded). The source of truth is
 * {@code _CHUNK_INSERTER_ALLOWLIST} in {@code tests/test_changelog_chunk_inserter_lint.py}, each entry
 * with its reason: that lint fails when a function that is not on the list inserts into
 * {@code nexus.chunks}, and when an entry no longer does, so a change that adds such a function (the
 * reaper's and the quarantine restore's) extends the list there and this paragraph needs no edit. It
 * deliberately names none of them here, nor the changeset that defines them, because that goes stale
 * the moment a function is redefined.
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
