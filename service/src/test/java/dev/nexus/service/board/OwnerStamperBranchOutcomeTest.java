// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The carry-forward of a path-filtered workflow's red, against a real database and the real hook: a --done row whose
 * per-sha outcome is success but whose branch_outcome is failure does not clear an open episode; a branch_outcome of
 * success does.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerStamperBranchOutcomeTest {

    StamperEnv se;

    @BeforeAll
    void up() throws Exception {
        se = new StamperEnv();
    }

    @AfterAll
    void down() throws Exception {
        se.close();
    }

    @Test
    void aCarriedRedKeepsTheEpisodeOpenAndALaterGreenBranchOutcomeClearsIt() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");

        se.doneBranch(t, StamperEnv.BASE, "X", "failure", "failure");   // Service CI red on X
        assertThat(kinds(t)).containsExactly("owner");

        se.doneBranch(t, StamperEnv.BASE, "Y", "success", "failure");   // Y: CI green, Service CI's red carried
        assertThat(kinds(t)).as("no clear while the carried red stands").doesNotContain("clear");
        assertThat(se.ownerRows(t, StamperEnv.BASE).getLast().sha()).isEqualTo("Y");

        se.doneBranch(t, StamperEnv.BASE, "Y", "success", "success");   // Service CI later green
        assertThat(kinds(t)).endsWith("clear");
        assertThat(se.ownerRows(t, StamperEnv.BASE).getLast().sha()).isEqualTo("Y");
    }

    @Test
    void aFailedOrUnverifiableCarryReadDoesNotClearThroughTheHookButAPlainGreenDoes() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        se.doneBody(t, StamperEnv.BASE, "X", "failure", "{\"state\":\"concluded\",\"sha\":\"X\"}");
        assertThat(kinds(t)).containsExactly("owner");

        se.doneBody(t, StamperEnv.BASE, "Y", "success", "{\"state\":\"concluded\",\"sha\":\"Y\",\"carried\":null}");
        assertThat(kinds(t)).as("carried null: the read failed").doesNotContain("clear");

        se.doneBody(t, StamperEnv.BASE, "Y", "success",
            "{\"state\":\"concluded\",\"sha\":\"Y\",\"carried\":[[\"Service CI\",7,\"failure\",\"X\"]]}");
        assertThat(kinds(t)).as("a carried list holding a red").doesNotContain("clear");

        se.doneBody(t, StamperEnv.BASE, "Y", "success",
            "{\"state\":\"concluded\",\"sha\":\"Y\",\"carried\":[[\"Service CI\",7,\"success\",\"X\"]],\"carried_n\":2}");
        assertThat(kinds(t)).as("a trimmed carried list").doesNotContain("clear");

        se.doneBody(t, StamperEnv.BASE, "Y", "success",
            "{\"state\":\"concluded\",\"sha\":\"Y\",\"carried\":[[\"Service CI\",7,\"success\",\"X\"]],\"carried_n\":1}");
        assertThat(kinds(t)).endsWith("clear");
    }

    /** The hook says why a sha row left no verdict: one info line per such row, the reason an operator greps for. */
    @Test
    void aShaRowWithNoCountingVerdictLogsOneLineNamingTheReason() {
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(OwnerStamper.class);
        var appender = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        appender.start();
        logger.addAppender(appender);
        try {
            String t = se.tenant();
            String sub = "board/ci/" + StamperEnv.BASE + "--done";
            String runs = "\"runs\":[[100,1,\"CI\",\"success\"]]";
            java.util.function.BiConsumer<String, String> hook = (outcome, carried) -> se.stamper.afterOut(t, sub,
                new java.util.LinkedHashMap<>(Map.of("from", "github", "kind", "sha", "sha", "Y", "outcome", outcome)),
                "{\"sha\":\"Y\",\"n\":1," + carried + runs + "}");
            hook.accept("success", "\"carried\":null,");
            hook.accept("success", "\"carried\":[[\"Service CI\",7,\"failure\",\"X\"]],");
            hook.accept("cancelled", "");
            se.stamper.afterOut(t, sub, new java.util.LinkedHashMap<>(Map.of("from", "github", "kind", "sha", "sha", "Y")), "{}");
            se.stamper.afterOut(t, sub, new java.util.LinkedHashMap<>(Map.of("from", "github", "kind", "run", "sha", "Y")), "{}");
            hook.accept("success", "\"carried\":[[\"Service CI\",7,\"success\",\"X\"]],");   // a verdict: no line

            List<String> lines = appender.list.stream().map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .filter(m -> m.contains("event=board_done_no_verdict")).toList();
            assertThat(lines).hasSize(4);
            assertThat(lines.get(0)).contains("tenant=" + t, "base=" + StamperEnv.BASE, "sha=Y", "reason=carry_failed");
            assertThat(lines.get(1)).contains("reason=carried_not_green");
            assertThat(lines.get(2)).contains("reason=cancelled");
            assertThat(lines.get(3)).contains("reason=unknown");
        } finally {
            logger.detachAppender(appender);
        }
    }

    private List<String> kinds(String tenant) {
        return se.ownerRows(tenant, StamperEnv.BASE).stream().map(StamperEnv.Row::kind).toList();
    }
}
