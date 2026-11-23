// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import javax.sql.DataSource;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * A {@link DataSource} whose connections apply a COMMIT and then report it to the client as an exception: PostgreSQL has the
 * transaction, the caller sees a {@link SQLException} (a lost acknowledgement; RDR-229 row 15 addendum). While {@link #arm}ed, the
 * {@code commit()} of a connection that has run a write statement ({@code insert}, {@code update} or {@code delete}) commits for
 * real and then throws (with {@link #armAnyCommit}, every commit does); a read-only transaction, and a commit while disarmed, are untouched. Each throw is counted.
 */
final class CommitThenThrowDataSource {

    private final DataSource proxy;
    private final AtomicBoolean armed = new AtomicBoolean();
    private final AtomicBoolean anyCommit = new AtomicBoolean();
    private final AtomicInteger lost = new AtomicInteger();

    CommitThenThrowDataSource(DataSource delegate) {
        this.proxy = (DataSource) Proxy.newProxyInstance(DataSource.class.getClassLoader(), new Class<?>[] {DataSource.class},
            (p, m, args) -> {
                Object result = invoke(delegate, m, args);
                return "getConnection".equals(m.getName()) ? wrap((Connection) result) : result;
            });
    }

    DataSource dataSource() {
        return proxy;
    }

    void arm() {
        anyCommit.set(false);
        armed.set(true);
    }

    /** As {@link #arm}, for a path whose writes are not prepared statements: every commit while armed is applied and then lost. */
    void armAnyCommit() {
        anyCommit.set(true);
        armed.set(true);
    }

    void disarm() {
        armed.set(false);
    }

    /** How many commits were applied and then reported as an exception. */
    int lostAcknowledgements() {
        return lost.get();
    }

    private Connection wrap(Connection real) {
        AtomicBoolean wrote = new AtomicBoolean();
        return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[] {Connection.class},
            (p, m, args) -> {
                if (args != null && args.length > 0 && args[0] instanceof String sql
                        && (m.getName().equals("prepareStatement") || m.getName().equals("prepareCall"))) {
                    String head = sql.stripLeading().toLowerCase(java.util.Locale.ROOT);
                    if (head.startsWith("insert") || head.startsWith("update") || head.startsWith("delete")) {
                        wrote.set(true);
                    }
                }
                Object result = invoke(real, m, args);
                if (m.getName().equals("commit") && armed.get() && (wrote.get() || anyCommit.get())) {
                    lost.incrementAndGet();
                    throw new SQLException("the COMMIT was applied, and its acknowledgement was lost (test)");
                }
                if (m.getName().equals("commit") || m.getName().equals("rollback")) {
                    wrote.set(false);
                }
                return result;
            });
    }

    private static Object invoke(Object target, java.lang.reflect.Method m, Object[] args) throws Throwable {
        try {
            return m.invoke(target, args);
        } catch (InvocationTargetException e) {
            throw e.getCause();
        }
    }
}
