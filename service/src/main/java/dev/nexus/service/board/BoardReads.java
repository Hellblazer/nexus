// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.GroupRepository;
import dev.nexus.service.db.IdentityRepository;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;

/**
 * Everything the owner decision reads, gathered for one evaluation: the live rows of a topic family's three
 * subspaces, the live group that serves it (through {@link GroupRepository#servingGroup}, never the table), and the
 * identity facts. The rows come from the caller's transaction, so under the topic lock they are what a concurrent
 * evaluation committed before it; the group and identity reads are their own short transactions.
 */
final class BoardReads {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, String>> DIMS = new TypeReference<>() { };
    /** The most rows read from one subspace: the template's own cap on live rows. */
    private static final int ROW_CAP = 5000;

    private BoardReads() {
    }

    static String subspace(String base, String suffix) {
        return "board/ci/" + base + "--" + suffix;
    }

    /** The live rows of {@code board/ci/<base>--<suffix>}, oldest first. */
    static List<BoardRow> rows(DSLContext ctx, String tenant, String base, String suffix) {
        List<BoardRow> out = new ArrayList<>();
        ctx.select(TUPLES.ID, TUPLES.CREATED_AT, TUPLES.DIMS, TUPLES.BODY, TUPLES.SENDER_SESSION, TUPLES.EXPIRES_AT).from(TUPLES)
            .where(TUPLES.TENANT_ID.eq(tenant)).and(TUPLES.SUBSPACE.eq(subspace(base, suffix)))
            .and(TUPLES.CONSUMED_AT.isNull()).and(TUPLES.EXPIRES_AT.gt(DSL.currentOffsetDateTime()))
            .orderBy(TUPLES.CREATED_AT.asc(), TUPLES.ID.asc()).limit(ROW_CAP)
            .forEach(r -> out.add(new BoardRow(java.util.HexFormat.of().formatHex(r.value1()), r.value2().toInstant(),
                dims(r.value3() == null ? null : r.value3().data()), r.value4(), r.value5(), r.value6().toInstant())));
        return out;
    }

    private static Map<String, String> dims(String json) {
        try {
            return json == null ? Map.of() : MAPPER.readValue(json, DIMS);
        } catch (java.io.IOException e) {
            return Map.of();
        }
    }

    /** The live group serving {@code base}, with its roster in join order and its record's leader, or null. */
    static GroupView group(GroupRepository groups, IdentityRepository identities, String tenant, String base) {
        Optional<Map<String, Object>> g = groups.servingGroup(tenant, base);
        if (g.isEmpty()) {
            return null;
        }
        Map<String, Object> rec = g.get();
        String name = (String) rec.get("name");
        List<String> members = identities.rosterOf(tenant, name).stream().map(m -> (String) m.get("identity")).toList();
        Map<String, Integer> byTopic = new LinkedHashMap<>();
        if (rec.get("ack_deadline_by_topic") instanceof Map<?, ?> m) {
            m.forEach((k, v) -> byTopic.put(String.valueOf(k), ((Number) v).intValue()));
        }
        int stall = rec.get("stall_factor") instanceof Number n ? n.intValue() : GroupView.DEFAULT_STALL_FACTOR;
        return new GroupView(name, (String) rec.get("leader"), members, ((Number) rec.get("ack_deadline")).intValue(), byTopic,
            stall);
    }

    /**
     * Each stamp in the rows mapped to an identity through the session index, a delegated identity to its parent,
     * and the state and lease of every identity the decision may name (the pushers, the leader, the members).
     */
    static Identities identities(IdentityRepository identities, String tenant, List<BoardRow> pushes,
                                 List<BoardRow> owners, GroupView group) {
        Set<String> stamps = new LinkedHashSet<>();
        pushes.forEach(r -> addStamp(stamps, r));
        owners.stream().filter(r -> "ack".equals(r.kind()) || "disclaim".equals(r.kind())).forEach(r -> addStamp(stamps, r));
        Map<String, String> rawIdentity = new LinkedHashMap<>();
        for (String stamp : stamps) {
            identities.sessionIdentity(tenant, stamp).ifPresent(s -> rawIdentity.put(stamp, s.identity()));
        }
        Set<String> wanted = new LinkedHashSet<>(rawIdentity.values());
        if (group != null) {
            wanted.add(group.leader());
            wanted.addAll(group.members());
        }
        Map<String, IdentityRepository.Liveness> read = new LinkedHashMap<>(identities.liveness(tenant, wanted));
        Set<String> parents = new LinkedHashSet<>();
        read.values().stream().filter(l -> "delegated".equals(l.kind()) && l.parent() != null).forEach(l -> parents.add(l.parent()));
        parents.removeAll(read.keySet());
        read.putAll(identities.liveness(tenant, parents));

        Map<String, String> identityOfStamp = new LinkedHashMap<>();
        rawIdentity.forEach((stamp, id) -> {
            IdentityRepository.Liveness l = read.get(id);
            identityOfStamp.put(stamp, Identities.parentIfDelegated(id, l));
        });
        Map<String, Identities.Presence> presence = new LinkedHashMap<>();
        read.forEach((id, l) -> presence.put(id, new Identities.Presence(l.state(), l.lease(), l.kind())));
        return new Identities(identityOfStamp, presence);
    }

    private static void addStamp(Set<String> stamps, BoardRow r) {
        if (r.senderSession() != null && !r.senderSession().isBlank()) {
            stamps.add(r.senderSession());
        }
    }
}
