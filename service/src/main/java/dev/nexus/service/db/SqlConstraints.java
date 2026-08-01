// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.sql.SQLException;

/**
 * Which unique key a PostgreSQL integrity violation actually named (nexus-0ehwe
 * arbiter class: nexus-pbawi / nexus-jq53b / nexus-z3ssg).
 *
 * <p><strong>Why this exists.</strong> An {@code INSERT ... ON CONFLICT} takes
 * exactly ONE conflict target — verified against PostgreSQL 17, where naming two
 * targets in one statement is a hard <em>syntax</em> error, not a runtime one. So on
 * any table carrying more than one unique key, every key the arbiter does NOT name is
 * an unhandled {@code 23505} path, and the ONLY way to tell those paths apart at
 * runtime is the constraint name the server reports. Discarding it — which is what
 * every call site did before nexus-0ehwe item 6 — turns three distinct defects into
 * one indistinguishable "integrity constraint violation".
 *
 * <p><strong>The rule this supports.</strong> An arbiter must be chosen against the
 * target table's COMPLETE unique-key set, not against the one key the author had in
 * mind. A key whose values are SERVER-generated (a {@code BIGSERIAL} surrogate id)
 * is exempt, because it cannot collide; every CALLER-determined key is exposed.
 *
 * <p>Lives in the {@code db} package rather than beside the HTTP mapper so both the
 * repository layer (which must decide converge-vs-refuse) and {@code HttpUtil} (which
 * must render the 409) share ONE extraction instead of two drifting copies.
 */
public final class SqlConstraints {

    /** PostgreSQL SQLSTATE for {@code unique_violation}. */
    public static final String UNIQUE_VIOLATION = "23505";

    private SqlConstraints() {
    }

    /**
     * The violated constraint's name, walking the cause chain, or {@code null}.
     *
     * <p>PostgreSQL reports it in {@code ServerErrorMessage}; the JDBC driver exposes
     * it on {@code PSQLException}. Reflection keeps this file free of a hard driver
     * import for one optional field — a null simply omits the name rather than
     * degrading the caller.
     *
     * <p>jOOQ wraps the driver exception in a {@code DataAccessException} and
     * {@code TenantScope} may wrap that again, so the violation is a CAUSE, not the
     * top-level throwable. The walk is depth-bounded to tolerate a malformed
     * (self- or mutually-referential) cause chain.
     */
    public static String violated(Throwable e) {
        for (Throwable t = e; t != null; t = t.getCause()) {
            try {
                var m = t.getClass().getMethod("getServerErrorMessage");
                Object sem = m.invoke(t);
                if (sem != null) {
                    Object name = sem.getClass().getMethod("getConstraint").invoke(sem);
                    if (name instanceof String str && !str.isBlank()) return str;
                }
            } catch (ReflectiveOperationException | RuntimeException ignored) {
                // not a PSQLException, or the driver shape changed — fall through
            }
        }
        return null;
    }

    /** True if a {@code 23505} unique violation appears anywhere in {@code t}'s cause chain. */
    public static boolean isUniqueViolation(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof SQLException se && UNIQUE_VIOLATION.equals(se.getSQLState())) {
                return true;
            }
        }
        return false;
    }

    /**
     * True if {@code t} is a {@code 23505} naming one of {@code constraints}.
     *
     * <p>Both halves are load-bearing. Matching the SQLSTATE alone would also catch a
     * violation of some OTHER key; matching the name alone would catch a non-unique
     * class-23 failure (a not-null or FK violation) that happens to name the same
     * relation.
     */
    public static boolean violatesAny(Throwable t, String... constraints) {
        if (!isUniqueViolation(t)) return false;
        String actual = violated(t);
        if (actual == null) return false;
        for (String c : constraints) {
            if (actual.equals(c)) return true;
        }
        return false;
    }
}
