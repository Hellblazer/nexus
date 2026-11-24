// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.db.PciBuilders;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;

/**
 * RDR-229 P3.7 row 23 (nexus-p2m1x.27), linda 2026-11-24: a drop move that has deferred for more than six hours says so, with the age, the
 * count and the builders' pids, and the engine logs a WARN; below the line it is a quiet note. Nothing here ends a build, fails or pages. The
 * clock is fixed and moved by the test; no database is read.
 */
class BuildWaitTest {

    private static final String LABEL = NameKeyDropMove.LABEL;
    private static final Instant T0 = Instant.parse("2026-11-24T00:00:00Z");
    private static final PciBuilders.Building TWO = new PciBuilders.Building(2, List.of(4101, 4102));

    private static BuildWait waitAt(Instant now) {
        BuildWait w = new BuildWait();
        w.clock = Clock.fixed(now, ZoneOffset.UTC);
        return w;
    }

    private static void at(BuildWait w, Instant now) {
        w.clock = Clock.fixed(now, ZoneOffset.UTC);
    }

    private static List<ILoggingEvent> capture(Runnable body) {
        Logger log = (Logger) LoggerFactory.getLogger(BuildWait.class);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        log.addAppender(logs);
        try {
            body.run();
            return List.copyOf(logs.list);
        } finally {
            log.detachAppender(logs);
            logs.stop();
        }
    }

    @Test
    void theFirstDeferralIsAgeZero_aNoteWithNoPids_andOneInfoLine() {
        BuildWait w = waitAt(T0);

        var logged = new MoveFinisher.Result[1];
        List<ILoggingEvent> lines = capture(() -> logged[0] = w.defer(LABEL, TWO, BuildWait.Path.BEFORE_LOCKS));

        MoveFinisher.Result r = logged[0];
        assertThat(r.outcome()).isEqualTo(MoveFinisher.Outcome.BLOCKED);
        assertThat(r.failure()).as("no failure for the handler: nothing counted, nothing paged").isNull();
        assertThat(BuildWait.isDeferral(r)).isTrue();
        assertThat(r.reason()).startsWith(BuildWait.REASON_PREFIX).contains("running=2").contains("deferred_minutes=0")
            .doesNotContain("overdue").doesNotContain("pids=").contains(BuildWait.Path.BEFORE_LOCKS.text);
        assertThat(MoveReasons.classOf(r.reason())).isEqualTo(MoveReasons.WAITING_FOR_BUILDS);
        assertThat(lines).as("one line per deferral").hasSize(1);
        assertThat(lines.get(0).getLevel()).isEqualTo(Level.INFO);
        assertThat(lines.get(0).getFormattedMessage()).contains("event=data_move_completion_deferred").contains("path=precheck");
    }

    @Test
    void atExactlySixHours_itIsNotOverdue_oneMinuteLater_itIs_withTheAgeTheCountAndThePids_andAWarn() {
        BuildWait w = waitAt(T0);
        w.defer(LABEL, TWO, BuildWait.Path.BEFORE_LOCKS);

        at(w, T0.plus(BuildWait.OVERDUE_AFTER));
        var atSix = new MoveFinisher.Result[1];
        List<ILoggingEvent> quiet = capture(() -> atSix[0] = w.defer(LABEL, TWO, BuildWait.Path.BEFORE_LOCKS));
        assertThat(atSix[0].reason()).as("not more than six hours").contains("deferred_minutes=360").doesNotContain("overdue");
        assertThat(MoveReasons.classOf(atSix[0].reason())).isEqualTo(MoveReasons.WAITING_FOR_BUILDS);
        assertThat(quiet).hasSize(1);
        assertThat(quiet.get(0).getLevel()).isEqualTo(Level.INFO);

        at(w, T0.plus(BuildWait.OVERDUE_AFTER).plusSeconds(60));
        var past = new MoveFinisher.Result[1];
        List<ILoggingEvent> loud = capture(() -> past[0] = w.defer(LABEL, TWO, BuildWait.Path.LOCK_WAIT));
        assertThat(past[0].reason()).contains("deferred_minutes=361").contains("; overdue;").contains("running=2").contains("pids=4101,4102");
        assertThat(MoveReasons.classOf(past[0].reason())).isEqualTo(MoveReasons.WAITING_FOR_BUILDS_OVERDUE);
        assertThat(past[0].failure()).as("still nothing counted or paged").isNull();
        assertThat(loud).as("one line per deferral").hasSize(1);
        assertThat(loud.get(0).getLevel()).isEqualTo(Level.WARN);
        assertThat(loud.get(0).getFormattedMessage()).contains("event=data_move_completion_overdue").contains("pids=4101,4102")
            .contains("deferred_minutes=361").contains("path=lock_wait");
    }

    @Test
    void theRunEndsWhenTheFinisherDoesAnythingButDefer_andTheNextDeferralStartsAtZero() {
        BuildWait w = waitAt(T0);
        w.defer(LABEL, TWO, BuildWait.Path.BEFORE_LOCKS);
        at(w, T0.plus(Duration.ofHours(7)));
        assertThat(w.age(LABEL)).isEqualTo(Duration.ofHours(7));

        w.clear(LABEL);

        assertThat(w.age(LABEL)).as("not deferring").isNull();
        assertThat(w.defer(LABEL, TWO, BuildWait.Path.BEFORE_LOCKS).reason()).as("a new run: age zero, not overdue")
            .contains("deferred_minutes=0").doesNotContain("overdue");
    }

    @Test
    void theRunIsPerMove_andOnlyADeferralReadsAsOne() {
        BuildWait w = waitAt(T0);
        w.defer(LABEL, TWO, BuildWait.Path.BEFORE_LOCKS);
        at(w, T0.plus(Duration.ofHours(1)));

        assertThat(w.defer("another", TWO, BuildWait.Path.BEFORE_LOCKS).reason()).contains("deferred_minutes=0");
        assertThat(BuildWait.isDeferral(MoveFinisher.Result.blocked("waiting for a background worker slot"))).isFalse();
        assertThat(BuildWait.isDeferral(MoveFinisher.Result.completed())).isFalse();
        assertThat(BuildWait.isDeferral(MoveFinisher.Result.leftOpen())).isFalse();
    }
}
