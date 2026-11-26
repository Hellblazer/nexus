// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * The one {@code statement_timeout_ms} that the re-check of the completion transaction ({@code VerifyingFinisher#recheck}) shares
 * across the table-wide run and every tenant's run, kept as DATABASE time spent, not as a wall-clock deadline.
 *
 * <p>A wall clock over the whole re-check also counted every round trip: the re-read of the catalogs, and for each tenant
 * {@code set_config}, {@code SET LOCAL statement_timeout} and the count, so the cost grew as {@code RTT x (6 + 3 x tenants)} and a
 * store with many tenants, or a database an RTT away, spent the budget on the network with every statement fast (nexus-p2m1x.21:
 * the 0.1.203 fork walk through an SSM forward, 78 ms RTT, budget spent at the 4th of 6 tenants, 57014 on every retry). What is
 * charged now is the time the database spends, less one round trip for each statement that was timed end to end:
 * <ul>
 *   <li>a run's execute and row read, less one round trip ({@link #chargeRun});</li>
 *   <li>the catalog re-read, less one round trip per statement it sent ({@link #chargeCatalogRead});</li>
 *   <li>nothing for a tenant's setup statements, which run no verification text.</li>
 * </ul>
 * The round trip is the floor: the smallest a trivial statement took on this session ({@link #observeRoundTrip}). It is a lower
 * bound, so a charge errs toward more than the database's own time by the jitter above the floor, never toward less than it; with no
 * round trip seen yet nothing is subtracted. The statement bound sent to the server for a run is what is left, so the database
 * stops a run at the same total whatever the network.
 *
 * <p>What this does NOT bound is the wall time the transaction holds the move's registry row: each tenant's run still costs about
 * four round trips (set the tenant, set the statement timeout, describe, execute) that are not charged. A move whose verification
 * reads only the catalog declares it ({@link MoveSql#tableWideOnly}) and the finisher then runs no per-tenant statement at all.
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

    /** What a trivial statement took end to end: the smallest of these is the round trip the charges are made less. */
    void observeRoundTrip(long nanos) {
        floorNs = Math.min(floorNs, Math.max(0L, nanos));
    }

    /** The round trip charges are made less: the smallest seen, or none until one is seen. */
    long floorNanos() {
        return floorNs == Long.MAX_VALUE ? 0L : floorNs;
    }

    /** One count's execute time, charged less one round trip. */
    void chargeRun(long executeNanos) {
        charge(executeNanos, 1);
    }

    /** The catalog re-read: {@code wallNanos} for {@code statements} statements, charged less one round trip for each. */
    void chargeCatalogRead(long wallNanos, int statements) {
        charge(wallNanos, statements);
    }

    private void charge(long nanos, int roundTrips) {
        spentNs += Math.max(0L, nanos - roundTrips * floorNanos());
    }

    /** Nanoseconds charged so far. */
    long spentNanos() {
        return spentNs;
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
