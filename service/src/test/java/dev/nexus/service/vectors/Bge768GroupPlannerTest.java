// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.Arrays;
import java.util.Random;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The length-grouping planner of {@link Bge768Embedder} ({@code planGroups}), tested without the
 * 416 MB model. A local engine is CPU-bound, and the cost of an ONNX call grows with the padded
 * token count of its group, so the planner's job is to keep rows of similar length together
 * while never breaking the padded-token-area ceiling that bounds memory (nexus-zu4ma).
 */
class Bge768GroupPlannerTest {

    private static final long AREA = 16L * 512 * 512;
    private static final double WASTE = Bge768Embedder.MAX_PAD_WASTE;

    private static long paddedTokens(Bge768Embedder.GroupPlan plan) {
        long total = 0;
        for (int[] g : plan.groups()) total += (long) (g[1] - g[0]) * g[2];
        return total;
    }

    @Test
    void groupsCoverEveryRowOnceAndOrderIsAnAscendingPermutation() {
        int[] lens = {512, 20, 300, 20, 511, 64, 300, 1, 0, 512, 128, 129};
        Bge768Embedder.GroupPlan plan = Bge768Embedder.planGroups(lens, AREA, WASTE);

        int[] seen = plan.order().clone();
        Arrays.sort(seen);
        assertThat(seen).containsExactly(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11);
        for (int k = 1; k < lens.length; k++) {
            assertThat(lens[plan.order()[k]]).isGreaterThanOrEqualTo(lens[plan.order()[k - 1]]);
        }
        int next = 0;
        for (int[] g : plan.groups()) {
            assertThat(g[0]).as("groups are contiguous over the sorted order").isEqualTo(next);
            assertThat(g[1]).isGreaterThan(g[0]);
            next = g[1];
        }
        assertThat(next).isEqualTo(lens.length);
    }

    @Test
    void sortIsStableSoEqualLengthsKeepInputOrder() {
        int[] lens = {100, 100, 100, 100};
        Bge768Embedder.GroupPlan plan = Bge768Embedder.planGroups(lens, AREA, WASTE);
        assertThat(plan.order()).containsExactly(0, 1, 2, 3);
        assertThat(plan.groups().length).isEqualTo(1);
    }

    @Test
    void everyGroupHonoursTheAreaAndWasteBoundsUnlessItIsASingleRow() {
        Random rnd = new Random(42);
        int[] lens = new int[200];
        for (int i = 0; i < lens.length; i++) lens[i] = 1 + rnd.nextInt(512);
        Bge768Embedder.GroupPlan plan = Bge768Embedder.planGroups(lens, AREA, WASTE);

        for (int[] g : plan.groups()) {
            int size = g[1] - g[0];
            long real = 0;
            int max = 0;
            for (int k = g[0]; k < g[1]; k++) {
                int len = Math.max(lens[plan.order()[k]], 1);
                real += len;
                max = Math.max(max, len);
            }
            assertThat(g[2]).as("group width is its longest row").isEqualTo(max);
            if (size > 1) {
                assertThat((long) size * max * max).isLessThanOrEqualTo(AREA);
                assertThat((double) size * max).isLessThanOrEqualTo((1.0 + WASTE) * real);
            }
        }
    }

    @Test
    void uniformLengthsAreBoundedByAreaAloneAsBefore() {
        int[] lens = new int[20];
        Arrays.fill(lens, 512);
        Bge768Embedder.GroupPlan plan = Bge768Embedder.planGroups(lens, AREA, WASTE);
        // 16 * 512^2 is the ceiling: 16 rows, then the remaining 4.
        assertThat(plan.groups().length).isEqualTo(2);
        assertThat(plan.groups()[0][1] - plan.groups()[0][0]).isEqualTo(16);
    }

    /**
     * Non-vacuity: arrival-order batches of 16 over a mixed corpus pad far above their real
     * tokens; the planner's grouping must cut that. Compared against the one-group-per-request
     * shape the engine used before (all 16 rows padded to the longest).
     */
    @Test
    void mixedLengthsPadMuchLessThanOneGroupPerRequest() {
        Random rnd = new Random(7);
        int requests = 200;
        long real = 0;
        long singleGroup = 0;
        long planned = 0;
        for (int r = 0; r < requests; r++) {
            int[] lens = new int[16];
            int max = 0;
            for (int i = 0; i < lens.length; i++) {
                // code-chunk-like: many mid-length rows, a tail at the 512 cap
                lens[i] = rnd.nextInt(10) == 0 ? 512 : 60 + rnd.nextInt(340);
                real += lens[i];
                max = Math.max(max, lens[i]);
            }
            singleGroup += 16L * max;
            planned += paddedTokens(Bge768Embedder.planGroups(lens, AREA, WASTE));
        }
        assertThat((double) singleGroup / real).as("the old shape pads heavily").isGreaterThan(1.4);
        assertThat(planned).as("padding can never be below the real tokens").isGreaterThanOrEqualTo(real);
        assertThat((double) planned / real).as("planned padding").isLessThan(1.2);
        assertThat(planned).isLessThan((long) (singleGroup * 0.8));
    }

    @Test
    void emptyAndSingleRowInputs() {
        assertThat(Bge768Embedder.planGroups(new int[0], AREA, WASTE).groups().length).isEqualTo(0);
        Bge768Embedder.GroupPlan one = Bge768Embedder.planGroups(new int[]{0}, AREA, WASTE);
        assertThat(one.groups().length).isEqualTo(1);
        assertThat(one.groups()[0][2]).as("zero-length text still gets a width of 1").isEqualTo(1);
    }
}
