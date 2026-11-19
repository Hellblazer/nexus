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
 * RDR-252 Step 3: a push by a delegated identity (a {@code claude -p} child, made through RDR-253 Step 1's delegated
 * route) is owned by the seat that dispatched it. The stamp maps to the child through the session index, and the
 * child maps to its parent before any liveness or membership is asked.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerDecisionDelegatedTest {

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
    void aPushByADelegatedIdentityIsOwnedByItsParentSeat() {
        String t = se.tenant();
        String leader = se.env.seat(t, "lead");
        String parent = se.env.seat(t, "parent");
        String child = se.env.delegated(t, parent, "child");
        se.form(t, leader, parent);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-child");

        se.done(t, StamperEnv.BASE, "X", "failure");

        var rows = se.ownerRows(t, StamperEnv.BASE);
        assertThat(rows).hasSize(1);
        assertThat(rows.getFirst().body()).containsEntry("owner", parent).containsEntry("reason", "earliest_suspect");
        assertThat(rows.getFirst().body().values()).doesNotContain(child);
    }

    @Test
    void theStampMapsToTheParentThroughTheSessionIndex() {
        String t = se.tenant();
        String parent = se.env.seat(t, "parent");
        String child = se.env.delegated(t, parent, "child");
        BoardRow push = new BoardRow("p1", java.time.Instant.now(), java.util.Map.of("kind", "push", "sha", "X"),
            "{\"base\":\"G0\"}", "sess-child");

        Identities ids = BoardReads.identities(se.env.ids.repo, t, List.of(push), List.of(), null);

        assertThat(ids.identityOf("sess-child")).isEqualTo(parent);
        assertThat(ids.live(parent)).isTrue();
        assertThat(child).isNotEqualTo(parent);
    }
}
