// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3: the timer after an engine restart. The rebuild re-evaluates each topic from its rows and arms what
 * the decision names, so a restart is the same read as any other: it keeps the deadline of an open red and revives
 * none that a newer row superseded.
 */
class AckDeadlineTimerRestartTest {

    private static final Instant EPISODE = OwnerFixture.T0.plus(Duration.ofHours(1));

    private OwnerFixture openRed() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        f.recordNow(f.decide());
        return f;
    }

    @Test
    void aRestartKeepsTheDeadline() {
        OwnerFixture f = openRed();
        f.now(EPISODE.plus(Duration.ofMinutes(5)));   // the engine is back five minutes later

        OwnerDecision.Decision d = f.decide();

        assertThat(d.post()).as("the rebuild posts nothing for an unchanged red").isNull();
        assertThat(d.deadline()).isEqualTo(EPISODE.plus(Duration.ofMinutes(20)));
    }

    @Test
    void aRestartAfterARefreshKeepsTheOriginalDeadline() {
        OwnerFixture f = openRed();
        f.push("Z", "Y", null).done("Z", "failure");   // an unstamped push (no pusher is known): no sign of life of anyone
        f.recordNow(f.decide());   // the refreshed row, same episode
        f.now(EPISODE.plus(Duration.ofMinutes(10)));

        assertThat(f.decide().deadline()).isEqualTo(EPISODE.plus(Duration.ofMinutes(20)));
    }

    @Test
    void aDeadlineAlreadyPastIsDueAtOnce() {
        OwnerFixture f = openRed();
        f.now(EPISODE.plus(Duration.ofHours(3)));

        assertThat(f.decide().deadline()).isBefore(f.now);
        assertThat(f.onDeadline().post()).as("the first deadline passed while the engine was down: hand-off").isNotNull();
    }

    @Test
    void noDeadlineIsRevivedForAnEpisodeANewerRowSuperseded() {
        OwnerFixture cleared = openRed();
        cleared.push("Z", "Y", "A").done("Z", "success");
        cleared.recordNow(cleared.decide());               // the clear row
        assertThat(cleared.decide().deadline()).isNull();

        OwnerFixture handed = openRed();
        handed.now(EPISODE.plus(Duration.ofMinutes(21)));
        handed.recordNow(handed.onDeadline());             // the hand-off row is newer
        assertThat(handed.decide().deadline()).as("counted from the hand-off row's episode_at")
            .isEqualTo(EPISODE.plus(Duration.ofMinutes(21)).plus(Duration.ofMinutes(20)));

        OwnerFixture acked = openRed();
        acked.ack("A", "Y");
        assertThat(acked.decide().deadline()).as("a counting ack ends the hand-off deadline: the stall deadline replaces it")
            .isEqualTo(acked.owners.getLast().createdAt().plus(Duration.ofMinutes(60)));
    }

    @Test
    void aRedWhoseLatestRowIsEscalatedIsSkipped() {
        OwnerFixture f = openRed();
        f.now(EPISODE.plus(Duration.ofMinutes(21)));
        f.recordNow(f.onDeadline());
        f.now(EPISODE.plus(Duration.ofMinutes(50)));
        f.recordNow(f.onDeadline());                       // escalated

        OwnerDecision.Decision d = f.decide();

        assertThat(d.post()).isNull();
        assertThat(d.deadline()).isNull();
    }
}
