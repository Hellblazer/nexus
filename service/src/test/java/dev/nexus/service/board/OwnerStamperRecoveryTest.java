// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import dev.nexus.service.db.IdentityRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3 against a real database: a stamper fault posts the leader with {@code reason=stamper_error}, and
 * the timer rebuild after an engine restart arms the deadline of an open red from its latest owner row and no other.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerStamperRecoveryTest {

    StamperEnv se;

    @BeforeAll
    void up() throws Exception {
        se = new StamperEnv();
    }

    @AfterAll
    void down() throws Exception {
        se.close();
    }

    /** A scheduler that records what is scheduled and runs none of it. */
    private static final class Recording extends ScheduledThreadPoolExecutor {
        final List<Long> delaysMs = new ArrayList<>();

        Recording() {
            super(1);
        }

        @Override
        public java.util.concurrent.ScheduledFuture<?> schedule(Runnable command, long delay, TimeUnit unit) {
            delaysMs.add(unit.toMillis(delay));
            return null;
        }
    }

    private String[] redOnY() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        return new String[] {t, l, a, c};
    }

    /** What an engine start does: a new wiring over the same database, its timer rebuilt from the rows. */
    private BoardOwnerWiring restart(String tenant) {
        var wiring = new BoardOwnerWiring(se.env.ids.scope, se.tuples, se.env.ids.repo, se.env.ids.clock);
        wiring.start(new Recording(), () -> List.of(tenant));
        wiring.rebuild(List.of(tenant));
        return wiring;
    }

    @Test
    void aStamperFaultPostsTheLeaderWithReasonStamperError() throws Exception {
        String[] f = redOnY();
        var faulty = new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock,
            (in, trigger) -> { throw new IllegalStateException("boom"); });
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");

        faulty.afterOut(f[0], "board/ci/nexus-develop--done",
            Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}");

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst().sha()).isEqualTo("Y");
        assertThat(rows.getFirst().body()).containsEntry("owner", f[1]).containsEntry("reason", "stamper_error");

        // A green that faults posts nothing: there is no block to name, and no open owner row to clear is known.
        String[] g = redOnY();
        faulty.afterOut(g[0], "board/ci/nexus-develop--done",
            Map.of("kind", "sha", "sha", "Y", "outcome", "success", "from", "github"), "{}");
        assertThat(se.ownerRows(g[0], StamperEnv.BASE)).isEmpty();
    }

    @Test
    void aStamperErrorRowArmsTheDeadlineAndItHandsOnWhenNobodyAcknowledges() throws Exception {
        String[] f = redOnY();
        var faulty = new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock,
            (in, trigger) -> { throw new IllegalStateException("boom"); });
        var timer = new AckDeadlineTimer(new Recording(), se.env.ids.clock, faulty::onDeadline);
        faulty.bind(timer);
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");

        faulty.afterOut(f[0], "board/ci/nexus-develop--done",
            Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}");

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(r -> r.get("reason")).containsExactly("stamper_error");
        Instant episode = Instant.parse((String) rows.getFirst().get("episode_at"));
        assertThat(timer.armedAt(f[0], StamperEnv.BASE)).as("a stamper_error row runs the 20-minute deadline, as every owner row")
            .isEqualTo(episode.plus(Duration.ofMinutes(20)));

        // The deadline passes with no ack: the engine's own stamper (the fault was transient) escalates the red. The
        // row showed no next up, so nobody receives a hand-off: the leader missed it and the escalation follows.
        se.env.ids.clock.advance(Duration.ofMinutes(21));
        for (int i = 1; i < f.length; i++) {
            se.env.renew(f[0], f[i]);
        }
        se.stamper.onDeadline(f[0], StamperEnv.BASE);

        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(r -> r.get("reason"))
            .as("the stamper_error owner's deadline escalates").containsExactly("stamper_error", "escalated");
    }

    /** A stamper whose decision throws on every trigger: a deterministic fault, not a transient one. */
    private OwnerStamper alwaysFaulty() {
        return new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock,
            (in, trigger) -> { throw new IllegalStateException("deterministic boom on " + trigger); });
    }

    private void renewSeats(String[] f) {
        for (int i = 1; i < f.length; i++) {
            se.env.renew(f[0], f[i]);
        }
    }

    @Test
    void aDeterministicFaultOnEveryTriggerStillEscalatesAtTheDeadlineFromTheCurrentRow() throws Exception {
        String[] f = redOnY();
        var faulty = alwaysFaulty();
        var timer = new AckDeadlineTimer(new RecordingScheduler(), se.env.ids.clock, faulty::onDeadline);
        faulty.bind(timer);
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");
        faulty.afterOut(f[0], "board/ci/nexus-develop--done",
            Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}");
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(r -> r.get("reason")).containsExactly("stamper_error");

        Instant episode = Instant.parse((String) se.ownerRows(f[0], StamperEnv.BASE).getFirst().get("episode_at"));
        timer.arm(f[0], StamperEnv.BASE, null);   // cancelled first: postStamperError armed it, so only the fail-safe can arm it again
        assertThat(timer.armedAt(f[0], StamperEnv.BASE)).isNull();

        faulty.onDeadline(f[0], StamperEnv.BASE);   // the timer fires early: nothing is posted, the deadline is armed again
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).hasSize(1);
        assertThat(timer.armedAt(f[0], StamperEnv.BASE)).as("an early firing re-arms at the row's deadline")
            .isEqualTo(episode.plus(Duration.ofMinutes(20)));

        se.env.ids.clock.advance(Duration.ofMinutes(21));
        renewSeats(f);
        faulty.onDeadline(f[0], StamperEnv.BASE);   // the evaluation throws; the fail-safe row still lands

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(r -> r.get("reason")).containsExactly("stamper_error", "escalated");
        assertThat(rows.getLast().body()).containsEntry("owner", f[1]).containsEntry("handoffs", 1);
        assertThat(timer.armedAt(f[0], StamperEnv.BASE)).as("an escalated row runs no deadline").isNull();
        faulty.onDeadline(f[0], StamperEnv.BASE);
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).as("no second escalated row").hasSize(2);
    }

    /** A stamper whose decision throws and whose fail-safe liveness read fails while {@code livenessDown} is set. */
    private OwnerStamper faultyWithLiveness(java.util.concurrent.atomic.AtomicBoolean livenessDown) {
        return faultyWithLiveness(livenessDown, new java.util.concurrent.atomic.AtomicInteger());
    }

    /** As above, counting every liveness read the fail-safe starts. */
    private OwnerStamper faultyWithLiveness(java.util.concurrent.atomic.AtomicBoolean livenessDown,
                                            java.util.concurrent.atomic.AtomicInteger reads) {
        return new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock,
            (in, trigger) -> { throw new IllegalStateException("deterministic boom on " + trigger); },
            IdentityRepository::liveAliases,
            (repo, tenant, pushes, owners, group) -> {
                reads.incrementAndGet();
                if (livenessDown.get()) {
                    throw new IllegalStateException("liveness read down");
                }
                return BoardReads.identities(repo, tenant, pushes, owners, group);
            });
    }

    private void stamperErrorRowOnY(OwnerStamper stamper, String[] f) {
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");
        stamper.afterOut(f[0], "board/ci/nexus-develop--done",
            Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}");
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(r -> r.get("reason")).containsExactly("stamper_error");
    }

    @Test
    void anAcknowledgedRedIsHeldInsideItsStallWindowWithTheLivenessReadDownAndEscalatedAsStalledAfterIt() {
        String[] f = redOnY();
        var down = new java.util.concurrent.atomic.AtomicBoolean(true);
        var reads = new java.util.concurrent.atomic.AtomicInteger();
        var faulty = faultyWithLiveness(down, reads);
        faulty.bind(new AckDeadlineTimer(new RecordingScheduler(), se.env.ids.clock, faulty::onDeadline));
        stamperErrorRowOnY(faulty, f);
        se.ackFrom(f[0], StamperEnv.BASE, "Y", f[1], "sess-lead");   // the row's owner, the leader, acknowledges the row's own tip: stamped from=<owner id>
        // Row times are the database's, so the test clock is set from the ack row's own time, never from the wall clock.
        Instant ackAt = se.tuples.rdp(f[0], "board/ci/nexus-develop--owner", Map.of(), 10, null).stream()
            .filter(r -> "ack".equals(r.dims().get("kind"))).findFirst().orElseThrow().createdAt().toInstant();
        se.env.ids.clock.set(ackAt.plus(Duration.ofMinutes(21)));
        renewSeats(f);

        faulty.onDeadline(f[0], StamperEnv.BASE);   // past the acknowledgement deadline, inside the stall window: nothing to do, no throw

        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(r -> r.get("reason")).containsExactly("stamper_error");
        assertThat(reads.get()).as("the identity read is made only once an escalation is due").isZero();

        se.env.ids.clock.advance(Duration.ofMinutes(45));   // 66 minutes after the ack
        renewSeats(f);
        faulty.onDeadline(f[0], StamperEnv.BASE);

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(r -> r.get("reason")).as("a fault no longer holds an acknowledged red open for ever")
            .containsExactly("stamper_error", "escalated");
        assertThat(rows.getLast().body()).containsEntry("owner", null).containsEntry("stalled", true).containsEntry("stalled_owner", f[1]);
    }

    /** A clock that follows the test clock and, while {@code failing} is set, throws from its second reading on. */
    private static final class FlakyClock extends java.time.Clock {
        final java.time.Clock real;
        final java.util.concurrent.atomic.AtomicBoolean failing = new java.util.concurrent.atomic.AtomicBoolean();
        final java.util.concurrent.atomic.AtomicInteger readings = new java.util.concurrent.atomic.AtomicInteger();

        FlakyClock(java.time.Clock real) {
            this.real = real;
        }

        @Override
        public java.time.ZoneId getZone() {
            return real.getZone();
        }

        @Override
        public java.time.Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            if (failing.get() && readings.incrementAndGet() > 1) {
                throw new IllegalStateException("fail-safe read down");   // the first reading is the evaluation's own
            }
            return real.instant();
        }
    }

    @Test
    void aFailSafeThatItselfThrowsIsRethrownWithTheEvaluationsFaultAndRetriedByTheTimerAtItsDelay() {
        String[] f = redOnY();
        var clock = new FlakyClock(se.env.ids.clock);
        var faulty = new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, clock,
            (in, trigger) -> { throw new IllegalStateException("deterministic boom on " + trigger); });
        var sched = new RecordingScheduler();
        var timer = new AckDeadlineTimer(sched, se.env.ids.clock, faulty::onDeadline);
        faulty.bind(timer);
        stamperErrorRowOnY(faulty, f);
        se.env.ids.clock.advance(Duration.ofMinutes(21));
        renewSeats(f);

        clock.failing.set(true);
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> faulty.onDeadline(f[0], StamperEnv.BASE))
            .as("a fail-safe that cannot run throws, and keeps the evaluation's own fault on what it throws")
            .hasMessageContaining("fail-safe read down")
            .satisfies(t -> assertThat(t.getSuppressed()).anySatisfy(sup -> assertThat(sup).hasMessageContaining("deterministic boom")));

        clock.readings.set(0);
        sched.deadlines().getLast().task().run();   // the timer fires; the evaluation and the fail-safe both throw
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).hasSize(1);
        assertThat(sched.deadlines().getLast().millis()).as("a retry is scheduled after the timer's delay")
            .isEqualTo(AckDeadlineTimer.RETRY.toMillis());
        assertThat(timer.armedAt(f[0], StamperEnv.BASE)).isEqualTo(se.env.ids.clock.instant().plus(AckDeadlineTimer.RETRY));

        clock.failing.set(false);
        renewSeats(f);
        sched.deadlines().getLast().task().run();   // the retry: the fail-safe can run, and escalates
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(r -> r.get("reason")).containsExactly("stamper_error", "escalated");
    }

    @Test
    void aNonOwnersStampedAckDoesNotHoldOffTheFailSafeWhenTheLivenessReadIsDown() {
        String[] f = redOnY();
        var faulty = faultyWithLiveness(new java.util.concurrent.atomic.AtomicBoolean(true));
        faulty.bind(new AckDeadlineTimer(new RecordingScheduler(), se.env.ids.clock, faulty::onDeadline));
        stamperErrorRowOnY(faulty, f);   // owner: the leader
        se.ackFrom(f[0], StamperEnv.BASE, "Y", f[2], "sess-alpha");   // alpha's ack, stamped from=alpha's id: not the owner's
        se.env.ids.clock.advance(Duration.ofMinutes(21));
        renewSeats(f);

        faulty.onDeadline(f[0], StamperEnv.BASE);   // no retry: nobody who owns the red acknowledged it

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(r -> r.get("reason")).containsExactly("stamper_error", "escalated");
        assertThat(rows.getLast().get("owner")).as("the nobody-live row: every member is woken").isNull();
    }

    @Test
    void anAckWithNoSenderSessionDoesNotHoldOffTheFailSafeEvenWithTheOwnersIdInFrom() {
        String[] f = redOnY();
        var faulty = faultyWithLiveness(new java.util.concurrent.atomic.AtomicBoolean(true));
        faulty.bind(new AckDeadlineTimer(new RecordingScheduler(), se.env.ids.clock, faulty::onDeadline));
        stamperErrorRowOnY(faulty, f);
        se.ackFrom(f[0], StamperEnv.BASE, "Y", f[1], null);   // from=<owner id> typed by hand, no session behind it
        se.env.ids.clock.advance(Duration.ofMinutes(21));
        renewSeats(f);

        faulty.onDeadline(f[0], StamperEnv.BASE);

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(r -> r.get("reason")).containsExactly("stamper_error", "escalated");
        assertThat(rows.getLast().get("owner")).isNull();
    }

    @Test
    void anOwnersAckPostedBeforeTheOwnerRowDoesNotHoldOffTheFailSafe() {
        String[] f = redOnY();
        var faulty = faultyWithLiveness(new java.util.concurrent.atomic.AtomicBoolean(true));
        faulty.bind(new AckDeadlineTimer(new RecordingScheduler(), se.env.ids.clock, faulty::onDeadline));
        se.env.ids.clock.advance(Duration.ofSeconds(1));
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");
        se.ackFrom(f[0], StamperEnv.BASE, "Y", f[1], "sess-lead");   // before the owner row exists
        faulty.afterOut(f[0], "board/ci/nexus-develop--done",
            Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}");
        se.env.ids.clock.advance(Duration.ofMinutes(21));
        renewSeats(f);

        faulty.onDeadline(f[0], StamperEnv.BASE);

        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(r -> r.get("reason"))
            .containsExactly("stamper_error", "escalated");
    }

    @Test
    void aLivenessReadFailureOnAnUnackedRedEscalatesAtOnceWithNoOwner() {
        String[] f = redOnY();
        var down = new java.util.concurrent.atomic.AtomicBoolean(true);
        var faulty = faultyWithLiveness(down);
        var timer = new AckDeadlineTimer(new RecordingScheduler(), se.env.ids.clock, faulty::onDeadline);
        faulty.bind(timer);
        stamperErrorRowOnY(faulty, f);
        se.env.ids.clock.advance(Duration.ofMinutes(21));
        renewSeats(f);

        faulty.onDeadline(f[0], StamperEnv.BASE);   // no ack row on the tip: nobody acknowledged, so the read is not needed to know that

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(r -> r.get("reason")).containsExactly("stamper_error", "escalated");
        assertThat(rows.getLast().get("owner")).as("the RDR's nobody-live row: every member is woken").isNull();
        assertThat(rows.getLast().body()).containsEntry("handoffs", 1);
        assertThat(timer.armedAt(f[0], StamperEnv.BASE)).as("an escalated row runs no deadline").isNull();
    }

    @Test
    void theFailSafeReadsLivenessOnlyWhenAnEscalationIsOwed() {
        String[] f = redOnY();
        se.env.ids.clock.set(Instant.now());   // row times come from the database's clock: the test clock must agree with them
        renewSeats(f);
        var down = new java.util.concurrent.atomic.AtomicBoolean(true);
        var reads = new java.util.concurrent.atomic.AtomicInteger();
        var faulty = faultyWithLiveness(down, reads);
        var timer = new AckDeadlineTimer(new RecordingScheduler(), se.env.ids.clock, faulty::onDeadline);
        faulty.bind(timer);

        faulty.onDeadline(f[0], StamperEnv.BASE);   // no owner row at all
        assertThat(reads).as("no open row: nothing owed").hasValue(0);

        stamperErrorRowOnY(faulty, f);
        faulty.onDeadline(f[0], StamperEnv.BASE);   // an open row that is not yet due
        assertThat(reads).as("not yet due: nothing owed, and a dead read is not even tried").hasValue(0);
        assertThat(timer.armedAt(f[0], StamperEnv.BASE)).isNotNull();

        se.env.ids.clock.advance(Duration.ofMinutes(21));
        renewSeats(f);
        faulty.onDeadline(f[0], StamperEnv.BASE);   // due: the escalation is owed
        assertThat(reads).hasValue(1);
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(r -> r.get("reason"))
            .containsExactly("stamper_error", "escalated");

        faulty.onDeadline(f[0], StamperEnv.BASE);   // already escalated: nothing owed again
        assertThat(reads).as("an escalated row owes nothing").hasValue(1);
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).hasSize(2);
    }

    @Test
    void aGroupWithNoLiveMemberStillEscalatesWithOwnerNullWhenTheLivenessReadWorks() {
        String[] f = redOnY();
        var faulty = faultyWithLiveness(new java.util.concurrent.atomic.AtomicBoolean(false));
        faulty.bind(new AckDeadlineTimer(new RecordingScheduler(), se.env.ids.clock, faulty::onDeadline));
        stamperErrorRowOnY(faulty, f);
        se.env.ids.clock.advance(Duration.ofHours(2));   // every seat's lease has lapsed

        faulty.onDeadline(f[0], StamperEnv.BASE);

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(r -> r.get("reason")).containsExactly("stamper_error", "escalated");
        assertThat(rows.getLast().get("owner")).as("the RDR's no-one-live case still names no one").isNull();
    }

    @Test
    void aRestartOverAStamperErrorRowWithAFaultingDecisionStillArmsTheDeadlineFromTheRow() throws Exception {
        String[] f = redOnY();
        var faulty = alwaysFaulty();
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");
        faulty.afterOut(f[0], "board/ci/nexus-develop--done",
            Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}");
        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(r -> r.get("reason")).containsExactly("stamper_error");
        Instant episode = Instant.parse((String) rows.getFirst().get("episode_at"));
        se.env.ids.clock.advance(Duration.ofMinutes(5));

        // What an engine start does with a stamper whose decision still throws: the census completes from the row.
        var wiring = new BoardOwnerWiring(se.tuples, faulty, faulty::rearm, se.env.ids.clock);
        wiring.start(new RecordingScheduler(), () -> List.of(f[0]));
        boolean full = wiring.rebuild(List.of(f[0]));

        assertThat(full).as("the topic was re-armed from its row, so the census is full").isTrue();
        assertThat(wiring.timer().armedAt(f[0], StamperEnv.BASE)).isEqualTo(episode.plus(Duration.ofMinutes(20)));
    }

    @Test
    void aRestartRearmOfATopicWithNoOpenRowAndAFaultingDecisionFails() {
        String[] f = redOnY();
        var faulty = alwaysFaulty();
        faulty.bind(new AckDeadlineTimer(new RecordingScheduler(), se.env.ids.clock, faulty::onDeadline));
        se.done(f[0], StamperEnv.BASE, "Y", "failure");
        se.push(f[0], StamperEnv.BASE, "Z", "Y", "sess-alpha");
        se.done(f[0], StamperEnv.BASE, "Z", "success");   // the clear row: nothing open to arm

        org.assertj.core.api.Assertions.assertThatThrownBy(() -> faulty.rearm(f[0], StamperEnv.BASE))
            .as("nothing to arm from, so the failure is the census's to retry").hasMessageContaining("deterministic boom");
    }

    @Test
    void aRestartRearmsTheDeadlineOfAnOpenRedFromItsEpisode() {
        String[] f = redOnY();
        se.done(f[0], StamperEnv.BASE, "Y", "failure");
        Instant episode = Instant.parse((String) se.ownerRows(f[0], StamperEnv.BASE).getFirst().get("episode_at"));
        se.env.ids.clock.advance(Duration.ofMinutes(5));

        BoardOwnerWiring wiring = restart(f[0]);

        assertThat(wiring.timer().armedAt(f[0], StamperEnv.BASE)).as("the first wake is next up's nudge, at half the deadline")
            .isEqualTo(episode.plus(Duration.ofMinutes(10)));
    }

    @Test
    void aRestartAfterARefreshKeepsTheOriginalDeadline() {
        String[] f = redOnY();
        se.done(f[0], StamperEnv.BASE, "Y", "failure");
        Instant episode = Instant.parse((String) se.ownerRows(f[0], StamperEnv.BASE).getFirst().get("episode_at"));
        se.env.ids.clock.advance(Duration.ofMinutes(4));
        se.push(f[0], StamperEnv.BASE, "Z", "Y", "sess-carol");   // not the owner's push: no implicit ack
        se.done(f[0], StamperEnv.BASE, "Z", "failure");
        assertThat(se.ownerRows(f[0], StamperEnv.BASE).getLast().body()).containsEntry("refresh", true);

        BoardOwnerWiring wiring = restart(f[0]);

        assertThat(wiring.timer().armedAt(f[0], StamperEnv.BASE)).as("still the original episode's nudge time")
            .isEqualTo(episode.plus(Duration.ofMinutes(10)));
    }

    @Test
    void noHandOffDeadlineIsRevivedForAClearedAnAckedOrAnEscalatedRed() {
        String[] cleared = redOnY();
        se.done(cleared[0], StamperEnv.BASE, "Y", "failure");
        se.push(cleared[0], StamperEnv.BASE, "Z", "Y", "sess-alpha");
        se.done(cleared[0], StamperEnv.BASE, "Z", "success");
        assertThat(restart(cleared[0]).timer().armedAt(cleared[0], StamperEnv.BASE)).isNull();

        String[] acked = redOnY();
        se.done(acked[0], StamperEnv.BASE, "Y", "failure");
        se.ack(acked[0], StamperEnv.BASE, "Y", "sess-alpha");
        assertThat(restart(acked[0]).timer().armedAt(acked[0], StamperEnv.BASE))
            .as("an ack ends the hand-off deadline and the nudge; the stall deadline, an hour from the ack, is what is armed")
            .isAfter(Instant.now().plus(Duration.ofMinutes(55))).isBefore(Instant.now().plus(Duration.ofMinutes(61)));

        String[] escalated = redOnY();
        se.done(escalated[0], StamperEnv.BASE, "Y", "failure");
        for (int miss = 0; miss < 2; miss++) {
            se.env.ids.clock.advance(Duration.ofMinutes(21));
            for (int i = 1; i < escalated.length; i++) {
                se.env.renew(escalated[0], escalated[i]);   // the seats keep renewing; only the acknowledgement is missing
            }
            se.stamper.onDeadline(escalated[0], StamperEnv.BASE);
        }
        var rows = se.ownerRows(escalated[0], StamperEnv.BASE);
        assertThat(rows).extracting(r -> r.get("reason")).containsExactly("earliest_suspect", "handoff", "escalated");
        assertThat(restart(escalated[0]).timer().armedAt(escalated[0], StamperEnv.BASE)).isNull();
        se.stamper.onDeadline(escalated[0], StamperEnv.BASE);
        assertThat(se.ownerRows(escalated[0], StamperEnv.BASE)).as("no second escalated row").hasSize(3);
    }
}
