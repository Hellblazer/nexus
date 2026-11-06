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
import java.sql.SQLException;
import java.util.Optional;
import java.util.concurrent.Callable;

/**
 * nexus-b1adx: the engine refuses to boot when the role it serves traffic as has {@code BYPASSRLS}.
 *
 * <p>{@code nexus.partition_copy_access} (vectors-039-1) skips every grantee of the parent whose {@code pg_roles}
 * row has {@code rolbypassrls}, so a leaf or model partition made after that changeset carries no grant for such a
 * grantee. That is right for {@code nexus_diag}. It assumes the service role is NOBYPASSRLS, as provisioning makes
 * it, and on a bring-your-own or managed Postgres nothing else says so: every tenant or model created afterwards
 * would get leaves with no DML for the service role, and the first write to one would fail with {@code permission
 * denied}. A BYPASSRLS service role also takes the row-level security that isolates tenants out of the picture
 * altogether. The fix is the role's, not the function's (no role-name carve-out in {@code partition_copy_access}).
 *
 * <p><b>Why a class of its own.</b> {@link ChunksIsolationCheck#violations} passes a SUPERUSER or BYPASSRLS role on
 * purpose: its question is whether a POLICY widens what a role-bound role reads, and for a role RLS does not bind no
 * policy can. That contract is pinned and consumed by {@code GET /v1/status} ({@code chunks_tenant_isolation_intact})
 * and the doctor, so this refusal is not folded into it: it asks a different question (is the role the kind this
 * schema's grants are written for) and would make the status boolean mean two things.
 *
 * <p><b>Superusers pass, with a WARN.</b> A SUPERUSER needs no grant, so the missing-DML failure cannot happen to
 * one, and single-role development setups (the bootstrap superuser as {@code NX_DB_USER}, which carries
 * {@code rolbypassrls} too) boot as they did. RLS does not bind a superuser either, so the pass logs
 * {@code event=service_role_superuser_isolation_not_enforced} ({@link #superuserWarning}). The refusal is for the role that is BYPASSRLS and not a superuser, the one that fails.
 *
 * <p>Runs on the SERVICE pool, so {@code current_user} IS the role that serves traffic whatever it is called, after
 * the schema migration. Raw SQL is not allowed in this tree; the catalog read goes through jOOQ's DSL.
 */
public final class ServiceRoleBypassRlsCheck {

    private static final Logger log = LoggerFactory.getLogger(ServiceRoleBypassRlsCheck.class);

    private ServiceRoleBypassRlsCheck() { }

    /** The attributes of the connected role that the decision reads. */
    public record RoleFacts(String role, boolean superuser, boolean bypassRls) {}

    /** Raised when the service role has BYPASSRLS and is not a superuser, or its attributes cannot be read. */
    public static final class BypassRlsException extends RuntimeException {
        public BypassRlsException(String message) { super(message); }
    }

    /** The refusal text for {@code facts}; empty when the role passes. */
    static Optional<String> refusal(RoleFacts facts) {
        if (!facts.bypassRls() || facts.superuser()) {
            return Optional.empty();
        }
        String r = facts.role();
        return Optional.of("the role this service connects as, " + r + ", has BYPASSRLS. nexus.partition_copy_access"
            + " (vectors-039-1) skips BYPASSRLS grantees, so every tenant leaf and model partition created from now"
            + " on would carry no DML grant for " + r + " and the first write to one would fail with permission"
            + " denied; row-level security, which isolates tenants, would not bind " + r + " either (nexus-b1adx)."
            + " Fix: as a superuser run ALTER ROLE " + r + " NOBYPASSRLS, then restart the engine. A role that"
            + " needs BYPASSRLS for diagnostics is nexus_diag, not the service role. See "
            + "https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md#storage-service-postgres-prerequisites");
    }

    /**
     * The warning for a superuser service role; empty for any other. A superuser passes the BYPASSRLS refusal
     * (it needs no grant) but row-level security does not bind it either, whether or not {@code rolbypassrls}
     * is set, so a deployment serving as one has no tenant isolation.
     */
    static Optional<String> superuserWarning(RoleFacts facts) {
        if (!facts.superuser()) {
            return Optional.empty();
        }
        return Optional.of("the role this service connects as, " + facts.role() + ", is a superuser: tenant isolation"
            + " is not enforced, because row-level security does not bind a superuser. Use a NOSUPERUSER NOBYPASSRLS"
            + " service role for anything but a single-role development setup (nexus-b1adx).");
    }

    /**
     * Reads the connected role's attributes through {@code reader} and throws {@link BypassRlsException} when
     * {@link #refusal} says so, or when they cannot be read: a service that cannot tell what role it is does not
     * serve.
     */
    static RoleFacts verify(Callable<RoleFacts> reader) {
        RoleFacts facts;
        try {
            facts = reader.call();
        } catch (Exception e) {
            throw new BypassRlsException("could not read the service role's attributes from pg_roles to check that it"
                + " is NOBYPASSRLS: " + e.getMessage());
        }
        Optional<String> refusal = refusal(facts);
        if (refusal.isPresent()) {
            throw new BypassRlsException(refusal.get());
        }
        superuserWarning(facts).ifPresent(w ->
            log.warn("event=service_role_superuser_isolation_not_enforced role={} error=\"{}\"", facts.role(), w));
        log.info("event=service_role_bypassrls_check_ok role={} superuser={} bypassrls={}",
            facts.role(), facts.superuser(), facts.bypassRls());
        return facts;
    }

    /** {@link #verify} for the role {@code ds} connects as. Run it after the schema migration. */
    public static RoleFacts verifyAtStartup(DataSource ds) {
        return verify(() -> read(ds));
    }

    static RoleFacts read(DataSource ds) throws SQLException {
        try (Connection conn = ds.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            var rolname = DSL.field(DSL.name("rolname"), SQLDataType.VARCHAR);
            var row = ctx.select(rolname,
                    DSL.field(DSL.name("rolsuper"), SQLDataType.BOOLEAN),
                    DSL.field(DSL.name("rolbypassrls"), SQLDataType.BOOLEAN))
                .from(DSL.table(DSL.name("pg_catalog", "pg_roles")))
                .where(rolname.eq(DSL.currentUser()))
                .fetchOne();
            if (row == null) {
                throw new SQLException("current_user has no pg_roles row");
            }
            return new RoleFacts(row.value1(), Boolean.TRUE.equals(row.value2()), Boolean.TRUE.equals(row.value3()));
        }
    }
}
