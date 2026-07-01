// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.vectors.ChromaRestClient;
import dev.nexus.service.vectors.PgVectorRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-176 Phase 4 (Gap 1, bead nexus-t9rmg.24) — cloud→cloud server-side ingest.
 *
 * <p>{@code POST /v1/migration/ingest-cloud} drives a ChromaCloud→pgvector copy
 * <em>entirely server-side</em>: the service pulls each chunk page (ids,
 * documents, pre-computed embeddings, metadatas) straight from ChromaCloud and
 * upserts into pgvector. The client only triggers + monitors — it NEVER
 * round-trips a vector.
 *
 * <p><b>Ephemeral credentials (Pillar 1b).</b> The source ChromaCloud
 * {@code tenant}/{@code database}/{@code api_key} are client-supplied in the
 * request body and held in method-local variables for the request lifetime
 * ONLY: never persisted, never logged. Error logs carry the exception TYPE, not
 * its message (a Chroma error message can embed the response body, which is
 * third-party-controlled) and never the request body. The response echoes
 * per-collection counts, never the credentials.
 *
 * <p><b>Correctness signals.</b> The response carries both {@code copied}
 * (chunks read from the source) and {@code dest_counts} (rows actually present
 * in pgvector after the upsert) per collection, so the client-side monitor can
 * verify parity (content-addressing means dest ≤ source-read when the source has
 * duplicate chunk text). A total drop (source had chunks, pgvector has none) is
 * a hard 500. A mid-copy failure returns 500 with the partial {@code copied}
 * progress so the operator can target a retry (upsert is idempotent via ON
 * CONFLICT).
 *
 * <p><b>Egress.</b> The default {@link CloudSource} routes ChromaCloud reads
 * through {@link ChromaRestClient#cloud} which wires {@code EgressProxy}
 * (api.trychroma.com is external, behind squid). A {@link CloudSourceFactory}
 * seam lets tests supply a fake source with no network.
 */
public final class MigrationHandler implements HttpHandler {

    static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Logger log = LoggerFactory.getLogger(MigrationHandler.class);

    /** Chroma read/upsert page size (Chroma quota MAX_RECORDS_PER_WRITE). */
    static final int PAGE = 300;

    /** Trigger-request body cap: the body is a small JSON of creds + names. */
    static final int MAX_BODY_BYTES = 1 << 20;  // 1 MiB

    /** One page of chunks read from a cloud source (server-side, pre-embedded). */
    public record ChunkPage(List<String> ids, List<String> documents,
                            List<float[]> embeddings, List<Map<String, Object>> metadatas) {
        static ChunkPage empty() {
            return new ChunkPage(List.of(), List.of(), List.of(), List.of());
        }
    }

    /** External cloud vector source. Seam so tests need no ChromaCloud/network. */
    public interface CloudSource extends AutoCloseable {
        /** Collection names available in the source (for a full-footprint copy). */
        List<String> collections();

        /** One page of pre-embedded chunks from {@code collection}. */
        ChunkPage read(String collection, int limit, int offset);

        @Override
        default void close() {}
    }

    /** Opens a {@link CloudSource} from client-supplied ephemeral credentials. */
    @FunctionalInterface
    public interface CloudSourceFactory {
        CloudSource open(String tenant, String database, String apiKey);
    }

    /** Thrown for a well-formed-but-invalid request → rendered as 400. */
    private static final class BadRequest extends RuntimeException {
        BadRequest(String message) { super(message); }
    }

    private final PgVectorRepository pgVectors;
    private final CloudSourceFactory sourceFactory;

    /** Production wiring: the default source is egress-routed ChromaCloud. */
    public MigrationHandler(PgVectorRepository pgVectors) {
        this(pgVectors, ChromaCloudSource::open);
    }

    /** Test wiring: inject a fake {@link CloudSourceFactory}. */
    MigrationHandler(PgVectorRepository pgVectors, CloudSourceFactory sourceFactory) {
        this.pgVectors = pgVectors;
        this.sourceFactory = sourceFactory;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        String path = exchange.getRequestURI().getPath();
        String method = exchange.getRequestMethod();
        if (!path.endsWith("/ingest-cloud")) {
            HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            return;
        }
        if (!"POST".equals(method)) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        handleIngestCloud(exchange, tenant);
    }

    private void handleIngestCloud(HttpExchange exchange, String tenant) throws IOException {
        // Progress + parity accumulators live OUT here so a mid-copy failure can
        // still report which collections landed (targeted-retry signal).
        Map<String, Integer> copied = new LinkedHashMap<>();
        Map<String, Integer> destCounts = new LinkedHashMap<>();
        try {
            Map<String, Object> body = MAPPER.readValue(readBodyCapped(exchange), Map.class);

            // Client-supplied EPHEMERAL credentials — method-local only. Never
            // logged, never persisted; they leave this method solely as the
            // arguments to sourceFactory.open(...) and die with the request.
            String srcTenant = requireString(body, "source_tenant");
            String srcDatabase = requireString(body, "source_database");
            String srcApiKey = requireString(body, "source_api_key");
            List<String> requested = stringListOrNull(body.get("collections"));

            try (CloudSource src = sourceFactory.open(srcTenant, srcDatabase, srcApiKey)) {
                List<String> available = src.collections();
                List<String> collections;
                if (requested != null && !requested.isEmpty()) {
                    // Fail loud on a typo'd / absent collection rather than a
                    // silent 200 with copied=0 (a "success" that copied nothing).
                    List<String> unknown = new ArrayList<>();
                    for (String c : requested) {
                        if (!available.contains(c)) unknown.add(c);
                    }
                    if (!unknown.isEmpty()) {
                        throw new BadRequest("collections not present in source: " + unknown);
                    }
                    collections = requested;
                } else {
                    collections = available;  // full-footprint copy
                }

                for (String coll : collections) {
                    int total = 0;
                    for (int offset = 0; ; offset += PAGE) {
                        ChunkPage page = src.read(coll, PAGE, offset);
                        int n = page.ids().size();
                        if (n == 0) break;
                        // Server-side upsert of PRE-COMPUTED vectors — no re-embed,
                        // no client round-trip.
                        pgVectors.upsertChunksWithVectors(tenant, coll,
                                page.ids(), page.documents(), page.embeddings(), page.metadatas());
                        total += n;
                        if (n < PAGE) break;  // short page = last page
                    }
                    copied.put(coll, total);
                    log.info("event=migration_ingest_cloud_collection tenant={} collection={} chunks={}",
                            tenant, coll, total);
                }
            }

            // Count-parity gate (RDR-176 invariant). dest_counts is pgvector's
            // actual row count per collection AFTER the upsert; a total drop
            // (source had chunks, dest has none) is a hard failure. dest ≤ copied
            // is possible under content-addressed dedup, so it is reported (for
            // the client-side monitor) rather than force-failed here.
            List<String> dropped = new ArrayList<>();
            for (Map.Entry<String, Integer> e : copied.entrySet()) {
                int dest = pgVectors.count(tenant, e.getKey());
                destCounts.put(e.getKey(), dest);
                if (e.getValue() > 0 && dest == 0) dropped.add(e.getKey());
            }
            if (!dropped.isEmpty()) {
                log.warn("event=migration_ingest_cloud_parity_drop tenant={} collections={}",
                        tenant, dropped);
                HttpUtil.send(exchange, 500, MAPPER.writeValueAsString(Map.of(
                        "error", "count-parity failure: source chunks did not land",
                        "dropped_collections", dropped,
                        "copied", copied, "dest_counts", destCounts)));
                return;
            }

            int grand = copied.values().stream().mapToInt(Integer::intValue).sum();
            HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of(
                    "copied", copied, "dest_counts", destCounts, "total", grand)));

        } catch (BadRequest bad) {
            HttpUtil.send(exchange, 400, "{\"error\":" + HttpUtil.jsonString(bad.getMessage()) + "}");
        } catch (Exception e) {  // noqa — must not echo the request body / creds
            // Log the exception TYPE only: a Chroma error message can embed the
            // (third-party-controlled) response body, and the request body carries
            // the api key — neither may reach the logs. The partial `copied` map is
            // safe (collection→count) and gives the operator a targeted-retry basis.
            log.warn("event=migration_ingest_cloud_failed tenant={} error_type={}",
                    tenant, e.getClass().getName());
            HttpUtil.send(exchange, 500, MAPPER.writeValueAsString(Map.of(
                    "error", "ingest-cloud failed",
                    "copied_before_failure", copied)));
        }
    }

    /** Read the request body with a hard size cap (H3): never buffer unbounded input. */
    private static byte[] readBodyCapped(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] buf = in.readNBytes(MAX_BODY_BYTES + 1);
            if (buf.length > MAX_BODY_BYTES) {
                throw new BadRequest("request body exceeds " + MAX_BODY_BYTES + " bytes");
            }
            return buf;
        }
    }

    private static String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new BadRequest(key + " (non-blank string) is required");
        }
        return s;
    }

    /** Validate an optional {@code collections} list is null or a list of strings. */
    private static List<String> stringListOrNull(Object v) {
        if (v == null) return null;
        if (!(v instanceof List<?> raw)) {
            throw new BadRequest("collections must be a list of strings");
        }
        List<String> out = new ArrayList<>(raw.size());
        for (Object o : raw) {
            if (!(o instanceof String s)) {
                throw new BadRequest("collections must be a list of strings");
            }
            out.add(s);
        }
        return out;
    }

    /** Default {@link CloudSource}: egress-routed ChromaCloud via {@link ChromaRestClient}. */
    static final class ChromaCloudSource implements CloudSource {
        private final ChromaRestClient chroma;
        // Resolve each collection id ONCE (read-only), reused across pages.
        private final Map<String, String> idCache = new HashMap<>();

        private ChromaCloudSource(String tenant, String database, String apiKey) {
            this.chroma = ChromaRestClient.cloud(tenant, database, apiKey);
        }

        static CloudSource open(String tenant, String database, String apiKey) {
            return new ChromaCloudSource(tenant, database, apiKey);
        }

        @Override
        public List<String> collections() {
            List<String> names = new ArrayList<>();
            for (Map<String, Object> c : chroma.listCollections()) {
                Object name = c.get("name");
                if (name != null) names.add(name.toString());
            }
            return names;
        }

        @Override
        @SuppressWarnings("unchecked")
        public ChunkPage read(String collection, int limit, int offset) {
            // READ-ONLY resolve (getCollection, not getOrCreate) so a migration
            // never creates a collection in the user's SOURCE tenant.
            String colId = idCache.computeIfAbsent(collection, chroma::getCollection);
            Map<String, Object> resp = chroma.getById(colId, null, limit, offset,
                    List.of("documents", "embeddings", "metadatas"), null);
            List<String> ids = (List<String>) resp.getOrDefault("ids", List.of());
            if (ids == null || ids.isEmpty()) return ChunkPage.empty();
            List<String> documents = (List<String>) resp.getOrDefault("documents", List.of());
            List<Map<String, Object>> metadatas =
                    (List<Map<String, Object>>) resp.getOrDefault("metadatas", List.of());
            List<List<Number>> rawEmb = (List<List<Number>>) resp.getOrDefault("embeddings", List.of());
            List<float[]> embeddings = new ArrayList<>(rawEmb.size());
            for (List<Number> vec : rawEmb) {
                float[] f = new float[vec.size()];
                for (int i = 0; i < f.length; i++) f[i] = vec.get(i).floatValue();
                embeddings.add(f);
            }
            return new ChunkPage(ids, documents, embeddings, metadatas);
        }
    }
}
