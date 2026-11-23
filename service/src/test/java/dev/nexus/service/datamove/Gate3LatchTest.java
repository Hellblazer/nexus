// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.4 (linda, 2026-11-23): the engine reads the gate 3 latch synchronously at boot, retries a failed read with backoff, and never
 * answers "not latched" because a read failed.
 */
class Gate3LatchTest {

    /** A clock the fake sleeper moves. */
    static final class MovingClock extends Clock {
        Instant now = Instant.parse("2026-11-23T00:00:00Z");

        @Override public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override public Instant instant() {
            return now;
        }
    }

    private static final Duration FIRST = Duration.ofMillis(500);
    private static final Duration MAX = Duration.ofSeconds(4);
    private static final Duration DEADLINE = Duration.ofMinutes(5);

    @Test
    void aRowReadsLatched_withItsPosture_andNoRowReadsNotLatched_withoutAnyWait() {
        var clock = new MovingClock();
        List<Duration> waits = new ArrayList<>();

        var latched = Gate3Latch.readBlocking(() -> Optional.of(true), FIRST, MAX, DEADLINE, waits::add, clock);
        var plain = Gate3Latch.readBlocking(() -> Optional.of(false), FIRST, MAX, DEADLINE, waits::add, clock);
        var none = Gate3Latch.readBlocking(Optional::empty, FIRST, MAX, DEADLINE, waits::add, clock);

        assertThat(latched.latched()).isTrue();
        assertThat(latched.pciOff()).isTrue();
        assertThat(plain.latched()).isTrue();
        assertThat(plain.pciOff()).isFalse();
        assertThat(none.latched()).as("no row is an answer: not latched").isFalse();
        assertThat(none.pciOff()).isFalse();
        assertThat(waits).as("a read that answers waits for nothing").isEmpty();
    }

    @Test
    void aReadThatThrows_isRetriedWithDoublingBackoff_andNeverAnswersFalse() {
        var clock = new MovingClock();
        var calls = new AtomicInteger();
        List<Duration> waits = new ArrayList<>();

        var read = Gate3Latch.readBlocking(() -> {
            if (calls.incrementAndGet() <= 5) {
                throw new IllegalStateException("the database is not answering");
            }
            return Optional.of(false);
        }, FIRST, MAX, DEADLINE, d -> {
            waits.add(d);
            clock.now = clock.now.plus(d);
        }, clock);

        assertThat(read.latched()).as("the row exists once the read answers").isTrue();
        assertThat(calls.get()).isEqualTo(6);
        assertThat(waits).as("doubling, capped at the maximum").containsExactly(
            Duration.ofMillis(500), Duration.ofSeconds(1), Duration.ofSeconds(2), Duration.ofSeconds(4), Duration.ofSeconds(4));
    }

    @Test
    void aReadThatNeverAnswers_failsTheBootAtTheDeadline_insteadOfGuessing() {
        var clock = new MovingClock();
        var calls = new AtomicInteger();

        assertThatThrownBy(() -> Gate3Latch.readBlocking(() -> {
            calls.incrementAndGet();
            throw new IllegalStateException("the database is down");
        }, FIRST, MAX, Duration.ofSeconds(20), d -> clock.now = clock.now.plus(d), clock))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("not serving on a guess")
            .hasMessageContaining("the database is down");

        assertThat(calls.get()).as("it kept trying until the deadline").isGreaterThan(3);
        assertThat(clock.now).as("and did not wait past it").isBeforeOrEqualTo(Instant.parse("2026-11-23T00:00:20Z"));
    }

    @Test
    void aGivenLatchStateIsWhatItSays() {
        assertThat(Gate3Latch.of(true, true).pciOff()).isTrue();
        assertThat(Gate3Latch.of(false, true).pciOff()).as("posture only means something with a row").isFalse();
    }
}
