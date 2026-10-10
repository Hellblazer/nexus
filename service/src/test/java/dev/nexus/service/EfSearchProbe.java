// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import javax.sql.DataSource;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * RDR-227 Step 1 test instrument: a {@link DataSource} wrapper that reads {@code hnsw.ef_search} back from
 * the pooled connection at the moment a {@code plain_search_<dim>} statement is PREPARED, which is when the
 * search transaction is about to run it. This is what Postgres will run the statement with, read inside the
 * statement's own transaction, not what the Java side meant to set. Same technique as
 * {@link ScanBudgetProbe}. An {@code EXPLAIN} of such a statement (the plan check's sample) is not recorded.
 */
public final class EfSearchProbe {

    private final List<String> efSearch = new CopyOnWriteArrayList<>();

    /** {@code hnsw.ef_search} in effect at each plain-search statement prepared so far, in order. */
    public List<String> efSearchPerStatement() {
        return efSearch;
    }

    public void clear() {
        efSearch.clear();
    }

    public DataSource wrap(DataSource delegate) {
        return (DataSource) Proxy.newProxyInstance(
            DataSource.class.getClassLoader(), new Class<?>[] {DataSource.class},
            (proxy, method, args) -> {
                Object result;
                try {
                    result = method.invoke(delegate, args);
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
                if (method.getName().equals("getConnection") && result instanceof Connection c) {
                    return wrapConnection(c);
                }
                return result;
            });
    }

    private Connection wrapConnection(Connection real) {
        return (Connection) Proxy.newProxyInstance(
            Connection.class.getClassLoader(), new Class<?>[] {Connection.class},
            (proxy, method, args) -> {
                if (args != null && args.length > 0 && args[0] instanceof String sql
                        && (method.getName().equals("prepareStatement")
                            || method.getName().equals("prepareCall")
                            || method.getName().equals("createStatement"))
                        && sql.toLowerCase(Locale.ROOT).contains("plain_search_")
                        // The plan check's EXPLAIN of the arm (RDR-227) names the function too; it is not an arm.
                        && !sql.stripLeading().toLowerCase(Locale.ROOT).startsWith("explain")) {
                    efSearch.add(DSL.using(real, SQLDialect.POSTGRES)
                        .select(DSL.function("current_setting", String.class,
                                             DSL.val("hnsw.ef_search"), DSL.inline(true)))
                        .fetchSingle().value1());
                }
                try {
                    return method.invoke(real, args);
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
            });
    }
}
