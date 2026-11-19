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
 * ({@code mailbox/<id>}; memberships and work are tables).
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

    /**
     * The row id of the identity's board entry: the newest that carries its line. A quarantined entry is hidden from
     * every read path: it is not "the board entry", so it is not retitled.
     */
    private static Long entryOf(DSLContext ctx, String tenant, String id) {
        return ctx.select(MEMORY.ID).from(MEMORY).where(carriesLine(tenant, id).and(MEMORY.QUARANTINED_AT.isNull()))
            .orderBy(MEMORY.TIMESTAMP.desc(), MEMORY.ID.desc()).limit(1).fetchOne(MEMORY.ID);
    }

    /** What {@link #retitleKeepingTaken} did: how many entries it retitled, and whether another entry held the title. */
    record Retitled(int count, boolean titleTaken) {
    }

    /**
     * {@link #retitle} for a promote (RDR-253 B4b): an entry already titled {@code title} is never overwritten. When
     * another entry holds the title the identity's entry keeps its own, nothing is changed and the answer says
     * {@code titleTaken}, so the promote itself stands: the entry stays findable by its {@code identity:} line.
     *
     * <p>The title is checked first, and the update still runs under a savepoint: a concurrent write can take the title
     * between the two, and a unique violation inside the caller's transaction would abort all of it, the promote
     * included. With the savepoint that case rolls back to it and answers {@code titleTaken} too.
     */
    static Retitled retitleKeepingTaken(DSLContext ctx, String tenant, String id, String title) {
        return retitleKeepingTaken(ctx, tenant, id, title, () -> { });
    }

    /** As above; {@code betweenCheckAndUpdate} runs after the title check and before the update (a test forces the race). */
    static Retitled retitleKeepingTaken(DSLContext ctx, String tenant, String id, String title, Runnable betweenCheckAndUpdate) {
        Long entry = entryOf(ctx, tenant, id);
        if (entry == null) {
            return new Retitled(0, false);
        }
        boolean taken = ctx.fetchExists(ctx.selectOne().from(MEMORY).where(MEMORY.TENANT_ID.eq(tenant))
            .and(MEMORY.PROJECT.eq(PROJECT)).and(MEMORY.TITLE.eq(title)).and(MEMORY.ID.ne(entry)));
        if (taken) {
            return new Retitled(0, true);
        }
        betweenCheckAndUpdate.run();
        java.sql.Savepoint[] before = new java.sql.Savepoint[1];
        ctx.connection(conn -> before[0] = conn.setSavepoint());
        try {
            int changed = setTitle(ctx, tenant, entry, title);
            ctx.connection(conn -> conn.releaseSavepoint(before[0]));
            return new Retitled(changed, false);
        } catch (DataAccessException e) {
            if (!"23505".equals(e.sqlState())) {
                throw e;
            }
            ctx.connection(conn -> conn.rollback(before[0]));
            return new Retitled(0, true);
        }
    }

    private static int setTitle(DSLContext ctx, String tenant, long entry, String title) {
        return ctx.update(MEMORY).set(MEMORY.TITLE, title)
            .where(MEMORY.ID.eq(entry)).and(MEMORY.TENANT_ID.eq(tenant)).execute();
    }

    /**
     * Retitles the identity's entry (the newest, if more than one carries the line) and returns how many it
     * changed. A title already held by another entry is refused with {@code board_title_taken}, which the caller's
     * transaction turns into a rollback.
     */
    static int retitle(DSLContext ctx, String tenant, String id, String title) {
        Long entry = entryOf(ctx, tenant, id);
        if (entry == null) {
            return 0;
        }
        try {
            return setTitle(ctx, tenant, entry, title);
        } catch (DataAccessException e) {
            if ("23505".equals(e.sqlState())) {
                throw new IdentityException(409, "board_title_taken",
                    "the board already has an entry titled '" + title + "'", Map.of("title", title));
            }
            throw e;
        }
    }

    /**
     * Deletes every board entry that carries the identity's line, quarantined ones included. This is an explicit
     * removal, the same as {@code MemoryRepository#delete} (which is a hard delete and does not look at quarantine):
     * the quarantine path exists for a TTL that ran out, a row {@code expire} hides until it is summarized or
     * deleted, and an identity that is being tombstoned or purged must not leave a hidden copy of its entry behind.
     */
    static int deleteEntries(DSLContext ctx, String tenant, String id) {
        return ctx.deleteFrom(MEMORY).where(carriesLine(tenant, id)).execute();
    }

    /** Deletes the identity's mailbox rows; returns the count under {@code mailbox}. */
    static Map<String, Integer> deleteMailbox(DSLContext ctx, String tenant, String id) {
        Map<String, Integer> counts = new LinkedHashMap<>();
        counts.put("mailbox", ctx.deleteFrom(TUPLES)
            .where(TUPLES.TENANT_ID.eq(tenant)).and(TUPLES.SUBSPACE.eq("mailbox/" + id)).execute());
        return counts;
    }
}
