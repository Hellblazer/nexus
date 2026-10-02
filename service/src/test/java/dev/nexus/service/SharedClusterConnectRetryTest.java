// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.net.ConnectException;
import java.sql.SQLException;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-33prh: pure-unit coverage of {@link SharedCluster#connectWithRetry}, the bounded
 * retry the shared cluster's bootstrap connect uses so it survives colima's asynchronous
 * host-port forward (a refused first connect to a just-started container). No Docker.
 */
final class SharedClusterConnectRetryTest {

    private static final Duration SLEEP = Duration.ofMillis(5);

    /** What pgjdbc throws for a refused connect: PSQLException (SQLSTATE 08001) caused by ConnectException. */
    private static SQLException refused() {
        return new SQLException("Connection to localhost:32772 refused.", "08001",
            new ConnectException("Connection refused"));
    }

    @Test
    void retriesWhileConnectionIsRefusedThenSucceeds() throws Exception {
        var calls = new AtomicInteger();

        String result = SharedCluster.connectWithRetry(() -> {
            if (calls.incrementAndGet() < 4) {
                throw refused();
            }
            return "connected";
        }, () -> true, Duration.ofSeconds(10), SLEEP);

        assertThat(result).isEqualTo("connected");
        assertThat(calls).hasValue(4);
    }

    @Test
    void givesUpAtTheDeadlineNamingTheCause() {
        var calls = new AtomicInteger();

        assertThatThrownBy(() -> SharedCluster.connectWithRetry(() -> {
            calls.incrementAndGet();
            throw refused();
        }, () -> true, Duration.ofMillis(150), SLEEP))
            .isInstanceOf(SQLException.class)
            .hasMessageContaining("did not become reachable")
            .hasMessageContaining("Connection refused")
            .hasCauseInstanceOf(SQLException.class);
        assertThat(calls.get()).isGreaterThan(1);
    }

    @Test
    void authFailureFailsAtOnceWithoutRetrying() {
        var calls = new AtomicInteger();
        var auth = new SQLException("FATAL: password authentication failed", "28P01");

        assertThatThrownBy(() -> SharedCluster.connectWithRetry(() -> {
            calls.incrementAndGet();
            throw auth;
        }, () -> true, Duration.ofSeconds(10), SLEEP))
            .isSameAs(auth);
        assertThat(calls).hasValue(1);
    }

    @Test
    void anyNonConnectionExceptionFailsAtOnce() {
        var calls = new AtomicInteger();
        var other = new SQLException("FATAL: the database system is starting up", "57P03");

        assertThatThrownBy(() -> SharedCluster.connectWithRetry(() -> {
            calls.incrementAndGet();
            throw other;
        }, () -> true, Duration.ofSeconds(10), SLEEP))
            .isSameAs(other);
        assertThat(calls).hasValue(1);
    }

    @Test
    void stopsRetryingOnceTheContainerIsNoLongerRunning() {
        var calls = new AtomicInteger();

        assertThatThrownBy(() -> SharedCluster.connectWithRetry(() -> {
            calls.incrementAndGet();
            throw refused();
        }, () -> calls.get() < 3, Duration.ofSeconds(10), SLEEP))
            .isInstanceOf(SQLException.class)
            .hasMessageContaining("not running");
        assertThat(calls).hasValue(3);
    }

    @Test
    void aConnectExceptionDeepInTheCauseChainIsStillRetried() throws Exception {
        var calls = new AtomicInteger();

        String result = SharedCluster.connectWithRetry(() -> {
            if (calls.incrementAndGet() < 2) {
                throw new SQLException("outer", "08001",
                    new RuntimeException("middle", new ConnectException("Connection refused")));
            }
            return "ok";
        }, () -> true, Duration.ofSeconds(10), SLEEP);

        assertThat(result).isEqualTo("ok");
    }
}
