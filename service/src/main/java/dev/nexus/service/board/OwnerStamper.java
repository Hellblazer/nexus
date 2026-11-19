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

    private final TenantScope scope;
    private final TupleRepository tuples;
    private final GroupRepository groups;
    private final IdentityRepository identities;
    private final Clock clock;
    private volatile AckDeadlineTimer timer;
    /** The decision; replaced in a test to make an evaluation fail. */
    private final BiFunction<OwnerDecision.Input, OwnerDecision.Trigger, OwnerDecision.Decision> decider;

    public OwnerStamper(TenantScope scope, TupleRepository tuples, GroupRepository groups, IdentityRepository identities,
                        Clock clock) {
        this(scope, tuples, groups, identities, clock, OwnerDecision::decide);
    }

    OwnerStamper(TenantScope scope, TupleRepository tuples, GroupRepository groups, IdentityRepository identities,
                 Clock clock, BiFunction<OwnerDecision.Input, OwnerDecision.Trigger, OwnerDecision.Decision> decider) {
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
            String base = doneBase(subspace);
            if (base == null) {
                return;
            }
            DoneVerdict.Verdict v = DoneVerdict.of(new BoardRow(null, clock.instant(), dims, body, null))
                .filter(DoneVerdict.Verdict::counts).orElse(null);
            if (v == null) {
                return;   // not kind=sha, or cancelled, or an outcome that cannot be told: not a verdict
            }
            try {
                evaluate(tenant, base, OwnerDecision.Trigger.DONE);
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

    /** The base topic of {@code board/ci/<base>--done}, or null for any other subspace. */
    static String doneBase(String subspace) {
        if (subspace == null || !subspace.startsWith(PREFIX) || !subspace.endsWith(DONE)) {
            return null;
        }
        String base = subspace.substring(PREFIX.length(), subspace.length() - DONE.length());
        return base.isEmpty() || base.contains("--") ? null : base;
    }

    /** The timer's hand-off: re-decide from fresh rows, and post a hand-off or the escalation when it is due. */
    public void onDeadline(String tenant, String base) {
        evaluate(tenant, base, OwnerDecision.Trigger.DEADLINE);
    }

    /** Re-evaluates a topic and arms its deadline (the rebuild at engine start); posts only what is owed. */
    public void rearm(String tenant, String base) {
        evaluate(tenant, base, OwnerDecision.Trigger.DONE);
    }

    // ── one evaluation ───────────────────────────────────────────────────────

    private void evaluate(String tenant, String base, OwnerDecision.Trigger trigger) {
        scope.withTenant(tenant, ctx -> {
            BoardTopicLock.lock(ctx, tenant, base);
            List<BoardRow> pushes = BoardReads.rows(ctx, tenant, base, "push");
            List<BoardRow> dones = BoardReads.rows(ctx, tenant, base, "done");
            List<BoardRow> owners = BoardReads.rows(ctx, tenant, base, "owner");
            GroupView group = BoardReads.group(groups, identities, tenant, base);
            Identities ids = group == null && owners.isEmpty()
                ? new Identities(Map.of(), Map.of())
                : BoardReads.identities(identities, tenant, pushes, owners, group);
            Instant now = clock.instant();
            OwnerDecision.Decision decision =
                decider.apply(new OwnerDecision.Input(base, pushes, dones, owners, group, ids, now), trigger);
            if (decision.post() != null) {
                post(tenant, base, decision.post());
            }
            // Armed under the lock, so an older evaluation cannot overwrite a newer one's deadline.
            if (timer != null) {
                timer.arm(tenant, base, decision.deadline());
            }
            return decision;
        });
    }

    private void postStamperError(String tenant, String base, String sha) {
        try {
            scope.withTenant(tenant, ctx -> {
                BoardTopicLock.lock(ctx, tenant, base);
                GroupView group = BoardReads.group(groups, identities, tenant, base);
                OwnerDecision.Decision d = OwnerDecision.stamperError(group, sha,
                    BoardReads.rows(ctx, tenant, base, "owner"), clock.instant());
                if (d.post() != null) {
                    post(tenant, base, d.post());
                }
                return null;
            });
        } catch (Throwable t) {
            log.warn("event=board_owner_stamper_error_row_failed tenant={} base={} error={}", tenant, base, t.getMessage(), t);
        }
    }

    private void post(String tenant, String base, OwnerDecision.Post p) {
        String subspace = BoardReads.subspace(base, "owner");
        tuples.out(tenant, subspace, Map.of("topic", base + "--owner"),
            Map.of("from", "engine", "kind", p.kind(), "sha", p.sha()), p.bodyJson(), UUID.randomUUID().toString(), null, null);
        log.info("event=board_owner_row tenant={} base={} kind={} sha={}", tenant, base, p.kind(), p.sha());
    }
}
