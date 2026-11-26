// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * The one {@code statement_timeout_ms} that the re-check of the completion transaction ({@code VerifyingFinisher#recheck}) shares
 * across the table-wide run and every tenant's run, kept as time SPENT, not as a wall-clock deadline.
 *
 * <p>The budget bounds how long the transaction holds the move's registry row and its snapshot, and what a run costs against it is
 * the time the database spends on the run. A wall clock over the whole re-check also counted every round trip: the re-read of the
 * catalogs, and for each tenant {@code set_config}, {@code SET LOCAL statement_timeout} and the count, so the cost grew as
 * {@code RTT x (6 + 3 x tenants)} and a store with many tenants, or a database an RTT away, spent the budget on the network with
 * every statement fast (nexus-p2m1x.21: the 0.1.203 fork walk through an SSM forward, 78 ms RTT, budget spent at the 4th of 6
 * tenants, 57014 on every retry). So only two things are charged: the fixed work before the runs (the catalog re-read, a constant
 * number of statements whatever the tenant count), and each run's execute time less one round trip, the smallest a trivial
 * statement was seen to take on this session. The setup round trips of a tenant are not charged. The statement bound sent to the
 * database for a run is what is left, so the server cuts a run at the same total either way.
 *
 * <p>Not thread-safe: one re-check, one session.
 */
final class RecheckBudget {

    private static final long NANOS_PER_MS = 1_000_000L;

    private final long totalNs;
    private final boolean unbounded;
    private long spentNs;
    private long floorNs = Long.MAX_VALUE;

    private RecheckBudget(long totalNs, boolean unbounded) {
        this.totalNs = totalNs;
        this.unbounded = unbounded;
    }

    static RecheckBudget of(int statementTimeoutMs) {
        return new RecheckBudget(statementTimeoutMs * NANOS_PER_MS, false);
    }

    static RecheckBudget unbounded() {
        return new RecheckBudget(0L, true);
    }

    boolean isUnbounded() {
        return unbounded;
    }

    /** What a trivial statement took end to end: the smallest of these is the round trip the runs are charged less. */
    void observeRoundTrip(long nanos) {
        floorNs = Math.min(floorNs, Math.max(0L, nanos));
    }

    /** Work charged in full (the fixed catalog reads before the runs). */
    void chargeWall(long nanos) {
        spentNs += Math.max(0L, nanos);
    }

    /** One count's execute time, charged less one round trip (nothing if no round trip has been seen yet). */
    void chargeRun(long executeNanos) {
        spentNs += Math.max(0L, executeNanos - (floorNs == Long.MAX_VALUE ? 0L : floorNs));
    }

    /** Milliseconds left, rounded down; negative once spent. {@code Long.MAX_VALUE} when unbounded. */
    long leftMs() {
        return unbounded ? Long.MAX_VALUE : Math.floorDiv(totalNs - spentNs, NANOS_PER_MS);
    }

    /** The {@code statement_timeout} for what follows, in ms as text: {@code 0} (none) when unbounded, else what is left, at least 1. */
    String statementTimeoutSetting() {
        return unbounded ? "0" : Long.toString(Math.max(1L, leftMs()));
    }
}
