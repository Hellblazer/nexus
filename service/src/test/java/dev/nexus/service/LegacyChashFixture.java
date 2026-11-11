// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.FRECENCY;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.ChunkReaper.Settings;
import dev.nexus.service.db.ChashConstraintValidator;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.vectors.ReaperRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.tables.records.ChunksRecord;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;

/**
 * Shared fixtures of the legacy-chunk report tests ({@link LegacyChunkReportsRouteTest}): nexus-f3yqd, engine 0.1.190, what the
 * chash VALIDATE job's refusals and the doctor's opt-in detail say about non-32-byte chunk and manifest rows, through the real
 * {@link NexusService} and its auth filter on a real PostgreSQL (the manifest's foreign key to its chunk is kept: a legacy
 * manifest row needs a chunk beside it, which a real store cannot be left without once the migrations have run). Every test starts from the state the migrations leave (both
 * width CHECKs NOT VALID) and plants what it needs; each names the mutation it kills.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class LegacyChashFixture {

    static final ObjectMapper MAPPER = new ObjectMapper();
    static final String ROOT = "tok-root-legacy-0123456789abcdefghijk";
    static final String TOKEN_A = "tok-a-legacy-0123456789abcdefghijklm";
    static final String TOKEN_B = "tok-b-legacy-0123456789abcdefghijklm";
    static final String TENANT_A = "t-legacy-a";
    static final String TENANT_B = "t-legacy-b";
    static final String COLLECTION = "knowledge__legacy__bge-base-en-v15-768__v1";
    static final String COLLECTION_RA = "knowledge__ra__bge-base-en-v15-768__v1";
    static final String COLLECTION_RB = "knowledge__rb__bge-base-en-v15-768__v1";
    static final String MODEL = "bge-base-en-v15-768";
    static final String CHUNKS_CHECK = "chunks_chash_octet_check";
    static final String MANIFEST_CHECK = "catalog_document_chunks_chash_octet_check";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;
    final HttpClient http = TestHttp.client();

    @FunctionalInterface
    interface Seed {
        void run(DSLContext ctx) throws Exception;
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, ROOT, TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL,
                TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, TOKEN_A, TENANT_A, "tenant-a");
            PgContainerHelper.seedServiceToken(dsl, TOKEN_B, TENANT_B, "tenant-b");
            PgContainerHelper.ensureTenantPartitions(dsl, TENANT_A);
            PgContainerHelper.ensureTenantPartitions(dsl, TENANT_B);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(768);
        var repo = new PgVectorRepository(new TenantScope(ds), embedder, embedder);
        service = new NexusService(0, ROOT, ds, null, repo);
        service.chashConstraintValidator(new ChashConstraintValidator(
            new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()), new TenantScope(ds),
            () -> List.of(TENANT_A, TENANT_B)));
        service.start();
    }

    @AfterAll
    void stopAll() {
        if (superuserDs != null) superuserDs.close();
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void fresh() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraintIfExists(CHUNKS_CHECK).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(MANIFEST_CHECK).execute();
            ctx.deleteFrom(TOPIC_ASSIGNMENTS).execute();
            ctx.deleteFrom(TOPICS).execute();
            ctx.deleteFrom(FRECENCY).execute();
            ctx.deleteFrom(RELEVANCE_LOG).execute();
            ctx.deleteFrom(CHUNK_ORPHANED_AT).execute();
            ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
            ctx.deleteFrom(CHUNKS).execute();
            ctx.deleteFrom(CATALOG_DOCUMENTS).execute();
            PgContainerHelper.readdChashOctetChecksNotValid(su);
            for (String t : new String[]{TENANT_A, TENANT_B}) {
                for (String c : new String[]{COLLECTION, COLLECTION_RA, COLLECTION_RB}) PgContainerHelper.insertCollection(ctx, t, c);
            }
        }
    }

    // ── fixtures ────────────────────────────────────────────────────────────

    static byte[] sha256(String text) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    /** The legacy (RDR-108) chash of a text: the first 16 bytes of its sha256. */
    static byte[] legacy(String text) throws Exception {
        return Arrays.copyOf(sha256(text), 16);
    }

    static String hex(byte[] b) {
        return HexFormat.of().formatHex(b);
    }

    static Vector unit(float x) {
        float[] v = new float[768];
        v[0] = x;
        v[1] = (float) Math.sqrt(1 - x * x);
        return Vector.of(v);
    }

    /** Runs {@code body} on a superuser session with the width CHECKs dropped, and puts them back NOT VALID. */
    void seed(Seed body) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraintIfExists(CHUNKS_CHECK).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(MANIFEST_CHECK).execute();
            try {
                body.run(ctx);
            } finally {
                PgContainerHelper.readdChashOctetChecksNotValid(su);
            }
        }
    }

    static void document(DSLContext ctx, String tenant, String docId, String collection, String state) {
        ctx.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, docId)
            .set(CATALOG_DOCUMENTS.TITLE, "doc " + docId).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.INDEX_STATE, state).onConflictDoNothing().execute();
    }

    static void chunk(DSLContext ctx, String tenant, String collection, byte[] chash, String text, Vector embedding,
                              JSONB metadata, OffsetDateTime writtenAt) {
        var ins = ctx.insertInto(CHUNKS).set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, collection).set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, MODEL).set(CHUNKS.CHUNK_TEXT, text).set(CHUNKS.EMBEDDING_768, embedding)
            .set(CHUNKS.LAST_WRITTEN_AT, writtenAt);
        if (metadata != null) ins.set(CHUNKS.METADATA, metadata);
        ins.execute();
    }

    static void manifest(DSLContext ctx, String tenant, String collection, String docId, int position, byte[] chash) {
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, docId)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position).set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL).execute();
    }

    ChunksRecord chunkRow(String tenant, String collection, byte[] chash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).selectFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
                .and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.eq(chash)).fetchOne();
        }
    }

    // ── http ────────────────────────────────────────────────────────────────

    HttpResponse<String> postTo(String path, String bearer, String body) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + path).header("Content-Type", "application/json");
        if (bearer != null) b.header("Authorization", "Bearer " + bearer);
        return http.send(b.POST(HttpRequest.BodyPublishers.ofString(body)).build(), HttpResponse.BodyHandlers.ofString());
    }

    private HikariDataSource superuserDs;

    /**
     * A tenant scope over a SUPERUSER connection pool: row-level security does not apply to it, so a statement that leaks across
     * tenants is held back only by its own explicit {@code tenant_id} predicate, which is what the cross-tenant tests mean to test.
     */
    TenantScope superuserScope() {
        if (superuserDs == null) {
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(pg.getUsername());
            cfg.setPassword(pg.getPassword());
            cfg.setMaximumPoolSize(3);
            cfg.setAutoCommit(true);
            superuserDs = new HikariDataSource(cfg);
        }
        return new TenantScope(superuserDs);
    }

    /** Every row of the chunk, manifest and document tables, as strings: two snapshots equal means nothing was written. */
    List<String> snapshot() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<String> out = new ArrayList<>();
            for (var r : ctx.select(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.METADATA, CHUNKS.RETENTION,
                    CHUNKS.LAST_WRITTEN_AT).from(CHUNKS).orderBy(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH).fetch()) {
                out.add("chunk|" + r.value1() + "|" + r.value2() + "|" + hex(r.value3()) + "|" + r.value4() + "|" + r.value5() + "|" + r.value6() + "|" + r.value7());
            }
            for (var r : ctx.select(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                    CATALOG_DOCUMENT_CHUNKS.CHASH).from(CATALOG_DOCUMENT_CHUNKS)
                    .orderBy(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION).fetch()) {
                out.add("manifest|" + r.value1() + "|" + r.value2() + "|" + r.value3() + "|" + hex(r.value4()));
            }
            for (var r : ctx.select(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.METADATA,
                    CATALOG_DOCUMENTS.INDEX_STATE).from(CATALOG_DOCUMENTS).orderBy(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER).fetch()) {
                out.add("doc|" + r.value1() + "|" + r.value2() + "|" + r.value3() + "|" + r.value4() + "|" + r.value5());
            }
            return out;
        }
    }

    JsonNode validateJob() throws Exception {
        var start = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/admin/validate-constraints")
            .header("Authorization", "Bearer " + ROOT).POST(HttpRequest.BodyPublishers.ofString("{}")).build(), HttpResponse.BodyHandlers.ofString());
        assertThat(start.statusCode()).as(start.body()).isEqualTo(202);
        String id = MAPPER.readTree(start.body()).get("job_id").asText();
        long deadline = System.nanoTime() + Duration.ofSeconds(120).toNanos();
        while (System.nanoTime() < deadline) {
            var s = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/admin/validate-constraints/" + id)
                .header("Authorization", "Bearer " + ROOT).GET().build(), HttpResponse.BodyHandlers.ofString());
            JsonNode n = MAPPER.readTree(s.body());
            if (!"running".equals(n.get("state").asText())) return n;
            Thread.sleep(25);
        }
        throw new AssertionError("the validate job did not finish");
    }

    static void note(DSLContext ctx, String tenant, String docId, String collection, String stampedDocId) {
        ctx.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, docId)
            .set(CATALOG_DOCUMENTS.TITLE, "note " + docId).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf("{\"doc_id\":\"" + stampedDocId + "\"}")).execute();
    }

    static void referenceOnlyChunk(DSLContext ctx, String tenant, String collection, byte[] chash) {
        ctx.insertInto(CHUNKS).set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, collection).set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, MODEL).set(CHUNKS.EMBEDDING_768, unit(1f)).set(CHUNKS.RETENTION, "reference-only")
            .set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minusHours(2)).execute();   // no text, by schema
    }

    JsonNode get(String bearer, String path) throws Exception {
        var resp = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + bearer).GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    static JsonNode refusalOf(JsonNode job, String tablePrefix) {
        for (JsonNode r : job.get("refused")) if (r.get("table").asText().startsWith(tablePrefix)) return r;
        throw new AssertionError("no refusal of " + tablePrefix + " in " + job);
    }

    static java.util.Map<String, Long> counts(JsonNode obj) {
        java.util.Map<String, Long> out = new java.util.LinkedHashMap<>();
        obj.fields().forEachRemaining(e -> out.put(e.getKey(), e.getValue().asLong()));
        return out;
    }
}
