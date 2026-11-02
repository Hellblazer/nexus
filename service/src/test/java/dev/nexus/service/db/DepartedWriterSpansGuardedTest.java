// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultConnectionProvider;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Savepoint;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-pvkbv: pins {@link DepartedWriterSpans#guarded}'s two exits without a database. A deadlock (SQLSTATE
 * 40P01, wrapped the way jOOQ and TenantScope deliver it) must reach the surrounding {@link DeadlockRetry}, so
 * the transaction re-runs; any other failure is undone to the savepoint and answered with the fallback, so the
 * manifest write that triggered the clear goes on. The connection is a recording stand-in that supports the
 * savepoint calls {@code guarded} makes; the behaviour under test is which exit {@code guarded} takes.
 */
class DepartedWriterSpansGuardedTest {

    /** The calls the connection saw, in order: {@code setSavepoint}, {@code release}, {@code rollback}. */
    private static DSLContext ctxRecording(List<String> calls) {
        Savepoint sp = new Savepoint() {
            @Override
            public int getSavepointId() {
                return 1;
            }

            @Override
            public String getSavepointName() throws SQLException {
                throw new SQLException("unnamed");
            }
        };
        Connection conn = (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(),
            new Class<?>[] {Connection.class}, (proxy, method, args) -> {
                switch (method.getName()) {
                    case "setSavepoint" -> {
                        calls.add("setSavepoint");
                        return sp;
                    }
                    case "releaseSavepoint" -> {
                        calls.add("release");
                        return null;
                    }
                    case "rollback" -> {
                        calls.add(args != null && args.length == 1 ? "rollbackToSavepoint" : "rollback");
                        return null;
                    }
                    default -> throw new UnsupportedOperationException(method.getName());
                }
            });
        return DSL.using(new DefaultConnectionProvider(conn), SQLDialect.POSTGRES);
    }

    @Test
    void aDeadlockInTheBodyIsRethrownForTheRetryAndNotSwallowed() {
        List<String> calls = new ArrayList<>();
        // The wrapper chain as it arrives in practice: RuntimeException over DataAccessException over the 40P01.
        RuntimeException deadlock = new RuntimeException("wrapped",
            new DataAccessException("deadlock detected",
                new SQLException("deadlock detected", DeadlockRetry.SQLSTATE_DEADLOCK)));

        assertThatThrownBy(() -> DepartedWriterSpans.guarded(ctxRecording(calls), "t", "test", () -> {
            throw deadlock;
        }, "fallback")).isSameAs(deadlock);

        assertThat(DeadlockRetry.isDeadlock(deadlock)).isTrue();
        assertThat(calls).containsExactly("setSavepoint");
    }

    @Test
    void aCheckedDeadlockIsRethrownToo() {
        List<String> calls = new ArrayList<>();
        SQLException deadlock = new SQLException("deadlock detected", DeadlockRetry.SQLSTATE_DEADLOCK);

        assertThatThrownBy(() -> DepartedWriterSpans.guarded(ctxRecording(calls), "t", "test", () -> {
            throw deadlock;
        }, "fallback")).isInstanceOf(IllegalStateException.class).hasCause(deadlock);
    }

    @Test
    void anyOtherErrorIsUndoneToTheSavepointAndAnswersTheFallback() {
        List<String> calls = new ArrayList<>();
        // 23514 check_violation, the failure the fail-open test provokes against a real database.
        RuntimeException other = new DataAccessException("check violation",
            new SQLException("check violation", "23514"));

        String out = DepartedWriterSpans.guarded(ctxRecording(calls), "t", "test", () -> {
            throw other;
        }, "fallback");

        assertThat(out).isEqualTo("fallback");
        assertThat(calls).containsExactly("setSavepoint", "rollbackToSavepoint");
    }

    @Test
    void aBodyThatSucceedsReleasesTheSavepointAndReturnsItsValue() {
        List<String> calls = new ArrayList<>();

        String out = DepartedWriterSpans.guarded(ctxRecording(calls), "t", "test", () -> "value", "fallback");

        assertThat(out).isEqualTo("value");
        assertThat(calls).containsExactly("setSavepoint", "release");
    }
}
