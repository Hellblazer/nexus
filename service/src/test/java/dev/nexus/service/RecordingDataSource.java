// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import javax.sql.DataSource;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * Test probe over a {@link DataSource} that records every prepared statement the code under test executes,
 * in execution order: its SQL text and the values bound to its placeholders (by position, in the order the
 * {@code setXxx} calls were made, which for jOOQ is placeholder order).
 *
 * <p>For tests that must show what a repository method actually SENDS, not what a helper would send: that a
 * transaction setting precedes a read, or which statement a read ran so its plan can be inspected.
 */
public final class RecordingDataSource {

    /** One executed prepared statement. */
    public record Executed(String sql, List<Object> binds) {
        @Override
        public String toString() {
            return sql + " " + binds;
        }
    }

    private final List<Executed> executed = new CopyOnWriteArrayList<>();
    private final DataSource proxy;

    public RecordingDataSource(DataSource delegate) {
        this.proxy = (DataSource) Proxy.newProxyInstance(DataSource.class.getClassLoader(),
            new Class<?>[] {DataSource.class}, (p, m, a) -> {
                Object out = call(delegate, m, a);
                if (!m.getName().equals("getConnection")) {
                    return out;
                }
                Connection real = (Connection) out;
                return Proxy.newProxyInstance(Connection.class.getClassLoader(),
                    new Class<?>[] {Connection.class}, (cp, cm, ca) -> {
                        Object r = call(real, cm, ca);
                        if (cm.getName().equals("prepareStatement") && ca != null && ca[0] instanceof String text) {
                            return recordingStatement((PreparedStatement) r, text);
                        }
                        return r;
                    });
            });
    }

    public DataSource dataSource() {
        return proxy;
    }

    public List<Executed> executed() {
        return List.copyOf(executed);
    }

    public void clear() {
        executed.clear();
    }

    private PreparedStatement recordingStatement(PreparedStatement real, String text) {
        InvocationHandler h = new InvocationHandler() {
            final List<Object> bound = new ArrayList<>();

            @Override
            public Object invoke(Object p, Method m, Object[] a) throws Throwable {
                if (m.getName().startsWith("set") && a != null && a.length >= 2) {
                    bound.add(a[1]);
                }
                if (m.getName().startsWith("execute")) {
                    executed.add(new Executed(text, java.util.Collections.unmodifiableList(new ArrayList<>(bound))));
                }
                return call(real, m, a);
            }
        };
        return (PreparedStatement) Proxy.newProxyInstance(PreparedStatement.class.getClassLoader(),
            new Class<?>[] {PreparedStatement.class}, h);
    }

    private static Object call(Object target, Method m, Object[] args) throws Throwable {
        try {
            return m.invoke(target, args);
        } catch (InvocationTargetException e) {
            throw e.getCause();
        }
    }
}
