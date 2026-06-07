package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Entry point for the nexus-service process.
 *
 * <p>Configuration (v1 bootstrap — all from environment):
 * <ul>
 *   <li>{@code NX_SERVICE_PORT} — listen port (default 8080)</li>
 *   <li>{@code NX_SERVICE_TOKEN} — bearer token for authentication</li>
 *   <li>{@code NX_DB_URL} — JDBC URL (e.g. {@code jdbc:postgresql://localhost/nexus})</li>
 *   <li>{@code NX_DB_USER} — database user</li>
 *   <li>{@code NX_DB_PASS} — database password</li>
 *   <li>{@code NX_POOL_SIZE} — HikariCP pool size (default 10)</li>
 * </ul>
 *
 * <p>Binds to {@code 127.0.0.1} only (loopback). No external TLS — forward proxy
 * or supervisor is responsible for TLS termination in production.
 */
public final class Main {

    private static final Logger log = LoggerFactory.getLogger(Main.class);

    public static void main(String[] args) throws Exception {
        int port   = intEnv("NX_SERVICE_PORT", 8080);
        String token  = requireEnv("NX_SERVICE_TOKEN");
        String dbUrl  = requireEnv("NX_DB_URL");
        String dbUser = requireEnv("NX_DB_USER");
        String dbPass = requireEnv("NX_DB_PASS");
        int poolSize  = intEnv("NX_POOL_SIZE", 10);

        var hikari = new HikariConfig();
        hikari.setJdbcUrl(dbUrl);
        hikari.setUsername(dbUser);
        hikari.setPassword(dbPass);
        hikari.setMaximumPoolSize(poolSize);
        hikari.setAutoCommit(true);   // pool default; TenantScope toggles to false per borrow
        var ds = new HikariDataSource(hikari);

        var service = new NexusService(port, token, ds);
        service.start();

        log.info("event=service_ready port={}", service.getPort());

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            log.info("event=shutdown_signal");
            service.stop();
            ds.close();
        }));

        // Block main thread until shutdown
        Thread.currentThread().join();
    }

    private static String requireEnv(String name) {
        String v = System.getenv(name);
        if (v == null) {
            throw new IllegalStateException("Required environment variable not set: " + name);
        }
        return v;
    }

    private static int intEnv(String name, int defaultValue) {
        String v = System.getenv(name);
        if (v == null || v.isBlank()) return defaultValue;
        return Integer.parseInt(v.trim());
    }
}
