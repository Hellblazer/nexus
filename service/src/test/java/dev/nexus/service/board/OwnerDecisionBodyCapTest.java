// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-252 Step 3: the template caps a body at 1024 bytes, so at most ten suspects are listed. */
class OwnerDecisionBodyCapTest {

    private static String sha(int i) {
        return String.format("%040x", i + 1);
    }

    @Test
    void aRedWithTwelveSuspectsListsTenAndCountsTwelve() {
        OwnerFixture f = new OwnerFixture().seat("A");
        String base = "0".repeat(40);
        for (int i = 0; i < 12; i++) {
            f.push(sha(i), i == 0 ? base : sha(i - 1), "A");
        }
        f.done(sha(11), "failure");

        OwnerDecision.Post post = f.decide().post();

        Map<String, Object> b = OwnerFixture.bodyOf(post);
        assertThat((List<?>) b.get("suspects")).hasSize(10).first().isEqualTo(sha(0));
        assertThat(b).containsEntry("suspects_total", 12);
        assertThat(post.bodyJson().getBytes(StandardCharsets.UTF_8).length).isLessThanOrEqualTo(1024);
    }

    @Test
    void aBodyThatWouldPassTheCapDropsSuspectsUntilItFits() {
        String longId = "i" + "9".repeat(300);
        Map<String, Object> b = OwnerDecision.body(longId, longId, "earliest_suspect",
            java.util.stream.IntStream.range(0, 10).mapToObj(OwnerDecisionBodyCapTest::sha).toList(), 10,
            OwnerFixture.T0, false, 0);

        assertThat(OwnerDecision.json(b).getBytes(StandardCharsets.UTF_8).length).isLessThanOrEqualTo(1024);
        assertThat(b).containsEntry("suspects_total", 10);
    }

    @Test
    void aliasesStampedAfterTheDecisionKeepTheBodyInsideTheCapAndLeaveItsOwnFieldsFirst() {
        String longId = "i" + "9".repeat(280);
        OwnerDecision.Post post = new OwnerDecision.Post("owner", sha(0), OwnerDecision.body(longId, longId, "earliest_suspect",
            java.util.stream.IntStream.range(0, 10).mapToObj(OwnerDecisionBodyCapTest::sha).toList(), 10, OwnerFixture.T0, false, 0));

        OwnerDecision.Post stamped = OwnerDecision.withAliases(post, "a".repeat(64), "b".repeat(64));

        assertThat(stamped.bodyJson().getBytes(StandardCharsets.UTF_8).length).isLessThanOrEqualTo(1024);
        assertThat(stamped.body()).containsEntry("owner_alias", "a".repeat(64)).containsEntry("next_alias", "b".repeat(64))
            .containsEntry("suspects_total", 10);
        assertThat(stamped.body().keySet()).containsSubsequence("owner", "owner_alias", "next", "next_alias", "reason");
        assertThat(OwnerDecision.withAliases(post, null, null)).as("no alias, no change").isSameAs(post);
    }
}
