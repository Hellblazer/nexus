// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.time.Clock;
import java.time.Instant;
import java.util.Collection;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

/**
 * The worker's in-memory view of its open moves, fed on the admin connection and read by boot_work and the nx doctor
 * line. One entry per open move label; an entry leaves when its move is no longer open.
 */
public final class MoveStates implements MoveReporter {

    /** What the worker last did with a move. */
    public enum Kind { WORKING, BLOCKED, PAUSED, RUNS_COMPLETE }

    /**
     * {@code reason} is set for {@link Kind#BLOCKED} and null otherwise; {@code since} is when the kind and reason were
     * entered, and {@code updatedAt} the last time the worker reported the move, which is what a retry waits from.
     */
    public record State(String label, Kind kind, String reason, Instant since, Instant updatedAt) { }

    private final Clock clock;
    private final Map<String, State> byLabel = new ConcurrentHashMap<>();

    public MoveStates(Clock clock) {
        this.clock = Objects.requireNonNull(clock, "clock");
    }

    private void put(DataMove move, Kind kind, String reason) {
        State old = byLabel.get(move.label());
        // the clock moves only when the kind or the reason does: a move that stays blocked keeps the time it first was
        Instant now = clock.instant();
        Instant since = old != null && old.kind() == kind && Objects.equals(old.reason(), reason) ? old.since() : now;
        byLabel.put(move.label(), new State(move.label(), kind, reason, since, now));
    }

    @Override
    public void working(DataMove move) {
        put(move, Kind.WORKING, null);
    }

    @Override
    public void blocked(DataMove move, String reason) {
        put(move, Kind.BLOCKED, Objects.requireNonNull(reason, "reason"));
    }

    @Override
    public void paused(DataMove move) {
        put(move, Kind.PAUSED, null);
    }

    @Override
    public void runsComplete(DataMove move) {
        put(move, Kind.RUNS_COMPLETE, null);
    }

    /** The state of one move, if the worker has seen it. */
    public Optional<State> state(String label) {
        return Optional.ofNullable(byLabel.get(label));
    }

    /** Every state, in no particular order. */
    public Collection<State> snapshot() {
        return java.util.List.copyOf(byLabel.values());
    }

    /** Drops the states of moves that are no longer open. */
    void retainOnly(Collection<String> openLabels) {
        byLabel.keySet().removeIf(l -> !openLabels.contains(l));
    }
}
