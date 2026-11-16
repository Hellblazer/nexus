// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

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
     * worker reported the move, which is what a retry waits from.
     */
    public record State(String label, Kind kind, String reason, String pausedBy, Instant since, Instant updatedAt) { }

    private final Clock clock;
    private final Map<String, State> byLabel = new ConcurrentHashMap<>();

    public MoveStates(Clock clock) {
        this.clock = Objects.requireNonNull(clock, "clock");
    }

    private void put(String label, Kind kind, String reason, String pausedBy) {
        State old = byLabel.get(label);
        Instant now = clock.instant();
        // the time moves only when the kind or the reason does: a move that stays blocked keeps the time it first was
        Instant since = old != null && old.kind() == kind && Objects.equals(old.reason(), reason) ? old.since() : now;
        byLabel.put(label, new State(label, kind, reason, pausedBy, since, now));
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
    public void forget(String label) {
        byLabel.remove(label);
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
    }
}
