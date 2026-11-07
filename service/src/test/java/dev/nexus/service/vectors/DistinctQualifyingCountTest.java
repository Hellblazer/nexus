// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-h7kw2: {@link PgVectorRepository#distinctQualifying}, the distinct-chunk count of a route (a) short page, reads
 * the cap first and widens to twice the cap only when that read holds repeats. The function under it is streaming, so
 * what the count costs is which reads it makes; this class feeds it a fake {@code manifest_qualifying_hits} (a fixed
 * list of keys, cut at the {@code n} asked for) and records the reads.
 *
 * <p>The decision it must keep (nexus-zk7rw): the count is compared with a page one row shorter than the cap, so any
 * answer at or above the cap means "re-run" and any answer below it must be exact, never lower than the distinct chunks
 * that exist. Integration pins of the same two paths, through the engine: {@code ManifestSearchShortPageIntegrationTest}.
 */
class DistinctQualifyingCountTest {

    /** A fake streaming function over {@code source}; every read it is asked for is recorded in {@code reads}. */
    private static final class Fake {
        final List<String> source;
        final List<Integer> reads = new ArrayList<>();

        Fake(String... source) {
            this.source = List.of(source);
        }

        List<String> read(int n) {
            reads.add(n);
            return source.subList(0, Math.min(n, source.size()));
        }
    }

    /** {@code count} distinct keys. */
    private static String[] distinctKeys(int count) {
        String[] keys = new String[count];
        for (int i = 0; i < count; i++) {
            keys[i] = "c/" + i;
        }
        return keys;
    }

    /** {@code copies} of one key. */
    private static String[] repeated(String key, int copies) {
        String[] keys = new String[copies];
        java.util.Arrays.fill(keys, key);
        return keys;
    }

    private static String[] concat(String[] a, String[] b) {
        String[] out = java.util.Arrays.copyOf(a, a.length + b.length);
        System.arraycopy(b, 0, out, a.length, b.length);
        return out;
    }

    private static long widens() {
        return PgVectorRepository.qualifyingWidenCount();
    }

    @Test
    void aPageOfOneOwnerChunks_isDecidedByTheFirstRead_atTheCap() {
        Fake fake = new Fake(distinctKeys(100));
        long before = widens();

        int count = PgVectorRepository.distinctQualifying(11, fake::read);

        assertThat(count).as("at least the cap: the page of 10 is short of it").isEqualTo(11);
        assertThat(fake.reads).as("one read, of the cap and not twice it").containsExactly(11);
        assertThat(widens() - before).isZero();
    }

    @Test
    void whenTheFunctionRunsOutUnderTheCap_theCountIsTheDistinctChunksReturned_andNothingIsWidened() {
        Fake fewDistinct = new Fake("c/0", "c/1", "c/2");
        Fake withRepeats = new Fake("c/0", "c/0", "c/1");
        long before = widens();

        assertThat(PgVectorRepository.distinctQualifying(11, fewDistinct::read)).isEqualTo(3);
        assertThat(PgVectorRepository.distinctQualifying(11, withRepeats::read))
            .as("a short read is exact: three owners of two chunks").isEqualTo(2);

        assertThat(fewDistinct.reads).containsExactly(11);
        assertThat(withRepeats.reads).containsExactly(11);
        assertThat(widens() - before).isZero();
    }

    @Test
    void aFirstReadWithRepeats_isWidenedToTwiceTheCap_andCountsExactlyWhenTheFunctionRunsOut() {
        // cap 4: the first read is [a, a, b, b]: four keys, two chunks. Eight keys are asked for and the function holds
        // only these four, so it has run out and the count is the two distinct chunks.
        Fake fake = new Fake("c/a", "c/a", "c/b", "c/b");
        long before = widens();

        int count = PgVectorRepository.distinctQualifying(4, fake::read);

        assertThat(count).isEqualTo(2);
        assertThat(fake.reads).as("the cap, then twice the cap").containsExactly(4, 8);
        assertThat(widens() - before).isEqualTo(1);
    }

    @Test
    void aFirstReadWithRepeats_whoseWideReadHoldsMoreChunks_countsThem() {
        // cap 4: [a, a, b, c] is three chunks. The wide read of 8 is [a, a, b, c, d, e, f, g], seven chunks, and it fills
        // its bound, so the count is at least 7 (any value at or above the cap re-runs).
        Fake fake = new Fake("c/a", "c/a", "c/b", "c/c", "c/d", "c/e", "c/f", "c/g", "c/h");
        long before = widens();

        int count = PgVectorRepository.distinctQualifying(4, fake::read);

        assertThat(count).isGreaterThanOrEqualTo(4);
        assertThat(fake.reads).containsExactly(4, 8);
        assertThat(widens() - before).isEqualTo(1);
    }

    @Test
    void aWideReadThatFillsItsBoundBelowTheCap_answersTheCap_notTheFewChunksItSaw() {
        // nexus-zk7rw review r1 blocker 1: one chunk, many owners. Eight keys of one chunk fill the bound of 8 with a
        // single distinct chunk, and a larger set might hold more: the answer is the cap, a re-run, never the 1.
        Fake fake = new Fake(repeated("c/a", 20));
        long before = widens();

        int count = PgVectorRepository.distinctQualifying(4, fake::read);

        assertThat(count).isEqualTo(4);
        assertThat(fake.reads).containsExactly(4, 8);
        assertThat(widens() - before).isEqualTo(1);
    }

    @Test
    void aWideReadOneKeyUnderItsBound_isExact_evenWhenItHoldsFewerChunksThanTheCap() {
        // Seven keys of two chunks, a bound of 8: the function ran out, so the two chunks are all there are.
        Fake fake = new Fake(concat(repeated("c/a", 4), repeated("c/b", 3)));
        long before = widens();

        int count = PgVectorRepository.distinctQualifying(4, fake::read);

        assertThat(count).isEqualTo(2);
        assertThat(fake.reads).containsExactly(4, 8);
        assertThat(widens() - before).isEqualTo(1);
    }

    @Test
    void theEmptyPageProbe_aCapOfOne_neverWidens() {
        long before = widens();

        Fake some = new Fake(repeated("c/a", 5));
        assertThat(PgVectorRepository.distinctQualifying(1, some::read)).isEqualTo(1);
        assertThat(some.reads).containsExactly(1);

        Fake none = new Fake();
        assertThat(PgVectorRepository.distinctQualifying(1, none::read)).isZero();
        assertThat(none.reads).containsExactly(1);

        assertThat(widens() - before).isZero();
    }

    @Test
    void aCapOfDistinctChunksExactly_isDecidedByTheFirstRead_evenWhenTheFunctionHasMore() {
        // rows == cap and distinct == cap: the count is at least the cap, so no wider read can change the decision.
        Fake fake = new Fake(concat(distinctKeys(5), repeated("c/0", 50)));
        long before = widens();

        assertThat(PgVectorRepository.distinctQualifying(5, fake::read)).isEqualTo(5);

        assertThat(fake.reads).containsExactly(5);
        assertThat(widens() - before).isZero();
    }
}
