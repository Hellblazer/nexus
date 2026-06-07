package dev.nexus.service;

import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.PlanRepository;
import dev.nexus.service.db.ScratchRepository;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.http.AuthFilter;
import dev.nexus.service.http.HealthHandler;
import dev.nexus.service.http.MemoryHandler;
import dev.nexus.service.http.PlanHandler;
import dev.nexus.service.http.ScratchHandler;
import dev.nexus.service.http.TaxonomyHandler;
import dev.nexus.service.http.TelemetryHandler;
import dev.nexus.service.http.WhoamiHandler;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

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
 *   <li>{@code /v1/t1/*} — T1 scratch: put/get/search/list/flag/session-close (bead nexus-gmiaf.13).</li>
 * </ul>
 *
 * <p>Auth filter ({@link AuthFilter}) intercepts all {@code /v1/*} routes,
 * enforces Bearer token (constant-time compare), and extracts
 * {@code X-Nexus-Tenant} before dispatch.
 */
public final class NexusService {

    private static final Logger log = LoggerFactory.getLogger(NexusService.class);

    /** How often to run the per-default-tenant TTL sweep (crash-safety backstop). */
    private static final long SWEEP_INTERVAL_HOURS = 6L;

    /** Age threshold: scratch rows older than this are eligible for TTL sweep. */
    private static final long SWEEP_TTL_HOURS = 24L;

    /** Default tenant used for the internal sweeper. Cross-tenant sweep deferred to bead .30. */
    private static final String DEFAULT_TENANT = "default";

    private final HttpServer server;
    private final TenantScope tenantScope;
    private final ScheduledExecutorService sweepScheduler;

    /**
     * @param port      listen port; 0 for OS-assigned ephemeral (use in tests)
     * @param token     expected bearer token (from NX_SERVICE_TOKEN env or config)
     * @param dataSource pooled connection source (HikariCP in production)
     */
    public NexusService(int port, String token, DataSource dataSource) throws IOException {
        this.tenantScope = new TenantScope(dataSource);
        var memoryRepo    = new MemoryRepository(tenantScope);
        var planRepo      = new PlanRepository(tenantScope);
        var telemetryRepo = new TelemetryRepository(tenantScope);
        var scratchRepo   = new ScratchRepository(tenantScope);
        var taxonomyRepo  = new TaxonomyRepository(tenantScope);

        this.server = HttpServer.create(
            new InetSocketAddress("127.0.0.1", port), /* backlog */ 10);

        // /health — unauthenticated
        server.createContext("/health", new HealthHandler(dataSource));

        // /v1/* — auth filter applied
        var authFilter = List.of(new AuthFilter(token));

        var whoamiCtx = server.createContext("/v1/_whoami", new WhoamiHandler(tenantScope));
        whoamiCtx.getFilters().addAll(authFilter);

        // /v1/memory/* — memory endpoints
        var memCtx = server.createContext("/v1/memory", new MemoryHandler(memoryRepo));
        memCtx.getFilters().addAll(authFilter);

        // /v1/plans/* — plan library endpoints (bead nexus-gmiaf.11)
        var planCtx = server.createContext("/v1/plans", new PlanHandler(planRepo));
        planCtx.getFilters().addAll(authFilter);

        // /v1/telemetry/* — telemetry endpoints (bead nexus-gmiaf.12)
        var telCtx = server.createContext("/v1/telemetry", new TelemetryHandler(telemetryRepo));
        telCtx.getFilters().addAll(authFilter);

        // /v1/t1/* — T1 scratch endpoints (bead nexus-gmiaf.13)
        var t1Ctx = server.createContext("/v1/t1", new ScratchHandler(scratchRepo));
        t1Ctx.getFilters().addAll(authFilter);

        // /v1/taxonomy/* — taxonomy endpoints (bead nexus-gmiaf.14)
        var taxonomyCtx = server.createContext("/v1/taxonomy", new TaxonomyHandler(taxonomyRepo));
        taxonomyCtx.getFilters().addAll(authFilter);

        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());

        // TTL sweep: crash-safety backstop for sessions that never called session-close.
        // Runs sweepTenant() for the default tenant every SWEEP_INTERVAL_HOURS.
        // Cross-tenant superuser sweep (sweepExpired) deferred to bead .30.
        this.sweepScheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "t1-ttl-sweep");
            t.setDaemon(true);
            return t;
        });
        this.sweepScheduler.scheduleAtFixedRate(
            () -> {
                try {
                    OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC)
                        .minusHours(SWEEP_TTL_HOURS);
                    int deleted = scratchRepo.sweepTenant(DEFAULT_TENANT, cutoff);
                    log.info("event=t1_scheduled_sweep tenant={} deleted={}", DEFAULT_TENANT, deleted);
                } catch (Exception ex) {
                    log.warn("event=t1_scheduled_sweep_failed error={}", ex.getMessage(), ex);
                }
            },
            SWEEP_INTERVAL_HOURS, SWEEP_INTERVAL_HOURS, TimeUnit.HOURS
        );
    }

    /** Start the HTTP server (non-blocking). */
    public void start() {
        server.start();
        log.info("event=service_started port={}", getPort());
    }

    /** Stop the HTTP server and TTL sweep scheduler immediately. */
    public void stop() {
        sweepScheduler.shutdownNow();
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
