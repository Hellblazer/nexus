// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3 across {@code /clear}, against real PostgreSQL: a {@code clear} binding gives the identity a new
 * session id and the session index maps both to it, so a push stamped before the clear and an ack stamped after it
 * belong to one identity. The session index, the binding and the stamper are the engine's own; no row is mapped by
 * hand.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerStamperSessionMappingTest {

    StamperEnv se;

    @BeforeAll
    void up() throws Exception {
        se = new StamperEnv();
    }

    @AfterAll
    void down() throws Exception {
        se.close();
    }

    /**
     * Alpha pushes X under its first session, carol pushes Y, the red on Y names alpha; alpha clears into a new
     * session; {@code ackStamp} acknowledges Y; the deadline falls due. Returns the owner-row kinds and reasons.
     */
    private java.util.List<String> afterAnAckStampedBy(String ackStamp) {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        se.done(t, StamperEnv.BASE, "Y", "failure");
        assertThat(se.ownerRows(t, StamperEnv.BASE).getFirst().body()).containsEntry("owner", a);

        int generation = ((Number) se.env.ids.show(t, a).get("generation")).intValue();
        var cleared = se.env.ids.bind(t, a, "clear", generation, Map.of("session_id", "sess-alpha-cleared"));
        assertThat(cleared.status()).as(String.valueOf(cleared.body())).isEqualTo(200);
        se.ack(t, StamperEnv.BASE, "Y", ackStamp);

        se.env.ids.clock.advance(Duration.ofMinutes(21));
        for (String id : new String[] {l, a, c}) {
            se.env.renew(t, id);
        }
        se.stamper.onDeadline(t, StamperEnv.BASE);
        return se.ownerRows(t, StamperEnv.BASE).stream().map(r -> r.kind() + "/" + r.get("reason")).toList();
    }

    @Test
    void aPushBeforeClearAndAnAckAfterItAreOneIdentityAndTheDeadlineHandsOffNothing() {
        assertThat(afterAnAckStampedBy("sess-alpha-cleared"))
            .as("alpha's ack under its new session counts: no hand-off row").containsExactly("owner/earliest_suspect");
    }

    @Test
    void anAckUnderASessionNoIdentityHoldsDoesNotCloseTheRed() {
        assertThat(afterAnAckStampedBy("sess-nobody"))
            .as("control: with the ack unmapped the same deadline hands off, so the test above is not vacuous")
            .containsExactly("owner/earliest_suspect", "owner/handoff");
    }
}
