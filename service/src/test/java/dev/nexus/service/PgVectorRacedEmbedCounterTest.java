// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.RacedEmbedActivity;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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
 * RDR-222 Phase 0 (bead nexus-ulrjq) — the raced-embed counter on {@link
 * PgVectorRepository#upsertChunksInternal}'s final {@code INSERT ... ON CONFLICT}.
 *
 * <p><strong>The race (M-b).</strong> {@link PgVectorRepository#resolveNeedEmbedIdx}'s
 * existence partition and the final INSERT run in TWO SEPARATE, independently
 * committed transactions, with the (uninstrumented, network-bound in production)
 * embed call in between (RDR-181 Technical Design). A concurrent second writer that
 * commits the SAME chash in that window makes this request's own INSERT hit
 * {@code ON CONFLICT} for a chash its OWN partition believed absent — a duplicate
 * embed. This suite forces the interleaving deterministically with the
 * {@code afterExistencePartitionHookForTests} seam (bead nexus-f0r8p.4), on TWO
 * separate {@link PgVectorRepository} instances sharing one {@link TenantScope}/
 * database — each instance owns its own hook field, so the second writer's own
 * (unhooked) call cannot be paused by the first writer's installed hook.
 *
 * <p><strong>Fix round (bead nexus-ulrjq, finding 2).</strong> {@code
 * contentDivergentChash_racedStaysZero} and
 * {@code zeroRowRerouteChash_racedStaysZeroEvenThroughOnConflict} prove the
 * ORIGINAL-absentee guard actually distinguishes those two classes from a
 * genuine race, not merely that they happen to read zero by construction — see
 * each test's own javadoc. The content-divergent case needs no concurrency at
 * all (a content-divergent chash's final INSERT conflicts DETERMINISTICALLY);
 * the zero-row-reroute case needs a SECOND interleaving seam,
 * {@code afterNeedEmbedResolvedHookForTests} (fires after {@code
 * resolveNeedEmbedIdx} returns, before embedding), to force that chash's own
 * final INSERT through a REAL {@code ON CONFLICT}.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, {@code nexus_svc}-shaped plain
 * LOGIN NOSUPERUSER role, PER_CLASS.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorRacedEmbedCounterTest {

    private static final String SVC_ROLE = "svc_racedembed_test";
    private static final String SVC_PASS = "svc_racedembed_test_pass";
    private static final String TENANT   = "racedembed-tenant";
    private static final String COLLECTION = "code__racedembed__voyage-code-3__v1";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private CountingEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        // >=2 so the paused writer's held connection and the racing writer's own
        // connection can be open at the same time without contending for one slot.
        cfg.setMaximumPoolSize(4);
        cfg.setConnectionTimeout(10_000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        embedder = new CountingEmbedder(1024);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void overlappingConcurrentWriters_racedEqualsTheOverlap() throws Exception {
        String chashRaced = String.format("%064x", 0x1EAD1L);
        String text = "raced-embed overlap fixture text";

        // Two SEPARATE repository instances over the SAME tenantScope/database: the
        // interleaving hook is a per-instance field, so writer 2's own (unhooked)
        // call is never blocked by writer 1's installed hook.
        PgVectorRepository writer1 = new PgVectorRepository(tenantScope, embedder, embedder);
        PgVectorRepository writer2 = new PgVectorRepository(tenantScope, embedder, embedder);

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
                writer1.upsertChunks(TENANT, COLLECTION, List.of(chashRaced), List.of(text),
                        List.of(Map.of("writer", "1")));
            } catch (Throwable t) {
                worker1Error.set(t);
            }
        }, "raced-embed-writer-1");
        worker1.start();

        try {
            // Writer 1 reaches the paused point: its existence partition has already
            // confirmed chashRaced ABSENT.
            assertThat(paused.await(30, TimeUnit.SECONDS))
                .as("writer 1 must reach the post-existence-partition pause point")
                .isTrue();

            // While writer 1 is paused, writer 2 runs a FULL, unhooked upsert of the
            // SAME chash and commits — the genuine winner (its own INSERT is a fresh
            // insert, xmax = 0, never raced).
            writer2.upsertChunks(TENANT, COLLECTION, List.of(chashRaced), List.of(text),
                    List.of(Map.of("writer", "2")));
        } finally {
            // Release writer 1: its own INSERT now hits ON CONFLICT against writer 2's
            // already-committed row for a chash its OWN partition believed absent.
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
            .as("exactly the one overlapping chash both writers raced on")
            .isEqualTo(1L);
    }

    @Test
    void nonOverlappingConcurrentWriters_racedStaysZero() {
        String chashA = String.format("%064x", 0x2EAD2L);
        String chashB = String.format("%064x", 0x3EAD3L);
        String text = "non-overlapping control fixture text";

        PgVectorRepository writerA = new PgVectorRepository(tenantScope, embedder, embedder);
        PgVectorRepository writerB = new PgVectorRepository(tenantScope, embedder, embedder);

        long racedBefore = RacedEmbedActivity.total();

        writerA.upsertChunks(TENANT, COLLECTION, List.of(chashA), List.of(text), List.of(Map.of("k", "a")));
        writerB.upsertChunks(TENANT, COLLECTION, List.of(chashB), List.of(text), List.of(Map.of("k", "b")));

        long racedAfter = RacedEmbedActivity.total();
        assertThat(racedAfter - racedBefore)
            .as("two disjoint chashes never race each other")
            .isEqualTo(0L);
    }

    @Test
    void contentDivergentChash_racedStaysZero() {
        String chash = String.format("%064x", 0x8EAD8L);
        String textOriginal = "content-divergent original text";
        String textDivergent = "content-divergent NEW text — deliberately different";

        PgVectorRepository writer = new PgVectorRepository(tenantScope, embedder, embedder);

        // Seed: chash exists with textOriginal (a real insert, no race).
        writer.upsertChunks(TENANT, COLLECTION, List.of(chash), List.of(textOriginal), List.of(Map.of("v", "1")));

        long racedBefore = RacedEmbedActivity.total();

        // Re-upsert the SAME chash with DIFFERENT text, single-threaded, no
        // concurrency anywhere: resolveNeedEmbedIdx's content-divergence guard
        // (present, but stored text != incoming text) routes this straight to
        // need-embed, so its final INSERT hits ON CONFLICT DETERMINISTICALLY —
        // the chash already exists, by construction, before this call even
        // starts. Must not count as raced: it was never in the
        // original-absentee set (see NeedEmbedResolution's javadoc).
        writer.upsertChunks(TENANT, COLLECTION, List.of(chash), List.of(textDivergent), List.of(Map.of("v", "2")));

        long racedAfter = RacedEmbedActivity.total();
        assertThat(racedAfter - racedBefore)
            .as("a content-divergent re-upsert deterministically hits ON CONFLICT but is not a race")
            .isEqualTo(0L);
    }

    @Test
    void zeroRowRerouteChash_racedStaysZeroEvenThroughOnConflict() throws Exception {
        String chash = String.format("%064x", 0x9EAD9L);
        String text = "zero-row-reroute fixture text";

        PgVectorRepository victim = new PgVectorRepository(tenantScope, embedder, embedder);
        PgVectorRepository recreator = new PgVectorRepository(tenantScope, embedder, embedder);

        // Seed: chash exists with `text` (a real insert, no race).
        victim.upsertChunks(TENANT, COLLECTION, List.of(chash), List.of(text), List.of(Map.of("v", "1")));

        long racedBefore = RacedEmbedActivity.total();

        CountDownLatch pausedAtPartition = new CountDownLatch(1);
        CountDownLatch resumeAfterDelete = new CountDownLatch(1);
        CountDownLatch pausedAfterResolve = new CountDownLatch(1);
        CountDownLatch resumeAfterRecreate = new CountDownLatch(1);
        AtomicBoolean partitionHookFired = new AtomicBoolean(false);
        AtomicBoolean resolvedHookFired = new AtomicBoolean(false);

        // Hook 1: pause AFTER the existence SELECT confirms `chash` present with
        // IDENTICAL text (the have-vector, unchanged-text branch) and BEFORE the
        // have-vector UPDATE runs — the exact PgVectorEmbedSkipGcRaceTest window.
        victim.setAfterExistencePartitionHookForTests(() -> {
            partitionHookFired.set(true);
            pausedAtPartition.countDown();
            awaitOrFail(resumeAfterDelete, "delete");
        });

        // Hook 2 (new this fix round): pause AFTER resolveNeedEmbedIdx returns —
        // the have-vector UPDATE already ran against the now-deleted row,
        // affected 0 rows, and self-healed `chash` into need-embed via the
        // zero-row reroute (NOT the original-absentee set) — and BEFORE
        // embedding/the final INSERT.
        victim.setAfterNeedEmbedResolvedHookForTests(() -> {
            resolvedHookFired.set(true);
            pausedAfterResolve.countDown();
            awaitOrFail(resumeAfterRecreate, "recreate");
        });

        AtomicReference<Throwable> victimError = new AtomicReference<>();
        Thread victimThread = new Thread(() -> {
            try {
                victim.upsertChunks(TENANT, COLLECTION, List.of(chash), List.of(text), List.of(Map.of("v", "2")));
            } catch (Throwable t) {
                victimError.set(t);
            }
        }, "zero-row-reroute-victim");
        victimThread.start();

        try {
            assertThat(pausedAtPartition.await(30, TimeUnit.SECONDS))
                .as("victim must reach the post-existence-partition pause point")
                .isTrue();

            // Concurrent GC-like delete of `chash` while the victim is paused.
            int deleted = victim.delete(TENANT, COLLECTION, List.of(chash));
            assertThat(deleted).as("the concurrent delete removes exactly one row").isEqualTo(1);
        } finally {
            resumeAfterDelete.countDown();
        }

        try {
            assertThat(pausedAfterResolve.await(30, TimeUnit.SECONDS))
                .as("victim must reach the post-resolution pause point (zero-row reroute confirmed)")
                .isTrue();

            // Recreate `chash` from a SEPARATE writer while the victim is paused a
            // SECOND time — so the victim's own final INSERT, moments later, hits
            // a REAL ON CONFLICT for a chash that was NEVER in its
            // original-absentee set (present-then-deleted, not absent, at the
            // existence SELECT).
            recreator.upsertChunks(TENANT, COLLECTION, List.of(chash), List.of(text), List.of(Map.of("v", "3")));
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

    /** Embedder stub that counts invocations; mirrors PgVectorEmbedSkipGcRaceTest's. */
    private static final class CountingEmbedder implements Embedder {
        private final int dim;
        private final AtomicInteger calls = new AtomicInteger();

        CountingEmbedder(int dim) {
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            int call = calls.incrementAndGet();
            List<float[]> out = new ArrayList<>(texts.size());
            for (String ignored : texts) {
                float[] v = new float[dim];
                v[0] = call;
                out.add(v);
            }
            return out;
        }
    }
}
