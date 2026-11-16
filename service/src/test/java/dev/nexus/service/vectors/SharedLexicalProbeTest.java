// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/** nexus-bbtc2: the shared probe's state machine, with no database. */
class SharedLexicalProbeTest {

    private static final long FAR = Long.MAX_VALUE / 4;

    private static byte[] b(int... v) {
        byte[] out = new byte[v.length];
        for (int i = 0; i < v.length; i++) out[i] = (byte) v[i];
        return out;
    }

    private static List<String> hex(byte[][] chashes) {
        return java.util.Arrays.stream(chashes).map(c -> java.util.HexFormat.of().formatHex(c)).toList();
    }

    @Test
    void aPerCollectionAnswerSeparatesSelectiveDenseAndEmptyCollections() {
        var share = new SharedLexicalProbe();
        share.completePerCollection(Map.of("a", new byte[][] {b(1)}, "b", new byte[][] {b(2), b(3)}), Set.of("dense"),
                                    5_000_000L);
        var kind = share.await(System.nanoTime() + FAR);
        assertThat(kind).isEqualTo(SharedLexicalProbe.Kind.PER_COLLECTION);
        assertThat(share.planFor(kind, "a")).isInstanceOfSatisfying(SharedLexicalProbe.RankChashes.class,
            r -> assertThat(hex(r.chashes())).containsExactly("01"));
        assertThat(share.planFor(kind, "b")).isInstanceOfSatisfying(SharedLexicalProbe.RankChashes.class,
            r -> assertThat(hex(r.chashes())).containsExactly("02", "03"));
        assertThat(share.probeMillis()).isEqualTo(5L);
        assertThat(share.planFor(kind, "dense")).isInstanceOf(SharedLexicalProbe.RankDense.class);
        assertThat(share.planFor(kind, "never-gated")).isInstanceOfSatisfying(SharedLexicalProbe.RankChashes.class,
            r -> assertThat(r.chashes()).isEmpty());
    }

    @Test
    void aFallbackAndATimeoutBothMeanTheArmRunsItsOwnProbe() {
        var failed = new SharedLexicalProbe();
        failed.completeFallback(1L);
        assertThat(failed.planFor(failed.await(System.nanoTime() + FAR), "a"))
            .isInstanceOf(SharedLexicalProbe.OwnProbe.class);

        var never = new SharedLexicalProbe();
        var kind = never.await(System.nanoTime() + 20_000_000L);   // 20 ms, and nobody answers
        assertThat(kind).isEqualTo(SharedLexicalProbe.Kind.FALLBACK);
        assertThat(never.planFor(kind, "a")).isInstanceOf(SharedLexicalProbe.OwnProbe.class);

        var spent = new SharedLexicalProbe();
        assertThat(spent.await(System.nanoTime() - 1L)).as("a deadline already past").isEqualTo(SharedLexicalProbe.Kind.FALLBACK);
    }

    @Test
    void theFirstAnswerWins_aLaterFallbackFromTheFinallyBlockChangesNothing() {
        var share = new SharedLexicalProbe();
        share.completePerCollection(Map.of("a", new byte[][] {b(9)}), Set.of(), 3_000_000L);
        share.completeFallback(99_000_000L);
        assertThat(share.kind()).isEqualTo(SharedLexicalProbe.Kind.PER_COLLECTION);
        assertThat(share.probeMillis()).isEqualTo(3L);
    }

    @Test
    void aWaitingArmWakesWhenTheProbeCompletes() throws Exception {
        var share = new SharedLexicalProbe();
        var kinds = new SharedLexicalProbe.Kind[1];
        Thread waiter = Thread.ofVirtual().start(() -> kinds[0] = share.await(System.nanoTime() + FAR));
        Thread.sleep(30);
        share.completePerCollection(Map.of(), Set.of(), 1L);
        waiter.join(5_000);
        assertThat(kinds[0]).isEqualTo(SharedLexicalProbe.Kind.PER_COLLECTION);
    }

    @Test
    void distinctKeepsFirstSeenOrder_andCollapsesTheSameChashFromTwoCollections() {
        byte[][] out = SharedLexicalProbe.distinct(List.of(b(3), b(1), b(3), b(2), b(1)));
        assertThat(hex(out)).containsExactly("03", "01", "02");
    }
}
