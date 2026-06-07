package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.vectors.ChromaRestClient;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.LocalChromaServer;
import dev.nexus.service.vectors.OnnxEmbedder;
import dev.nexus.service.vectors.VectorRepository;
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
 * <p>Vector backend (Seam B, bead nexus-gmiaf.20):
 * <ul>
 *   <li>{@code NX_CHROMA_MODE} — {@code local} (default) or {@code cloud}</li>
 *   <li>{@code NX_CHROMA_PATH} — data directory for local mode
 *       (default {@code ~/.config/nexus/chroma})</li>
 *   <li>{@code NX_CHROMA_HTTP_PORT} — port for local chroma run server (default: ephemeral)</li>
 *   <li>{@code NX_CHROMA_BINARY} — path to {@code chroma} CLI (auto-detected if unset)</li>
 *   <li>{@code NX_VOYAGE_API_KEY} — cloud mode: Voyage AI API key</li>
 *   <li>{@code NX_VOYAGE_MODEL_DOC} — cloud mode: Voyage model for docs (default voyage-context-3)</li>
 *   <li>{@code NX_VOYAGE_MODEL_QUERY} — cloud mode: Voyage model for queries (default voyage-context-3)</li>
 *   <li>{@code NX_CHROMA_CLOUD_TENANT} — cloud mode: Chroma Cloud tenant</li>
 *   <li>{@code NX_CHROMA_CLOUD_DATABASE} — cloud mode: Chroma Cloud database</li>
 *   <li>{@code NX_CHROMA_CLOUD_API_KEY} — cloud mode: Chroma Cloud API key</li>
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

        // Vector backend setup (Seam B — optional; only when configured)
        LocalChromaServer localChroma    = null;
        VectorRepository  vectorRepo     = null;
        EmbedderRouter    docEmbedRouter = null;

        String chromaMode = System.getenv().getOrDefault("NX_CHROMA_MODE", "local");
        if ("local".equalsIgnoreCase(chromaMode)) {
            localChroma = buildLocalChroma();
            localChroma.start();
            ChromaRestClient chromaClient = ChromaRestClient.local("127.0.0.1", localChroma.getPort());
            OnnxEmbedder onnx = new OnnxEmbedder();
            // EmbedderRouter in local mode — all prefixes route to ONNX (S0.2 proof)
            docEmbedRouter = new EmbedderRouter(onnx, "document");
            EmbedderRouter qryEmbedRouter = new EmbedderRouter(onnx, "query");
            vectorRepo = new VectorRepository(docEmbedRouter, qryEmbedRouter, chromaClient);
            log.info("event=vector_backend_local port={}", localChroma.getPort());
        } else if ("cloud".equalsIgnoreCase(chromaMode)) {
            // Cloud mode: build collection-aware routers using VOYAGE_API_KEY
            String voyageKey = requireEnv("NX_VOYAGE_API_KEY");
            OnnxEmbedder onnx = new OnnxEmbedder();  // ONNX fallback for unrecognised prefixes
            docEmbedRouter = new EmbedderRouter(onnx, voyageKey, "document");
            EmbedderRouter qryEmbedRouter = new EmbedderRouter(onnx, voyageKey, "query");
            vectorRepo = buildCloudVectorRepo(docEmbedRouter, qryEmbedRouter);
            log.info("event=vector_backend_cloud");
        } else if (System.getenv("NX_VOYAGE_API_KEY") != null) {
            // Parity-gate mode: no Chroma backend, but enable the /embed endpoint
            // so test_embed_parity.py can call it without a full storage stack.
            OnnxEmbedder onnx = new OnnxEmbedder();
            docEmbedRouter = new EmbedderRouter(onnx, System.getenv("NX_VOYAGE_API_KEY"), "document");
            log.info("event=embed_only_mode NX_CHROMA_MODE={}", chromaMode);
        } else {
            log.info("event=vector_backend_disabled NX_CHROMA_MODE={}", chromaMode);
        }

        final LocalChromaServer finalLocalChroma    = localChroma;
        final VectorRepository  finalVectorRepo      = vectorRepo;
        final EmbedderRouter    finalDocEmbedRouter  = docEmbedRouter;

        var service = new NexusService(port, token, ds, finalVectorRepo, finalDocEmbedRouter);
        service.start();

        log.info("event=service_ready port={}", service.getPort());

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            log.info("event=shutdown_signal");
            service.stop();
            ds.close();
            if (finalLocalChroma != null) {
                log.info("event=stopping_local_chroma");
                finalLocalChroma.stop();
            }
        }));

        // Block main thread until shutdown
        Thread.currentThread().join();
    }

    // ── Vector backend factories ──────────────────────────────────────────────

    private static LocalChromaServer buildLocalChroma() throws Exception {
        String dataPath = System.getenv().getOrDefault(
                "NX_CHROMA_PATH",
                System.getProperty("user.home") + "/.config/nexus/chroma");

        int chromaPort;
        String portStr = System.getenv("NX_CHROMA_HTTP_PORT");
        if (portStr != null && !portStr.isBlank()) {
            chromaPort = Integer.parseInt(portStr.trim());
        } else {
            chromaPort = LocalChromaServer.findFreePort();
        }

        String chromaBinary = LocalChromaServer.findChromaBinary();
        return new LocalChromaServer(chromaBinary, dataPath, chromaPort);
    }

    /**
     * Cloud vector backend using collection-aware EmbedderRouters (nexus-gmiaf.21).
     *
     * <p>Routes by collection prefix:
     * <ul>
     *   <li>{@code knowledge__}, {@code docs__}, {@code rdr__} → CCE (voyage-context-3)</li>
     *   <li>{@code code__} → standard Voyage (voyage-code-3)</li>
     *   <li>unrecognised → ONNX fallback</li>
     * </ul>
     */
    private static VectorRepository buildCloudVectorRepo(EmbedderRouter docRouter,
                                                          EmbedderRouter qryRouter) {
        String tenant   = requireEnv("NX_CHROMA_CLOUD_TENANT");
        String database = requireEnv("NX_CHROMA_CLOUD_DATABASE");
        String apiKey   = requireEnv("NX_CHROMA_CLOUD_API_KEY");

        ChromaRestClient chromaClient = ChromaRestClient.cloud(tenant, database, apiKey);
        return new VectorRepository(docRouter, qryRouter, chromaClient);
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
