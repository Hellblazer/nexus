// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import dev.nexus.service.http.IdentityTestEnv;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3: a topic no live group serves gets no owner row of any kind; a clear row still goes out for an
 * open owner row. The decision cases run on recorded rows; the stamper cases run the same rules against a real
 * database, where {@code GroupRepository.servingGroup} says whether a group is live ({@code ended_at}, {@code ends},
 * or no record).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerDecisionServingTest {

    StamperEnv se;

    @BeforeAll
    void up() throws Exception {
        se = new StamperEnv();
    }

    @AfterAll
    void down() throws Exception {
        se.close();
    }

    private OwnerFixture red() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        return f;
    }

    @Test
    void aRedOnATopicNoLiveGroupServesGetsNoOwnerRow() {
        OwnerFixture f = red();
        f.served = false;

        assertThat(f.decide().post()).isNull();
        assertThat(f.decide().deadline()).isNull();
    }

    @Test
    void aRefreshOrAHandOffNeedsALiveGroupToo() {
        OwnerFixture f = red();
        f.recordNow(f.decide());
        f.served = false;
        f.push("Z", "Y", "A").done("Z", "failure");
        assertThat(f.decide().post()).as("no refresh row").isNull();
        f.now(f.now.plusSeconds(10 * 3600));
        assertThat(f.onDeadline().post()).as("no hand-off row").isNull();
    }

    @Test
    void aGreenOnATopicWithAnOpenOwnerRowStillClearsWhateverTheGroupsState() {
        OwnerFixture f = red();
        f.recordNow(f.decide());
        f.served = false;
        f.push("Z", "Y", "A").done("Z", "success");

        OwnerDecision.Post clear = f.decide().post();

        assertThat(clear).isNotNull();
        assertThat(clear.kind()).isEqualTo("clear");
        assertThat(clear.sha()).isEqualTo("Z");
        assertThat(clear.body()).isEmpty();
    }

    @Test
    void aGreenOnATopicWithNoOpenOwnerRowPostsNothing() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.served = false;
        f.push("X", "G0", "A").done("X", "success");

        assertThat(f.decide().post()).isNull();
    }

    // ── against a real database ──────────────────────────────────────────────

    /** Seats L (leader), A and C, a group serving the topic, and the pushes X (A), Y (C). Returns [t, L, A, C]. */
    private String[] fixture(String ends) {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.formEnding(t, l, ends, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        return new String[] {t, l, a, c};
    }

    @Test
    void aRedOnALiveServedTopicPostsTheOwnerRow() {
        String[] f = fixture(null);

        se.done(f[0], StamperEnv.BASE, "Y", "failure");

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst().kind()).isEqualTo("owner");
        assertThat(rows.getFirst().from()).isEqualTo("engine");
        assertThat(rows.getFirst().sha()).isEqualTo("Y");
        assertThat(rows.getFirst().body()).containsEntry("owner", f[2]).containsEntry("next", f[3])
            .containsEntry("suspects_total", 2).containsEntry("refresh", false);
    }

    @Test
    void aRedOnATopicNoGroupServesGetsNoOwnerRow() {
        String[] f = fixture(null);
        se.push(f[0], "nexus-other", "X", "G0", "sess-alpha");

        se.done(f[0], "nexus-other", "X", "failure");

        assertThat(se.ownerRows(f[0], "nexus-other")).isEmpty();
    }

    @Test
    void aDisbandedGroupGetsNoOwnerRowAndTheGreenStillClears() {
        String[] f = fixture(null);
        se.done(f[0], StamperEnv.BASE, "Y", "failure");
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).hasSize(1);
        assertThat(se.env.as(f[0], f[1], "POST", "/push-train/disband", Map.of()).status()).isEqualTo(200);

        se.push(f[0], StamperEnv.BASE, "Z", "Y", "sess-alpha");
        se.done(f[0], StamperEnv.BASE, "Z", "failure");
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).as("ended_at is set: no refresh row").hasSize(1);
        se.stamper.onDeadline(f[0], StamperEnv.BASE);
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).as("and no hand-off row").hasSize(1);

        se.push(f[0], StamperEnv.BASE, "W", "Z", "sess-alpha");
        se.done(f[0], StamperEnv.BASE, "W", "success");
        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).hasSize(2);
        assertThat(rows.getLast().kind()).isEqualTo("clear");
        assertThat(rows.getLast().sha()).isEqualTo("W");
    }

    @Test
    void aGroupWhoseEndsHasPassedGetsNoOwnerRow() {
        String[] f = fixture(se.env.ids.clock.instant().plus(Duration.ofHours(1)).toString());
        se.env.ids.clock.advance(Duration.ofHours(2));

        se.done(f[0], StamperEnv.BASE, "Y", "failure");

        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).isEmpty();
    }
}
