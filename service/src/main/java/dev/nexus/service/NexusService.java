package dev.nexus.service;

import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.http.AuthFilter;
import dev.nexus.service.http.HealthHandler;
import dev.nexus.service.http.WhoamiHandler;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.concurrent.Executors;

/**
 * RDR-152 skeleton service.
 *
 * <p>Binds to {@code 127.0.0.1} (loopback only). Port 0 assigns an ephemeral
 * port — used in tests. Production port is read from config/env (see {@link Main}).
 *
 * <p>Route table:
 * <ul>
 *   <li>{@code GET /health} — no auth; liveness + DB probe via SELECT 1.</li>
 *   <li>{@code GET /v1/_whoami} — auth filter + tenant extraction + GUC stamp.</li>
 * </ul>
 *
 * <p>Auth filter ({@link AuthFilter}) intercepts all {@code /v1/*} routes,
 * enforces Bearer token (constant-time compare), and extracts
 * {@code X-Nexus-Tenant} before dispatch.
 */
public final class NexusService {

    private static final Logger log = LoggerFactory.getLogger(NexusService.class);

    private final HttpServer server;
    private final TenantScope tenantScope;

    /**
     * @param port      listen port; 0 for OS-assigned ephemeral (use in tests)
     * @param token     expected bearer token (from NX_SERVICE_TOKEN env or config)
     * @param dataSource pooled connection source (HikariCP in production)
     */
    public NexusService(int port, String token, DataSource dataSource) throws IOException {
        this.tenantScope = new TenantScope(dataSource);

        this.server = HttpServer.create(
            new InetSocketAddress("127.0.0.1", port), /* backlog */ 10);

        // /health — unauthenticated
        server.createContext("/health", new HealthHandler(dataSource));

        // /v1/* — auth filter applied
        var whoamiCtx = server.createContext("/v1/", new WhoamiHandler(tenantScope));
        whoamiCtx.getFilters().addAll(List.of(new AuthFilter(token)));

        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
    }

    /** Start the HTTP server (non-blocking). */
    public void start() {
        server.start();
        log.info("event=service_started port={}", getPort());
    }

    /** Stop the HTTP server immediately. */
    public void stop() {
        server.stop(0);
        log.info("event=service_stopped");
    }

    /**
     * Actual bound port. Useful when constructed with port 0.
     */
    public int getPort() {
        return server.getAddress().getPort();
    }
}
