// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.vectors.ChunkConstraintAdmin;
import dev.nexus.service.vectors.ChunkMetadataKeys;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Shared fixture of the retire-keys tests (nexus-6pfgb.43): a real {@link NexusService} over the product schema, the
 * routes under test reached through HTTP with real tokens, and typed-jOOQ seeds and reads over a superuser connection
 * (RLS bypassed, so a read sees what the sweep did whatever its stamp). The admin connection the constraint routes run
 * on is the container's superuser, the owner of the product schema.
 */
abstract class ChunkKeysTestBase extends AtomicWriteTestBase {

    static final ObjectMapper MAPPER = new ObjectMapper();
    static final String ROOT = "tok-root-chunk-keys-0123456789abcdef01";
    static final String MODEL = "minilm-l6-v2-384";
    static final List<String> ROW_KEYS =
        List.of("origin_collection", "quarantined_at", "quarantined_by", "reaper_quarantined_at");
    static final String TAXONOMY_PREFIX = "taxonomy" + "_".repeat(2);

    protected NexusService service;
    protected PgVectorRepository vectors;
    private final HttpClient http = TestHttp.client();

    @BeforeAll
    void startService() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), ROOT,
                TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL, TokenStore.SCOPE_ROOT, null, null);
        }
        vectors = new PgVectorRepository(tenantScope, embedder, embedder);
        service = new NexusService(0, ROOT, svcDs, null, vectors, null, null, null);
        service.chunkConstraintAdmin(new ChunkConstraintAdmin(
            new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword())));
        service.start();
    }

    @AfterAll
    void stopService() {
        if (service != null) service.stop();
    }

    // ---- tokens ------------------------------------------------------------------------------------------------

    /** A tenant credential for {@code tenant}; returns the raw token. */
    String tenantToken(String tenant) throws Exception {
        return token(tenant, TokenStore.SCOPE_TENANT);
    }

    String token(String tenant, String scope) throws Exception {
        String raw = "tok-" + scope + "-" + tenant + "-0123456789abcdef012345";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), raw, tenant, scope + "-token",
                scope, null, null);
        }
        return raw;
    }

    // ---- HTTP --------------------------------------------------------------------------------------------------

    HttpResponse<String> post(String path, String bearer, Map<String, Object> body) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Content-Type", "application/json");
        if (bearer != null) b.header("Authorization", "Bearer " + bearer);
        return http.send(b.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    HttpResponse<String> get(String path, String bearer) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + path);
        if (bearer != null) b.header("Authorization", "Bearer " + bearer);
        return http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    HttpResponse<String> sweep(String bearer, Map<String, Object> body) throws Exception {
        return post("/v1/admin/chunk-keys/sweep", bearer, body);
    }

    static JsonNode json(HttpResponse<String> r) throws Exception {
        return MAPPER.readTree(r.body());
    }

    /** The plan fields the client renders from the Python key table. */
    static Map<String, Object> plan() {
        List<String> allowed = new ArrayList<>(ChunkMetadataKeys.CHUNK);
        allowed.addAll(ROW_KEYS);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("allowed_keys", allowed);
        m.put("bypass_prefixes", List.of(TAXONOMY_PREFIX, "quarantine-"));
        m.put("quarantine_implies", List.of("quarantined_by", "quarantined_at"));
        return m;
    }

    static Map<String, Object> plan(Object... extra) {
        Map<String, Object> m = plan();
        for (int i = 0; i < extra.length; i += 2) {
            m.put((String) extra[i], extra[i + 1]);
        }
        return m;
    }

    // ---- seeds -------------------------------------------------------------------------------------------------

    static final OffsetDateTime STAMP = OffsetDateTime.parse("2026-01-01T00:00:00Z");

    String conformant(String slug) {
        return "knowledge" + "_".repeat(2) + slug + "_".repeat(2) + MODEL + "_".repeat(2) + "v1";
    }

    void register(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection, MODEL);
        }
    }

    /**
     * Seed one chunk per seed with that seed's metadata and a fixed old {@code last_written_at}; returns the chashes
     * in seed order. The collection is registered if it is not yet.
     */
    List<String> seed(String tenant, String collection, Map<String, Map<String, Object>> metadataBySeed)
            throws Exception {
        List<String> hex = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        for (var e : metadataBySeed.entrySet()) {
            hex.add(Chash.ofText(tenant + "/" + collection + "/" + e.getKey()).toHex());
            metas.add(e.getValue());
            texts.add(e.getKey() + " text");
            vecs.add(new float[384]);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection, MODEL);
            PgContainerHelper.insertChunks(ctx, tenant, collection, hex, texts, vecs, metas);
            ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, STAMP)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection)).execute();
        }
        return hex;
    }

    /** As {@link #seed}, with the chashes given: the same chash can stand under two tenants, each its own row. */
    void seedHashes(String tenant, String collection, List<String> hex, Map<String, Object> metadata)
            throws Exception {
        List<Map<String, Object>> metas = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        for (String h : hex) {
            Map<String, Object> m = new LinkedHashMap<>(metadata);
            m.put("chunk_text_hash", h);
            metas.add(m);
            texts.add("shared " + h.substring(0, 8));
            vecs.add(new float[384]);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection, MODEL);
            PgContainerHelper.insertChunks(ctx, tenant, collection, hex, texts, vecs, metas);
        }
    }

    String hexOf(String tenant, String collection, String seed) {
        return Chash.ofText(tenant + "/" + collection + "/" + seed).toHex();
    }

    /** The two CHUNK keys a conforming row holds, plus {@code extra}. */
    Map<String, Object> keyed(String tenant, String collection, String seed, Object... extra) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("chunk_text_hash", hexOf(tenant, collection, seed));
        m.put("embedding_model", MODEL);
        for (int i = 0; i < extra.length; i += 2) {
            m.put((String) extra[i], extra[i + 1]);
        }
        return m;
    }

    // ---- reads -------------------------------------------------------------------------------------------------

    @SuppressWarnings("unchecked")
    Map<String, Object> metadataOf(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            JSONB json = DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                    .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())))
                .fetchOne(CHUNKS.METADATA);
            return MAPPER.readValue(json.data(), Map.class);
        }
    }

    OffsetDateTime lastWrittenAt(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.LAST_WRITTEN_AT).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                    .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())))
                .fetchOne(CHUNKS.LAST_WRITTEN_AT);
        }
    }

    /** Every key a chunk row in the tenant holds that the allow-list does not: what a sweep would still have to do. */
    long retiredRowCount(String tenant, String collection) throws Exception {
        long n = 0;
        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))).fetch(CHUNKS.METADATA);
            for (JSONB j : rows) {
                @SuppressWarnings("unchecked")
                Map<String, Object> m = MAPPER.readValue(j.data(), Map.class);
                if (!(plan().get("allowed_keys") instanceof List<?> allowed) || !allowed.containsAll(m.keySet())) {
                    n++;
                }
            }
        }
        return n;
    }
}
