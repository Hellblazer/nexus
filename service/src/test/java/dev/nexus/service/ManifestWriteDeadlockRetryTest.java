// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashHex;
import dev.nexus.service.db.DeadlockRetry;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.RacedEmbedActivity;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-wbfpw.66: the catalog manifest writers run each transaction under
 * {@link DeadlockRetry}, so a 40P01 against a concurrent chunk writer is retried instead of surfacing.
 *
 * <p><strong>The deadlock.</strong> A manifest change that drops an owner row fires the vectors-021-3
 * statement triggers, which lock the dropped chunk rows ({@code FOR NO KEY UPDATE}) after the manifest
 * rows are already locked by the same statement. A concurrent writer that holds one of those chunk rows
 * and then wants one of the manifest rows closes a cycle. Every test here builds exactly that, with a
 * genuine, unmocked 40P01:
 * <ol>
 *   <li>the "other writer" (a superuser connection) locks a chunk row the manifest change will drop;</li>
 *   <li>the manifest write starts and blocks in the trigger (waited for through {@code pg_stat_activity});</li>
 *   <li>the other writer then asks for a lock on the document's manifest rows, which the blocked write
 *       holds, closing the cycle;</li>
 *   <li>Postgres kills the victim after {@code deadlock_timeout}. The other writer's own timeout is set
 *       far above the service's, so the service connection starts waiting first, runs its check first and
 *       is the victim, every time;</li>
 *   <li>the other writer commits once its statement returns, and the retried attempt then succeeds.</li>
 * </ol>
 * Each test asserts the retry really happened ({@link DeadlockRetry#retryAttemptCount()} moved), that the
 * write then produced its effects exactly once, and that nothing the discarded attempt did outside the
 * transaction (the raced-embed counter, the sweep's audit row) was repeated.
 *
 * <p><strong>Coverage.</strong> Seven writers are driven through a real 40P01 here: writeManifest,
 * writeManifestMany, appendManifestChunks, appendManifestMany, purgeManifest, importChunksBatch and
 * importChunk. deleteCollectionTxn, renameCollectionTxn, purgeTrash, rehomeCollection and
 * ChashRepository.renameCollection are covered by {@code ManifestWriteRetryGateTest} (structure) and
 * their functional tests, not by a provoked deadlock. The cycle is built with a superuser connection, so
 * this proves the retry works, not that a production writer takes these locks in the opposite order.
 *
 * <p><strong>What the completion-stamp assertions cannot show.</strong> The per-attempt reset of the
 * refusal list is not exercised: the stamp is the last statement of the transaction, so the deadlock this
 * test provokes (in the manifest statement's trigger) always comes BEFORE any refusal is collected, and the
 * list is empty at every retry. A 40P01 raised at commit, after a refusal was collected, could repeat it;
 * no test builds that. The {@code complete_refused_count} assertions pin the committed attempt's value only.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestWriteDeadlockRetryTest {

    private static final String SVC_ROLE = "svc_manifest_deadlock_test";
    private static final String SVC_PASS = "svc_manifest_deadlock_test_pass";
    private static final String TENANT = "manifest-deadlock-tenant";
    private static final Duration TWO_HOURS = Duration.ofHours(2);

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private CatalogRepository repo;
    private ExecutorService pool;
    private final AtomicInteger scenarios = new AtomicInteger();

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
        cfg.setMaximumPoolSize(6);
        cfg.setConnectionTimeout(5000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        pool = Executors.newFixedThreadPool(3);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (repo != null) repo.close();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    /** One isolated scenario: its own collection, document and chunks. c1..c3 are old enough for the triggers to lock. */
    private record Scenario(String coll, String doc, String c1, String c2, String c3, String c4) { }

    private static byte[] bytes(String hex) {
        return Chash.fromHex(hex).toBytes();
    }

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    /** Document with manifest [pos0 -> c1, pos1 -> c2]; c1..c3 aged two hours; c4 fresh, with no manifest row. */
    private Scenario seed() throws Exception {
        int n = scenarios.incrementAndGet();
        var s = new Scenario("knowledge__mwdr-" + n + "__minilm-l6-v2-384__v1", "1.9." + n,
            Chash.ofText("mwdr/" + n + "/c1").toHex(), Chash.ofText("mwdr/" + n + "/c2").toHex(),
            Chash.ofText("mwdr/" + n + "/c3").toHex(), Chash.ofText("mwdr/" + n + "/c4").toHex());
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, s.coll());
            PgContainerHelper.insertChunks(ctx, TENANT, s.coll(), List.of(s.c1(), s.c2(), s.c3(), s.c4()),
                List.of("c1", "c2", "c3", "c4"),
                List.of(new float[384], new float[384], new float[384], new float[384]),
                List.of(Map.of(), Map.of(), Map.of(), Map.of()));
            OffsetDateTime then = OffsetDateTime.now().minus(TWO_HOURS);
            for (String h : List.of(s.c1(), s.c2(), s.c3())) {
                ctx.update(CHUNKS).set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then)
                   .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(s.coll()))
                          .and(CHUNKS.CHASH.eq(bytes(h)))).execute();
            }
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(TENANT, s.doc(), "doc " + s.doc(), s.coll()).execute();
            int pos = 0;
            for (String h : List.of(s.c1(), s.c2())) {
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                        CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                        CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                        CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                   .values(TENANT, s.doc(), pos++, bytes(h), s.coll(),
                           PgContainerHelper.collectionModel(ctx, TENANT, s.coll())).execute();
            }
        });
        return s;
    }

    private static Map<String, Object> row(int position, String chash) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("position", position);
        r.put("chash", chash);
        r.put("chunk_index", position);
        return r;
    }

    private Map<Integer, String> manifest(Scenario s) throws Exception {
        Map<Integer, String> out = new TreeMap<>();
        su(ctx -> ctx.select(CATALOG_DOCUMENT_CHUNKS.POSITION, ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH))
            .from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(s.doc())))
            .fetch().forEach(r -> out.put(r.value1(), r.value2())));
        return out;
    }

    /** chash -> number of orphaning records, for the scenario's collection. */
    private Map<String, Integer> orphanRecords(Scenario s) throws Exception {
        Map<String, Integer> out = new TreeMap<>();
        su(ctx -> ctx.select(ChashHex.hex(CHUNK_ORPHANED_AT.CHASH))
            .from(CHUNK_ORPHANED_AT)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(TENANT).and(CHUNK_ORPHANED_AT.COLLECTION.eq(s.coll())))
            .fetch().forEach(r -> out.merge(r.value1(), 1, Integer::sum)));
        return out;
    }

    private int gcAuditRows(Scenario s) throws Exception {
        int[] n = new int[1];
        su(ctx -> n[0] = ctx.fetchCount(GC_AUDIT,
            GC_AUDIT.TENANT_ID.eq(TENANT).and(GC_AUDIT.COLLECTION.eq(s.coll()))));
        return n[0];
    }

    private boolean chunkExists(Scenario s, String hex) throws Exception {
        boolean[] b = new boolean[1];
        su(ctx -> b[0] = ctx.fetchExists(ctx.selectOne().from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(s.coll())).and(CHUNKS.CHASH.eq(bytes(hex))))));
        return b[0];
    }

    // ── the deterministic deadlock ──────────────────────────────────────────

    /**
     * Runs {@code write} on a service connection while a second, superuser connection holds
     * {@code lockedChash} and then reaches for the document's manifest rows, so the write deadlocks
     * with it (40P01) at least once. Returns what the write returned once it succeeded; any failure of
     * the write, or of the harness, fails the test with the cause.
     */
    private <T> T deadlockedOnce(Scenario s, String lockedChash, Callable<T> write) throws Exception {
        long retriesBefore = DeadlockRetry.retryAttemptCount();
        try (Connection other = pg.createConnection("")) {
            other.setAutoCommit(false);
            DSLContext ex = DSL.using(other, SQLDialect.POSTGRES);
            // The other writer never runs its own deadlock check first: the service connection, which
            // starts waiting earlier and keeps the default 1 s, is always the one Postgres kills.
            ex.select(DSL.function("set_config", String.class,
                DSL.val("deadlock_timeout"), DSL.val("120s"), DSL.val(false))).fetch();
            ex.select(CHUNKS.CHASH).from(CHUNKS)
              .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(s.coll()))
                     .and(CHUNKS.CHASH.eq(bytes(lockedChash))))
              .forNoKeyUpdate().fetch();

            Future<T> writer = pool.submit(write);
            assertThat(PgActivityProbe.waitsOnALock(pg, "%catalog_document_chunks%"))
                .as("the manifest write must block on the chunk row the other writer holds (else no race happened)")
                .isTrue();

            Future<?> closeCycle = pool.submit(() -> ex.select(CATALOG_DOCUMENT_CHUNKS.POSITION)
                .from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(s.doc())))
                .forUpdate().fetch());
            // Returns when the service connection was killed as the deadlock victim and its locks went.
            closeCycle.get(60, TimeUnit.SECONDS);
            other.commit();

            T result = writer.get(60, TimeUnit.SECONDS);
            assertThat(DeadlockRetry.retryAttemptCount() - retriesBefore)
                .as("the manifest write must have been killed by a real 40P01 and retried")
                .isGreaterThanOrEqualTo(1);
            return result;
        }
    }

    // ── the entry points ────────────────────────────────────────────────────

    @Test
    void writeManifest_isRetriedAfterADeadlock() throws Exception {
        Scenario s = seed();
        deadlockedOnce(s, s.c2(), () -> {
            repo.writeManifest(TENANT, s.doc(), s.coll(), List.of(row(0, s.c1()), row(1, s.c3())));
            return null;
        });
        assertThat(manifest(s)).containsExactly(Map.entry(0, s.c1()), Map.entry(1, s.c3()));
        // The DELETE trigger records every chash the statement removed, once; the discarded attempt's
        // records rolled back with it and the retried attempt wrote them again.
        assertThat(orphanRecords(s)).containsOnly(Map.entry(s.c1(), 1), Map.entry(s.c2(), 1));
        su(ctx -> assertThat(ctx.select(CATALOG_DOCUMENTS.CHUNK_COUNT).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(s.doc())))
            .fetchOne().value1()).isEqualTo(2));
    }

    @Test
    void writeManifestMany_isRetriedAfterADeadlock_andItsSideEffectsRunOnce() throws Exception {
        Scenario s = seed();
        // [c1, c1] with the completion stamp requested: drops c2, which the post-commit sweep then deletes.
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("doc_id", s.doc());
        doc.put("rows", List.of(row(0, s.c1()), row(1, s.c1())));
        Map<String, Object> result = deadlockedOnce(s, s.c2(), () ->
            repo.writeManifestMany(TENANT, List.of(doc), s.coll(), Map.of(s.doc(), "hash-1"), true, null));

        assertThat(result.get("docs")).isEqualTo(1);
        assertThat((List<?>) result.get("failed_doc_ids")).as("the deadlock must not surface as a failed doc").isEmpty();
        assertThat(result.get("complete_refused_count")).isEqualTo(0);
        assertThat(manifest(s)).containsExactly(Map.entry(0, s.c1()), Map.entry(1, s.c1()));
        su(ctx -> assertThat(ctx.select(CATALOG_DOCUMENTS.INDEX_STATE).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(s.doc())))
            .fetchOne().value1()).as("the completion stamp landed with the committed attempt").isEqualTo("complete"));
        assertThat(result.get("swept")).isEqualTo(1);
        assertThat(chunkExists(s, s.c2())).as("the sweep ran, once, after the commit").isFalse();
        assertThat(gcAuditRows(s)).as("exactly one sweep audit row").isEqualTo(1);
        // c1 was recorded as dropped by the DELETE trigger; c2's record went with the chunk the sweep deleted.
        assertThat(orphanRecords(s)).containsOnly(Map.entry(s.c1(), 1));
    }

    @Test
    void writeManifestMany_racedEmbedIsCountedOncePerCommittedWrite() throws Exception {
        Scenario s = seed();
        // c4 exists already (a writer that committed it between the caller's existence check and this
        // insert) while the request says it was absent: one raced embed, detected inside the transaction
        // BEFORE the manifest DELETE that deadlocks. A retry must not count it twice.
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("doc_id", s.doc());
        doc.put("rows", List.of(row(0, s.c1()), row(1, s.c4())));
        var resolved = Map.of(s.c4(), new CatalogRepository.ResolvedChunk("c4", new float[384], "{}", true));
        long racedBefore = RacedEmbedActivity.total();
        Map<String, Object> result = deadlockedOnce(s, s.c2(), () ->
            repo.writeManifestMany(TENANT, List.of(doc), s.coll(), null, false, resolved));

        assertThat((List<?>) result.get("failed_doc_ids")).isEmpty();
        assertThat(result.get("chunks_written")).isEqualTo(1);
        assertThat(manifest(s)).containsExactly(Map.entry(0, s.c1()), Map.entry(1, s.c4()));
        assertThat(RacedEmbedActivity.total() - racedBefore).as("raced embeds counted once").isEqualTo(1);
    }

    @Test
    void appendManifestChunks_isRetriedAfterADeadlock() throws Exception {
        Scenario s = seed();
        // Re-point position 0 from c1 to c3: the UPDATE trigger drops c1 and locks it.
        deadlockedOnce(s, s.c1(), () -> {
            repo.appendManifestChunks(TENANT, s.doc(), s.coll(), List.of(row(0, s.c3())));
            return null;
        });
        assertThat(manifest(s)).containsExactly(Map.entry(0, s.c3()), Map.entry(1, s.c2()));
        assertThat(orphanRecords(s)).containsOnly(Map.entry(s.c1(), 1));
    }

    @Test
    void appendManifestMany_isRetriedAfterADeadlock_andReportsTheCommittedRefusalOnce() throws Exception {
        Scenario s = seed();
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("doc_id", s.doc());
        doc.put("rows", List.of(row(0, s.c3())));
        // Refused: 2 rows referenced, not 5. Collected by the committed attempt's stamp, the last statement of
        // the transaction, so it cannot exist yet when the earlier attempt deadlocks (see the class javadoc).
        doc.put("complete", Map.of("content_hash", "hash-1", "chunk_count", 5));
        Map<String, Object> result = deadlockedOnce(s, s.c1(), () ->
            repo.appendManifestMany(TENANT, s.coll(), List.of(doc), null));

        assertThat(result.get("docs")).isEqualTo(1);
        assertThat((List<?>) result.get("failed_doc_ids")).isEmpty();
        assertThat(result.get("complete_refused_count")).isEqualTo(1);
        assertThat(manifest(s)).containsExactly(Map.entry(0, s.c3()), Map.entry(1, s.c2()));
        assertThat(orphanRecords(s)).containsOnly(Map.entry(s.c1(), 1));
    }

    @Test
    void purgeManifest_isRetriedAfterADeadlock() throws Exception {
        Scenario s = seed();
        int deleted = deadlockedOnce(s, s.c2(), () -> repo.purgeManifest(TENANT, s.doc()));
        assertThat(deleted).isEqualTo(2);
        assertThat(manifest(s)).isEmpty();
        assertThat(orphanRecords(s)).containsOnly(Map.entry(s.c1(), 1), Map.entry(s.c2(), 1));
    }

    @Test
    void importChunksBatch_isRetriedAfterADeadlock() throws Exception {
        Scenario s = seed();
        int imported = deadlockedOnce(s, s.c1(), () ->
            repo.importChunksBatch(TENANT, s.doc(), s.coll(), List.of(row(0, s.c3()))));
        assertThat(imported).isEqualTo(1);
        assertThat(manifest(s)).containsExactly(Map.entry(0, s.c3()), Map.entry(1, s.c2()));
        assertThat(orphanRecords(s)).containsOnly(Map.entry(s.c1(), 1));
    }

    @Test
    void importChunk_isRetriedAfterADeadlock() throws Exception {
        Scenario s = seed();
        deadlockedOnce(s, s.c1(), () -> {
            repo.importChunk(TENANT, s.doc(), s.coll(), row(0, s.c3()));
            return null;
        });
        assertThat(manifest(s)).containsExactly(Map.entry(0, s.c3()), Map.entry(1, s.c2()));
        assertThat(orphanRecords(s)).containsOnly(Map.entry(s.c1(), 1));
    }

    @Test
    void aNonDeadlockFailureIsNotRetried() {
        long before = DeadlockRetry.retryAttemptCount();
        List<Map<String, Object>> rows = new ArrayList<>(List.of(row(0, Chash.ofText("x").toHex())));
        assertThatThrownBy(() -> repo.writeManifest(TENANT, "1.9.no-such-doc", "knowledge__none__minilm-l6-v2-384__v1", rows))
            .isInstanceOf(CatalogRepository.DocumentNotFoundException.class);
        assertThat(DeadlockRetry.retryAttemptCount()).isEqualTo(before);
    }
}
