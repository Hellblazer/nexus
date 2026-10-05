// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import javax.sql.DataSource;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.SQLException;
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
    volatile Supplier<SQLException> failure = () -> new SQLException("probe failure");

    private final DataSource delegate;

    ArmProbeDataSource(DataSource delegate) {
        this.delegate = delegate;
    }

    DataSource dataSource() {
        return (DataSource) Proxy.newProxyInstance(
            DataSource.class.getClassLoader(), new Class<?>[] {DataSource.class}, this::onDataSource);
    }

    void reset() {
        open.set(0);
        peak.set(0);
        armBorrows.set(0);
        holdMs = 0L;
        failFromArm = 0;
    }

    private static boolean inArm() {
        return StackWalker.getInstance().walk(
            frames -> frames.anyMatch(f -> f.getMethodName().equals("runArm")));
    }

    private Object onDataSource(Object proxy, java.lang.reflect.Method m, Object[] args) throws Throwable {
        if (!m.getName().equals("getConnection")) {
            return invoke(delegate, m, args);
        }
        boolean arm = inArm();
        if (arm) {
            int nth = armBorrows.incrementAndGet();
            if (failFromArm > 0 && nth >= failFromArm) {
                throw failure.get();
            }
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
            return invoke(real, cm, cargs);
        };
        return Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[] {Connection.class}, h);
    }

    private static Object invoke(Object target, java.lang.reflect.Method m, Object[] args) throws Throwable {
        try {
            return m.invoke(target, args);
        } catch (InvocationTargetException e) {
            throw e.getCause();
        }
    }
}
