// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import org.jooq.ExecuteContext;
import org.jooq.impl.DefaultExecuteListener;
import org.jooq.impl.DefaultExecuteListenerProvider;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * nexus-p2m1x.21 residual (linda, 2026-11-24, bob's 0.1.203 fork walk): the completion transaction's re-check shares one
 * {@code statement_timeout_ms} (RDR-246 split bounds), and that budget is DATABASE time: it was a Java wall clock over every round
 * trip of the re-check (the catalog re-read, the table-wide run, and {@code set_config}, {@code SET LOCAL statement_timeout} and the
 * count of every tenant), so a database an RTT away spent it as {@code RTT x (6 + 3 x tenants)} and a store with enough tenants
 * timed out (57014, stage RECHECK, retried for ever) though every statement was fast. Here the database is behind
 * {@link LatencyProxy}: no statement is slow, only the network.
 *
 * <p>Sized so that the round trips alone are far over the budget: (tenants + 1) x RTT is {@code 31 x 60 ms}, 1.9 s, over 1.4 s, so a
 * budget that charged each run its whole time (the floor of one round trip not taken off) fails the first test, as does one that
 * charges the tenants' setup trips. Two moves are run: one whose verification is read per tenant, whose re-check still makes about
 * four round trips a tenant in wall time and is bounded by database time only; and one that declares itself table-wide-only
 * ({@link DataMove#tableWideOnly}, the swaps' shape), whose re-check makes the same few round trips whatever the tenant count.
 */
class VerifyingFinisherRecheckLatencyIntegrationTest extends DataMoveIntegrationBase {

    /** One way, so each round trip costs about 60 ms. */
    private static final long ONE_WAY_MS = 30;

    private static final int TENANTS = 30;

    /** What the shipped swaps carry (Rdr229SwapMove.STATEMENT_TIMEOUT_MS): the bound this fix may not change. */
    private static final int BUDGET_MS = 1_400;

    /**
     * What a finish measured from the start of the re-check to its end (the commit): {@code holdMs} is how long the transaction held
     * the registry row's lock in wall time, {@code chunks} how many messages the client sent the database over that time.
     */
    private record Timed(MoveFinisher.Result result, long holdMs, int chunks) { }

    /** The finish of {@code label} with the finisher's session and its first-pass connection both behind the latency proxy. */
    private Timed finishBehindLatency(String label, List<String> tenants) throws Exception {
        DataMove move = DataMoveRegistry.reload(ctx(), label).orElseThrow();
        AtomicLong began = new AtomicLong();
        AtomicInteger chunksAtStart = new AtomicInteger();
        try (var proxy = new LatencyProxy(pg.getHost(), pg.getMappedPort(5432), ONE_WAY_MS)) {
            var finisher = new VerifyingFinisher();
            finisher.recheckHook = tx -> {
                began.set(System.nanoTime());
                chunksAtStart.set(proxy.clientChunks());
            };
            var slow = new AdminConnection("jdbc:postgresql://127.0.0.1:" + proxy.port() + "/" + pg.getDatabaseName(),
                ADMIN, ADMIN_PASSWORD);
            try (var session = AdminDdlSession.open(slow,
                new AdminDdlSession.Settings(WORKER_NAME + "-probe", 0L, "test", "dm_test", move.drivingTable(), "none"), false)) {
                var result = finisher.finish(
                    new MoveFinisher.Context(move, tenants, session.ctx(), slow, WORKER_NAME, () -> false));
                long holdMs = began.get() == 0 ? -1 : (System.nanoTime() - began.get()) / 1_000_000L;
                return new Timed(result, holdMs, proxy.clientChunks() - chunksAtStart.get());
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

        assertThat(timed.result().outcome()).as("every statement is fast: the budget is database time, not round trips: %s",
            timed.result().reason()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(timed.holdMs()).as("non-vacuity: from the start of the re-check to the commit, the round trips alone took "
            + "longer than the whole budget").isGreaterThan(BUDGET_MS);
        assertThat(timed.chunks()).as("non-vacuity: a verification read per tenant makes several round trips for each").isGreaterThanOrEqualTo(3 * TENANTS);
        assertThat(completedAt("lat-many")).isNotNull();
    }

    @Test
    void aTableWideOnlyVerification_makesTheSameFewRoundTripsWhateverTheTenantCount() throws Exception {
        DataMoveTestFixtures.register(ctx(), "lat-catalog", "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-table-wide-only.sql", BUDGET_MS, null);
        runsOnly("lat-catalog");

        var timed = finishBehindLatency("lat-catalog", tenants(TENANTS));

        assertThat(timed.result().outcome()).as(timed.result().reason()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(timed.chunks()).as("no per-tenant statement: the table-wide run, the tenant guards, the bounds and the commit, "
            + "not %s tenants' worth", TENANTS).isLessThanOrEqualTo(15);
        // the wall hold of the registry row for 30 tenants at a 60 ms round trip: reported, and held under the budget
        System.err.println("recheck-hold-ms tenants=" + TENANTS + " rtt~" + 2 * ONE_WAY_MS + "ms table-wide-only hold="
            + timed.holdMs() + "ms chunks=" + timed.chunks());
        assertThat(timed.holdMs()).as("a constant number of round trips, under the budget").isLessThan(BUDGET_MS);
    }

    @Test
    void aTableWideOnlyVerification_stillCatchesARowOfTheOldShapeInTheTableWideRun() {
        DataMoveTestFixtures.register(ctx(), "lat-catalog-slip", "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-table-wide-only.sql", 60_000, "on-complete-add-marker.sql");
        runsOnly("lat-catalog-slip");
        var finisher = new VerifyingFinisher();
        finisher.beforeCompletionHook = () -> ctx().insertInto(table("dm_test", "tenant_plain"))
            .columns(TENANT_ID, org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("id"), Long.class)).values("acme", 9_000L).execute();

        var result = finish(finisher, "lat-catalog-slip", "tenant_plain", tenants(3));

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION_NONEMPTY);
        assertThat(result.reason()).contains("completion transaction").contains("table-wide=1");
        assertThat(completedAt("lat-catalog-slip")).isNull();
    }

    @Test
    void theRechecksBudget_isStillDatabaseTime_onADatabaseAnRttAway() throws Exception {
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

    // ── the catalog re-read's statement count, which the budget charges the floor for ────────────────────

    private int statementsOfReread(String table) {
        var count = new AtomicInteger();
        var conf = ctx().configuration().derive(new DefaultExecuteListenerProvider(new DefaultExecuteListener() {
            @Override
            public void executeStart(ExecuteContext c) {
                count.incrementAndGet();
            }
        }));
        MoveTenants.reread(org.jooq.impl.DSL.using(conf), "dm_test", table, List.of("default"));
        return count.get();
    }

    @Test
    void theCatalogReread_sendsNoMoreStatementsThanTheBudgetChargesTheFloorFor() {
        assertThat(statementsOfReread("tenant_plain")).as("a table that is not partitioned: the tokens and the partition check")
            .isBetween(1, MoveTenants.REREAD_STATEMENTS);
        assertThat(statementsOfReread("leafed")).as("a table partitioned a tenant to a leaf: the most it sends")
            .isEqualTo(MoveTenants.REREAD_STATEMENTS);
    }
}
