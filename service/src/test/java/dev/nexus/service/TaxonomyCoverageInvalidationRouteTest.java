// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TaxonomyCoverage.Counts;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Consumer;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-7fku2.1.2 (RDR-134 Step 2): which engine routes invalidate a collection's cached topic coverage.
 *
 * <p><b>The choice this pins: option (a), the engine invalidates on every store, no client signalling.</b>
 * {@link #aStoredBatchWhoseAssignmentIsDeferred_lowersTheRatioOnTheNextRead} is the test of that choice. The engine
 * cannot see the client's deferral or its lost-chunk tripwire; it sees the store. The test caches a fully assigned
 * collection (coverage 1.0), then stores ten new chunks through the real combined-write route and makes NO assignment
 * call, which is what a deferred or lost assignment is, and the next read is 0.5. With option (b) the cache would
 * still say 1.0 until the client told it.
 *
 * <p>Each route test reads the value first (caching it), changes the database behind the cache's back (so a stale
 * read is detectable), calls the route, and reads again. The cache is the {@link NexusService#taxonomyCoverage()}
 * one the handlers share, not a private instance.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyCoverageInvalidationRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-7fku212-0123456789abcdef0123456789abcdef";
    private static final String TENANT = "7fku212-route";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    CatalogRepository catalog;
    HttpClient http;
    private final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "7fku212-test");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME); // nexus_svc, the role taxonomy-022 grants EXECUTE to
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        var embedder = new FakeEmbedder();
        var scope = new TenantScope(svcDs);
        var vectors = new PgVectorRepository(scope, embedder, embedder);
        catalog = new CatalogRepository(scope);
        service = new NexusService(0, TOKEN, svcDs, new EmbedderRouter(embedder, "document"), vectors);
        service.start();
        http = TestHttp.client();
        // Burn the per-tenant ghost sweep before registering collections (see OwnerlessWriteRefusalTest).
        http.send(TestHttp.request(url("/v1/catalog/collections/list"))
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ───────────────────────────────────────────────────────────────

    private record Coll(String name, long topic) {}

    private String url(String path) {
        return "http://127.0.0.1:" + service.getPort() + path;
    }

    private void seed(Consumer<DSLContext> work) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            work.accept(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private static String chash(String seed) {
        return Chash.ofText(seed).toHex();
    }

    /** A registered 384-dim collection with a topic and its centroid, and {@code assigned} live chunks all assigned to it. */
    private Coll collection(String stem, int assigned) throws Exception {
        String name = "knowledge__7fku212-" + stem + "-" + seq.incrementAndGet() + "__" + MODEL + "__v1";
        long[] topic = new long[1];
        seed(dsl -> {
            PgContainerHelper.insertCollection(dsl, TENANT, name);
            topic[0] = dsl.insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, TENANT).set(TOPICS.LABEL, "topic of " + name).set(TOPICS.COLLECTION, name).set(TOPICS.COLLECTION_ID, CollectionIds.idOf(TENANT, name))
                .set(TOPICS.CREATED_AT, SEED_TS)
                .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
            dsl.insertInto(TAXONOMY_CENTROIDS)
                .set(TAXONOMY_CENTROIDS.TENANT_ID, TENANT).set(TAXONOMY_CENTROIDS.COLLECTION, name).set(TAXONOMY_CENTROIDS.COLLECTION_ID, CollectionIds.idOf(TENANT, name))
                .set(TAXONOMY_CENTROIDS.TOPIC_ID, topic[0]).set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, MODEL)
                .set(TAXONOMY_CENTROIDS.LABEL, "centroid")
                .set(VectorSeeds.centroidEmbedding(384), VectorSeeds.vecXY(384, 1f, 0f))
                .execute();
        });
        Coll c = new Coll(name, topic[0]);
        liveChunks(c, "k", assigned, true);
        return c;
    }

    /** {@code n} live chunks (stored, owned) {@code prefix0..}; assigned to the collection's topic when {@code assign}. */
    private void liveChunks(Coll c, String prefix, int n, boolean assign) throws Exception {
        if (n == 0) return;
        String[] hex = new String[n];
        for (int i = 0; i < n; i++) hex[i] = chash(c.name() + "/" + prefix + i);
        seed(dsl -> {
            PgContainerHelper.insertOwnedChunks(dsl, TENANT, c.name(), 384, hex);
            if (assign) {
                for (String h : hex) assignTo(dsl, c, h);
            }
        });
    }

    private void assignTo(DSLContext dsl, Coll c, String hex) {
        dsl.insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT).set(TOPIC_ASSIGNMENTS.DOC_ID, HexFormat.of().parseHex(hex))
            .set(TOPIC_ASSIGNMENTS.TOPIC_ID, c.topic()).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, c.name()).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, CollectionIds.idOf(TENANT, c.name()))
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, MODEL)
            .execute();
    }

    private Counts read(Coll c) {
        var counts = service.taxonomyCoverage().lookup(TENANT, c.name());
        assertThat(counts).as("the count could be taken").isPresent();
        return counts.get();
    }

    private HttpResponse<String> post(String path, Object body) throws Exception {
        var req = TestHttp.request(url(path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static List<Double> vector(int seed) {
        List<Double> v = new ArrayList<>(384);
        for (int i = 0; i < 384; i++) v.add(Math.sin(seed * 31.0 + i * 0.37) * 0.25);
        return v;
    }

    // ── option (a): the store invalidates ─────────────────────────────────────

    @Test
    void aStoredBatchWhoseAssignmentIsDeferred_lowersTheRatioOnTheNextRead() throws Exception {
        Coll c = collection("deferred", 10);
        assertThat(read(c)).as("the cached reading: fully assigned").isEqualTo(new Counts(true, 10, 0));
        assertThat(read(c).routable()).isTrue();

        // ten new chunks stored through the real combined-write route; the client's assignment hook never runs
        // (deferred) or fails (lost): the engine is told nothing
        String doc = "7fku212.deferred." + seq.incrementAndGet();
        catalog.upsertDocument(TENANT, Map.of("tumbler", doc, "title", "deferred batch", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", c.name(), "chunk_count", 0));
        List<Map<String, Object>> chunks = new ArrayList<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int i = 0; i < 10; i++) {
            String h = chash(c.name() + "/stored" + i);
            chunks.add(Map.of("chash", h, "text", "stored text " + i, "metadata", Map.of()));
            rows.add(Map.of("position", i, "chash", h, "chunk_index", i));
        }
        var resp = post("/v1/catalog/manifest/write_many", Map.of("collection", c.name(),
            "docs", List.of(Map.of("doc_id", doc, "rows", rows)), "chunks", chunks));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);

        Counts after = read(c);
        assertThat(after).as("the next read counts the stored batch, unassigned").isEqualTo(new Counts(true, 20, 10));
        assertThat(after.coverage()).isEqualTo(0.5);
        assertThat(after.routable()).as("so the collection stops being routed until the chunks are assigned").isFalse();
    }

    @Test
    void upsertChunks_invalidatesItsOwnCollection_onBothBranches_andNoOther() throws Exception {
        Coll a = collection("upsertA", 6);
        Coll b = collection("upsertB", 6);
        assertThat(read(a)).isEqualTo(new Counts(true, 6, 0));
        assertThat(read(b)).isEqualTo(new Counts(true, 6, 0));
        liveChunks(a, "late", 6, false); // behind the cache's back
        liveChunks(b, "late", 6, false);
        assertThat(read(a)).as("precondition: the cache serves the old value").isEqualTo(new Counts(true, 6, 0));

        // the embed branch: an owned chash is re-embedded (force_re_embed)
        String owned = chash(a.name() + "/k0");
        var embedded = post("/v1/vectors/upsert-chunks", Map.of("collection", a.name(), "ids", List.of(owned),
            "documents", List.of("refreshed"), "metadatas", List.of(Map.of()), "force_re_embed", true));
        assertThat(embedded.statusCode()).as("body: %s", embedded.body()).isEqualTo(200);

        assertThat(read(a)).as("A recounted").isEqualTo(new Counts(true, 12, 6));
        assertThat(read(b)).as("B untouched: a store to A is not a store to B").isEqualTo(new Counts(true, 6, 0));

        // the passthrough branch: supplied vectors, no embedder
        liveChunks(a, "later", 6, false);
        assertThat(read(a)).as("precondition: cached again").isEqualTo(new Counts(true, 12, 6));
        var passthrough = post("/v1/vectors/upsert-chunks", Map.of("collection", a.name(), "ids", List.of(owned),
            "documents", List.of("refreshed again"), "metadatas", List.of(Map.of()),
            "embeddings", List.of(vector(1))));
        assertThat(passthrough.statusCode()).as("body: %s", passthrough.body()).isEqualTo(200);
        assertThat(read(a)).as("A recounted again").isEqualTo(new Counts(true, 18, 12));
        assertThat(read(b)).isEqualTo(new Counts(true, 6, 0));
    }

    @Test
    void aFailedUpsert_stillInvalidates_becauseARefusedBatchMayHaveCommittedPartOfItself() throws Exception {
        Coll a = collection("upsertfail", 4);
        assertThat(read(a)).isEqualTo(new Counts(true, 4, 0));
        liveChunks(a, "late", 4, false);

        var policy = service.ownerlessWritePolicy();
        var before = policy.mode();
        HttpResponse<String> resp;
        try {
            policy.set(dev.nexus.service.vectors.OwnerlessWriteMode.ENFORCE);
            // a chash with no owner is refused (422) by the ownerless-write guard: nothing is stored, but the handler
            // cannot know that from outside the repository, so it drops the entry either way
            resp = post("/v1/vectors/upsert-chunks", Map.of("collection", a.name(),
                "ids", List.of(chash(a.name() + "/no-owner")), "documents", List.of("ownerless"),
                "metadatas", List.of(Map.of())));
        } finally {
            policy.set(before);
        }
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isIn(409, 422);

        assertThat(read(a)).isEqualTo(new Counts(true, 8, 4));
    }

    // ── the taxonomy routes ───────────────────────────────────────────────────

    @Test
    void anAssignment_raisesTheRatioOnTheNextRead_aCollectionScopedRouteDropsOnlyItsCollection() throws Exception {
        Coll a = collection("assignA", 4);
        Coll b = collection("assignB", 4);
        liveChunks(a, "late", 4, false);
        liveChunks(b, "late", 4, false);
        service.taxonomyCoverage().invalidateTenant(TENANT);
        assertThat(read(a)).as("4 of 8 live chunks unassigned").isEqualTo(new Counts(true, 8, 4));
        assertThat(read(b)).isEqualTo(new Counts(true, 8, 4));
        // behind the cache's back both collections get their four unassigned chunks assigned
        seed(dsl -> {
            for (int i = 0; i < 4; i++) {
                assignTo(dsl, a, chash(a.name() + "/late" + i));
                assignTo(dsl, b, chash(b.name() + "/late" + i));
            }
        });
        assertThat(read(a)).as("precondition: the cache serves the old value").isEqualTo(new Counts(true, 8, 4));

        // assign_from_chashes names its collection: A's entry drops, B's does not. Whatever the route itself
        // assigns, every chunk of A already carries its assignment, so A's recount is 8 of 8.
        var resp = post("/v1/taxonomy/assignments/assign_from_chashes", Map.of("collection", a.name(),
            "chashes", List.of(chash(a.name() + "/late0")), "cross_collection", false));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);

        assertThat(read(a)).as("A was recounted").isEqualTo(new Counts(true, 8, 0));
        assertThat(read(b)).as("B is not the collection the route named: still the old value")
            .isEqualTo(new Counts(true, 8, 4));
    }

    @Test
    void aTaxonomyRouteThatNamesNoCollection_dropsTheWholeTenant() throws Exception {
        Coll a = collection("noname", 4);
        liveChunks(a, "late", 4, false);
        service.taxonomyCoverage().invalidateTenant(TENANT);
        assertThat(read(a)).isEqualTo(new Counts(true, 8, 4));
        String lateHash = chash(a.name() + "/late0");
        seed(dsl -> assignTo(dsl, a, lateHash));
        assertThat(read(a)).as("precondition: cached").isEqualTo(new Counts(true, 8, 4));

        // /assignments/assign carries doc_id and topic_id, no collection: the tenant is dropped. The row exists
        // already (seeded above), so the route is an ON CONFLICT no-op; the invalidation does not depend on a write
        var resp = post("/v1/taxonomy/assignments/assign", Map.of("doc_id", lateHash, "topic_id", a.topic(),
            "assigned_by", "centroid", "source_collection", a.name()));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);

        assertThat(read(a)).isEqualTo(new Counts(true, 8, 3));
    }

    @Test
    void aRouteThatChangesNoCoverageInput_leavesTheCacheAlone() throws Exception {
        Coll a = collection("untouched", 4);
        assertThat(read(a)).isEqualTo(new Counts(true, 4, 0));
        liveChunks(a, "late", 4, false);

        // a topic label rename changes no count: the cached value must survive it
        var resp = post("/v1/taxonomy/topics/rename", Map.of("id", a.topic(), "label", "renamed " + a.name()));
        assertThat(resp.statusCode()).as("a route that reached its handler, body: %s", resp.body()).isLessThan(500);

        assertThat(read(a)).as("still the cached value: the rename is not a coverage input")
            .isEqualTo(new Counts(true, 4, 0));
    }

    // ── the catalog routes ────────────────────────────────────────────────────

    @Test
    void aDocumentDelete_whichNamesNoCollection_dropsTheTenant_andItsChunksLeaveTheCount() throws Exception {
        Coll a = collection("docdelete", 0);
        String doc = "7fku212.docdelete." + seq.incrementAndGet();
        catalog.upsertDocument(TENANT, Map.of("tumbler", doc, "title", "to delete", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", a.name(), "chunk_count", 0));
        List<Map<String, Object>> chunks = new ArrayList<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int i = 0; i < 4; i++) {
            String h = chash(a.name() + "/d" + i);
            chunks.add(Map.of("chash", h, "text", "d " + i, "metadata", Map.of()));
            rows.add(Map.of("position", i, "chash", h, "chunk_index", i));
        }
        var written = post("/v1/catalog/manifest/write_many", Map.of("collection", a.name(),
            "docs", List.of(Map.of("doc_id", doc, "rows", rows)), "chunks", chunks));
        assertThat(written.statusCode()).as("body: %s", written.body()).isEqualTo(200);
        assertThat(read(a)).as("four live, unassigned chunks").isEqualTo(new Counts(true, 4, 4));

        var deleted = post("/v1/catalog/delete", Map.of("tumbler", doc));
        assertThat(deleted.statusCode()).as("body: %s", deleted.body()).isEqualTo(200);

        assertThat(read(a)).as("the tombstoned document's chunks are no longer live: out of both counts")
            .isEqualTo(new Counts(true, 0, 0));
    }

    @Test
    void assignFromChashesWithTheCrossPass_dropsTheWholeTenant_becauseItWritesOntoOtherCollectionsTopics() throws Exception {
        Coll a = collection("crossA", 4);
        Coll b = collection("crossB", 4);
        liveChunks(a, "late", 4, false);
        liveChunks(b, "late", 4, false);
        service.taxonomyCoverage().invalidateTenant(TENANT);
        assertThat(read(a)).isEqualTo(new Counts(true, 8, 4));
        assertThat(read(b)).isEqualTo(new Counts(true, 8, 4));
        seed(dsl -> {
            for (int i = 0; i < 4; i++) {
                assignTo(dsl, a, chash(a.name() + "/late" + i));
                assignTo(dsl, b, chash(b.name() + "/late" + i));
            }
        });

        // cross_collection is absent, so it defaults to true: the route may write projection rows onto B's topics
        var resp = post("/v1/taxonomy/assignments/assign_from_chashes", Map.of("collection", a.name(),
            "chashes", List.of(chash(a.name() + "/late0"))));
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);

        assertThat(read(a)).isEqualTo(new Counts(true, 8, 0));
        assertThat(read(b)).as("B is dropped too: the cross pass can write onto its topics").isEqualTo(new Counts(true, 8, 0));
    }

    // ── routes that answer before they write: the job's own end drops ─────────

    @SuppressWarnings("unchecked")
    private Map<String, Object> pollUntilDone(String path, String stateKey) throws Exception {
        for (int i = 0; i < 300; i++) {
            var resp = http.send(TestHttp.request(url(path)).header("Authorization", "Bearer " + TOKEN).GET().build(),
                HttpResponse.BodyHandlers.ofString());
            assertThat(resp.statusCode()).as("poll body: %s", resp.body()).isEqualTo(200);
            Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
            if (!"running".equals(body.get(stateKey))) return body;
            Thread.sleep(100);
        }
        throw new AssertionError("the job did not finish in 30 s: " + path);
    }

    @SuppressWarnings("unchecked")
    @Test
    void aReconcileCommitJob_dropsTheTenantWhenItEnds_evenWhenItFails() throws Exception {
        Coll a = collection("reconcile", 4);
        assertThat(read(a)).isEqualTo(new Counts(true, 4, 0));
        liveChunks(a, "late", 4, false); // behind the cache's back

        // the run was never staged: the commit job ends failed (run_not_found). This read passes whether the drop lands
        // at the 202 or at the job's end; CoverageInvalidationTest pins that the route is not listed, so only the job's
        // own drop can make it pass.
        var started = post("/v1/catalog/reconcile/commit", Map.of("owner", "1.7", "run_id", "7fku212-no-such-run"));
        assertThat(started.statusCode()).as("body: %s", started.body()).isEqualTo(202);
        String jobId = (String) MAPPER.readValue(started.body(), Map.class).get("job_id");
        var done = pollUntilDone("/v1/catalog/reconcile/" + jobId, "status");
        assertThat(done.get("status")).as("the job ended: %s", done).isEqualTo("failed");

        assertThat(read(a)).as("a count read after the job ended is current, whether the job wrote or failed")
            .isEqualTo(new Counts(true, 8, 4));
    }

    @SuppressWarnings("unchecked")
    @Test
    void aManifestFillJob_dropsTheTenantWhenItEnds() throws Exception {
        Coll a = collection("fill", 4);
        assertThat(read(a)).isEqualTo(new Counts(true, 4, 0));
        liveChunks(a, "late", 4, false);

        var started = post("/v1/catalog/manifest/fill", Map.of("kind", "labels", "dry_run", true));
        assertThat(started.statusCode()).as("body: %s", started.body()).isEqualTo(202);
        String jobId = (String) MAPPER.readValue(started.body(), Map.class).get("job_id");
        var done = pollUntilDone("/v1/catalog/manifest/fill/" + jobId, "state");
        assertThat(done.get("state")).as("the job ended: %s", done).isNotEqualTo("running");

        assertThat(read(a)).isEqualTo(new Counts(true, 8, 4));
    }

    // ── the restore routes put chunks back into their origin ──────────────────

    @Test
    void theRestoreRoutes_dropTheOriginCollection_andNotAnother() throws Exception {
        Coll a = collection("restoreA", 4);
        Coll b = collection("restoreB", 4);
        seed(dsl -> PgContainerHelper.insertCollection(dsl, TENANT, "quarantine-" + a.name()));
        assertThat(read(a)).isEqualTo(new Counts(true, 4, 0));
        assertThat(read(b)).isEqualTo(new Counts(true, 4, 0));
        liveChunks(a, "late", 4, false);
        liveChunks(b, "late", 4, false);

        var rereferenced = post("/v1/vectors/gc/restore-rereferenced",
            Map.of("quarantine_collection", "quarantine-" + a.name(), "origin_collection", a.name()));
        assertThat(rereferenced.statusCode()).as("body: %s", rereferenced.body()).isLessThan(500);
        assertThat(read(a)).as("restore-rereferenced dropped its origin").isEqualTo(new Counts(true, 8, 4));
        assertThat(read(b)).as("and not B").isEqualTo(new Counts(true, 4, 0));

        liveChunks(a, "later", 4, false);
        assertThat(read(a)).as("precondition: cached again").isEqualTo(new Counts(true, 8, 4));
        var restored = post("/v1/vectors/gc/quarantine-restore",
            Map.of("origin_collection", a.name(), "chashes", List.of(chash(a.name() + "/later0"))));
        assertThat(restored.statusCode()).as("body: %s", restored.body()).isLessThan(500);
        assertThat(read(a)).as("quarantine-restore dropped its origin").isEqualTo(new Counts(true, 12, 8));
        assertThat(read(b)).isEqualTo(new Counts(true, 4, 0));
    }

    // ── fixture embedder ──────────────────────────────────────────────────────

    /** Deterministic 384-dim one-hot embedder. */
    private static final class FakeEmbedder implements Embedder {
        @Override
        public List<float[]> embed(List<String> texts) {
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
            return MODEL;
        }
    }
}
