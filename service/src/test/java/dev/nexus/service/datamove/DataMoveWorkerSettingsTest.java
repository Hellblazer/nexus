// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Map;
import java.util.Set;

/** nexus-f3yqd.2.13: the parts of the worker that need no database: the sleep override's clamp and the in-memory states. */
class DataMoveWorkerSettingsTest {

    private static Integer override(String value) {
        Map<String, String> env = value == null ? Map.of() : Map.of(DataMoveWorker.SLEEP_ENV, value);
        return DataMoveWorker.Settings.fromEnv(env::get).sleepOverrideMs();
    }

    @Test
    void sleepOverride_isClampedToTheRegistrysBounds() {
        assertThat(override(null)).isNull();
        assertThat(override("")).isNull();
        assertThat(override("250")).isEqualTo(250);
        assertThat(override("0")).isEqualTo(0);
        assertThat(override("-5")).isEqualTo(0);
        assertThat(override("60000")).isEqualTo(60_000);
        assertThat(override("100000")).isEqualTo(60_000);
        assertThat(override("99999999999999")).isEqualTo(60_000);
        assertThat(override("fast")).as("not a number: ignored").isNull();
    }

    private static DataMove move(String label) {
        return new DataMove(label, "t", "s", "t", 10, 0, 1000, "v", null, null, null, null, 0, null, null);
    }

    @Test
    void moveStates_keepTheTimeAMoveFirstWasBlockedForTheSameReason() {
        var clock = new Clock() {
            Instant now = Instant.parse("2026-11-16T00:00:00Z");

            @Override
            public java.time.ZoneId getZone() {
                return ZoneOffset.UTC;
            }

            @Override
            public Clock withZone(java.time.ZoneId zone) {
                return this;
            }

            @Override
            public Instant instant() {
                return now;
            }
        };
        var states = new MoveStates(clock);
        var m = move("a");
        states.blocked(m, "no slot");
        Instant first = clock.now;
        clock.now = first.plus(Duration.ofMinutes(1));
        states.blocked(m, "no slot");
        var same = states.state("a").orElseThrow();
        assertThat(same.since()).isEqualTo(first);
        assertThat(same.updatedAt()).isEqualTo(clock.now);
        clock.now = clock.now.plus(Duration.ofMinutes(1));
        states.blocked(m, "no install");
        assertThat(states.state("a").orElseThrow().since()).isEqualTo(clock.now);

        // nexus-f3yqd.2.17 (M3): a retry in flight is working, and the first blocked time survives it and the next block
        Instant firstBlocked = states.state("a").orElseThrow().firstBlockedAt();
        assertThat(firstBlocked).isEqualTo(first);
        clock.now = clock.now.plus(Duration.ofMinutes(1));
        states.working(m);
        var retrying = states.state("a").orElseThrow();
        assertThat(retrying.kind()).isEqualTo(MoveStates.Kind.WORKING);
        assertThat(retrying.reason()).isNull();
        assertThat(retrying.firstBlockedAt()).isEqualTo(firstBlocked);
        clock.now = clock.now.plus(Duration.ofMinutes(1));
        states.blocked(m, "no install");
        assertThat(states.state("a").orElseThrow().firstBlockedAt()).as("still one stretch of blocks").isEqualTo(firstBlocked);
        states.runsComplete(m);
        assertThat(states.state("a").orElseThrow().firstBlockedAt()).isNull();
        states.blocked(m, "again");
        assertThat(states.state("a").orElseThrow().firstBlockedAt()).as("a new stretch").isEqualTo(clock.now);

        // a CALL in flight is the worker's to report and goes with the move
        states.callStarted(m, "a/default", 4242);
        assertThat(states.call("a")).hasValueSatisfying(c -> {
            assertThat(c.runLabel()).isEqualTo("a/default");
            assertThat(c.pid()).isEqualTo(4242);
            assertThat(c.startedAt()).isEqualTo(clock.now);
        });
        states.callEnded(m);
        assertThat(states.call("a")).isEmpty();

        states.working(move("b"));
        states.callStarted(move("b"), "b", 1);
        states.retainOnly(Set.of("b"), Set.of());
        assertThat(states.call("b")).isPresent();
        assertThat(states.state("a")).isEmpty();
        assertThat(states.snapshot()).extracting(MoveStates.State::label).containsExactly("b");

        // a failed move stays until a move supersedes it; a paused or blocked one that is no longer open goes
        states.failed(move("f"), "bad");
        states.blocked(move("g"), "waiting");
        states.retainOnly(Set.of("b"), Set.of());
        assertThat(states.state("f")).isPresent();
        assertThat(states.state("g")).isEmpty();
        states.retainOnly(Set.of("b"), Set.of("f"));
        assertThat(states.state("f")).isEmpty();
    }
}
