// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-252 Step 3: who owns develop's red, as a pure function of the rows read under the topic lock plus the clock
 * the caller injects in {@link Input#now()}. No database, no clock read, no write: it returns the one row to post
 * (or none) and the instant the acknowledgement deadline of the resulting state falls due (or none).
 *
 * <p>The state comes from commit order ({@link PushChain}): only {@code success} and {@code failure} are verdicts,
 * and a verdict counts only if its tip is the newest tip in the chain that has one, so a late red on an older tip
 * raises nothing and a late green on an older tip clears nothing. The suspects are every tip after the last green
 * up to the red tip; the owner is the pusher of the earliest suspect, or the leader with the first reason that
 * holds (owner_unleased, owner_not_live, owner_not_member, no_record, chain_gap). Rows: at most one per call.
 * A hand-off or an escalation is posted only for {@link Trigger#DEADLINE}.
 *
 * <p>Reading of the escalation counter: {@code handoffs} counts hands-on, the escalation to Sam included. The
 * first missed deadline hands the red to next up (handoffs 1, {@code next} null, since Sam is the one after); the
 * next missed deadline, or a missed deadline with no next up, posts the {@code escalated} row (handoffs 2 or 1).
 */
public final class OwnerDecision {

    public enum Trigger { DONE, DEADLINE }

    /** The rows and facts one evaluation reads. {@code group} is null when no live group serves the topic. */
    public record Input(String base, List<BoardRow> pushes, List<BoardRow> dones, List<BoardRow> owners,
                        GroupView group, Identities ids, Instant now) {
    }

    /** The row to post: its {@code kind} dim ({@code owner} or {@code clear}), its {@code sha} dim and its body. */
    public record Post(String kind, String sha, Map<String, Object> body) {
        public String bodyJson() {
            return json(body);
        }
    }

    /** {@code post} may be null; {@code deadline} is when the resulting state's deadline falls due, or null. */
    public record Decision(Post post, Instant deadline) {
        static Decision none() {
            return new Decision(null, null);
        }
    }

    public static final String REASON_EARLIEST = "earliest_suspect";
    public static final String REASON_HANDOFF = "handoff";
    public static final String REASON_ESCALATED = "escalated";
    public static final String REASON_STAMPER_ERROR = "stamper_error";
    static final int MAX_SUSPECTS = 10;
    static final int MAX_BODY_BYTES = 1024;

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP = new TypeReference<>() { };

    private OwnerDecision() {
    }

    // ── entry points ─────────────────────────────────────────────────────────

    public static Decision decide(Input in, Trigger trigger) {
        Map<String, DoneVerdict.Verdict> verdicts = new LinkedHashMap<>();
        Map<String, Instant> verdictTimes = new LinkedHashMap<>();
        List<BoardRow> dones = new ArrayList<>(in.dones());
        dones.sort(BY_TIME);
        for (BoardRow d : dones) {
            DoneVerdict.of(d).filter(DoneVerdict.Verdict::counts).ifPresent(v -> {
                verdicts.put(v.sha(), v);   // a later post for the same tip supersedes (a rerun)
                verdictTimes.put(v.sha(), d.createdAt());
            });
        }
        List<PushChain.Node> chain = PushChain.order(in.pushes(), verdictTimes);
        int newest = -1;
        for (int i = chain.size() - 1; i >= 0; i--) {
            if (verdicts.containsKey(chain.get(i).sha())) {
                newest = i;
                break;
            }
        }
        if (newest < 0) {
            return Decision.none();
        }
        BoardRow cur = currentRow(in.owners());
        boolean open = cur != null && "owner".equals(cur.kind());
        PushChain.Node tip = chain.get(newest);

        if (DoneVerdict.OK.equals(verdicts.get(tip.sha()).outcome())) {
            // Green: a clear row, for a topic with an open owner row, whatever the group's state.
            return open ? new Decision(new Post("clear", tip.sha(), new LinkedHashMap<>()), null) : Decision.none();
        }
        if (in.group() == null) {
            return Decision.none();   // no live group serves the topic: no owner row of any kind
        }

        int green = -1;
        for (int i = newest - 1; i >= 0; i--) {
            DoneVerdict.Verdict v = verdicts.get(chain.get(i).sha());
            if (v != null && DoneVerdict.OK.equals(v.outcome())) {
                green = i;
                break;
            }
        }
        List<PushChain.Node> suspects = chain.subList(green + 1, newest + 1);
        boolean gap = suspects.stream().anyMatch(PushChain.Node::gapBefore);
        Set<String> redTips = new HashSet<>();
        for (PushChain.Node n : suspects) {
            DoneVerdict.Verdict v = verdicts.get(n.sha());
            if (v != null && DoneVerdict.RED.equals(v.outcome())) {
                redTips.add(n.sha());
            }
        }

        Map<String, Object> curBody = open ? parse(cur.body()) : null;
        if (!open) {
            return withDeadline(newEpisode(in, tip, suspects, gap), in, redTips);
        }
        if (!tip.sha().equals(cur.sha())) {
            String earliest = suspects.get(0).sha();
            List<?> was = curBody.get("suspects") instanceof List<?> l ? l : List.of();
            if (!was.isEmpty() && earliest.equals(was.get(0))) {
                return withDeadline(refresh(in, tip, suspects, curBody), in, redTips);
            }
            return withDeadline(newEpisode(in, tip, suspects, gap), in, redTips);
        }
        Instant due = dueAt(in, curBody, redTips);
        if (trigger != Trigger.DEADLINE || due == null || in.now().isBefore(due)) {
            return new Decision(null, due);
        }
        return withDeadline(handOn(in, cur, curBody, suspects), in, redTips);
    }

    /**
     * A stamper error posts the leader as owner with {@code reason=stamper_error}, for a red tip {@code sha} with no
     * open owner row at that tip and a live serving group; otherwise nothing. The row runs the acknowledgement deadline
     * like every owner row (RDR-252): {@code episode_at} plus the topic's deadline, so a red whose evaluation failed is
     * still handed on, or escalated, when nobody acknowledges it; the deadline's own evaluation re-reads the rows.
     */
    public static Decision stamperError(GroupView group, String base, String sha, List<BoardRow> owners, Instant now) {
        if (group == null || sha == null) {
            return Decision.none();
        }
        BoardRow cur = currentRow(owners);
        if (cur != null && "owner".equals(cur.kind()) && sha.equals(cur.sha())) {
            return Decision.none();
        }
        return new Decision(new Post("owner", sha,
            body(group.leader(), null, REASON_STAMPER_ERROR, List.of(), 0, now, false, 0)),
            now.plus(Duration.ofMinutes(group.deadlineMinutes(base))));
    }

    /**
     * The fail-safe for a deadline whose evaluation threw: built only from the current engine owner row, the group and
     * the liveness facts, with no push chain and no verdicts, so a fault in {@link #decide} cannot defeat it. For an
     * open row that is not {@code escalated}: not yet due, no post and the deadline to arm at; due, an {@code escalated}
     * row naming the leader if live, else the first live member, else no one, with {@code handoffs} one higher, the
     * suspects carried over and no deadline. An ack from the row's owner on the row's own tip ends it. Nothing for an
     * escalated or clear row, no row, or no live group.
     */
    static Decision failSafe(GroupView group, Identities ids, List<BoardRow> owners, String base, Instant now) {
        BoardRow cur = currentRow(owners);
        Instant due = rowDeadline(group, base, owners);
        if (cur == null || due == null) {
            return Decision.none();
        }
        if (now.isBefore(due)) {
            return new Decision(null, due);
        }
        Map<String, Object> body = parse(cur.body());
        String owner = (String) body.get("owner");
        for (BoardRow a : owners) {
            if ("ack".equals(a.kind()) && cur.sha().equals(a.sha()) && owner.equals(ids.identityOf(a.senderSession()))) {
                return Decision.none();
            }
        }
        String target = ids.live(group.leader()) ? group.leader() : null;
        for (String m : group.members()) {
            if (target == null && ids.live(m)) {
                target = m;
            }
        }
        List<String> suspects = body.get("suspects") instanceof List<?> l ? l.stream().map(String::valueOf).toList() : List.of();
        int total = body.get("suspects_total") instanceof Number n ? n.intValue() : suspects.size();
        return new Decision(new Post("owner", cur.sha(),
            body(target, null, REASON_ESCALATED, suspects, total, now, false, ((Number) body.get("handoffs")).intValue() + 1)), null);
    }

    /**
     * Whether the owner's stamped ack exists on the current owner row's own tip, posted at or after the row: an ack
     * row with a non-null {@code sender_session} whose {@code from} equals the row's owner. The engine stamps an ack's
     * {@code from} with the identity of its sender session at write time (and leaves it as typed when the session maps
     * to nothing), so this reads the identity the deadline counts from the row itself and needs no identity read; the
     * fail-safe can then tell "possibly acknowledged" from "plainly not" when the liveness read is down. The time
     * bound keeps a handed-away owner's late ack from suppressing the next owner's escalation.
     *
     * <p>Residual: a minted session with no identity that hand-types the owner's id in {@code from} still holds the
     * escalation off while the read is down (a recovered read then matches nothing and escalates). Known transition: a
     * pre-0.1.200 ack row has a typed {@code from} (perhaps an alias) and does not match, so during a liveness fault an
     * open red acknowledged before the deploy escalates with owner null, a bounded false wake. This check is also
     * narrower than the normal count ({@link #dueAt}): an owner's ack before its owner row, or on an older tip, makes
     * the fail-safe escalate where the normal path would count it, the safe direction.
     */
    static boolean ackOnTip(List<BoardRow> owners) {
        BoardRow cur = currentRow(owners);
        if (cur == null || cur.sha() == null) {
            return false;
        }
        Object owner = parse(cur.body()).get("owner");
        if (!(owner instanceof String ownerId)) {
            return false;
        }
        for (BoardRow a : owners) {
            if ("ack".equals(a.kind()) && cur.sha().equals(a.sha()) && !a.createdAt().isBefore(cur.createdAt())
                && a.senderSession() != null && ownerId.equals(a.dim("from"))) {
                return true;
            }
        }
        return false;
    }

    /**
     * When the deadline of the current open owner row falls due, from that row alone ({@code episode_at} plus the
     * topic's deadline); null when the current row is not an open owner row, is {@code escalated} or names no owner,
     * or there is no live group.
     */
    static Instant rowDeadline(GroupView group, String base, List<BoardRow> owners) {
        BoardRow cur = currentRow(owners);
        if (group == null || cur == null || !"owner".equals(cur.kind())) {
            return null;
        }
        Map<String, Object> body = parse(cur.body());
        if (REASON_ESCALATED.equals(body.get("reason")) || body.get("owner") == null) {
            return null;
        }
        return Instant.parse((String) body.get("episode_at")).plus(Duration.ofMinutes(group.deadlineMinutes(base)));
    }

    // ── rows ─────────────────────────────────────────────────────────────────

    private static Decision newEpisode(Input in, PushChain.Node tip, List<PushChain.Node> suspects, boolean gap) {
        GroupView g = in.group();
        PushChain.Node earliest = suspects.get(0);
        String pusher = pusherOf(earliest, in.ids());
        String reason = null;
        if (pusher != null) {
            if (in.ids().unleased(pusher)) {
                reason = "owner_unleased";
            } else if (!in.ids().live(pusher)) {
                reason = "owner_not_live";
            } else if (!g.isMember(pusher)) {
                reason = "owner_not_member";
            }
        }
        if (reason == null && (pusher == null || !tip.recorded())) {
            reason = "no_record";
        }
        if (reason == null && gap) {
            reason = "chain_gap";
        }
        String owner = reason == null ? pusher : g.leader();
        String next = nextUp(owner, suspects, g, in.ids());
        return new Decision(new Post("owner", tip.sha(),
            body(owner, next, reason == null ? REASON_EARLIEST : reason, shas(suspects), suspects.size(), in.now(),
                false, 0)), null);
    }

    /** A newer red with the same earliest suspect: owner, next, handoffs and reason carried, episode kept. */
    private static Decision refresh(Input in, PushChain.Node tip, List<PushChain.Node> suspects, Map<String, Object> cur) {
        Instant episode = Instant.parse((String) cur.get("episode_at"));
        return new Decision(new Post("owner", tip.sha(),
            body((String) cur.get("owner"), (String) cur.get("next"), (String) cur.get("reason"), shas(suspects),
                suspects.size(), episode, true, ((Number) cur.get("handoffs")).intValue())), null);
    }

    /** The deadline passed with no counting ack: hand the red to next up, or escalate. */
    private static Decision handOn(Input in, BoardRow cur, Map<String, Object> curBody, List<PushChain.Node> suspects) {
        GroupView g = in.group();
        int handoffs = ((Number) curBody.get("handoffs")).intValue();
        String owner = (String) curBody.get("owner");
        // A stamper_error row shows no next up (Sam is the one after the leader), so it escalates at its deadline.
        String next = handoffs == 0 && !REASON_STAMPER_ERROR.equals(curBody.get("reason"))
            ? nextUp(owner, suspects, g, in.ids()) : null;
        if (next != null) {
            return new Decision(new Post("owner", cur.sha(),
                body(next, null, REASON_HANDOFF, shas(suspects), suspects.size(), in.now(), false, handoffs + 1)), null);
        }
        String target = in.ids().live(g.leader()) ? g.leader() : null;
        for (String m : g.members()) {
            if (target == null && in.ids().live(m)) {
                target = m;
            }
        }
        return new Decision(new Post("owner", cur.sha(),
            body(target, null, REASON_ESCALATED, shas(suspects), suspects.size(), in.now(), false, handoffs + 1)), null);
    }

    /** Fills in the deadline of the state the posted row makes. */
    private static Decision withDeadline(Decision d, Input in, Set<String> redTips) {
        Instant due = dueAt(in, parse(d.post().bodyJson()), redTips);
        return new Decision(d.post(), due);
    }

    /**
     * When the acknowledgement deadline of the open row {@code body} falls due; null when none runs: the row is
     * {@code escalated}, has no owner, or a counting ack came in. An ack counts when its stamp maps to the row's
     * owner and its sha is any red tip since the last green.
     */
    private static Instant dueAt(Input in, Map<String, Object> body, Set<String> redTips) {
        String owner = (String) body.get("owner");
        if (REASON_ESCALATED.equals(body.get("reason")) || owner == null) {
            return null;
        }
        for (BoardRow a : in.owners()) {
            if ("ack".equals(a.kind()) && redTips.contains(a.sha()) && owner.equals(in.ids().identityOf(a.senderSession()))) {
                return null;
            }
        }
        return Instant.parse((String) body.get("episode_at"))
            .plus(Duration.ofMinutes(in.group().deadlineMinutes(in.base())));
    }

    // ── owner and next up ────────────────────────────────────────────────────

    /** The identity a suspect's push record names through its stamp, or null: unrecorded, unstamped or unmapped. */
    private static String pusherOf(PushChain.Node node, Identities ids) {
        return node.push() == null ? null : ids.identityOf(node.push().senderSession());
    }

    /**
     * Next up, the first that applies: the pusher of a later suspect who is not the owner and is a live, leased seat
     * in the group; the leader, if a live seat and not the owner; the first other live seat by membership order.
     */
    static String nextUp(String owner, List<PushChain.Node> suspects, GroupView g, Identities ids) {
        for (int i = 1; i < suspects.size(); i++) {
            String q = pusherOf(suspects.get(i), ids);
            if (q != null && !q.equals(owner) && g.isMember(q) && ids.liveSeat(q)) {
                return q;
            }
        }
        if (g.leader() != null && !g.leader().equals(owner) && ids.liveSeat(g.leader())) {
            return g.leader();
        }
        for (String m : g.members()) {
            if (!m.equals(owner) && ids.liveSeat(m)) {
                return m;
            }
        }
        return null;
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static final Comparator<BoardRow> BY_TIME = Comparator.comparing(BoardRow::createdAt).thenComparing(BoardRow::id);

    /**
     * The newest owner or clear row the engine posted; acks are not state, nor is a row another writer posted (one that
     * says {@code from=engine} but carries a sender session is a client's), nor an owner row whose body
     * cannot be read as one (it is treated as absent, never thrown on).
     */
    private static BoardRow currentRow(List<BoardRow> owners) {
        return owners.stream().filter(OwnerDecision::isState).max(BY_TIME).orElse(null);
    }

    private static boolean isState(BoardRow r) {
        // {@code from} is a dim any writer can set, so it proves nothing. The stamper writes with no sender session, so
        // a row that carries one (a blank one included: the aura reads the stamp with {@code is None}, and the two must
        // agree) was written by a client through the route and is not state. This check alone does not prove the engine
        // wrote a row: a raw-HTTP client with no minted session also has a null stamp. The control is the tuple route,
        // which refuses a client write that sets from=engine on a board/ci topic (TupleHandler, BoardCiGuard).
        if (!"engine".equals(r.dim("from")) || r.senderSession() != null) {
            return false;
        }
        if ("clear".equals(r.kind())) {
            return true;
        }
        if (!"owner".equals(r.kind())) {
            return false;
        }
        Map<String, Object> b = parse(r.body());
        if (b == null || !(b.get("episode_at") instanceof String episode) || !(b.get("handoffs") instanceof Number)) {
            return false;
        }
        try {
            Instant.parse(episode);
        } catch (java.time.format.DateTimeParseException e) {
            return false;
        }
        return (b.get("owner") == null || b.get("owner") instanceof String)
            && (b.get("next") == null || b.get("next") instanceof String)
            && (b.get("reason") == null || b.get("reason") instanceof String);
    }

    private static List<String> shas(List<PushChain.Node> suspects) {
        return suspects.stream().map(PushChain.Node::sha).toList();
    }

    /** The owner row body; the suspects are cut to ten, and further if the body would pass 1024 bytes. */
    static Map<String, Object> body(String owner, String next, String reason, List<String> suspects, int total,
                                    Instant episodeAt, boolean refresh, int handoffs) {
        List<String> listed = new ArrayList<>(suspects.subList(0, Math.min(MAX_SUSPECTS, suspects.size())));
        while (true) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("owner", owner);
            m.put("next", next);
            m.put("reason", reason);
            m.put("suspects", List.copyOf(listed));
            m.put("suspects_total", total);
            m.put("episode_at", episodeAt.toString());
            m.put("refresh", refresh);
            m.put("handoffs", handoffs);
            if (json(m).getBytes(java.nio.charset.StandardCharsets.UTF_8).length <= MAX_BODY_BYTES || listed.size() <= 1) {
                return m;
            }
            listed.remove(listed.size() - 1);
        }
    }

    /**
     * The post with {@code owner_alias} and {@code next_alias} beside the ids they name (RDR-252, Sam 2026-11-19). The
     * decision is a pure function of ids; the stamper reads each id's live alias and adds it here, at stamp time. A
     * null alias leaves its field out, so a reader falls back to the id. The suspects are cut again, as in
     * {@link #body}, when the aliases would carry the body past 1024 bytes.
     */
    static Post withAliases(Post post, String ownerAlias, String nextAlias) {
        if (ownerAlias == null && nextAlias == null) {
            return post;
        }
        Map<String, Object> m = new LinkedHashMap<>();
        post.body().forEach((k, v) -> {
            m.put(k, v);
            if ("owner".equals(k) && ownerAlias != null) {
                m.put("owner_alias", ownerAlias);
            } else if ("next".equals(k) && nextAlias != null) {
                m.put("next_alias", nextAlias);
            }
        });
        while (json(m).getBytes(java.nio.charset.StandardCharsets.UTF_8).length > MAX_BODY_BYTES
            && m.get("suspects") instanceof List<?> l && l.size() > 1) {
            m.put("suspects", List.copyOf(l.subList(0, l.size() - 1)));
        }
        return new Post(post.kind(), post.sha(), m);
    }

    static String json(Map<String, Object> m) {
        try {
            return MAPPER.writeValueAsString(m);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    static Map<String, Object> parse(String body) {
        try {
            return body == null || body.isBlank() ? new LinkedHashMap<>() : MAPPER.readValue(body, MAP);
        } catch (java.io.IOException e) {
            return new LinkedHashMap<>();
        }
    }
}
