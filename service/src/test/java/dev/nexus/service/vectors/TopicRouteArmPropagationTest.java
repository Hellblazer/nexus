// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PgVectorRepository.ArmRun;
import dev.nexus.service.vectors.PgVectorRepository.FanoutBudgetSpentException;
import dev.nexus.service.vectors.PgVectorRepository.Limiter;
import org.junit.jupiter.api.Test;

import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-7fku2.1.3 (RDR-134 Step 3): the split {@link TopicRouteArm#propagates} decides, with no database: which failures
 * of a routed attempt keep their existing handling (the request fails, or the collection is isolated) and which degrade
 * the collection to an unscoped search. A {@code propagates} that returned false for everything would pass every
 * integration test that induces an undefined-function error, so each class of failure is pinned here, wrapped in a
 * jOOQ-style cause chain as it arrives.
 */
class TopicRouteArmPropagationTest {

    private static RuntimeException wrapped(Throwable cause) {
        return new RuntimeException("jooq wrapper", new RuntimeException("inner", cause));
    }

    private static ArmRun limitedBy(Limiter limiter) {
        ArmRun run = new ArmRun();
        run.limiter = limiter;
        return run;
    }

    @Test
    void theRequestDeadlineBudgetAndPoolExhaustionAlwaysPropagate_atEveryStage() {
        for (boolean stage : new boolean[] {true, false}) {
            for (Limiter l : Limiter.values()) {
                ArmRun run = limitedBy(l);
                assertThat(TopicRouteArm.propagates(new RequestDeadlineExceededException("spent", 1), run, stage))
                    .as("deadline, stage=%s limiter=%s", stage, l).isTrue();
                assertThat(TopicRouteArm.propagates(new FanoutBudgetSpentException("spent"), run, stage))
                    .as("fan-out budget, stage=%s limiter=%s", stage, l).isTrue();
                assertThat(TopicRouteArm.propagates(wrapped(new SQLTransientConnectionException("pool")), run, stage))
                    .as("pool exhaustion, stage=%s limiter=%s", stage, l).isTrue();
            }
        }
    }

    @Test
    void aWrappedDeadlineOrBudgetStillPropagates() {
        ArmRun run = limitedBy(Limiter.SEARCH);
        assertThat(TopicRouteArm.propagates(wrapped(new RequestDeadlineExceededException("spent", 1)), run, false)).isTrue();
        assertThat(TopicRouteArm.propagates(wrapped(new FanoutBudgetSpentException("spent")), run, true)).isTrue();
    }

    @Test
    void everyTransientSqlStatePropagates() {
        ArmRun run = limitedBy(Limiter.SEARCH);
        for (String state : new String[] {"40001", "40P01", "55P03", "08006", "08001", "53100", "53200", "53300",
                                          "57P01", "57P03"}) {
            assertThat(TopicRouteArm.propagates(wrapped(new SQLException("transient", state)), run, false))
                .as(state).isTrue();
            assertThat(TopicRouteArm.propagates(wrapped(new SQLException("transient", state)), run, true))
                .as(state + " at a routing read").isTrue();
        }
    }

    @Test
    void anythingElseDegrades() {
        ArmRun run = limitedBy(Limiter.SEARCH);
        assertThat(TopicRouteArm.propagates(wrapped(new SQLException("undefined function", "42883")), run, false)).isFalse();
        assertThat(TopicRouteArm.propagates(wrapped(new SQLException("syntax", "42601")), run, true)).isFalse();
        assertThat(TopicRouteArm.propagates(new IllegalStateException("boom"), run, false)).isFalse();
        assertThat(TopicRouteArm.propagates(new IllegalArgumentException("unsupported dim"), run, true)).isFalse();
    }

    @Test
    void aTopicSearchTimeoutDegradesUnderTheSearchBound_andPropagatesUnderTheRequestOrFanoutBound() {
        RuntimeException timeout = wrapped(new SQLException("canceling statement due to statement timeout", "57014"));
        assertThat(TopicRouteArm.propagates(timeout, limitedBy(Limiter.SEARCH), false)).isFalse();
        assertThat(TopicRouteArm.propagates(timeout, limitedBy(Limiter.FANOUT), false)).isTrue();
        assertThat(TopicRouteArm.propagates(timeout, limitedBy(Limiter.REQUEST), false)).isTrue();
    }

    @Test
    void aRoutingReadTimeoutDegradesWhateverTheArmsLimiter_itsBoundIsAShareOfTheArms() {
        RuntimeException timeout = wrapped(new SQLException("canceling statement due to statement timeout", "57014"));
        for (Limiter l : Limiter.values()) {
            assertThat(TopicRouteArm.propagates(timeout, limitedBy(l), true)).as(l.name()).isFalse();
        }
    }
}
