// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252, Sam 2026-11-19 (T2 nexus/sam-ruling-red-notice-alias-2026-11-19): the stamper names the owner and next up
 * by alias beside the identity id, read from the identity's live alias at stamp time, so the red notice can say
 * "halcyon" and not "i12". An identity with no live alias (an ephemeral one) gets no alias field: a reader falls back
 * to the id. Against real PostgreSQL, through the real stamper.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerStamperAliasTest {

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
    void aStampedRowCarriesTheAliasOfTheOwnerAndOfNextUp() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        se.done(t, StamperEnv.BASE, "Y", "failure");

        Map<String, Object> body = se.ownerRows(t, StamperEnv.BASE).getFirst().body();

        assertThat(body).containsEntry("owner", a).containsEntry("owner_alias", "alpha")
            .containsEntry("next", c).containsEntry("next_alias", "carol");
    }

    /**
     * The group rules keep an ephemeral identity off a roster, so the decision never names one; the stamper still reads
     * whatever the decision names, and an identity with no live alias (an ephemeral one, or a seat since purged) must
     * leave the field out. A decision that names an ephemeral owner is forced in, the way the stamper's failure tests
     * force an error.
     */
    @Test
    void anEphemeralOwnerGetsNoAliasFieldAndTheSeatBesideItStillDoes() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String e = se.env.ephemeral(t, "scratchpad");
        String c = se.env.seat(t, "carol");
        se.form(t, l, c);
        String sha = "Y";
        OwnerStamper forced = new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock,
            (in, trigger) -> new OwnerDecision.Decision(new OwnerDecision.Post("owner", sha,
                OwnerDecision.body(e, c, "earliest_suspect", java.util.List.of(sha), 1, se.env.ids.clock.instant(), false, 0)), null));
        se.doneRowOnly(t, StamperEnv.BASE, sha, "failure");

        forced.afterOut(t, "board/ci/" + StamperEnv.BASE + "--done",
            Map.of("from", "github", "kind", "sha", "sha", sha, "outcome", "failure"),
            "{\"state\":\"concluded\",\"sha\":\"" + sha + "\"}");

        Map<String, Object> body = se.ownerRows(t, StamperEnv.BASE).getFirst().body();
        assertThat(body).containsEntry("owner", e).doesNotContainKey("owner_alias")
            .containsEntry("next", c).containsEntry("next_alias", "carol");
    }

    @Test
    void aRowWithNoNextUpHasNoNextAlias() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        se.form(t, l);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-lead");
        se.done(t, StamperEnv.BASE, "X", "failure");

        Map<String, Object> body = se.ownerRows(t, StamperEnv.BASE).getFirst().body();

        assertThat(body).containsEntry("owner", l).containsEntry("owner_alias", "lead")
            .containsEntry("next", null).doesNotContainKey("next_alias");
    }

    @Test
    void aRenamedSeatIsNamedByItsAliasAtStampTime() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        se.form(t, l, a);
        assertThat(se.env.ids.post(t, "/rename", Map.of("old", "alpha", "new", "alfa")).status()).isEqualTo(200);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.done(t, StamperEnv.BASE, "X", "failure");

        assertThat(se.ownerRows(t, StamperEnv.BASE).getFirst().body())
            .containsEntry("owner", a).containsEntry("owner_alias", "alfa");
    }

    /**
     * The alias is a cosmetic field: an alias read that fails must not suppress the owner row. Without the guard the
     * failure leaves evaluate, the hook falls back to the stamper_error row, which reads the aliases again and fails the
     * same way, so a red with a down alias read would post no row at all.
     */
    @Test
    void aFailingAliasReadStillPostsTheOwnerRowWithIdsAndNoAliasFields() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        se.doneRowOnly(t, StamperEnv.BASE, "Y", "failure");
        OwnerStamper broken = new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock,
            OwnerDecision::decide, (ctx, tenant, ids) -> {
                throw new IllegalStateException("alias read is down");
            });

        broken.afterOut(t, "board/ci/" + StamperEnv.BASE + "--done",
            Map.of("from", "github", "kind", "sha", "sha", "Y", "outcome", "failure"),
            "{\"state\":\"concluded\",\"sha\":\"Y\"}");

        var rows = se.ownerRows(t, StamperEnv.BASE);
        assertThat(rows).as("the owner row is posted").hasSize(1);
        assertThat(rows.getFirst().body()).containsEntry("owner", a).containsEntry("next", c)
            .doesNotContainKey("owner_alias").doesNotContainKey("next_alias");
        assertThat(rows.getFirst().body()).as("a real decision, not the stamper_error fallback").containsEntry("reason", "earliest_suspect");
    }
}
