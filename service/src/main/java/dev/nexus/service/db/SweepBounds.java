package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.time.Duration;

/**
 * The execution bound shared by every arm of the scheduled sweep (nexus-lgiqw).
 *
 * <p>WHY THIS EXISTS AT ALL. {@code NexusService}'s {@code t1-ttl-sweep} cycle is
 * single-threaded across every tenant and all of its arms, and until this class no
 * arm set a {@code statement_timeout} — so one blocked statement stalled the entire
 * cycle, for every other tenant, with no ceiling. The failure was silent by
 * construction: a daemon thread, no exception, no restart, and the only signal was
 * the ABSENCE of the cycle's completion log line, which nothing alarms on.
 * Silent-and-unbounded is the worst pair available.
 *
 * <p>THE MECHANISM IS REMOTE BUT NOT HYPOTHETICAL. A {@code DELETE} holds
 * {@code RowExclusiveLock}, which conflicts with {@code ShareLock} and above, so DDL
 * against a swept table blocks it indefinitely: a manual migration, a
 * {@code VACUUM FULL}, or a non-{@code CONCURRENT} index build landing during a
 * cycle — including a rebuild of {@code idx_service_tokens_data_expires} itself.
 * Liquibase at engine boot cannot collide, because the first cycle runs six hours
 * after start.
 *
 * <p>WHY THE BOUND IS PER-TASK AND NOT PER-ARM. Bounding one arm leaves the others
 * unbounded on the same thread, so the CYCLE stays unbounded and the only thing
 * bought is an arm that behaves differently from its siblings. The hazard is a
 * property of the shared loop, so the bound belongs to the loop. Every arm the task
 * invokes takes this same value; the constant lives here, once, rather than being
 * restated per repository.
 *
 * <p>WHY A TIMEOUT AND NOT BATCHING. {@code statement_timeout} restarts its clock at
 * the START OF EACH STATEMENT, so N batched statements are bounded at N x the
 * constant rather than collectively — batching would have WEAKENED the bound it was
 * meant to provide. One statement with one timeout is the stronger guarantee. (Same
 * fact, independently confirmed against live PostgreSQL 17, is recorded on
 * {@code CatalogRepository}'s own sweep-gate constants.)
 *
 * <p>ABORTING IS SAFE HERE, which is what makes the trade cheap: every sweep is
 * idempotent and cumulative, so a cancelled statement simply leaves the rows for the
 * next cycle. The per-tenant {@code try/catch} in the sweep loop turns the resulting
 * PostgreSQL error ({@code 57014}, {@code query_canceled}) into a logged warning and
 * carries on with the remaining tenants and arms. A timeout that ABANDONED work
 * would be a different argument.
 *
 * <p>Callers OUTSIDE the scheduled task — {@code ScratchHandler}'s request-path
 * sweep, for instance — deliberately keep the unbounded overloads. This bound is a
 * property of the background task, not of the repositories.
 */
public final class SweepBounds {

    /**
     * Per-statement ceiling for sweep DELETEs.
     *
     * <p>Deliberately generous. The sweep runs every six hours with no latency
     * requirement, and the steady-state work is ~80 rows against a few-megabyte
     * table — so this should never fire in normal operation, and a value tight
     * enough to fire routinely would convert a non-problem into recurring aborted
     * cycles. Its job is to put a ceiling where there was none, not to be tight.
     * Worst case per cycle is {@code arms x tenants x} this value.
     */
    public static final Duration STATEMENT_TIMEOUT = Duration.ofSeconds(30);

    /**
     * Apply {@code statement_timeout} to the CURRENT TRANSACTION.
     *
     * <p>{@code is_local = true}, so the setting reverts when the transaction ends
     * and never leaks back into the pooled connection for whoever borrows it next.
     * That is load-bearing: these repositories hand connections back to a shared
     * HikariCP pool, and a session-level timeout set here would silently bound
     * unrelated request-path work on the same connection.
     *
     * @param tx      a {@link DSLContext} bound to an open transaction
     * @param timeout the ceiling; {@code null} applies nothing, which is how the
     *                unbounded (non-task) overloads preserve their prior behaviour
     */
    public static void applyStatementTimeout(DSLContext tx, Duration timeout) {
        if (timeout == null) {
            return;
        }
        tx.select(DSL.function("set_config", String.class,
                               DSL.val("statement_timeout"),
                               DSL.val(Long.toString(timeout.toMillis())),
                               DSL.val(true)))
          .fetch();
    }

    private SweepBounds() {
    }
}
