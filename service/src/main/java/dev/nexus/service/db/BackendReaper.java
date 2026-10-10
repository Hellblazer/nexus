/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.HexFormat;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.FutureTask;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static dev.nexus.service.jooq.nexus.Tables.TERMINATE_OWN_BACKENDS;

/**
 * Terminates this process's own Postgres backends at shutdown (nexus-g17tf).
 *
 * <p>Closing the HikariCP pool aborts the client sockets, but a CPU-bound
 * backend only notices a dead client when it next WRITES to that socket, so
 * a long vector scan survives its container: measured in production, a
 * {@code /v1/vectors/search} backend ran 8.9h after the deploy removed its
 * container, pinning xmin database-wide. The only thing that reaches such a
 * backend is a postmaster signal, which is what {@code pg_terminate_backend}
 * sends. It works for backends of the caller's own role without superuser.
 *
 * <p>Own backends are identified by a per-boot unique {@code application_name}
 * ({@link #newApplicationName}) stamped on every pooled connection, so a
 * concurrently running peer container (blue/green) is never touched.
 *
 * <p>The terminate runs on a FRESH connection, never one borrowed from the
 * pool: at the moment this matters the pool is exactly what the runaways are
 * holding, and a borrow would block the shutdown hook past the container's
 * stop grace period.
 */
public final class BackendReaper {

    private static final Logger log = LoggerFactory.getLogger(BackendReaper.class);

    /** Postgres truncates {@code application_name} to 63 bytes; this stays well under. */
    static final String APPLICATION_NAME_PREFIX = "nexus-service/";

    /** Connect + terminate must finish well inside a 10s container stop grace period. */
    public static final int CONNECT_TIMEOUT_SECONDS = 3;

    private BackendReaper() {
    }

    /** {@code nexus-service/<release>/<8 hex>} -- unique per process boot. */
    public static String newApplicationName(String releaseVersion) {
        String release = (releaseVersion == null || releaseVersion.isBlank()) ? "dev" : releaseVersion.trim();
        byte[] nonce = new byte[4];
        ThreadLocalRandom.current().nextBytes(nonce);
        return APPLICATION_NAME_PREFIX + release + "/" + HexFormat.of().formatHex(nonce);
    }

    /**
     * This boot's nonce: the last segment of the name {@link #newApplicationName} made. The per-collection index
     * builder (RDR-227) names its backend with it, so one boot has one id and the builder mints no second one.
     *
     * @throws IllegalArgumentException when {@code applicationName} has no non-empty last segment
     */
    public static String bootNonce(String applicationName) {
        int slash = applicationName == null ? -1 : applicationName.lastIndexOf('/');
        if (slash < 0 || slash == applicationName.length() - 1) {
            throw new IllegalArgumentException("not an application name from newApplicationName: " + applicationName);
        }
        return applicationName.substring(slash + 1);
    }

    /**
     * Terminate every backend whose {@code application_name} equals
     * {@code applicationName}, other than the one issuing the call.
     *
     * @return the number of backends signalled (each {@code pg_terminate_backend}
     *         that returned true); -1 when the reaper could not connect, which is
     *         logged and swallowed -- a shutdown hook must never wedge on it
     */
    public static int terminateOwnBackends(String jdbcUrl, String user, String password,
                                           String applicationName) {
        return terminateOwnBackends(jdbcUrl, user, password, applicationName, -1);
    }

    /**
     * As {@link #terminateOwnBackends(String, String, String, String)}, with the
     * pool's active-connection count at the moment of shutdown. A pool that
     * reports active borrows while the query matches NOTHING means the
     * {@code application_name} did not reach {@code pg_stat_activity} (a pooler
     * that strips startup parameters would do this) and the reaper is blind:
     * that is logged at WARN as {@code backend_reaper_matched_nothing}, never as
     * a normal-looking {@code count=0}.
     *
     * @param activeConnections the pool's active count, or -1 when unknown
     */
    public static int terminateOwnBackends(String jdbcUrl, String user, String password,
                                           String applicationName, int activeConnections) {
        Properties props = new Properties();
        props.setProperty("user", user);
        props.setProperty("password", password);
        props.setProperty("ApplicationName", applicationName + "/reaper");
        props.setProperty("connectTimeout", Integer.toString(CONNECT_TIMEOUT_SECONDS));
        props.setProperty("socketTimeout", Integer.toString(CONNECT_TIMEOUT_SECONDS * 2));
        // nexus-zrcj7: the raw PreparedStatement over pg_stat_activity +
        // pg_terminate_backend is retired onto backend-reaper-001's
        // nexus.terminate_own_backends(text) table function, called through
        // its jOOQ-generated table reference -- same FRESH, never-pool-
        // borrowed Connection as before, just wrapped in a DSLContext
        // (DSL.using(connection, ...), the same idiom PoolerModeCheck /
        // HealthHandler / VersionHandler already use for a bootstrap
        // connection) rather than a hand-built PreparedStatement.
        try (Connection c = DriverManager.getConnection(jdbcUrl, props)) {
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            var rows = ctx
                .selectFrom(TERMINATE_OWN_BACKENDS.call(applicationName))
                .fetch();
            int terminated = 0;
            for (var rec : rows) {
                if (Boolean.TRUE.equals(rec.get(TERMINATE_OWN_BACKENDS.TERMINATED))) {
                    terminated++;
                } else {
                    log.warn("event=backend_terminate_refused pid={}",
                             rec.get(TERMINATE_OWN_BACKENDS.PID));
                }
            }
            if (terminated == 0 && activeConnections > 0) {
                log.warn("event=backend_reaper_matched_nothing application_name={} "
                         + "pool_active={} hint=\"application_name did not reach "
                         + "pg_stat_activity; the reaper cannot see this process's backends\"",
                         applicationName, activeConnections);
            } else {
                log.info("event=own_backends_terminated application_name={} count={} pool_active={}",
                         applicationName, terminated, activeConnections);
            }
            return terminated;
        } catch (SQLException | DataAccessException e) {
            log.warn("event=backend_reaper_failed application_name={} error=\"{}\"",
                     applicationName, e.getMessage());
            return -1;
        }
    }

    /**
     * What the shutdown hook may spend ending Postgres backends: the pool's reaper and the builder's, together.
     *
     * <p><b>Worst case.</b> One {@link #terminateOwnBackends} call is bounded by its connect timeout
     * ({@value #CONNECT_TIMEOUT_SECONDS} s) plus one socket read (twice that, 6 s): 9 s. Two in sequence would reach
     * 18 s, past the container's 10 s stop grace, which is why the builder's call (RDR-227, nexus-43ulx.17) does not
     * follow the pool's. Both run on their own threads and the hook waits for both under one deadline of 9 s, so
     * the pair costs what one call always did. A call still running at the deadline is abandoned (its thread is a
     * daemon; the JVM is exiting) and reported as {@link #TIMED_OUT}.
     */
    public static final long SHUTDOWN_BUDGET_MILLIS = (CONNECT_TIMEOUT_SECONDS + CONNECT_TIMEOUT_SECONDS * 2L) * 1000;

    /** A call's result when it could not connect or threw; the same value {@link #terminateOwnBackends} returns. */
    public static final int FAILED = -1;

    /** A call's result when it was still running at the deadline. */
    public static final int TIMED_OUT = -2;

    /**
     * What {@link #terminateAtShutdown} ended: backends signalled by the pool's call and by the builder's, each
     * {@link #FAILED} or {@link #TIMED_OUT} when that call did not finish.
     */
    public record ShutdownReap(int pool, int builder) { }

    /**
     * End this boot's backends at shutdown: the pool's, by the application's role, and the per-collection index
     * builder's ({@code nexus-pci-builder-<nonce>}, RDR-227), by the admin role. The builder runs as the admin
     * role and {@code pg_terminate_backend} needs the target's role, so the application's credentials cannot reach
     * it. A long {@code CREATE INDEX CONCURRENTLY} is the backend this is for: closing the client socket does not
     * stop it.
     *
     * <p>Both calls run together under {@link #SHUTDOWN_BUDGET_MILLIS} (see there for the worst case). The admin
     * values are {@link AdminConnection#resolve} at boot: the application's own when no {@code NX_DB_ADMIN_*} is set.
     *
     * @param poolActive the pool's active-connection count, or -1 when unknown
     */
    public static ShutdownReap terminateAtShutdown(String poolUrl, String poolUser, String poolPassword,
                                                   String poolApplicationName, int poolActive,
                                                   AdminConnection admin, String builderApplicationName) {
        return terminateAtShutdown(poolUrl, poolUser, poolPassword, poolApplicationName, poolActive, admin,
            builderApplicationName, SHUTDOWN_BUDGET_MILLIS);
    }

    static ShutdownReap terminateAtShutdown(String poolUrl, String poolUser, String poolPassword,
                                            String poolApplicationName, int poolActive,
                                            AdminConnection admin, String builderApplicationName,
                                            long budgetMillis) {
        List<Integer> results = runConcurrently(List.of(
            () -> terminateOwnBackends(poolUrl, poolUser, poolPassword, poolApplicationName, poolActive),
            () -> terminateOwnBackends(admin.url(), admin.user(), admin.password(), builderApplicationName)),
            budgetMillis);
        return new ShutdownReap(results.get(0), results.get(1));
    }

    /**
     * Run {@code calls} each on its own daemon thread and collect their results under one deadline.
     *
     * @return one entry per call, in order: its result, {@link #FAILED} when it threw, {@link #TIMED_OUT} when it
     *         was still running {@code budgetMillis} after this method started
     */
    static List<Integer> runConcurrently(List<Callable<Integer>> calls, long budgetMillis) {
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(budgetMillis);
        List<FutureTask<Integer>> tasks = new ArrayList<>(calls.size());
        for (int i = 0; i < calls.size(); i++) {
            FutureTask<Integer> task = new FutureTask<>(calls.get(i));
            Thread t = new Thread(task, "backend-reaper-" + i);
            t.setDaemon(true);
            t.start();
            tasks.add(task);
        }
        List<Integer> results = new ArrayList<>(calls.size());
        for (FutureTask<Integer> task : tasks) {
            try {
                results.add(task.get(Math.max(0, deadline - System.nanoTime()), TimeUnit.NANOSECONDS));
            } catch (TimeoutException e) {
                task.cancel(true);
                log.warn("event=backend_reaper_timed_out budget_ms={}", budgetMillis);
                results.add(TIMED_OUT);
            } catch (ExecutionException e) {
                log.warn("event=backend_reaper_failed error=\"{}\"", e.getCause().toString());
                results.add(FAILED);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                task.cancel(true);
                results.add(TIMED_OUT);
            }
        }
        return results;
    }
}
