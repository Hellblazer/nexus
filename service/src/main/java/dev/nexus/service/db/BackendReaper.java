/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.HexFormat;
import java.util.Properties;
import java.util.concurrent.ThreadLocalRandom;

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
    static final int CONNECT_TIMEOUT_SECONDS = 3;

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
        try (Connection c = DriverManager.getConnection(jdbcUrl, props);
             PreparedStatement ps = c.prepareStatement(
                 "SELECT pid, pg_terminate_backend(pid) FROM pg_stat_activity"
                 + " WHERE application_name = ? AND pid <> pg_backend_pid()")) {
            ps.setString(1, applicationName);
            int terminated = 0;
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    if (rs.getBoolean(2)) {
                        terminated++;
                    } else {
                        log.warn("event=backend_terminate_refused pid={}", rs.getInt(1));
                    }
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
        } catch (SQLException e) {
            log.warn("event=backend_reaper_failed application_name={} error=\"{}\"",
                     applicationName, e.getMessage());
            return -1;
        }
    }
}
