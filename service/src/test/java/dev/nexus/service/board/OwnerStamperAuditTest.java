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
    void aPushByAnotherSeatDoesNotHoldTheHandOffOff() {
        String[] f = openRed();
        se.push(f[0], StamperEnv.BASE, "Z", "Y", "sess-carol");   // carol is next up, not the owner

        deadlineAfter(f, 21);

        assertThat(rows(f[0])).as("control: the same push by another seat leaves the hand-off")
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

    @Test
    void aTipWithNoPushRecordIsLoggedWithItsShaAndCountedInTheGroupRead() {
        se.env.ids.clock.set(Instant.now());
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        se.form(t, l, a);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        assertThat(se.env.read(t, "push-train").body()).as("every verdict tip so far has a record").containsEntry("unrecorded_tips", 0);

        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(OwnerStamper.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        try {
            se.done(t, StamperEnv.BASE, "X", "success");
            assertThat(logs.list).as("a recorded tip logs nothing").noneMatch(e -> e.getFormattedMessage().contains("board_done_tip_unrecorded"));
            se.done(t, StamperEnv.BASE, "NOPUSHRECORD", "success");   // pushed by hand, or the script's record write failed
            se.done(t, StamperEnv.BASE, "ALSOMISSING", "failure");
        } finally {
            logger.detachAppender(logs);
        }

        var warned = logs.list.stream().filter(e -> e.getFormattedMessage().contains("board_done_tip_unrecorded")).toList();
        assertThat(warned).hasSize(2);
        assertThat(warned).allSatisfy(e -> assertThat(e.getLevel().toString()).isEqualTo("WARN"));
        assertThat(warned.get(0).getFormattedMessage()).contains("sha=NOPUSHRECORD").contains("unrecorded_tips=1");
        assertThat(warned.get(1).getFormattedMessage()).contains("sha=ALSOMISSING").contains("unrecorded_tips=2");
        Map<String, Object> read = se.env.read(t, "push-train").body();
        assertThat(read).as("S3: the group read carries the running count").containsEntry("unrecorded_tips", 2);
    }
}
