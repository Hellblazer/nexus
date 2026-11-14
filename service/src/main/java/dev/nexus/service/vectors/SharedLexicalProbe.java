// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * The text-gate probe of one {@code searchPerCollection} request's lexical leg, run ONCE for every collection
 * instead of once per collection arm (nexus-bbtc2).
 *
 * <p>The trigram and tsvector GIN indexes are per leaf (tenant x model), so a one-collection probe rebuilds the
 * leaf-wide bitmap for the pattern before it ANDs it down to its collection: about 65 ms an arm for an identifier,
 * 120 arms. One statement over all the request's collections pays that once: {@code
 * text_gate_probe_by_collection_<dim>} (vectors-060) says, per collection, which chashes the gate admits or that the
 * collection is dense ({@link Kind#PER_COLLECTION}), and an arm ranks exactly the rows its own one-collection probe
 * would have handed it, so the lexical leg's rows and their order are what the per-arm path returns.
 *
 * <p>One instance is shared by the request's worker threads: the probe task completes it once, the lexical arms
 * {@link #await} it. It never blocks past the fan-out deadline and never fails an arm: anything but a clean answer
 * is {@link Kind#FALLBACK}, and the arm then runs its own probe as it did before this class existed.
 */
final class SharedLexicalProbe {

    /** How the shared probe answered. */
    enum Kind {
        /** Still running (before {@link #complete}); {@link #await} never returns it. */
        PENDING,
        /**
         * The per-collection statement answered: each collection either has its own chash set (at most {@code
         * selectiveGateMax} rows), or is dense (more) and ranks HNSW-first, or has no gated row.
         */
        PER_COLLECTION,
        /** The shared probe failed, timed out or was never run: every arm runs its own probe. */
        FALLBACK
    }

    /** What one collection's arm does with the shared answer. */
    sealed interface ArmPlan permits OwnProbe, RankChashes, RankDense {}

    /** Run the arm's own probe, as the per-arm path always did. */
    record OwnProbe() implements ArmPlan {}

    /** Rank exactly these chashes (empty: the gate admitted nothing in this collection). */
    record RankChashes(byte[][] chashes) implements ArmPlan {}

    /** The collection's gate admitted more than {@code selectiveGateMax} rows: the dense, HNSW-first rank. */
    record RankDense() implements ArmPlan {}

    private final CountDownLatch done = new CountDownLatch(1);
    private volatile Kind kind = Kind.PENDING;
    private volatile Map<String, byte[][]> byCollection = Map.of();
    private volatile Set<String> dense = Set.of();
    private volatile long probeNanos;

    /** Distinct chashes of {@code rows}, in first-seen order; the same chunk text can be a row of several collections. */
    static byte[][] distinct(Iterable<byte[]> rows) {
        Set<ByteKey> seen = new LinkedHashSet<>();
        for (byte[] r : rows) {
            seen.add(new ByteKey(r));
        }
        byte[][] out = new byte[seen.size()][];
        int i = 0;
        for (ByteKey k : seen) {
            out[i++] = k.bytes;
        }
        return out;
    }

    /** A byte[] with value equality, for de-duplication. */
    private record ByteKey(byte[] bytes) {
        @Override public boolean equals(Object o) {
            return o instanceof ByteKey k && java.util.Arrays.equals(bytes, k.bytes);
        }

        @Override public int hashCode() {
            return java.util.Arrays.hashCode(bytes);
        }
    }

    /**
     * The per-collection statement answered. {@code chashesByCollection} holds the collections whose gate admitted
     * at most the cap (never empty arrays; a collection without a gated row is absent), {@code denseCollections}
     * the ones it admitted more rows of.
     */
    void completePerCollection(Map<String, byte[][]> chashesByCollection, Set<String> denseCollections, long nanos) {
        this.byCollection = new HashMap<>(chashesByCollection);
        this.dense = Set.copyOf(denseCollections);
        finish(Kind.PER_COLLECTION, nanos);
    }

    /** The shared probe did not answer; every arm runs its own. Safe to call after another complete: first wins. */
    void completeFallback(long nanos) {
        finish(Kind.FALLBACK, nanos);
    }

    private synchronized void finish(Kind k, long nanos) {
        if (kind == Kind.PENDING) {
            probeNanos = nanos;
            kind = k;
        }
        done.countDown();
    }

    /**
     * Wait for the answer, no longer than {@code deadlineNanos} ({@link System#nanoTime} base). An answer that has
     * not come by then is {@link Kind#FALLBACK}: the arm's own probe then meets the same deadline itself.
     */
    Kind await(long deadlineNanos) {
        try {
            long remaining = deadlineNanos - System.nanoTime();
            if (remaining > 0L && done.await(remaining, TimeUnit.NANOSECONDS)) {
                return kind;
            }
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
        }
        return done.getCount() == 0L ? kind : Kind.FALLBACK;
    }

    Kind kind() {
        return kind;
    }

    /** Milliseconds the shared probe's statements took, for the request's log line; 0 before it completes. */
    long probeMillis() {
        return probeNanos / 1_000_000L;
    }

    /** What the arm of {@code collection} does once {@link #await} returned {@code answered}. */
    ArmPlan planFor(Kind answered, String collection) {
        return switch (answered) {
            case PER_COLLECTION -> dense.contains(collection)
                ? new RankDense()
                : new RankChashes(byCollection.getOrDefault(collection, new byte[0][]));
            case PENDING, FALLBACK -> new OwnProbe();
        };
    }
}
