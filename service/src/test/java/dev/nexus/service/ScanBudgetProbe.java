/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service;

import org.jooq.Field;
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
 * nexus-wbfpw.47 test instrument: a {@link DataSource} wrapper that reads the HNSW scan
 * GUCs back from a pooled connection at the moment a vector-search statement is PREPARED,
 * which is when the search transaction is about to run it. Reading at statement time (not
 * at commit) is what makes a budget set AFTER the fetch show up as the pgvector defaults.
 *
 * <p>A statement counts as a vector search when its SQL names one of the generated search
 * functions ({@code plain_search_<dim>}, {@code text_gated_search_*}, {@code search_*_scoped_*},
 * {@code search_graph_hop_*}) or {@code taxonomy_ann_query_<dim>}. Nothing is recorded
 * unless a label is armed, so fixture seeding stays out of the record.
 */
final class ScanBudgetProbe {

    /** One vector-search statement and the GUCs in effect when it was prepared. */
    record Seen(String label, String sql, String iterativeScan, String maxScanTuples, String multiplier) {}

    private final List<Seen> seen = new CopyOnWriteArrayList<>();
    private volatile String label = null;

    List<Seen> seen() {
        return seen;
    }

    /** Statements recorded under {@code name} that ran an HNSW iterative scan. */
    List<Seen> hnswStatements(String name) {
        return seen.stream()
            .filter(s -> s.label().equals(name) && "relaxed_order".equals(s.iterativeScan()))
            .toList();
    }

    void arm(String name) {
        label = name;
    }

    void disarm() {
        label = null;
    }

    DataSource wrap(DataSource delegate) {
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

    private static boolean isVectorSearch(String sql) {
        String s = sql.toLowerCase(Locale.ROOT);
        return s.contains("search_") || s.contains("taxonomy_ann_query_");
    }

    private static Field<String> setting(String guc) {
        return DSL.function("current_setting", String.class, DSL.val(guc), DSL.inline(true));
    }

    private Connection wrapConnection(Connection real) {
        return (Connection) Proxy.newProxyInstance(
            Connection.class.getClassLoader(), new Class<?>[] {Connection.class},
            (proxy, method, args) -> {
                String armed = label;
                if (armed != null && args != null && args.length > 0 && args[0] instanceof String sql
                        && (method.getName().equals("prepareStatement")
                            || method.getName().equals("prepareCall")
                            || method.getName().equals("createStatement"))
                        && isVectorSearch(sql)) {
                    var db = DSL.using(real, SQLDialect.POSTGRES);
                    var row = db.select(setting("hnsw.iterative_scan"),
                                        setting("hnsw.max_scan_tuples"),
                                        setting("hnsw.scan_mem_multiplier"))
                        .fetchSingle();
                    seen.add(new Seen(armed, sql, row.value1(), row.value2(), row.value3()));
                }
                try {
                    return method.invoke(real, args);
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
            });
    }
}
