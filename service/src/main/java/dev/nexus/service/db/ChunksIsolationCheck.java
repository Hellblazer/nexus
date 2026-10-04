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
import java.util.List;
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

    /** What one startup probe saw: the connected role and the violations that apply to it. */
    record Probe(String role, List<Violation> found) {}

    private static Probe probe(DataSource ds) throws SQLException {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            String me = ctx.select(DSL.currentUser()).fetchOne(0, String.class);
            return new Probe(me, violations(ctx));
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

    /**
     * Whether RLS is still wired on {@code nexus.chunks} at all: row security enabled and forced, and the
     * tenant policy present. A status-field concern only; the boot refusal never asks it, so it adds no way to
     * brick a start.
     */
    static boolean rlsStructureIntact(DSLContext ctx) {
        var cls = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        boolean wired = ctx.fetchExists(
            DSL.selectOne().from(cls).join(ns)
                .on(DSL.field(DSL.name("c", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
                .where(DSL.field(DSL.name("n", "nspname"), SQLDataType.VARCHAR).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relname"), SQLDataType.VARCHAR).eq("chunks"))
                .and(DSL.condition(DSL.field(DSL.name("c", "relrowsecurity"), SQLDataType.BOOLEAN)))
                .and(DSL.condition(DSL.field(DSL.name("c", "relforcerowsecurity"), SQLDataType.BOOLEAN))));
        if (!wired) {
            return false;
        }
        return ctx.fetchExists(
            DSL.selectOne().from(DSL.table(DSL.name("pg_catalog", "pg_policies")))
                .where(DSL.field(DSL.name("schemaname"), SQLDataType.VARCHAR).eq("nexus"))
                .and(DSL.field(DSL.name("tablename"), SQLDataType.VARCHAR).eq("chunks"))
                .and(DSL.field(DSL.name("policyname"), SQLDataType.VARCHAR).eq(TENANT_POLICY)));
    }

    private static boolean intact(DataSource ds) throws SQLException {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            return violations(ctx).isEmpty() && rlsStructureIntact(ctx);
        }
    }

    /**
     * What {@code GET /v1/status} reports as {@code chunks_tenant_isolation_intact}: no other permissive policy
     * applies to the connected role AND row security on nexus.chunks is still enabled, forced and carries the
     * tenant policy. Asked live (the boot check cannot see a grant made later) but never on the request thread:
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
