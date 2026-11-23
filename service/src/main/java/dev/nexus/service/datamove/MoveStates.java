// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.log.SecretMask;

import java.time.Clock;
import java.time.Instant;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

/**
 * The worker's in-memory view of its moves, fed on the admin connection and read by boot_work and the nx doctor line. One
 * entry per move label: an open move while it is open, and a failed move until a move supersedes it.
 */
public final class MoveStates implements MoveReporter {

    /** What the worker last did with a move. */
    public enum Kind { WORKING, BLOCKED, FAILED, PAUSED, RUNS_COMPLETE }

    /**
     * {@code reason} is set for {@link Kind#BLOCKED} and {@link Kind#FAILED} and null otherwise; {@code pausedBy} for
     * {@link Kind#PAUSED}. {@code since} is when the kind and reason were entered, and {@code updatedAt} the last time the
     * worker reported the move, which is what a retry waits from. {@code firstBlockedAt} is when the move was first
     * blocked in the run of blocked passes and retries it is in (a retry in flight is {@link Kind#WORKING}, so
     * {@code since} restarts with it); null until the move has been blocked, and cleared when it is paused, failed or its
     * runs complete. {@code needsOperator} is whether the last blocked reason said "needs an operator"
     * ({@link MoveRetryPolicy#blockedReason}: the retry policy's one rule), and {@code terminatesIndexBuilds} whether it also
     * said each retry ends in-flight index builds; both are kept while the move is retried ({@link Kind#WORKING}) and cleared
     * by a blocked reason without them, and by a failed, paused or runs-complete report.
     */
    public record State(String label, Kind kind, String reason, String pausedBy, Instant since, Instant updatedAt,
                        Instant firstBlockedAt, boolean needsOperator, boolean terminatesIndexBuilds) { }

    /** A CALL in flight for a move: the run, the server backend that holds it, and when it began. */
    public record CallInFlight(String runLabel, int pid, Instant startedAt) { }

    private final Clock clock;
    private final Map<String, State> byLabel = new ConcurrentHashMap<>();
    private final Map<String, CallInFlight> calls = new ConcurrentHashMap<>();

    public MoveStates(Clock clock) {
        this.clock = Objects.requireNonNull(clock, "clock");
    }

    private void put(String label, Kind kind, String maskable, String pausedBy) {
        String reason = SecretMask.mask(maskable);
        State old = byLabel.get(label);
        Instant now = clock.instant();
        // the time moves only when the kind or the reason does: a move that stays blocked keeps the time it first was
        Instant since = old != null && old.kind() == kind && Objects.equals(old.reason(), reason) ? old.since() : now;
        // blocked passes and the retries between them are one stretch: a retry that runs for hours is working, and the
        // move was still first blocked when it began
        Instant firstBlocked = switch (kind) {
            case BLOCKED -> old != null && old.firstBlockedAt() != null ? old.firstBlockedAt() : now;
            case WORKING -> old != null ? old.firstBlockedAt() : null;
            case FAILED, PAUSED, RUNS_COMPLETE -> null;
        };
        boolean needs = switch (kind) {
            case BLOCKED -> MoveReasons.needsOperator(reason);
            case WORKING -> old != null && old.needsOperator();
            case FAILED, PAUSED, RUNS_COMPLETE -> false;
        };
        boolean builds = switch (kind) {
            case BLOCKED -> MoveReasons.terminatesIndexBuilds(reason);
            case WORKING -> old != null && old.terminatesIndexBuilds();
            case FAILED, PAUSED, RUNS_COMPLETE -> false;
        };
        byLabel.put(label, new State(label, kind, reason, pausedBy, since, now, firstBlocked, needs, builds));
    }

    @Override
    public void working(DataMove move) {
        put(move.label(), Kind.WORKING, null, null);
    }

    @Override
    public void blocked(DataMove move, String reason) {
        put(move.label(), Kind.BLOCKED, Objects.requireNonNull(reason, "reason"), null);
    }

    @Override
    public void failed(DataMove move, String reason) {
        put(move.label(), Kind.FAILED, Objects.requireNonNull(reason, "reason"), null);
    }

    @Override
    public void paused(DataMove move) {
        put(move.label(), Kind.PAUSED, null, move.pausedBy());
    }

    @Override
    public void runsComplete(DataMove move) {
        put(move.label(), Kind.RUNS_COMPLETE, null, null);
    }

    @Override
    public void callStarted(DataMove move, String runLabel, int pid) {
        calls.put(move.label(), new CallInFlight(runLabel, pid, clock.instant()));
    }

    @Override
    public void callEnded(DataMove move) {
        calls.remove(move.label());
    }

    @Override
    public void forget(String label) {
        byLabel.remove(label);
        calls.remove(label);
    }

    /** The CALL now in flight for the move, if any. */
    public Optional<CallInFlight> call(String label) {
        return Optional.ofNullable(calls.get(label));
    }

    /** The state of one move, if the worker has seen it. */
    public Optional<State> state(String label) {
        return Optional.ofNullable(byLabel.get(label));
    }

    /** Every state, in no particular order. */
    public Collection<State> snapshot() {
        return List.copyOf(byLabel.values());
    }

    /**
     * Drops the state of every move that is not open, except a failed one, which stays until a move supersedes it
     * ({@code supersededLabels}).
     */
    void retainOnly(Collection<String> openLabels, Collection<String> supersededLabels) {
        byLabel.entrySet().removeIf(e -> {
            if (supersededLabels.contains(e.getKey())) {
                return true;
            }
            return e.getValue().kind() != Kind.FAILED && !openLabels.contains(e.getKey());
        });
        calls.keySet().removeIf(label -> !byLabel.containsKey(label));
    }
}
