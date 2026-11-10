// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * Profiling harness, not a gate (scratch branch, never pushed): where does one ladder-shaped
 * {@code POST /v1/vectors/store-get} batch spend its time on a 140k-row store? Answers wren's ask
 * (T2 nexus/store-get-latency-profile). Production-shaped container: migrated by a non-superuser owner,
 * reads as nexus_svc under RLS. Data lives on tmpfs, so these timings exclude disk reads.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class StoreGetLatencyProfileIT {

    private static final String TENANT = "storeget-profile";
    private static final String ADMIN_ROLE = "nexus_admin_storeget";
    private static final String ADMIN_PASS = "nexus_admin_storeget_pass";
    private static final String COLL = "docs__storeget__bge-base-en-v15-768__v1";
    private static final int DIM = 768;
    private static final int ROWS = Integer.getInteger("storeget.rows", 140_000);
    private static final int BATCH = 300;
    private static final int RUNS = 20;
    private static final Path OUT = Path.of("target", "store-get-profile.txt");

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    HikariDataSource explainDs;
    TenantScope tenantScope;
    TenantScope explainScope;
    PgVectorRepository repo;
    final List<String> all = new ArrayList<>(ROWS);
    final StringBuilder out = new StringBuilder();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startDedicated("4g", 7L * 1024 * 1024 * 1024);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var adminCfg = new HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        try (var adminDs = new HikariDataSource(adminCfg)) {
            SchemaMigrator.migrate(adminDs);
            try (Connection c = adminDs.getConnection()) {
                PgContainerHelper.installTestObjects(c);
            }
        }
        svcDs = pool("storeget-svc");
        tenantScope = new TenantScope(svcDs);
        repo = new PgVectorRepository(tenantScope, null, null);
        long t0 = System.nanoTime();
        seed();
        line("seed: %d rows in %.1f s", ROWS, (System.nanoTime() - t0) / 1e9);
    }

    private HikariDataSource pool(String name) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setPoolName(name);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() throws Exception {
        Files.createDirectories(OUT.getParent());
        Files.writeString(OUT, out.toString());
        if (explainDs != null) explainDs.close();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLL,
                "bge-base-en-v15-768");
        }
        Random rnd = new Random(20261110L);
        String[] words = {"engine", "vector", "chunk", "catalog", "tenant", "search", "index", "manifest", "owner",
            "batch", "query", "lexical", "partition", "router", "collection", "metadata", "upgrade", "ladder"};
        int step = 1000;
        for (int start = 0; start < ROWS; start += step) {
            int end = Math.min(start + step, ROWS);
            List<String> ids = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            List<float[]> vecs = new ArrayList<>();
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int i = start; i < end; i++) {
                String id = Chash.ofText("storeget-chunk-" + i).toHex();
                ids.add(id);
                all.add(id);
                StringBuilder t = new StringBuilder(1600);
                while (t.length() < 1500) t.append(words[rnd.nextInt(words.length)]).append(rnd.nextInt(100000)).append(' ');
                texts.add(t.toString());
                vecs.add(unit(rnd));
                metas.add(Map.of("source_path", "/repo/src/file" + (i / 20) + ".py", "chunk_index", i % 20,
                    "content_hash", Integer.toHexString(i), "embedding_model", "bge-base-en-v15-768"));
            }
            repo.upsertChunksWithVectors(TENANT, COLL, ids, texts, vecs, metas);
        }
        // Owners: one document per 20 chunks, as an indexed repo's manifest looks.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int d = 0; d * 20 < ROWS; d += 500) {
                int dEnd = Math.min(d + 500, (ROWS + 19) / 20);
                for (int doc = d; doc < dEnd; doc++) {
                    String[] own = all.subList(doc * 20, Math.min(doc * 20 + 20, ROWS)).toArray(new String[0]);
                    ownAs(ctx, "doc-" + doc, own);
                }
            }
            PgContainerHelper.analyzeTable(su, CHUNKS);
            su.createStatement().execute("ANALYZE nexus.catalog_document_chunks");
            su.createStatement().execute("ANALYZE nexus.catalog_documents");
        }
    }

    /** One owner document per call (PgContainerHelper.ownChunks names its document after the collection). */
    private void ownAs(DSLContext ctx, String docId, String[] chashHex) {
        ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
            + "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING", TENANT, docId, "Owner " + docId, COLL);
        String model = PgContainerHelper.collectionModel(ctx, TENANT, COLL);
        for (int i = 0; i < chashHex.length; i++) {
            ctx.execute("INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection, "
                + "embedding_model) VALUES (?, ?, ?, ?, ?, ?)", TENANT, docId, i,
                Chash.fromHex(chashHex[i]).toBytes(), COLL, model);
        }
    }

    private static float[] unit(Random rnd) {
        float[] v = new float[DIM];
        double n = 0;
        for (int i = 0; i < DIM; i++) { v[i] = (float) rnd.nextGaussian(); n += v[i] * v[i]; }
        float inv = (float) (1.0 / Math.sqrt(n));
        for (int i = 0; i < DIM; i++) v[i] *= inv;
        return v;
    }

    private void line(String fmt, Object... args) {
        String s = String.format(fmt, args);
        out.append(s).append('\n');
        System.out.println("[storeget] " + s);
    }

    private List<String> batch(long seed) {
        List<String> copy = new ArrayList<>(all);
        java.util.Collections.shuffle(copy, new Random(seed));
        return new ArrayList<>(copy.subList(0, BATCH));
    }

    private double[] time(Supplier<?> op) {
        for (int i = 0; i < 3; i++) op.get();
        double[] ms = new double[RUNS];
        for (int i = 0; i < RUNS; i++) {
            long t = System.nanoTime();
            op.get();
            ms[i] = (System.nanoTime() - t) / 1e6;
        }
        Arrays.sort(ms);
        return ms;
    }

    private void report(String label, double[] ms) {
        line("%-58s median %7.1f ms  p10 %7.1f  p90 %7.1f  (n=%d)", label, ms[ms.length / 2], ms[ms.length / 10],
            ms[ms.length * 9 / 10], ms.length);
    }

    @Test
    void profile() throws Exception {
        List<String> ids = batch(7L);
        byte[][] raw = ids.stream().map(h -> Chash.fromHex(h).toBytes()).toArray(byte[][]::new);

        // 1. The production call, end to end, as the handler makes it (limit = the page's own size).
        report("repo.get (store-get path, excludeSensitive=false)",
            time(() -> repo.get(TENANT, COLL, ids, BATCH, 0, false, false)));
        report("repo.get (store-get path, excludeSensitive=true)",
            time(() -> repo.get(TENANT, COLL, ids, BATCH, 0, false, true)));
        Map<String, Object> env = repo.get(TENANT, COLL, ids, BATCH, 0, false, false);
        line("repo.get returned %d ids, count=%s", ((List<?>) env.get("ids")).size(), env.get("count"));

        // 2. Pieces: an empty tenant transaction, the sensitives lookup alone, presentRows.
        report("empty withTenant transaction (SELECT 1)",
            time(() -> tenantScope.withTenant(TENANT, ctx -> ctx.fetch("SELECT 1"))));
        report("ChunkSensitivity.sensitiveChashes alone (2nd transaction)",
            time(() -> tenantScope.withTenant(TENANT, ctx -> ChunkSensitivity.sensitiveChashes(ctx, TENANT, COLL, ids))));
        report("repo.presentRows (no content, no liveness)",
            time(() -> repo.presentRows(TENANT, COLL, ids, false)));

        // 2b. The listing the client's get_collection makes before every chunk read (list_collections(strict=True)
        // -> GET /v1/vectors/stats, full form), and the routing form, on this store.
        report("repo.collectionStats (GET /v1/vectors/stats, full)",
            time(() -> repo.collectionStats(TENANT, null)));
        report("repo.collectionRouting (GET /v1/vectors/stats?fields=routing)",
            time(() -> repo.collectionRouting(TENANT, null)));

        // 3. Hand variants of the main statement, as nexus_svc, fully fetched.
        String base = " FROM nexus.chunks c WHERE c.tenant_id = ? AND c.collection = ? AND c.embedding_model = ?"
            + " AND c.chash = ANY(?)";
        String live = " AND EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))";
        String tail = " ORDER BY c.chash LIMIT " + BATCH;
        String model = "bge-base-en-v15-768";
        Map<String, String> variants = new java.util.LinkedHashMap<>();
        variants.put("main stmt as built: text+meta+count, live", "SELECT c.chash, c.chunk_text, c.metadata, count(*) OVER ()" + base + live + tail);
        variants.put("no chunk_text: meta+count, live", "SELECT c.chash, c.metadata, count(*) OVER ()" + base + live + tail);
        variants.put("no count: text+meta, live", "SELECT c.chash, c.chunk_text, c.metadata" + base + live + tail);
        variants.put("no liveness: text+meta+count", "SELECT c.chash, c.chunk_text, c.metadata, count(*) OVER ()" + base + tail);
        variants.put("meta only, no liveness, no count", "SELECT c.chash, c.metadata" + base + tail);
        for (var v : variants.entrySet()) {
            String sql = v.getValue();
            report("sql: " + v.getKey(), time(() -> tenantScope.withTenant(TENANT,
                ctx -> ctx.fetch(sql, TENANT, COLL, model, raw).size())));
        }
        for (var v : variants.entrySet()) {
            Result<Record> plan = tenantScope.withTenant(TENANT,
                ctx -> ctx.fetch("EXPLAIN (ANALYZE, BUFFERS, VERBOSE) " + v.getValue(), TENANT, COLL, model, raw));
            line("---- EXPLAIN ANALYZE: %s", v.getKey());
            for (Record r : plan) line("%s", r.get(0));
        }

        // 4. auto_explain on the real call: every statement repo.get runs, nested ones included.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.enableAutoExplainForService(su);
        }
        explainDs = pool("storeget-explain");
        explainScope = new TenantScope(explainDs);
        var explainRepo = new PgVectorRepository(explainScope, null, null);
        explainRepo.get(TENANT, COLL, ids, BATCH, 0, false, false);
        int from = PgContainerHelper.serverLog(pg).length();
        explainRepo.get(TENANT, COLL, ids, BATCH, 0, false, false);
        Thread.sleep(1500);
        String logs = PgContainerHelper.serverLog(pg);
        line("---- auto_explain log of one warm repo.get call");
        line("%s", logs.substring(Math.min(from, logs.length())));

        // 5. Function properties.
        try (Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery("SELECT proname, (SELECT lanname FROM pg_language l WHERE l.oid = p.prolang), provolatile, prosecdef, "
                + "proisstrict, proretset, array_to_string(proconfig, ',') FROM pg_proc p WHERE proname IN "
                + "('chunk_live_owners','chunk_sensitive_many','chunk_sensitive')");
            while (rs.next()) {
                line("pg_proc: %s lang=%s volatile=%s secdef=%s strict=%s retset=%s config=%s", rs.getString(1),
                    rs.getString(2), rs.getString(3), rs.getBoolean(4), rs.getBoolean(5), rs.getBoolean(6),
                    rs.getString(7));
            }
            var idx = su.createStatement().executeQuery("SELECT indexdef FROM pg_indexes WHERE tablename IN "
                + "('catalog_document_chunks') ORDER BY 1");
            while (idx.next()) line("index: %s", idx.getString(1));
            var sz = su.createStatement().executeQuery("SELECT pg_size_pretty(pg_total_relation_size('nexus.chunks')), "
                + "current_setting('shared_buffers')");
            if (sz.next()) line("chunks total size %s, shared_buffers %s", sz.getString(1), sz.getString(2));
        }
    }
}
