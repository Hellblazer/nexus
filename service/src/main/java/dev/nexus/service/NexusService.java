package dev.nexus.service;

import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.PipelineRepository;
import dev.nexus.service.db.PlanRepository;
import dev.nexus.service.db.RemapRepository;
import dev.nexus.service.db.ScratchRepository;
import dev.nexus.service.db.SweepBounds;
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
import dev.nexus.service.http.LivezHandler;
import dev.nexus.service.http.VersionHandler;
import dev.nexus.service.http.LadderHandler;
import dev.nexus.service.http.MemoryHandler;
import dev.nexus.service.http.PipelineHandler;
import dev.nexus.service.http.PlanHandler;
import dev.nexus.service.http.RemapHandler;
import dev.nexus.service.http.StagingHandler;
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
import java.time.Duration;
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
 *   <li>{@code GET /health} — no auth; READINESS: DB probe via SELECT 1.</li>
 *   <li>{@code GET /livez} — no auth; LIVENESS only, zero dependencies.</li>
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

    /**
     * How far past {@code expires_at} a {@code scope=data} service token must be
     * before the reaper deletes it (nexus-lgiqw).
     *
     * <p>Not arbitrary, and not a round number chosen for comfort: nothing else in
     * the database records that a token was minted, so the row is the only DB
     * evidence of it. What outlives the row is the control plane's
     * {@code engine_data_token_mint_ok} CloudWatch line, retained 30 days. The
     * window must therefore stay SHORTER than that retention so the log always
     * outlives the row; 7 leaves margin if the log retention is cut again.
     */
    private static final long SWEEP_DATA_TOKEN_GRACE_DAYS = 7L;

    /**
     * nexus-4tosp. Consecutive cycles whose data-token arm reaped nothing AND
     * failed for every tenant, before the arm is reported stalled. Three, not
     * one: at {@link #SWEEP_INTERVAL_HOURS} that is 18h, so a single transient
     * DB blip does not raise a wolf — and a check that cries wolf gets blessed
     * reflexively and stops meaning anything, which is the failure mode on the
     * far side of this fix.
     */
    static final int DATA_SWEEP_STALL_CYCLES = 3;

    /** Always-swept tenant; the sweeper additionally loops every token-bearing tenant (nexus-4qq1m). */
    private static final String DEFAULT_TENANT = "default";

    private final HttpServer server;
    private final TenantScope tenantScope;
    private final ScheduledExecutorService sweepScheduler;
    private final TokenStore tokenStore;
    /** Held as a field, not a constructor local, so {@link #runScheduledSweep}
     *  can reach it — the scratch arm of the sweep loop (nexus-lgiqw). */
    private final ScratchRepository scratchRepo;
    private final TokenCache tokenCache;
    /** nexus-4tosp: consecutive cycles the data-token arm failed outright. */
    private final java.util.concurrent.atomic.AtomicInteger consecutiveDataSweepFailures =
        new java.util.concurrent.atomic.AtomicInteger();

    /**
     * nexus-4tosp: PER-TENANT consecutive data-token sweep failures, keyed by
     * tenant and cleared (entry removed) on that tenant's next successful
     * sweep. Complements {@link #consecutiveDataSweepFailures} above, which
     * only trips when EVERY tenant fails in the same cycle and is therefore
     * blind to the bead's headline scenario: one tenant's backlog exceeds the
     * statement bound every cycle while other tenants keep succeeding.
     */
    private final java.util.concurrent.ConcurrentHashMap<String, Integer> dataSweepFailuresByTenant =
        new java.util.concurrent.ConcurrentHashMap<>();

    /**
     * nexus-4tosp test seam: overrides the per-tenant data-token sweep call
     * (tenant -&gt; deleted count; throw to simulate that tenant's failure) so
     * a unit test can exercise per-tenant escalation without a live 30s
     * statement-timeout race. Null in production, where the real
     * {@link TokenStore#sweepExpiredDataTokens} call is used. Package-private
     * so a same-package test can set it directly.
     */
    java.util.function.Function<String, Integer> dataTokenSweepOverride;

    /**
     * nexus-tyxnh: owns the post-commit purge-trash VACUUM executor; shut down in
     * {@link #stop()} alongside {@code sweepScheduler}.
     */
    private final CatalogRepository catalogRepo;

    /**
     * hygiene-001 follow-on (coordinator scope addition): {@link
     * #runScheduledSweep}'s plans-expiry arm needs it; PlanHandler needs it
     * too, so it is a field rather than a constructor-local like scratchRepo
     * and catalogRepo above.
     */
    private final PlanRepository planRepo;

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
     * The address the HTTP server binds. Defaults to loopback ({@code 127.0.0.1});
     * {@code NX_SERVICE_BIND} overrides it (e.g. {@code 0.0.0.0}) for container
     * hosting where a peer must reach the service across a network namespace.
     *
     * <p><strong>Security:</strong> the service has no external TLS (forward proxy
     * / supervisor terminates TLS in production). Binding beyond loopback exposes a
     * token-authed but plaintext service, so a non-loopback bind is logged loudly
     * and is intended only for trusted container networking.
     */
    static String resolveBindHost() {
        return resolveBindHost(System.getenv("NX_SERVICE_BIND"));
    }

    /** Pure resolution (testable): {@code null}/blank → loopback; otherwise the
     *  trimmed value, with a loud security warning for any non-loopback bind. */
    static String resolveBindHost(String envValue) {
        if (envValue == null || envValue.isBlank()) {
            return "127.0.0.1";
        }
        String bind = envValue.trim();
        // Normalize "localhost" → "127.0.0.1": InetSocketAddress resolves the name
        // at bind time, and on IPv6-only-loopback /etc/hosts it would bind [::1]
        // while the supervisor + Python clients connect to 127.0.0.1 → refused
        // (code-review H-1). An explicit "::1" is left as-is (deliberate IPv6).
        if (bind.equals("localhost")) {
            return "127.0.0.1";
        }
        if (!bind.equals("127.0.0.1") && !bind.equals("::1")) {
            log.warn("event=service_bind_non_loopback bind={} security=\"no external TLS; "
                    + "token-authed plaintext (and unauthenticated /health, /version) "
                    + "exposed beyond loopback — intended only for trusted container "
                    + "networking\"", bind);
        }
        return bind;
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
        this(port, token, dataSource, docEmbedderRouter, pgVectorRepository, null);
    }

    /**
     * Full constructor with the fused-rerank scorer (RDR-188, bead nexus-9o6y2.2).
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
     * @param reranker          optional scorer for the fused rerank stage on the search
     *                          routes (may be null — {@code rerank=true} requests
     *                          degrade LOUD with a structured {@code rerank_error})
     */
    public NexusService(int port, String token, DataSource dataSource,
                        EmbedderRouter docEmbedderRouter,
                        PgVectorRepository pgVectorRepository,
                        dev.nexus.service.vectors.Reranker reranker) throws IOException {
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
        // Field, not a constructor local (mirrors scratchRepo/catalogRepo above):
        // runScheduledSweep's plan-expiry arm (hygiene-001 follow-on) needs it.
        this.planRepo     = new PlanRepository(tenantScope);
        var telemetryRepo = new TelemetryRepository(tenantScope);
        this.scratchRepo  = new ScratchRepository(tenantScope);
        var taxonomyRepo  = new TaxonomyRepository(tenantScope);
        var taxonomyCentroidRepo = new dev.nexus.service.vectors.TaxonomyCentroidRepository(tenantScope);
        var aspectRepo    = new AspectRepository(tenantScope);
        var chashRepo     = new ChashRepository(tenantScope);
        var remapRepo     = new RemapRepository(tenantScope);
        var ladderRepo    = new LadderRepository(tenantScope);
        var pipelineRepo  = new PipelineRepository(tenantScope);
        this.catalogRepo  = new CatalogRepository(tenantScope);

        this.server = HttpServer.create(
            new InetSocketAddress(resolveBindHost(), port), /* backlog */ 10);

        // /health — unauthenticated. READINESS: includes a DB probe.
        server.createContext("/health", new HealthHandler(dataSource));
        // /livez — unauthenticated. LIVENESS: no dependency of any kind, so a
        // saturated pool cannot make a live process look dead (nexus-hubc0 /
        // nexus-7f7gb). This is the supervisor's restart authority; /health is
        // not, because a 503 from it means "alive but not serving".
        server.createContext("/livez", new LivezHandler());

        // /version — unauthenticated app+schema+embedding-mode handshake
        // (nexus-pebfx.4 + nexus-pebfx.5)
        server.createContext("/version", new VersionHandler(dataSource, docEmbedderRouter));

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
        var taxonomyCtx = server.createContext("/v1/taxonomy", new TaxonomyHandler(taxonomyRepo, taxonomyCentroidRepo));
        taxonomyCtx.getFilters().addAll(authFilter);

        // /v1/aspects/* — aspects / highlights / queue / promotion-log (bead nexus-gmiaf.15)
        var aspectCtx = server.createContext("/v1/aspects", new AspectHandler(aspectRepo));
        aspectCtx.getFilters().addAll(authFilter);

        // /v1/chash/* — chash_index endpoints (bead nexus-gmiaf.16)
        var chashCtx = server.createContext("/v1/chash", new ChashHandler(chashRepo));
        chashCtx.getFilters().addAll(authFilter);

        // /v1/remap/* — chash_remap endpoints (RDR-186 nexus-146xx.4: wire re-id
        // map write-through + per-leg clear + live membership counts). The
        // RDR-180 per-tenant full-digest rekey (nexus-jxizy.6/nexus-b878d) was
        // RETIRED with nexus.chash_alias (nexus-lgdel.l1) — see RemapHandler's
        // class javadoc.
        var remapCtx = server.createContext("/v1/remap", new RemapHandler(remapRepo));
        remapCtx.getFilters().addAll(authFilter);

        // /v1/staging/* — RDR-180 land-then-transform (nexus-jxizy.10.4):
        // verbatim landing + embed-fill + in-DB promote/finalize + clear/counts
        var stagingCtx = server.createContext("/v1/staging",
                new StagingHandler(tenantScope,
                    new dev.nexus.service.db.StagingPromoteOps(tenantScope),
                    docEmbedderRouter));
        stagingCtx.getFilters().addAll(authFilter);

        // /v1/ladder/* — upgrade-ladder completion bookkeeping (RDR-186
        // nexus-146xx.12: the ladder.db retirement's PG write/read surface)
        var ladderCtx = server.createContext("/v1/ladder", new LadderHandler(ladderRepo));
        ladderCtx.getFilters().addAll(authFilter);

        // /v1/pipeline/* — engine-hosted streaming-PDF buffer (RDR-186
        // nexus-146xx.16: pipeline.db's PG twin; state hosts here, the
        // extraction compute stays client-side)
        var pipelineCtx = server.createContext("/v1/pipeline", new PipelineHandler(pipelineRepo));
        pipelineCtx.getFilters().addAll(authFilter);

        // /v1/catalog/* — catalog endpoints (bead nexus-gmiaf.18). The
        // combined-write orchestration seam (nexus-kl2z6 increment 1) rides
        // the SAME embedder router VectorHandler/PgVectorRepository use —
        // null when no router is wired (matches every other embed-dependent
        // capability's absent-backend 503, never a partially-wired NPE).
        var combinedWriteService = docEmbedderRouter != null
            ? new dev.nexus.service.db.CombinedWriteService(tenantScope, catalogRepo, docEmbedderRouter)
            : null;
        var catalogCtx = server.createContext("/v1/catalog",
                new CatalogHandler(catalogRepo, combinedWriteService));
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

        // /v1/data-tokens/* — short-TTL per-tenant DATA tokens minted JIT by a
        // scope=mint credential (nexus-x1h07, conexus RDR-005 A1). Rate-limited
        // + TTL-ceilinged from env; the AuthFilter confines mint credentials to
        // exactly this surface.
        var dataTokensCtx = server.createContext("/v1/data-tokens",
                dev.nexus.service.http.DataTokenHandler.fromEnv(
                    tokenStore, java.time.Clock.systemUTC()));
        dataTokensCtx.getFilters().addAll(authFilter);

        // /v1/vectors/* — vector endpoints (bead nexus-gmiaf.20; hybrid: RDR-155 P3.2;
        // pgvector serving cutover: RDR-155 P4a.2, bead nexus-1k8s1). Always registered:
        // the handler answers an explicit 503 per route when its backend (pgvector
        // repository for storage/query, embedder router for /embed) is absent — a
        // missing backend is a refusal, never a 404 that masquerades as an unknown route.
        var vectorCtx = server.createContext("/v1/vectors",
                new VectorHandler(docEmbedderRouter, pgVectorRepository, reranker));
        vectorCtx.getFilters().addAll(authFilter);
        log.info("event=vector_endpoints_registered has_embed_router={} has_pgvector={} has_reranker={}",
                docEmbedderRouter != null, pgVectorRepository != null, reranker != null);

        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());

        // TTL sweep: crash-safety backstop for sessions that never called session-close.
        // nexus-4qq1m: CROSS-TENANT — loops every tenant that has ever held a
        // service token (revoked included) through the RLS-scoped per-tenant
        // sweep, plus the default tenant. Stays on the nexus_svc role; no
        // BYPASSRLS connection is required because token-bearing tenants are
        // enumerable from service_tokens (read pre-tenant by design) and any
        // tenant that wrote scratch necessarily presented a token.
        this.sweepScheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "t1-ttl-sweep");
            t.setDaemon(true);
            return t;
        });
        this.sweepScheduler.scheduleAtFixedRate(
            () -> {
                try {
                    runScheduledSweep(OffsetDateTime.now(ZoneOffset.UTC));
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

    /**
     * One cycle of the {@code t1-ttl-sweep} loop, extracted from the scheduler
     * lambda so the WIRING is testable and not merely the three methods it calls.
     *
     * <p>nexus-lgiqw: before this extraction nothing exercised the loop at all —
     * every arm's own unit test passed while nothing proved the scheduler invoked
     * it, so removing an arm would have left the whole suite green. The counters
     * are returned rather than only logged for the same reason: the aggregate line
     * is the reaper's tripwire, and a tripwire nothing asserts on is not one.
     *
     * <p>Package-private and time-injected deliberately. The scheduler passes the
     * real clock; tests pass a fixed instant.
     *
     * @param now the cycle's reference instant
     * @return per-arm deletion counts for this cycle
     */
    // EVERY ARM IS BOUNDED BY SweepBounds.STATEMENT_TIMEOUT (nexus-lgiqw). Read its
    // javadoc before changing any of this; the short version:
    //
    // This cycle is single-threaded across every tenant and all three arms, so one
    // blocked statement used to stall the entire cycle, for every other tenant, with
    // no ceiling — and silently, because this is a daemon thread with no exception,
    // no restart, and no alarm on the ABSENCE of the completion line below.
    // The bound is passed to EVERY arm rather than just the newest: bounding one of
    // three leaves the cycle unbounded and buys only an arm that behaves differently
    // from its siblings. The hazard belongs to the loop, so the bound does too.
    //
    // A cancelled statement is safe here because every sweep is idempotent and
    // cumulative — the rows simply wait for the next cycle, and the per-tenant catch
    // blocks below turn PostgreSQL's 57014 into a logged warning without abandoning
    // the remaining tenants or arms.
    SweepCounts runScheduledSweep(OffsetDateTime now) {
        return runScheduledSweep(now, SweepBounds.STATEMENT_TIMEOUT);
    }

    /**
     * As {@link #runScheduledSweep(OffsetDateTime)}, with the per-statement bound
     * injected. Production always passes {@link SweepBounds#STATEMENT_TIMEOUT};
     * tests pass a short one so that "a blocked statement does not stall the cycle"
     * is a fast assertion rather than a thirty-second one.
     */
    SweepCounts runScheduledSweep(OffsetDateTime now, java.time.Duration statementTimeout) {
        OffsetDateTime cutoff = now.minusHours(SWEEP_TTL_HOURS);
        var tenants = new java.util.LinkedHashSet<String>();
        tenants.add(DEFAULT_TENANT);
        // Bounded too, and deliberately: this runs BEFORE any arm, so an unbounded
        // enumeration would stall the cycle where no per-arm bound can reach it.
        tenants.addAll(tokenStore.listKnownTenants(statementTimeout));
        int total = 0;
        int totalSessionTokens = 0;
        int totalDataTokens = 0;
        // nexus-4tosp: a cycle counts as a data-arm failure only when NO tenant
        // succeeded. One bad tenant among healthy ones is the case the per-tenant
        // catch already handles correctly and must not trip the stall counter.
        boolean dataArmSucceededForAnyTenant = false;
        boolean dataArmFailedForAnyTenant = false;
        // nexus-4tosp: per-CYCLE counts for the run-summary heartbeat below —
        // distinct from the cumulative per-tenant streaks in
        // dataSweepFailuresByTenant, which persist across cycles.
        int dataArmFailedTenantsThisCycle = 0;
        int dataArmStalledTenantsThisCycle = 0;
        for (String tenant : tenants) {
            try {
                int deleted = scratchRepo.sweepTenant(tenant, cutoff, statementTimeout);
                total += deleted;
                log.info("event=t1_scheduled_sweep tenant={} deleted={}", tenant, deleted);
            } catch (Exception ex) {
                // One tenant's failure must not starve the rest of the fleet's sweep.
                log.warn("event=t1_scheduled_sweep_tenant_failed tenant={} error={}",
                    tenant, ex.getMessage(), ex);
            }
            // nexus-t23zk: expired session_tokens backstop, riding the SAME
            // per-tenant loop and schedule as the scratch sweep above (one
            // extra query per tenant, no new thread). closeSession alone
            // leaves a permanent row behind whenever a minting process dies
            // without calling it (crashed MCP, killed dispatch, reboot) —
            // inert (auth checks expires_at live) but otherwise never deleted.
            try {
                int sessionDeleted = tokenStore.sweepExpiredSessions(
                    tenant, now.toInstant(), statementTimeout);
                totalSessionTokens += sessionDeleted;
            } catch (Exception ex) {
                log.warn("event=t1_scheduled_session_sweep_tenant_failed tenant={} error={}",
                    tenant, ex.getMessage(), ex);
            }
            // nexus-lgiqw: expired scope=data service_tokens reaper, riding
            // this SAME loop and schedule for the same reason as the session
            // arm above. The JIT mint path writes a short-TTL row per (tenant,
            // TTL window) and nothing ever deleted one — expiry was read-time
            // filtering only, so the table grew monotonically (14,308 rows,
            // 14,307 expired, ~313/day, measured 2026-08-25).
            //
            // WHY THIS LOOP CANNOT MISS A TENANT — state this explicitly,
            // because the property is invisible after the fact and a
            // well-meaning refactor that sourced `tenants` from a tenants
            // table instead would silently break it: `tenants` is
            // {DEFAULT_TENANT} ∪ tokenStore.listKnownTenants(), and
            // listKnownTenants() is SELECT DISTINCT tenant_id FROM
            // service_tokens — the very table this arm sweeps. Any tenant
            // holding a sweepable row is in the list by construction.
            try {
                int dataDeleted = dataTokenSweepOverride != null
                    ? dataTokenSweepOverride.apply(tenant)
                    : tokenStore.sweepExpiredDataTokens(
                        tenant, now.toInstant(), Duration.ofDays(SWEEP_DATA_TOKEN_GRACE_DAYS),
                        statementTimeout);
                totalDataTokens += dataDeleted;
                dataArmSucceededForAnyTenant = true;
                // nexus-4tosp: per-tenant recovery. Only worth an INFO line
                // when the tenant had actually crossed the stall threshold —
                // a reset after 1-2 ordinary failures is not news.
                Integer priorFailures = dataSweepFailuresByTenant.remove(tenant);
                if (priorFailures != null && priorFailures >= DATA_SWEEP_STALL_CYCLES) {
                    log.info("event=t1_data_token_sweep_tenant_recovered tenant={} after_failures={}",
                        tenant, priorFailures);
                }
            } catch (Exception ex) {
                dataArmFailedForAnyTenant = true;
                dataArmFailedTenantsThisCycle++;
                log.warn("event=t1_scheduled_data_token_sweep_tenant_failed tenant={} error={}",
                    tenant, ex.getMessage(), ex);
                // nexus-4tosp: per-tenant stall escalation, distinct from the
                // fleet-wide ERROR below. Fires every cycle at or past the
                // threshold (not just on the crossing) so the alarm keeps
                // signaling until the tenant recovers.
                int streak = dataSweepFailuresByTenant.merge(tenant, 1, Integer::sum);
                // Fires on EVERY cycle at/past the threshold, unlike the fleet-wide
                // t1_scheduled_data_token_sweep_stalled gate below, which fires once on
                // the crossing: this arm runs every 6 h and is read from the log window
                // alone (conexus's DATA EFFECTS row), so a once-only line scrolls out of
                // a 24 h window while the tenant is still stalled. Four lines a day per
                // stalled tenant is the intended noise level (nexus-4tosp).
                if (streak >= DATA_SWEEP_STALL_CYCLES) {
                    dataArmStalledTenantsThisCycle++;
                    log.error("event=t1_data_token_sweep_tenant_stalled tenant={} consecutive_failures={} "
                            + "last_error=\"{}\"",
                        tenant, streak, ex.getMessage());
                }
            }
            // hygiene-001 follow-on (coordinator scope addition): plans whose
            // read-time expiry predicate (PlanRepository.notExpiredCondition,
            // shared with listActivePlans/searchPlans/listPlans) says NOT
            // active are filtered out of every read but were never deleted by
            // any sweep — riding this SAME per-tenant loop and 6h schedule,
            // same reasoning as the session/data-token arms above.
            try {
                int plansDeleted = planRepo.deleteExpiredPlans(tenant, statementTimeout);
                log.info("event=plan_ttl_sweep tenant={} deleted={}", tenant, plansDeleted);
            } catch (Exception ex) {
                log.warn("event=plan_ttl_sweep_tenant_failed tenant={} error={}",
                    tenant, ex.getMessage(), ex);
            }
        }
        // nexus-4tosp: ONE line per scheduled run, unconditional (a zero-
        // failure cycle included), so "scheduler never fired" is
        // distinguishable from "fired and failed" from outside the logs.
        log.info("event=t1_scheduled_data_token_sweep_run tenants={} failed={} stalled={}",
            tenants.size(), dataArmFailedTenantsThisCycle, dataArmStalledTenantsThisCycle);
        // Emitted unconditionally, zero-delete cycles included: a cycle that
        // swept nothing must be distinguishable from a cycle that did not run.
        //
        // total_data_tokens_deleted is a BREADCRUMB, not a tripwire. Steady state
        // is ~80 per cycle, so an order-of-magnitude departure means the cadence
        // slipped or accrual changed shape — but this is a log line and NOTHING
        // ALERTS ON IT. It makes that diagnosis possible for someone already
        // looking; it does not detect anything on its own. The design notes for
        // nexus-lgiqw originally called it a tripwire, which claimed a property
        // no code here provides.
        log.info("event=t1_scheduled_sweep_complete tenants={} total_deleted={} "
                + "total_session_tokens_deleted={} total_data_tokens_deleted={}",
            tenants.size(), total, totalSessionTokens, totalDataTokens);

        // nexus-4tosp. The per-tenant catch above is correct and stays: one
        // tenant's failure must not starve the fleet. What was missing is any
        // consequence for it happening EVERY cycle.
        //
        // THIS IS DIAGNOSIS, NOT DETECTION -- do not mistake it for the alarm.
        // There are three failure modes and this counter sees exactly one:
        //   reaped                      sweep_complete present, counts sane
        //   installed but never reaping failure events every cycle   <- this
        //   scheduler never fired       NOTHING at all               <- BLIND
        // If the sweep thread dies or the JVM wedges there are no failures to
        // count, so this counter reads ZERO -- indistinguishable from healthy,
        // silent in the reassuring direction, which is the same shape as the
        // bug it is fixing. The DETECTOR is the ABSENCE of the unconditional
        // t1_scheduled_sweep_complete heartbeat below, alarmed over a window
        // longer than one period; that covers all three modes. This counter
        // only tells you WHICH one, once that alarm fires.
        // (Design credit: conexus-a4, 2026-08-27, who caught that the counter
        // alone reproduces the defect one level up.)
        if (dataArmFailedForAnyTenant && !dataArmSucceededForAnyTenant) {
            int streak = consecutiveDataSweepFailures.incrementAndGet();
            // ERROR only on the crossing, not every cycle after: a line repeated
            // forever is noise, and noise is how a real signal gets blessed away.
            if (streak == DATA_SWEEP_STALL_CYCLES) {
                log.error("event=t1_scheduled_data_token_sweep_stalled consecutive_cycles={} "
                        + "tenants={} — the data-token arm has reaped NOTHING for {} "
                        + "consecutive cycles; expired tokens are accruing unreaped",
                    streak, tenants.size(), streak);
            }
        } else if (dataArmSucceededForAnyTenant) {
            consecutiveDataSweepFailures.set(0);
        }
        return new SweepCounts(tenants.size(), total, totalSessionTokens, totalDataTokens);
    }

    /** Per-arm deletion counts from one {@link #runScheduledSweep} cycle. */
    record SweepCounts(int tenants, int scratch, int sessionTokens, int dataTokens) { }

    /**
     * nexus-4tosp: consecutive cycles whose data-token arm failed for every
     * tenant. Zero whenever any tenant last succeeded.
     *
     * <p>Package-private ON PURPOSE. It exists so the threshold behaviour is
     * assertable by value rather than by the absence of a stall, which is what
     * let the original defect ship. It is NOT a production surface: nothing in
     * the deployment reads the engine's /health body (the ALB probes the edge's
     * own /healthz -- fan-out was deliberately rejected, conexus-dn4; the
     * redeploy poll hits /version and discards the body), so publishing this
     * there would have been an inert guard wearing the look of a live one.
     */
    int consecutiveDataSweepFailures() {
        return consecutiveDataSweepFailures.get();
    }

    /**
     * nexus-4tosp: true once the data-token arm has failed outright for
     * {@link #DATA_SWEEP_STALL_CYCLES} consecutive cycles. Clears on the first
     * cycle that reaps for any tenant — a latched flag would be a wolf-crier.
     * Package-private for the same reason as above: assertable, not published.
     */
    boolean dataSweepStalled() {
        return consecutiveDataSweepFailures.get() >= DATA_SWEEP_STALL_CYCLES;
    }

    /**
     * nexus-4tosp: per-tenant consecutive data-token sweep failures for the
     * given tenant. Zero once that tenant has succeeded (or never failed).
     * Package-private for the same reason as {@link #consecutiveDataSweepFailures()}:
     * assertable by value, not a production surface.
     */
    int dataSweepFailuresForTenant(String tenant) {
        return dataSweepFailuresByTenant.getOrDefault(tenant, 0);
    }


    /** Stop the HTTP server, TTL sweep scheduler, and purge-trash VACUUM
     *  executor immediately. */
    public void stop() {
        sweepScheduler.shutdownNow();
        catalogRepo.close();
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
