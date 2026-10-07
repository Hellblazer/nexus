// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.io.PrintWriter;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLFeatureNotSupportedException;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executor;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Logger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * The parts of {@link ChunksIsolationCheck} that need no database: what the status supplier does when the probe
 * cannot run, and its cache. The policy logic against a real catalog is
 * {@code TextGateProbeSingleRoleGuardIntegrationTest}.
 */
class ChunksIsolationCheckTest {

    private static final class DeadDataSource implements DataSource {
        final AtomicInteger connects = new AtomicInteger();

        @Override public Connection getConnection() throws SQLException {
            connects.incrementAndGet();
            throw new SQLException("pool is down");
        }
        @Override public Connection getConnection(String u, String p) throws SQLException { return getConnection(); }
        @Override public PrintWriter getLogWriter() { return null; }
        @Override public void setLogWriter(PrintWriter out) { }
        @Override public void setLoginTimeout(int seconds) { }
        @Override public int getLoginTimeout() { return 0; }
        @Override public Logger getParentLogger() throws SQLFeatureNotSupportedException {
            throw new SQLFeatureNotSupportedException();
        }
        @Override public <T> T unwrap(Class<T> iface) throws SQLException { throw new SQLException("no"); }
        @Override public boolean isWrapperFor(Class<?> iface) { return false; }
    }

    private static final class MutableClock extends Clock {
        Instant now = Instant.parse("2026-10-04T12:00:00Z");
        @Override public ZoneId getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(ZoneId zone) { return this; }
        @Override public Instant instant() { return now; }
    }

    /** Runs a refresh only when the test says so, so what the request thread sees is deterministic. */
    private static final class ManualExecutor implements Executor {
        final java.util.ArrayDeque<Runnable> queued = new java.util.ArrayDeque<>();
        @Override public void execute(Runnable r) { queued.add(r); }
        void runAll() { while (!queued.isEmpty()) queued.poll().run(); }
    }

    @Test
    void aProbeThatCannotRunIsAnUnknown_notATrue_andIsCachedForTheTtl() {
        var ds = new DeadDataSource();
        var clock = new MutableClock();
        var exec = new ManualExecutor();
        var status = ChunksIsolationCheck.statusSupplier(ds, clock, exec);

        assertThat(status.get()).as("before any refresh has run the answer is absent").isNull();
        assertThat(ds.connects.get()).as("a status call never touches the pool itself").isEqualTo(0);
        exec.runAll();
        assertThat(ds.connects.get()).as("the primed refresh asked once").isEqualTo(1);
        assertThat(status.get()).as("cannot tell is null, never a fabricated true").isNull();
        assertThat(exec.queued).as("the second poll inside the TTL starts no refresh").isEmpty();

        clock.now = clock.now.plusMillis(ChunksIsolationCheck.STATUS_TTL_MILLIS - 1);
        status.get();
        assertThat(exec.queued).as("still inside the TTL").isEmpty();

        clock.now = clock.now.plusMillis(1);
        status.get();
        assertThat(exec.queued).as("at the TTL a refresh is queued, not run on the request thread").hasSize(1);
        assertThat(ds.connects.get()).isEqualTo(1);
        status.get();
        assertThat(exec.queued).as("at most one refresh is in flight").hasSize(1);
        exec.runAll();
        assertThat(ds.connects.get()).as("and it asked again").isEqualTo(2);
    }

    /** A pool whose getConnection blocks until released: the saturated-pool case. */
    private static final class StuckDataSource implements DataSource {
        final CountDownLatch entered = new CountDownLatch(1);
        final CountDownLatch release = new CountDownLatch(1);
        final AtomicInteger connects = new AtomicInteger();

        @Override public Connection getConnection() throws SQLException {
            connects.incrementAndGet();
            entered.countDown();
            try {
                release.await(30, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            throw new SQLException("pool saturated");
        }
        @Override public Connection getConnection(String u, String p) throws SQLException { return getConnection(); }
        @Override public PrintWriter getLogWriter() { return null; }
        @Override public void setLogWriter(PrintWriter out) { }
        @Override public void setLoginTimeout(int seconds) { }
        @Override public int getLoginTimeout() { return 0; }
        @Override public Logger getParentLogger() throws SQLFeatureNotSupportedException {
            throw new SQLFeatureNotSupportedException();
        }
        @Override public <T> T unwrap(Class<T> iface) throws SQLException { throw new SQLException("no"); }
        @Override public boolean isWrapperFor(Class<?> iface) { return false; }
    }

    /** I3: a refresh stuck on a pool connection must not hold up a status request, from any number of callers. */
    @Test
    void aStatusCallReturnsPromptly_whileARefreshIsStuckOnThePool() throws Exception {
        var ds = new StuckDataSource();
        var clock = new MutableClock();
        var status = ChunksIsolationCheck.statusSupplier(ds, clock);   // the real background executor
        try {
            assertThat(ds.entered.await(10, TimeUnit.SECONDS)).as("the primed refresh is stuck in getConnection").isTrue();
            clock.now = clock.now.plusMillis(ChunksIsolationCheck.STATUS_TTL_MILLIS + 1);   // stale: wants a refresh

            var callers = Executors.newFixedThreadPool(4);
            try {
                List<Future<Boolean>> calls = new ArrayList<>();
                for (int i = 0; i < 8; i++) {
                    calls.add(callers.submit(status::get));
                }
                for (var f : calls) {
                    assertThat(f.get(2, TimeUnit.SECONDS)).as("served without waiting on the pool: absent").isNull();
                }
            } finally {
                callers.shutdownNow();
            }
            assertThat(ds.connects.get()).as("one refresh in flight, however many polls").isEqualTo(1);
        } finally {
            ds.release.countDown();
        }
    }

    private static SQLException sqlState(String state) {
        return new SQLException("boom " + state, state);
    }

    private static ChunksIsolationCheck.Probe clean() {
        return new ChunksIsolationCheck.Probe("nexus_svc", List.of());
    }

    /** A probe that fails with {@code failure} for its first {@code failures} calls, then reports clean. */
    private static final class Flaky implements Callable<ChunksIsolationCheck.Probe> {
        final int failures;
        final Exception failure;
        final AtomicInteger calls = new AtomicInteger();
        Flaky(int failures, Exception failure) { this.failures = failures; this.failure = failure; }
        @Override public ChunksIsolationCheck.Probe call() throws Exception {
            if (calls.incrementAndGet() <= failures) throw failure;
            return clean();
        }
    }

    @Test
    void aTransientProbeFailureThatClearsOnRetry_boots_afterTheBackoff() {
        for (String state : new String[] {"08006", "53300", "57P01", "57014", "40001"}) {
            var probe = new Flaky(2, sqlState(state));
            List<Long> slept = new ArrayList<>();
            ChunksIsolationCheck.verifyAtStartup(probe, ChunksIsolationCheck.STARTUP_BACKOFF_MILLIS, slept::add);
            assertThat(probe.calls.get()).as("SQLState %s: third attempt succeeds", state).isEqualTo(3);
            assertThat(slept).as("backoff before each retry").containsExactly(1_000L, 2_000L);
        }
    }

    @Test
    void aTransientProbeFailureThatPersists_refusesAfterTheRetries() {
        var probe = new Flaky(Integer.MAX_VALUE, sqlState("08006"));
        List<Long> slept = new ArrayList<>();
        assertThatThrownBy(() ->
                ChunksIsolationCheck.verifyAtStartup(probe, ChunksIsolationCheck.STARTUP_BACKOFF_MILLIS, slept::add))
            .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
            .hasMessageContaining("could not check")
            .hasMessageContaining("boom 08006");
        assertThat(probe.calls.get()).as("the first attempt and three retries").isEqualTo(4);
        assertThat(slept).containsExactly(1_000L, 2_000L, 4_000L);
    }

    @Test
    void aTransientStateReachedThroughAWrapperOrAPoolTimeout_isRetried_aPermissionErrorIsNot() {
        var wrapped = new Flaky(1, new org.jooq.exception.DataAccessException("wrapped", sqlState("57P03")));
        ChunksIsolationCheck.verifyAtStartup(wrapped, ChunksIsolationCheck.STARTUP_BACKOFF_MILLIS, ms -> { });
        assertThat(wrapped.calls.get()).isEqualTo(2);

        var timeout = new Flaky(1, new java.sql.SQLTransientConnectionException("pool timed out"));
        ChunksIsolationCheck.verifyAtStartup(timeout, ChunksIsolationCheck.STARTUP_BACKOFF_MILLIS, ms -> { });
        assertThat(timeout.calls.get()).as("Hikari's timeout carries no SQLState").isEqualTo(2);

        var denied = new Flaky(1, sqlState("42501"));
        assertThatThrownBy(() ->
                ChunksIsolationCheck.verifyAtStartup(denied, ChunksIsolationCheck.STARTUP_BACKOFF_MILLIS, ms -> { }))
            .isInstanceOf(ChunksIsolationCheck.IsolationException.class);
        assertThat(denied.calls.get()).as("insufficient_privilege is not transient").isEqualTo(1);
    }

    @Test
    void aRealViolationRefusesImmediately_withNoRetryAndNoBackoff() {
        var calls = new AtomicInteger();
        List<Long> slept = new ArrayList<>();
        assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(() -> {
                calls.incrementAndGet();
                return new ChunksIsolationCheck.Probe("nexus_svc",
                    List.of(new ChunksIsolationCheck.Violation("some_policy", "public")));
            }, ChunksIsolationCheck.STARTUP_BACKOFF_MILLIS, slept::add))
            .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
            .hasMessageContaining("some_policy");
        assertThat(calls.get()).isEqualTo(1);
        assertThat(slept).isEmpty();
    }

    @Test
    void startupRefusesWhenTheProbeCannotRun_aServiceThatCannotTellDoesNotServe() {
        assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(new DeadDataSource()))
            .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
            .hasMessageContaining("could not check")
            .hasMessageContaining("pool is down");
    }

    @Test
    void theRefusalNamesThePolicy_theConnectedRole_theRoleItArrivesThrough_andBothRemedies() {
        String msg = ChunksIsolationCheck.refusal("nexus_svc", java.util.List.of(
            new ChunksIsolationCheck.Violation("chunks_gate_probe_owner_read", "nexus_admin")));
        assertThat(msg).contains("chunks_gate_probe_owner_read", "nexus_svc", "nexus_admin",
            "DROP POLICY chunks_gate_probe_owner_read ON nexus.chunks", "NX_DB_ADMIN_URL", "WITH INHERIT FALSE",
            "read or write every tenant's chunks");
        assertThat(msg).as("dropping a policy on the parent alone leaves its copy on every leaf (nexus-3wh8d.17 M1)")
            .contains("partition_sync_access('nexus.chunks'::regclass)");
        assertThat(ChunksIsolationCheck.refusal("nexus_svc", java.util.List.of(
                new ChunksIsolationCheck.Violation("some_policy", "public"))))
            .as("a policy for PUBLIC says PUBLIC").contains("through PUBLIC");
    }
}
