// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 B4b, Sam 2026-11-19 (T2 nexus/sam-ruling-step5-board-text-and-promote-2026-11-19): promote retitles the
 * identity's threads entry to the new alias inside the engine, in the promote's own transaction, so it no longer
 * depends on the caller being the session that owns the entry. Both routes reach it: the tenant credential, and the
 * data credential's self-service promote (RDR-253 amendment, fenced by the caller's identity token). A title already
 * held by another entry is never overwritten: the promote still succeeds, the entry keeps its title, and the answer
 * says so ({@code board_retitle}: {@code title_taken}); the entry stays findable by its {@code identity:} line.
 */
class IdentityPromoteBoardTest extends IdentityFenceHarness {

    private void putEntry(String title, String identityId) {
        scope.withTenant(TENANT, ctx -> ctx.insertInto(MEMORY)
            .set(MEMORY.TENANT_ID, TENANT).set(MEMORY.PROJECT, "threads").set(MEMORY.TITLE, title)
            .set(MEMORY.CONTENT, "session: sess-" + title + "\n"
                + (identityId == null ? "" : "identity: " + identityId + "\n") + "## the work\nstate: running\n")
            .set(MEMORY.TIMESTAMP, OffsetDateTime.now()).execute());
    }

    private List<String> titles() {
        return scope.withTenant(TENANT, ctx -> ctx.select(MEMORY.TITLE).from(MEMORY)
            .where(MEMORY.TENANT_ID.eq(TENANT)).and(MEMORY.PROJECT.eq("threads")).orderBy(MEMORY.TITLE)
            .fetch(MEMORY.TITLE));
    }

    private Resp tenantPromote(Seat e, String name) {
        return post(null, "/v1/identity/" + e.id() + "/promote", Map.of("name", name));
    }

    private Resp dataPromote(Seat e, String name) {
        return call(DATA_TOKEN, e.token(), null, "POST", "/v1/identity/" + e.id() + "/promote", Map.of("name", name));
    }

    @Test
    void theTenantCredentialsPromoteRetitlesTheEntryToTheNewAlias() {
        Seat e = bindEphemeral(301);
        String old = "ephemeral (" + e.id() + ")";
        String name = unique("tenant-promoted");
        putEntry(old, e.id());
        putEntry(unique("someone-else"), null);

        Resp r = tenantPromote(e, name);

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(r.json().get("board_entries_retitled").asInt()).isEqualTo(1);
        assertThat(titles()).contains(name).doesNotContain(old);
    }

    @Test
    void theDataCredentialsSelfServicePromoteRetitlesTheEntryToo() {
        Seat e = bindEphemeral(302);
        String old = "ephemeral (" + e.id() + ")";
        String name = unique("data-promoted");
        putEntry(old, e.id());

        Resp r = dataPromote(e, name);

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(r.json().get("board_entries_retitled").asInt()).isEqualTo(1);
        assertThat(titles()).contains(name).doesNotContain(old);
    }

    @Test
    void aPromoteOfAnIdentityWithNoBoardEntryRetitlesNothingAndStillSucceeds() {
        Seat e = bindEphemeral(303);
        String name = unique("no-entry");

        Resp r = tenantPromote(e, name);

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(r.json().get("board_entries_retitled").asInt()).isZero();
        assertThat(r.text("board_retitle")).isNull();
        assertThat(get(null, "/v1/identity/" + e.id()).text("name")).isEqualTo(name);
    }

    @Test
    void anEntryAlreadyTitledWithTheAliasIsNeverOverwrittenAndThePromoteStillSucceeds() {
        for (boolean viaData : new boolean[] {false, true}) {
            Seat e = bindEphemeral(viaData ? 305 : 304);
            String old = "ephemeral (" + e.id() + ")";
            String name = unique("taken");
            putEntry(old, e.id());
            putEntry(name, null);   // another entry already holds the title the promote would give

            Resp r = viaData ? dataPromote(e, name) : tenantPromote(e, name);

            assertThat(r.status()).as(r.raw()).isEqualTo(200);
            assertThat(r.json().get("board_entries_retitled").asInt()).isZero();
            assertThat(r.text("board_retitle")).isEqualTo("title_taken");
            assertThat(titles()).as("both entries survive, the identity's under its old title").contains(old, name);
            assertThat(get(null, "/v1/identity/" + e.id()).text("kind")).isEqualTo("seat");
        }
    }

    @Test
    void aPromoteTheEngineRefusesLeavesTheEntryAlone() {
        Seat s = bindSeat("already-a-seat", 306);
        String old = "kept " + unique("entry");
        putEntry(old, s.id());

        Resp r = tenantPromote(s, unique("again"));

        assertThat(r.status()).as(r.raw()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("not_ephemeral");
        assertThat(titles()).contains(old);
    }
}
