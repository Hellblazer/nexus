// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * nexus-s9wav (HNSW maintenance policy nexus-4lnn1, R1): the operator-visible half of the leaf autovacuum
 * policy. {@code vectors-034-2} sets four storage parameters on every leaf of {@code nexus.chunks}, but
 * Liquibase drops notices and a {@code RAISE WARNING} reaches only the PostgreSQL server log, so a leaf the
 * self-heal skipped (locked, foreign-owned, vanished) or one carrying {@code autovacuum_enabled=false} (which
 * the self-heal deliberately keeps, and which defeats the whole policy) would otherwise show up nowhere.
 *
 * <p>After the migration this reads every leaf's {@code reloptions} and logs
 * {@code event=chunks_leaf_autovacuum_checked leaves=N policy_unset=M autovacuum_disabled=K} at INFO, or at WARN
 * naming the leaves when M or K is above 0 or no leaf was found. Read-only; nothing here throws.
 */
public final class ChunksLeafAutovacuumCheck {

    private static final Logger log = LoggerFactory.getLogger(ChunksLeafAutovacuumCheck.class);

    /** Sam's ruling, 2026-10-18. The same texts vectors-034 compares against; a drift test pins them equal. */
    static final List<String> POLICY = List.of(
        "autovacuum_vacuum_scale_factor=0.05",
        "autovacuum_vacuum_threshold=1000",
        "autovacuum_vacuum_insert_scale_factor=0.05",
        "autovacuum_analyze_scale_factor=0.02");

    private static final Set<String> FALSE_SPELLINGS = Set.of("false", "off", "0", "no", "f", "n");

    private ChunksLeafAutovacuumCheck() {}

    /**
     * @param leaves             leaves of the {@code nexus.chunks} tree examined (0 reads as a finding)
     * @param policyUnset        leaves missing at least one of the four values, by name
     * @param autovacuumDisabled leaves carrying {@code autovacuum_enabled} false, by name
     */
    public record Report(int leaves, List<String> policyUnset, List<String> autovacuumDisabled) {
        public boolean healthy() {
            return leaves > 0 && policyUnset.isEmpty() && autovacuumDisabled.isEmpty();
        }
    }

    private record Rel(long oid, String name, String kind, String[] options, Long parent) {}

    /** Reads the {@code nexus.chunks} tree through {@code pg_inherits} and classifies every ordinary leaf. */
    public static Report inspect(DSLContext ctx) {
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        var i = DSL.table(DSL.name("pg_catalog", "pg_inherits")).as("i");
        var oid = DSL.field(DSL.name("c", "oid")).cast(SQLDataType.BIGINT);
        var relname = DSL.field(DSL.name("c", "relname"), SQLDataType.VARCHAR);
        var relkind = DSL.field(DSL.name("c", "relkind")).cast(SQLDataType.VARCHAR);
        var inhparent = DSL.field(DSL.name("i", "inhparent")).cast(SQLDataType.BIGINT);
        var reloptions = DSL.field(DSL.name("c", "reloptions"), String[].class);
        Map<Long, Rel> byOid = new HashMap<>();
        for (var row : ctx.select(oid, relname, relkind, reloptions, inhparent)
                .from(c)
                .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .leftJoin(i).on(DSL.field(DSL.name("i", "inhrelid")).eq(DSL.field(DSL.name("c", "oid"))))
                .where(DSL.field(DSL.name("n", "nspname"), SQLDataType.VARCHAR).eq("nexus"))
                .and(relkind.in("r", "p"))
                .and(relname.eq("chunks")
                    .or(DSL.condition(DSL.field(DSL.name("c", "relispartition"), SQLDataType.BOOLEAN))))
                .fetch()) {
            byOid.put(row.value1(), new Rel(row.value1(), row.value2(), row.value3(), row.value4(), row.value5()));
        }
        Rel root = byOid.values().stream()
            .filter(r -> r.parent() == null && r.name().equals("chunks")).findFirst().orElse(null);
        if (root == null) {
            return new Report(0, List.of(), List.of());
        }
        Map<Long, List<Rel>> childrenOf = new HashMap<>();
        for (Rel r : byOid.values()) {
            if (r.parent() != null) {
                childrenOf.computeIfAbsent(r.parent(), k -> new ArrayList<>()).add(r);
            }
        }
        List<Rel> leaves = new ArrayList<>();
        Set<Long> seen = new HashSet<>();
        List<Rel> stack = new ArrayList<>(List.of(root));
        while (!stack.isEmpty()) {
            Rel r = stack.remove(stack.size() - 1);
            if (!seen.add(r.oid())) {
                continue;
            }
            if (r.kind().equals("r") && r.parent() != null) {
                leaves.add(r);
            }
            stack.addAll(childrenOf.getOrDefault(r.oid(), List.of()));
        }
        List<String> unset = new ArrayList<>();
        List<String> disabled = new ArrayList<>();
        for (Rel leaf : leaves) {
            Set<String> opts = leaf.options() == null ? Set.of() : new HashSet<>(Arrays.asList(leaf.options()));
            if (!opts.containsAll(POLICY)) {
                unset.add(leaf.name());
            }
            if (opts.stream().anyMatch(ChunksLeafAutovacuumCheck::disablesAutovacuum)) {
                disabled.add(leaf.name());
            }
        }
        unset.sort(null);
        disabled.sort(null);
        return new Report(leaves.size(), List.copyOf(unset), List.copyOf(disabled));
    }

    private static boolean disablesAutovacuum(String option) {
        int eq = option.indexOf('=');
        return eq > 0
            && option.substring(0, eq).equals("autovacuum_enabled")
            && FALSE_SPELLINGS.contains(option.substring(eq + 1).toLowerCase(java.util.Locale.ROOT));
    }

    /** Logs a report: INFO when healthy, WARN naming the leaves otherwise. */
    static void log(Report report) {
        if (report.healthy()) {
            log.info("event=chunks_leaf_autovacuum_checked leaves={} policy_unset=0 autovacuum_disabled=0",
                report.leaves());
            return;
        }
        log.warn("event=chunks_leaf_autovacuum_checked leaves={} policy_unset={} autovacuum_disabled={} "
                + "policy_unset_leaves={} autovacuum_disabled_leaves={}{}",
            report.leaves(), report.policyUnset().size(), report.autovacuumDisabled().size(),
            report.policyUnset(), report.autovacuumDisabled(),
            report.leaves() == 0 ? " cause=\"no chunks leaf found\"" : "");
    }

    /** Post-migrate step: inspect on the migration connection and log. Never throws. */
    public static void checkAndLog(Connection conn) {
        try {
            log(inspect(DSL.using(conn, SQLDialect.POSTGRES)));
        } catch (RuntimeException e) {
            log.warn("event=chunks_leaf_autovacuum_check_failed error={}", e.toString());
            try {
                if (!conn.getAutoCommit()) {
                    conn.rollback();   // a failed read must not leave the migration connection's transaction aborted
                }
            } catch (java.sql.SQLException ignored) {
                // best effort; the caller's later reads report their own failure
            }
        }
    }
}
