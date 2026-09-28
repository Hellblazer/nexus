// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.concurrent.atomic.AtomicLong;

/**
 * RDR-222 Phase 0 (bead nexus-ulrjq) — process-wide, lifetime counter of "raced
 * embeds": a chash a request's existence partition found ABSENT (so the request
 * paid to embed it) that, by the time its final {@code INSERT ... ON CONFLICT}
 * ran, had already been committed by another concurrent writer (RDR-181's
 * existence-check-then-embed window, RDR-222's M-b). Detected via the
 * {@code RETURNING chash, (xmax = 0) AS inserted} idiom on that INSERT.
 *
 * <p>Two feeders, one counter: {@link PgVectorRepository#upsertChunksInternal}'s
 * final multi-row insert, and the combined-write per-doc path
 * ({@code CatalogRepository#upsertManifestChunkVectors}, fed by {@code
 * CombinedWriteService}'s own existence-partition). Global/JVM-wide by design —
 * same shape as {@code DeadlockRetry#RETRY_ATTEMPTS} — there is no per-tenant or
 * per-collection dimension on the wire for this counter at Phase 0.
 *
 * <p>Deliberately NOT a field on {@link EmbedActivitySnapshot}: that record is
 * inherently PER-EMBEDDER (one snapshot per {@code Embedder#modelToken()} in
 * {@code GET /v1/status}'s {@code embedder_activity} map), and a raced embed is
 * a DB-write-layer phenomenon with no embedder dimension — duplicating one
 * process-wide value into every embedder's entry would read as per-embedder
 * data it is not. {@code StatusHandler} surfaces it instead as a sibling
 * top-level {@code raced_embeds_total} field.
 */
public final class RacedEmbedActivity {

    private static final AtomicLong RACED_EMBEDS_TOTAL = new AtomicLong();

    private RacedEmbedActivity() {
    }

    /** Record {@code count} raced embeds detected in one write. No-op for {@code count <= 0}. */
    public static void record(long count) {
        if (count > 0) {
            RACED_EMBEDS_TOTAL.addAndGet(count);
        }
    }

    /** @return total raced embeds detected so far, process-wide, since boot. */
    public static long total() {
        return RACED_EMBEDS_TOTAL.get();
    }

    /** Test-only reset — production code never calls this. */
    public static void resetForTests() {
        RACED_EMBEDS_TOTAL.set(0);
    }
}
