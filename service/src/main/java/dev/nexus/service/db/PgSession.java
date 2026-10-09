/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

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

    private static final Logger log = LoggerFactory.getLogger(PgSession.class);

    /** GUCs the service is allowed to set transaction-locally. */
    private static final Set<String> ALLOWED_GUCS = Set.of(
        "hnsw.iterative_scan",
        "hnsw.ef_search",
        // nexus-wbfpw.47: the iterative-scan budget pair, see setHnswScanBudget.
        "hnsw.max_scan_tuples",
        "hnsw.scan_mem_multiplier",
        "pg_trgm.word_similarity_threshold",
        "statement_timeout",
        // nexus-r0vkh: the taxonomy assign transaction bounds its lock WAIT as
        // well as its run time, so a caller queued behind a slow head on the
        // topics row locks (doc_count recount trigger + the FK's KEY SHARE)
        // gives its pool connection back in seconds instead of minutes.
        "lock_timeout",
        "plan_cache_mode",
        "enable_indexscan",
        "enable_seqscan",
        "enable_bitmapscan",
        "enable_sort",
        // nexus-zrcj7 (T2 critic follow-up, 2026-09-04): EXPLAIN-based plan-shape test
        // discipline (a positive "must use the HNSW index" assertion) needs every
        // non-index access path penalized, hash joins included -- this GUC is set only
        // from test code (PlainSearchTextGatedSearchExplainTest et al.), never from
        // production PgVectorRepository dispatch paths.
        "enable_hashjoin",
        // nexus-mz9jv: the collection-stats statement is a few ms of executor work whose inflated cost
        // estimate tripped JIT compilation (190 to 270 ms per call); see disableJit.
        "jit"
    );

    /**
     * pgvector's hard bound on {@code hnsw.ef_search} (range 1..1000);
     * {@link #efSearchFor} clamps to it — Postgres rejects a set above it.
     */
    static final int EF_SEARCH_MAX = 1000;

    /**
     * The widest {@code hnsw.ef_search} pgvector accepts, for a single-collection walk of the shared leaf
     * index that has no per-collection index of its own (RDR-227 Step 1). Same value as the clamp.
     */
    public static final int EF_SEARCH_WIDEST = EF_SEARCH_MAX;

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
     * Only a larger candidate list finds them. The floor was 200 (5x the
     * default; the measured failure was marginal: a single insert displaced
     * the top-2, i.e. the boundary sat right at 40). RDR-225 replaced the one
     * global graph with one graph per (model, tenant) leaf, and the cloud
     * gate then missed one true neighbor at ef 200 (code-filtered-001,
     * recall@10 0.90; tail variance near tied distances, nexus-3wh8d.31,
     * T2 nexus_rdr/225-recall-per-leaf-analysis). The live sweep (conexus
     * T2 [29566]) restored 12/12 at ef 400 with latency flat to 1000; 600
     * keeps headroom over that measured point.
     */
    static final int DEFAULT_EF_SEARCH_FLOOR = 600;

    /**
     * Default serving value for {@code hnsw.max_scan_tuples} (nexus-wbfpw.47;
     * pgvector default 20000), overridable by {@code NX_HNSW_MAX_SCAN_TUPLES}.
     * An iterative scan stops at this many visited tuples OR at
     * {@code work_mem x hnsw.scan_mem_multiplier} bytes, whichever comes first.
     * Measured 2026-09-30 (T2 nexus/rdr-192-livec-recall-extended-2026-09-30,
     * nexus-wbfpw.44/.45): at 98% correlated-dead chunks on a shared 768/1024-d
     * index recall@10 fell to 0.85-0.89 under the old caps; this cap together
     * with the memory budget ({@link #DEFAULT_SCAN_MEM_BUDGET_MB}) restored
     * 1.000 in every cell measured, and neither alone changed anything.
     * Decided by Sam 2026-09-30.
     *
     * <p>COST, both directions. Measured at 98% dead: p50 ~42 to ~80 ms, worst
     * ~60 to ~300 ms, none at 90% dead or below. That measurement only covers
     * queries that had at least LIMIT qualifying rows. The raised budget also
     * applies to any filtered search that CANNOT fill its LIMIT (a small
     * collection on the shared index, a narrow {@code where}, tenant
     * crowd-out): the scan then runs to the cap, up from 20000 to 200000
     * tuples and from ~4 to ~16 MB per call, and that is paid BEFORE the
     * empty-result exact re-run ({@code PgVectorRepository#exactOnUnderReturn}).
     * That case is unmeasured. A scan that exhausts the budget on a large
     * shared index now ends at the search statement timeout (57014) rather
     * than returning low recall quickly.
     */
    static final int DEFAULT_MAX_SCAN_TUPLES = 200_000;

    /** Bounds on the {@code NX_HNSW_MAX_SCAN_TUPLES} override. Below 1,000 the search
     *  regresses recall well under pgvector's own 20000 default; above 100M a
     *  single search could hold a backend for the whole statement timeout. */
    static final int MAX_SCAN_TUPLES_MIN = 1_000;
    static final int MAX_SCAN_TUPLES_MAX = 100_000_000;

    /**
     * Fixed per-search memory budget for the iterative scan, in MB
     * (nexus-wbfpw.47), overridable by {@code NX_HNSW_SCAN_MEM_BUDGET_MB}.
     * pgvector bounds the scan at {@code work_mem x hnsw.scan_mem_multiplier}
     * and does not spill: an exhausted scan simply stops. The budget is held
     * FIXED and the multiplier is DERIVED from the engine role's effective
     * {@code work_mem} at boot ({@link #startupScanBudget}), because work_mem
     * differs by an order of magnitude between installs (stock 4 MB locally,
     * 384 MB measured on the managed cloud): a fixed multiplier would mean
     * ~16 MB in one place and ~1.5 GB per search in the other. Sized so
     * multiplier 4 at the local 4 MB work_mem restores recall (multiplier 2
     * measured 0.994, 4 measured 1.000).
     */
    static final int DEFAULT_SCAN_MEM_BUDGET_MB = 16;
    static final int SCAN_MEM_BUDGET_MB_MAX = 4096;

    /** pgvector's own upper bound for {@code hnsw.scan_mem_multiplier}. */
    static final int SCAN_MEM_MULTIPLIER_MAX = 1000;

    /** The resolved serving scan budget: what {@link #setHnswScanBudget} sets. */
    public record ScanBudget(int maxScanTuples, long workMemBytes, long budgetBytes, int memMultiplier) {
        /** The effective per-search memory ceiling the scan may use. */
        public long effectiveMemBytes() {
            return workMemBytes * memMultiplier;
        }
    }

    private static final int MAX_SCAN_TUPLES =
        maxScanTuples(System.getenv("NX_HNSW_MAX_SCAN_TUPLES"));

    private static final long SCAN_MEM_BUDGET_BYTES =
        scanMemBudgetBytes(System.getenv("NX_HNSW_SCAN_MEM_BUDGET_MB"));

    /** Null until {@link #startupScanBudget} (or the first search) resolves it. */
    private static volatile ScanBudget scanBudget;

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

    /**
     * Server-side bound on ONE {@code assign_from_chashes_<dim>} transaction
     * (nexus-r0vkh). Sized to the client's own 30s per-request timeout
     * ({@code _refreshable_client._DEFAULT_TIMEOUT_S}): a bound longer than
     * the caller's wait keeps a pool connection burning CPU after the client
     * has already recorded the batch as lost. Measured 2026-09-16 09:05Z on
     * engine-service-v0.1.123: one assign call ran 782s of DB CPU with the
     * server's 30s statement_timeout never reaching it, and every other
     * request on the box starved behind the pool it held.
     */
    static final int DEFAULT_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS = 30_000;

    /**
     * Server-side bound on the lock WAIT inside that same transaction
     * (nexus-r0vkh). The doc_count recount trigger (taxonomy-017) and the
     * FK's implicit KEY SHARE both take row locks on {@code nexus.topics},
     * so N concurrent assign calls for overlapping topic sets serialize
     * behind the first: eight callers sat 667-780s on the head's
     * transactionid, each holding one of the pool's ten connections. With
     * the client's own fires serialized (the hook's serialize opt-out is
     * withdrawn in the same change) only cross-process callers ever wait
     * here; five seconds is generous for a healthy head and short enough
     * that a wedged one cannot take the pool with it. Postgres raises
     * SQLSTATE 55P03 ({@code lock_not_available}) when it trips; the
     * client's tripwire records the batch, and the index write itself is
     * already committed, so nothing is lost but that batch's assignment.
     */
    static final int DEFAULT_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS = 5_000;

    /**
     * Env-resolved bounds ({@code NX_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS},
     * {@code NX_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS}), same precedent as
     * {@link #SEARCH_STATEMENT_TIMEOUT_MS}: read once at class load,
     * validated at boot by {@link #startupTaxonomyAssignStatementTimeoutMs()}
     * and {@link #startupTaxonomyAssignLockTimeoutMs()}.
     */
    private static final int TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS =
        boundedTimeoutMs("NX_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS",
                         System.getenv("NX_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS"),
                         DEFAULT_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS);
    private static final int TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS =
        boundedTimeoutMs("NX_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS",
                         System.getenv("NX_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS"),
                         DEFAULT_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS);

    // ── nexus-u9zkn: the per-path read bound ────────────────────────────────────────────────────
    //
    // Measured in the 2026-10-05 production failover: new connections recovered after 31.5 s, but a
    // search read already in flight hung 59 s, because the old server sent no RST and a read with no
    // network timeout waits on TCP. A pool-wide PgJDBC socketTimeout was rejected: a connection that is
    // waiting on a busy server is as silent as one waiting on a dead one, and several main-pool statements
    // legitimately run for minutes with no engine statement_timeout (collection re-home, quarantine,
    // purge, delete and rename collection, the taxonomy link joins), and a socket timeout closes the
    // socket without cancelling the backend, so the write rolls back and the retry stacks a second
    // backend behind the first. So the bound is per path and exists only where the engine ALREADY bounds
    // the statement: statement_timeout fires server-side first, its error comes back as a normal reply,
    // and the network timeout (statement bound + margin) only ever fires when the server is silent past
    // a bound it was itself told to enforce. Every Java-set statement bound and its read bound are set in ONE
    // place, setLocal, so they cannot drift (server-side bounds set inside plpgsql functions are covered in
    // setLocal's javadoc).

    /** Env name; whole seconds, {@code 0} disables the per-path network bound. */
    public static final String NETWORK_BOUND_MARGIN_ENV = "NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS";

    /**
     * Headroom above a statement bound before the read is given up. Large enough that the server's own
     * cancel and its reply always win the race against the client's timer, small enough that a silent
     * server is noticed within a minute of the default 30 s search bound.
     */
    public static final int DEFAULT_NETWORK_BOUND_MARGIN_SECONDS = 30;

    /** Upper bound on the override: an hour. */
    static final int NETWORK_BOUND_MARGIN_MAX_SECONDS = 3600;

    /** Env-resolved margin, read once at class load and validated at boot by {@link #startupNetworkBoundMarginMs()}. */
    private static final int NETWORK_BOUND_MARGIN_MS =
        networkBoundMarginMs(System.getenv(NETWORK_BOUND_MARGIN_ENV));

    /** Test seam, {@code -1} for none: see {@link #setNetworkBoundMarginMsForTests}. */
    private static volatile int networkBoundMarginMsOverride = -1;

    /**
     * Parse {@code NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS} into milliseconds. Null or blank is the default;
     * {@code 0} disables the per-path network bound; anything else must be an integer in
     * [1, {@link #NETWORK_BOUND_MARGIN_MAX_SECONDS}]. A bad value throws, naming the variable.
     */
    static int networkBoundMarginMs(String raw) {
        if (raw == null || raw.isBlank()) {
            return DEFAULT_NETWORK_BOUND_MARGIN_SECONDS * 1000;
        }
        int seconds;
        try {
            seconds = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(NETWORK_BOUND_MARGIN_ENV + " must be an integer, got: " + raw, e);
        }
        if (seconds < 0 || seconds > NETWORK_BOUND_MARGIN_MAX_SECONDS) {
            throw new IllegalArgumentException(NETWORK_BOUND_MARGIN_ENV + " must be 0 (disabled) or in 1.."
                + NETWORK_BOUND_MARGIN_MAX_SECONDS + ", got: " + seconds);
        }
        return seconds * 1000;
    }

    /** Boot-time touch for {@code NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS}; returns milliseconds, {@code 0} when disabled. */
    public static int startupNetworkBoundMarginMs() {
        return NETWORK_BOUND_MARGIN_MS;
    }

    /**
     * The network timeout a statement bound implies: {@code statementTimeoutMs + marginMs}, clamped to
     * the int PgJDBC takes. {@code 0} (no timeout) for a non-positive statement bound, which is how
     * Postgres reads {@code statement_timeout=0}, and for a disabled margin.
     */
    static int networkTimeoutMs(long statementTimeoutMs, int marginMs) {
        if (statementTimeoutMs <= 0 || marginMs <= 0) {
            return 0;
        }
        return (int) Math.min(statementTimeoutMs + marginMs, Integer.MAX_VALUE);
    }

    /**
     * Test seam: replace the margin so a test can use a small one; {@code -1} restores the env-resolved
     * value. Same shape as {@code TenantScope#replaceFanoutArmGateForTests}.
     */
    public static void setNetworkBoundMarginMsForTests(int marginMs) {
        networkBoundMarginMsOverride = marginMs;
    }

    private static int effectiveMarginMs() {
        int override = networkBoundMarginMsOverride;
        return override >= 0 ? override : NETWORK_BOUND_MARGIN_MS;
    }

    /**
     * Give the connection {@code ctx} runs on a network (socket read) timeout of {@code statementTimeoutMs}
     * plus the margin, for the rest of this borrow. The Hikari proxy marks the connection's network
     * timeout dirty and restores the pool's value (none) when the connection is closed, so nothing leaks
     * to the next borrower; {@code SET LOCAL} values do not outlive the transaction and this does not
     * outlive the borrow, which is the same lifetime for every request-path caller. A non-positive bound
     * puts the connection back to no timeout. Does nothing when the margin is {@code 0}.
     *
     * <p>A failure to set it is logged and does not fail the request: it is a backstop, and the statement
     * that follows on a dead connection fails on its own.
     */
    private static void bindNetworkTimeout(DSLContext ctx, long statementTimeoutMs) {
        int marginMs = effectiveMarginMs();
        if (marginMs <= 0) {
            return;
        }
        int timeoutMs = networkTimeoutMs(statementTimeoutMs, marginMs);
        try {
            ctx.connection(conn -> conn.setNetworkTimeout(Runnable::run, timeoutMs));
        } catch (DataAccessException e) {
            log.warn("event=pg_network_timeout_not_set statement_timeout_ms={} error=\"{}\"",
                     statementTimeoutMs, e.getMessage());
        }
    }

    /**
     * Floor on the tenant stamp's network bound: the stamp is one {@code set_config}, but the margin is
     * the operator's choice for a statement's headroom and its accepted minimum is 1 s, which would kill a
     * healthy server that is merely saturated and slow to answer a trivial statement. 5 s is far above a
     * healthy answer and still ends a silent peer at the stamp quickly.
     */
    static final int STAMP_NETWORK_BOUND_FLOOR_MS = 5_000;

    /** The network timeout the tenant stamp gets for a margin: {@code max(margin, 5 s)}; {@code 0} when disabled. */
    static int stampNetworkTimeoutMs(int marginMs) {
        return marginMs <= 0 ? 0 : Math.max(marginMs, STAMP_NETWORK_BOUND_FLOOR_MS);
    }

    /**
     * Bound the tenant stamp, the first round trip of every {@code TenantScope} borrow, which runs before
     * any path sets its statement bound. {@code on=true} sets a network timeout of {@code max(margin, 5 s)}
     * ({@link #stampNetworkTimeoutMs}); {@code on=false} puts the connection back to no timeout, so a path
     * with no statement bound still runs unbounded. Does nothing when the margin is {@code 0}. A failure to
     * set it is logged, as in {@link #bindNetworkTimeout}.
     */
    public static void bindStampNetworkTimeout(java.sql.Connection conn, boolean on) {
        int marginMs = effectiveMarginMs();
        if (marginMs <= 0) {
            return;
        }
        try {
            conn.setNetworkTimeout(Runnable::run, on ? stampNetworkTimeoutMs(marginMs) : 0);
        } catch (java.sql.SQLException e) {
            log.warn("event=pg_network_timeout_not_set stage=tenant_stamp error=\"{}\"", e.getMessage());
        }
    }

    private PgSession() {
    }

    /**
     * Parse one millisecond-bound env override (nexus-r0vkh): null/blank
     * means {@code defaultMs}; anything else must be an integer in
     * [1, {@link #SEARCH_STATEMENT_TIMEOUT_MAX_MS}]. Zero is refused for the
     * same reason {@link #searchStatementTimeoutMs} refuses it: to Postgres
     * {@code statement_timeout=0} and {@code lock_timeout=0} both mean
     * DISABLED, the unbounded state the setting exists to end.
     */
    static int boundedTimeoutMs(String envName, String raw, int defaultMs) {
        if (raw == null || raw.isBlank()) {
            return defaultMs;
        }
        int ms;
        try {
            ms = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                envName + " must be an integer, got: " + raw, e);
        }
        if (ms < 1 || ms > SEARCH_STATEMENT_TIMEOUT_MAX_MS) {
            throw new IllegalArgumentException(
                envName + " must be in 1.." + SEARCH_STATEMENT_TIMEOUT_MAX_MS
                + " (0 would DISABLE the bound), got: " + ms);
        }
        return ms;
    }

    /** Boot-time touch for {@code NX_TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS}. */
    public static int startupTaxonomyAssignStatementTimeoutMs() {
        return TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS;
    }

    /** Boot-time touch for {@code NX_TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS}. */
    public static int startupTaxonomyAssignLockTimeoutMs() {
        return TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS;
    }

    /**
     * Bound the taxonomy assign transaction (nexus-r0vkh): every statement
     * in it to {@link #TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS} and every lock
     * wait to {@link #TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS}. Called at the top of
     * {@code TaxonomyRepository#assignFromChashes}'s {@code withTenant}
     * block; the pairing is pinned by {@code TaxonomyAssignBoundsIntegrationTest}.
     * Postgres raises 57014 ({@code query_canceled}) on the first bound and
     * 55P03 ({@code lock_not_available}) on the second.
     */
    public static void setTaxonomyAssignBounds(DSLContext ctx) {
        setTaxonomyAssignBounds(ctx, TAXONOMY_ASSIGN_STATEMENT_TIMEOUT_MS,
                                TAXONOMY_ASSIGN_LOCK_TIMEOUT_MS);
    }

    /** Explicit-bound form, for tests that need bounds shorter than the env-resolved ones. */
    static void setTaxonomyAssignBounds(DSLContext ctx, int statementTimeoutMs, int lockTimeoutMs) {
        setStatementAndLockBounds(ctx, statementTimeoutMs, lockTimeoutMs);
    }

    /**
     * Bounds for one bounded quarantine batch ({@code gc_quarantine_orphans_bounded},
     * nexus-a6mon). The function body's own {@code set_config('statement_timeout',
     * '25000', true)} is INERT for the statement running it: Postgres arms
     * statement_timeout once, when the top-level statement starts, from the value
     * in effect at that moment, and nothing a plpgsql body sets later re-arms it
     * (the unbounded function's identical in-body 5 s bound was live throughout
     * the 2026-09-16 incident in which one call ran 5m41s). {@code lock_timeout}
     * IS armed at each lock wait, so the body's 2 s gate bound works. The values
     * here mirror the body's, set as their OWN statement before the call so the
     * statement bound is real; substantive-critic finding on a990fe8f1.
     */
    public static final int DEFAULT_GC_QUARANTINE_BOUNDED_STATEMENT_TIMEOUT_MS = 25_000;
    public static final int DEFAULT_GC_QUARANTINE_BOUNDED_LOCK_TIMEOUT_MS = 2_000;

    public static void setGcQuarantineBoundedBounds(DSLContext ctx) {
        setStatementAndLockBounds(ctx, DEFAULT_GC_QUARANTINE_BOUNDED_STATEMENT_TIMEOUT_MS,
                                  DEFAULT_GC_QUARANTINE_BOUNDED_LOCK_TIMEOUT_MS);
    }

    /**
     * Bounds for one bounded restore batch ({@code gc_restore_rereferenced_bounded},
     * nexus-e8h5x), mirroring {@link #DEFAULT_GC_QUARANTINE_BOUNDED_STATEMENT_TIMEOUT_MS}
     * / {@link #DEFAULT_GC_QUARANTINE_BOUNDED_LOCK_TIMEOUT_MS} for the opposite
     * direction — set as their OWN statement before the call for the identical
     * reason: the function body's own {@code set_config('statement_timeout', ...)}
     * is inert for the statement already running it.
     */
    public static final int DEFAULT_GC_RESTORE_BOUNDED_STATEMENT_TIMEOUT_MS = 25_000;
    public static final int DEFAULT_GC_RESTORE_BOUNDED_LOCK_TIMEOUT_MS = 2_000;

    public static void setGcRestoreBoundedBounds(DSLContext ctx) {
        setStatementAndLockBounds(ctx, DEFAULT_GC_RESTORE_BOUNDED_STATEMENT_TIMEOUT_MS,
                                  DEFAULT_GC_RESTORE_BOUNDED_LOCK_TIMEOUT_MS);
    }

    /**
     * Bound every later statement in this transaction to {@code statementTimeoutMs}
     * and every lock wait to {@code lockTimeoutMs}. Two {@code set_config} calls,
     * each its own top-level statement, so the statement bound applies to the
     * statements that FOLLOW; a bound set from inside a running function body
     * never applies to that body's own statement (see
     * {@link #setGcQuarantineBoundedBounds}).
     */
    public static void setStatementAndLockBounds(DSLContext ctx, int statementTimeoutMs, int lockTimeoutMs) {
        setLocal(ctx, "statement_timeout", Integer.toString(statementTimeoutMs));
        setLocal(ctx, "lock_timeout", Integer.toString(lockTimeoutMs));
    }

    /**
     * Parse the {@code NX_SEARCH_STATEMENT_TIMEOUT_MS} override. Null/blank
     * means the default; anything else must be an integer in
     * [1, {@link #SEARCH_STATEMENT_TIMEOUT_MAX_MS}]. Zero is refused
     * explicitly: to Postgres {@code statement_timeout=0} means DISABLED,
     * which is exactly the unbounded state this setting exists to end.
     */
    static int searchStatementTimeoutMs(String raw) {
        return boundedTimeoutMs("NX_SEARCH_STATEMENT_TIMEOUT_MS", raw,
                                DEFAULT_SEARCH_STATEMENT_TIMEOUT_MS);
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

    /**
     * Explicit-bound form: for tests that need a bound shorter than the env-resolved one,
     * and for the per-collection fan-out, whose arms bound each statement by
     * {@code min(search bound, remaining request budget)} (nexus-tu8wp.1). The caller owns
     * keeping the value at {@code >= 1}: to Postgres {@code 0} means DISABLED.
     */
    public static void setSearchStatementTimeout(DSLContext ctx, int timeoutMs) {
        setLocal(ctx, "statement_timeout", Integer.toString(timeoutMs));
    }

    /**
     * Force a CUSTOM plan for every statement in this transaction
     * (nexus-6nkn3). The vector-ranked statements are parameterised on the
     * collection set, and their right plan depends on that set's
     * selectivity: a 176-row collection wants the pk scan (0.6s), a large
     * one wants the HNSW-ordered scan. pgjdbc switches a prepared statement
     * to a GENERIC plan after five executions on each pooled connection, and
     * a generic plan cannot see selectivity. Measured in production
     * 2026-09-03: a generic HNSW-ordered plan built under stale stats,
     * applied to a 176-row collection under iterative scan, removed 22,932
     * rows by filter, admitted NONE, ran ~30s cold and returned EMPTY,
     * while the custom plan on the same statement took 0.6s. Paired with
     * {@link #setSearchStatementTimeout} at every vector-ranked site; the
     * pairing is pinned by {@code HnswServingGucParityTest}.
     *
     * <p>This removes the TRIGGER (a stale generic plan), not the CLASS: an
     * HNSW-ordered scan under a filter selective enough to exhaust
     * {@code hnsw.max_scan_tuples} with too few admitted rows still
     * under-returns, or returns empty, silently. That is nexus-bq06h, open.
     *
     * <p>Reaches the combined-query SQL functions ({@code search_*_scoped},
     * {@code search_graph_hop}) only because they are inlinable (STABLE,
     * SECURITY INVOKER, not STRICT — pinned by {@code CombinedQueryParityTest});
     * a non-inlinable function body plans on its own and would not see this.
     */
    public static void setSearchPlanCacheMode(DSLContext ctx) {
        setLocal(ctx, "plan_cache_mode", "force_custom_plan");
    }

    /**
     * Turn JIT compilation off for the rest of this transaction ({@code SET LOCAL jit = off}, nexus-mz9jv).
     * PostgreSQL JIT-compiles a statement whose estimated cost passes {@code jit_above_cost}; an aggregate over
     * a large partitioned table does, however cheap its real execution, and compiling cost 190 to 270 ms per call
     * on the collection-stats statement (T2 {@code nexus/search-latency-root-cause-2026-10-08}). For a statement
     * whose executor time is milliseconds that compile time is pure latency. Not applied to the vector-ranked
     * statements: it does not touch the serving GUC pairing {@code HnswServingGucParityTest} counts.
     */
    public static void disableJit(DSLContext ctx) {
        setLocal(ctx, "jit", "off");
    }

    /**
     * Exact-ordering fallback (nexus-bq06h): disable index scans for the rest
     * of this transaction so the re-run of a vector-ranked statement orders
     * the FILTERED rows exactly instead of walking the shared HNSW index.
     * Used only after the index-ordered attempt returned NOTHING: an HNSW scan
     * under a selective filter can exhaust {@code hnsw.max_scan_tuples}
     * admitting no row and return empty, silently (measured in production
     * 2026-09-03: 176-row collection, 22,932 removed by filter, 0 admitted).
     *
     * <p>Bitmap scans stay ON: pgvector's HNSW cannot serve a bitmap scan, so
     * the planner's exact alternatives are a bitmap scan on the primary key's
     * (tenant, collection) prefix, proportional to the collection set, or a
     * sequential scan when that is cheaper, then a sort. Seq scan and sort are
     * re-enabled outright so a session that penalised them to force the index
     * cannot leave the fallback with no exact plan.
     */
    public static void disableIndexScanForExactFallback(DSLContext ctx) {
        // One statement, not four (nexus-wym0l): the four settings are independent and run in this order.
        new GucBatch(ctx)
            .set("enable_indexscan", "off")
            .set("enable_bitmapscan", "on")
            .set("enable_seqscan", "on")
            .set("enable_sort", "on")
            .applyInTransaction();
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

    /** Sizing against the env-resolved floor, or the test pin when set. */
    static int efSearchFor(int nResults) {
        Integer o = efSearchFloorOverride;
        return efSearchFor(nResults, o != null ? o : EF_SEARCH_FLOOR);
    }

    /** Test pin for the serving floor; null means the env-resolved {@link #EF_SEARCH_FLOOR}. */
    private static volatile Integer efSearchFloorOverride;

    /**
     * TEST SEAM (nexus-3wh8d.31): pin the serving {@code hnsw.ef_search} floor, so a
     * fixture of a few hundred rows can starve an HNSW walk the way a large leaf does in
     * production; at the 600 default the first ef batch covers such a fixture's whole
     * graph. Pair with {@link #resetEfSearchFloorForTests()}.
     */
    public static void overrideEfSearchFloorForTests(int floor) {
        efSearchFloorOverride = floor;
    }

    /** Drop the pinned floor; the env-resolved value applies again. */
    public static void resetEfSearchFloorForTests() {
        efSearchFloorOverride = null;
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
     * Parse the {@code NX_HNSW_MAX_SCAN_TUPLES} override. Null/blank means
     * {@link #DEFAULT_MAX_SCAN_TUPLES}; anything else must be an integer in
     * [{@link #MAX_SCAN_TUPLES_MIN}, {@link #MAX_SCAN_TUPLES_MAX}], loud at boot.
     */
    static int maxScanTuples(String raw) {
        if (raw == null || raw.isBlank()) {
            return DEFAULT_MAX_SCAN_TUPLES;
        }
        int v;
        try {
            v = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                "NX_HNSW_MAX_SCAN_TUPLES must be an integer, got: " + raw, e);
        }
        if (v < MAX_SCAN_TUPLES_MIN || v > MAX_SCAN_TUPLES_MAX) {
            throw new IllegalArgumentException("NX_HNSW_MAX_SCAN_TUPLES must be in "
                + MAX_SCAN_TUPLES_MIN + ".." + MAX_SCAN_TUPLES_MAX + ", got: " + v);
        }
        return v;
    }

    /**
     * Cardinality router threshold (nexus-tu8wp.6): when the collections one plain search selects
     * hold at most this many PHYSICAL rows in the tenant, the statement runs exact (index scans
     * off) instead of walking the shared HNSW index. 0 disables the router.
     *
     * <p>30000 (nexus-nqsa7, Sam's decision 2026-10-09). Above the threshold an arm walks its leaf's HNSW
     * index under {@code relaxed_order}, which stops at the first {@code k} rows the collection filter
     * admits; in a leaf shared by many collections the filter discarded about 80% of the walk, and a full
     * page missed the collection's true nearest row (recall against exact 0.925 at k=40 on code__1-72,
     * 27,893 rows; T2 conexus/nqsa7-fork-hnsw-vs-exact-2026-10-09). The empty-result re-run cannot see
     * that, because the page is full. Exact is complete.
     *
     * <p>Why not higher. engine-service-v0.1.155 shipped 60000, and on the live engine the five code
     * collections then searched exact ran five at a time and each took about 2.5x its solo time on the
     * fork (CPU and memory-bandwidth contention, zero permit wait): the two largest, 58,588 and 45,525 rows,
     * took 1.1-2.0 s, and a warm default search got about 1 s slower. More fan-out permits would add
     * concurrent exact scans and make it worse. 30000 keeps the measured repro and the 29,355-row collection
     * exact and puts the two largest back on HNSW, which leaves them exposed to the same miss; a remedy
     * independent of this threshold is tracked as its own bead. It was a provisional 10000 before v0.1.155.
     * The probe reads up to {@code limit + 1} keys, so a collection above the threshold pays a 30,001-key
     * Index Only Scan before its HNSW walk.
     */
    static final int DEFAULT_SEARCH_EXACT_MAX_ROWS = 30_000;

    /** Upper bound on the {@code NX_SEARCH_EXACT_MAX_ROWS} override: an exact scan over more rows than
     *  this would no longer be the cheap plan the router exists to take. */
    static final int SEARCH_EXACT_MAX_ROWS_MAX = 1_000_000;

    private static final int SEARCH_EXACT_MAX_ROWS =
        searchExactMaxRows(System.getenv("NX_SEARCH_EXACT_MAX_ROWS"));

    /** Test pin for the threshold; null means the env-resolved value. */
    private static volatile Integer searchExactMaxRowsOverride;

    /**
     * Parse the {@code NX_SEARCH_EXACT_MAX_ROWS} override. Null/blank means
     * {@link #DEFAULT_SEARCH_EXACT_MAX_ROWS}; anything else must be an integer in
     * [0, {@link #SEARCH_EXACT_MAX_ROWS_MAX}] (0 disables the router), loud at boot.
     */
    static int searchExactMaxRows(String raw) {
        if (raw == null || raw.isBlank()) {
            return DEFAULT_SEARCH_EXACT_MAX_ROWS;
        }
        int v;
        try {
            v = Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                "NX_SEARCH_EXACT_MAX_ROWS must be an integer, got: " + raw, e);
        }
        if (v < 0 || v > SEARCH_EXACT_MAX_ROWS_MAX) {
            throw new IllegalArgumentException("NX_SEARCH_EXACT_MAX_ROWS must be in 0.."
                + SEARCH_EXACT_MAX_ROWS_MAX + " (0 disables the router), got: " + v);
        }
        return v;
    }

    /**
     * Boot-time touch for {@code NX_SEARCH_EXACT_MAX_ROWS}, for the same class-init reason as
     * {@link #startupEfSearchFloor()}: a malformed value must fail at boot, not at the first search.
     *
     * @return the resolved threshold, for the boot log line
     */
    public static int startupSearchExactMaxRows() {
        return SEARCH_EXACT_MAX_ROWS;
    }

    /** The threshold the router compares against: the test pin when set, else the env-resolved value. */
    public static int searchExactMaxRows() {
        Integer o = searchExactMaxRowsOverride;
        return o != null ? o : SEARCH_EXACT_MAX_ROWS;
    }

    /** TEST SEAM (nexus-tu8wp.6): pin the router threshold. Pair with {@link #resetSearchExactMaxRowsForTests()}. */
    public static void overrideSearchExactMaxRowsForTests(int maxRows) {
        searchExactMaxRowsOverride = maxRows;
    }

    /** Drop the pinned threshold; the env-resolved value applies again. */
    public static void resetSearchExactMaxRowsForTests() {
        searchExactMaxRowsOverride = null;
    }

    /**
     * Parse the {@code NX_HNSW_SCAN_MEM_BUDGET_MB} override into bytes. Null/blank
     * means {@link #DEFAULT_SCAN_MEM_BUDGET_MB}; anything else must be an integer
     * in [1, {@link #SCAN_MEM_BUDGET_MB_MAX}], loud at boot.
     */
    static long scanMemBudgetBytes(String raw) {
        int mb = DEFAULT_SCAN_MEM_BUDGET_MB;
        if (raw != null && !raw.isBlank()) {
            try {
                mb = Integer.parseInt(raw.trim());
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                    "NX_HNSW_SCAN_MEM_BUDGET_MB must be an integer, got: " + raw, e);
            }
            if (mb < 1 || mb > SCAN_MEM_BUDGET_MB_MAX) {
                throw new IllegalArgumentException("NX_HNSW_SCAN_MEM_BUDGET_MB must be in 1.."
                    + SCAN_MEM_BUDGET_MB_MAX + ", got: " + mb);
            }
        }
        return mb * 1024L * 1024L;
    }

    private static final java.util.regex.Pattern WORK_MEM =
        java.util.regex.Pattern.compile("(\\d+)\\s*(B|kB|MB|GB|TB)");

    /** Parse a {@code current_setting('work_mem')} value ("4MB", "384MB", "64kB") to bytes. */
    static long parseWorkMemBytes(String setting) {
        java.util.regex.Matcher m = WORK_MEM.matcher(setting == null ? "" : setting.trim());
        if (!m.matches()) {
            throw new IllegalStateException("cannot parse work_mem setting: '" + setting + "'");
        }
        long n = Long.parseLong(m.group(1));
        return switch (m.group(2)) {
            case "B" -> n;
            case "kB" -> n * 1024L;
            case "MB" -> n * 1024L * 1024L;
            case "GB" -> n * 1024L * 1024L * 1024L;
            default -> n * 1024L * 1024L * 1024L * 1024L;
        };
    }

    /** {@code clamp(budgetBytes / workMemBytes, 1, SCAN_MEM_MULTIPLIER_MAX)} - pure, for tests. */
    static int scanMemMultiplier(long workMemBytes, long budgetBytes) {
        if (workMemBytes <= 0) {
            throw new IllegalArgumentException("work_mem must be positive, got " + workMemBytes);
        }
        return (int) Math.max(1L, Math.min(SCAN_MEM_MULTIPLIER_MAX, budgetBytes / workMemBytes));
    }

    /**
     * Resolve and publish the serving scan budget (nexus-wbfpw.47): read the
     * engine role's effective {@code work_mem} from the database and derive
     * {@code hnsw.scan_mem_multiplier = max(1, budget / work_mem)}. Called from
     * {@code Main} at boot so a bad environment override or an unreadable
     * work_mem fails loud before serving, and the result is logged there. Also
     * called lazily by the first {@link #setHnswScanBudget} in a process that
     * never ran the boot path (tests).
     *
     * @return the resolved budget, for the boot log line
     */
    public static ScanBudget startupScanBudget(DSLContext ctx) {
        String raw = ctx.select(DSL.function("current_setting", String.class, DSL.val("work_mem")))
            .fetchSingle().value1();
        long workMem = parseWorkMemBytes(raw);
        ScanBudget b = new ScanBudget(MAX_SCAN_TUPLES, workMem, SCAN_MEM_BUDGET_BYTES,
            scanMemMultiplier(workMem, SCAN_MEM_BUDGET_BYTES));
        scanBudget = b;
        return b;
    }

    /** The resolved budget, or null before it has been resolved. */
    public static ScanBudget currentScanBudget() {
        return scanBudget;
    }

    /**
     * TEST SEAM (nexus-wbfpw.47): pin the budget the search paths set, e.g. a tiny
     * tuple cap so a fixture of a few thousand rows reaches the scan cap the way a
     * large index does in production. Replaces a connection-level
     * {@code -c hnsw.max_scan_tuples=...}, which the per-search SET LOCAL now
     * overrides. Pair with {@link #resetScanBudgetForTests()} in a finally.
     */
    public static void overrideScanBudgetForTests(int maxScanTuples, int memMultiplier) {
        scanBudget = new ScanBudget(maxScanTuples, 0L, 0L, memMultiplier);
    }

    /** Drop any pinned or resolved budget; the next search (or boot) resolves afresh. */
    public static void resetScanBudgetForTests() {
        scanBudget = null;
    }

    /**
     * Set the serving iterative-scan budget for this transaction:
     * {@code hnsw.max_scan_tuples} and {@code hnsw.scan_mem_multiplier}
     * (nexus-wbfpw.47). Called at every vector-ranked site next to
     * {@link #setHnswEfSearch}, BEFORE the fetch; the pairing and the order are
     * pinned by {@code HnswServingGucParityTest}. Both must be raised together:
     * the scan stops at whichever cap it reaches first, so raising one alone
     * changes nothing (see {@link #DEFAULT_MAX_SCAN_TUPLES}).
     *
     * <p>At the centroid ANN site ({@code TaxonomyCentroidRepository#annQuery})
     * this is crowd-out headroom, not dead-row recall: centroids have no
     * liveness predicate, but the same-collection query is a selective filter on
     * a unified table (RLS after the scan) and a collection with fewer centroids
     * than {@code n} would exhaust the default cap. The table is small, so it is
     * a no-op below 20000 centroid rows.
     */
    public static void setHnswScanBudget(DSLContext ctx) {
        ScanBudget b = scanBudget;
        if (b == null) {
            b = startupScanBudget(ctx);
        }
        setLocal(ctx, "hnsw.max_scan_tuples", Integer.toString(b.maxScanTuples()));
        setLocal(ctx, "hnsw.scan_mem_multiplier", Integer.toString(b.memMultiplier()));
    }

    /**
     * Set a transaction-local GUC ({@code SET LOCAL} semantics) via
     * {@code set_config(name, value, is_local=true)}.
     *
     * <p>Must be called inside a transaction (jOOQ {@code transaction(...)} /
     * TenantScope block) — set_config with {@code is_local=true} outside a
     * transaction is a silent no-op, same as SET LOCAL.
     *
     * <p>Setting {@code statement_timeout} here also gives the connection a network (socket read)
     * timeout of that bound plus the margin (nexus-u9zkn, see {@link #bindNetworkTimeout}). Every
     * statement bound the JAVA side sets goes through this method, so a bounded path and its read bound
     * cannot drift apart; a path with no statement bound never calls it and gets no read bound. Call it
     * FIRST among a borrow's {@code setLocal} round trips, so the others run under the read bound too.
     *
     * <p>Not covered: a plpgsql function that sets {@code statement_timeout} itself, server-side
     * (the {@code gc_*} and {@code reaper_*} functions, 5 s or 25 s). The Java bound that precedes such a
     * call is the one that arms the call's own timer, and on every production path that reaches a 25 s
     * function it is itself 25 s, so the read bound (25 s plus a margin of at least 1 s) outlasts the
     * function's value. A new path that calls a function setting 25 s must set a Java bound of at least 25 s.
     *
     * @param ctx   transaction-bound DSL context
     * @param guc   GUC name; must be whitelisted in {@link #ALLOWED_GUCS}
     * @param value value to set for the remainder of the transaction
     * @throws IllegalArgumentException on a non-whitelisted GUC
     */
    public static void setLocal(DSLContext ctx, String guc, String value) {
        requireAllowed(guc);
        // nexus-u9zkn: a statement bound and its network bound are set together here, before the
        // set_config round trip so that round trip is covered too.
        if ("statement_timeout".equals(guc)) {
            bindNetworkTimeout(ctx, Long.parseLong(value));
        }
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(guc), DSL.val(value), DSL.inline(true)))
           .fetch();
    }

    private static void requireAllowed(String guc) {
        if (!ALLOWED_GUCS.contains(guc)) {
            throw new IllegalArgumentException(
                "GUC '" + guc + "' is not whitelisted for SET LOCAL (allowed: "
                + ALLOWED_GUCS + ")");
        }
    }

    // ── batched settings (nexus-wym0l) ───────────────────────────────────────

    /**
     * A set of transaction-local GUC assignments that travel to Postgres as ONE statement,
     * {@code SELECT set_config(a, ..., true), set_config(b, ..., true), ...} (nexus-wym0l).
     *
     * <p>Why: every {@link #setLocal} is a client-to-server round trip, and a search-per-collection arm
     * used to send the tenant stamp and six serving settings as seven of them before its first real
     * statement (T2 {@code nexus/search-latency-root-cause-2026-10-08}: 131 of 183 statements in a
     * 13-collection request). Against a database one network hop away each costs that hop's RTT.
     *
     * <p>Semantics are exactly the unbatched ones. The assignments are the same {@code set_config(name,
     * value, true)} calls with the same names and values, transaction-local, applied in the order added
     * (the target list of a SELECT is evaluated left to right, and the settings are independent of each
     * other), and the statement runs BEFORE the first statement that depends on them, because
     * {@link TenantScope#withTenant(String, java.util.function.Consumer, java.util.function.Function)}
     * applies the batch before it hands the caller its context. A setting that is rejected (a value Postgres
     * refuses) fails the statement and so the transaction, as an unbatched one would.
     *
     * <p>The {@link #setSearchStatementTimeout}, {@link #setHnswEfSearch}, {@link #setHnswScanBudget} and
     * {@link #setSearchPlanCacheMode} overloads that take a batch add what the {@code DSLContext} forms
     * set; {@code HnswServingGucParityTest} counts those calls by name, so the pairing is checked at every
     * site whichever form it uses.
     */
    public static final class GucBatch {
        private final DSLContext ctx;
        private final java.util.List<org.jooq.Field<String>> calls = new java.util.ArrayList<>();
        private Long statementTimeoutMs;

        GucBatch(DSLContext ctx) {
            this.ctx = ctx;
        }

        /** Add {@code set_config(guc, value, true)}; the name must be whitelisted. */
        public GucBatch set(String guc, String value) {
            requireAllowed(guc);
            if ("statement_timeout".equals(guc)) {
                statementTimeoutMs = Long.parseLong(value);
            }
            add(guc, value);
            return this;
        }

        /** The tenant stamp: names outside {@link #ALLOWED_GUCS}, validated by {@code TenantScope}. */
        GucBatch stamp(String gucName, String tenant) {
            add(gucName, tenant);
            return this;
        }

        private void add(String guc, String value) {
            calls.add(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(guc), DSL.val(value), DSL.inline(true)));
        }

        /** The context the batch will run on; the scan budget is resolved through it when not yet known. */
        DSLContext ctx() {
            return ctx;
        }

        /**
         * Send the batch as one statement, with the network bounds of the unbatched sequence: the statement
         * runs under the tenant stamp's bound (it is the first read of the borrow, before any path has a
         * statement bound), and afterwards the connection is bound to {@code statement_timeout} plus the
         * margin when the batch set one, else put back to no bound. A batch with no calls sends nothing.
         */
        void apply(java.sql.Connection conn) {
            if (calls.isEmpty()) {
                return;
            }
            bindStampNetworkTimeout(conn, true);
            ctx.select(calls).fetch();
            if (statementTimeoutMs != null) {
                bindNetworkTimeout(ctx, statementTimeoutMs);
            } else {
                bindStampNetworkTimeout(conn, false);
            }
        }

        /**
         * Send the batch as one statement mid-transaction, when the borrow's stamp and bounds are already
         * in place (the exact route's settings, which follow the router probe).
         */
        void applyInTransaction() {
            if (calls.isEmpty()) {
                return;
            }
            if (statementTimeoutMs != null) {
                bindNetworkTimeout(ctx, statementTimeoutMs);
            }
            ctx.select(calls).fetch();
        }
    }

    /** A new, empty batch on {@code ctx}'s transaction. */
    public static GucBatch gucBatch(DSLContext ctx) {
        return new GucBatch(ctx);
    }

    /** {@link #setSearchStatementTimeout(DSLContext, int)} for a batch. */
    public static void setSearchStatementTimeout(GucBatch batch, int timeoutMs) {
        batch.set("statement_timeout", Integer.toString(timeoutMs));
    }

    /** {@link #setHnswEfSearch(DSLContext, int)} for a batch. */
    public static void setHnswEfSearch(GucBatch batch, int nResults) {
        batch.set("hnsw.ef_search", Integer.toString(efSearchFor(nResults)));
    }

    /**
     * The value {@link #setHnswEfSearch(GucBatch, int)} would set for {@code nResults}: the serving floor
     * (or its test pin) clamped to pgvector's bound. For telemetry and for a caller that must know the
     * value before the batch runs.
     */
    public static int servingEfSearch(int nResults) {
        return efSearchFor(nResults);
    }

    /**
     * Set {@code hnsw.ef_search} to {@link #EF_SEARCH_WIDEST} for a batch, instead of the request-sized
     * serving value {@link #setHnswEfSearch(GucBatch, int)} sets (RDR-227 Step 1). A caller picks one of the
     * two per statement; it is the same setting, so the pairing {@code HnswServingGucParityTest} pins holds.
     */
    public static void setHnswEfSearchWidest(GucBatch batch) {
        batch.set("hnsw.ef_search", Integer.toString(EF_SEARCH_WIDEST));
    }

    /** {@link #setHnswScanBudget(DSLContext)} for a batch. */
    public static void setHnswScanBudget(GucBatch batch) {
        ScanBudget b = scanBudget;
        if (b == null) {
            b = startupScanBudget(batch.ctx());
        }
        batch.set("hnsw.max_scan_tuples", Integer.toString(b.maxScanTuples()));
        batch.set("hnsw.scan_mem_multiplier", Integer.toString(b.memMultiplier()));
    }

    /** {@link #setSearchPlanCacheMode(DSLContext)} for a batch. */
    public static void setSearchPlanCacheMode(GucBatch batch) {
        batch.set("plan_cache_mode", "force_custom_plan");
    }
}
