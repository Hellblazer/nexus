// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Record2;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.time.Clock;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.Executor;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.LongConsumer;
import java.util.function.Supplier;

/**
 * nexus-wbfpw.48: the runtime backstop under vectors-029's owner policy. Identity-based, never name-based.
 *
 * <p>{@code nexus.chunks} carries one policy that is meant to bind the service role,
 * {@code tenant_isolation}, and (since vectors-029) a permissive {@code SELECT USING (true)} policy for the
 * migrating role, {@code chunks_gate_probe_owner_read}. Permissive policies are OR-ed, so ANY other
 * permissive policy that applies to the role serving tenant traffic lets that role read every tenant's
 * chunks, with no error anywhere (a non-SELECT policy lets it write them too). A policy applies to a role when it names PUBLIC, names the role, or names
 * a role whose privileges the role has ({@code has_privs_of_role}, which honours a grant's INHERIT option).
 *
 * <p>The changeset's precondition keeps the policy from being created for the roles it can see at migration
 * time. It cannot see the role the service actually connects as (that is whatever {@code NX_DB_USER} names,
 * not necessarily {@code nexus_svc}), and it cannot see a membership granted after it ran. This check runs
 * on the SERVICE pool, so {@code current_user} IS the role that serves traffic whatever its name, and asks
 * the policy engine's own question: does any permissive policy on {@code nexus.chunks} other than
 * {@code tenant_isolation} apply to me? {@code Main} refuses to boot if one does, and {@code GET /v1/status}
 * reports the answer for {@code nx doctor}.
 *
 * <p>A role that is SUPERUSER or BYPASSRLS is not bound by row-level security at all, so no policy can widen
 * what it reads and the check passes for it ({@code pg_has_role} answers true for a superuser for every
 * role, which would otherwise read as a violation of every policy). RESTRICTIVE policies only narrow and are
 * not considered.
 *
 * <p><b>The partition tree (RDR-225, nexus-3wh8d.16).</b> {@code nexus.chunks} and
 * {@code nexus.taxonomy_centroids} are LIST-partitioned by {@code embedding_model}, then by {@code tenant_id}.
 * PostgreSQL inherits neither the row-security flags nor the policies down a partition tree, and a leaf can be
 * queried directly, so the parents being right proves nothing about a model partition or a leaf. {@link #structure}
 * therefore asserts, on every relation of both trees, that row security is enabled and forced and that the policy
 * set is the parent's own, and {@link #verifyAtStartup} refuses to serve when one relation fails. The check is
 * about the tables, not the role, so it runs for a SUPERUSER or BYPASSRLS role too.
 *
 * <p>Raw SQL is not allowed in this tree; every catalog read below goes through jOOQ's DSL.
 */
public final class ChunksIsolationCheck {

    private static final Logger log = LoggerFactory.getLogger(ChunksIsolationCheck.class);

    /** The one policy that is meant to bind the service role on nexus.chunks. */
    static final String TENANT_POLICY = "tenant_isolation";

    /** How long {@link #statusSupplier} keeps one answer, so an unauthenticated poller cannot drive catalog reads. */
    static final long STATUS_TTL_MILLIS = 30_000L;

    /** Pauses between the retries of a startup probe that failed for a transient reason (up to three retries). */
    static final long[] STARTUP_BACKOFF_MILLIS = {1_000L, 2_000L, 4_000L};

    private ChunksIsolationCheck() { }

    /**
     * @param policy the permissive policy on nexus.chunks, other than tenant_isolation
     * @param role   the role in the policy's list that applies to the connected role: its name, or
     *               {@code public}
     */
    public record Violation(String policy, String role) {}

    /** Raised when a policy other than tenant_isolation applies to the role serving tenant traffic. */
    public static final class IsolationException extends RuntimeException {
        public IsolationException(String message) { super(message); }
    }

    /**
     * The permissive policies on {@code nexus.chunks}, other than {@code tenant_isolation}, that apply to the
     * role {@code ds} connects as. Empty when none does, and empty for a role RLS does not bind.
     */
    public static List<Violation> violations(DataSource ds) throws SQLException {
        try (Connection conn = ds.getConnection()) {
            return violations(DSL.using(conn, SQLDialect.POSTGRES));
        }
    }

    static List<Violation> violations(DSLContext ctx) {
        var roles = DSL.table(DSL.name("pg_catalog", "pg_roles"));
        var rolname = DSL.field(DSL.name("rolname"), SQLDataType.VARCHAR);
        boolean rlsDoesNotBindMe = ctx.fetchExists(
            DSL.selectOne().from(roles)
                .where(rolname.eq(DSL.currentUser()))
                .and(DSL.condition(DSL.field(DSL.name("rolsuper"), SQLDataType.BOOLEAN))
                    .or(DSL.condition(DSL.field(DSL.name("rolbypassrls"), SQLDataType.BOOLEAN)))));
        if (rlsDoesNotBindMe) {
            return List.of();
        }
        var policies = DSL.table(DSL.name("pg_catalog", "pg_policies"));
        var policyname = DSL.field(DSL.name("policyname"), SQLDataType.VARCHAR);
        var policyRoles = DSL.field(DSL.name("roles"), SQLDataType.VARCHAR.array());
        List<Violation> out = new ArrayList<>();
        for (Record2<String, String[]> row : ctx.select(policyname, policyRoles)
                .from(policies)
                .where(DSL.field(DSL.name("schemaname"), SQLDataType.VARCHAR).eq("nexus"))
                .and(DSL.field(DSL.name("tablename"), SQLDataType.VARCHAR).eq("chunks"))
                .and(DSL.field(DSL.name("permissive"), SQLDataType.VARCHAR).eq("PERMISSIVE"))
                .and(policyname.ne(TENANT_POLICY))
                .orderBy(policyname)
                .fetch()) {
            for (String role : row.value2()) {
                if (appliesToMe(ctx, role)) {
                    out.add(new Violation(row.value1(), role));
                }
            }
        }
        return out;
    }

    private static boolean appliesToMe(DSLContext ctx, String role) {
        if ("public".equals(role)) {
            return true;   // pg_policies reports PUBLIC as the name "public"; no role can be named that
        }
        Boolean has = ctx.select(DSL.function("pg_has_role", SQLDataType.BOOLEAN,
                DSL.currentUser(), DSL.val(role), DSL.inline("USAGE")))
            .fetchOne(0, Boolean.class);
        return Boolean.TRUE.equals(has);
    }

    /** What {@link #verifyAtStartup} says. Names the policy and the role, and both remedies. */
    static String refusal(String connectedRole, List<Violation> violations) {
        var v = violations.get(0);
        return "policy " + v.policy() + " on nexus.chunks applies to role " + connectedRole
            + " (through " + (v.role().equals("public") ? "PUBLIC" : "role " + v.role())
            + "), the role this service connects as, and it is not tenant_isolation: permissive policies are "
            + "OR-ed, so this role would read or write every tenant's chunks (nexus-wbfpw.48)."
            + (violations.size() > 1 ? " " + (violations.size() - 1) + " more violation(s) follow it." : "")
            + " Either drop the policy as the table owner (DROP POLICY " + v.policy()
            + " ON nexus.chunks), or stop " + connectedRole + " inheriting " + v.role()
            + " (REVOKE it, or GRANT ... WITH INHERIT FALSE). For chunks_gate_probe_owner_read, which"
            + " vectors-029 creates for the migrating role, run migrations as a role the service does not"
            + " inherit (NX_DB_ADMIN_URL, NX_DB_ADMIN_USER and NX_DB_ADMIN_PASS), then drop the policy.";
    }

    /**
     * Startup backstop, run AFTER the schema migration on the SERVICE pool. Throws
     * {@link IsolationException} when a policy other than tenant_isolation applies to the connected role.
     * A probe that cannot run is also a refusal: a service that cannot tell whether its tenant isolation holds
     * does not serve. A probe that fails for a transient reason (connection, resources, operator intervention,
     * serialization: SQLState classes 08, 53, 57 and 40001, or a pool timeout) is retried after each pause in
     * {@link #STARTUP_BACKOFF_MILLIS} before it counts as a refusal, so a pooler hiccup does not become a
     * restart storm. A real violation is never retried.
     */
    public static void verifyAtStartup(DataSource ds) {
        verifyAtStartup(() -> probe(ds), STARTUP_BACKOFF_MILLIS, ChunksIsolationCheck::sleepMillis);
    }

    /**
     * What one startup probe saw: the connected role, the violations that apply to it, and the relations of the
     * two partition trees that do not mirror their parent.
     */
    record Probe(String role, List<Violation> found, List<Gap> gaps) {
        /** A probe that saw no structural gap. */
        Probe(String role, List<Violation> found) {
            this(role, found, List.of());
        }
    }

    private static Probe probe(DataSource ds) throws SQLException {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            String me = ctx.select(DSL.currentUser()).fetchOne(0, String.class);
            return new Probe(me, violations(ctx), structure(ctx).gaps());
        }
    }

    private static void sleepMillis(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IsolationException("interrupted while retrying the chunks isolation check");
        }
    }

    static void verifyAtStartup(Callable<Probe> probe, long[] backoffMillis, LongConsumer sleeper) {
        for (int attempt = 0; ; attempt++) {
            Probe seen;
            try {
                seen = probe.call();
            } catch (Exception e) {
                if (attempt < backoffMillis.length && isTransient(e)) {
                    log.warn("event=chunks_isolation_check_retry attempt={} error={}", attempt + 1, e.getMessage());
                    sleeper.accept(backoffMillis[attempt]);
                    continue;
                }
                throw new IsolationException("could not check which policies on nexus.chunks apply to the service role: "
                    + e.getMessage());
            }
            if (!seen.found().isEmpty()) {
                throw new IsolationException(refusal(seen.role(), seen.found()));
            }
            if (!seen.gaps().isEmpty()) {
                log.error("event=chunks_isolation_structure_gaps count={} relations={}", seen.gaps().size(),
                    seen.gaps().stream().map(Gap::relation).toList());
                throw new IsolationException(structureRefusal(seen.gaps()));
            }
            log.info("event=chunks_isolation_check_ok role={}", seen.role());
            return;
        }
    }

    /** True when {@code t}, or anything it wraps or chains, is a connection, resource, operator or serialization failure. */
    static boolean isTransient(Throwable t) {
        int depth = 0;
        for (Throwable c = t; c != null && depth < 8; c = c.getCause(), depth++) {
            if (c instanceof SQLException se) {
                int hops = 0;
                for (SQLException s = se; s != null && hops < 8; s = s.getNextException(), hops++) {
                    if (s instanceof SQLTransientConnectionException || transientState(s.getSQLState())) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    private static boolean transientState(String state) {
        return state != null && (state.startsWith("08") || state.startsWith("53") || state.startsWith("57")
            || state.equals("40001"));
    }

    /** The two partitioned parents and the trees under them. */
    static final List<String> TREE_ROOTS = List.of("chunks", "taxonomy_centroids");

    /** How many gaps a refusal spells out before it counts the rest (the log line carries every relation). */
    static final int REFUSAL_GAPS_SHOWN = 3;

    /** Bind-parameter slice for the policy read, well under PostgreSQL's 32767. */
    private static final int POLICY_READ_SLICE = 4_000;

    /**
     * One relation of a partition tree that does not mirror its parent.
     *
     * @param relation the relation's name in schema nexus
     * @param problem  what is wrong with it, in words
     */
    public record Gap(String relation, String problem) {}

    /**
     * What {@link #structure} found.
     *
     * @param relationsInspected parents, model partitions and leaves read, so a caller can tell a clean tree from
     *                           a scan that saw nothing
     * @param gaps               every relation that failed, parents first, then model partitions, then leaves
     */
    public record StructureReport(int relationsInspected, List<Gap> gaps) {}

    private record Rel(long oid, String name, boolean rowSecurity, boolean forceRowSecurity, Long parent) {}

    private record PolicyShape(String signature) {}

    /**
     * Reads both partition trees and reports every relation whose row-level security does not mirror its root:
     * row security off, FORCE off, or a PERMISSIVE policy set that is not the root's (a policy missing, an extra
     * one, or one whose command, roles or expressions differ; RESTRICTIVE policies only narrow and are not compared). The root itself must carry
     * {@value #TENANT_POLICY}. The trees are found through {@code pg_inherits}, never by name pattern.
     */
    static StructureReport structure(DSLContext ctx) {
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        var i = DSL.table(DSL.name("pg_catalog", "pg_inherits")).as("i");
        var oid = DSL.field(DSL.name("c", "oid")).cast(SQLDataType.BIGINT);
        var relname = DSL.field(DSL.name("c", "relname"), SQLDataType.VARCHAR);
        var inhparent = DSL.field(DSL.name("i", "inhparent")).cast(SQLDataType.BIGINT);
        Map<Long, Rel> byOid = new HashMap<>();
        for (var row : ctx.select(oid, relname,
                    DSL.field(DSL.name("c", "relrowsecurity"), SQLDataType.BOOLEAN),
                    DSL.field(DSL.name("c", "relforcerowsecurity"), SQLDataType.BOOLEAN), inhparent)
                .from(c)
                .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .leftJoin(i).on(DSL.field(DSL.name("i", "inhrelid")).eq(DSL.field(DSL.name("c", "oid"))))
                .where(DSL.field(DSL.name("n", "nspname"), SQLDataType.VARCHAR).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relkind")).cast(SQLDataType.VARCHAR).in("r", "p"))
                .and(relname.in(TREE_ROOTS)
                    .or(DSL.condition(DSL.field(DSL.name("c", "relispartition"), SQLDataType.BOOLEAN))))
                .fetch()) {
            long id = row.value1();
            byOid.put(id, new Rel(id, row.value2(), Boolean.TRUE.equals(row.value3()),
                Boolean.TRUE.equals(row.value4()), row.value5()));
        }
        Map<Long, List<Rel>> childrenOf = new HashMap<>();
        for (Rel r : byOid.values()) {
            if (r.parent() != null) {
                childrenOf.computeIfAbsent(r.parent(), k -> new ArrayList<>()).add(r);
            }
        }
        List<Gap> gaps = new ArrayList<>();
        // root name -> every relation of its tree, root first, then level by level, by name within a level
        Map<String, List<Rel>> trees = new LinkedHashMap<>();
        for (String root : TREE_ROOTS) {
            Rel rootRel = byOid.values().stream()
                .filter(r -> r.parent() == null && r.name().equals(root)).findFirst().orElse(null);
            if (rootRel == null) {
                gaps.add(new Gap(root, "relation not found: row-level security cannot be checked"));
                continue;
            }
            List<Rel> ordered = new ArrayList<>();
            ordered.add(rootRel);
            List<Rel> level = List.of(rootRel);
            while (!level.isEmpty()) {
                List<Rel> next = new ArrayList<>();
                for (Rel p : level) {
                    next.addAll(childrenOf.getOrDefault(p.oid(), List.of()));
                }
                next.sort(Comparator.comparing(Rel::name));
                ordered.addAll(next);
                level = next;
            }
            trees.put(root, ordered);
        }
        Map<String, Map<String, PolicyShape>> policiesOf = readPolicies(ctx, trees.values().stream()
            .flatMap(List::stream).map(Rel::name).toList());
        int inspected = 0;
        for (var tree : trees.entrySet()) {
            String root = tree.getKey();
            Map<String, PolicyShape> rootPolicies = policiesOf.getOrDefault(root, Map.of());
            for (Rel r : tree.getValue()) {
                inspected++;
                if (!r.rowSecurity()) {
                    gaps.add(new Gap(r.name(), "row-level security is not enabled"));
                }
                if (!r.forceRowSecurity()) {
                    gaps.add(new Gap(r.name(), "FORCE ROW LEVEL SECURITY is not set, so the table owner is not bound"));
                }
                Map<String, PolicyShape> mine = policiesOf.getOrDefault(r.name(), Map.of());
                if (r.name().equals(root)) {
                    if (!mine.containsKey(TENANT_POLICY)) {
                        gaps.add(new Gap(r.name(), "the tenant policy " + TENANT_POLICY + " is missing"));
                    }
                    continue;
                }
                for (var want : rootPolicies.entrySet()) {
                    PolicyShape have = mine.get(want.getKey());
                    if (have == null) {
                        gaps.add(new Gap(r.name(), "policy " + want.getKey() + " of nexus." + root + " is missing"));
                    } else if (!have.equals(want.getValue())) {
                        gaps.add(new Gap(r.name(), "policy " + want.getKey() + " differs from nexus." + root + "'s"));
                    }
                }
                for (String extra : mine.keySet()) {
                    if (!rootPolicies.containsKey(extra)) {
                        gaps.add(new Gap(r.name(), "policy " + extra + " is not on nexus." + root));
                    }
                }
            }
        }
        return new StructureReport(inspected, gaps);
    }

    /** Relation name -> policy name -> the policy's shape, for every named relation of schema nexus. */
    private static Map<String, Map<String, PolicyShape>> readPolicies(DSLContext ctx, List<String> relations) {
        var tablename = DSL.field(DSL.name("tablename"), SQLDataType.VARCHAR);
        var policyname = DSL.field(DSL.name("policyname"), SQLDataType.VARCHAR);
        var roles = DSL.field(DSL.name("roles"), SQLDataType.VARCHAR.array());
        var cmd = DSL.field(DSL.name("cmd"), SQLDataType.VARCHAR);
        var permissive = DSL.field(DSL.name("permissive"), SQLDataType.VARCHAR);
        var qual = DSL.field(DSL.name("qual"), SQLDataType.VARCHAR);
        var check = DSL.field(DSL.name("with_check"), SQLDataType.VARCHAR);
        Map<String, Map<String, PolicyShape>> out = new HashMap<>();
        for (int from = 0; from < relations.size(); from += POLICY_READ_SLICE) {
            var slice = relations.subList(from, Math.min(relations.size(), from + POLICY_READ_SLICE));
            for (var row : ctx.select(tablename, policyname, permissive, cmd, roles, qual, check)
                    .from(DSL.table(DSL.name("pg_catalog", "pg_policies")))
                    .where(DSL.field(DSL.name("schemaname"), SQLDataType.VARCHAR).eq("nexus"))
                    .and(tablename.in(slice))
                    // RESTRICTIVE policies only narrow what a relation shows, so one missing from, added to or
                    // different on a child cannot widen what a tenant reads: not part of the mirror.
                    .and(permissive.eq("PERMISSIVE"))
                    .fetch()) {
                String[] r = row.value5() == null ? new String[0] : row.value5().clone();
                java.util.Arrays.sort(r);
                String signature = String.join("\001", row.value3(), row.value4(), String.join(",", r),
                    String.valueOf(row.value6()), String.valueOf(row.value7()));
                out.computeIfAbsent(row.value1(), k -> new HashMap<>()).put(row.value2(), new PolicyShape(signature));
            }
        }
        return out;
    }

    /** What {@link #verifyAtStartup} says for a tree that does not mirror its parent. Names the first gaps and the remedy. */
    static String structureRefusal(List<Gap> gaps) {
        var shown = new StringBuilder();
        for (int k = 0; k < Math.min(REFUSAL_GAPS_SHOWN, gaps.size()); k++) {
            if (k > 0) {
                shown.append("; ");
            }
            shown.append("nexus.").append(gaps.get(k).relation()).append(": ").append(gaps.get(k).problem());
        }
        return "row-level security is not intact on the partition tree of nexus.chunks or nexus.taxonomy_centroids: "
            + shown
            + (gaps.size() > REFUSAL_GAPS_SHOWN ? "; " + (gaps.size() - REFUSAL_GAPS_SHOWN) + " more gap(s) follow" : "")
            + ". PostgreSQL inherits neither the row-security flags nor the policies down a partition tree, and a "
            + "model partition or leaf can be queried directly, so this service does not serve tenant traffic until "
            + "every relation mirrors its parent (RDR-225). As the table owner, re-mirror each parent onto its "
            + "whole tree: SELECT nexus.partition_sync_access('nexus.chunks'::regclass) and the same for "
            + "nexus.taxonomy_centroids (after putting right any policy that is wrong on the PARENT, which the "
            + "function copies from).";
    }

    private static boolean intact(DataSource ds) throws SQLException {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            return violations(ctx).isEmpty() && structure(ctx).gaps().isEmpty();
        }
    }

    /**
     * What {@code GET /v1/status} reports as {@code chunks_tenant_isolation_intact}: no other permissive policy
     * applies to the connected role AND every relation of the nexus.chunks and nexus.taxonomy_centroids partition
     * trees (parents, model partitions, leaves) still has row security enabled and forced and the parent's policies
     * ({@link #structure}). Asked live (the boot check cannot see a grant made later) but never on the request thread:
     * {@code get()} returns the last answer, or null (the field is omitted) before the first one or when the
     * probe could not run, and starts at most one background refresh once the answer is older than
     * {@link #STATUS_TTL_MILLIS}. A status request therefore never waits for a pool connection, which is
     * exactly when a saturated pool makes the status endpoint most wanted. A boolean only: the route is
     * unauthenticated, so it never names a policy or a role.
     */
    public static Supplier<Boolean> statusSupplier(DataSource ds, Clock clock) {
        var pool = new ThreadPoolExecutor(0, 1, 30, TimeUnit.SECONDS, new LinkedBlockingQueue<>(), r -> {
            Thread t = new Thread(r, "chunks-isolation-status");
            t.setDaemon(true);
            return t;
        });
        return statusSupplier(ds, clock, pool);
    }

    static Supplier<Boolean> statusSupplier(DataSource ds, Clock clock, Executor refresher) {
        return new Supplier<>() {
            private record Snapshot(long computedAt, Boolean value) {}

            private volatile Snapshot snapshot = new Snapshot(Long.MIN_VALUE, null);
            private final AtomicBoolean refreshing = new AtomicBoolean();

            {
                refresh(clock.millis());   // prime it, so the first poll after boot usually has an answer
            }

            @Override
            public Boolean get() {
                Snapshot s = snapshot;
                long now = clock.millis();
                if (s.computedAt() == Long.MIN_VALUE || now - s.computedAt() >= STATUS_TTL_MILLIS) {
                    refresh(now);
                }
                return s.value();
            }

            private void refresh(long startedAt) {
                if (!refreshing.compareAndSet(false, true)) {
                    return;
                }
                try {
                    refresher.execute(() -> {
                        try {
                            Boolean v;
                            try {
                                v = intact(ds);
                            } catch (SQLException | RuntimeException e) {
                                log.warn("event=chunks_isolation_status_unavailable error={}", e.getMessage());
                                v = null;
                            }
                            snapshot = new Snapshot(startedAt, v);
                        } finally {
                            refreshing.set(false);
                        }
                    });
                } catch (RuntimeException e) {
                    refreshing.set(false);
                    log.warn("event=chunks_isolation_status_refresh_rejected error={}", e.getMessage());
                }
            }
        };
    }
}
