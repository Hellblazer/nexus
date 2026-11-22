// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import dev.nexus.service.db.BoardTopicLock;
import dev.nexus.service.db.GroupRepository;
import dev.nexus.service.db.IdentityRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TupleRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.BiFunction;

/**
 * RDR-252 Step 3: the write-path hook of the owner stamper and the deadline timer's hand-off. Each evaluation takes
 * the per-topic advisory lock ({@link BoardTopicLock}, key (tenant, base topic), the key the group writes take),
 * re-reads the push, verdict and owner rows under it, hands them to {@link OwnerDecision}, posts at most one row to
 * {@code board/ci/<base>--owner} as the engine, and arms the deadline the decision names. A hand-off and a
 * {@code --done} write for one topic are therefore serialized, and neither decides from state the other changed.
 *
 * <p>The post goes through {@link TupleRepository#out} in its own transaction and commits before the lock is
 * released, so the next holder reads it. {@link #onDoneWrite} never throws and runs after the adapter's response has
 * gone out, so a stamper fault cannot fail or slow the write; an evaluation that fails posts the leader with
 * {@code reason=stamper_error} when the write was a red.
 */
public final class OwnerStamper implements OwnerOutHook {

    private static final Logger log = LoggerFactory.getLogger(OwnerStamper.class);
    private static final String PREFIX = "board/ci/";
    private static final String DONE = "--done";
    private static final String OWNER = "--owner";

    private final TenantScope scope;
    private final TupleRepository tuples;
    private final GroupRepository groups;
    private final IdentityRepository identities;
    private final Clock clock;
    private volatile AckDeadlineTimer timer;
    private final AliasReader aliases;
    private final LivenessReader liveness;
    /** The decision; replaced in a test to make an evaluation fail. */
    private final BiFunction<OwnerDecision.Input, OwnerDecision.Trigger, OwnerDecision.Decision> decider;

    public OwnerStamper(TenantScope scope, TupleRepository tuples, GroupRepository groups, IdentityRepository identities,
                        Clock clock) {
        this(scope, tuples, groups, identities, clock, OwnerDecision::decide);
    }

    OwnerStamper(TenantScope scope, TupleRepository tuples, GroupRepository groups, IdentityRepository identities,
                 Clock clock, BiFunction<OwnerDecision.Input, OwnerDecision.Trigger, OwnerDecision.Decision> decider) {
        this(scope, tuples, groups, identities, clock, decider, IdentityRepository::liveAliases);
    }

    OwnerStamper(TenantScope scope, TupleRepository tuples, GroupRepository groups, IdentityRepository identities,
                 Clock clock, BiFunction<OwnerDecision.Input, OwnerDecision.Trigger, OwnerDecision.Decision> decider,
                 AliasReader aliases) {
        this(scope, tuples, groups, identities, clock, decider, aliases, BoardReads::identities);
    }

    /** The fail-safe's liveness read; replaced in a test to make it fail. */
    @FunctionalInterface
    interface LivenessReader {
        Identities read(IdentityRepository identities, String tenant, List<BoardRow> pushes, List<BoardRow> owners,
                        GroupView group);
    }

    /** The live alias of each identity, read in the evaluation's transaction; replaced in a test to make the read fail. */
    @FunctionalInterface
    interface AliasReader {
        Map<String, String> read(org.jooq.DSLContext ctx, String tenant, java.util.Collection<String> ids);
    }

    OwnerStamper(TenantScope scope, TupleRepository tuples, GroupRepository groups, IdentityRepository identities,
                 Clock clock, BiFunction<OwnerDecision.Input, OwnerDecision.Trigger, OwnerDecision.Decision> decider,
                 AliasReader aliases, LivenessReader liveness) {
        this.aliases = aliases;
        this.liveness = liveness;
        this.scope = scope;
        this.tuples = tuples;
        this.groups = groups;
        this.identities = identities;
        this.clock = clock;
        this.decider = decider;
    }

    /** Binds the deadline timer, whose hand-off is {@link #onDeadline}; called once before the first write. */
    public void bind(AckDeadlineTimer timer) {
        this.timer = timer;
    }

    // ── the hook ─────────────────────────────────────────────────────────────

    @Override
    public void afterOut(String tenant, String subspace, Map<String, String> dims, String body) {
        try {
            String ownerBase = ownerBase(subspace);
            if (ownerBase != null) {
                if ("disclaim".equals(dims.get("kind"))) {
                    onDisclaim(tenant, ownerBase);
                }
                return;
            }
            String base = doneBase(subspace);
            if (base == null) {
                return;
            }
            DoneVerdict.Resolution r = DoneVerdict.resolve(new BoardRow(null, clock.instant(), dims, body, null));
            DoneVerdict.Verdict v = r.verdict().filter(DoneVerdict.Verdict::counts).orElse(null);
            if (v == null) {
                // not kind=sha, or cancelled, or an outcome that cannot be told: not a verdict. A sha row that leaves
                // no verdict says why, since a suppressed clear after a failed carry read is otherwise silent.
                if ("sha".equals(dims.get("kind"))) {
                    log.info("event=board_done_no_verdict tenant={} base={} sha={} reason={}",
                        tenant, base, dims.get("sha"), r.reason());
                }
                return;
            }
            try {
                evaluate(tenant, base, OwnerDecision.Trigger.DONE, v);
            } catch (Throwable t) {
                log.warn("event=board_owner_stamper_failed tenant={} base={} error={}", tenant, base, t.getMessage(), t);
                if (DoneVerdict.RED.equals(v.outcome())) {
                    postStamperError(tenant, base, v.sha());
                }
            }
        } catch (Throwable t) {
            log.warn("event=board_owner_stamper_hook_failed error={}", t.getMessage(), t);
        }
    }

    /**
     * A {@code disclaim} row on the owner subspace (RDR-252 audit S1b): re-evaluates the topic at once, so the owner's
     * "not mine" is handed on without waiting for the deadline. The decision, not this hook, judges whose disclaim it
     * is. A failed evaluation is logged and left to the deadline, which hands the red on anyway.
     */
    private void onDisclaim(String tenant, String base) {
        try {
            if (!disclaimerIsOwner(tenant, base)) {
                log.info("event=board_owner_disclaim tenant={} base={} outcome=dropped_not_owner", tenant, base);
                return;
            }
            evaluate(tenant, base, OwnerDecision.Trigger.DONE, null);
            log.info("event=board_owner_disclaim tenant={} base={} outcome=evaluated", tenant, base);
        } catch (Throwable t) {
            log.warn("event=board_owner_disclaim tenant={} base={} outcome=failed error={}", tenant, base, t.getMessage(), t);
        }
    }

    /**
     * Whether the newest {@code disclaim} row of the topic was sent by the identity the current owner row names. Read
     * outside the topic lock, so a client that loops on disclaims it has no right to make costs two reads and never the
     * lock the {@code --done} writes and the deadline need. The decision still judges the disclaim under the lock.
     */
    private boolean disclaimerIsOwner(String tenant, String base) {
        List<BoardRow> owners = scope.withTenant(tenant, ctx -> BoardReads.rows(ctx, tenant, base, "owner"));
        String owner = OwnerDecision.currentOwner(owners);
        if (owner == null) {
            return false;
        }
        for (int i = owners.size() - 1; i >= 0; i--) {
            BoardRow r = owners.get(i);
            if ("disclaim".equals(r.kind())) {
                return owner.equals(Identities.countedIdentity(identities, tenant, r.senderSession()));
            }
        }
        return false;
    }

    /** The base topic of {@code board/ci/<base>--owner}, or null for any other subspace. */
    static String ownerBase(String subspace) {
        if (subspace == null || !subspace.startsWith(PREFIX) || !subspace.endsWith(OWNER)) {
            return null;
        }
        String base = subspace.substring(PREFIX.length(), subspace.length() - OWNER.length());
        return base.isEmpty() || base.contains("--") ? null : base;
    }

    /** The base topic of {@code board/ci/<base>--done}, or null for any other subspace. */
    static String doneBase(String subspace) {
        if (subspace == null || !subspace.startsWith(PREFIX) || !subspace.endsWith(DONE)) {
            return null;
        }
        String base = subspace.substring(PREFIX.length(), subspace.length() - DONE.length());
        return base.isEmpty() || base.contains("--") ? null : base;
    }

    /**
     * The timer's hand-off: re-decide from fresh rows, and post a hand-off or the escalation when it is due. If the
     * evaluation throws, the fail-safe still escalates a due row from the current owner row alone, so a deterministic
     * fault in the decision cannot leave a red with no hand-on. A fail-safe that cannot run throws, with the
     * evaluation's fault kept as suppressed on what it throws, and the timer tries again after its retry delay.
     */
    public void onDeadline(String tenant, String base) {
        try {
            evaluate(tenant, base, OwnerDecision.Trigger.DEADLINE);
        } catch (Throwable t) {
            try {
                failSafe(tenant, base);
            } catch (Throwable f) {
                f.addSuppressed(t);
                throw f;
            }
            log.warn("event=board_owner_deadline_evaluation_failed tenant={} base={} error={}; the fail-safe ran",
                tenant, base, t.getMessage(), t);
        }
    }

    /**
     * Re-evaluates a topic and arms its deadline (the rebuild at engine start); posts only what is owed. If the
     * evaluation throws, the deadline is armed from the current owner row alone ({@code episode_at} plus the topic's
     * deadline); with no open owner row to arm from, the failure is rethrown for the census to retry.
     */
    public void rearm(String tenant, String base) {
        try {
            evaluate(tenant, base, OwnerDecision.Trigger.DONE);
        } catch (Throwable t) {
            log.warn("event=board_owner_rearm_evaluation_failed tenant={} base={} error={}; arming from the owner row",
                tenant, base, t.getMessage(), t);
            if (!armFromRow(tenant, base)) {
                throw t instanceof RuntimeException r ? r : new IllegalStateException(t);
            }
        }
    }

    /** Arms the topic's deadline from its current owner row alone; false when no open owner row names one. */
    private boolean armFromRow(String tenant, String base) {
        return scope.withTenant(tenant, ctx -> {
            BoardTopicLock.lock(ctx, tenant, base);
            Instant due = OwnerDecision.rowDeadline(BoardReads.group(groups, identities, tenant, base), base,
                BoardReads.rows(ctx, tenant, base, "owner"));
            if (due == null) {
                return false;
            }
            if (timer != null) {
                timer.arm(tenant, base, due);
            }
            return true;
        });
    }

    /**
     * The row {@link OwnerDecision#failSafe} names, posted and armed under the topic lock. Who is live, and whose ack
     * an ack row is, come from the identity read, which is made only when an escalation is owed. If that read fails and
     * the owner's stamped ack sits on the row's own tip ({@link OwnerDecision#ackOnTip}), the red may be acknowledged and cannot be matched without the read, so this
     * throws and the timer retries; escalating then would wake every member over an acknowledged red. If no such ack
     * row exists, nobody acknowledged, so the escalation is posted at once with no one named, the row the RDR gives
     * for nobody live.
     */
    private void failSafe(String tenant, String base) {
        scope.withTenant(tenant, ctx -> {
            BoardTopicLock.lock(ctx, tenant, base);
            List<BoardRow> owners = BoardReads.rows(ctx, tenant, base, "owner");
            GroupView group = BoardReads.group(groups, identities, tenant, base);
            Instant now = clock.instant();
            // The deadline from the rows alone: the acknowledgement deadline, or, for a red the owner's stamped ack
            // holds, the stall deadline. The identity read is made only once it is due.
            Instant due = OwnerDecision.failSafeDue(group, null, base, owners);
            Identities ids = new Identities(Map.of(), Map.of());
            if (due != null && !now.isBefore(due)) {
                try {
                    ids = liveness.read(identities, tenant, List.of(), owners, group);
                } catch (Throwable t) {
                    log.warn("event=board_owner_failsafe_liveness_failed tenant={} base={} error={}; escalating with no owner",
                        tenant, base, t.getMessage());
                }
            }
            OwnerDecision.Decision d = OwnerDecision.failSafe(group, ids, owners, base, now);
            if (d.post() != null) {
                post(ctx, tenant, base, d.post());
                log.warn("event=board_owner_failsafe_row tenant={} base={} sha={}", tenant, base, d.post().sha());
            }
            if (timer != null) {
                timer.arm(tenant, base, d.deadline());
            }
            return null;
        });
    }

    // ── one evaluation ───────────────────────────────────────────────────────

    private void evaluate(String tenant, String base, OwnerDecision.Trigger trigger) {
        evaluate(tenant, base, trigger, null);
    }

    /** {@code done}: the verdict of the {@code --done} write that triggered this evaluation, or null. */
    private void evaluate(String tenant, String base, OwnerDecision.Trigger trigger, DoneVerdict.Verdict done) {
        scope.withTenant(tenant, ctx -> {
            BoardTopicLock.lock(ctx, tenant, base);
            List<BoardRow> pushes = BoardReads.rows(ctx, tenant, base, "push");
            List<BoardRow> dones = BoardReads.rows(ctx, tenant, base, "done");
            if (done != null && DoneVerdict.RED.equals(done.outcome()) && pushes.stream().noneMatch(p -> done.sha().equals(p.sha()))
                && dones.stream().filter(d -> DoneVerdict.of(d).filter(v -> v.counts() && done.sha().equals(v.sha())).isPresent())
                    .count() == 1) {
                // RDR-252 audit S3: a red tip with no push record has no pusher, so an earliest suspect among them hands
                // the red to the leader as no_record. The push script's failure is silent; this line is the durable trace
                // (the group read's unrecorded_tips only counts what the live rows still hold). Logged once per sha, on
                // the first --done row it has: a rerun's row, and a tip that went green, add no line.
                log.warn("event=board_done_tip_unrecorded tenant={} base={} sha={} unrecorded_tips={}", tenant, base, done.sha(),
                    UnrecordedTips.missing(pushes, dones, clock.instant()).size());
            }
            List<BoardRow> owners = BoardReads.rows(ctx, tenant, base, "owner");
            GroupView group = BoardReads.group(groups, identities, tenant, base);
            Identities ids = group == null && owners.isEmpty()
                ? new Identities(Map.of(), Map.of())
                : BoardReads.identities(identities, tenant, pushes, owners, group);
            Instant now = clock.instant();
            OwnerDecision.Decision decision =
                decider.apply(new OwnerDecision.Input(base, pushes, dones, owners, group, ids, now), trigger);
            if (decision.post() != null) {
                post(ctx, tenant, base, decision.post());
            }
            // Armed under the lock, so an older evaluation cannot overwrite a newer one's deadline.
            if (timer != null) {
                timer.arm(tenant, base, decision.wakeAt());
            }
            return decision;
        });
    }

    private void postStamperError(String tenant, String base, String sha) {
        try {
            scope.withTenant(tenant, ctx -> {
                BoardTopicLock.lock(ctx, tenant, base);
                GroupView group = BoardReads.group(groups, identities, tenant, base);
                OwnerDecision.Decision d = OwnerDecision.stamperError(group, base, sha,
                    BoardReads.rows(ctx, tenant, base, "owner"), clock.instant());
                if (d.post() != null) {
                    post(ctx, tenant, base, d.post());
                    // Armed under the lock, as in evaluate: the row is an owner row and runs the deadline like any other.
                    if (timer != null) {
                        timer.arm(tenant, base, d.deadline());
                    }
                }
                return null;
            });
        } catch (Throwable t) {
            log.warn("event=board_owner_stamper_error_row_failed tenant={} base={} error={}", tenant, base, t.getMessage(), t);
        }
    }

    /**
     * RDR-252, Sam 2026-11-19: the body names the owner and next up by identity id, and this adds each one's live alias
     * as {@code owner_alias} and {@code next_alias}, read at stamp time through the evaluation's own transaction, so a notice
     * can say a seat's name. An identity with no live alias gets no field, and a read that fails gets none either: the
     * row still posts, with ids. The decision stays a function of ids; a {@code clear} row names no one.
     */
    private OwnerDecision.Post withAliases(org.jooq.DSLContext ctx, String tenant, OwnerDecision.Post p) {
        Object owner = p.body().get("owner");
        Object next = p.body().get("next");
        List<String> named = java.util.stream.Stream.of(owner, next).filter(String.class::isInstance)
            .map(String.class::cast).distinct().toList();
        if (named.isEmpty()) {
            return p;
        }
        Map<String, String> alias;
        try {
            alias = aliases.read(ctx, tenant, named);
        } catch (Throwable t) {
            // The alias is a cosmetic field: the row goes out with ids alone, as an older engine's did.
            log.warn("event=board_owner_alias_read_failed tenant={} error={}", tenant, t.getMessage(), t);
            return p;
        }
        return OwnerDecision.withAliases(p, owner instanceof String o ? alias.get(o) : null,
            next instanceof String n ? alias.get(n) : null);
    }

    private void post(org.jooq.DSLContext ctx, String tenant, String base, OwnerDecision.Post decided) {
        OwnerDecision.Post p = withAliases(ctx, tenant, decided);
        String subspace = BoardReads.subspace(base, "owner");
        tuples.out(tenant, subspace, Map.of("topic", base + "--owner"),
            Map.of("from", "engine", "kind", p.kind(), "sha", p.sha()), p.bodyJson(), UUID.randomUUID().toString(), null, null);
        log.info("event=board_owner_row tenant={} base={} kind={} sha={}", tenant, base, p.kind(), p.sha());
    }
}
