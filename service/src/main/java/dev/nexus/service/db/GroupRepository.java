// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.records.GroupRecordsRecord;
import dev.nexus.service.jooq.nexus.tables.records.IdentityRecordsRecord;
import org.jooq.DSLContext;

import java.time.Clock;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.GROUP_RECORDS;

/**
 * RDR-252 Step 1: the group record and the operations that change it.
 *
 * <p>One permanent row per (tenant, name) in {@code nexus.group_records}. The first write is an insert that fails
 * when a live record has the name and, in the same transaction, writes the leader's membership; an update changes
 * the one row in place under a row lock; a disband sets {@code ended_at} once and removes every membership in the
 * same transaction; a disbanded name is re-formed in place. The level-1 check (the writer is the leader, at its
 * current generation) is the handler's, in its own statement before the write, as every handler-layer check of
 * the engine is: here each write names the leader it was checked against and refuses a row whose leader has changed
 * since ({@code 409 leader_changed}).
 *
 * <p><b>Lock order</b>, for every write: the group record row, then the per-topic advisory locks in sorted order
 * ({@link BoardTopicLock}), then the identity row of a leader. The membership rules
 * ({@link GroupRules#onJoin}, {@link GroupRules#onLeave}) take the group row first for the same reason.
 *
 * <p>The decisions the RDR left open, as built:
 * <ul>
 *   <li>A record is disbanded once {@code ended_at} is set or {@code ends} has passed (a join is then refused, and
 *       a re-form may take the name). The disband cascade runs only when {@code ended_at} is set.</li>
 *   <li>{@code ends} must be in the future on a form and a {@code set}; a leader extends a lapsed group by setting
 *       it, which takes the topic locks again, since the group serves its topics once more.</li>
 *   <li>A re-form is checked as a first write: the writer is the leader it names. The former leader may re-form
 *       while it is live and leased; when it is not, any live, leased seat may.</li>
 *   <li>The audit trail ({@code gc_audit}, written in the transaction of the change): {@code group_disband} at a
 *       disband, {@code group_reform} at a re-form with the old record's fields (including its {@code ended_at}
 *       and {@code ends}, which is how an expired record's history reads), {@code group_override} at an override.</li>
 *   <li>{@code ack_deadline_by_topic} given on a {@code set} replaces the whole map.</li>
 * </ul>
 */
public final class GroupRepository {

    private static final Set<String> SET_FIELDS =
        Set.of("leader", "ends", "ended_at", "ack_deadline", "ack_deadline_by_topic", "serves");

    private final TenantScope scope;
    private final Clock clock;
    private final IdentityMemberships memberships;

    public GroupRepository(TenantScope scope, Clock clock) {
        this.scope = scope;
        this.clock = clock;
        this.memberships = new IdentityMemberships(scope, clock);
    }

    // ── reads ────────────────────────────────────────────────────────────────

    /** The group record as the routes return it; 404 {@code unknown_group} when there is none. */
    public Map<String, Object> read(String tenant, String name) {
        IdentityMemberships.requireGroup(name);
        return scope.withTenant(tenant, ctx -> {
            GroupRecordsRecord rec = GroupRules.peek(ctx, tenant, name);
            if (rec == null) {
                throw unknown(name);
            }
            return view(rec, OffsetDateTime.now(clock));
        });
    }

    /** The stored leader of {@code name}, whatever its state; 404 when there is no record. */
    public String leaderOf(String tenant, String name) {
        IdentityMemberships.requireGroup(name);
        return scope.withTenant(tenant, ctx -> {
            GroupRecordsRecord rec = GroupRules.peek(ctx, tenant, name);
            if (rec == null) {
                throw unknown(name);
            }
            return rec.getLeader();
        });
    }

    /**
     * The live group that serves the board topic family {@code baseTopic} (the topic before {@code --}), for the
     * owner stamper: empty when no live group serves it. At most one does, by the topic lock every write takes.
     */
    public Optional<Map<String, Object>> servingGroup(String tenant, String baseTopic) {
        return scope.withTenant(tenant, ctx -> {
            OffsetDateTime now = OffsetDateTime.now(clock);
            return ctx.selectFrom(GROUP_RECORDS).where(GROUP_RECORDS.TENANT_ID.eq(tenant))
                .and(GROUP_RECORDS.ENDED_AT.isNull()).orderBy(GROUP_RECORDS.NAME).fetch().stream()
                .filter(r -> !GroupRules.disbanded(r, now) && Arrays.asList(r.getServes()).contains(baseTopic))
                .findFirst().map(r -> view(r, now));
        });
    }

    // ── form and re-form ─────────────────────────────────────────────────────

    /**
     * Forms the group {@code body.name}, or re-forms it when a disbanded record has the name. The caller has checked
     * that the writer is {@code body.leader}. {@code actor} names the writer for the audit row.
     */
    public Map<String, Object> form(String tenant, String actor, Map<String, Object> body) {
        String name = GroupInput.text(body, "name");
        IdentityMemberships.requireGroup(name);
        String leader = GroupInput.text(body, "leader");
        IdentityRepository.requireId(leader);
        String purpose = GroupInput.text(body, "purpose");
        String requestedBy = GroupInput.text(body, "requested_by");
        OffsetDateTime ends = GroupInput.instant(body.get("ends"), "ends");
        int ackDeadline = body.get("ack_deadline") == null ? GroupInput.DEFAULT_DEADLINE
            : GroupInput.deadline(body.get("ack_deadline"), "ack_deadline");
        List<String> serves = body.get("serves") == null ? List.of() : GroupInput.serves(body.get("serves"));
        Map<String, Integer> byTopic = body.get("ack_deadline_by_topic") == null ? Map.of()
            : GroupInput.byTopic(body.get("ack_deadline_by_topic"));
        requireKeysServed(byTopic, serves);

        return scope.withTenant(tenant, ctx -> {
            OffsetDateTime now = OffsetDateTime.now(clock);
            if (ends != null && !ends.isAfter(now)) {
                throw new IdentityException(400, "bad_ends", "ends must be in the future");
            }
            var json = IdentityRows.json(new LinkedHashMap<String, Object>(byTopic));
            int inserted = ctx.insertInto(GROUP_RECORDS)
                .set(GROUP_RECORDS.TENANT_ID, tenant).set(GROUP_RECORDS.NAME, name).set(GROUP_RECORDS.LEADER, leader)
                .set(GROUP_RECORDS.PURPOSE, purpose).set(GROUP_RECORDS.REQUESTED_BY, requestedBy)
                .set(GROUP_RECORDS.FORMED_AT, now).set(GROUP_RECORDS.ENDS, ends)
                .set(GROUP_RECORDS.ACK_DEADLINE, ackDeadline).set(GROUP_RECORDS.ACK_DEADLINE_BY_TOPIC, json)
                .set(GROUP_RECORDS.SERVES, serves.toArray(new String[0]))
                .onConflict(GROUP_RECORDS.TENANT_ID, GROUP_RECORDS.NAME).doNothing().execute();
            Map<String, Object> reformed = null;
            if (inserted == 0) {
                // A record has the name. Lock it (a concurrent re-form finishes first and we read what it left).
                GroupRecordsRecord old = GroupRules.lock(ctx, tenant, name);
                if (old == null || !GroupRules.disbanded(old, now)) {
                    throw new IdentityException(409, "group_exists", name + " has a live record: update it, or disband it first",
                        Map.of("group", name));
                }
                IdentityRecordsRecord former = IdentityRows.peek(ctx, tenant, old.getLeader());
                if (GroupRules.liveLeased(former, now) && !leader.equals(old.getLeader())) {
                    throw new IdentityException(403, "reform_needs_former_leader",
                        "the former leader " + old.getLeader() + " of " + name + " is live: only it may re-form the name",
                        Map.of("group", name, "former_leader", old.getLeader()));
                }
                reformed = fields(old);
                old.setLeader(leader);
                old.setPurpose(purpose);
                old.setRequestedBy(requestedBy);
                old.setFormedAt(now);
                old.setEnds(ends);
                old.setEndedAt(null);
                old.setAckDeadline(ackDeadline);
                old.setAckDeadlineByTopic(json);
                old.setServes(serves.toArray(new String[0]));
                old.update();
                // A re-form starts with the leader as the only member.
                memberships.removeAll(ctx, tenant, name);
            }
            BoardTopicLock.lockAll(ctx, tenant, serves);
            refuseSecondServer(ctx, tenant, name, serves, now);
            requireLiveLeasedSeat(ctx, tenant, leader, now);
            memberships.join(ctx, tenant, leader, name, "leader", now);
            GroupRecordsRecord rec = GroupRules.peek(ctx, tenant, name);
            if (reformed != null) {
                Map<String, Object> d = new LinkedHashMap<>();
                d.put("group", name);
                d.put("old", reformed);
                d.put("new", fields(rec));
                audit(ctx, tenant, "group_reform", actor, d);
            }
            return view(rec, now);
        });
    }

    // ── update ───────────────────────────────────────────────────────────────

    /**
     * Changes the record in place: {@code leader}, {@code ends}, {@code ack_deadline}, {@code ack_deadline_by_topic}
     * (the whole map), {@code serves}; or {@code ended_at} alone, which disbands. {@code expectedLeader} is the
     * leader the caller's level-1 check named.
     */
    public Map<String, Object> update(String tenant, String name, String expectedLeader, String actor,
                                      Map<String, Object> body) {
        IdentityMemberships.requireGroup(name);
        if (SET_FIELDS.stream().noneMatch(body::containsKey)) {
            throw new IdentityException(400, "nothing_to_set", "set takes one or more of " + new java.util.TreeSet<>(SET_FIELDS));
        }
        if (body.containsKey("ended_at")) {
            if (SET_FIELDS.stream().anyMatch(f -> !f.equals("ended_at") && body.containsKey(f))) {
                throw new IdentityException(400, "ended_at_alone", "ended_at disbands the group and is set on its own");
            }
            return scope.withTenant(tenant, ctx -> {
                OffsetDateTime now = OffsetDateTime.now(clock);
                OffsetDateTime at = endedAt(body.get("ended_at"), now);
                GroupRecordsRecord rec = lockLive(ctx, tenant, name, expectedLeader);
                return endGroup(ctx, tenant, rec, at, "group_disband", actor, Map.of(), now);
            });
        }
        String newLeader = GroupInput.text(body, "leader");
        if (body.containsKey("leader")) {
            IdentityRepository.requireId(newLeader);
        }
        boolean setEnds = body.containsKey("ends");
        OffsetDateTime ends = setEnds ? GroupInput.instant(body.get("ends"), "ends") : null;
        Integer ack = body.containsKey("ack_deadline") ? GroupInput.deadline(body.get("ack_deadline"), "ack_deadline") : null;
        List<String> serves = body.containsKey("serves") ? GroupInput.serves(body.get("serves")) : null;
        Map<String, Integer> byTopic = body.containsKey("ack_deadline_by_topic")
            ? GroupInput.byTopic(body.get("ack_deadline_by_topic")) : null;

        return scope.withTenant(tenant, ctx -> {
            OffsetDateTime now = OffsetDateTime.now(clock);
            if (setEnds && ends != null && !ends.isAfter(now)) {
                throw new IdentityException(400, "bad_ends", "ends must be in the future; disband the group to end it now");
            }
            GroupRecordsRecord rec = lockLive(ctx, tenant, name, expectedLeader);
            Map<String, Object> before = fields(rec);
            List<String> newServes = serves != null ? serves : Arrays.asList(rec.getServes());
            Map<String, Integer> newByTopic = byTopic != null ? byTopic : currentByTopic(rec, newServes);
            requireKeysServed(newByTopic, newServes);
            if (serves != null || setEnds) {
                BoardTopicLock.lockAll(ctx, tenant, newServes);
                refuseSecondServer(ctx, tenant, name, newServes, now);
            }
            boolean handOver = newLeader != null && !newLeader.equals(rec.getLeader());
            if (handOver) {
                requireLiveLeasedSeat(ctx, tenant, newLeader, now);
                requireMember(ctx, tenant, newLeader, name);
                rec.setLeader(newLeader);
            }
            if (setEnds) {
                rec.setEnds(ends);
            }
            if (ack != null) {
                rec.setAckDeadline(ack);
            }
            rec.setServes(newServes.toArray(new String[0]));
            rec.setAckDeadlineByTopic(IdentityRows.json(new LinkedHashMap<String, Object>(newByTopic)));
            rec.update();
            if (handOver) {
                // A hand-over is the one in-place change whose history matters to the next reader of a red.
                Map<String, Object> d = new LinkedHashMap<>();
                d.put("group", name);
                d.put("old", before);
                d.put("new", fields(rec));
                audit(ctx, tenant, "group_set", actor, d);
            }
            return view(rec, now);
        });
    }

    /** Disbands the group: {@code ended_at} is set once and every membership goes, in this transaction. */
    public Map<String, Object> disband(String tenant, String name, String expectedLeader, String actor) {
        IdentityMemberships.requireGroup(name);
        return scope.withTenant(tenant, ctx -> {
            OffsetDateTime now = OffsetDateTime.now(clock);
            GroupRecordsRecord rec = lockLive(ctx, tenant, name, expectedLeader);
            return endGroup(ctx, tenant, rec, now, "group_disband", actor, Map.of(), now);
        });
    }

    // ── operator override ────────────────────────────────────────────────────

    /**
     * The operator override (the tenant credential, no identity token): sets a new leader or {@code ended_at}, only
     * while the stored leader's identity cannot write the record: {@code ended}, {@code unheld}, {@code lapsed} with a
     * recorded {@code gone}, {@code live} with lease {@code unleased}, or gone from the table. Written to
     * {@code gc_audit} as {@code group_override}, in the same transaction.
     */
    public Map<String, Object> override(String tenant, String name, String actor, Map<String, Object> body) {
        IdentityMemberships.requireGroup(name);
        boolean setLeader = body.containsKey("leader");
        boolean setEnded = body.containsKey("ended_at");
        if (setLeader == setEnded) {
            throw new IdentityException(400, "bad_override", "the override sets a new leader or ended_at, one of the two");
        }
        String newLeader = GroupInput.text(body, "leader");
        if (setLeader) {
            IdentityRepository.requireId(newLeader);
        }
        return scope.withTenant(tenant, ctx -> {
            OffsetDateTime now = OffsetDateTime.now(clock);
            OffsetDateTime at = setEnded ? endedAt(body.get("ended_at"), now) : null;
            GroupRecordsRecord rec = GroupRules.lock(ctx, tenant, name);
            if (rec == null) {
                throw unknown(name);
            }
            if (rec.getEndedAt() != null) {
                throw new IdentityException(409, "group_disbanded", name + " is disbanded: re-form it", Map.of("group", name));
            }
            IdentityRecordsRecord holder = IdentityRows.peek(ctx, tenant, rec.getLeader());
            String state = holder == null ? "gone" : GroupRules.effectiveState(holder, now);
            boolean allowed = holder == null || "ended".equals(state) || "unheld".equals(state)
                || ("lapsed".equals(state) && holder.getGoneAt() != null)
                || ("live".equals(state) && IdentityEvidence.UNLEASED.equals(holder.getLease()));
            if (!allowed) {
                throw new IdentityException(409, "leader_not_overridable",
                    "the leader " + rec.getLeader() + " is " + state + (holder.getLease() == null ? "" : " (" + holder.getLease() + ")")
                        + ": the override is for a leader that cannot write the record (ended, unheld, lapsed with its "
                        + "holder gone, or live and unleased)",
                    Map.of("group", name, "leader", rec.getLeader(), "state", String.valueOf(state)));
            }
            Map<String, Object> extra = new LinkedHashMap<>();
            extra.put("leader_state", state);
            if (setEnded) {
                return endGroup(ctx, tenant, rec, at, "group_override", actor, extra, now);
            }
            Map<String, Object> before = fields(rec);
            requireLiveLeasedSeat(ctx, tenant, newLeader, now);
            requireMember(ctx, tenant, newLeader, name);
            rec.setLeader(newLeader);
            rec.update();
            Map<String, Object> d = new LinkedHashMap<>(extra);
            d.put("group", name);
            d.put("old", before);
            d.put("new", fields(rec));
            audit(ctx, tenant, "group_override", actor, d);
            return view(rec, now);
        });
    }

    // ── shared steps ─────────────────────────────────────────────────────────

    /** The row locked, from the leader the check named, and not disbanded by {@code ended_at}. */
    private static GroupRecordsRecord lockLive(DSLContext ctx, String tenant, String name, String expectedLeader) {
        GroupRecordsRecord rec = GroupRules.lock(ctx, tenant, name);
        if (rec == null) {
            throw unknown(name);
        }
        if (!rec.getLeader().equals(expectedLeader)) {
            throw new IdentityException(409, "leader_changed",
                name + " is led by " + rec.getLeader() + " now, not " + expectedLeader + ": read it and try again",
                Map.of("group", name, "leader", rec.getLeader()));
        }
        if (rec.getEndedAt() != null) {
            throw new IdentityException(409, "group_disbanded", name + " is disbanded: only a re-form changes it",
                Map.of("group", name));
        }
        return rec;
    }

    private Map<String, Object> endGroup(DSLContext ctx, String tenant, GroupRecordsRecord rec, OffsetDateTime at,
                                         String operation, String actor, Map<String, Object> extra, OffsetDateTime now) {
        Map<String, Object> before = fields(rec);
        rec.setEndedAt(at);
        rec.update();
        int removed = memberships.removeAll(ctx, tenant, rec.getName());
        Map<String, Object> d = new LinkedHashMap<>(extra);
        d.put("group", rec.getName());
        d.put("old", before);
        d.put("ended_at", IdentityRows.str(at));
        d.put("memberships_removed", removed);
        audit(ctx, tenant, operation, actor, d);
        return view(rec, now);
    }

    /** ended_at is {@code true} (now) or a UTC instant that is not in the future. */
    private static OffsetDateTime endedAt(Object v, OffsetDateTime now) {
        if (Boolean.TRUE.equals(v)) {
            return now;
        }
        OffsetDateTime at = v instanceof String ? GroupInput.instant(v, "ended_at") : null;
        if (at == null) {
            throw new IdentityException(400, "bad_ended_at", "'ended_at' is true (now) or a UTC instant like 2026-12-01T00:00:00Z");
        }
        if (at.isAfter(now)) {
            throw new IdentityException(400, "bad_ended_at", "'ended_at' is not in the future; set ends to end a group later");
        }
        return at;
    }

    /** A first or new leader is a live, leased seat. Locks its identity row. */
    private static void requireLiveLeasedSeat(DSLContext ctx, String tenant, String id, OffsetDateTime now) {
        IdentityRecordsRecord rec = IdentityRows.lock(ctx, tenant, id);
        if (!GroupRules.liveLeasedSeat(rec, now)) {
            throw new IdentityException(409, "bad_leader",
                id + " cannot lead a group: a leader is a live, leased seat, and it is a " + rec.getKind() + " that is "
                    + GroupRules.effectiveState(rec, now) + (rec.getLease() == null ? "" : " (" + rec.getLease() + ")"),
                Map.of("identity", id, "kind", rec.getKind(), "state", GroupRules.effectiveState(rec, now)));
        }
    }

    private void requireMember(DSLContext ctx, String tenant, String id, String group) {
        if (!memberships.isMember(ctx, tenant, id, group)) {
            throw new IdentityException(409, "leader_not_member",
                id + " is not a member of " + group + ": it joins first, then takes the group", Map.of("identity", id, "group", group));
        }
    }

    /** Refuses a write that would leave a second live group serving a topic; the caller holds the topic locks. */
    private static void refuseSecondServer(DSLContext ctx, String tenant, String name, List<String> serves,
                                           OffsetDateTime now) {
        if (serves.isEmpty()) {
            return;
        }
        for (GroupRecordsRecord other : ctx.selectFrom(GROUP_RECORDS).where(GROUP_RECORDS.TENANT_ID.eq(tenant))
            .and(GROUP_RECORDS.NAME.ne(name)).and(GROUP_RECORDS.ENDED_AT.isNull()).orderBy(GROUP_RECORDS.NAME).fetch()) {
            if (GroupRules.disbanded(other, now)) {
                continue;
            }
            for (String topic : other.getServes()) {
                if (serves.contains(topic)) {
                    throw new IdentityException(409, "topic_served",
                        "the topic " + topic + " is served by " + other.getName() + " already: one live group serves a topic",
                        Map.of("topic", topic, "group", other.getName()));
                }
            }
        }
    }

    private static void requireKeysServed(Map<String, Integer> byTopic, List<String> serves) {
        for (String key : byTopic.keySet()) {
            if (!serves.contains(key)) {
                throw new IdentityException(400, "deadline_topic_not_served",
                    "ack_deadline_by_topic names " + key + ", which the group does not serve", Map.of("topic", key));
            }
        }
    }

    /** The stored per-topic deadlines, without the keys of a topic the group no longer serves. */
    private static Map<String, Integer> currentByTopic(GroupRecordsRecord rec, List<String> serves) {
        Map<String, Integer> out = new LinkedHashMap<>();
        Map<String, Object> stored = IdentityRows.parse(rec.getAckDeadlineByTopic());
        if (stored != null) {
            stored.forEach((k, v) -> {
                if (serves.contains(k)) {
                    out.put(k, ((Number) v).intValue());
                }
            });
        }
        return out;
    }

    private static void audit(DSLContext ctx, String tenant, String operation, String actor, Map<String, Object> details) {
        CatalogRepository.insertGcAuditRow(ctx, tenant, operation, "", actor, false, List.of(), details);
    }

    private static IdentityException unknown(String name) {
        return new IdentityException(404, "unknown_group", "no group record " + name, Map.of("group", name));
    }

    // ── rendering ────────────────────────────────────────────────────────────

    /** The stored fields, for the audit rows and the routes. */
    private static Map<String, Object> fields(GroupRecordsRecord r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", r.getName());
        m.put("leader", r.getLeader());
        m.put("purpose", r.getPurpose());
        m.put("requested_by", r.getRequestedBy());
        m.put("formed_at", IdentityRows.str(r.getFormedAt()));
        m.put("ends", IdentityRows.str(r.getEnds()));
        m.put("ended_at", IdentityRows.str(r.getEndedAt()));
        m.put("ack_deadline", r.getAckDeadline());
        m.put("ack_deadline_by_topic", IdentityRows.parse(r.getAckDeadlineByTopic()));
        m.put("serves", new ArrayList<>(Arrays.asList(r.getServes())));
        return m;
    }

    /** The record as the routes return it, with whether it is live now. */
    static Map<String, Object> view(GroupRecordsRecord r, OffsetDateTime now) {
        Map<String, Object> m = fields(r);
        m.put("live", !GroupRules.disbanded(r, now));
        return m;
    }
}
