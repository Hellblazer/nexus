// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The RDR-252 post-close audit (2026-11-22), S1 to S3, through the real stamper over PostgreSQL: the owner's push is an
 * implicit ack (a), a disclaim hands the red on at once (b), an acknowledged red that stays open escalates as stalled
 * (c), next up is nudged once at half the deadline (S2), and a tip with no push record is logged and counted (S3).
 * Alpha pushes X and owns the red on Y, which carol pushed; lead leads. The clock is the system's, as the rows'
 * creation times are, so the moves of the test clock are what the deadlines see.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerStamperAuditTest {

    StamperEnv se;

    @BeforeAll
    void up() throws Exception {
        se = new StamperEnv();
    }

    @AfterAll
    void down() throws Exception {
        se.close();
    }

    /** lead, alpha and carol in a group; the red on Y is posted; returns {tenant, lead, alpha, carol}. */
    private String[] openRed() {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        se.done(t, StamperEnv.BASE, "Y", "failure");
        assertThat(se.ownerRows(t, StamperEnv.BASE)).hasSize(1);
        assertThat(se.ownerRows(t, StamperEnv.BASE).getFirst().body()).containsEntry("owner", a);
        return new String[] {t, l, a, c};
    }

    /** Minutes pass, every seat renews so none lapses, and the deadline timer's hand-off runs. */
    private void deadlineAfter(String[] f, long minutes) {
        se.env.ids.clock.advance(Duration.ofMinutes(minutes));
        for (int i = 1; i < f.length; i++) {
            se.env.renew(f[0], f[i]);
        }
        se.stamper.onDeadline(f[0], StamperEnv.BASE);
    }

    private List<String> rows(String t) {
        return se.ownerRows(t, StamperEnv.BASE).stream().map(r -> r.kind() + "/" + r.get("reason")).toList();
    }

    @Test
    void theOwnersPushAfterTheRedTipHoldsTheHandOffOffAtTheDeadline() {
        String[] f = openRed();
        se.push(f[0], StamperEnv.BASE, "Z", "Y", "sess-alpha");   // alpha pushes the fix on top of the red

        deadlineAfter(f, 21);

        assertThat(rows(f[0])).as("S1a: the owner is fixing it, so no hand-off at 20 minutes")
            .containsExactly("owner/earliest_suspect");
    }

    @Test
    void aPushFromASessionNoIdentityHoldsDoesNotHoldTheHandOffOff() {
        String[] f = openRed();
        se.push(f[0], StamperEnv.BASE, "Z", "Y", "sess-nobody");   // a session the index maps to no identity

        deadlineAfter(f, 21);

        assertThat(rows(f[0])).as("control: a push no identity can be named for holds nothing")
            .containsExactly("owner/earliest_suspect", "owner/handoff");
    }

    @Test
    void aDisclaimFromTheOwnerHandsTheRedOnAtOnceAndOneFromAnotherSeatIsIgnored() {
        String[] f = openRed();

        se.disclaim(f[0], StamperEnv.BASE, "Y", "sess-carol");
        assertThat(rows(f[0])).as("carol is not the owner: nothing happens").containsExactly("owner/earliest_suspect");

        se.disclaim(f[0], StamperEnv.BASE, "Y", "sess-alpha");

        assertThat(rows(f[0])).as("S1b: alpha said not mine, with no deadline passing")
            .containsExactly("owner/earliest_suspect", "owner/disclaimed");
        var handed = se.ownerRows(f[0], StamperEnv.BASE).getLast();
        assertThat(handed.body()).containsEntry("owner", f[3]).containsEntry("handoffs", 1).containsEntry("next", null);
    }

    @Test
    void anAcknowledgedRedStillOpenAfterThreeDeadlinesEscalatesAsStalled() {
        String[] f = openRed();
        se.ack(f[0], StamperEnv.BASE, "Y", "sess-alpha");

        deadlineAfter(f, 21);
        assertThat(rows(f[0])).as("the ack ends the hand-off deadline").containsExactly("owner/earliest_suspect");

        deadlineAfter(f, 45);   // 66 minutes after the ack

        assertThat(rows(f[0])).as("S1c: 66 minutes on an acknowledged red").containsExactly("owner/earliest_suspect", "owner/escalated");
        assertThat(se.ownerRows(f[0], StamperEnv.BASE).getLast().body()).containsEntry("stalled", true);
    }

    @Test
    void nextUpIsNudgedOnceAtHalfTheDeadlineByTheTimersWake() {
        String[] f = openRed();

        deadlineAfter(f, 11);
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(StamperEnv.Row::kind).containsExactly("owner", "nudge");
        var nudge = se.ownerRows(f[0], StamperEnv.BASE).getLast();
        assertThat(nudge.sha()).isEqualTo("Y");
        assertThat(nudge.from()).isEqualTo("engine");
        assertThat(nudge.body()).as("the row names next up").containsEntry("next", f[3]).containsEntry("owner", f[2]);

        deadlineAfter(f, 2);
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).as("once per episode").extracting(StamperEnv.Row::kind)
            .containsExactly("owner", "nudge");

        deadlineAfter(f, 10);
        assertThat(rows(f[0])).as("the nudge leaves the deadline as it was")
            .containsExactly("owner/earliest_suspect", "nudge/null", "owner/handoff");
    }

    @Test
    void theGroupViewTheDecisionReadsCarriesTheGroupsStallFactor() {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        Map<String, Object> form = dev.nexus.service.http.GroupTestEnv.body("push-train", l, List.of(StamperEnv.BASE));
        form.put("stall_factor", 4);
        assertThat(se.env.form(t, l, form).status()).isEqualTo(200);

        GroupView view = BoardReads.group(se.env.groups, se.env.ids.repo, t, StamperEnv.BASE);

        assertThat(view.stallFactor()).isEqualTo(4);
        assertThat(view.ackDeadlineMinutes()).isEqualTo(20);
    }

    private static List<ch.qos.logback.classic.spi.ILoggingEvent> unrecordedLines(
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs) {
        return logs.list.stream().filter(e -> e.getFormattedMessage().contains("board_done_tip_unrecorded")).toList();
    }

    @Test
    void aRedTipWithNoPushRecordIsLoggedOnceWithItsShaAndEveryUnrecordedTipIsCountedInTheGroupRead() {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        se.form(t, l, a);
        assertThat(se.env.read(t, "push-train").body()).as("no done row yet: nothing to examine")
            .containsEntry("unrecorded_tips", 0).containsEntry("unattributed_tips", 0);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");

        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(OwnerStamper.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        try {
            se.done(t, StamperEnv.BASE, "X", "success");
            assertThat(se.env.read(t, "push-train").body())
                .as("a done row for a recorded, attributed tip was examined and counts nothing")
                .containsEntry("unrecorded_tips", 0).containsEntry("unattributed_tips", 0);
            assertThat(unrecordedLines(logs)).as("a recorded tip logs nothing").isEmpty();

            se.done(t, StamperEnv.BASE, "NOPUSHRECORD", "success");   // hand push or a failed record write, but it went green
            assertThat(unrecordedLines(logs)).as("a green adds no line").isEmpty();
            se.done(t, StamperEnv.BASE, "ALSOMISSING", "failure");
            se.done(t, StamperEnv.BASE, "ALSOMISSING", "failure");     // a rerun's row
            se.done(t, StamperEnv.BASE, "ALSOMISSING", "failure");
        } finally {
            logger.detachAppender(logs);
        }

        var warned = unrecordedLines(logs);
        assertThat(warned).as("once per sha, on its first row, red only").hasSize(1);
        assertThat(warned.getFirst().getLevel().toString()).isEqualTo("WARN");
        assertThat(warned.getFirst().getFormattedMessage()).contains("sha=ALSOMISSING").contains("unrecorded_tips=2");
        assertThat(se.env.read(t, "push-train").body()).as("S3: the group read counts both unrecorded tips, green and red")
            .containsEntry("unrecorded_tips", 2);
    }

    @Test
    void aTipWhosePushRecordNamesNoPusherIsCountedAsUnattributed() {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        se.form(t, l, a);
        se.push(t, StamperEnv.BASE, "OK", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "NOSESSION", "OK", null);               // the script's minimal fallback record
        se.push(t, StamperEnv.BASE, "UNMAPPED", "NOSESSION", "sess-nobody");   // a session the index does not map
        for (String sha : List.of("OK", "NOSESSION", "UNMAPPED")) {
            se.done(t, StamperEnv.BASE, sha, "success");
        }

        assertThat(se.env.read(t, "push-train").body()).containsEntry("unrecorded_tips", 0).containsEntry("unattributed_tips", 2);
    }

    @Test
    void aDisclaimFromAnIdentityThatIsNotTheOwnerIsDroppedBeforeTheTopicLockIsTaken() {
        String[] f = openRed();
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(OwnerStamper.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        try {
            se.disclaim(f[0], StamperEnv.BASE, "Y", "sess-carol");
            se.disclaim(f[0], StamperEnv.BASE, "Y", "sess-alpha");
        } finally {
            logger.detachAppender(logs);
        }

        var lines = logs.list.stream().map(e -> e.getFormattedMessage()).filter(m -> m.contains("event=board_owner_disclaim")).toList();
        assertThat(lines).hasSize(2);
        assertThat(lines.get(0)).as("carol is not the owner: dropped without an evaluation").contains("outcome=dropped_not_owner");
        assertThat(lines.get(1)).as("alpha is: evaluated").contains("outcome=evaluated");
    }

    @Test
    void aStrangersLaterDisclaimDoesNotMaskTheOwnersEarlierOne() {
        String[] f = openRed();
        se.disclaimRowOnly(f[0], StamperEnv.BASE, "Y", "sess-alpha");   // the owner's disclaim, whose evaluation did not run
        se.disclaim(f[0], StamperEnv.BASE, "Y", "sess-carol");           // a stranger's later one is what the hook sees

        assertThat(rows(f[0])).as("the owner's disclaim is on the topic: the red is handed on")
            .containsExactly("owner/earliest_suspect", "owner/disclaimed");
    }

    @Test
    void anAckedRedWhoseEvaluationKeepsFailingIsStillEscalatedOnceItsStallWindowIsPastAndNotBefore() {
        String[] f = openRed();
        se.ackFrom(f[0], StamperEnv.BASE, "Y", f[2], "sess-alpha");   // as the route stores it: from is the owner's id
        var failing = new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock,
            (in, trigger) -> { throw new IllegalStateException("evaluation fault"); });

        deadlineWith(failing, f, 30);   // past the acknowledgement deadline, inside the stall window
        assertThat(rows(f[0])).as("the ack holds the escalation off inside the stall window").containsExactly("owner/earliest_suspect");

        deadlineWith(failing, f, 35);   // 65 minutes after the ack
        assertThat(rows(f[0])).as("a fault no longer leaves an acknowledged red open for ever")
            .containsExactly("owner/earliest_suspect", "owner/escalated");
        assertThat(se.ownerRows(f[0], StamperEnv.BASE).getLast().body()).containsEntry("stalled", true).containsEntry("stalled_owner", f[2]);
    }

    private void deadlineWith(OwnerStamper stamper, String[] f, long minutes) {
        se.env.ids.clock.advance(Duration.ofMinutes(minutes));
        for (int i = 1; i < f.length; i++) {
            se.env.renew(f[0], f[i]);
        }
        stamper.onDeadline(f[0], StamperEnv.BASE);
    }
}
