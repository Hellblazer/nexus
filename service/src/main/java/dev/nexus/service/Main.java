package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Bge768Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
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
 *   <li>{@code NX_DB_USER} — database user (application / DML role)</li>
 *   <li>{@code NX_DB_PASS} — database password</li>
 *   <li>{@code NX_POOL_SIZE} — HikariCP pool size (default 10)</li>
 *   <li>{@code NX_DB_ADMIN_URL} — optional JDBC URL for schema migration (defaults to
 *       {@code NX_DB_URL}). Useful when the application role is {@code nexus_svc}
 *       (NOSUPERUSER NOBYPASSRLS) and a separate schema-owner role runs DDL.</li>
 *   <li>{@code NX_DB_ADMIN_USER} — optional migration user (defaults to {@code NX_DB_USER})</li>
 *   <li>{@code NX_DB_ADMIN_PASS} — optional migration password (defaults to {@code NX_DB_PASS})</li>
 * </ul>
 *
 * <p>Vector backend (RDR-155 P4a.2, bead nexus-1k8s1 — pgvector serves every vector
 * route; the Chroma serving backend and its {@code NX_CHROMA_*} wiring are retired):
 * <ul>
 *   <li>{@code NX_VOYAGE_API_KEY} — optional Voyage AI API key. Present: embedders
 *       route by collection prefix to Voyage (CCE for {@code knowledge__}/{@code docs__}/
 *       {@code rdr__}, voyage-code-3 for {@code code__}), ONNX fallback for
 *       unrecognised prefixes. Absent: ONNX-only (local mode).</li>
 * </ul>
 *
 * <p>Binds to {@code 127.0.0.1} only (loopback). No external TLS — forward proxy
 * or supervisor is responsible for TLS termination in production.
 */
public final class Main {

    private static final Logger log = LoggerFactory.getLogger(Main.class);

    public static void main(String[] args) throws Exception {
        int port   = intEnv("NX_SERVICE_PORT", 8080);
        // RDR-152 bead nexus-gmiaf.32.5: NX_SERVICE_TOKEN is the persistent random
        // root bearer token (minted + persisted by `nx init --service`). Auth resolves
        // bearer→tenant against the service_tokens registry; this token is seeded BOUND
        // to the default tenant (the wildcard bootstrap is retired — no token crosses
        // tenants anymore). Optional: a deployment whose tokens already live in the DB
        // can start without it.
        String token  = System.getenv("NX_SERVICE_TOKEN");
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
        // search_path: set via connectionInitSql (not ALTER ROLE, which requires superuser).
        // Covers nexus (T2 tables), t1 (T1 scratch), and public for pg_catalog visibility.
        hikari.setConnectionInitSql("SET search_path TO nexus, t1, public");
        var ds = new HikariDataSource(hikari);

        // ── Schema migration (RDR-152 bead nexus-net63) ───────────────────────
        // Run Liquibase BEFORE the HTTP server binds so the service never serves
        // requests against an unmigrated database.  Fail fast on any error so
        // the process exits non-zero and the supervisor knows not to route traffic.
        //
        // When NX_DB_ADMIN_* are set, use a dedicated single-connection migration
        // pool whose credentials have DDL rights (schema-owner or superuser).
        // Falls back to NX_DB_* when NX_DB_ADMIN_* are absent, covering dev/test
        // setups where the application role also owns the schema.
        var migrationDs = buildMigrationDataSource(dbUrl, dbUser, dbPass);
        try {
            SchemaMigrator.migrate(migrationDs);
        } catch (SchemaMigrator.MigrationException e) {
            // Close the migration pool BEFORE System.exit: exit does not run finally blocks,
            // so explicit close here avoids leaking the pool on the error path.
            migrationDs.close();
            log.error("event=schema_migration_failed error=\"{}\"", e.getMessage(), e);
            System.exit(1);
        }
        migrationDs.close();

        // Root-token provisioning (RDR-152 bead nexus-gmiaf.32.5): seed NX_SERVICE_TOKEN
        // as a BOUND default-tenant row. The transitional wildcard (tenant_id="*") is
        // retired — every token, including the root, is strictly tenant-bound and the
        // client X-Nexus-Tenant header never crosses tenants. Runs AFTER migration (table
        // exists) as the app role (nexus_svc has INSERT via grants-nexus-svc). Idempotent
        // (ON CONFLICT DO NOTHING). No-op when NX_SERVICE_TOKEN is unset.
        new dev.nexus.service.db.TokenStore(ds, java.time.Clock.systemUTC())
            .ensureBootstrapToken(token, dev.nexus.service.db.TenantConstants.DEFAULT_TENANT);

        // Vector backend (RDR-155 P4a.2, bead nexus-1k8s1): pgvector serves every
        // vector route against the SAME Postgres the service already requires — no
        // separate vector store process. Embedders route by collection prefix
        // (Voyage when NX_VOYAGE_API_KEY is present, ONNX otherwise/fallback).
        // PRODUCTION WIRING INVARIANT (P2.2 contract, recorded on nexus-1k8s1):
        // PgVectorRepository takes the ROUTER constructor — EmbedderRouter through the
        // plain-Embedder constructor would fall back to ONNX for ALL collections and
        // break cloud-mode routing (caught only at the first upsert's dim check).
        String voyageKey = System.getenv("NX_VOYAGE_API_KEY");
        EmbedderRouter docEmbedRouter;
        EmbedderRouter qryEmbedRouter;
        // nexus-pebfx.2: LOUD one-line embedding-mode banner. The 2026-06-10
        // migration ran for hours against silent ONNX-384 fallback because the
        // mode was invisible; onnx-local now logs at WARN and names the refusal
        // behaviour so a missing key is unmissable in the service log.
        dev.nexus.service.vectors.Reranker reranker = null;
        if (voyageKey != null && !voyageKey.isBlank()) {
            // Cloud mode: PURE Voyage routing — NO local ONNX embedder (nexus-0n7uc).
            // The cloud container has no MiniLM model on disk; constructing
            // OnnxEmbedder would call onnxruntime createSession() on a missing file,
            // which SEGFAULTS (not throws) and crashed the engine at boot (conexus
            // STEP-5, conexus-qcn). A voyage-1024 cloud corpus has no use for a local
            // 384-dim fallback; non-conformant collections are REFUSED (422).
            docEmbedRouter = new EmbedderRouter(voyageKey, "document");
            qryEmbedRouter = new EmbedderRouter(voyageKey, "query");
            // RDR-188: the same key grants the fused rerank stage — the engine owns
            // ALL Voyage traffic (embed + rerank); no new credential surface.
            reranker = new dev.nexus.service.vectors.VoyageReranker(
                    voyageKey, dev.nexus.service.vectors.VoyageReranker.DEFAULT_MODEL);
            log.info("event=embedding_mode_banner mode={} models={} reranker={} backend=pgvector",
                    docEmbedRouter.modeName(), docEmbedRouter.availableModels(),
                    dev.nexus.service.vectors.VoyageReranker.DEFAULT_MODEL);
        } else {
            // Local mode (RDR-160): bge-768 serves EVERY collection. MiniLM is
            // NOT loaded on the local path (Decision 5) — a non-bge collection
            // is REFUSED (422), never silently embedded at the wrong dim.
            Bge768Embedder bge = new Bge768Embedder();
            docEmbedRouter = new EmbedderRouter(bge, "document");
            qryEmbedRouter = new EmbedderRouter(bge, "query");
            log.warn("event=embedding_mode_banner mode={} models={} backend=pgvector "
                    + "voyage_collections=REFUSED_422 hint=\"set NX_VOYAGE_API_KEY (or let "
                    + "the supervisor plumb it from VOYAGE_API_KEY / config.yml credentials) "
                    + "to serve voyage-* collections\"",
                    docEmbedRouter.modeName(), docEmbedRouter.availableModels());
        }
        var pgVectorRepo = new PgVectorRepository(new TenantScope(ds), docEmbedRouter,
                                                  qryEmbedRouter);

        // Pooler-mode backstop (nexus-bhzuv): if a PgBouncer is interposed
        // (NX_PGBOUNCER_ADMIN_URL set), refuse to bind unless it reports
        // pool_mode=transaction. SET LOCAL tenant GUCs leak across borrows under
        // session-mode pooling → cross-tenant read. No-op on the direct-PG path.
        // Runs BEFORE service.start(), mirroring the schema-migration fail-fast ordering.
        try {
            dev.nexus.service.db.PoolerModeCheck.verifyAtStartup();
        } catch (dev.nexus.service.db.PoolerModeCheck.PoolerModeException e) {
            ds.close();
            log.error("event=pooler_mode_check_failed error=\"{}\"", e.getMessage(), e);
            System.exit(1);
        }

        var service = new NexusService(port, token, ds, docEmbedRouter, pgVectorRepo, reranker);
        service.start();

        log.info("event=service_ready port={}", service.getPort());

        // Parent-death watchdog (nexus-03bcg): exit if the supervisor (our parent
        // process) dies, so an OOM-killed supervisor leaves no orphaned-but-serving
        // JVM (the orphan would keep /health green while its lease ages out, and a
        // heal-on-next-use re-spawn would then double-spawn a 2nd JVM). Opt-in via
        // NX_SERVICE_PARENT_DEATH_EXIT=1 (set by the supervisor when it spawns us)
        // so standalone/test runs with no supervisor are unaffected. This is the
        // portable (Linux + macOS) complement to the Linux-only PR_SET_PDEATHSIG
        // the supervisor also arms on the child.
        if ("1".equals(System.getenv("NX_SERVICE_PARENT_DEATH_EXIT"))) {
            ProcessHandle.current().parent().ifPresentOrElse(parent -> {
                Thread watchdog = new Thread(() -> {
                    try {
                        parent.onExit().get();   // completes when the supervisor exits
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        return;
                    } catch (Exception e) {       // onExit unsupported for this handle (RuntimeException)
                        log.warn("event=parent_death_watchdog_error error=\"{}\"", e.toString());
                        return;
                    }
                    log.warn("event=supervisor_exited action=self_exit "
                            + "reason=orphan_prevention parent_pid={}", parent.pid());
                    // halt, not exit: orphan-prevention is a hard kill, not a graceful
                    // shutdown. System.exit would run the shutdown hook → HikariCP
                    // ds.close() can stall ~30s on a dead PG (the OOM path took the
                    // supervisor AND likely PG). On Linux PR_SET_PDEATHSIG SIGKILLs us
                    // first anyway; halt makes the macOS path equally prompt (CRE M-1).
                    Runtime.getRuntime().halt(143);
                }, "parent-death-watchdog");
                watchdog.setDaemon(true);
                watchdog.start();
                log.info("event=parent_death_watchdog_armed parent_pid={}", parent.pid());
            }, () -> log.warn("event=parent_death_watchdog_skipped reason=no_parent_handle"));
        }

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            log.info("event=shutdown_signal");
            service.stop();
            // Close the embedder's native ONNX session + tokenizer once. doc and
            // qry routers share the SAME embedder instance, so closing one is
            // sufficient (a second close is harmless — close() swallows it).
            docEmbedRouter.close();
            ds.close();
        }));

        // Block main thread until shutdown
        Thread.currentThread().join();
    }

    // ── Migration datasource factory ─────────────────────────────────────────

    /**
     * Builds a single-connection HikariCP pool for schema migration.
     *
     * <p>Uses {@code NX_DB_ADMIN_*} when present, falling back to the supplied
     * {@code defaultUrl/defaultUser/defaultPass} (the regular application
     * credentials) for dev/test setups where one role owns both DDL and DML.
     *
     * <p><strong>Partial-config guard</strong>: if any one of {@code NX_DB_ADMIN_URL},
     * {@code NX_DB_ADMIN_USER}, or {@code NX_DB_ADMIN_PASS} is set, all three must be
     * set. A partial configuration (e.g. ADMIN_USER set but ADMIN_PASS absent) would
     * silently mix admin and app credentials, producing a cryptic auth error at connect
     * time instead of a clear startup failure.
     *
     * <p>Pool size 1: Liquibase uses a single connection sequentially.
     */
    private static HikariDataSource buildMigrationDataSource(String defaultUrl,
                                                              String defaultUser,
                                                              String defaultPass) {
        String adminUrl  = System.getenv("NX_DB_ADMIN_URL");
        String adminUser = System.getenv("NX_DB_ADMIN_USER");
        String adminPass = System.getenv("NX_DB_ADMIN_PASS");

        // Partial-config guard: require all-or-nothing.
        long adminSet = (adminUrl != null ? 1 : 0)
                      + (adminUser != null ? 1 : 0)
                      + (adminPass != null ? 1 : 0);
        if (adminSet > 0 && adminSet < 3) {
            throw new IllegalStateException(
                "Partial NX_DB_ADMIN_* configuration detected (" + adminSet + "/3 vars set). " +
                "Set all of NX_DB_ADMIN_URL, NX_DB_ADMIN_USER, NX_DB_ADMIN_PASS, " +
                "or none (to fall back to NX_DB_* credentials).");
        }

        String url  = adminSet == 3 ? adminUrl  : defaultUrl;
        String user = adminSet == 3 ? adminUser : defaultUser;
        String pass = adminSet == 3 ? adminPass : defaultPass;

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(url);
        cfg.setUsername(user);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(1);
        cfg.setMinimumIdle(1);
        cfg.setConnectionTimeout(30_000);
        cfg.setPoolName("nexus-migration");
        return new HikariDataSource(cfg);
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
