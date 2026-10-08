// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import javax.sql.DataSource;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

/**
 * Test probe over a {@link DataSource} for the per-collection search fan-out (nexus-tu8wp.1).
 *
 * <p>It watches only the connections borrowed by a fan-out ARM, recognised by a
 * {@code PgVectorRepository#runArm} frame on the borrowing thread's stack, so the request
 * thread's own sequential lookups never pollute the figures. For those connections it keeps
 * <em>simultaneous</em> holders: a counter raised at borrow and lowered when the connection is
 * closed, with its high-water mark. That is the quantity a concurrency cap bounds, and it is not
 * the same as an exit code or a call count. {@link #holdMs} keeps each arm connection open a
 * little longer on close, so arms that CAN overlap do overlap and a missing cap shows as a peak
 * above the limit rather than hiding behind fast arms.
 *
 * <p>It records every {@code statement_timeout} an arm connection is given ({@link #statementTimeouts},
 * read off the bound parameters of the {@code set_config('statement_timeout', ?, true)} call, in
 * order), and it can delay each arm's connection borrow ({@link #borrowDelayMs}) to stand in for an
 * admission or pool wait.
 *
 * <p>It can also fail the Nth arm borrow and every one after it ({@link #failFromArm}), with
 * whatever {@link SQLException} the test supplies, to stand in for pool exhaustion or a statement
 * timeout without a flaky real one.
 */
final class ArmProbeDataSource {

    final AtomicInteger open = new AtomicInteger();
    final AtomicInteger peak = new AtomicInteger();
    final AtomicInteger armBorrows = new AtomicInteger();

    /** Extra time an arm connection stays held at close. */
    volatile long holdMs = 0L;
    /** 1-based arm borrow from which every borrow fails; 0 = never. */
    volatile int failFromArm = 0;
    volatile Supplier<? extends Exception> failure = () -> new SQLException("probe failure");
    /** Time every arm borrow sleeps BEFORE it gets its connection (an admission or pool wait). */
    volatile long borrowDelayMs = 0L;
    /**
     * Fail every borrow made under {@code PgVectorRepository#attachEmbeddings} (the include_embeddings fill,
     * nexus-92q1p): stands in for a DB error or statement timeout in the by-id vector read.
     */
    volatile boolean failEmbeddingFill = false;
    /** Every statement_timeout value an arm connection was given, in the order it was set. */
    final List<Integer> statementTimeouts = new CopyOnWriteArrayList<>();

    private final DataSource delegate;
    private final DataSource proxy;

    ArmProbeDataSource(DataSource delegate) {
        this.delegate = delegate;
        this.proxy = (DataSource) Proxy.newProxyInstance(
            DataSource.class.getClassLoader(), new Class<?>[] {DataSource.class}, this::onDataSource);
    }

    /** One proxy for the probe's life, so it is also a stable key for per-DataSource static state. */
    DataSource dataSource() {
        return proxy;
    }

    void reset() {
        open.set(0);
        peak.set(0);
        armBorrows.set(0);
        holdMs = 0L;
        failFromArm = 0;
        borrowDelayMs = 0L;
        failEmbeddingFill = false;
        failure = () -> new SQLException("probe failure");
        statementTimeouts.clear();
    }

    private static boolean inArm() {
        return StackWalker.getInstance().walk(
            frames -> frames.anyMatch(f -> f.getMethodName().equals("runArm")));
    }

    private static boolean inEmbeddingFill() {
        return StackWalker.getInstance().walk(
            frames -> frames.anyMatch(f -> f.getMethodName().equals("attachEmbeddings")));
    }

    private Object onDataSource(Object proxy, java.lang.reflect.Method m, Object[] args) throws Throwable {
        if (!m.getName().equals("getConnection")) {
            return invoke(delegate, m, args);
        }
        if (failEmbeddingFill && inEmbeddingFill()) {
            throw failure.get();
        }
        boolean arm = inArm();
        if (arm) {
            int nth = armBorrows.incrementAndGet();
            if (failFromArm > 0 && nth >= failFromArm) {
                throw failure.get();
            }
        }
        if (arm && borrowDelayMs > 0) {
            Thread.sleep(borrowDelayMs);
        }
        Connection real = (Connection) invoke(delegate, m, args);
        if (!arm) {
            return real;
        }
        int now = open.incrementAndGet();
        peak.accumulateAndGet(now, Math::max);
        InvocationHandler h = (p, cm, cargs) -> {
            if (cm.getName().equals("close")) {
                try {
                    if (holdMs > 0) {
                        Thread.sleep(holdMs);
                    }
                } finally {
                    open.decrementAndGet();
                }
            }
            Object out = invoke(real, cm, cargs);
            if (cm.getName().equals("prepareStatement") && cargs != null && cargs.length > 0
                    && cargs[0] instanceof String sql && sql.contains("set_config")) {
                return spy((PreparedStatement) out);
            }
            return out;
        };
        return Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[] {Connection.class}, h);
    }

    /** Watches one {@code set_config(?, ?, true)} statement: records the value bound for statement_timeout. */
    private PreparedStatement spy(PreparedStatement real) {
        Map<Integer, Object> bound = new HashMap<>();
        InvocationHandler h = (p, m, a) -> {
            String name = m.getName();
            if ((name.equals("setString") || name.equals("setObject")) && a != null && a.length >= 2
                    && a[0] instanceof Integer idx) {
                bound.put(idx, a[1]);
            }
            if (name.startsWith("execute") && "statement_timeout".equals(String.valueOf(bound.get(1)))
                    && bound.get(2) != null) {
                statementTimeouts.add(Integer.parseInt(String.valueOf(bound.get(2))));
            }
            return invoke(real, m, a);
        };
        return (PreparedStatement) Proxy.newProxyInstance(
            PreparedStatement.class.getClassLoader(), new Class<?>[] {PreparedStatement.class}, h);
    }

    private static Object invoke(Object target, java.lang.reflect.Method m, Object[] args) throws Throwable {
        try {
            return m.invoke(target, args);
        } catch (InvocationTargetException e) {
            throw e.getCause();
        }
    }
}
