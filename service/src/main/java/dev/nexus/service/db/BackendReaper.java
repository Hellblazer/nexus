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
import java.util.Properties;
import java.util.concurrent.ThreadLocalRandom;

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

    /** Hex characters of the per-boot nonce ({@code newApplicationName} makes 4 random bytes). */
    static final int NONCE_HEX_CHARS = 8;

    /**
     * The longest pool name whose derived names still fit Postgres' 63 bytes: the lock connection appends
     * {@link EngineSingletonLock#NAME_SUFFIX}, and the reaper's {@code /reaper} is shorter. A name Postgres
     * truncates no longer equals the string compared against {@code pg_stat_activity}, and the nonce, which is
     * the end of the name, is what gets cut.
     */
    static final int MAX_POOL_NAME_BYTES = 63 - EngineSingletonLock.NAME_SUFFIX.length();

    /**
     * The longest release part: what is left of {@link #MAX_POOL_NAME_BYTES} after the prefix, the slash after
     * the release and the nonce. The release is the stamped release tag, which nothing else bounds.
     */
    static final int MAX_RELEASE_BYTES = MAX_POOL_NAME_BYTES - APPLICATION_NAME_PREFIX.length() - 1 - NONCE_HEX_CHARS;

    /**
     * {@code nexus-service/<release>/<8 hex>} -- unique per process boot. The release part is printable ASCII
     * (anything else becomes {@code _}) cut to {@link #MAX_RELEASE_BYTES} bytes, never the nonce.
     */
    public static String newApplicationName(String releaseVersion) {
        String release = (releaseVersion == null || releaseVersion.isBlank()) ? "dev" : releaseVersion.trim();
        // Postgres rewrites non-printable-ASCII bytes in application_name, so a name carrying them would never
        // equal the string compared against pg_stat_activity (the reaper's and the singleton lock's own match).
        release = release.replaceAll("[^\\x20-\\x7E]", "_");
        release = truncateUtf8(release, MAX_RELEASE_BYTES);
        byte[] nonce = new byte[4];
        ThreadLocalRandom.current().nextBytes(nonce);
        return APPLICATION_NAME_PREFIX + release + "/" + HexFormat.of().formatHex(nonce);
    }

    /** {@code s} cut to at most {@code maxBytes} UTF-8 bytes, on a character boundary. */
    static String truncateUtf8(String s, int maxBytes) {
        if (s.getBytes(java.nio.charset.StandardCharsets.UTF_8).length <= maxBytes) {
            return s;
        }
        StringBuilder out = new StringBuilder();
        int bytes = 0;
        for (int i = 0; i < s.length(); ) {
            int cp = s.codePointAt(i);
            int n = new String(Character.toChars(cp)).getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
            if (bytes + n > maxBytes) {
                break;
            }
            out.appendCodePoint(cp);
            bytes += n;
            i += Character.charCount(cp);
        }
        return out.toString();
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
     * End this boot's pool backends at shutdown (the application role's, by {@code application_name}). A long vector
     * scan is the backend this is for: closing the client socket does not stop it.
     *
     * <p><b>The per-collection index builder's backend is deliberately NOT ended here</b> (nexus-f3yqd). A
     * {@code CREATE INDEX CONCURRENTLY} can take tens of minutes, and ending it threw the work away at every restart,
     * so a store whose first rollout queued several large builds never finished the queue when the engine restarted
     * often. The build's session holds the builder's session advisory lock
     * ({@code PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY}, {@code pg_try_advisory_lock}) until it ends, so the
     * next engine's builder reports standby and starts no parallel build; when the build finishes the backend finds its
     * client gone and exits, and the next engine's pass adopts the finished index. A migration that has schema work
     * pending still ends it ({@code SchemaMigrator}). Stopping Postgres itself ends the build, and the next pass
     * rebuilds it.
     *
     * @param poolActive the pool's active-connection count, or -1 when unknown
     * @return the number of pool backends signalled, or {@code -1} when the reaper could not connect
     */
    public static int terminateAtShutdown(String poolUrl, String poolUser, String poolPassword,
                                          String poolApplicationName, int poolActive) {
        return terminateOwnBackends(poolUrl, poolUser, poolPassword, poolApplicationName, poolActive);
    }
}
