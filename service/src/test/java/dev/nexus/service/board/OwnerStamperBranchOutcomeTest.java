// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;

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

    private List<String> kinds(String tenant) {
        return se.ownerRows(tenant, StamperEnv.BASE).stream().map(StamperEnv.Row::kind).toList();
    }
}
