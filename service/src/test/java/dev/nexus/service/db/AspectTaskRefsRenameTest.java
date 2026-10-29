// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.NexusService;
import dev.nexus.service.PgActivityProbe;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-235 Test Plan scenarios 17, 18, 19 and 22 (bead nexus-5r2sc.2.6), on a real Postgres: a collection's
 * unconsumed {@code aspects} tasks name it in their body, so every engine verb that renames, moves, re-homes
 * or purges the collection, or asks whether it holds anything, must act on those tasks in its own
 * transaction.
 *
 * <p>Every rename-shaped scenario seeds the three kinds of unconsumed task (one available, one claimed by a
 * worker, one dead-lettered) and asserts all three name the new collection afterwards, the claimed one is
 * still claimed by the same claim, and the dead one is still dead.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AspectTaskRefsRenameTest {

    private static final String SVC_ROLE = "svc_aspect_task_refs_test";
    private static final String SVC_PASS = "svc_aspect_task_refs_test_pass";
    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINI = "minilm-l6-v2-384";
    private static final String WORKER = "worker-1";
    private static final ObjectMapper JSON = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository catalog;
    AspectRepository aspects;
    TupleRepository tuples;
    CombinedWriteService srcSvc;
    CombinedWriteService moveSvc;
    final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
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
        aspects = new AspectRepository(scope);
        tuples = new TupleRepository(scope,
            TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L));
        srcSvc = new CombinedWriteService(scope, catalog, new EmbedderRouter(new Fake(BGE, 768), "document"));
        moveSvc = new CombinedWriteService(scope, catalog, new EmbedderRouter(new Fake(MINI, 384), "document"));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ────────────────────────────────────────────────────────────

    static final class Fake implements Embedder {
        final String token;
        final int dim;

        Fake(String token, int dim) {
            this.token = token;
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
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

    <T> T su(Function<DSLContext, T> f) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return f.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    String freshTenant(String label) {
        return "atr-" + label + "-" + UUID.randomUUID();
    }

    /** A conformant knowledge collection name, unique per call. */
    String coll(String label) {
        return "knowledge__atr" + label + seq.incrementAndGet() + "__" + MINI + "__v1";
    }

    void register(String tenant, String collection) {
        scope.withTenant(tenant, ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            return null;
        });
    }

    String taskBody(String collection, String docId) {
        return "{\"collection\":\"" + collection + "\",\"source_path\":\"p/" + docId + ".md\",\"doc_id\":\"" + docId
            + "\",\"content_hash\":\"h-" + docId + "\"}";
    }

    byte[] outTask(String tenant, String collection, String docId) {
        return tuples.out(tenant, "aspects", Map.of("queue", "aspects"), Map.of(), taskBody(collection, docId), docId, null);
    }

    record Seeded(String claimedDoc, String deadDoc, String availableDoc, String claimId) {}

    /**
     * One claimed, one dead and one available task naming {@code collection}. Call it before any other task of
     * the tenant is queued: the claims take the oldest available task.
     */
    Seeded seedThree(String tenant, String collection, String prefix) {
        String claimedDoc = prefix + ".1";
        String deadDoc = prefix + ".2";
        String availableDoc = prefix + ".3";
        outTask(tenant, collection, claimedDoc);
        var claimed = tuples.inp(tenant, "aspects", Map.of("queue", "aspects"), WORKER, 600L).orElseThrow();
        outTask(tenant, collection, deadDoc);
        // another claimant: a claimant that already holds a live claim is handed that same claim back by inp
        for (int attempt = 0; attempt < 6; attempt++) {
            var c = tuples.inp(tenant, "aspects", Map.of("queue", "aspects"), "worker-failing", 600L).orElseThrow();
            tuples.nack(tenant, c.claimId(), "worker-failing");
        }
        outTask(tenant, collection, availableDoc);
        Seeded s = new Seeded(claimedDoc, deadDoc, availableDoc, claimed.claimId());
        assertThat(state(tenant, deadDoc).claimState()).as("seed: the dead task is dead").isEqualTo("dead");
        assertThat(state(tenant, claimedDoc).claimState()).as("seed: the claimed task is claimed").isEqualTo("claimed");
        assertThat(state(tenant, availableDoc).claimState()).as("seed: the available task is available").isNull();
        return s;
    }

    record TaskState(String collection, String claimState, String claimant, String claimId) {}

    TaskState state(String tenant, String docId) {
        var rows = su(ctx -> ctx.select(TUPLES.BODY, TUPLES.CLAIM_STATE, TUPLES.CLAIMANT, TUPLES.CLAIM_ID)
            .from(TUPLES)
            .where(TUPLES.TENANT_ID.eq(tenant)).and(TUPLES.SUBSPACE.eq("aspects")).and(TUPLES.CONSUMED_AT.isNull())
            .fetch());
        for (var r : rows) {
            try {
                JsonNode body = JSON.readTree(r.value1());
                if (docId.equals(body.path("doc_id").asText())) {
                    return new TaskState(body.path("collection").asText(), r.value2(), r.value3(), r.value4());
                }
            } catch (com.fasterxml.jackson.core.JsonProcessingException notJson) {
                // a body that is not JSON names no document
            }
        }
        throw new AssertionError("no unconsumed task for doc " + docId);
    }

    /** The collection every unconsumed task of the tenant names, by doc id. */
    Map<String, String> collectionsByDoc(String tenant) {
        var rows = su(ctx -> ctx.select(TUPLES.BODY).from(TUPLES)
            .where(TUPLES.TENANT_ID.eq(tenant)).and(TUPLES.SUBSPACE.eq("aspects")).and(TUPLES.CONSUMED_AT.isNull())
            .fetch(TUPLES.BODY));
        Map<String, String> out = new java.util.TreeMap<>();
        for (String b : rows) {
            try {
                JsonNode n = JSON.readTree(b);
                out.put(n.path("doc_id").asText(), n.path("collection").asText());
            } catch (com.fasterxml.jackson.core.JsonProcessingException notJson) {
                // a body that is not JSON names no document
            }
        }
        return out;
    }

    /** All three tasks name {@code collection}; the claim and the dead letter are intact. */
    void assertThreeNamed(String tenant, Seeded s, String collection) {
        TaskState claimed = state(tenant, s.claimedDoc());
        assertThat(claimed.collection()).as("claimed task names the new collection").isEqualTo(collection);
        assertThat(claimed.claimState()).as("claimed task is still claimed").isEqualTo("claimed");
        assertThat(claimed.claimant()).as("claimed task is still held by its worker").isEqualTo(WORKER);
        assertThat(claimed.claimId()).as("claimed task keeps its claim id").isEqualTo(s.claimId());
        TaskState dead = state(tenant, s.deadDoc());
        assertThat(dead.collection()).as("dead task names the new collection").isEqualTo(collection);
        assertThat(dead.claimState()).as("dead task is still dead").isEqualTo("dead");
        TaskState available = state(tenant, s.availableDoc());
        assertThat(available.collection()).as("available task names the new collection").isEqualTo(collection);
        assertThat(available.claimState()).as("available task is still unclaimed").isNull();
    }

    // ── scenario 17: rename, through both routes ───────────────────────────

    @Test
    void renameThroughTheCatalogRoute_rewritesAllThreeTasks_andReportsThem() {
        String tenant = freshTenant("catalog-rename");
        String a = coll("a");
        String b = coll("b");
        register(tenant, a);
        Seeded s = seedThree(tenant, a, "1.1");

        Map<String, Integer> counts = catalog.renameCollection(tenant, a, b);

        assertThat(counts).as("the rename's counts map reports the tasks under aspects_tasks").containsEntry("aspects_tasks", 3);
        assertThreeNamed(tenant, s, b);
    }

    @Test
    void renameThroughTheAspectQueueRoute_rewritesAllThreeTasks() {
        String tenant = freshTenant("queue-rename");
        String a = coll("a");
        String b = coll("b");
        register(tenant, a);
        register(tenant, b);
        Seeded s = seedThree(tenant, a, "1.2");

        aspects.renameQueueCollection(tenant, a, b);

        assertThreeNamed(tenant, s, b);
    }

    @Test
    void aWholeCollectionRehome_rewritesAllThreeTasks_andReportsThem() {
        String tenant = freshTenant("rehome");
        String a = coll("a");
        String b = coll("b");
        register(tenant, a);
        register(tenant, b);
        Seeded s = seedThree(tenant, a, "1.3");
        assertThat(catalog.rehomeStatus(tenant, a).remainingByTable())
            .as("before the re-home the status counts the tasks").containsEntry("aspects_tasks", 3);

        var result = catalog.rehomeCollection(tenant, a, b);

        assertThat(result.movedByTable()).containsEntry("aspects_tasks", 3);
        assertThat(result.status().remainingByTable()).as("nothing names the source afterwards")
            .doesNotContainKey("aspects_tasks");
        assertThreeNamed(tenant, s, b);
    }

    @Test
    void aReembedMoveOfOneDocument_rewritesThatDocumentsTaskOnly() {
        String tenant = freshTenant("move");
        String owner = "mv" + seq.incrementAndGet();
        String src = "knowledge__" + owner + "__" + BGE + "__v1";
        String tgt = "knowledge__" + owner + "__" + MINI + "__v1";
        register(tenant, src);
        register(tenant, tgt);
        // documents 1.4.1 (claimed task), 1.4.2 (dead task), 1.4.3 (available task) move; 1.4.4 stays
        for (int d = 1; d <= 4; d++) {
            seedDocument(tenant, src, "1.4." + d);
        }
        Seeded s = seedThree(tenant, src, "1.4");
        outTask(tenant, src, "1.4.4");

        for (int d = 1; d <= 3; d++) {
            move(tenant, src, tgt, "1.4." + d);
        }

        assertThreeNamed(tenant, s, tgt);
        assertThat(state(tenant, "1.4.4").collection()).as("the document that did not move keeps its task on the source")
            .isEqualTo(src);
    }

    // ── scenario 18: a collection that only a queued task names ────────────

    @Test
    void aCollectionWithOnlyAQueuedTask_namesAspectsTasksAsItsBlockingTable_andIsNotSweptAsAGhost() {
        String tenant = freshTenant("blocking");
        String held = coll("held");
        String empty = coll("empty");
        register(tenant, held);
        register(tenant, empty);
        outTask(tenant, held, "1.5.1");

        var blocker = catalog.blockingTable(tenant, held);
        assertThat(blocker).as("a queued task blocks").isPresent();
        assertThat(blocker.get().table()).isEqualTo("aspects_tasks");
        assertThat(blocker.get().auditOnly()).as("a task is content, not an audit breadcrumb").isFalse();
        assertThat(catalog.blockingTable(tenant, empty)).as("control: a collection naming nothing is not blocked").isEmpty();
        assertThat(catalog.collectionIsEmpty(tenant, held)).isFalse();

        var swept = catalog.sweepGhostsAndMarkDormant(tenant);

        assertThat(swept.ghostNames()).as("control: the sweep ran and reclaimed the empty registration").contains(empty);
        assertThat(swept.ghostNames()).as("the registry row a queued task names is not a ghost").doesNotContain(held);
        assertThat(registered(tenant, held)).isTrue();
    }

    @Test
    void renamingOntoARetiredCollectionAQueuedTaskNames_isRefusedNamingAspectsTasks() {
        String tenant = freshTenant("merge-refusal");
        String a = coll("a");
        String retired = coll("retired");
        register(tenant, a);
        register(tenant, retired);
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "elsewhere")
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(retired)).execute());
        outTask(tenant, retired, "1.6.1");

        assertThatThrownBy(() -> catalog.renameCollection(tenant, a, retired))
            .isInstanceOf(CatalogRepository.CollectionMergeRefused.class)
            .hasMessageContaining("aspects_tasks");
    }

    boolean registered(String tenant, String name) {
        return su(ctx -> ctx.fetchExists(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(tenant),
            CATALOG_COLLECTIONS.NAME.eq(name)));
    }

    // ── scenario 22: purge ─────────────────────────────────────────────────

    @Test
    void purgeDeletesTheCollectionsUnconsumedTasks_andLeavesAnotherCollectionsAlone() {
        String tenant = freshTenant("purge");
        String doomed = coll("doomed");
        String kept = coll("kept");
        register(tenant, doomed);
        register(tenant, kept);
        Seeded s = seedThree(tenant, doomed, "1.7");
        outTask(tenant, kept, "2.7.1");

        catalog.deleteCollection(tenant, doomed);

        Map<String, String> left = collectionsByDoc(tenant);
        assertThat(left).as("the purged collection's available, claimed and dead tasks are gone")
            .doesNotContainKeys(s.claimedDoc(), s.deadDoc(), s.availableDoc());
        assertThat(left).as("another collection's task is untouched").containsEntry("2.7.1", kept).hasSize(1);
    }

    // ── lock order: tuples first, then document_aspects ────────────────────

    /**
     * A claim-fenced aspect write holds its task's row lock and then writes document_aspects (E5). A rename
     * that took document_aspects first (its registry loop) and the task rows after would deadlock against it;
     * the helper locks the task rows before the loop touches document_aspects, so the rename waits at the
     * task row, the write finishes, and the rename completes.
     */
    @Test
    void aRenameConcurrentWithAFencedStyleWrite_waitsForTheTaskRow_andCompletesWithoutDeadlock() throws Exception {
        String tenant = freshTenant("lock-order");
        String owner = "lo" + seq.incrementAndGet();
        String a = "knowledge__" + owner + "__" + BGE + "__v1";
        String b = "knowledge__" + owner + "x__" + BGE + "__v1";
        register(tenant, a);
        seedDocument(tenant, a, "1.8.1");
        su(ctx -> ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
            .values(tenant, a, "p/1.8.1.md", OffsetDateTime.now(), "v1", "docling", "1.8.1", "file:///p/1.8.1.md")
            .execute());
        outTask(tenant, a, "1.8.1");
        var claimed = tuples.inp(tenant, "aspects", Map.of("queue", "aspects"), WORKER, 600L).orElseThrow();

        CountDownLatch locked = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            CompletableFuture<Void> writer = CompletableFuture.runAsync(() -> scope.withTenant(tenant, ctx -> {
                ctx.select(TUPLES.ID).from(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(tenant)).and(TUPLES.CLAIM_ID.eq(claimed.claimId())).forUpdate().fetch();
                locked.countDown();
                try {
                    assertThat(release.await(30, TimeUnit.SECONDS)).isTrue();
                } catch (InterruptedException e) {
                    throw new IllegalStateException(e);
                }
                ctx.update(DOCUMENT_ASPECTS).set(DOCUMENT_ASPECTS.MODEL_VERSION, "fenced")
                    .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant)).and(DOCUMENT_ASPECTS.COLLECTION.eq(a)).execute();
                return null;
            }), pool);
            assertThat(locked.await(30, TimeUnit.SECONDS)).as("the writer holds the task row").isTrue();

            CompletableFuture<Map<String, Integer>> rename =
                CompletableFuture.supplyAsync(() -> catalog.renameCollection(tenant, a, b), pool);
            assertThat(PgActivityProbe.waitsOnALock(pg, "%no key update%"))
                .as("the rename reached the task row lock and is waiting there, before any document_aspects statement")
                .isTrue();
            assertThat(rename.isDone()).as("the rename is blocked behind the writer").isFalse();
            release.countDown();

            writer.get(30, TimeUnit.SECONDS);
            Map<String, Integer> counts = rename.get(30, TimeUnit.SECONDS);

            assertThat(counts).containsEntry("aspects_tasks", 1);
            assertThat(state(tenant, "1.8.1").collection()).isEqualTo(b);
            assertThat(state(tenant, "1.8.1").claimId()).as("the claim survived").isEqualTo(claimed.claimId());
            List<String> versions = su(ctx -> ctx.select(DOCUMENT_ASPECTS.MODEL_VERSION).from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant)).and(DOCUMENT_ASPECTS.COLLECTION.eq(b))
                .fetch(DOCUMENT_ASPECTS.MODEL_VERSION));
            assertThat(versions).as("the fenced-style write landed, and the rename moved it").containsExactly("fenced");
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    // ── non-JSON bodies are skipped, not fatal ─────────────────────────────

    @Test
    void aTaskWhoseBodyIsNotJson_isSkippedByTheHelper_andDoesNotBreakTheRename() {
        String tenant = freshTenant("non-json");
        String a = coll("a");
        String b = coll("b");
        register(tenant, a);
        tuples.out(tenant, "aspects", Map.of("queue", "aspects"), Map.of(), "this is not json", "1.9.1", null);
        outTask(tenant, a, "1.9.2");

        Map<String, Integer> counts = catalog.renameCollection(tenant, a, b);

        assertThat(counts).containsEntry("aspects_tasks", 1);
        assertThat(state(tenant, "1.9.2").collection()).isEqualTo(b);
        List<String> bodies = su(ctx -> ctx.select(TUPLES.BODY).from(TUPLES)
            .where(TUPLES.TENANT_ID.eq(tenant)).and(TUPLES.SUBSPACE.eq("aspects")).fetch(TUPLES.BODY));
        assertThat(bodies).as("the body that is not JSON is left exactly as it was").contains("this is not json");
    }

    // ── a re-embed move's fixtures (the combined write, as DocumentMoveTest) ─

    void seedDocument(String tenant, String collection, String docId) {
        catalog.upsertDocument(tenant, Map.of("tumbler", docId, "title", "title-" + docId, "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", collection, "chunk_count", 0));
        String text = "document " + docId + " chunk lorem ipsum";
        String chash = Chash.ofText(text).toHex();
        var r = srcSvc.writeManyCombined(tenant, collection,
            List.of(Map.of("chash", chash, "text", text, "metadata", Map.of())),
            List.of(Map.of("doc_id", docId, "rows", List.of(Map.of("position", 0, "chash", chash, "chunk_index", 0)))),
            null, false, false);
        assertThat((List<?>) r.response().get("failed_doc_ids")).as("client write of " + docId).isEmpty();
    }

    void move(String tenant, String src, String tgt, String docId) {
        List<Map<String, Object>> rows = new ArrayList<>();
        List<String> order = new ArrayList<>();
        for (Map<String, Object> m : catalog.getManifest(tenant, docId)) {
            String chash = (String) m.get("chash");
            int position = ((Number) m.get("position")).intValue();
            order.add(chash);
            rows.add(Map.of("position", position, "chash", chash, "chunk_index", position));
        }
        List<byte[]> keys = order.stream().map(h -> Chash.fromHex(h).toBytes()).toList();
        List<Map<String, Object>> chunks = new ArrayList<>();
        su(ctx -> ctx.select(CHUNKS.CHASH, CHUNKS.CHUNK_TEXT).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(src)).and(CHUNKS.CHASH.in(keys)).fetch())
            .forEach(r -> chunks.add(Map.of("chash", HexFormat.of().formatHex(r.value1()), "text", r.value2(),
                "metadata", Map.of())));
        var response = moveSvc.moveDocsCombined(tenant, src, tgt, chunks,
            List.of(Map.of("doc_id", docId, "rows", rows)), Map.of(docId, order)).response();
        assertThat((List<?>) response.get("failed_doc_ids")).as("move of " + docId).isEmpty();
    }
}
