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
        "hnsw.ef_search",
        "pg_trgm.word_similarity_threshold",
        "statement_timeout"
    );

    /**
     * pgvector's hard bound on {@code hnsw.ef_search} (range 1..1000);
     * {@link #efSearchFor} clamps to it — Postgres rejects a set above it.
     */
    static final int EF_SEARCH_MAX = 1000;

    /**
     * Serving floor for {@code hnsw.ef_search} (nexus-4ktfm; design of record
     * T2 nexus/design-4ktfm-hnsw-crowding-remedy). The chunks HNSW index is
     * ONE index across all tenants with RLS filtering AFTER the scan, so at
     * pgvector's default ef_search=40 another tenant's insert near the query
     * displaces this tenant's true neighbors from the bounded traversal
     * (measured live: conexus-szjl, v0.1.92 STEP-6 — one 2026-08-30 insert
     * crowded the gate tenant's true top-2 out). {@code iterative_scan}
     * cannot recover them: it is starvation-triggered and scans OUTWARD from
     * the frontier; neighbors pruned by the ef-bounded traversal are gone.
     * Only a larger candidate list finds them — 200 is 5x the default
     * (the measured failure was marginal: a single insert displaced the
     * top-2, i.e. the boundary sat right at 40).
     */
    static final int DEFAULT_EF_SEARCH_FLOOR = 200;

    /**
     * Env-resolved floor ({@code NX_HNSW_EF_SEARCH}) so the managed cloud can
     * tune serving recall without an engine release (same precedent as
     * {@code NX_DATA_TOKEN_TTL_CEILING_SECONDS}). Read once at class load;
     * a malformed or out-of-range value fails loud at first use.
     */
    private static final int EF_SEARCH_FLOOR =
        efSearchFloor(System.getenv("NX_HNSW_EF_SEARCH"));

    /**
     * Server-side bound on a vector-ranked statement (nexus-g17tf). Sized to
     * the edge's 30s time-to-first-byte budget: a bound LONGER than the edge's
     * guarantees the client has already given up while the backend keeps
     * burning CPU and holding its snapshot -- the measured shape was a search
     * backend running 8.9h after its container was removed, pinning xmin so
     * autovacuum reclaimed nothing database-wide. A CPU-bound backend never
     * notices a dead client between socket writes; only the timer reaches it.
     */
    static final int DEFAULT_SEARCH_STATEMENT_TIMEOUT_MS = 30_000;

    /** Upper bound on the override: past this the edge has long since 504'd. */
    static final int SEARCH_STATEMENT_TIMEOUT_MAX_MS = 600_000;

    /**
     * Env-resolved bound ({@code NX_SEARCH_STATEMENT_TIMEOUT_MS}), same
     * precedent as {@link #EF_SEARCH_FLOOR}: read once at class load,
     * validated at boot by {@link #startupSearchStatementTimeoutMs()}.
     */
    private static final int SEARCH_STATEMENT_TIMEOUT_MS =
        searchStatementTimeoutMs(System.getenv("NX_SEARCH_STATEMENT_TIMEOUT_MS"));

    private PgSession() {
    }

    /**
     * Parse the {@code NX_SEARCH_STATEMENT_TIMEOUT_MS} override. Null/blank
     * means the default; anything else must be an integer in
     * [1, {@link #SEARCH_STATEMENT_TIMEOUT_MAX_MS}]. Zero is refused
     * explicitly: to Postgres {@code statement_timeout=0} means DISABLED,
     * which is exactly the unbounded state this setting exists to end.
     */
    static int searchStatementTimeoutMs(String raw) {
        if (raw == null || raw.isBlank()) {
            return DEFAULT_SEARCH_STATEMENT_TIMEOUT_MS;
        }
        int ms;
        try {
            ms = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                "NX_SEARCH_STATEMENT_TIMEOUT_MS must be an integer, got: " + raw, e);
        }
        if (ms < 1 || ms > SEARCH_STATEMENT_TIMEOUT_MAX_MS) {
            throw new IllegalArgumentException(
                "NX_SEARCH_STATEMENT_TIMEOUT_MS must be in 1.." + SEARCH_STATEMENT_TIMEOUT_MAX_MS
                + " (0 would DISABLE the bound), got: " + ms);
        }
        return ms;
    }

    /**
     * Boot-time touch for {@code NX_SEARCH_STATEMENT_TIMEOUT_MS}, for the same
     * class-init reason as {@link #startupEfSearchFloor()}.
     *
     * @return the resolved bound in milliseconds, for the boot log line
     */
    public static int startupSearchStatementTimeoutMs() {
        return SEARCH_STATEMENT_TIMEOUT_MS;
    }

    /**
     * Bound every statement in this transaction to the serving timeout
     * (nexus-g17tf). Paired with {@link #setHnswEfSearch} at every
     * vector-ranked call site; the pairing is pinned by
     * {@code HnswServingGucParityTest}. Postgres raises SQLSTATE 57014
     * ({@code query_canceled}) when the bound is hit.
     */
    public static void setSearchStatementTimeout(DSLContext ctx) {
        setSearchStatementTimeout(ctx, SEARCH_STATEMENT_TIMEOUT_MS);
    }

    /** Explicit-bound form, for tests that need a bound shorter than the env-resolved one. */
    static void setSearchStatementTimeout(DSLContext ctx, int timeoutMs) {
        setLocal(ctx, "statement_timeout", Integer.toString(timeoutMs));
    }

    /**
     * Parse the {@code NX_HNSW_EF_SEARCH} override. Null/blank means the
     * default floor; anything else must be an integer in
     * [1, {@link #EF_SEARCH_MAX}] — out-of-range would either regress recall
     * silently (0/negative) or be rejected by Postgres at query time (>1000),
     * so both fail loud here instead.
     */
    static int efSearchFloor(String raw) {
        if (raw == null || raw.isBlank()) {
            return DEFAULT_EF_SEARCH_FLOOR;
        }
        int floor;
        try {
            floor = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                "NX_HNSW_EF_SEARCH must be an integer, got: " + raw, e);
        }
        if (floor < 1 || floor > EF_SEARCH_MAX) {
            throw new IllegalArgumentException(
                "NX_HNSW_EF_SEARCH must be in 1.." + EF_SEARCH_MAX + ", got: " + floor);
        }
        return floor;
    }

    /** {@code clamp(max(floor, nResults), 1, EF_SEARCH_MAX)} — pure, for tests. */
    static int efSearchFor(int nResults, int floor) {
        return Math.min(EF_SEARCH_MAX, Math.max(1, Math.max(floor, nResults)));
    }

    /** Sizing against the env-resolved floor. */
    static int efSearchFor(int nResults) {
        return efSearchFor(nResults, EF_SEARCH_FLOOR);
    }

    /**
     * Force {@code NX_HNSW_EF_SEARCH} validation at BOOT (review fold,
     * 2026-08-31, both reviewers): {@link #EF_SEARCH_FLOOR} is a static
     * initializer, so without a boot-time touch a malformed value would not
     * fail until the FIRST query — and then poison the whole class
     * ({@code NoClassDefFoundError} on every later call, JLS class-init
     * semantics) invisibly to health checks. {@code Main} calls this before
     * {@code service.start()}, alongside the {@code PoolerModeCheck}
     * fail-fast, so a bad value kills the process at startup with the
     * parse's own message instead.
     *
     * @return the resolved serving floor, for the boot log line
     */
    public static int startupEfSearchFloor() {
        return EF_SEARCH_FLOOR;
    }

    /**
     * Set the serving {@code hnsw.ef_search} for this transaction, sized to
     * the request: {@code max(floor, nResults)} clamped to pgvector's bound.
     * Pairs with the {@code hnsw.iterative_scan} set at every vector-ranked
     * call site (the pairing is pinned by {@code HnswServingGucParityTest});
     * iterative scan covers filtered under-RETURN, this covers cross-tenant
     * crowd-out mis-ranking (nexus-4ktfm) — neither substitutes for the other.
     */
    public static void setHnswEfSearch(DSLContext ctx, int nResults) {
        setLocal(ctx, "hnsw.ef_search", Integer.toString(efSearchFor(nResults)));
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
