// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.log.SecretMask;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.Clock;

/**
 * nexus-f3yqd.2.18: every place a text becomes a reason, an event field or a reported state removes the engine's credentials,
 * and {@link FailureHandler#oneLine} does it before it cuts. Pure: no database.
 */
class MoveTextRedactionTest {

    private static final String PW = "Hunter2-s3cret-passw0rd";

    @BeforeEach
    void secret() {
        SecretMask.add(PW);
    }

    @AfterEach
    void forget() {
        SecretMask.reset();
    }

    private static DataMove move(String label) {
        return new DataMove(label, "t", "dm_test", "plain_rows", 10, 0, 1000, "v", null, null, null, null, 0, null, null);
    }

    @Test
    void aSecretStraddlingTheThreeHundredCharacterCut_leavesNoPrefix() {
        // starts 10 characters before the cut: cut first and 10 characters of it would stay
        String text = "x".repeat(290) + PW + " tail";

        String line = FailureHandler.oneLine(text);

        assertThat(line).doesNotContain(PW.substring(0, 10)).doesNotContain("Hunter2").contains("<redacted>");
        assertThat(line).as("cut after the redaction: 290 + the 10-character marker is the 300").isEqualTo("x".repeat(290) + "<redacted>...");

        String longer = "x".repeat(290) + PW + "y".repeat(100);
        assertThat(FailureHandler.oneLine(longer)).doesNotContain("Hunter").endsWith("...");
    }

    @Test
    void aSecretOnTheSecondLine_neverReaches_aReason() {
        assertThat(FailureHandler.oneLine("first\nsecond " + PW)).isEqualTo("first");
        assertThat(FailureHandler.oneLine(PW + " first\nsecond")).isEqualTo("<redacted> first");
    }

    @Test
    void aDispositionAndAVerdict_maskTheirReasonWhoeverBuiltThem() {
        assertThat(FailureHandler.Disposition.blocked("raised: " + PW).reason()).isEqualTo("raised: <redacted>");
        assertThat(FailureHandler.Disposition.failed("raised: " + PW, "22012").reason()).isEqualTo("raised: <redacted>");
        assertThat(FailureHandler.Disposition.blocked("r " + PW, "40001", 3).reason()).isEqualTo("r <redacted>");
        assertThat(DataMoveFailureClassifier.Verdict.permanentFailure("p " + PW).reason()).isEqualTo("p <redacted>");
        assertThat(DataMoveFailureClassifier.Verdict.transientFailure("t " + PW).reason()).isEqualTo("t <redacted>");
    }

    @Test
    void aFailure_isMaskedOnItsDetail_andIsTheSameInstanceWhenItHoldsNone() {
        var clean = new FailureHandler.Failure(null, FailureHandler.Stage.PASS, null, "s", "t", null, 0, null, "clean", null);
        var dirty = new FailureHandler.Failure(null, FailureHandler.Stage.PASS, null, "s", "t", null, 0, null,
            "dsn=" + PW, null);

        assertThat(clean.masked()).isSameAs(clean);
        assertThat(dirty.masked().detail()).isEqualTo("dsn=<redacted>");
        assertThat(dirty.masked().stage()).isEqualTo(FailureHandler.Stage.PASS);
    }

    @Test
    void anEventValue_andAReportedState_areMasked() {
        assertThat(MoveEvents.value("a " + PW + " \"b\"")).isEqualTo("a <redacted> 'b'");
        assertThat(MoveEvents.value(null)).isEqualTo("-");

        var states = new MoveStates(Clock.systemUTC());
        var move = move("m");
        states.blocked(move, "retry 1: " + PW);
        assertThat(states.state("m").orElseThrow().reason()).isEqualTo("retry 1: <redacted>");
        states.failed(move, "failed " + PW);
        assertThat(states.state("m").orElseThrow().reason()).isEqualTo("failed <redacted>");
    }

    @Test
    void aShortSecret_isStillRedacted_inAReason() {
        SecretMask.reset();
        SecretMask.add("pw1");

        assertThat(FailureHandler.oneLine("login failed for pw1 (a PW1 attempt)")).isEqualTo("login failed for <redacted> (a <redacted> attempt)");
    }

    @Test
    void aBlankSecret_changesNothing() {
        SecretMask.reset();
        SecretMask.add("   ");
        SecretMask.add("");
        SecretMask.add(null);

        assertThat(SecretMask.active()).isFalse();
        assertThat(FailureHandler.oneLine("a   b")).isEqualTo("a   b");
    }
}
