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
import java.util.ArrayList;
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
 * round-trips a vector. This is the managed-cloud analogue of the local
 * migrate-to-service path (RDR-176 §Decision "cloud→cloud server-side").
 *
 * <p><b>Ephemeral credentials (Pillar 1b).</b> The source ChromaCloud
 * {@code tenant}/{@code database}/{@code api_key} are client-supplied in the
 * request body and held in method-local variables for the request lifetime
 * ONLY: never written to any persistent store, never logged (no request-body
 * logging on this route; error logs carry the exception, not the body). The
 * response echoes per-collection counts, never the credentials.
 *
 * <p><b>Egress.</b> The default {@link CloudSource} routes ChromaCloud reads
 * through {@link ChromaRestClient#cloud} which now wires {@code EgressProxy}
 * (api.trychroma.com is external and must traverse squid from the private
 * subnet). A {@link CloudSourceFactory} seam lets tests supply a fake source
 * with no network.
 */
public final class MigrationHandler implements HttpHandler {

    static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Logger log = LoggerFactory.getLogger(MigrationHandler.class);

    /** Chroma read/upsert page size (Chroma quota MAX_RECORDS_PER_WRITE). */
    static final int PAGE = 300;

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
        try {
            if (path.endsWith("/ingest-cloud") && "POST".equals(method)) {
                handleIngestCloud(exchange, tenant);
            } else if (path.endsWith("/ingest-cloud")) {
                HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            } else {
                HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException bad) {
            HttpUtil.send(exchange, 400, "{\"error\":" + HttpUtil.jsonString(bad.getMessage()) + "}");
        } catch (Exception e) {  // noqa — must not echo the request body (credentials)
            // Log the exception, NOT the body: a stack trace from the Chroma client
            // or pgvector upsert carries no credential (the api key is only ever a
            // header/local var). Generic client message.
            log.warn("event=migration_ingest_cloud_failed tenant={} error={}", tenant, e.toString());
            HttpUtil.send(exchange, 500, "{\"error\":\"ingest-cloud failed\"}");
        }
    }

    @SuppressWarnings("unchecked")
    private void handleIngestCloud(HttpExchange exchange, String tenant) throws IOException {
        Map<String, Object> body = MAPPER.readValue(exchange.getRequestBody(), Map.class);

        // Client-supplied EPHEMERAL credentials — method-local only. Never logged,
        // never persisted; they leave this method solely as the arguments to
        // sourceFactory.open(...) and die with the request.
        String srcTenant = requireString(body, "source_tenant");
        String srcDatabase = requireString(body, "source_database");
        String srcApiKey = requireString(body, "source_api_key");
        List<String> requested = (List<String>) body.get("collections");

        Map<String, Integer> copied = new LinkedHashMap<>();
        try (CloudSource src = sourceFactory.open(srcTenant, srcDatabase, srcApiKey)) {
            List<String> collections = (requested != null && !requested.isEmpty())
                    ? requested : src.collections();
            for (String coll : collections) {
                int total = 0;
                for (int offset = 0; ; offset += PAGE) {
                    ChunkPage page = src.read(coll, PAGE, offset);
                    int n = page.ids().size();
                    if (n == 0) break;
                    // Server-side upsert of the PRE-COMPUTED vectors — no re-embed,
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

        int grand = copied.values().stream().mapToInt(Integer::intValue).sum();
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
                Map.of("copied", copied, "total", grand)));
    }

    private static String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException(key + " (non-blank string) is required");
        }
        return s;
    }

    /** Default {@link CloudSource}: egress-routed ChromaCloud via {@link ChromaRestClient}. */
    static final class ChromaCloudSource implements CloudSource {
        private final ChromaRestClient chroma;

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
            Map<String, Object> resp = chroma.get(collection, null, limit, offset,
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
