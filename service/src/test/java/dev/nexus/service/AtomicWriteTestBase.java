// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * Shared fixture for the RDR-223 atomic chunk-plus-owner write tests (beads
 * nexus-z0o2p.2 through .7): a hermetic Testcontainers PG with the product schema, a
 * service role, a {@link CountingFakeEmbedder}-backed {@link CombinedWriteService}, and
 * the row/chunk/manifest helpers every one of them needs. Each subclass boots its own
 * container ({@code PER_CLASS}), so tenant and role names are constants here.
 *
 * <p>Every read of {@code nexus.chunks} goes through typed jOOQ over a superuser
 * connection (RLS bypassed): {@code RawSqlGateTest}'s test-tree ratchet is reduce-only.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
public abstract class AtomicWriteTestBase {

    protected static final String SVC_ROLE = "svc_atomic_write_test";
    protected static final String SVC_PASS = "svc_atomic_write_test_pass";
    protected static final String TENANT   = "atomic-write-tenant";

    protected PostgreSQLContainer<?> pg;
    protected HikariDataSource svcDs;
    protected TenantScope tenantScope;
    protected CatalogRepository repo;
    protected CombinedWriteService svc;
    protected CountingFakeEmbedder embedder;
    protected final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    protected void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(8);
        cfg.setConnectionTimeout(10_000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        embedder = new CountingFakeEmbedder();
        svc = new CombinedWriteService(tenantScope, repo, new EmbedderRouter(embedder, "document"));
    }

    @AfterAll
    protected void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ────────────────────────────────────────────────────────────────

    /** A fresh registered collection plus one registered document. */
    protected record Fx(String collection, String docId) {}

    protected Fx fixture(String tag) {
        int n = seq.incrementAndGet();
        String collection = "code__aw" + tag + n + "__minilm-l6-v2-384__v1";
        String docId = "aw." + tag + "." + n;
        registerCollection(collection);
        registerDoc(docId, collection);
        return new Fx(collection, docId);
    }

    protected void registerCollection(String collection) {
        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, collection);
            return null;
        });
    }

    protected void registerDoc(String tumbler, String collection) {
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "atomic-write-" + tumbler,
            "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0));
    }

    protected String freshDoc(String tag, String collection) {
        String docId = "aw." + tag + "." + seq.incrementAndGet();
        registerDoc(docId, collection);
        return docId;
    }

    // ── wire-shaped builders ────────────────────────────────────────────────────

    protected static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    protected static Map<String, Object> chunk(String chash, String text) {
        return Map.of("chash", chash, "text", text, "metadata", Map.of());
    }

    protected static Map<String, Object> row(int position, String chash) {
        return Map.of("position", position, "chash", chash, "chunk_index", position);
    }

    protected static Map<String, Object> doc(String docId, List<Map<String, Object>> rows) {
        return Map.of("doc_id", docId, "rows", rows);
    }

    // ── reads ───────────────────────────────────────────────────────────────────

    protected String chunkText(String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(CHUNKS.CHUNK_TEXT).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hexChash)))
                .fetchOne(CHUNKS.CHUNK_TEXT);
        }
    }

    protected boolean chunkExists(String collection, String hexChash) throws Exception {
        return chunkText(collection, hexChash) != null;
    }

    protected long chunkCount(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return DSL.using(su, SQLDialect.POSTGRES)
                .selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION.eq(collection))
                .fetchOne(0, Long.class);
        }
    }

    protected List<String> manifestChashes(String docId) {
        List<String> out = new ArrayList<>();
        for (var r : repo.getManifest(TENANT, docId)) {
            out.add(String.valueOf(r.get("chash")));
        }
        return out;
    }

    // ── embedder ────────────────────────────────────────────────────────────────

    /** Deterministic 384-dim one-hot embedder that counts every text it is asked to embed. */
    protected static final class CountingFakeEmbedder implements Embedder {
        public final AtomicInteger calls = new AtomicInteger();

        @Override
        public List<float[]> embed(List<String> texts) {
            calls.addAndGet(texts.size());
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                float[] v = new float[384];
                v[Math.floorMod(t.hashCode(), 384)] = 1.0f;
                out.add(v);
            }
            return out;
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(embed(texts), texts.size());
        }

        @Override
        public String modelToken() {
            return "minilm-l6-v2-384";
        }
    }
}
