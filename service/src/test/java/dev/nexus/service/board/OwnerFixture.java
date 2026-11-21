// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Recorded tuples for the owner decision tests: push, done and owner rows on a fixed clock that each row advances by
 * ten seconds, a group, and the identity facts. Nothing here reads a clock or a database.
 */
final class OwnerFixture {

    static final ObjectMapper MAPPER = new ObjectMapper();
    static final String BASE = "nexus-develop";
    static final Instant T0 = Instant.parse("2026-11-18T12:00:00Z");

    Instant clock = T0;
    int seq;
    final List<BoardRow> pushes = new ArrayList<>();
    final List<BoardRow> dones = new ArrayList<>();
    final List<BoardRow> owners = new ArrayList<>();
    final Map<String, String> stamps = new LinkedHashMap<>();
    final Map<String, Identities.Presence> presence = new LinkedHashMap<>();
    String leader = "L";
    List<String> members = new ArrayList<>(List.of("L"));
    int deadlineMinutes = 20;
    Map<String, Integer> byTopic = new LinkedHashMap<>();
    boolean served = true;
    Instant now = T0.plus(Duration.ofHours(1));

    OwnerFixture() {
        seat("L");   // the leader is a live, leased seat unless a test says otherwise
    }

    /** A live, leased identity that a session {@code s<id>} maps to, joined to the group. */
    OwnerFixture seat(String id) {
        stamps.put("s" + id, id);
        presence.put(id, new Identities.Presence("live", "leased", "seat"));
        if (!members.contains(id)) {
            members.add(id);
        }
        return this;
    }

    OwnerFixture state(String id, String state, String lease) {
        presence.put(id, new Identities.Presence(state, lease, "seat"));
        return this;
    }

    /** An identity of this kind (a roster can hold an ephemeral or delegated identity that joined early). */
    OwnerFixture kind(String id, String kind) {
        Identities.Presence p = presence.get(id);
        presence.put(id, new Identities.Presence(p.state(), p.lease(), kind));
        return this;
    }

    OwnerFixture now(Instant t) {
        now = t;
        return this;
    }

    /** Records what a decision posted at {@code now}, then moves the clock to {@code at}. */
    OwnerFixture recordNow(OwnerDecision.Decision d) {
        return record(d, now);
    }

    OwnerFixture notMember(String id) {
        members.remove(id);
        return this;
    }

    private Instant tick() {
        clock = clock.plusSeconds(10);
        return clock;
    }

    /** A push record by the session of {@code pusher} ({@code null}: no stamp). */
    OwnerFixture push(String sha, String base, String pusherSeat) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", pusherSeat == null ? "unknown" : pusherSeat);
        dims.put("kind", "push");
        dims.put("sha", sha);
        pushes.add(new BoardRow("p" + (++seq), tick(), dims,
            "{\"base\":\"" + base + "\",\"n\":1}", pusherSeat == null ? null : "s" + pusherSeat));
        return this;
    }

    /** A push record stamped with a session of its own, whatever it maps to. */
    OwnerFixture pushStamped(String sha, String base, String session) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "x");
        dims.put("kind", "push");
        dims.put("sha", sha);
        pushes.add(new BoardRow("p" + (++seq), tick(), dims, "{\"base\":\"" + base + "\",\"n\":1}", session));
        return this;
    }

    /** A done row with {@code dims.outcome}. */
    OwnerFixture done(String sha, String outcome) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "github");
        dims.put("kind", "sha");
        dims.put("sha", sha);
        dims.put("outcome", outcome);
        dones.add(new BoardRow("d" + (++seq), tick(), dims, "{\"state\":\"concluded\",\"sha\":\"" + sha + "\"}", null));
        return this;
    }

    /** A done row with {@code dims.outcome} and {@code dims.branch_outcome}; a null {@code branchOutcome} omits the dim. */
    OwnerFixture doneBranch(String sha, String outcome, String branchOutcome) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "github");
        dims.put("kind", "sha");
        dims.put("sha", sha);
        dims.put("outcome", outcome);
        if (branchOutcome != null) {
            dims.put("branch_outcome", branchOutcome);
        }
        dones.add(new BoardRow("d" + (++seq), tick(), dims, "{\"state\":\"concluded\",\"sha\":\"" + sha + "\"}", null));
        return this;
    }

    /** A done row with {@code dims.outcome}, no {@code branch_outcome}, and a body whose {@code carried} is null. */
    OwnerFixture doneCarriedNull(String sha, String outcome) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "github");
        dims.put("kind", "sha");
        dims.put("sha", sha);
        dims.put("outcome", outcome);
        dones.add(new BoardRow("d" + (++seq), tick(), dims,
            "{\"state\":\"concluded\",\"sha\":\"" + sha + "\",\"carried\":null}", null));
        return this;
    }

    /** A done row before the adapter's result dims: the verdict is in {@code runs} only. */
    OwnerFixture doneRuns(String sha, String... conclusions) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "github");
        dims.put("kind", "sha");
        StringBuilder runs = new StringBuilder();
        for (int i = 0; i < conclusions.length; i++) {
            runs.append(i == 0 ? "" : ",").append("[").append(100 + i).append(",1,\"CI\",")
                .append(conclusions[i] == null ? "null" : "\"" + conclusions[i] + "\"").append("]");
        }
        dones.add(new BoardRow("d" + (++seq), tick(), dims,
            "{\"state\":\"concluded\",\"sha\":\"" + sha + "\",\"runs\":[" + runs + "]}", null));
        return this;
    }

    /** An owner row as the stamper posted it earlier. */
    OwnerFixture ownerRow(OwnerDecision.Post post) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "engine");
        dims.put("kind", post.kind());
        dims.put("sha", post.sha());
        owners.add(new BoardRow("o" + (++seq), tick(), dims, post.bodyJson(), null));
        return this;
    }

    /** The row a decision posted, recorded as if the stamper had written it at {@code now}. */
    OwnerFixture record(OwnerDecision.Decision d, Instant at) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "engine");
        dims.put("kind", d.post().kind());
        dims.put("sha", d.post().sha());
        Instant last = owners.isEmpty() ? null : owners.getLast().createdAt();
        Instant when = last != null && !at.isAfter(last) ? last.plusSeconds(1) : at;   // rows land in order
        owners.add(new BoardRow(String.format("o%05d", ++seq), when, dims, d.post().bodyJson(), null));
        clock = when;
        return this;
    }

    OwnerFixture ack(String pusherSeat, String sha) {
        return ack(pusherSeat, sha, "s" + pusherSeat);
    }

    /** An ack row with the given {@code from} dim and sender session (null: no session stamp). */
    OwnerFixture ack(String from, String sha, String senderSession) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", from);
        dims.put("kind", "ack");
        dims.put("sha", sha);
        owners.add(new BoardRow("a" + (++seq), tick(), dims, "{}", senderSession));
        return this;
    }

    OwnerDecision.Input input() {
        GroupView group = served
            ? new GroupView("push-train", leader, List.copyOf(members), deadlineMinutes, Map.copyOf(byTopic)) : null;
        return new OwnerDecision.Input(BASE, List.copyOf(pushes), List.copyOf(dones), List.copyOf(owners), group,
            new Identities(Map.copyOf(stamps), Map.copyOf(presence)), now);
    }

    OwnerDecision.Decision decide() {
        return OwnerDecision.decide(input(), OwnerDecision.Trigger.DONE);
    }

    OwnerDecision.Decision onDeadline() {
        return OwnerDecision.decide(input(), OwnerDecision.Trigger.DEADLINE);
    }

    static Map<String, Object> bodyOf(OwnerDecision.Post post) {
        return MAPPER.convertValue(post.body(), new TypeReference<Map<String, Object>>() { });
    }
}
