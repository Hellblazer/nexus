// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.RacedEmbedActivity;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-222 Phase 0 (bead nexus-ulrjq) — the raced-embed counter on the
 * combined-write per-doc path ({@code CatalogRepository#upsertManifestChunkVectors},
 * fed by {@link CombinedWriteService}'s own existence-partition).
 *
 * <p>Mirrors {@code PgVectorRacedEmbedCounterTest}'s interleaving shape but for the
 * combined-write path: {@link CombinedWriteService} had no comparable test-only
 * interleaving seam before this bead (unlike {@code PgVectorRepository}'s
 * pre-existing {@code afterExistencePartitionHookForTests}, bead nexus-f0r8p.4), so
 * this bead adds {@link CombinedWriteService#setAfterExistencePartitionHookForTests}
 * as its direct sibling — same shape, same window (after the existence-partition
 * transaction commits, before the embed call).
 *
 * <p>Hermetic: Testcontainers PG, mirrors {@code CombinedWriteRepositoryTest}'s
 * fixture (small dedicated collection, {@code minilm-l6-v2-384} fake embedder).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CombinedWriteRacedEmbedCounterTest {

    private static final String SVC_ROLE = "svc_cw_racedembed_test";
    private static final String SVC_PASS = "svc_cw_racedembed_test_pass";
    private static final String TENANT   = "cw-racedembed-tenant";
    private static final String COLLECTION = "code__cwracedembed__minilm-l6-v2-384__v1";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private CatalogRepository repo;
    private CountingFakeEmbedder embedder;

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
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        embedder = new CountingFakeEmbedder();

        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void registerDoc(String tumbler) {
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "cw-racedembed-" + tumbler,
            "content_type", "code", "corpus", "code",
            "physical_collection", COLLECTION, "chunk_count", 0));
    }

    private static Map<String, Object> chunk(String chash, String text) {
        return Map.of("chash", chash, "text", text, "metadata", Map.of());
    }

    private static Map<String, Object> row(int position, String chash) {
        return Map.of("position", position, "chash", chash, "chunk_index", position);
    }

    private static Map<String, Object> doc(String docId, List<Map<String, Object>> rows) {
        return Map.of("doc_id", docId, "rows", rows);
    }

    @Test
    void overlappingConcurrentCombinedWrites_racedEqualsTheOverlap() throws Exception {
        String chashRaced = String.format("%064x", 0x4EAD4L);
        String text = "combined-write raced-embed overlap fixture text";
        String docA = "cwr.a1";
        String docB = "cwr.a2";
        registerDoc(docA);
        registerDoc(docB);

        CombinedWriteService writer1 = new CombinedWriteService(
                tenantScope, repo, new EmbedderRouter(embedder, "document"));
        CombinedWriteService writer2 = new CombinedWriteService(
                tenantScope, repo, new EmbedderRouter(embedder, "document"));

        long racedBefore = RacedEmbedActivity.total();

        CountDownLatch paused = new CountDownLatch(1);
        CountDownLatch resume = new CountDownLatch(1);
        AtomicBoolean hookFired = new AtomicBoolean(false);
        writer1.setAfterExistencePartitionHookForTests(() -> {
            hookFired.set(true);
            paused.countDown();
            try {
                boolean released = resume.await(30, TimeUnit.SECONDS);
                if (!released) {
                    throw new IllegalStateException("test timed out waiting to be resumed");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException(e);
            }
        });

        AtomicReference<Throwable> worker1Error = new AtomicReference<>();
        Thread worker1 = new Thread(() -> {
            try {
                writer1.writeManyCombined(TENANT, COLLECTION,
                        List.of(chunk(chashRaced, text)),
                        List.of(doc(docA, List.of(row(0, chashRaced)))),
                        null, false, false);
            } catch (Throwable t) {
                worker1Error.set(t);
            }
        }, "cw-raced-embed-writer-1");
        worker1.start();

        try {
            assertThat(paused.await(30, TimeUnit.SECONDS))
                .as("writer 1 must reach the post-existence-partition pause point")
                .isTrue();

            // Writer 2 runs a FULL, unhooked combined write of the SAME chash (a
            // different doc referencing it — RDR-108 shared-chash semantics) and
            // commits — the genuine winner.
            writer2.writeManyCombined(TENANT, COLLECTION,
                    List.of(chunk(chashRaced, text)),
                    List.of(doc(docB, List.of(row(0, chashRaced)))),
                    null, false, false);
        } finally {
            resume.countDown();
        }

        worker1.join(TimeUnit.SECONDS.toMillis(30));
        assertThat(worker1.isAlive()).as("writer 1 must finish within the join budget").isFalse();
        assertThat(worker1Error.get())
            .as("the raced writer must complete without throwing — a raced embed is not an error")
            .isNull();
        assertThat(hookFired.get()).as("the interleaving seam must actually have fired").isTrue();

        long racedAfter = RacedEmbedActivity.total();
        assertThat(racedAfter - racedBefore)
            .as("exactly the one overlapping chash both combined writes raced on")
            .isEqualTo(1L);
    }

    @Test
    void nonOverlappingConcurrentCombinedWrites_racedStaysZero() {
        String chashA = String.format("%064x", 0x5EAD5L);
        String chashB = String.format("%064x", 0x6EAD6L);
        String text = "combined-write non-overlapping control fixture text";
        String docC = "cwr.b1";
        String docD = "cwr.b2";
        registerDoc(docC);
        registerDoc(docD);

        CombinedWriteService writerA = new CombinedWriteService(
                tenantScope, repo, new EmbedderRouter(embedder, "document"));
        CombinedWriteService writerB = new CombinedWriteService(
                tenantScope, repo, new EmbedderRouter(embedder, "document"));

        long racedBefore = RacedEmbedActivity.total();

        writerA.writeManyCombined(TENANT, COLLECTION,
                List.of(chunk(chashA, text)), List.of(doc(docC, List.of(row(0, chashA)))),
                null, false, false);
        writerB.writeManyCombined(TENANT, COLLECTION,
                List.of(chunk(chashB, text)), List.of(doc(docD, List.of(row(0, chashB)))),
                null, false, false);

        long racedAfter = RacedEmbedActivity.total();
        assertThat(racedAfter - racedBefore)
            .as("two disjoint chashes never race each other")
            .isEqualTo(0L);
    }

    /** Mirrors CombinedWriteRepositoryTest's own fake embedder (minilm-l6-v2-384). */
    static final class CountingFakeEmbedder implements Embedder {
        final AtomicInteger calls = new AtomicInteger();

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
