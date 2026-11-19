// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.http.IdentityTestEnv;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_ALIASES;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Two engine-side guards of RDR-252 and RDR-253 B4b that the route tests cannot reach: {@code liveAliases} reads the
 * live alias and never a retired one, and a retitle that loses the race for its title to a concurrent insert answers
 * {@code title_taken} and leaves the surrounding transaction (the promote) alive, where it used to roll it back.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityAliasReadAndRetitleRaceTest {

    IdentityTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = IdentityTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    @Test
    void liveAliasesNamesTheLiveAliasAfterARenameAndNeverARetiredOne() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "alpha");
        assertThat(env.post(t, "/rename", Map.of("old", "alpha", "new", "alfa")).status()).isEqualTo(200);

        assertThat(env.repo.liveAliases(t, List.of(id))).containsExactly(Map.entry(id, "alfa"));
    }

    /**
     * No ordering of the rows can make the filter pass by luck here: the seat's only alias is retired, so a query that
     * does not filter on {@code state = 'live'} returns it and this fails.
     */
    @Test
    void anIdentityWhoseOnlyAliasIsRetiredHasNoLiveAlias() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "solo");
        env.scope.withTenant(t, ctx -> ctx.update(IDENTITY_ALIASES).set(IDENTITY_ALIASES.STATE, "retired")
            .set(IDENTITY_ALIASES.RETIRED_AT, OffsetDateTime.now())
            .where(IDENTITY_ALIASES.TENANT_ID.eq(t)).and(IDENTITY_ALIASES.NAME.eq("solo")).execute());

        assertThat(env.repo.liveAliases(t, List.of(id))).isEmpty();
    }

    @Test
    void aTitleInsertedBetweenTheCheckAndTheUpdateIsTitleTakenAndTheTransactionStillCommits() {
        String t = IdentityTestEnv.freshTenant();
        env.putThreadsEntry(t, "ephemeral (i77)", "i77");

        env.scope.withTenant(t, ctx -> {
            IdentityBoard.Retitled r = IdentityBoard.retitleKeepingTaken(ctx, t, "i77", "taken-late",
                () -> env.putThreadsEntry(t, "taken-late", null));   // another session commits the title just now
            assertThat(r.titleTaken()).isTrue();
            assertThat(r.count()).isZero();
            // the promote's transaction goes on: a later statement in it still runs and its write commits
            ctx.insertInto(dev.nexus.service.jooq.nexus.Tables.MEMORY)
                .set(dev.nexus.service.jooq.nexus.Tables.MEMORY.TENANT_ID, t)
                .set(dev.nexus.service.jooq.nexus.Tables.MEMORY.PROJECT, "threads")
                .set(dev.nexus.service.jooq.nexus.Tables.MEMORY.TITLE, "written after the lost race")
                .set(dev.nexus.service.jooq.nexus.Tables.MEMORY.CONTENT, "x\n")
                .set(dev.nexus.service.jooq.nexus.Tables.MEMORY.TIMESTAMP, OffsetDateTime.now()).execute();
            return null;
        });

        assertThat(env.threadsTitles(t)).containsExactly("ephemeral (i77)", "taken-late", "written after the lost race");
    }
}
