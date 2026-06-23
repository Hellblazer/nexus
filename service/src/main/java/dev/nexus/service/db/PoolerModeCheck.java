package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * nexus-bhzuv — fail-closed startup assertion that an interposed PgBouncer is in
 * {@code transaction} pool mode.
 *
 * <p>{@link TenantScope#withTenant} stamps the tenant with {@code set_config('nexus.tenant',
 * ?, true)} — SET LOCAL (transaction-local) semantics. Under a SESSION-mode pooler the GUC
 * is not reset at transaction boundary, so the backend returns to the pool still carrying
 * the previous tenant's {@code nexus.tenant}; the next borrower inherits it and FORCE RLS
 * evaluates as the wrong tenant → cross-tenant read. Only transaction-mode pooling is safe.
 *
 * <p>The checked-in {@code service/deploy/pgbouncer.ini} is the source of truth for the
 * deployed config; this is the runtime backstop that refuses to serve against a pooler that
 * does not actually report {@code pool_mode = transaction}.
 *
 * <p>NO-OP on the direct-PG (v1, no pooler) path: when {@code NX_PGBOUNCER_ADMIN_URL} is
 * unset the check returns immediately, so local/dev {@code nx init --service} is unaffected.
 */
public final class PoolerModeCheck {

    private static final Logger log = LoggerFactory.getLogger(PoolerModeCheck.class);

    /** JDBC URL of the PgBouncer admin pseudo-database. Unset ⇒ no pooler ⇒ check no-ops. */
    public static final String ADMIN_URL_ENV = "NX_PGBOUNCER_ADMIN_URL";
    public static final String ADMIN_USER_ENV = "NX_PGBOUNCER_ADMIN_USER";
    public static final String ADMIN_PASS_ENV = "NX_PGBOUNCER_ADMIN_PASS";

    private static final String REQUIRED_POOL_MODE = "transaction";

    private PoolerModeCheck() { }

    /** Raised when the live pooler is not in transaction mode (or cannot be probed). */
    public static final class PoolerModeException extends RuntimeException {
        public PoolerModeException(String message) { super(message); }
        public PoolerModeException(String message, Throwable cause) { super(message, cause); }
    }

    /**
     * Pure extraction: pull the {@code pool_mode} value out of a {@code SHOW CONFIG}
     * key→value map. Returns {@code null} when the key is absent (older/unknown pooler).
     */
    public static String extractPoolMode(Map<String, String> showConfig) {
        return showConfig == null ? null : showConfig.get("pool_mode");
    }

    /**
     * Pure assertion: throw unless {@code poolMode} is exactly {@code "transaction"}.
     * Fail-closed — a null/blank/unknown value is a refusal, never a warn-and-continue.
     */
    public static void assertTransactionMode(String poolMode) {
        if (poolMode == null || poolMode.isBlank()) {
            throw new PoolerModeException(
                    "pgbouncer pool_mode could not be determined (SHOW CONFIG had no pool_mode "
                    + "row); refusing to serve — a session-mode pooler leaks the tenant GUC "
                    + "across borrows (nexus-bhzuv)");
        }
        if (!REQUIRED_POOL_MODE.equals(poolMode)) {
            throw new PoolerModeException(
                    "pgbouncer pool_mode=" + poolMode + " but '" + REQUIRED_POOL_MODE
                    + "' is required: SET LOCAL tenant GUC leaks across borrows under "
                    + poolMode + "-mode pooling → cross-tenant read (nexus-bhzuv). Fix the "
                    + "deployed pooler (see service/deploy/pgbouncer.ini)");
        }
    }

    /**
     * Startup backstop. No-op when {@link #ADMIN_URL_ENV} is unset (direct-PG path).
     * Otherwise probes the PgBouncer admin console and throws {@link PoolerModeException}
     * unless {@code pool_mode = transaction}. Reads env via {@link System#getenv}.
     */
    public static void verifyAtStartup() {
        verifyAtStartup(System.getenv(ADMIN_URL_ENV),
                        System.getenv(ADMIN_USER_ENV),
                        System.getenv(ADMIN_PASS_ENV));
    }

    /** Testable form of {@link #verifyAtStartup()} with explicit admin connection params. */
    public static void verifyAtStartup(String adminUrl, String adminUser, String adminPass) {
        if (adminUrl == null || adminUrl.isBlank()) {
            // HONEST phrasing (nexus-bhzuv review HIGH-3): absence of the admin URL means
            // the probe is DISABLED, not that no pooler exists. If a deployment interposes
            // PgBouncer but omits NX_PGBOUNCER_ADMIN_URL, the transaction-mode invariant
            // goes unenforced — the env var is REQUIRED for any pooled deployment.
            log.warn("event=pooler_mode_check_skipped reason=no_pgbouncer_admin_url "
                    + "note=\"NX_PGBOUNCER_ADMIN_URL unset — pooler-mode probe DISABLED. "
                    + "Safe only for the direct-PG (no pooler) path. If PgBouncer is "
                    + "interposed you MUST set NX_PGBOUNCER_ADMIN_URL or a session-mode "
                    + "pooler can leak the tenant GUC across borrows (nexus-bhzuv).\"");
            return;
        }
        Map<String, String> config;
        try {
            config = fetchShowConfig(adminUrl, adminUser, adminPass);
        } catch (SQLException e) {
            throw new PoolerModeException(
                    "could not probe PgBouncer admin console at " + adminUrl
                    + " to verify pool_mode (nexus-bhzuv)", e);
        }
        String poolMode = extractPoolMode(config);
        assertTransactionMode(poolMode);
        log.info("event=pooler_mode_check_ok pool_mode={}", poolMode);
    }

    private static Map<String, String> fetchShowConfig(String adminUrl, String user, String pass)
            throws SQLException {
        Map<String, String> rows = new LinkedHashMap<>();
        // Bound the connect/socket time (nexus-bhzuv review HIGH-1): without this a dead or
        // firewalled admin endpoint hangs the main thread at startup forever, turning a
        // fail-fast check into fail-never. Append driver timeouts unless the URL sets its own.
        String timedUrl = adminUrl.contains("connectTimeout=") || adminUrl.contains("socketTimeout=")
                ? adminUrl
                : adminUrl + (adminUrl.contains("?") ? "&" : "?") + "connectTimeout=10&socketTimeout=10";
        try (Connection conn = DriverManager.getConnection(timedUrl, user, pass);
             Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SHOW CONFIG")) {
            // PgBouncer SHOW CONFIG returns columns: key, value, [changeable].
            while (rs.next()) {
                rows.put(rs.getString("key"), rs.getString("value"));
            }
        }
        return rows;
    }
}
