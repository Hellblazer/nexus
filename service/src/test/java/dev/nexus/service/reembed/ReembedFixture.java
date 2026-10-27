// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedSourceReader;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * The substrate the re-embed runner tests share (RDR-210 Phase 6 Step 2, bead nexus-f98bg): a real Postgres
 * (Testcontainers, the product schema, the service role, so RLS applies to every engine read and write), the
 * job repository, the source reader, the catalog and two combined-write services, as {@code DocumentMoveTest} sets
 * them up. The embedders are seeded-random fakes with the real tokens and widths: {@code bge-base-en-v15-768} for the
 * source, {@code minilm-l6-v2-384} for the target. The target embedder's call count IS the number of chunks the
 * moves embedded.
 *
 * <p>Every test builds its own tenant ({@link #pair}), so tests share a database without sharing a source lock, a
 * profile or a job. Every read of a table goes through typed jOOQ over a superuser connection; no raw SQL.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class ReembedFixture {

    static final String SVC_ROLE = "svc_reembed_runner_test";
    static final String SVC_PASS = "svc_reembed_runner_test_pass";
    static final String BGE = "bge-base-en-v15-768";
    static final String MINI = "minilm-l6-v2-384";
    static final String CONTENT_TYPE = "knowledge";

    /** One batch holds one three-chunk document (a second would make six), a pass bound of three. */
    static final ReembedRunner.Settings ONE_DOC_PER_BATCH =
        new ReembedRunner.Settings(3, 3, Duration.ofSeconds(60));

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository catalog;
    ReembedJobRepository jobs;
    ReembedSourceReader reader;
    Fake srcEmbed;
    Fake tgtEmbed;
    CombinedWriteService srcSvc;
    CombinedWriteService moveSvc;
    final AtomicInteger seq = new AtomicInteger();

    /** A tenant with a source and a target collection, the profile naming the target's model, and the tag its ids carry. */
    record Fx(String tenant, String tag, String src, String tgt) {
    }

    @BeforeAll
    void startFixture() throws Exception {
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
        scope = new TenantScope(svcDs);
        catalog = new CatalogRepository(scope);
        jobs = new ReembedJobRepository(scope);
        reader = new ReembedSourceReader(scope);
        srcEmbed = new Fake(BGE, 768);
        tgtEmbed = new Fake(MINI, 384);
        var srcRouter = new EmbedderRouter(srcEmbed, "document");
        var tgtRouter = new EmbedderRouter(tgtEmbed, "document");
        srcSvc = new CombinedWriteService(scope, catalog, srcRouter);
        moveSvc = new CombinedWriteService(scope, catalog, tgtRouter);
    }

    @AfterAll
    void stopFixture() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── embedder ─────────────────────────────────────────────────────────────

    /** Deterministic unit vectors seeded by the text, counting every text embedded. */
    static final class Fake implements Embedder {
        final String token;
        final int dim;
        final AtomicInteger calls = new AtomicInteger();

        Fake(String token, int dim) {
            this.token = token;
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            calls.addAndGet(texts.size());
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                Random r = new Random(t.hashCode());
                float[] v = new float[dim];
                double norm = 0;
                for (int i = 0; i < dim; i++) {
                    v[i] = (float) r.nextGaussian();
                    norm += v[i] * v[i];
                }
                float n = (float) Math.sqrt(norm);
                for (int i = 0; i < dim; i++) v[i] /= n;
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
            return token;
        }
    }

    // ── tenants, collections, profile ────────────────────────────────────────

    <T> T su(Function<DSLContext, T> f) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return f.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    void register(String tenant, String collection) {
        scope.withTenant(tenant, ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            return null;
        });
    }

    /** Make {@code model} the tenant's profile model for {@link #CONTENT_TYPE}. */
    void setProfile(String tenant, String model, int dimension) {
        su(ctx -> {
            ctx.deleteFrom(EMBEDDING_PROFILE).where(EMBEDDING_PROFILE.TENANT_ID.eq(tenant))
                .and(EMBEDDING_PROFILE.CONTENT_TYPE.eq(CONTENT_TYPE)).execute();
            return ctx.insertInto(EMBEDDING_PROFILE, EMBEDDING_PROFILE.TENANT_ID, EMBEDDING_PROFILE.CONTENT_TYPE,
                    EMBEDDING_PROFILE.EMBEDDING_MODEL, EMBEDDING_PROFILE.DIMENSION)
                .values(tenant, CONTENT_TYPE, model, dimension).execute();
        });
    }

    /** A fresh tenant with a source and target sibling pair (bge-768 and minilm-384) and the profile naming minilm. */
    Fx pair(String tag) {
        String t = tag + seq.incrementAndGet();
        String owner = "rr" + t;
        Fx fx = new Fx("reembed-runner-" + t, t, CONTENT_TYPE + "__" + owner + "__" + BGE + "__v1",
            CONTENT_TYPE + "__" + owner + "__" + MINI + "__v1");
        register(fx.tenant(), fx.src());
        register(fx.tenant(), fx.tgt());
        setProfile(fx.tenant(), MINI, 384);
        return fx;
    }

    // ── documents ────────────────────────────────────────────────────────────

    static String ch(String text) {
        return Chash.ofText(text).toHex();
    }

    /** Registers the document in the source and writes {@code texts} as its chunks, one manifest row each. */
    String seedDoc(Fx fx, String name, List<String> texts) {
        String docId = "rr." + fx.tag() + "." + name;
        catalog.upsertDocument(fx.tenant(), Map.of("tumbler", docId, "title", "title-" + docId,
            "content_type", CONTENT_TYPE, "corpus", CONTENT_TYPE, "physical_collection", fx.src(), "chunk_count", 0));
        clientWrite(fx, fx.src(), docId, texts);
        return docId;
    }

    /** A client manifest write of {@code texts} into {@code collection}, through the combined write. */
    void clientWrite(Fx fx, String collection, String docId, List<String> texts) {
        List<Map<String, Object>> chunks = new ArrayList<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int k = 0; k < texts.size(); k++) {
            chunks.add(Map.of("chash", ch(texts.get(k)), "text", texts.get(k), "metadata", Map.of("k", k)));
            rows.add(Map.of("position", k, "chash", ch(texts.get(k)), "chunk_index", k));
        }
        var r = srcSvc.writeManyCombined(fx.tenant(), collection, chunks,
            List.of(Map.of("doc_id", docId, "rows", rows)), null, false, false);
        assertThat((List<?>) r.response().get("failed_doc_ids")).as("client write of " + docId).isEmpty();
    }

    /** {@code n} documents of {@code perDoc} chunks each, ids sorting in creation order. */
    List<String> seedDocs(Fx fx, int n, int perDoc) {
        List<String> ids = new ArrayList<>();
        for (int d = 0; d < n; d++) {
            List<String> texts = new ArrayList<>();
            for (int k = 0; k < perDoc; k++) texts.add("doc " + fx.tag() + " d" + d + " chunk " + k + " lorem ipsum");
            ids.add(seedDoc(fx, String.format("d%03d", d), texts));
        }
        return ids;
    }

    /** The note URI a seeded note carries in the source: {@code nx-note://<source>/<name>}. */
    static String noteUri(String collection, String name) {
        return "nx-note://" + collection + "/" + name;
    }

    /** {@link #seedDoc} for a note: the document's source_uri is {@code nx-note://<source>/<name>}. */
    String seedNote(Fx fx, String name, List<String> texts) {
        String docId = seedDoc(fx, name, texts);
        setSourceUri(fx, docId, noteUri(fx.src(), name));
        return docId;
    }

    void setSourceUri(Fx fx, String docId, String uri) {
        su(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, uri)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant())).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId)).execute());
    }

    String sourceUri(Fx fx, String docId) {
        return su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant())).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
            .fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
    }

    /** A live document homed in the target that holds {@code uri}; returns its tumbler. */
    String seedHolder(Fx fx, String name, String uri) {
        String docId = "rr." + fx.tag() + "." + name;
        catalog.upsertDocument(fx.tenant(), Map.of("tumbler", docId, "title", "title-" + docId,
            "content_type", CONTENT_TYPE, "corpus", CONTENT_TYPE, "physical_collection", fx.tgt(), "chunk_count", 0));
        setSourceUri(fx, docId, uri);
        return docId;
    }

    // ── jobs and runners ─────────────────────────────────────────────────────

    Job startJob(Fx fx, Long tokenCap, boolean keepSource) {
        return jobs.start(fx.tenant(), fx.src(), fx.tgt(), tokenCap, keepSource, false);
    }

    Job job(Fx fx, UUID id) {
        return jobs.get(fx.tenant(), id).orElseThrow();
    }

    ReembedRunner runner(Fx fx, ReembedRunner.Settings settings) {
        return new ReembedRunner(jobs, reader, catalog, moveSvc, () -> List.of(fx.tenant()), settings);
    }

    /** Move a job to {@code to} by the repository's compare-and-set, as a control route would. */
    Job force(Fx fx, Job from, String to, ReembedJobRepository.Transition change) {
        return jobs.applyTransition(fx.tenant(), from.jobId(), from.state(), from.cutoverStep(), to, change)
            .orElseThrow(() -> new AssertionError(from.state() + " -> " + to + " matched no row"));
    }

    // ── reads ────────────────────────────────────────────────────────────────

    String physical(Fx fx, String docId) {
        return su(ctx -> ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant())).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId))
            .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION));
    }

    Set<String> manifestCollections(Fx fx, String docId) {
        return new LinkedHashSet<>(su(ctx -> ctx.selectDistinct(CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(fx.tenant())).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
            .fetch(CATALOG_DOCUMENT_CHUNKS.COLLECTION)));
    }

    List<String> manifestChashes(Fx fx, String docId) {
        return su(ctx -> ctx.select(CATALOG_DOCUMENT_CHUNKS.CHASH).from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(fx.tenant())).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
            .orderBy(CATALOG_DOCUMENT_CHUNKS.POSITION)
            .fetch(r -> java.util.HexFormat.of().formatHex(r.value1())));
    }

    int chunkCount(Fx fx, String collection) {
        return su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(fx.tenant()), CHUNKS.COLLECTION.eq(collection)));
    }

    int gcAuditRows(Fx fx) {
        return su(ctx -> ctx.fetchCount(GC_AUDIT, GC_AUDIT.TENANT_ID.eq(fx.tenant())));
    }

    /** The collection's {@code superseded_by}, or null when the collection is not registered. */
    String supersededBy(Fx fx, String collection) {
        Map<String, Object> row = catalog.getCollection(fx.tenant(), collection);
        return row == null ? null : String.valueOf(row.get("superseded_by"));
    }

    /** Map of the first {@code n} docs of the list, for a readable assertion. */
    static List<String> head(List<String> docs, int n) {
        return docs.subList(0, n);
    }

    /** A fresh text map used by a writer that rewrites a document, distinct on every call. */
    final Map<String, Integer> rewrites = new HashMap<>();

    List<String> freshTexts(Fx fx, String docId) {
        int n = rewrites.merge(docId, 1, Integer::sum);
        return List.of("rewrite " + n + " of " + docId + " " + fx.tag() + " a", "rewrite " + n + " of " + docId
            + " " + fx.tag() + " b", "rewrite " + n + " of " + docId + " " + fx.tag() + " c");
    }
}
