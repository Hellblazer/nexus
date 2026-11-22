// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.Callable;
import java.util.function.LongConsumer;

/**
 * nexus-f3yqd.2.16 (RDR-246, Row-level security): the boot backstop under a data move's owner-read policy, for every
 * driving table in {@code nexus.data_moves} other than {@code nexus.chunks} ({@link ChunksIsolationCheck} covers that
 * one).
 *
 * <p>A move over a tenant-scoped table reads every tenant's rows as the migrating role, which needs a permissive
 * {@code SELECT ... USING (true)} policy {@code TO CURRENT_USER} on the table, added by a changeset of its own with
 * vectors-029's guard (the pattern is {@code service/src/test/resources/datamove/owner-read-policy/}). The guard sees the
 * roles that exist when the changeset runs; it cannot see a membership granted afterwards, and a role that then inherits
 * the migrating role reads every tenant's rows of that table through the policy, with no error anywhere. This check asks
 * the policy engine's own question ({@link ChunksIsolationCheck#violations(DSLContext, String, String)}) of every driving
 * table: does a permissive policy other than {@code tenant_isolation} apply to the role serving traffic?
 *
 * <p>The tables come from {@code nexus.data_moves}, read on the ADMIN connection ({@code nexus_svc} holds no grant on
 * it); the question is asked on the SERVICE pool, so {@code current_user} is the role that serves traffic, whatever
 * {@code NX_DB_USER} names. Every row counts, whatever its state: a completed move's table keeps its owner-read policy
 * until a changeset drops it, and stays in scope until then. A table that no longer exists has no policy and passes. The
 * list is read at boot, after the migration, which is the only time a changeset registers a move.
 *
 * <p><b>Scope.</b> The pattern, and so this check, is for a table with one set of policies on its parent. It does not
 * apply to a table partitioned one tenant per leaf (the shape of {@code nexus.chunks} and
 * {@code nexus.taxonomy_centroids}): that needs {@code nexus.partition_sync_access} so each leaf carries the policy, and a
 * walk of the leaves, which {@link ChunksIsolationCheck#structure} does for the two trees it knows (it checks that the
 * tenant policy exists and that every leaf mirrors its root; it does not ask whether a policy reaches the serving role).
 * A move over {@code nexus.taxonomy_centroids} still gets the parent's question from this check. A non-chunks table partitioned some other way gets its policy on the parent only, because the worker runs
 * such a move per tenant over the parent.
 *
 * <p>Like the chunks check, a probe that cannot run is a refusal, and a transient failure (SQLState classes 08, 53, 57,
 * 40001, a pool timeout) is retried after each pause in {@link ChunksIsolationCheck#STARTUP_BACKOFF_MILLIS} first. A
 * role RLS does not bind (SUPERUSER, BYPASSRLS) is never a violation.
 *
 * <p>The live status route ({@code chunks_tenant_isolation_intact}) is not extended: it asks about nexus.chunks and its
 * partition trees only, and a grant made after boot to a role on a driving table is caught at the next boot.
 */
public final class DrivingTableIsolationCheck {

    private static final Logger log = LoggerFactory.getLogger(DrivingTableIsolationCheck.class);

    private static final int ADMIN_CONNECT_SECONDS = 15;
    private static final int ADMIN_SOCKET_SECONDS = 60;

    private DrivingTableIsolationCheck() { }

    /** A driving table of a registered move. */
    public record Table(String schema, String name) {
        String label() {
            return schema + "." + name;
        }
    }

    /**
     * One permissive policy, other than {@code tenant_isolation}, that applies to the role serving traffic.
     *
     * @param table  the driving table the policy is on
     * @param policy its name
     * @param role   the role in its list that applies to the connected role: its name, or {@code public}
     */
    public record Violation(Table table, String policy, String role) { }

    /** Raised when such a policy applies to the serving role, or the probe cannot tell. */
    public static final class IsolationException extends RuntimeException {
        public IsolationException(String message) {
            super(message);
        }
    }

    /**
     * Every distinct driving table {@code nexus.data_moves} names, in any state, except {@code nexus.chunks},
     * which {@link ChunksIsolationCheck} asks about; ordered by
     * schema, then name. Read on the admin connection.
     */
    public static List<Table> registeredDrivingTables(DSLContext admin) {
        var schema = DSL.field(DSL.name("driving_schema"), SQLDataType.VARCHAR);
        var table = DSL.field(DSL.name("driving_table"), SQLDataType.VARCHAR);
        List<Table> out = new ArrayList<>();
        for (var row : admin.selectDistinct(schema, table).from(DSL.table(DSL.name("nexus", "data_moves")))
                .orderBy(schema, table).fetch()) {
            Table t = new Table(row.value1(), row.value2());
            if (!coveredByChunksCheck(t)) {
                out.add(t);
            }
        }
        // tables a registered move's verification reads under an owner-read policy of their own, though they drive no move
        var label = DSL.field(DSL.name("label"), SQLDataType.VARCHAR);
        for (var support : SUPPORT_TABLES) {
            boolean registered = admin.fetchExists(DSL.table(DSL.name("nexus", "data_moves")),
                DSL.condition(DSL.function("starts_with", SQLDataType.BOOLEAN, label, DSL.val(support.labelPrefix()))));
            if (registered && !out.contains(support.table())) {
                out.add(support.table());
            }
        }
        return out;
    }

    /** A table a registered move reads under an owner-read policy of its own, keyed by the label prefix of the moves that read it. */
    record SupportTable(String labelPrefix, Table table) { }

    /**
     * Tables that carry an owner-read policy for a move's VERIFICATION and drive no move, by the label prefix of the moves that
     * read them (a prefix may name several tables). RDR-229 P2.10 (the policy is {@code vectors-065}'s, RDR-229 D2): the three
     * residue-repair verifications join {@code nexus.catalog_collections} for the content type, and the table-wide verification
     * (the backstop for a tenant the list missed) runs as the migrating role with no tenant, which under FORCE row level
     * security sees no collection without it. RDR-229 owner-is-source: the re-own move's verification reads
     * {@code nexus.catalog_owners} (the curator test, the repo owners) and {@code nexus.gc_audit} (the skip reports), whose
     * policies are {@code vectors-067-2} and {@code vectors-067-3}. RDR-229 P3.3c: the chunks swap's verification reads
     * {@code nexus.catalog_collections} (the anti-join of the composite foreign key's validation, the warm-up), whose policy is {@code vectors-065}'s. Each policy is as exposed as a driving table's, so it gets the
     * same boot question while any of those moves is registered, in any state.
     */
    static final java.util.List<SupportTable> SUPPORT_TABLES = java.util.List.of(
        new SupportTable("rdr229-residue-", new Table("nexus", "catalog_collections")),
        new SupportTable("rdr229-reown-", new Table("nexus", "catalog_owners")),
        new SupportTable("rdr229-reown-", new Table("nexus", "gc_audit")),
        new SupportTable("rdr229-swap-", new Table("nexus", "catalog_collections")));

    /**
     * {@code nexus.chunks}: {@link ChunksIsolationCheck#violations(DSLContext)} asks the policy question of it alone. Its
     * {@code structure} check also walks {@code nexus.taxonomy_centroids}, but only for the tenant policy and the leaves
     * mirroring the root, so a centroid driving table is asked here.
     */
    private static boolean coveredByChunksCheck(Table t) {
        return t.schema().equals("nexus") && t.name().equals("chunks");
    }

    /** The policies that apply to the role {@code service} is connected as, on each of {@code tables}, tables in order. */
    public static List<Violation> violations(DSLContext service, List<Table> tables) {
        List<Violation> out = new ArrayList<>();
        for (Table t : tables) {
            for (var v : ChunksIsolationCheck.violations(service, t.schema(), t.name())) {
                out.add(new Violation(t, v.policy(), v.role()));
            }
        }
        return out;
    }

    /** What a refusal says: the table, the policy and the role it reaches, both remedies. */
    static String refusal(String connectedRole, List<Violation> violations) {
        var v = violations.get(0);
        boolean everyone = v.role().equals("public");
        return "policy " + v.policy() + " on " + v.table().label() + " applies to role " + connectedRole
            + " (through " + (everyone ? "PUBLIC" : "role " + v.role())
            + "), the role this service connects as, and it is not tenant_isolation: permissive policies are "
            + "OR-ed, so this role would read every tenant's rows of the table a data move runs over (nexus-f3yqd.2.16)."
            + (violations.size() > 1 ? " " + (violations.size() - 1) + " more violation(s) follow it." : "")
            + (everyone
                ? " The policy names PUBLIC, so no membership can be revoked: drop it as the table owner (DROP POLICY "
                    + v.policy() + " ON " + v.table().label() + "), or re-scope it to the migrating role."
                : " Either drop the policy as the table owner (DROP POLICY " + v.policy() + " ON " + v.table().label()
                    + "), or stop " + connectedRole + " inheriting " + v.role()
                    + " (REVOKE it, or GRANT ... WITH INHERIT FALSE).")
            + " The owner-read policy of a data move is created for the migrating role: run migrations as a role the "
            + "service does not inherit (NX_DB_ADMIN_URL, NX_DB_ADMIN_USER and NX_DB_ADMIN_PASS).";
    }

    /**
     * Startup backstop, run AFTER the schema migration: reads the driving tables on {@code admin}, asks the policy
     * question of each on {@code service}. Throws {@link IsolationException} on a violation, or when the probe cannot
     * run (after the transient retries).
     */
    public static void verifyAtStartup(DataSource service, AdminConnection admin) {
        verifyAtStartup(() -> probe(service, admin), ChunksIsolationCheck.STARTUP_BACKOFF_MILLIS,
            DrivingTableIsolationCheck::sleepMillis);
    }

    /**
     * What one startup probe saw: the connected role, how many tables it asked about, and {@link #refusal} of the
     * violations it found, or null when there were none.
     */
    record Probe(String role, int tables, String refusal) { }

    private static Probe probe(DataSource service, AdminConnection admin) throws SQLException {
        List<Table> tables;
        Properties props = new Properties();
        props.setProperty("user", admin.user());
        props.setProperty("password", admin.password());
        props.setProperty("ApplicationName", "nexus-driving-table-isolation-check");
        props.setProperty("connectTimeout", Integer.toString(ADMIN_CONNECT_SECONDS));
        props.setProperty("loginTimeout", Integer.toString(ADMIN_CONNECT_SECONDS));
        props.setProperty("socketTimeout", Integer.toString(ADMIN_SOCKET_SECONDS));
        try (Connection conn = DriverManager.getConnection(admin.url(), props)) {
            tables = registeredDrivingTables(DSL.using(conn, SQLDialect.POSTGRES));
        }
        try (Connection conn = service.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            String me = ctx.select(DSL.currentUser()).fetchOne(0, String.class);
            List<Violation> found = violations(ctx, tables);
            return new Probe(me, tables.size(), found.isEmpty() ? null : refusal(me, found));
        }
    }

    static void verifyAtStartup(Callable<Probe> probe, long[] backoffMillis, LongConsumer sleeper) {
        for (int attempt = 0; ; attempt++) {
            Probe seen;
            try {
                seen = probe.call();
            } catch (Exception e) {
                if (attempt < backoffMillis.length && ChunksIsolationCheck.isTransient(e)) {
                    log.warn("event=driving_table_isolation_check_retry attempt={} error={}", attempt + 1, e.getMessage());
                    sleeper.accept(backoffMillis[attempt]);
                    continue;
                }
                throw new IsolationException("could not check which policies on the driving tables of the registered data "
                    + "moves apply to the service role: " + e.getMessage());
            }
            if (seen.refusal() != null) {
                throw new IsolationException(seen.refusal());
            }
            log.info("event=driving_table_isolation_check_ok role={} tables={}", seen.role(), seen.tables());
            return;
        }
    }

    private static void sleepMillis(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IsolationException("interrupted while retrying the driving-table isolation check");
        }
    }
}
