/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.Set;

/**
 * Transaction-scoped PostgreSQL session settings — the ONE sanctioned home
 * for {@code SET LOCAL}-shaped statements (nexus-xtmtf).
 *
 * <p>{@code SET LOCAL x = y} has no jOOQ DSL form, but its exact equivalent
 * {@code SELECT set_config('x', 'y', true)} does: a plain function call with
 * real bind parameters, no string-concatenated SQL. Every repository that
 * needs a transaction-local GUC (HNSW iterative scan, trigram similarity
 * threshold) routes through {@link #setLocal}; the gate test
 * ({@code RawSqlGateTest}) forbids {@code ctx.execute(} string-SQL anywhere
 * in {@code service/src/main}, this class included — there is nothing raw
 * left to sanction.
 *
 * <p>The GUC name is validated against a closed whitelist. set_config binds
 * the name as a parameter so injection is structurally impossible, but an
 * unknown GUC at this layer is a programming error worth failing loudly on
 * rather than shipping to Postgres.
 */
public final class PgSession {

    /** GUCs the service is allowed to set transaction-locally. */
    private static final Set<String> ALLOWED_GUCS = Set.of(
        "hnsw.iterative_scan",
        "pg_trgm.word_similarity_threshold"
    );

    private PgSession() {
    }

    /**
     * Set a transaction-local GUC ({@code SET LOCAL} semantics) via
     * {@code set_config(name, value, is_local=true)}.
     *
     * <p>Must be called inside a transaction (jOOQ {@code transaction(...)} /
     * TenantScope block) — set_config with {@code is_local=true} outside a
     * transaction is a silent no-op, same as SET LOCAL.
     *
     * @param ctx   transaction-bound DSL context
     * @param guc   GUC name; must be whitelisted in {@link #ALLOWED_GUCS}
     * @param value value to set for the remainder of the transaction
     * @throws IllegalArgumentException on a non-whitelisted GUC
     */
    public static void setLocal(DSLContext ctx, String guc, String value) {
        if (!ALLOWED_GUCS.contains(guc)) {
            throw new IllegalArgumentException(
                "GUC '" + guc + "' is not whitelisted for SET LOCAL (allowed: "
                + ALLOWED_GUCS + ")");
        }
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(guc), DSL.val(value), DSL.inline(true)))
           .fetch();
    }
}
