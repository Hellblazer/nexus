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
 *
 * <p>Post-close audit (2026-11-22): the owner's own stamped push after a red tip, chained from it, counts as an ack
 * (S1a); a {@code disclaim} row from the current owner hands the red on at once with reason {@code disclaimed} (S1b);
 * an acknowledged red still open after {@code stall_factor} deadlines from the last ack or push escalates with the
 * existing reason {@code escalated} and {@code stalled: true} (S1c); next up gets one {@code nudge} row at half the
 * deadline while nothing is acknowledged (S2).
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

    /**
     * {@code post} may be null; {@code deadline} is when the resulting state's deadline falls due (the hand-off
     * deadline of an unacknowledged red, the stall deadline of an acknowledged one), or null. {@code nudgeAt} is when
     * the half-deadline nudge to next up falls due, or null when none is pending. The timer wakes at the earlier of
     * the two ({@link #wakeAt()}).
     */
    public record Decision(Post post, Instant deadline, Instant nudgeAt) {
        public Decision(Post post, Instant deadline) {
            this(post, deadline, null);
        }

        static Decision none() {
            return new Decision(null, null, null);
        }

        /** The instant the deadline timer is armed for: the earlier of the deadline and the pending nudge, or null. */
        public Instant wakeAt() {
            if (deadline == null || nudgeAt == null) {
                return deadline != null ? deadline : nudgeAt;
            }
            return nudgeAt.isBefore(deadline) ? nudgeAt : deadline;
        }
    }

    public static final String REASON_EARLIEST = "earliest_suspect";
    public static final String REASON_HANDOFF = "handoff";
    /** The owner said "not mine" (a {@code disclaim} row): the hand-off row carries this reason. */
    public static final String REASON_DISCLAIMED = "disclaimed";
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
        Map<String, Instant> redAt = new LinkedHashMap<>();   // when each tip's red verdict first came in
        List<BoardRow> dones = new ArrayList<>(in.dones());
        dones.sort(BY_TIME);
        for (BoardRow d : dones) {
            DoneVerdict.of(d).filter(DoneVerdict.Verdict::counts).ifPresent(v -> {
                verdicts.put(v.sha(), v);   // a later post for the same tip supersedes (a rerun)
                verdictTimes.put(v.sha(), d.createdAt());
                if (DoneVerdict.RED.equals(v.outcome())) {
                    redAt.putIfAbsent(v.sha(), d.createdAt());
                }
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

        Ctx ctx = new Ctx(in, chain, redTips, redAt);
        Map<String, Object> curBody = open ? parse(cur.body()) : null;
        if (!open) {
            return withSchedule(newEpisode(in, tip, suspects, gap), ctx);
        }
        if (!tip.sha().equals(cur.sha())) {
            String earliest = suspects.get(0).sha();
            List<?> was = curBody.get("suspects") instanceof List<?> l ? l : List.of();
            if (!was.isEmpty() && earliest.equals(was.get(0))) {
                return withSchedule(refresh(in, tip, suspects, curBody), ctx);
            }
            return withSchedule(newEpisode(in, tip, suspects, gap), ctx);
        }
        if (disclaimed(ctx, cur, curBody)) {
            // "Not mine", said by the owner the row names: handed on at once, by the path the deadline takes.
            return withSchedule(handOn(in, cur, curBody, suspects, Mode.DISCLAIMED, null), ctx);
        }
        Schedule due = schedule(ctx, curBody);
        if (trigger == Trigger.DEADLINE && due.at() != null && !in.now().isBefore(due.at())) {
            boolean stall = due.stalled() || !in.now().isBefore(due.cap());
            return withSchedule(handOn(in, cur, curBody, suspects, stall ? Mode.STALLED : Mode.HANDOFF, due), ctx);
        }
        if (due.nudgeAt() != null && !in.now().isBefore(due.nudgeAt()) && in.now().isBefore(due.at())) {
            return new Decision(nudge(cur, curBody), due.at(), null);
        }
        return new Decision(null, due.at(), due.nudgeAt());
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
     * suspects carried over and no deadline. Nothing for an escalated or clear row, no row, or no live group.
     *
     * <p>An ack from the row's owner on the row's own tip, read from the ack rows alone, moves the escalation from the
     * acknowledgement deadline to the stall deadline ({@link #failSafeDue}): the later of {@code episode_at} and the ack,
     * plus {@code stall_factor} deadlines. So a fault in the evaluation no longer leaves an acknowledged red to stand for
     * ever, and the row it then posts is a stall escalation ({@code stalled: true}, {@code stalled_owner} the acker,
     * {@code stalled_since} the ack). The direction stays the safe one, a false wake and never a suppression: a push
     * the owner made is not read (the fail-safe has no push rows or chain by construction, and mapping a push's session
     * to the owner is the identity read this path is the fallback for), so an owner whose only sign is pushes is
     * escalated at its first deadline; a {@code disclaim} is not acted on here (the normal path hands it on at once) and
     * never suppresses the escalation; the hard cap of the normal path is not read.
     */
    static Decision failSafe(GroupView group, Identities ids, List<BoardRow> owners, String base, Instant now) {
        BoardRow cur = currentRow(owners);
        Instant due = failSafeDue(group, ids, base, owners);
        if (cur == null || due == null) {
            return Decision.none();
        }
        if (now.isBefore(due)) {
            return new Decision(null, due);
        }
        Map<String, Object> body = parse(cur.body());
        String owner = (String) body.get("owner");
        Instant ackAt = ownerAckAt(owners, ids);
        String target = ids.live(group.leader()) ? group.leader() : null;
        for (String m : group.members()) {
            if (target == null && ids.live(m)) {
                target = m;
            }
        }
        List<String> suspects = body.get("suspects") instanceof List<?> l ? l.stream().map(String::valueOf).toList() : List.of();
        int total = body.get("suspects_total") instanceof Number n ? n.intValue() : suspects.size();
        Instant redSince = redStart(owners);
        Map<String, Object> stall = ackAt == null ? null
            : stallInfo(new Sign(ackAt, owner), redSince == null ? now : redSince);
        return new Decision(new Post("owner", cur.sha(),
            body(target, null, REASON_ESCALATED, suspects, total, now, false, ((Number) body.get("handoffs")).intValue() + 1,
                stall)), null);
    }

    /**
     * When the fail-safe's escalation falls due, from the owner rows and the group alone: the row's deadline for a red
     * nobody acknowledged, and for one the owner acknowledged (a stamped {@code ack} on the row's tip, {@link
     * #ownerAckAt}) the stall deadline, the later of {@code episode_at} and the ack plus {@code stall_factor}
     * deadlines. {@code ids} may be null when the identity read has not been made (or has failed): then only the ack's
     * own {@code from} counts, the narrower reading, so a red acknowledged under a typed {@code from} is escalated at
     * its first deadline, a false wake and never a suppression.
     */
    static Instant failSafeDue(GroupView group, Identities ids, String base, List<BoardRow> owners) {
        Instant due = rowDeadline(group, base, owners);
        Instant ack = due == null ? null : ownerAckAt(owners, ids);
        if (ack == null) {
            return due;
        }
        Instant episode = Instant.parse((String) parse(currentRow(owners).body()).get("episode_at"));
        return (ack.isAfter(episode) ? ack : episode)
            .plus(Duration.ofMinutes((long) group.deadlineMinutes(base) * Math.max(1, group.stallFactor())));
    }

    /**
     * The time of the latest ack of the current owner row's owner on the row's own tip, or null: a stamped ack (a
     * non-null sender session) whose {@code from} is the owner and that was posted at or after the row, read when
     * {@code ids} is null or carries no mapping at all (the identity read was not made or failed); with the read in hand,
     * one whose session the session index maps to the owner instead.
     */
    static Instant ownerAckAt(List<BoardRow> owners, Identities ids) {
        BoardRow cur = currentRow(owners);
        if (cur == null || cur.sha() == null || !(parse(cur.body()).get("owner") instanceof String ownerId)) {
            return null;
        }
        Instant best = null;
        for (BoardRow a : owners) {
            if (!"ack".equals(a.kind()) || !cur.sha().equals(a.sha())) {
                continue;
            }
            // With the identity read in hand its mapping decides (a typed from proves nothing); without it (not made,
            // or failed, which reads as no mapping at all) only the ack's own from can say.
            boolean byFrom = !a.createdAt().isBefore(cur.createdAt()) && a.senderSession() != null && ownerId.equals(a.dim("from"));
            boolean byIndex = ids != null && ownerId.equals(ids.identityOf(a.senderSession()));
            boolean unread = ids == null || (ids.identityOfStamp().isEmpty() && ids.presence().isEmpty());
            if ((unread ? byFrom : byIndex) && (best == null || a.createdAt().isAfter(best))) {
                best = a.createdAt();
            }
        }
        return best;
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
     * narrower than the normal count ({@link #lastAck}): an owner's ack before its owner row, or on an older tip, makes
     * the fail-safe escalate where the normal path would count it, the safe direction.
     */
    static boolean ackOnTip(List<BoardRow> owners) {
        return ownerAckAt(owners, null) != null;
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

    /** Why a red is handed on: the deadline passed unacknowledged, the owner disclaimed, or the red stalled. */
    private enum Mode { HANDOFF, DISCLAIMED, STALLED }

    /** What one evaluation knows once and the timers read: the input, the chain, the red tips and when each went red. */
    private record Ctx(Input in, List<PushChain.Node> chain, Set<String> redTips, Map<String, Instant> redAt) {
    }

    /** A sign of life: who acked or pushed, and when. */
    private record Sign(Instant at, String identity) {
        static Sign later(Sign a, Sign b) {
            return a == null ? b : b == null ? a : b.at().isAfter(a.at()) ? b : a;
        }
    }

    /**
     * The red goes to next up, or, with none (or when it has already been handed on once, or stalled), is escalated.
     * A disclaim hands on by the same path as a missed deadline and names its reason {@code disclaimed}; a stall skips
     * next up and escalates, with {@code stalled: true} in the body and who and since when ({@link #stallInfo}).
     */
    private static Decision handOn(Input in, BoardRow cur, Map<String, Object> curBody, List<PushChain.Node> suspects,
                                   Mode mode, Schedule due) {
        GroupView g = in.group();
        int handoffs = ((Number) curBody.get("handoffs")).intValue();
        String owner = (String) curBody.get("owner");
        // A stamper_error row shows no next up (Sam is the one after the leader), so it escalates at its deadline.
        String next = mode != Mode.STALLED && handoffs == 0 && !REASON_STAMPER_ERROR.equals(curBody.get("reason"))
            ? nextUp(owner, suspects, g, in.ids()) : null;
        if (next != null) {
            return new Decision(new Post("owner", cur.sha(),
                body(next, null, mode == Mode.DISCLAIMED ? REASON_DISCLAIMED : REASON_HANDOFF, shas(suspects),
                    suspects.size(), in.now(), false, handoffs + 1, null,
                    mode == Mode.DISCLAIMED ? Map.of("previous_owner", owner) : null)), null);
        }
        String target = in.ids().live(g.leader()) ? g.leader() : null;
        for (String m : g.members()) {
            if (target == null && in.ids().live(m)) {
                target = m;
            }
        }
        Map<String, Object> stall = mode == Mode.STALLED
            ? stallInfo(due == null ? null : due.sign(), due == null ? in.now() : due.redSince()) : null;
        return new Decision(new Post("owner", cur.sha(),
            body(target, null, REASON_ESCALATED, shas(suspects), suspects.size(), in.now(), false, handoffs + 1, stall)), null);
    }

    /**
     * The extra fields of a stall escalation: {@code red_since} (the red's first owner row), and when someone had acked
     * or pushed, {@code stalled_owner} (that identity) and {@code stalled_since} (when). {@code episode_at} keeps its
     * meaning, the start of the row's own deadline.
     */
    private static Map<String, Object> stallInfo(Sign sign, Instant redSince) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (sign != null) {
            m.put("stalled_owner", sign.identity());
            m.put("stalled_since", sign.at().toString());
        }
        m.put("red_since", redSince.toString());
        return m;
    }

    /** The half-deadline nudge: one {@code nudge} row on the tip, naming next up and the owner it stands by for. */
    private static Post nudge(BoardRow cur, Map<String, Object> curBody) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("owner", curBody.get("owner"));
        m.put("next", curBody.get("next"));
        m.put("episode_at", curBody.get("episode_at"));
        return new Post("nudge", cur.sha(), m);
    }

    /**
     * Whether the owner the open row {@code cur} names has disclaimed it: a {@code disclaim} row on any red tip of the
     * episode (the rule an ack follows, so it survives a refresh), posted at or after the row that began this owner's
     * ownership (the newest owner row that is not a refresh), whose sender session the session index maps to that
     * owner. A disclaim from anyone else, on another sha, or from before this ownership (an earlier owner's, or an
     * earlier episode's) is ignored, and so is one on a row that has no owner or is already {@code escalated}.
     */
    private static boolean disclaimed(Ctx c, BoardRow cur, Map<String, Object> curBody) {
        Object owner = curBody.get("owner");
        if (!(owner instanceof String ownerId) || REASON_ESCALATED.equals(curBody.get("reason"))) {
            return false;
        }
        Instant since = ownershipSince(c.in().owners(), cur);
        for (BoardRow a : c.in().owners()) {
            if ("disclaim".equals(a.kind()) && c.redTips().contains(a.sha()) && !a.createdAt().isBefore(since)
                && ownerId.equals(c.in().ids().identityOf(a.senderSession()))) {
                return true;
            }
        }
        return false;
    }

    /** When the current owner's ownership began: the newest owner row that is not a refresh. */
    private static Instant ownershipSince(List<BoardRow> owners, BoardRow cur) {
        Instant since = cur.createdAt();
        for (BoardRow r : owners.stream().filter(OwnerDecision::isState).sorted(BY_TIME).toList()) {
            if ("owner".equals(r.kind()) && !Boolean.TRUE.equals(parse(r.body()).get("refresh"))) {
                since = r.createdAt();
            }
        }
        return since;
    }

    /** The identity the current open owner row names, or null when the current row is not an open owner row. */
    static String currentOwner(List<BoardRow> owners) {
        BoardRow cur = currentRow(owners);
        return cur != null && "owner".equals(cur.kind()) && parse(cur.body()).get("owner") instanceof String o ? o : null;
    }

    /** The first owner row of the red now open: the first owner row after the latest clear row; null when none. */
    static Instant redStart(List<BoardRow> owners) {
        Instant start = null;
        for (BoardRow r : owners.stream().filter(OwnerDecision::isState).sorted(BY_TIME).toList()) {
            if ("clear".equals(r.kind())) {
                start = null;
            } else if (start == null) {
                start = r.createdAt();
            }
        }
        return start;
    }

    /** The owners the red has had since it began (since the latest clear row), in order, with {@code extra} too. */
    private static Set<String> episodeOwners(List<BoardRow> owners, String extra) {
        Set<String> out = new java.util.LinkedHashSet<>();
        for (BoardRow r : owners.stream().filter(OwnerDecision::isState).sorted(BY_TIME).toList()) {
            if ("clear".equals(r.kind())) {
                out.clear();
            } else if (parse(r.body()).get("owner") instanceof String o) {
                out.add(o);
            }
        }
        if (extra != null) {
            out.add(extra);
        }
        return out;
    }

    /** Fills in the deadline, and the pending nudge, of the state the posted row makes. */
    private static Decision withSchedule(Decision d, Ctx c) {
        Schedule due = schedule(c, parse(d.post().bodyJson()));
        return new Decision(d.post(), due.at(), due.nudgeAt());
    }

    /**
     * When the open row's deadline falls due ({@code at}; null: none runs), whether it is a stall, the nudge due, the
     * hard cap, and the last sign of life that put the deadline where it is ({@code sign}, null when there was none).
     */
    private record Schedule(Instant at, boolean stalled, Instant nudgeAt, Instant cap, Sign sign, Instant redSince) {
    }

    /**
     * The timers of the open row {@code body}. None for an {@code escalated} row or one with no owner. Three kinds of
     * sign of life move them, all read from rows the identity read maps to people, and each must be a push (or an ack)
     * by an identity that is or was an owner of this red, after the red's verdict:
     * <ul>
     *   <li>the current owner's own ack (an {@code ack} row on a red tip) or push: the red is acknowledged, and the
     *       stall deadline is the later of {@code episode_at} and that sign, plus {@code stall_factor} deadlines;</li>
     *   <li>a push by any other identity that is or was an owner of this red: it postpones the deadline, so the
     *       acknowledgement deadline counts from the later of {@code episode_at} and that push (S1a as it happened: the
     *       original owner's fix landed after the hand-off);</li>
     *   <li>none: the acknowledgement deadline, {@code episode_at} plus the topic's deadline, with a nudge to next up at
     *       half of it while the row names one and none was posted this episode.</li>
     * </ul>
     * A push counts only if its base chains from a red tip and it was made at or after the verdict of the first red tip
     * it chains from (a follow-up pushed before the red was known is not a response to it). Whatever the signs, the red
     * escalates once {@code 2 x stall_factor} deadlines have passed since its first owner row (the hard cap).
     */
    private static Schedule schedule(Ctx c, Map<String, Object> body) {
        Object ownerObj = body.get("owner");
        if (REASON_ESCALATED.equals(body.get("reason")) || !(ownerObj instanceof String owner)) {
            return new Schedule(null, false, null, null, null, null);
        }
        Input in = c.in();
        Instant episode = Instant.parse((String) body.get("episode_at"));
        long minutes = in.group().deadlineMinutes(in.base());
        int k = Math.max(1, in.group().stallFactor());
        Instant redSince = redStart(in.owners());
        if (redSince == null) {
            redSince = in.now();   // the row being posted starts the red
        }
        Instant cap = redSince.plus(Duration.ofMinutes(2L * k * minutes));
        List<Sign> pushes = pushSigns(c);
        Sign ack = null;
        for (BoardRow a : in.owners()) {
            if ("ack".equals(a.kind()) && c.redTips().contains(a.sha()) && owner.equals(in.ids().identityOf(a.senderSession()))) {
                ack = Sign.later(ack, new Sign(a.createdAt(), owner));
            }
        }
        Set<String> owners = episodeOwners(in.owners(), owner);
        Sign live = null;
        for (Sign p : pushes) {
            if (owners.contains(p.identity())) {
                live = Sign.later(live, p);
            }
            if (owner.equals(p.identity())) {
                ack = Sign.later(ack, p);
            }
        }
        Sign sign = Sign.later(ack, live);
        Instant base = sign != null && sign.at().isAfter(episode) ? sign.at() : episode;
        Instant at;
        boolean stalled;
        Instant nudgeAt = null;
        if (ack != null) {
            at = base.plus(Duration.ofMinutes(minutes * k));
            stalled = true;
        } else {
            at = base.plus(Duration.ofMinutes(minutes));
            stalled = false;
            if (body.get("next") instanceof String && !nudged(in, body)) {
                nudgeAt = base.plus(Duration.ofSeconds(minutes * 30));
            }
        }
        if (at.isAfter(cap)) {
            at = cap;
            stalled = true;
            nudgeAt = null;
        }
        return new Schedule(at, stalled, nudgeAt, cap, sign, redSince);
    }

    /**
     * The pushes that respond to the red: a stamped push whose base chains from a red tip and that was made at or after
     * the verdict of the first red tip it chains from, as who the session index says pushed it. Read from the chain, so
     * it survives the refresh a failed fix causes.
     */
    private static List<Sign> pushSigns(Ctx c) {
        Map<String, Instant> bound = new java.util.HashMap<>();
        c.redTips().forEach(t -> bound.put(t, c.redAt().getOrDefault(t, Instant.MAX)));
        List<Sign> out = new ArrayList<>();
        for (PushChain.Node n : c.chain()) {
            String base = n.push() == null ? null : PushChain.baseOf(n.push());
            Instant b = base == null ? null : bound.get(base);
            if (b == null) {
                continue;
            }
            bound.merge(n.sha(), b, (x, y) -> x.isBefore(y) ? x : y);
            String pusher = pusherOf(n, c.in().ids());
            if (pusher != null && !n.push().createdAt().isBefore(b)) {
                out.add(new Sign(n.push().createdAt(), pusher));
            }
        }
        return out;
    }

    /** Whether the engine already posted a nudge for this episode and this next up. */
    private static boolean nudged(Input in, Map<String, Object> body) {
        for (BoardRow r : in.owners()) {
            if ("nudge".equals(r.kind()) && "engine".equals(r.dim("from")) && r.senderSession() == null) {
                Map<String, Object> b = parse(r.body());
                if (java.util.Objects.equals(body.get("episode_at"), b.get("episode_at"))
                    && java.util.Objects.equals(body.get("next"), b.get("next"))) {
                    return true;
                }
            }
        }
        return false;
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
        return body(owner, next, reason, suspects, total, episodeAt, refresh, handoffs, null);
    }

    /**
     * As above; a non-null {@code stall} marks an escalation of a red that stalled: {@code "stalled": true} and the
     * fields of {@code stall} ({@link #stallInfo}).
     */
    static Map<String, Object> body(String owner, String next, String reason, List<String> suspects, int total,
                                    Instant episodeAt, boolean refresh, int handoffs, Map<String, Object> stall) {
        return body(owner, next, reason, suspects, total, episodeAt, refresh, handoffs, stall, null);
    }

    /** As above; the fields of {@code extra}, when given, are written last (a disclaim hand-off's {@code previous_owner}). */
    static Map<String, Object> body(String owner, String next, String reason, List<String> suspects, int total,
                                    Instant episodeAt, boolean refresh, int handoffs, Map<String, Object> stall,
                                    Map<String, Object> extra) {
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
            if (stall != null) {
                m.put("stalled", true);
                m.putAll(stall);
            }
            if (extra != null) {
                m.putAll(extra);
            }
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
