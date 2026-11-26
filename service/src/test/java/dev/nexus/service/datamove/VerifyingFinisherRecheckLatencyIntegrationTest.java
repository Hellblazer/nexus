// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;

/**
 * nexus-p2m1x.21 residual (linda, 2026-11-24, bob's 0.1.203 fork walk): the completion transaction's re-check shares one
 * {@code statement_timeout_ms} (RDR-246 split bounds) so the lock it holds has a bound, and that budget must be SERVER time. It
 * was a Java wall clock over every round trip of the re-check (the catalog re-read, the table-wide run, and {@code set_config},
 * {@code SET LOCAL statement_timeout} and the count of every tenant), so a database {@code RTT} away spent it as
 * {@code RTT x (6 + 3 x tenants)} and a store with enough tenants timed out (57014, stage RECHECK, retried for ever) though
 * every statement was fast. Here the database is behind {@link LatencyProxy}: no statement is slow, only the network.
 */
class VerifyingFinisherRecheckLatencyIntegrationTest extends DataMoveIntegrationBase {

    /** One way, so each round trip costs about 30 ms. */
    private static final long ONE_WAY_MS = 15;

    private static final int TENANTS = 20;

    /** What the shipped swaps carry (Rdr229SwapMove.STATEMENT_TIMEOUT_MS): the bound this fix may not change. */
    private static final int BUDGET_MS = 1_400;

    /** {@code completionMs}: from the start of the re-check to the end of the finish (the commit). */
    private record Timed(MoveFinisher.Result result, long completionMs) { }

    /** The finish of {@code label} with the finisher's session and its first-pass connection both behind the latency proxy. */
    private Timed finishBehindLatency(String label, List<String> tenants) throws Exception {
        DataMove move = DataMoveRegistry.reload(ctx(), label).orElseThrow();
        AtomicLong began = new AtomicLong();
        var finisher = new VerifyingFinisher();
        finisher.recheckHook = tx -> began.set(System.nanoTime());
        try (var proxy = new LatencyProxy(pg.getHost(), pg.getMappedPort(5432), ONE_WAY_MS)) {
            var slow = new AdminConnection("jdbc:postgresql://127.0.0.1:" + proxy.port() + "/" + pg.getDatabaseName(),
                ADMIN, ADMIN_PASSWORD);
            try (var session = AdminDdlSession.open(slow,
                new AdminDdlSession.Settings(WORKER_NAME + "-probe", 0L, "test", "dm_test", move.drivingTable(), "none"), false)) {
                var result = finisher.finish(
                    new MoveFinisher.Context(move, tenants, session.ctx(), slow, WORKER_NAME, () -> false));
                return new Timed(result, began.get() == 0 ? -1 : (System.nanoTime() - began.get()) / 1_000_000L);
            }
        }
    }

    private static List<String> tenants(int n) {
        var out = new ArrayList<String>();
        for (int i = 0; i < n; i++) {
            out.add(String.format("lt%02d", i));
        }
        return out;
    }

    @Test
    void theRecheckOfManyTenants_onADatabaseAnRttAway_spendsNoBudgetOnRoundTrips() throws Exception {
        DataMoveTestFixtures.register(ctx(), "lat-many", "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-tenant-plain.sql", BUDGET_MS, null);
        runsOnly("lat-many");

        var timed = finishBehindLatency("lat-many", tenants(TENANTS));

        assertThat(timed.result().outcome()).as("every statement is fast: the budget is server time, not round trips: %s",
            timed.result().reason()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(timed.completionMs()).as("non-vacuity: from the start of the re-check to the commit, the round trips alone took "
            + "longer than the whole budget").isGreaterThan(BUDGET_MS);
        assertThat(completedAt("lat-many")).isNotNull();
    }

    @Test
    void theRechecksBudget_isStillServerTime_onADatabaseAnRttAway() throws Exception {
        // control: the same latency, and the runs' own time (0.4 s each, four of them) is over the budget: the shared bound still holds
        DataMoveTestFixtures.register(ctx(), "lat-slow", "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-slow-run.sql", 1_000, null);
        DataMoveTestFixtures.setVerifyStatementTimeout(ctx(), "lat-slow", 10_000);
        runsOnly("lat-slow");

        var timed = finishBehindLatency("lat-slow", List.of("lt00", "lt01", "lt02"));

        assertThat(timed.result().outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(timed.result().failure().stage()).isEqualTo(FailureHandler.Stage.RECHECK);
        assertThat(timed.result().failure().raised().getSQLState()).as("query_canceled: the shared budget ran out").isEqualTo("57014");
        assertThat(completedAt("lat-slow")).isNull();
    }
}
