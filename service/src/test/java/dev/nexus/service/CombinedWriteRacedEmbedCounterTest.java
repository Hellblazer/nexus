// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
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
 * this bead adds two direct siblings, mirroring both of {@code PgVectorRepository}'s
 * hooks: {@link CombinedWriteService#setAfterExistencePartitionHookForTests} (fires
 * INSIDE the existence-partition transaction, before the have-vector UPDATE) and
 * {@link CombinedWriteService#setAfterNeedEmbedResolvedHookForTests} (fires after
 * that transaction commits, before the embed call).
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
        writer1.setAfterNeedEmbedResolvedHookForTests(() -> {
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

    @Test
    void sameCallTwoDocsSharedAbsentChash_racedStaysZero() {
        String chashShared = String.format("%064x", 0x7EAD7L);
        String text = "same-call shared chash, two docs, no concurrency";
        String docE = "cwr.c1";
        String docF = "cwr.c2";
        registerDoc(docE);
        registerDoc(docF);

        CombinedWriteService svc = new CombinedWriteService(
                tenantScope, repo, new EmbedderRouter(embedder, "document"));

        long racedBefore = RacedEmbedActivity.total();

        // ONE writeManyCombined call: `chunks` names chashShared ONCE (dedup'd
        // by chash, Phase 1), but BOTH docs' manifest rows reference it
        // (RDR-108 shared-chash fan-out). The existence partition runs ONCE
        // for the whole call and resolves chashShared as absent once; the
        // per-doc dispatch then writes it via docE's transaction first, and
        // docF's transaction hits ON CONFLICT against docE's own
        // just-committed row. Code-review CRITICAL (nexus-ulrjq fix round):
        // that is NOT a race with another writer, it is this call's own
        // fan-out — before the fix, docF's write counted raced=1 here with
        // ZERO concurrency anywhere.
        svc.writeManyCombined(TENANT, COLLECTION,
                List.of(chunk(chashShared, text)),
                List.of(doc(docE, List.of(row(0, chashShared))), doc(docF, List.of(row(0, chashShared)))),
                null, false, false);

        long racedAfter = RacedEmbedActivity.total();
        assertThat(racedAfter - racedBefore)
            .as("one call's own two docs sharing a chash is not a race with another writer")
            .isEqualTo(0L);
    }

    @Test
    void contentDivergentChash_racedStaysZero() {
        String chash = String.format("%064x", 0xAEADAL);
        String textOriginal = "combined-write content-divergent original text";
        String textDivergent = "combined-write content-divergent NEW text";
        String docG = "cwr.d1";
        String docH = "cwr.d2";
        registerDoc(docG);
        registerDoc(docH);

        CombinedWriteService svc = new CombinedWriteService(
                tenantScope, repo, new EmbedderRouter(embedder, "document"));

        // Seed: chash exists with textOriginal (a real write, no race).
        svc.writeManyCombined(TENANT, COLLECTION, List.of(chunk(chash, textOriginal)),
                List.of(doc(docG, List.of(row(0, chash)))), null, false, false);

        long racedBefore = RacedEmbedActivity.total();

        // Re-send the SAME chash with DIFFERENT text via a DIFFERENT doc, one
        // call, no concurrency anywhere: CombinedWriteService's phase-2a
        // routes this to `need` because stored text != incoming (content
        // divergent), so the per-doc INSERT hits ON CONFLICT
        // DETERMINISTICALLY. Must not count as raced.
        svc.writeManyCombined(TENANT, COLLECTION, List.of(chunk(chash, textDivergent)),
                List.of(doc(docH, List.of(row(0, chash)))), null, false, false);

        long racedAfter = RacedEmbedActivity.total();
        assertThat(racedAfter - racedBefore)
            .as("a content-divergent re-send deterministically hits ON CONFLICT but is not a race")
            .isEqualTo(0L);
    }

    @Test
    void zeroRowRerouteChash_racedStaysZeroEvenThroughOnConflict() throws Exception {
        String chash = String.format("%064x", 0xBEADBL);
        String text = "combined-write zero-row-reroute fixture text";
        String docJ = "cwr.e2";
        String docK = "cwr.e3";
        registerDoc(docJ);
        registerDoc(docK);

        CombinedWriteService victim = new CombinedWriteService(
                tenantScope, repo, new EmbedderRouter(embedder, "document"));
        CombinedWriteService recreator = new CombinedWriteService(
                tenantScope, repo, new EmbedderRouter(embedder, "document"));

        // Seed the CHUNK ROW ONLY, via raw SQL, deliberately WITHOUT any
        // manifest reference — mirrors PgVectorEmbedSkipGcRaceTest's own
        // fixture-correction rationale: RDR-191 F10c's anti-join FK
        // (fk_catalog_chunks_chunk) refuses to delete a chunk any LIVE
        // manifest row still references, so the concurrent delete below
        // needs chash to be a genuine (transient) orphan at that moment. A
        // combined write's own per-doc transaction always commits the chunk
        // row AND its manifest reference together (design memo §0), so the
        // ordinary API cannot produce this shape — only a direct chunk-table
        // insert can seed it unreferenced.
        seedChunk(chash, text);

        long racedBefore = RacedEmbedActivity.total();

        CountDownLatch pausedAtPartition = new CountDownLatch(1);
        CountDownLatch resumeAfterDelete = new CountDownLatch(1);
        CountDownLatch pausedAfterResolve = new CountDownLatch(1);
        CountDownLatch resumeAfterRecreate = new CountDownLatch(1);
        AtomicBoolean partitionHookFired = new AtomicBoolean(false);
        AtomicBoolean resolvedHookFired = new AtomicBoolean(false);

        // Hook 1 (new this fix round): pause INSIDE Phase 2a's
        // existence-partition transaction, AFTER the existence SELECT
        // resolves `chash` present with IDENTICAL text (the metadataOnly
        // branch) and BEFORE batchUpdateMetadata's have-vector UPDATE runs.
        victim.setAfterExistencePartitionHookForTests(() -> {
            partitionHookFired.set(true);
            pausedAtPartition.countDown();
            awaitOrFail(resumeAfterDelete, "delete");
        });
        // Hook 2: pause AFTER that transaction commits — the have-vector
        // UPDATE already ran against the now-deleted row, affected 0 rows,
        // and self-healed `chash` into `need` via the zero-row reroute (NOT
        // the original-absentee set) — and BEFORE the embed call.
        victim.setAfterNeedEmbedResolvedHookForTests(() -> {
            resolvedHookFired.set(true);
            pausedAfterResolve.countDown();
            awaitOrFail(resumeAfterRecreate, "recreate");
        });

        AtomicReference<Throwable> victimError = new AtomicReference<>();
        Thread victimThread = new Thread(() -> {
            try {
                // SAME text as the seed -> takes the metadataOnly branch in
                // Phase 2a, via docJ (a different doc than the seed's docI,
                // both referencing the shared chash — RDR-108 fan-out, same
                // as sameCallTwoDocsSharedAbsentChash's shape but across two
                // separate calls here so the interleaving hooks fire exactly
                // once each for THIS call's own transaction).
                victim.writeManyCombined(TENANT, COLLECTION, List.of(chunk(chash, text)),
                        List.of(doc(docJ, List.of(row(0, chash)))), null, false, false);
            } catch (Throwable t) {
                victimError.set(t);
            }
        }, "cw-zero-row-reroute-victim");
        victimThread.start();

        try {
            assertThat(pausedAtPartition.await(30, TimeUnit.SECONDS))
                .as("victim must reach the post-existence-partition pause point")
                .isTrue();
            deleteChunk(chash);
        } finally {
            resumeAfterDelete.countDown();
        }

        try {
            assertThat(pausedAfterResolve.await(30, TimeUnit.SECONDS))
                .as("victim must reach the post-resolution pause point (zero-row reroute confirmed)")
                .isTrue();

            // Recreate `chash` from a SEPARATE writer/doc while victim is
            // paused a SECOND time — so victim's own per-doc INSERT, moments
            // later, hits a REAL ON CONFLICT for a chash that was NEVER in
            // its original-absentee set (present-then-deleted, not absent,
            // at victim's own existence SELECT).
            recreator.writeManyCombined(TENANT, COLLECTION, List.of(chunk(chash, text)),
                    List.of(doc(docK, List.of(row(0, chash)))), null, false, false);
        } finally {
            resumeAfterRecreate.countDown();
        }

        victimThread.join(TimeUnit.SECONDS.toMillis(30));
        assertThat(victimThread.isAlive()).as("victim must finish within the join budget").isFalse();
        assertThat(victimError.get())
            .as("the victim must complete without throwing")
            .isNull();
        assertThat(partitionHookFired.get()).as("the first interleaving seam must have fired").isTrue();
        assertThat(resolvedHookFired.get()).as("the second interleaving seam must have fired").isTrue();

        long racedAfter = RacedEmbedActivity.total();
        assertThat(racedAfter - racedBefore)
            .as("a zero-row-reroute chash hitting a REAL ON CONFLICT must not count as raced")
            .isEqualTo(0L);
    }

    /** Raw-SQL chunk-row-only seed, mirrors CombinedWriteRepositoryTest's seedChunk384
     *  (384-dim, this suite's own collection model) — deliberately bypasses the
     *  combined-write API so the row carries NO manifest reference yet. */
    private void seedChunk(String hexChash, String text) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.setTenant(su, TenantScope.DEFAULT_TENANT_GUC, TENANT, false);
            String zeroVec = "[" + "0,".repeat(383) + "0]";
            var ps = su.prepareStatement(
                "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME
                + " (tenant_id, collection, chash, embedding_model, chunk_text, " + DimTables.embeddingColumn(384) + ")"
                + " VALUES (?, ?, ?, 'minilm-l6-v2-384', ?, ?::nexus.vector)"
                + " ON CONFLICT (tenant_id, collection, chash, embedding_model) DO NOTHING");
            ps.setString(1, TENANT);
            ps.setString(2, COLLECTION);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(hexChash));
            ps.setString(4, text);
            ps.setString(5, zeroVec);
            ps.executeUpdate();
        }
    }

    private void deleteChunk(String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ps = su.prepareStatement(
                "DELETE FROM " + DimTables.CHUNKS_TABLE_NAME + " WHERE tenant_id = ? AND collection = ? AND chash = ?");
            ps.setString(1, TENANT);
            ps.setString(2, COLLECTION);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(hexChash));
            int n = ps.executeUpdate();
            assertThat(n).as("the concurrent delete removes exactly one row").isEqualTo(1);
        }
    }

    private static void awaitOrFail(CountDownLatch latch, String label) {
        try {
            boolean released = latch.await(30, TimeUnit.SECONDS);
            if (!released) {
                throw new IllegalStateException("test timed out waiting to be resumed (" + label + ")");
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
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
