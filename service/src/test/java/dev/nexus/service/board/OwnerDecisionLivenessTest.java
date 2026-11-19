// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import dev.nexus.service.db.IdentityRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3: liveness as the decision reads it, on recorded facts, and the read itself ({@code
 * IdentityRepository.liveness}) against a real database: the 300-second rule is applied to the last renewal, and
 * reading writes nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerDecisionLivenessTest {

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
    void aSeatWhoseLastRenewalIsOver300SecondsOldReadsAsNotLiveWithNoLapseRowWritten() {
        String t = se.tenant();
        String id = se.env.seat(t, "idle");
        IdentityRepository repo = se.env.ids.repo;
        assertThat(repo.liveness(t, id)).hasValueSatisfying(l -> {
            assertThat(l.state()).isEqualTo("live");
            assertThat(l.lease()).isEqualTo("leased");
            assertThat(l.kind()).isEqualTo("seat");
            assertThat(l.generation()).isEqualTo(1);
        });
        se.env.ids.clock.advance(Duration.ofSeconds(300));
        assertThat(repo.liveness(t, id).orElseThrow().state()).as("300 seconds is not over 300").isEqualTo("live");
        se.env.ids.clock.advance(Duration.ofSeconds(1));

        assertThat(repo.liveness(t, id).orElseThrow().state()).isEqualTo("lapsed");
        assertThat(se.env.ids.events(t, id)).as("no lapse row was written by the read").doesNotContain("lapse");
        assertThat(repo.liveness(t, id).orElseThrow().state()).as("and none by the next read").isEqualTo("lapsed");
    }

    @Test
    void anUnleasedSeatNeverLapsesAndAnUnknownIdentityHasNoRecord() {
        String t = se.tenant();
        String id = se.env.desktopSeat(t, "desktop");
        se.env.ids.clock.advance(Duration.ofHours(3));

        IdentityRepository.Liveness l = se.env.ids.repo.liveness(t, id).orElseThrow();

        assertThat(l.state()).isEqualTo("live");
        assertThat(l.lease()).isEqualTo("unleased");
        assertThat(se.env.ids.repo.liveness(t, "i-no-such")).isEmpty();
    }

    @Test
    void aLapsedOwnerOnARealRowGivesTheLeaderOwnerNotLive() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        se.form(t, l, a);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.env.ids.clock.advance(Duration.ofSeconds(301));
        se.env.renew(t, l);   // the leader renews; alpha does not

        se.done(t, StamperEnv.BASE, "X", "failure");

        var rows = se.ownerRows(t, StamperEnv.BASE);
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst().body()).containsEntry("owner", l).containsEntry("reason", "owner_not_live");
    }

    @Test
    void anUnleasedOwnerOnARealRowGivesTheLeaderOwnerUnleased() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String d = se.env.desktopSeat(t, "desk");
        se.form(t, l);
        assertThat(se.env.join(t, d, "push-train", "member").status()).isEqualTo(200);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-desk");

        se.done(t, StamperEnv.BASE, "X", "failure");

        assertThat(se.ownerRows(t, StamperEnv.BASE).getFirst().body())
            .containsEntry("owner", l).containsEntry("reason", "owner_unleased");
    }

    @Test
    void aLapsedOwnerGivesOwnerNotLive() {
        OwnerFixture f = new OwnerFixture().seat("A").state("A", "lapsed", "leased");
        f.push("X", "G0", "A").done("X", "failure");

        assertThat(OwnerFixture.bodyOf(f.decide().post())).containsEntry("owner", "L").containsEntry("reason", "owner_not_live");
    }

    @Test
    void anUnleasedOwnerGivesOwnerUnleased() {
        OwnerFixture f = new OwnerFixture().seat("A").state("A", "live", "unleased");
        f.push("X", "G0", "A").done("X", "failure");

        assertThat(OwnerFixture.bodyOf(f.decide().post())).containsEntry("owner", "L").containsEntry("reason", "owner_unleased");
    }

    @Test
    void anIdentityTheReadDoesNotKnowIsNotLive() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.presence.remove("A");
        f.push("X", "G0", "A").done("X", "failure");

        assertThat(OwnerFixture.bodyOf(f.decide().post())).containsEntry("reason", "owner_not_live");
    }
}
