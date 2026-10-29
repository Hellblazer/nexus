// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.NexusService;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-5r2sc.2.5 (RDR-235 E5): the claim-fenced {@code document_aspects} upsert. A write that names
 * {@code claim_id} and {@code claimant} looks the claim up inside its own transaction, with the row lock,
 * before it writes; a claim that is not live answers {@code ClaimNotFound}, one held by another claimant
 * {@code ClaimOwnership}, and a claim on some other task {@code ClaimTaskMismatch}. Each refusal writes
 * nothing, and each is the answer {@code ack} gives for the same claim (Test Plan scenarios 2 to 5).
 *
 * <p>Lapses are made with a direct UPDATE of {@code lease_until} (the repository only ever writes
 * "now"), so no test sleeps for a lease, except the race test, which needs a real database-clock lapse
 * while a row lock is held (an UPDATE would block on that lock).
 */
class AspectUpsertClaimFenceRepoTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String SUBSPACE = "aspects";
    private static final Map<String, String> PATTERN = Map.of("queue", "aspects");

    private AspectRepository aspects;
    private TupleRepository tuples;

    @BeforeAll
    void wire() {
        aspects = new AspectRepository(tenantScope);
        tuples = new TupleRepository(tenantScope,
                TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L));
    }

    /** One queue per tenant: every test starts from an empty one, so a claim takes the task it seeded. */
    @BeforeEach
    void emptyQueue() throws Exception {
        admin(ctx -> ctx.deleteFrom(TUPLES)
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(SUBSPACE))).execute());
    }

    @AfterEach
    void clearSeams() {
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    private <T> T admin(Function<DSLContext, T> f) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return f.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    /** Seeds the task the worker bead's producer would: {@code queue=aspects}, nonce = the doc_id. */
    private void seedTask(Fx f) throws Exception {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("source_path", "src/" + f.docId() + ".pdf");
        body.put("doc_id", f.docId());
        body.put("content_hash", "h1");
        tuples.out(TENANT, SUBSPACE, PATTERN, Map.of(), JSON.writeValueAsString(body), f.docId(), null);
    }

    private TupleRepository.ClaimedTuple claim(String claimant) {
        var c = tuples.inp(TENANT, SUBSPACE, PATTERN, claimant, (Long) 600L);
        assertThat(c).as("%s must find a claimable task", claimant).isPresent();
        return c.get();
    }

    private void lapse(String claimId) throws Exception {
        int n = admin(ctx -> ctx.update(TUPLES)
                .set(TUPLES.LEASE_UNTIL, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(5))
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.CLAIM_ID.eq(claimId)))
                .execute());
        assertThat(n).as("the lapse touched the claimed row").isEqualTo(1);
    }

    private Map<String, Object> aspectBody(Fx f, String problem, String claimId, String claimant) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", f.collection());
        b.put("source_path", "src/" + f.docId() + ".pdf");
        b.put("problem_formulation", problem);
        b.put("proposed_method", "m");
        b.put("experimental_results", "r");
        b.put("confidence", 0.9);
        b.put("extracted_at", "2026-10-29T00:00:00.000000Z");
        b.put("model_version", "v1");
        b.put("extractor_name", "scholarly-paper");
        b.put("doc_id", f.docId());
        if (claimId != null) b.put("claim_id", claimId);
        if (claimant != null) b.put("claimant", claimant);
        return b;
    }

    private String problemOf(Fx f) {
        return aspects.getAspect(TENANT, f.collection(), "src/" + f.docId() + ".pdf")
                .map(m -> (String) m.get("problem_formulation")).orElse(null);
    }

    // ── scenario 2: a lapse, then a second worker claims ─────────────────────

    @Test
    void lapsedLeaseRetakenByAnotherWorker_firstWorkersWriteIs404AndChangesNoRow_secondWorkersResultStands()
            throws Exception {
        Fx f = fixture("lt");
        seedTask(f);
        var a = claim("wA:0");
        lapse(a.claimId());
        var b = claim("wB:0");
        assertThat(b.tuple().attempts()).as("the lapse counted an attempt").isEqualTo(1);

        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "from A", a.claimId(), "wA:0")))
                .isInstanceOf(ClaimNotFoundException.class);
        assertThat(problemOf(f)).as("A's refused write changed no row").isNull();

        assertThat(aspects.upsertAspect(TENANT, aspectBody(f, "from B", b.claimId(), "wB:0"))).isPositive();
        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "from A", a.claimId(), "wA:0")))
                .isInstanceOf(ClaimNotFoundException.class);
        assertThat(problemOf(f)).as("the row holds the second worker's result").isEqualTo("from B");

        assertThatThrownBy(() -> tuples.ack(TENANT, a.claimId(), "wA:0"))
                .as("A's ack answers what A's write answered")
                .isInstanceOf(ClaimNotFoundException.class);
        tuples.ack(TENANT, b.claimId(), "wB:0");
    }

    // ── scenario 3: a lapse nobody re-took ───────────────────────────────────

    @Test
    void lapsedLeaseNobodyRetook_writeIs404ChangesNoRow_ackIs404_taskIsRetakenWithAnAttemptCounted()
            throws Exception {
        Fx f = fixture("ln");
        seedTask(f);
        var a = claim("wA:0");
        lapse(a.claimId());

        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "late", a.claimId(), "wA:0")))
                .isInstanceOf(ClaimNotFoundException.class);
        assertThat(problemOf(f)).isNull();
        assertThatThrownBy(() -> tuples.ack(TENANT, a.claimId(), "wA:0"))
                .isInstanceOf(ClaimNotFoundException.class);

        var again = claim("wB:0");
        assertThat(again.tuple().attempts()).as("an attempt was counted for the lapse").isEqualTo(1);
    }

    // ── scenario 4: a lapse between the write and the ack ────────────────────

    @Test
    void leaseLapsesBetweenWriteAndAck_ackIs404_aspectRowStands_taskIsRetakenAndRewritten() throws Exception {
        Fx f = fixture("la");
        seedTask(f);
        var a = claim("wA:0");

        assertThat(aspects.upsertAspect(TENANT, aspectBody(f, "first", a.claimId(), "wA:0"))).isPositive();
        lapse(a.claimId());
        assertThatThrownBy(() -> tuples.ack(TENANT, a.claimId(), "wA:0"))
                .isInstanceOf(ClaimNotFoundException.class);
        assertThat(problemOf(f)).as("the write stands").isEqualTo("first");

        var b = claim("wB:0");
        assertThat(b.tuple().attempts()).isEqualTo(1);
        assertThat(aspects.upsertAspect(TENANT, aspectBody(f, "second", b.claimId(), "wB:0"))).isPositive();
        assertThat(problemOf(f)).as("the re-extraction lands in the same row").isEqualTo("second");
        tuples.ack(TENANT, b.claimId(), "wB:0");
    }

    // ── scenario 5: the pair, the owner, and the task ────────────────────────

    @Test
    void oneOfTheClaimPairWithoutTheOther_is400NamingBothFields_andWritesNothing() throws Exception {
        Fx f = fixture("pr");
        seedTask(f);
        var a = claim("wA:0");

        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "x", a.claimId(), null)))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("claim_id").hasMessageContaining("claimant");
        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "x", null, "wA:0")))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("claim_id").hasMessageContaining("claimant");
        assertThat(problemOf(f)).isNull();
        // The claim itself is untouched: the refusal happened before any lookup.
        assertThat(aspects.upsertAspect(TENANT, aspectBody(f, "ok", a.claimId(), "wA:0"))).isPositive();
    }

    @Test
    void liveClaimHeldByAnotherClaimant_isClaimOwnership_andWritesNothing() throws Exception {
        Fx f = fixture("ow");
        seedTask(f);
        var a = claim("wA:0");

        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "x", a.claimId(), "wB:0")))
                .isInstanceOf(ClaimOwnershipException.class);
        assertThat(problemOf(f)).isNull();
        assertThatThrownBy(() -> tuples.ack(TENANT, a.claimId(), "wB:0"))
                .as("the same answer ack gives").isInstanceOf(ClaimOwnershipException.class);
    }

    @Test
    void claimThatNeverExisted_isClaimNotFound_andWritesNothing() throws Exception {
        Fx f = fixture("nx");
        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "x", "no-such-claim", "wA:0")))
                .isInstanceOf(ClaimNotFoundException.class);
        assertThat(problemOf(f)).isNull();
    }

    @Test
    void liveClaimOnAnotherDocumentsTask_isRefusedAsATaskMismatch_andWritesNothing() throws Exception {
        Fx held = fixture("mh");
        Fx other = fixture("mo");
        seedTask(held);
        var a = claim("wA:0");

        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(other, "x", a.claimId(), "wA:0")))
                .isInstanceOf(TupleException.class)
                .satisfies(e -> {
                    var t = (TupleException) e;
                    assertThat(t.httpStatus()).isEqualTo(403);
                    assertThat(t.code()).isEqualTo("ClaimTaskMismatch");
                    assertThat(t.getMessage()).contains(other.docId());
                });
        assertThat(problemOf(other)).isNull();
        assertThat(problemOf(held)).isNull();
        assertThat(aspects.upsertAspect(TENANT, aspectBody(held, "ok", a.claimId(), "wA:0")))
                .as("the claim is still good for its own task").isPositive();
    }

    @Test
    void liveClaimOnATaskInAnotherSubspace_isRefusedAsATaskMismatch_andWritesNothing() throws Exception {
        Fx f = fixture("ms");
        String q = "q" + f.docId().replace('.', '-');
        tuples.out(TENANT, "queue/" + q, Map.of("queue", q), Map.of("from", "p"),
                JSON.writeValueAsString(Map.of("doc_id", f.docId())), "n-1", null);
        var c = tuples.inp(TENANT, "queue/" + q, Map.of("queue", q), "wA:0", (Long) 60L).orElseThrow();

        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "x", c.claimId(), "wA:0")))
                .isInstanceOfSatisfying(TupleException.class, t -> {
                    assertThat(t.httpStatus()).isEqualTo(403);
                    assertThat(t.code()).isEqualTo("ClaimTaskMismatch");
                });
        assertThat(problemOf(f)).isNull();
    }

    @Test
    void liveClaimOnAnAspectsTaskWhoseBodyIsNotJsonOrHasNoDocId_isRefusedAsATaskMismatch() throws Exception {
        Fx notJson = fixture("bj");
        Fx noDoc = fixture("bd");
        tuples.out(TENANT, SUBSPACE, PATTERN, Map.of(), "not json at all", notJson.docId(), null);
        var a = claim("wA:0");
        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(notJson, "x", a.claimId(), "wA:0")))
                .isInstanceOfSatisfying(TupleException.class, t -> assertThat(t.code()).isEqualTo("ClaimTaskMismatch"));
        tuples.ack(TENANT, a.claimId(), "wA:0");

        tuples.out(TENANT, SUBSPACE, PATTERN, Map.of(), "{\"collection\":\"c\"}", noDoc.docId(), null);
        var b = claim("wA:1");
        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(noDoc, "x", b.claimId(), "wA:1")))
                .isInstanceOfSatisfying(TupleException.class, t -> assertThat(t.code()).isEqualTo("ClaimTaskMismatch"));
        assertThat(problemOf(notJson)).isNull();
        assertThat(problemOf(noDoc)).isNull();
    }

    // ── order: the claim lookup runs before the retired-name check ───────────

    @Test
    void retiredCollection_lapsedClaimIs404_liveClaimIs409() throws Exception {
        Fx f = fixture("rt");
        seedTask(f);
        var a = claim("wA:0");
        String successor = f.collection().replace("__v1", "__v2");
        assertThat(repo.supersedeCollection(TENANT, f.collection(), successor, "")).isEqualTo(1);

        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "x", a.claimId(), "wA:0")))
                .as("a live claim: the retired name answers 409, with the claim live at that instant")
                .isInstanceOf(CollectionSupersededException.class);
        lapse(a.claimId());
        assertThatThrownBy(() -> aspects.upsertAspect(TENANT, aspectBody(f, "x", a.claimId(), "wA:0")))
                .as("a lapsed claim: 404 first, so a 409 always means the claim was live")
                .isInstanceOf(ClaimNotFoundException.class);
        assertThat(problemOf(f)).isNull();
    }

    // ── claim-less callers are unchanged ─────────────────────────────────────

    @Test
    void writeWithNoClaimFields_isUnchanged_evenWithALapsedClaimOnTheTask() throws Exception {
        Fx f = fixture("cl");
        seedTask(f);
        var a = claim("wA:0");
        lapse(a.claimId());

        assertThat(aspects.upsertAspect(TENANT, aspectBody(f, "claimless", null, null))).isPositive();
        assertThat(problemOf(f)).isEqualTo("claimless");
    }

    // ── the row lock ─────────────────────────────────────────────────────────

    /**
     * The fenced write holds {@code FOR NO KEY UPDATE} on the tuples row from its lookup to its commit.
     * The lease lapses (database clock) while the write is held between its lookup and its insert; a
     * re-take in that window skips the locked row, and gets it once the write has committed. Without the
     * lock the re-take would hand the task to a second worker while the first worker's write is still in
     * flight.
     */
    @Test
    void reTakeDuringTheFencedWrite_skipsTheLockedRow_untilTheWriteCommits() throws Exception {
        Fx f = fixture("rc");
        seedTask(f);
        var a = tuples.inp(TENANT, SUBSPACE, PATTERN, "wA:0", (Long) 3L).orElseThrow();

        var held = new CountDownLatch(1);
        var go = new CountDownLatch(1);
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> {
            if ("fenced-writer".equals(Thread.currentThread().getName())) {
                held.countDown();
                try {
                    go.await(60, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
        };
        var write = new CompletableFuture<Long>();
        var t = new Thread(() -> {
            try {
                write.complete(aspects.upsertAspect(TENANT, aspectBody(f, "from A", a.claimId(), "wA:0")));
            } catch (Throwable e) {
                write.completeExceptionally(e);
            }
        }, "fenced-writer");
        t.start();
        try {
            assertThat(held.await(30, TimeUnit.SECONDS))
                    .as("the fenced write reached its claim lookup and holds the row").isTrue();

            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
            while (!leaseLapsed(a.claimId())) {
                assertThat(System.nanoTime()).as("the 3 s lease lapsed on the database clock").isLessThan(deadline);
                Thread.sleep(100);
            }
            assertThat(tuples.inp(TENANT, SUBSPACE, PATTERN, "wB:0", (Long) 600L))
                    .as("the lapsed row is locked by the in-flight write, so the re-take skips it")
                    .isEmpty();
            assertThat(write.isDone()).as("the write is still in flight").isFalse();
        } finally {
            go.countDown();
        }

        assertThat(write.get(30, TimeUnit.SECONDS)).as("A's write passed its check and committed").isPositive();
        var b = tuples.inp(TENANT, SUBSPACE, PATTERN, "wB:0", (Long) 600L).orElseThrow();
        assertThat(b.tuple().attempts()).isEqualTo(1);
        assertThat(aspects.upsertAspect(TENANT, aspectBody(f, "from B", b.claimId(), "wB:0"))).isPositive();
        assertThat(problemOf(f)).as("the later holder's result stands").isEqualTo("from B");
    }

    private boolean leaseLapsed(String claimId) throws Exception {
        return admin(ctx -> ctx.fetchExists(ctx.selectOne().from(TUPLES)
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.CLAIM_ID.eq(claimId))
                        .and(TUPLES.LEASE_UNTIL.lt(DSL.currentOffsetDateTime())))));
    }
}
