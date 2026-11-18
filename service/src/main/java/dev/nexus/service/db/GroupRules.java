// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.records.GroupRecordsRecord;
import dev.nexus.service.jooq.nexus.tables.records.IdentityRecordsRecord;
import org.jooq.DSLContext;

import java.time.OffsetDateTime;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.GROUP_RECORDS;

/**
 * What a group record means to the code around it (RDR-252): whether it is disbanded, whether an identity is live
 * enough to lead it, and the three rules the membership routes apply to a group that has a record. Static helpers
 * over an already-open, tenant-stamped {@link DSLContext}, as {@link IdentityRows} is for identities.
 *
 * <p>The membership rules take the group row {@code FOR SHARE}: a concurrent disband, leader change or re-form of
 * that group (each holds the row {@code FOR UPDATE}) finishes first, and the rule then reads the row it left, so a
 * join cannot slip in behind a disband's cascade and a leader cannot leave while it is being made the leader.
 */
final class GroupRules {

    private GroupRules() {
    }

    /**
     * Disbanded: {@code ended_at} is set, or {@code ends} has passed. The stamper and the load stage read it the
     * same way. The cascade that removes the memberships runs only when {@code ended_at} is set, so a record whose
     * {@code ends} passed keeps its rows until the leader or the override sets {@code ended_at}, or the name is
     * re-formed.
     */
    static boolean disbanded(GroupRecordsRecord rec, OffsetDateTime now) {
        return rec.getEndedAt() != null || (rec.getEnds() != null && !rec.getEnds().isAfter(now));
    }

    /** The group row locked {@code FOR UPDATE}, or null. */
    static GroupRecordsRecord lock(DSLContext ctx, String tenant, String name) {
        return ctx.selectFrom(GROUP_RECORDS)
            .where(GROUP_RECORDS.TENANT_ID.eq(tenant)).and(GROUP_RECORDS.NAME.eq(name)).forUpdate().fetchOne();
    }

    /** The group row without a lock, or null. */
    static GroupRecordsRecord peek(DSLContext ctx, String tenant, String name) {
        return ctx.selectFrom(GROUP_RECORDS)
            .where(GROUP_RECORDS.TENANT_ID.eq(tenant)).and(GROUP_RECORDS.NAME.eq(name)).fetchOne();
    }

    /**
     * The state an identity is in now: the stored state, with a lapse that is due applied (the stored row only
     * says {@code live} until something writes the lapse). Null for an identity that does not exist.
     */
    static String effectiveState(IdentityRecordsRecord rec, OffsetDateTime now) {
        if (rec == null) {
            return null;
        }
        return IdentityRows.lapseDue(rec, now) ? "lapsed" : rec.getState();
    }

    /** A live, leased seat: the only kind of identity that may lead a group. */
    static boolean liveLeasedSeat(IdentityRecordsRecord rec, OffsetDateTime now) {
        return rec != null && "seat".equals(rec.getKind()) && "live".equals(effectiveState(rec, now))
            && IdentityEvidence.LEASED.equals(rec.getLease());
    }

    /** A live, leased holder of any kind: "live" as the re-form rule means it. */
    static boolean liveLeased(IdentityRecordsRecord rec, OffsetDateTime now) {
        return rec != null && "live".equals(effectiveState(rec, now)) && IdentityEvidence.LEASED.equals(rec.getLease());
    }

    /** Rule: a join to a group that has a record needs a seat, and a disbanded group takes no joins. */
    static void onJoin(DSLContext ctx, String tenant, String id, String group, OffsetDateTime now) {
        GroupRecordsRecord rec = ctx.selectFrom(GROUP_RECORDS)
            .where(GROUP_RECORDS.TENANT_ID.eq(tenant)).and(GROUP_RECORDS.NAME.eq(group)).forShare().fetchOne();
        if (rec == null) {
            return;
        }
        if (disbanded(rec, now)) {
            throw new IdentityException(409, "group_disbanded",
                group + " is disbanded: " + (rec.getEndedAt() != null ? "ended_at is set" : "its ends has passed")
                    + ", and a join is refused until the name is re-formed", Map.of("group", group));
        }
        IdentityRecordsRecord ident = IdentityRows.peek(ctx, tenant, id);
        if (ident != null && !"seat".equals(ident.getKind())) {
            throw new IdentityException(403, "seat_required",
                group + " has a group record, and only a seat joins it; " + id + " is " + ident.getKind(),
                Map.of("group", group, "identity", id, "kind", ident.getKind()));
        }
    }

    /** Rule: the current leader's membership is not removed; the leader hands the group on first. */
    static void onLeave(DSLContext ctx, String tenant, String id, String group) {
        GroupRecordsRecord rec = ctx.selectFrom(GROUP_RECORDS)
            .where(GROUP_RECORDS.TENANT_ID.eq(tenant)).and(GROUP_RECORDS.NAME.eq(group)).forShare().fetchOne();
        if (rec != null && rec.getEndedAt() == null && id.equals(rec.getLeader())) {
            throw new IdentityException(409, "leader_cannot_leave",
                id + " leads " + group + ": hand the group on (set a new leader) or disband it first",
                Map.of("group", group, "identity", id));
        }
    }
}
