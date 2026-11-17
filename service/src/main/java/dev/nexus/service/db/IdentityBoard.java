// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;

import java.util.LinkedHashMap;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;

/**
 * What an identity owns outside its own tables (RDR-253): its entry on the cross-session board (T2 memory, project
 * {@code threads}, found by the {@code identity: <id>} line it carries) and its tuple subspaces
 * ({@code mailbox/<id>} and {@code work/<id>}; memberships are a table).
 *
 * <p>Every method takes the caller's open, tenant-stamped {@link DSLContext}. Identity rows and memory rows are
 * both tenant row-level-security tables stamped from the one {@code nexus.tenant} setting, so a rename's alias
 * rows and its retitle of the board entry go in one transaction, and a failed retitle rolls the alias change back.
 *
 * <p>No client writes the {@code identity:} line before RDR-253 Step 5, so in production these find nothing until
 * then; the tests carry fixture entries that have the line.
 */
final class IdentityBoard {

    static final String PROJECT = "threads";

    private IdentityBoard() {
    }

    /** An entry carries the line {@code identity: <id>} on a line of its own (trailing blanks allowed). */
    private static Condition carriesLine(String tenant, String id) {
        if (!id.matches("i[0-9]+")) {
            throw new IllegalArgumentException("not an identity id: " + id);
        }
        String line = "(^|\n)identity:[ \t]*" + id + "[ \t\r]*(\n|$)";
        return MEMORY.TENANT_ID.eq(tenant).and(MEMORY.PROJECT.eq(PROJECT)).and(MEMORY.CONTENT.likeRegex(line));
    }

    /** Whether the board holds an entry of that title in {@code threads}. */
    static boolean titleTaken(DSLContext ctx, String tenant, String title) {
        return ctx.fetchExists(MEMORY, MEMORY.TENANT_ID.eq(tenant).and(MEMORY.PROJECT.eq(PROJECT))
            .and(MEMORY.TITLE.eq(title)));
    }

    /**
     * Retitles the identity's entry (the newest, if more than one carries the line) and returns how many it
     * changed. A title already held by another entry is refused with {@code board_title_taken}, which the caller's
     * transaction turns into a rollback.
     */
    static int retitle(DSLContext ctx, String tenant, String id, String title) {
        Long entry = ctx.select(MEMORY.ID).from(MEMORY).where(carriesLine(tenant, id))
            .orderBy(MEMORY.TIMESTAMP.desc(), MEMORY.ID.desc()).limit(1).fetchOne(MEMORY.ID);
        if (entry == null) {
            return 0;
        }
        try {
            return ctx.update(MEMORY).set(MEMORY.TITLE, title)
                .where(MEMORY.ID.eq(entry)).and(MEMORY.TENANT_ID.eq(tenant)).execute();
        } catch (DataAccessException e) {
            if ("23505".equals(e.sqlState())) {
                throw new IdentityException(409, "board_title_taken",
                    "the board already has an entry titled '" + title + "'", Map.of("title", title));
            }
            throw e;
        }
    }

    /** Deletes every board entry that carries the identity's line. */
    static int deleteEntries(DSLContext ctx, String tenant, String id) {
        return ctx.deleteFrom(MEMORY).where(carriesLine(tenant, id)).execute();
    }

    /** Deletes the identity's mailbox and work rows; returns the count per family. */
    static Map<String, Integer> deleteTuples(DSLContext ctx, String tenant, String id) {
        Map<String, Integer> counts = new LinkedHashMap<>();
        for (String family : new String[] {"mailbox", "work"}) {
            counts.put(family, ctx.deleteFrom(TUPLES)
                .where(TUPLES.TENANT_ID.eq(tenant)).and(TUPLES.SUBSPACE.eq(family + "/" + id)).execute());
        }
        return counts;
    }
}
