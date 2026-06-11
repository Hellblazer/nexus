package dev.nexus.service;

import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.PlanRepository;
import dev.nexus.service.db.ScratchRepository;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenCache;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.http.AspectHandler;
import dev.nexus.service.http.AuthFilter;
import dev.nexus.service.http.CatalogHandler;
import dev.nexus.service.http.ChashHandler;
import dev.nexus.service.http.HealthHandler;
import dev.nexus.service.http.VersionHandler;
import dev.nexus.service.http.MemoryHandler;
import dev.nexus.service.http.PlanHandler;
import dev.nexus.service.http.ScratchHandler;
import dev.nexus.service.http.SessionTokenHandler;
import dev.nexus.service.http.TaxonomyHandler;
import dev.nexus.service.http.TelemetryHandler;
import dev.nexus.service.http.TokenAdminHandler;
import dev.nexus.service.http.VectorHandler;
import dev.nexus.service.http.WhoamiHandler;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
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
    private final TokenStore tokenStore;
    private final TokenCache tokenCache;

    /**
     * Convenience constructor: no vector backend (original signature for existing tests).
     * The {@code /v1/vectors/*} routes answer 503 (explicit refusal, never a 404 or NPE).
     *
     * @param port      listen port; 0 for OS-assigned ephemeral (use in tests)
     * @param token     expected bearer token (from NX_SERVICE_TOKEN env or config)
     * @param dataSource pooled connection source (HikariCP in production)
     */
    public NexusService(int port, String token, DataSource dataSource) throws IOException {
        this(port, token, dataSource, null, null);
    }

    /**
     * Embed-only constructor (parity-gate mode, nexus-gmiaf.21): {@code /v1/vectors/embed}
     * is live, every storage/query route answers 503.
     *
     * @param port              listen port; 0 for OS-assigned ephemeral (use in tests)
     * @param token             expected bearer token
     * @param dataSource        pooled connection source
     * @param docEmbedderRouter EmbedderRouter for {@code /v1/vectors/embed} (may be null)
     */
    public NexusService(int port, String token, DataSource dataSource,
                        EmbedderRouter docEmbedderRouter) throws IOException {
        this(port, token, dataSource, docEmbedderRouter, null);
    }

    /**
     * Deprecated 6-arg bridge for pre-P4a callers (RDR-155 P4a.2, bead nexus-1k8s1).
     *
     * <p>The fourth parameter is the RETIRED Chroma vector-repository slot —
     * the Phase 4a serving cutover removed Chroma from the serving wiring, so the
     * slot survives only because the locked P4a.1 contract suite
     * ({@code PgVectorServingContractTest}) pins this call shape with {@code null}
     * in the slot. Passing anything non-null fails loud.
     *
     * @param retiredChromaRepositorySlot MUST be null — the Chroma serving backend
     *                                    is retired (pgvector serves all vector routes)
     * @deprecated use {@link #NexusService(int, String, DataSource, EmbedderRouter,
     *             PgVectorRepository)}; this bridge is deleted with the Phase 4b
     *             Chroma removal (gated on P5.G)
     */
    @Deprecated(forRemoval = true)
    public NexusService(int port, String token, DataSource dataSource,
                        Object retiredChromaRepositorySlot,
                        EmbedderRouter docEmbedderRouter,
                        PgVectorRepository pgVectorRepository) throws IOException {
        // Validation happens INSIDE the delegation expression so it runs BEFORE
        // any resource creation — a post-this() check would leak the bound
        // HTTP socket and the started sweep-scheduler thread on rejection
        // (P4a.2 dual-review finding M-1/A-1).
        this(port, token, dataSource,
             requireRetiredSlotNull(retiredChromaRepositorySlot, docEmbedderRouter),
             pgVectorRepository);
    }

    /** Fail-loud gate for the retired Chroma slot; returns the router unchanged. */
    private static EmbedderRouter requireRetiredSlotNull(
            Object retiredChromaRepositorySlot, EmbedderRouter docEmbedderRouter) {
        if (retiredChromaRepositorySlot != null) {
            throw new IllegalArgumentException(
                "the Chroma repository slot is retired (RDR-155 Phase 4a): vector serving "
                + "routes exclusively through PgVectorRepository — pass null or use the "
                + "5-arg constructor");
        }
        return docEmbedderRouter;
    }

    /**
     * Full constructor — the production wiring (RDR-155 P4a.2, bead nexus-1k8s1).
     *
     * @param port              listen port; 0 for OS-assigned ephemeral (use in tests)
     * @param token             expected bearer token
     * @param dataSource        pooled connection source
     * @param docEmbedderRouter optional EmbedderRouter for {@code /v1/vectors/embed}
     *                          (may be null — /embed answers 503, the pinned
     *                          absent-router invariant)
     * @param pgVectorRepository optional PgVectorRepository serving every
     *                          {@code /v1/vectors/*} storage/query route (may be null —
     *                          those routes answer 503)
     */
    public NexusService(int port, String token, DataSource dataSource,
                        EmbedderRouter docEmbedderRouter,
                        PgVectorRepository pgVectorRepository) throws IOException {
        this.tenantScope = new TenantScope(dataSource);

        // Token lifecycle (RDR-152 bead nexus-gmiaf.32.2): resolve bearer→tenant
        // server-side against the service_tokens registry (RLS-off, read pre-context
        // via a plain DataSource path), fronted by a bounded positive cache. The
        // constructor performs NO DB writes — bootstrap-token provisioning is an
        // explicit post-migration step (see Main.seedBootstrapToken / Phase E
        // nexus-gmiaf.32.5), so constructing the service has no schema side effect.
        // The `token` parameter is retained for source/signature compatibility but is
        // no longer the auth secret (auth is registry-backed).
        this.tokenStore = new TokenStore(dataSource, java.time.Clock.systemUTC());
        this.tokenCache = new TokenCache(tokenStore, java.time.Clock.systemUTC());

        var memoryRepo    = new MemoryRepository(tenantScope);
        var planRepo      = new PlanRepository(tenantScope);
        var telemetryRepo = new TelemetryRepository(tenantScope);
        var scratchRepo   = new ScratchRepository(tenantScope);
        var taxonomyRepo  = new TaxonomyRepository(tenantScope);
        var aspectRepo    = new AspectRepository(tenantScope);
        var chashRepo     = new ChashRepository(tenantScope);
        var catalogRepo   = new CatalogRepository(tenantScope);

        this.server = HttpServer.create(
            new InetSocketAddress("127.0.0.1", port), /* backlog */ 10);

        // /health — unauthenticated
        server.createContext("/health", new HealthHandler(dataSource));

        // /version — unauthenticated app+schema handshake (nexus-pebfx.4)
        server.createContext("/version", new VersionHandler(dataSource));

        // /v1/* — auth filter applied
        var authFilter = List.of(new AuthFilter(tokenCache, tokenStore));

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

        // /v1/aspects/* — aspects / highlights / queue / promotion-log (bead nexus-gmiaf.15)
        var aspectCtx = server.createContext("/v1/aspects", new AspectHandler(aspectRepo));
        aspectCtx.getFilters().addAll(authFilter);

        // /v1/chash/* — chash_index endpoints (bead nexus-gmiaf.16)
        var chashCtx = server.createContext("/v1/chash", new ChashHandler(chashRepo));
        chashCtx.getFilters().addAll(authFilter);

        // /v1/catalog/* — catalog endpoints (bead nexus-gmiaf.18)
        var catalogCtx = server.createContext("/v1/catalog", new CatalogHandler(catalogRepo));
        catalogCtx.getFilters().addAll(authFilter);

        // /v1/tenants/* + /v1/service-tokens/* — token lifecycle admin (bead nexus-gmiaf.32.3).
        // Shares the live tokenStore + tokenCache so revoke invalidates the cache AuthFilter reads.
        var tokenAdmin = new TokenAdminHandler(tokenStore, tokenCache, java.time.Clock.systemUTC());
        var tenantsCtx = server.createContext("/v1/tenants", tokenAdmin);
        tenantsCtx.getFilters().addAll(authFilter);
        var svcTokensCtx = server.createContext("/v1/service-tokens", tokenAdmin);
        svcTokensCtx.getFilters().addAll(authFilter);

        // /v1/sessions/* — per-session token mint/close (bead nexus-gmiaf.32.4). Tenant from
        // the authenticated bearer; the MCP lifespan mints on session start, closes on end.
        var sessionsCtx = server.createContext("/v1/sessions", new SessionTokenHandler(tokenStore));
        sessionsCtx.getFilters().addAll(authFilter);

        // /v1/vectors/* — vector endpoints (bead nexus-gmiaf.20; hybrid: RDR-155 P3.2;
        // pgvector serving cutover: RDR-155 P4a.2, bead nexus-1k8s1). Always registered:
        // the handler answers an explicit 503 per route when its backend (pgvector
        // repository for storage/query, embedder router for /embed) is absent — a
        // missing backend is a refusal, never a 404 that masquerades as an unknown route.
        var vectorCtx = server.createContext("/v1/vectors",
                new VectorHandler(docEmbedderRouter, pgVectorRepository));
        vectorCtx.getFilters().addAll(authFilter);
        log.info("event=vector_endpoints_registered has_embed_router={} has_pgvector={}",
                docEmbedderRouter != null, pgVectorRepository != null);

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

    /**
     * The live token cache the AuthFilter reads on every request. Phase C's
     * revoke/rotate endpoint MUST call {@code getTokenCache().invalidate(hash)} on this
     * instance for immediate revocation — allocating a separate TokenCache would no-op
     * against the cache actually serving requests (RDR-152 bead nexus-gmiaf.32.2).
     */
    public TokenCache getTokenCache() {
        return tokenCache;
    }

    /** The token store backing auth resolution (shared seam for Phase C/E lifecycle ops). */
    public TokenStore getTokenStore() {
        return tokenStore;
    }
}
