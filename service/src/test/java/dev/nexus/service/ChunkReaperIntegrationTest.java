// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.ChunkReaper.CollectionResult;
import dev.nexus.service.ChunkReaper.Refusal;
import dev.nexus.service.ChunkReaper.RunResult;
import dev.nexus.service.ChunkReaper.Settings;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 9 (bead nexus-2x9xa): the state-derived reaper. Drives {@link ChunkReaper#runOnce} against a real
 * PostgreSQL substrate, never a mock.
 *
 * <p>Orphans are seeded through substrate SQL ({@link PgContainerHelper#insertChunks}, nexus-z0o2p.23), never
 * through {@code upsert-chunks}, which from RDR-223 Phase 3 refuses an ownerless write. Tests that need a chunk
 * to be reapable NOW inject a zero grace into the package-private entry point; production has no grace setting
 * at all (the predicate's own 30 days). Nothing here reads or writes {@code last_written_at} except the two
 * tests that exist to exercise the default window and a client write racing the move.
 *
 * <p>Each test works in its own tenant, so a pass over one test's tenant cannot move another test's chunks and
 * the per-tenant backfill gate is a per-test fact.
 */
class ChunkReaperIntegrationTest extends AtomicWriteTestBase {

    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-10-01T12:00:00Z"), ZoneOffset.UTC);

    private PgVectorRepository vectors;
    private ReaperRepository store;
    private LadderRepository ladder;
    private Rdr192BackfillGate gate;

    @BeforeAll
    void wire() {
        vectors = new PgVectorRepository(tenantScope, embedder, embedder);
        store = new ReaperRepository(tenantScope);
        ladder = new LadderRepository(tenantScope);
        gate = new Rdr192BackfillGate(ladder);
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    private String newTenant() {
        return "rp" + seq.incrementAndGet();
    }

    private void openGate(String tenant) {
        ladder.record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
    }

    private String col(String prefix) {
        return prefix + "__rp" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private static String quarantineOf(String collection) {
        return "quarantine-" + collection;
    }

    private static Settings settings(int batch, double fraction, int minChunks) {
        return new Settings(true, Duration.ofHours(1), batch, fraction, minChunks, Duration.ofMinutes(10),
            Duration.ofSeconds(60));
    }

    private ChunkReaper reaper(Settings s, String... tenants) {
        return new ChunkReaper(store, vectors, repo, gate, () -> List.of(tenants), s, CLOCK);
    }

    /** A reaper whose census and monotonic clock a test supplies (a census the SQL cannot produce, a clock that jumps). */
    private ChunkReaper reaper(Settings s, ChunkReaper.Census census, java.util.function.LongSupplier nanos,
                               String... tenants) {
        return new ChunkReaper(store, vectors, repo, gate, () -> List.of(tenants), s, CLOCK, census, nanos);
    }

    private ChunkReaper reaper(String... tenants) {
        return reaper(Settings.defaults(), tenants);
    }

    /** One chunk with no manifest row, written through substrate SQL. */
    private String orphan(String tenant, String collection, String seed, Map<String, Object> metadata) throws Exception {
        String hex = Chash.ofText(collection + "/" + seed).toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            PgContainerHelper.insertChunks(ctx, tenant, collection, List.of(hex), List.of(seed + " text"),
                List.of(new float[384]), List.of(metadata));
        }
        return hex;
    }

    private String orphan(String tenant, String collection, String seed) throws Exception {
        return orphan(tenant, collection, seed, Map.of());
    }

    /** {@code n} chunks, the first {@code owned} of them owned by a manifest row. */
    private List<String> bulk(String tenant, String collection, int n, int owned) throws Exception {
        List<String> hexes = new ArrayList<>();
        for (int i = 0; i < n; i++) hexes.add(orphan(tenant, collection, "bulk" + i));
        if (owned > 0) {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.ownChunks(DSL.using(su, SQLDialect.POSTGRES), tenant, collection,
                    hexes.subList(0, owned).toArray(new String[0]));
            }
        }
        return hexes;
    }

    private void registerNote(String tenant, String docId, String collection, Map<String, Object> metadata) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("tumbler", docId);
        d.put("title", "reaper-" + docId);
        d.put("content_type", "knowledge");
        d.put("corpus", "knowledge");
        d.put("physical_collection", collection);
        d.put("chunk_count", 0);
        if (metadata != null) d.put("metadata", metadata);
        repo.upsertDocument(tenant, d);
    }

    private Map<String, Object> manifestDoc(String docId, String... hexes) {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int i = 0; i < hexes.length; i++) rows.add(Map.of("position", i, "chash", hexes[i], "chunk_index", i));
        return Map.of("doc_id", docId, "rows", rows);
    }

    private boolean inCollection(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))));
        }
    }

    private long countIn(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(CHUNKS,
                CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)));
        }
    }

    private record AuditRow(String operation, String actor, String collection, int chashCount, String chashes,
                            String details) {}

    /** The reaper's MOVE rows ({@code reaper_quarantine}): what a pass that moved something writes. */
    private List<AuditRow> auditRows(String tenant) throws Exception {
        return auditRows(tenant, ChunkReaper.AUDIT_MOVED);
    }

    /** The reaper's durable REFUSAL rows ({@code reaper_refused}). */
    private List<AuditRow> refusedRows(String tenant) throws Exception {
        return auditRows(tenant, ChunkReaper.AUDIT_REFUSED);
    }

    private List<AuditRow> auditRows(String tenant, String operation) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).selectFrom(GC_AUDIT)
                .where(GC_AUDIT.TENANT_ID.eq(tenant).and(GC_AUDIT.OPERATION.eq(operation))).orderBy(GC_AUDIT.ID)
                .fetch(r -> new AuditRow(r.getOperation(), r.getActor(), r.getCollection(), r.getChashCount(),
                    r.getChashes().data(), r.getDetails() == null ? "" : r.getDetails().data()));
        }
    }

    private List<String> captureLogs(org.junit.jupiter.api.function.Executable body) throws Throwable {
        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            body.execute();
            return logs.list.stream().map(e -> e.getLevel() + " " + e.getFormattedMessage()).toList();
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    // ── MVV (b): a failed post-commit sweep leaves debris; the reaper takes it ──────────

    @Test
    void aFailedPostCommitSweepLeavesTheOldChunk_theReaperQuarantinesItAndTheAuditRowNamesIt() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String doc = "rp.note." + seq.incrementAndGet();
        String oldHex = orphan(t, c, "old");
        String newHex = orphan(t, c, "new");
        registerNote(t, doc, c, null);
        repo.writeManifestMany(t, List.of(manifestDoc(doc, oldHex)), c);

        // Re-manifest the note with the sweep ON while a peer holds the sweep gate SHARED: the sweep's
        // EXCLUSIVE acquire times out (55P03) and the writer fails open, leaving the old chunk behind.
        Map<String, Object> result;
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock_shared",
                SQLDataType.OTHER, DSL.function("hashtext", SQLDataType.INTEGER,
                    DSL.val("sweepgate:" + t + "/" + c)))).execute();
            result = repo.writeManifestMany(t, List.of(manifestDoc(doc, newHex)), c, null, true);
            holder.rollback();
        }
        assertThat(result.get("sweep_skipped")).as("the sweep failed on the gate and the writer failed open").isEqualTo(1);

        assertThat(inCollection(t, c, oldHex)).as("the debris row still exists").isTrue();
        assertThat((List<?>) vectors.get(t, c, List.of(oldHex), 10, 0, false).get("ids"))
            .as("raw (live(c)) search hides it").isEmpty();

        RunResult run = reaper(t).runOnce(Duration.ZERO);

        CollectionResult cr = run.tenant(t).collection(c);
        assertThat(cr.moved()).isEqualTo(1);
        assertThat(inCollection(t, c, oldHex)).as("the debris left the collection").isFalse();
        assertThat(inCollection(t, quarantineOf(c), oldHex)).as("and sits in quarantine, reversibly").isTrue();
        assertThat(inCollection(t, c, newHex)).as("the current chunk is untouched").isTrue();
        assertThat(auditRows(t)).singleElement().satisfies(a -> {
            assertThat(a.actor()).isEqualTo(ChunkReaper.ACTOR);
            assertThat(a.collection()).isEqualTo(c);
            assertThat(a.chashCount()).isEqualTo(1);
            assertThat(a.chashes()).contains(oldHex);
        });
    }

    // ── never delete what is owned ───────────────────────────────────────────

    @Test
    void aCurrentNoteNeverReput_isNeverMoved() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String doc = "rp.note." + seq.incrementAndGet();
        String h = orphan(t, c, "current");
        registerNote(t, doc, c, null);
        repo.writeManifestMany(t, List.of(manifestDoc(doc, h)), c);

        RunResult run = reaper(t).runOnce(Duration.ZERO);

        assertThat(run.tenant(t).collection(c).moved()).isZero();
        assertThat(inCollection(t, c, h)).isTrue();
        assertThat(auditRows(t)).isEmpty();
    }

    @Test
    void aChunkSharedByTwoDocuments_oneOfWhichIsReput_survives() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String shared = orphan(t, c, "shared");
        String onlyA = orphan(t, c, "only-a");
        String replacement = orphan(t, c, "replacement");
        String docA = "rp.a." + seq.incrementAndGet();
        String docB = "rp.b." + seq.incrementAndGet();
        registerNote(t, docA, c, null);
        registerNote(t, docB, c, null);
        repo.writeManifestMany(t, List.of(manifestDoc(docA, shared, onlyA), manifestDoc(docB, shared)), c);
        // A is re-put with different content, sweep off: its old chunks lose A as an owner.
        repo.writeManifestMany(t, List.of(manifestDoc(docA, replacement)), c);

        RunResult run = reaper(t).runOnce(Duration.ZERO);

        assertThat(run.tenant(t).collection(c).moved()).as("only A's exclusive chunk").isEqualTo(1);
        assertThat(inCollection(t, c, shared)).as("B still owns the shared chunk").isTrue();
        assertThat(inCollection(t, c, replacement)).isTrue();
        assertThat(inCollection(t, c, onlyA)).isFalse();
    }

    @Test
    void aNoteWhoseCatalogMetaDocIdStillNamesTheOldChash_theOldChunkIsMoved() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String doc = "rp.note." + seq.incrementAndGet();
        String oldHex = orphan(t, c, "old");
        String newHex = orphan(t, c, "new");
        registerNote(t, doc, c, Map.of("doc_id", oldHex));   // the note's own pointer still names the old chash
        repo.writeManifestMany(t, List.of(manifestDoc(doc, oldHex)), c);
        repo.writeManifestMany(t, List.of(manifestDoc(doc, newHex)), c);

        RunResult run = reaper(t).runOnce(Duration.ZERO);

        // Decided by reapable(c) alone. A reaper that deferred to the sweep's notes guard would leave this
        // chunk where it is forever, and this assertion fails.
        assertThat(run.tenant(t).collection(c).moved()).isEqualTo(1);
        assertThat(inCollection(t, c, oldHex)).isFalse();
        assertThat(inCollection(t, quarantineOf(c), oldHex)).isTrue();
        assertThat(inCollection(t, c, newHex)).isTrue();
    }

    // ── every prefix ─────────────────────────────────────────────────────────

    @Test
    void everyContentTypePrefixIsCovered() throws Exception {
        String t = newTenant();
        openGate(t);
        for (String prefix : List.of("knowledge", "docs", "code", "rdr")) {
            String c = col(prefix);
            String h = orphan(t, c, "x");
            RunResult run = reaper(t).runOnce(Duration.ZERO);
            assertThat(run.tenant(t).collection(c).moved()).as("%s__ orphan moves", prefix).isEqualTo(1);
            assertThat(inCollection(t, c, h)).isFalse();
            assertThat(inCollection(t, quarantineOf(c), h)).isTrue();
        }
    }

    // ── grace ────────────────────────────────────────────────────────────────

    @Test
    void theDefaultGraceKeepsAFreshOrphan() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "fresh");

        RunResult run = reaper(t).runOnce(null);

        assertThat(run.tenant(t).collection(c).moved()).isZero();
        assertThat(run.tenant(t).collection(c).candidates()).isZero();
        assertThat(inCollection(t, c, h)).isTrue();
    }

    @Test
    void theGraceIsRecheckedInTheMoveStatement_aRacingClientRefreshWins() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String raced = orphan(t, c, "raced");
        String neighbour = orphan(t, c, "neighbour");
        // Both chunks were last written 40 days ago: reapable under the default window.
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minusDays(40))
               .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(c))).execute();
        }

        try (Connection writer = svcDs.getConnection()) {
            writer.setAutoCommit(false);
            PgContainerHelper.setTenant(writer, TenantScope.DEFAULT_TENANT_GUC, t, true);
            // A client re-write: an uncommitted refresh of one orphan.
            DSL.using(writer, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now())
               .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(c))
                      .and(CHUNKS.CHASH.eq(Chash.fromHex(raced).toBytes()))).execute();

            CompletableFuture<RunResult> pass = CompletableFuture.supplyAsync(() -> reaper(t).runOnce(null));

            assertThat(PgActivityProbe.waitsOnALock(pg, "%reaper_quarantine_chunks%"))
                .as("the reaper's move statement blocks on the uncommitted refresh").isTrue();
            writer.commit();

            CollectionResult cr = pass.get(60, TimeUnit.SECONDS).tenant(t).collection(c);
            assertThat(cr.moved()).as("only the unrefreshed neighbour moves").isEqualTo(1);
        }
        assertThat(inCollection(t, c, raced)).as("the refreshed chunk stays").isTrue();
        assertThat(inCollection(t, c, neighbour)).isFalse();
        assertThat(inCollection(t, quarantineOf(c), raced)).as("and was never copied to quarantine").isFalse();
        assertThat(inCollection(t, quarantineOf(c), neighbour)).isTrue();
    }

    @Test
    void theMoveTakesTheExclusiveSweepGate_aHeldSharedGateSkipsTheCollectionAndNothingMoves() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        CollectionResult busy;
        try (Connection writer = pg.createConnection("")) {
            writer.setAutoCommit(false);
            // A manifest writer holds the gate SHARED, as every manifest write does for its transaction.
            DSL.using(writer, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock_shared",
                SQLDataType.OTHER, DSL.function("hashtext", SQLDataType.INTEGER,
                    DSL.val("sweepgate:" + t + "/" + c)))).execute();
            busy = reaper(t).runOnce(Duration.ZERO).tenant(t).collection(c);
            writer.rollback();
        }
        assertThat(busy.refusal()).as("an exclusive acquire cannot be granted over a shared holder")
            .isEqualTo(Refusal.GATE_BUSY);
        assertThat(inCollection(t, c, h)).isTrue();

        assertThat(reaper(t).runOnce(Duration.ZERO).tenant(t).collection(c).moved())
            .as("the next pass, gate free, takes it").isEqualTo(1);
    }

    // ── the backfill gate and the census ─────────────────────────────────────

    @Test
    void aTenantWithNoBackfillRecord_isRefusedVisibly_andNothingMoves() throws Throwable {
        String t = newTenant();   // no openGate
        String c = col("knowledge");
        String h = orphan(t, c, "x");

        RunResult[] run = new RunResult[1];
        List<String> logs = captureLogs(() -> run[0] = reaper(t).runOnce(Duration.ZERO));

        assertThat(run[0].tenant(t).tenantRefusal()).isEqualTo(Refusal.BACKFILL_INCOMPLETE);
        assertThat(inCollection(t, c, h)).isTrue();
        assertThat(auditRows(t)).isEmpty();
        assertThat(logs).as("a counted, visible signal, not an info line")
            .anyMatch(l -> l.startsWith("WARN") && l.contains("event=reaper_tenant_refused")
                && l.contains("tenant=" + t) && l.contains("reason=BACKFILL_INCOMPLETE"));
    }

    @Test
    void aStaleRecordWithAResidualLegacyNote_refusesThatCollectionAndDeletesNothing() throws Throwable {
        String t = newTenant();
        openGate(t);   // the stored ladder record exists; the census is what has to say no
        String legacyCol = col("knowledge");
        String cleanCol = col("knowledge");
        String legacyDoc = "rp.legacy." + seq.incrementAndGet();
        registerNote(t, legacyDoc, legacyCol, null);
        // A live legacy note: its chunk names the document and no manifest row ever existed.
        String legacyChunk = orphan(t, legacyCol, "legacy", Map.of("catalog_doc_id", legacyDoc));
        String legacyNeighbour = orphan(t, legacyCol, "debris");
        String cleanDebris = orphan(t, cleanCol, "debris");

        RunResult[] run = new RunResult[1];
        List<String> logs = captureLogs(() -> run[0] = reaper(t).runOnce(Duration.ZERO));

        CollectionResult legacy = run[0].tenant(t).collection(legacyCol);
        assertThat(legacy.refusal()).isEqualTo(Refusal.CENSUS_LEGACY_UNMANIFESTED);
        assertThat(legacy.moved()).isZero();
        assertThat(inCollection(t, legacyCol, legacyChunk)).isTrue();
        assertThat(inCollection(t, legacyCol, legacyNeighbour)).as("the whole collection is refused").isTrue();
        assertThat(logs).anyMatch(l -> l.startsWith("WARN") && l.contains("event=reaper_collection_refused")
            && l.contains("collection=" + legacyCol) && l.contains("reason=CENSUS_LEGACY_UNMANIFESTED"));
        assertThat(run[0].tenant(t).collection(cleanCol).moved()).as("a clean collection is not held back").isEqualTo(1);
        assertThat(inCollection(t, cleanCol, cleanDebris)).isFalse();
    }

    // ── the fraction floor on the move ───────────────────────────────────────

    @Test
    void aPassThatWouldTakeMoreThanTheFloorFractionOfACollectionIsRefusedAndCounted() throws Throwable {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        bulk(t, c, 300, 200);   // 100 of 300 reapable: at the 100 minimum and 0.33 > 0.25

        RunResult[] run = new RunResult[1];
        List<String> logs = captureLogs(() -> run[0] = reaper(t).runOnce(Duration.ZERO));

        CollectionResult cr = run[0].tenant(t).collection(c);
        assertThat(cr.refusal()).isEqualTo(Refusal.FLOOR_EXCEEDED);
        assertThat(cr.moved()).isZero();
        assertThat(cr.candidates()).isEqualTo(100);
        assertThat(cr.total()).isEqualTo(300);
        assertThat(countIn(t, c)).isEqualTo(300);
        assertThat(countIn(t, quarantineOf(c))).isZero();
        assertThat(auditRows(t)).isEmpty();
        assertThat(logs).anyMatch(l -> l.startsWith("WARN") && l.contains("event=reaper_collection_refused")
            && l.contains("reason=FLOOR_EXCEEDED") && l.contains("candidates=100") && l.contains("total=300"));
    }

    @Test
    void aPassUnderTheFloorFractionMoves() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        bulk(t, c, 400, 300);   // 100 of 400: exactly 0.25, not more than it

        CollectionResult cr = reaper(t).runOnce(Duration.ZERO).tenant(t).collection(c);

        assertThat(cr.refusal()).isNull();
        assertThat(cr.moved()).isEqualTo(100);
        assertThat(countIn(t, c)).isEqualTo(300);
        assertThat(countIn(t, quarantineOf(c))).isEqualTo(100);
    }

    @Test
    void aReapableSetBelowTheFloorMinimumIsExemptFromTheFraction() throws Exception {
        String t = newTenant();
        openGate(t);
        String small = col("knowledge");
        String nearlyAll = col("knowledge");
        bulk(t, small, 10, 0);        // every chunk reapable, but only 10 of them
        bulk(t, nearlyAll, 100, 1);   // 99 of 100 reapable: 99% of the collection, one under the minimum

        RunResult run = reaper(t).runOnce(Duration.ZERO);

        // The minimum counts reapable chunks, as NX_GC_FLOOR_FRACTION's does (indexer._GC_FLOOR_MIN_CHUNKS): below
        // it, even a 100% verdict is a plausible real cleanup of a small set.
        assertThat(run.tenant(t).collection(small).refusal()).isNull();
        assertThat(run.tenant(t).collection(small).moved()).isEqualTo(10);
        assertThat(run.tenant(t).collection(nearlyAll).refusal()).isNull();
        assertThat(run.tenant(t).collection(nearlyAll).moved()).isEqualTo(99);
    }

    @Test
    void theFloorIsConfigurable() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        bulk(t, c, 300, 200);   // 100 of 300: 0.33

        CollectionResult cr = reaper(settings(300, 0.5, 100), t).runOnce(Duration.ZERO).tenant(t).collection(c);

        assertThat(cr.refusal()).isNull();
        assertThat(cr.moved()).isEqualTo(100);
    }

    // ── bounded work ─────────────────────────────────────────────────────────

    @Test
    void atMostTheBatchSizeMovesPerCollectionPerPass() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        bulk(t, c, 350, 0);
        ChunkReaper r = reaper(settings(300, 1.0, 100), t);

        assertThat(r.runOnce(Duration.ZERO).tenant(t).collection(c).moved()).isEqualTo(300);
        assertThat(countIn(t, c)).isEqualTo(50);
        assertThat(r.runOnce(Duration.ZERO).tenant(t).collection(c).moved()).isEqualTo(50);
        assertThat(countIn(t, c)).isZero();
        assertThat(countIn(t, quarantineOf(c))).isEqualTo(350);
        assertThat(auditRows(t)).hasSize(2).allSatisfy(a -> assertThat(a.actor()).isEqualTo(ChunkReaper.ACTOR));
    }

    // ── which collections are visited ────────────────────────────────────────

    @Test
    void aQuarantineSiblingIsNeverReaped() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        String h = orphan(t, q, "aged-in-quarantine");

        RunResult run = reaper(t).runOnce(Duration.ZERO);

        assertThat(inCollection(t, q, h)).as("quarantine chunks have gc_expire_quarantine's own clock and floors").isTrue();
        assertThat(run.tenant(t).collection(q)).as("not even visited").isNull();
    }

    @Test
    void aCollectionThatIsNotLiveIsRefusedVisibly() throws Throwable {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
               .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "dormant")
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t).and(CATALOG_COLLECTIONS.NAME.eq(c))).execute();
        }

        RunResult[] run = new RunResult[1];
        List<String> logs = captureLogs(() -> run[0] = reaper(t).runOnce(Duration.ZERO));

        assertThat(run[0].tenant(t).collection(c).refusal()).isEqualTo(Refusal.COLLECTION_NOT_LIVE);
        assertThat(inCollection(t, c, h)).isTrue();
        assertThat(logs).anyMatch(l -> l.startsWith("WARN") && l.contains("reason=COLLECTION_NOT_LIVE"));
    }

    // ── the event line ───────────────────────────────────────────────────────

    @Test
    void aPassThatFindsNothingStillLogsItsEventWithCandidatesZero() throws Throwable {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String doc = "rp.note." + seq.incrementAndGet();
        String h = orphan(t, c, "owned");
        registerNote(t, doc, c, null);
        repo.writeManifestMany(t, List.of(manifestDoc(doc, h)), c);

        List<String> logs = captureLogs(() -> reaper(t).runOnce(Duration.ZERO));

        assertThat(logs).anyMatch(l -> l.startsWith("INFO") && l.contains("event=reaper_pass")
            && l.contains("tenant=" + t) && l.contains("collections=1") && l.contains("candidates=0")
            && l.contains("moved=0") && l.contains("audit_rows=0"));
    }

    @Test
    void aTenantWithNoCollectionsStillLogsItsEvent() throws Throwable {
        String t = newTenant();
        openGate(t);

        List<String> logs = captureLogs(() -> reaper(t).runOnce(Duration.ZERO));

        assertThat(logs).anyMatch(l -> l.contains("event=reaper_pass") && l.contains("tenant=" + t)
            && l.contains("collections=0") && l.contains("candidates=0"));
    }

    // ── a multi-batch re-index through the real manifest writers ─────────────

    @Test
    void anOldTailChunkOfARunInFlightIsNotMoved_betweenBatches_andTheRunCompletes() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("docs");
        String docId = "rp.doc." + seq.incrementAndGet();
        registerNote(t, docId, c, null);
        writeDocument(t, c, docId, new String[][] {{"a1", "a2"}, {"a3", "a4"}, {"a5", "a6"}}, null);
        makeOld(t, c);
        Set<String> oldTail = Set.of(h(c, "a3"), h(c, "a4"), h(c, "a5"), h(c, "a6"));

        // Run 2 replaces a3 and a6. The reaper (default grace: what production runs) passes between batches.
        List<Long> movedBetween = new ArrayList<>();
        writeDocument(t, c, docId, new String[][] {{"a1", "a2"}, {"b3", "a4"}, {"a5", "b6"}}, k -> {
            if (k < 3) {
                CollectionResult cr = reaper(t).runOnce(null).tenant(t).collection(c);
                movedBetween.add(cr == null ? -1L : cr.moved());
                try {
                    assertThat(chunkSet(t, c)).as("after request %d the old tail is still here", k).containsAll(oldTail);
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            }
        });

        assertThat(movedBetween).as("the reaper ran between batches and took nothing").containsExactly(0L, 0L);
        assertThat(chunkSet(t, c)).contains(h(c, "a4"), h(c, "a5"));
    }

    // ── the floor is judged on the whole reapable set ────────────────────────

    /**
     * {@code n} chunks in ONE connection (a 5000-chunk fixture must not pay a connection per chunk), the first
     * {@code owned} owned by a manifest row, the rest ownerless. {@code metas} supplies each chunk's metadata.
     */
    private List<String> bulkFast(String tenant, String collection, int n, int owned,
                                  java.util.function.IntFunction<Map<String, Object>> metas) throws Exception {
        List<String> hexes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vectors = new ArrayList<>();
        List<Map<String, Object>> metadata = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            hexes.add(Chash.ofText(collection + "/bulk" + i).toHex());
            texts.add("bulk" + i + " text");
            vectors.add(new float[384]);
            metadata.add(metas.apply(i));
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            PgContainerHelper.insertChunks(ctx, tenant, collection, hexes, texts, vectors, metadata);
            if (owned > 0) {
                PgContainerHelper.ownChunks(ctx, tenant, collection, hexes.subList(0, owned).toArray(new String[0]));
            }
        }
        return hexes;
    }

    @Test
    void theFloorIsJudgedOnTheWholeReapableSet_1500OfA5000ChunkCollectionIsRefusedAtQuarter() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        // 1500 reapable of 5000 = 0.30 of the collection, over 0.25. A pass moves at most 300 chunks, and 300 of
        // 5000 is 0.06: judged on the BATCH the floor never trips on a collection past 1200 chunks, which is
        // exactly the mass-orphaning (a manifest TRUNCATE, the deploy+30d cliff) it exists to refuse.
        bulkFast(t, c, 5000, 3500, i -> Map.of());

        CollectionResult cr = reaper(t).runOnce(Duration.ZERO).tenant(t).collection(c);

        assertThat(cr.refusal()).isEqualTo(Refusal.FLOOR_EXCEEDED);
        assertThat(cr.candidates()).as("the whole reapable set, not the 300 one pass would take").isEqualTo(1500);
        assertThat(cr.total()).isEqualTo(5000);
        assertThat(cr.moved()).isZero();
        assertThat(countIn(t, c)).isEqualTo(5000);
        assertThat(countIn(t, quarantineOf(c))).isZero();
    }

    @Test
    void theMoveFunctionItselfRefusesOnTheWholeSet_notOnlyTheJavaPreJudgement() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        bulkFast(t, c, 5000, 3500, i -> Map.of());

        // Straight to the SQL function, bypassing the Java pre-judgement: it re-judges under the sweep gate.
        ReaperRepository.Pass pass = store.move(t, c, quarantineOf(c), "2026-10-01T12:00:00Z", 300, Duration.ZERO,
            0.25, 100, 25_000, 2_000);

        assertThat(pass.refused()).isTrue();
        assertThat(pass.reapable()).isEqualTo(1500);
        assertThat(pass.moved()).isZero();
        assertThat(countIn(t, c)).isEqualTo(5000);
    }

    @Test
    void aFloorRefusedCollectionDoesNotPayForACensus() throws Exception {
        String t = newTenant();
        openGate(t);
        String refused = col("knowledge");
        String fine = col("knowledge");
        bulkFast(t, refused, 300, 200, i -> Map.of());   // 100 of 300: refused by the floor
        orphan(t, fine, "x");
        List<String> censused = new ArrayList<>();
        ChunkReaper.Census counting = (tenant, collection, limit, timeout) -> {
            censused.add(collection);
            return vectors.manifestLessCensusBounded(tenant, collection, limit, 0, timeout);
        };

        RunResult run = reaper(Settings.defaults(), counting, System::nanoTime, t).runOnce(Duration.ZERO);

        assertThat(run.tenant(t).collection(refused).refusal()).isEqualTo(Refusal.FLOOR_EXCEEDED);
        assertThat(run.tenant(t).collection(fine).moved()).isEqualTo(1);
        assertThat(censused).as("the census ran for the collection that would move, and only for it")
            .containsExactly(fine);
    }

    // ── the census: bounded, non-vacuous, unclassified is a refusal ──────────

    /** A census result the SQL could not produce, to drive the gates that guard against its failure modes. */
    private static PgVectorRepository.ManifestLessCensusResult censusOf(long scope, Map<String, Long> totals) {
        Map<String, Long> all = new LinkedHashMap<>();
        for (String b : List.of("superseded", "legacy-unmanifested", "dead-owner", "no-owner", "unclassified")) {
            all.put(b, 0L);
        }
        all.putAll(totals);
        return new PgVectorRepository.ManifestLessCensusResult(0, new LinkedHashMap<>(), new LinkedHashMap<>(), all,
            scope);
    }

    @Test
    void anUnclassifiedChunkRefusesTheCollection_theBucketIsUnreachableInTheSqlToday_andTheGateIsPinned() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        // The census SQL's ELSE branch is unreachable today (every case above it is exhaustive), so no real
        // data produces this. The gate stays, because the SQL's own header promises the bucket is "reported,
        // never dropped" and a future branch must not open the move by silence. Pinned with a census the
        // SQL cannot yet produce.
        ChunkReaper.Census unclassified = (tenant, collection, limit, timeout) ->
            censusOf(1, Map.of("unclassified", 1L));

        CollectionResult cr = reaper(Settings.defaults(), unclassified, System::nanoTime, t).runOnce(Duration.ZERO)
            .tenant(t).collection(c);

        assertThat(cr.refusal()).isEqualTo(Refusal.CENSUS_UNCLASSIFIED);
        assertThat(cr.moved()).isZero();
        assertThat(inCollection(t, c, h)).isTrue();
    }

    @Test
    void aCensusThatReadADifferentSetThanTheOneJudged_isNotTakenForACleanCollection() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        // The false zero the SQL header warns of: a wrong tenant or an unset RLS GUC reads scope 0 and every
        // bucket 0, which is indistinguishable from a clean collection unless the scope total is compared.
        ChunkReaper.Census falseZero = (tenant, collection, limit, timeout) -> censusOf(0, Map.of());

        CollectionResult cr = reaper(Settings.defaults(), falseZero, System::nanoTime, t).runOnce(Duration.ZERO)
            .tenant(t).collection(c);

        assertThat(cr.refusal()).isEqualTo(Refusal.CENSUS_SCOPE_MISMATCH);
        assertThat(cr.moved()).isZero();
        assertThat(inCollection(t, c, h)).as("a census that read nothing is not evidence of a clean collection").isTrue();
    }

    @Test
    void aCensusThatSawMoreChunksThanTheDryRun_isNotAMismatch_growthBetweenTheTwoReadsIsNormal() throws Exception {
        // nexus-wbfpw.56 / RDR-192 Phase 3 gate M6: a chunk written between the dry run and the census (up to 60 s
        // apart) made every pass on a busy collection refuse and write an audit row per state change.
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        ChunkReaper.Census grew = (tenant, collection, limit, timeout) -> censusOf(2, Map.of());

        CollectionResult cr = reaper(Settings.defaults(), grew, System::nanoTime, t).runOnce(Duration.ZERO)
            .tenant(t).collection(c);

        assertThat(cr.refusal()).as("growth is not a mismatch").isNull();
        assertThat(cr.moved()).isEqualTo(1);
        assertThat(inCollection(t, quarantineOf(c), h)).isTrue();
        assertThat(refusedRows(t)).isEmpty();
    }

    @Test
    void aCensusThatSawFewerChunksThanTheDryRun_isStillAMismatch_evenWhenItIsNotZero() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String a = orphan(t, c, "a");
        String b = orphan(t, c, "b");
        ChunkReaper.Census shrank = (tenant, collection, limit, timeout) -> censusOf(1, Map.of());

        CollectionResult cr = reaper(Settings.defaults(), shrank, System::nanoTime, t).runOnce(Duration.ZERO)
            .tenant(t).collection(c);

        assertThat(cr.refusal()).isEqualTo(Refusal.CENSUS_SCOPE_MISMATCH);
        assertThat(cr.moved()).isZero();
        assertThat(inCollection(t, c, a)).isTrue();
        assertThat(inCollection(t, c, b)).isTrue();
    }

    // ── a pass that dies, and the time of the last one that did not (nexus-wbfpw.56, gate S5) ──────────────────

    @Test
    void anErrorOutOfAPassIsAFailedPass_notAThrowableThatEscapesRunOnce_andTheNextPassRuns() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        orphan(t, c, "x");
        var armed = new java.util.concurrent.atomic.AtomicBoolean(true);
        // An Error from deep inside a collection's pass: passCollection catches RuntimeException only.
        ChunkReaper.Census dies = (tenant, collection, limit, timeout) -> {
            if (armed.get()) throw new NoClassDefFoundError("simulated: a class the census needs is gone");
            return censusOf(1, Map.of());
        };
        ChunkReaper r = reaper(Settings.defaults(), dies, System::nanoTime, t);
        assertThat(r.lastCompletedPassAt()).as("before the first pass").isNull();

        RunResult failed = r.runOnce(Duration.ZERO);       // must return, not throw

        assertThat(failed.tenants()).isEmpty();
        assertThat(r.failedPassesTotal()).isEqualTo(1);
        assertThat(r.lastCompletedPassAt()).as("a pass that died is not a completed pass").isNull();

        armed.set(false);
        RunResult next = r.runOnce(Duration.ZERO);
        assertThat(next.tenant(t).collection(c).moved()).as("the reaper is still alive").isEqualTo(1);
        assertThat(r.lastCompletedPassAt()).isEqualTo(CLOCK.instant());
        assertThat(r.failedPassesTotal()).isEqualTo(1);
    }

    @Test
    void anErrorFromTheTenantListIsAFailedPassToo() {
        ChunkReaper r = new ChunkReaper(store, vectors, repo, gate,
            () -> { throw new StackOverflowError("simulated"); }, Settings.defaults(), CLOCK);

        RunResult failed = r.runOnce(null);

        assertThat(failed.tenants()).isEmpty();
        assertThat(r.failedPassesTotal()).isEqualTo(1);
        assertThat(r.lastCompletedPassAt()).isNull();
    }

    @Test
    void aPassThatCompletesStampsItsTime_evenWhenItMovedNothing() throws Exception {
        // A candidates=0 pass writes no gc_audit row; this stamp is the only trace that the reaper is alive.
        String t = newTenant();
        openGate(t);
        ChunkReaper r = reaper(t);
        assertThat(r.lastCompletedPassAt()).isNull();

        r.runOnce(null);

        assertThat(r.lastCompletedPassAt()).isEqualTo(CLOCK.instant());
    }

    // ── a pass that ran to the end but did nothing useful (nexus-wbfpw.55 round 2, critique S4a) ──────────────

    @Test
    void aPassThatRefusedItsOnlyTenantIsStillACompletedPass_andSaysEveryTenantWasRefused() throws Exception {
        // lastCompletedPassAt moves for any pass that reaches the end, so a reaper whose every tenant is refused
        // (BACKFILL_INCOMPLETE until the rung runs) read as healthy. The pass summary tells them apart.
        String t = newTenant();   // gate NOT opened: the tenant is refused
        orphan(t, col("knowledge"), "x");
        ChunkReaper r = reaper(t);
        assertThat(r.lastPass()).as("before the first pass").isNull();

        r.runOnce(Duration.ZERO);

        assertThat(r.lastCompletedPassAt()).isEqualTo(CLOCK.instant());
        assertThat(r.lastPass()).isNotNull();
        assertThat(r.lastPass().tenantsVisited()).isEqualTo(1);
        assertThat(r.lastPass().tenantsRefused()).isEqualTo(1);
        assertThat(r.lastPass().tenantsErrored()).isZero();
    }

    @Test
    void aPassWhoseEveryCollectionErroredCountsTheTenantAsErrored() throws Exception {
        // The v0.1.78 shape (a grants regression): each collection's statement throws, the pass still completes.
        String t = newTenant();
        openGate(t);
        orphan(t, col("knowledge"), "x");
        ChunkReaper.Census broken = (tenant, collection, limit, timeout) -> {
            throw new IllegalStateException("simulated: permission denied for table chunks");
        };
        ChunkReaper r = reaper(Settings.defaults(), broken, System::nanoTime, t);

        r.runOnce(Duration.ZERO);

        assertThat(r.lastCompletedPassAt()).as("it completed").isEqualTo(CLOCK.instant());
        assertThat(r.lastPass().tenantsVisited()).isEqualTo(1);
        assertThat(r.lastPass().tenantsErrored()).isEqualTo(1);
        assertThat(r.lastPass().tenantsRefused()).isZero();
    }

    @Test
    void aHealthyTenantBesideARefusedOneIsCountedOnItsOwn() throws Exception {
        String healthy = newTenant();
        openGate(healthy);
        orphan(healthy, col("knowledge"), "x");
        String refused = newTenant();   // gate not opened
        orphan(refused, col("knowledge"), "y");
        ChunkReaper r = reaper(healthy, refused);

        r.runOnce(Duration.ZERO);

        assertThat(r.lastPass().tenantsVisited()).isEqualTo(2);
        assertThat(r.lastPass().tenantsRefused()).isEqualTo(1);
        assertThat(r.lastPass().tenantsErrored()).isZero();
    }

    @Test
    void aFailedPassLeavesTheLastCompletedPassSummaryAlone() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        orphan(t, c, "x");
        var armed = new java.util.concurrent.atomic.AtomicBoolean(false);
        ChunkReaper.Census dies = (tenant, collection, limit, timeout) -> {
            if (armed.get()) throw new NoClassDefFoundError("simulated");
            return censusOf(1, Map.of());
        };
        ChunkReaper r = reaper(Settings.defaults(), dies, System::nanoTime, t);
        r.runOnce(Duration.ZERO);
        var summary = r.lastPass();
        assertThat(summary.tenantsVisited()).isEqualTo(1);

        armed.set(true);
        r.runOnce(Duration.ZERO);

        assertThat(r.lastPass()).as("a pass that died is not a completed pass").isEqualTo(summary);
    }

    @Test
    void aCensusThatReadFarMoreThanTheDryRun_proceeds_butWarns() throws Exception {
        // M6 is shrink-only, so growth no longer refuses; but a census that read a different, much larger set is
        // the shape of a wrong scope that happens to be bigger, and must not pass in silence.
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        ChunkReaper.Census muchLarger = (tenant, collection, limit, timeout) -> censusOf(500, Map.of());

        var run = new RunResult[1];
        List<String> logs = captureLogsQuietly(() ->
            run[0] = reaper(Settings.defaults(), muchLarger, System::nanoTime, t).runOnce(Duration.ZERO));

        assertThat(run[0].tenant(t).collection(c).moved()).as("growth still proceeds").isEqualTo(1);
        assertThat(inCollection(t, quarantineOf(c), h)).isTrue();
        assertThat(logs).anySatisfy(l -> assertThat(l).startsWith("WARN event=reaper_census_scope_much_larger")
            .contains("dry_run_total=1").contains("census_total=500"));
    }

    /**
     * nexus-wbfpw.60: the REAL census must read the same chunk count the dry run does when one chash is named by many
     * own-collection manifest rows (identical text shared by many documents, by design). It once counted join rows,
     * so a collection of 2 chunks read 151: a false "much larger" warning on every pass, and, worse, a surplus that
     * could cancel a genuine shortfall against the scope-below-dry-run refusal.
     */
    @Test
    void aChashNamedByManyManifestRows_isOneChunkToTheCensus_noFalseMuchLargerWarning() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        String shared = orphan(t, c, "shared-by-many");
        int owners = 150;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            byte[] chash = Chash.fromHex(shared).toBytes();
            for (int i = 0; i < owners; i++) {
                String doc = "wbfpw60-owner-" + i;
                ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                        CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                    .values(t, doc, "Owner " + doc, c).execute();
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                        CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                        CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                    .values(t, doc, 0, chash, c).execute();
            }
        }
        assertThat(countIn(t, c)).as("two chunks, however many manifest rows name one of them").isEqualTo(2);
        assertThat(vectors.manifestLessCensusBounded(t, c, 1, 0, Duration.ofSeconds(60)).scopeChunkTotal())
            .as("scope counts chunks, not chunk x own-manifest-row join rows").isEqualTo(2L);
        assertThat(store.probe(t, c, Duration.ZERO, 60_000).total())
            .as("and it agrees with the dry run's own count").isEqualTo(2L);

        var run = new RunResult[1];
        List<String> logs = captureLogsQuietly(() ->
            run[0] = reaper(Settings.defaults(), t).runOnce(Duration.ZERO));

        assertThat(run[0].tenant(t).collection(c).moved()).isEqualTo(1);
        assertThat(inCollection(t, quarantineOf(c), h)).isTrue();
        assertThat(logs).noneSatisfy(l -> assertThat(l)
            .containsAnyOf("reaper_census_scope_much_larger", "reaper_census_scope_grew"));
    }

    @Test
    void aCensusThatGrewByALittle_doesNotWarn() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        orphan(t, c, "x");
        ChunkReaper.Census slightlyLarger = (tenant, collection, limit, timeout) -> censusOf(2, Map.of());

        List<String> logs = captureLogsQuietly(() ->
            reaper(Settings.defaults(), slightlyLarger, System::nanoTime, t).runOnce(Duration.ZERO));

        assertThat(logs).noneSatisfy(l -> assertThat(l).contains("reaper_census_scope_much_larger"));
    }

    private List<String> captureLogsQuietly(Runnable body) throws Exception {
        try {
            return captureLogs(body::run);
        } catch (Exception | Error e) {
            throw e;
        } catch (Throwable t) {
            throw new IllegalStateException(t);
        }
    }

    @Test
    void aCensusCancelledByItsStatementBoundIsARefusal_notAZero() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        ChunkReaper.Census cancelled = (tenant, collection, limit, timeout) -> {
            throw new org.jooq.exception.DataAccessException("canceling statement due to statement timeout",
                new java.sql.SQLException("canceling statement due to statement timeout", "57014"));
        };

        CollectionResult cr = reaper(Settings.defaults(), cancelled, System::nanoTime, t).runOnce(Duration.ZERO)
            .tenant(t).collection(c);

        assertThat(cr.refusal()).isEqualTo(Refusal.CENSUS_TIMED_OUT);
        assertThat(cr.moved()).isZero();
        assertThat(inCollection(t, c, h)).isTrue();
        assertThat(refusedRows(t)).singleElement().satisfies(a -> assertThat(a.details()).contains("CENSUS_TIMED_OUT"));
    }

    @Test
    void theCensusStatementIsReallyBounded_aOneMillisecondBoundCancelsItWithSqlState57014() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        bulkFast(t, c, 2000, 1000, i -> Map.of());

        org.assertj.core.api.Assertions.assertThatThrownBy(
                () -> vectors.manifestLessCensusBounded(t, c, 1, 0, Duration.ofMillis(1)))
            .satisfies(e -> {
                Throwable x = e;
                String state = null;
                for (int i = 0; x != null && i < 32; i++, x = x.getCause()) {
                    if (x instanceof java.sql.SQLException se) state = se.getSQLState();
                }
                assertThat(state).as("statement_timeout cancel").isEqualTo("57014");
            });
        assertThat(vectors.manifestLessCensusBounded(t, c, 1, 0, Duration.ofSeconds(60)).scopeChunkTotal())
            .as("and an adequate bound reads the whole collection").isEqualTo(2000);
    }

    // ── wall clock ───────────────────────────────────────────────────────────

    private static String colNamed(String prefix, int n, String tag) {
        return prefix + "__wc" + n + tag + "__minilm-l6-v2-384__v1";
    }

    private static Settings budgetOf(Duration budget) {
        return new Settings(true, Duration.ofHours(1), 300, 0.25, 100, budget, Duration.ofSeconds(60));
    }

    @Test
    void aWallClockCutInsideTheLastTenantIsReported_asOnASingleTenantLocalInstall() throws Throwable {
        String t = newTenant();
        openGate(t);
        int n = seq.incrementAndGet();
        String first = colNamed("knowledge", n, "a");
        String second = colNamed("knowledge", n, "b");
        String hFirst = orphan(t, first, "x");
        String hSecond = orphan(t, second, "x");
        java.util.concurrent.atomic.AtomicLong fakeNanos = new java.util.concurrent.atomic.AtomicLong();
        // The first collection's census takes 20 s of a 10 s budget.
        ChunkReaper.Census slow = (tenant, collection, limit, timeout) -> {
            fakeNanos.addAndGet(Duration.ofSeconds(20).toNanos());
            return vectors.manifestLessCensusBounded(tenant, collection, limit, 0, timeout);
        };

        RunResult[] run = new RunResult[1];
        List<String> logs = captureLogs(() ->
            run[0] = reaper(budgetOf(Duration.ofSeconds(10)), slow, fakeNanos::get, t).runOnce(Duration.ZERO));

        assertThat(run[0].wallClockCut()).as("a cut inside the only tenant is a cut").isTrue();
        assertThat(run[0].tenant(t).wallClockCut()).isTrue();
        assertThat(run[0].tenant(t).collection(first).moved()).as("the first collection was done").isEqualTo(1);
        assertThat(run[0].tenant(t).collection(second)).as("the second was not visited").isNull();
        assertThat(inCollection(t, first, hFirst)).isFalse();
        assertThat(inCollection(t, second, hSecond)).isTrue();
        assertThat(logs).anyMatch(l -> l.contains("event=reaper_pass") && l.contains("tenant=" + t)
            && l.contains("wall_clock_cut=true"));
        assertThat(logs).anyMatch(l -> l.contains("event=reaper_run") && l.contains("wall_clock_cut=true"));
    }

    @Test
    void aWallClockCutAtATenantBoundaryLeavesTheLaterTenantsUnvisited() throws Exception {
        String t1 = newTenant();
        String t2 = newTenant();
        openGate(t1);
        openGate(t2);
        String c1 = col("knowledge");
        String c2 = col("knowledge");
        orphan(t1, c1, "x");
        String h2 = orphan(t2, c2, "x");
        java.util.concurrent.atomic.AtomicLong fakeNanos = new java.util.concurrent.atomic.AtomicLong();
        ChunkReaper.Census slow = (tenant, collection, limit, timeout) -> {
            fakeNanos.addAndGet(Duration.ofSeconds(20).toNanos());
            return vectors.manifestLessCensusBounded(tenant, collection, limit, 0, timeout);
        };

        RunResult run = reaper(budgetOf(Duration.ofSeconds(10)), slow, fakeNanos::get, t1, t2).runOnce(Duration.ZERO);

        assertThat(run.wallClockCut()).isTrue();
        assertThat(run.tenant(t1)).isNotNull();
        assertThat(run.tenant(t2)).as("never reached").isNull();
        assertThat(inCollection(t2, c2, h2)).isTrue();
    }

    /** Code L1: the supplier's order is the database's (SELECT DISTINCT has no ORDER BY); the reaper sorts. */
    @Test
    void tenantsAreVisitedInSortedOrder_whateverOrderTheSupplierReturnsThem() throws Exception {
        String z = "zz" + seq.incrementAndGet();
        String a = "aa" + seq.incrementAndGet();
        openGate(z);
        openGate(a);
        String cz = col("knowledge");
        String ca = col("knowledge");
        orphan(z, cz, "x");
        orphan(a, ca, "x");
        java.util.concurrent.atomic.AtomicLong fakeNanos = new java.util.concurrent.atomic.AtomicLong();
        ChunkReaper.Census slow = (tenant, collection, limit, timeout) -> {
            fakeNanos.addAndGet(Duration.ofSeconds(20).toNanos());
            return vectors.manifestLessCensusBounded(tenant, collection, limit, 0, timeout);
        };

        RunResult run = reaper(budgetOf(Duration.ofSeconds(10)), slow, fakeNanos::get, z, a).runOnce(Duration.ZERO);

        assertThat(run.wallClockCut()).isTrue();
        assertThat(run.tenant(a)).as("aa sorts first, so it is the one visited before the cut").isNotNull();
        assertThat(run.tenant(z)).as("zz is the one left for the next pass").isNull();
    }

    // ── refusals are durable ─────────────────────────────────────────────────

    private static final com.fasterxml.jackson.databind.ObjectMapper JSON = new com.fasterxml.jackson.databind.ObjectMapper();

    @Test
    void aFloorRefusalWritesOneAuditRowWithASample_andNotAnotherUntilTheStateChanges() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        // 100 reapable of 300, each with a title and a source path the operator can recognise.
        bulkFast(t, c, 300, 200, i -> Map.of("title", "Doc " + i, "source_path", "/src/file" + i + ".md"));
        ChunkReaper r = reaper(t);

        r.runOnce(Duration.ZERO);
        r.runOnce(Duration.ZERO);
        reaper(t).runOnce(Duration.ZERO);   // a fresh reaper (a restart) reads the state from gc_audit, not memory

        assertThat(refusedRows(t)).as("written once per state change, not once a pass").singleElement().satisfies(a -> {
            assertThat(a.actor()).isEqualTo(ChunkReaper.ACTOR);
            assertThat(a.collection()).isEqualTo(c);
            var d = readJson(a.details());
            assertThat(d.get("reason").asText()).isEqualTo("FLOOR_EXCEEDED");
            assertThat(d.get("candidates").asLong()).isEqualTo(100);
            assertThat(d.get("total").asLong()).isEqualTo(300);
            assertThat(d.get("sample")).as("up to five title/source_path rows").hasSize(5);
            for (var row : d.get("sample")) {
                assertThat(row.get("title").asText()).startsWith("Doc ");
                assertThat(row.get("source_path").asText()).startsWith("/src/file");
            }
        });

        // The state changes: the collection leaves `live`. A new reason is a new row.
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
               .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "dormant")
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t).and(CATALOG_COLLECTIONS.NAME.eq(c))).execute();
        }
        r.runOnce(Duration.ZERO);
        r.runOnce(Duration.ZERO);
        assertThat(refusedRows(t)).hasSize(2).last().satisfies(a ->
            assertThat(a.details()).contains("COLLECTION_NOT_LIVE"));
    }

    private static com.fasterxml.jackson.databind.JsonNode readJson(String json) {
        try {
            return JSON.readTree(json);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void aCensusRefusalAuditRowNamesTheLegacyChunksByTitleAndSourcePath() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String doc = "rp.legacy." + seq.incrementAndGet();
        registerNote(t, doc, c, null);
        orphan(t, c, "legacy", Map.of("catalog_doc_id", doc, "title", "Legacy note", "source_path", "/notes/legacy.md"));
        orphan(t, c, "debris");

        reaper(t).runOnce(Duration.ZERO);

        assertThat(refusedRows(t)).singleElement().satisfies(a -> {
            var d = readJson(a.details());
            assertThat(d.get("reason").asText()).isEqualTo("CENSUS_LEGACY_UNMANIFESTED");
            assertThat(d.get("sample")).hasSize(1);
            assertThat(d.get("sample").get(0).get("title").asText()).isEqualTo("Legacy note");
            assertThat(d.get("sample").get(0).get("source_path").asText()).isEqualTo("/notes/legacy.md");
        });
    }

    @Test
    void aTenantRefusalIsAuditedOncePerStateToo() throws Exception {
        String t = newTenant();   // no backfill record
        ChunkReaper r = reaper(t);

        r.runOnce(Duration.ZERO);
        r.runOnce(Duration.ZERO);

        assertThat(refusedRows(t)).singleElement().satisfies(a -> {
            assertThat(a.collection()).isEmpty();
            assertThat(a.details()).contains("BACKFILL_INCOMPLETE");
        });
        assertThat(r.refusedTotal()).as("counted every time, audited once").isEqualTo(2);
    }

    // ── a busy gate and a lock timeout are skips, not refusals ───────────────

    @Test
    void aBusyGateIsCountedOnItsOwn_notAsARefusal_andIsNeverAudited() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        orphan(t, c, "x");
        ChunkReaper r = reaper(t);
        try (Connection writer = pg.createConnection("")) {
            writer.setAutoCommit(false);
            DSL.using(writer, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock_shared",
                SQLDataType.OTHER, DSL.function("hashtext", SQLDataType.INTEGER,
                    DSL.val("sweepgate:" + t + "/" + c)))).execute();
            assertThat(r.runOnce(Duration.ZERO).tenant(t).collection(c).refusal()).isEqualTo(Refusal.GATE_BUSY);
            writer.rollback();
        }

        assertThat(r.gateBusyTotal()).isEqualTo(1);
        assertThat(r.lockTimeoutTotal()).isZero();
        assertThat(r.refusedTotal()).as("routine contention is not a refusal").isZero();
        assertThat(refusedRows(t)).isEmpty();
    }

    @Test
    void aRowLockTimeoutIsLabelledAsOne_notAsABusyGate() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String raced = orphan(t, c, "raced");
        ChunkReaper r = reaper(t);
        try (Connection writer = svcDs.getConnection()) {
            writer.setAutoCommit(false);
            PgContainerHelper.setTenant(writer, TenantScope.DEFAULT_TENANT_GUC, t, true);
            // A client holding an uncommitted refresh of the chunk for longer than the move's 2 s lock bound.
            DSL.using(writer, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now())
               .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(c))
                      .and(CHUNKS.CHASH.eq(Chash.fromHex(raced).toBytes()))).execute();

            CollectionResult cr = r.runOnce(Duration.ZERO).tenant(t).collection(c);

            assertThat(cr.refusal()).isEqualTo(Refusal.LOCK_TIMEOUT);
            writer.rollback();
        }
        assertThat(r.lockTimeoutTotal()).isEqualTo(1);
        assertThat(r.gateBusyTotal()).as("the gate was free; a row lock was not").isZero();
        assertThat(r.refusedTotal()).isZero();
        assertThat(inCollection(t, c, raced)).isTrue();
    }

    // ── the move stamps whole seconds ────────────────────────────────────────

    @Test
    void quarantinedAtIsWholeSeconds_theShapeGcExpireQuarantineCompares() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        Clock fractional = Clock.fixed(Instant.parse("2026-10-01T12:00:00.987654321Z"), ZoneOffset.UTC);
        ChunkReaper r = new ChunkReaper(store, vectors, repo, gate, () -> List.of(t), Settings.defaults(), fractional);

        assertThat(r.runOnce(Duration.ZERO).tenant(t).collection(c).moved()).isEqualTo(1);

        try (Connection su = pg.createConnection("")) {
            String stamp = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "quarantined_at")).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(quarantineOf(c)))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(h).toBytes())))
                .fetchOne(0, String.class);
            assertThat(stamp).isEqualTo("2026-10-01T12:00:00Z");
        }
    }

    // ── the pass expires the quarantine it fills ─────────────────────────────

    /**
     * {@code n} chunks in the quarantine sibling of {@code origin}, each stamped {@code ageDays} before CLOCK and
     * tagged as the reaper's own move writes them.
     */
    private List<String> quarantined(String tenant, String origin, String tag, int n, int ageDays,
                                     boolean registerOrigin) throws Exception {
        return quarantined(tenant, origin, tag, n, ageDays, registerOrigin, true);
    }

    private List<String> quarantined(String tenant, String origin, String tag, int n, int ageDays,
                                     boolean registerOrigin, boolean byReaper) throws Exception {
        String stamp = CLOCK.instant().minus(Duration.ofDays(ageDays)).truncatedTo(java.time.temporal.ChronoUnit.SECONDS)
            .toString();
        return quarantinedAt(tenant, origin, tag, n, stamp, registerOrigin, byReaper);
    }

    /** As above with an exact {@code quarantined_at} stamp; {@code byReaper} adds the tag the engine's move writes. */
    private List<String> quarantinedAt(String tenant, String origin, String tag, int n, String stamp,
                                       boolean registerOrigin, boolean byReaper) throws Exception {
        String q = quarantineOf(origin);
        List<String> hexes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<float[]> vecs = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            hexes.add(Chash.ofText(q + "/" + tag + i).toHex());
            texts.add(tag + i);
            vecs.add(new float[384]);
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("quarantined_at", stamp);
            m.put("origin_collection", origin);
            if (byReaper) {
                m.put("quarantined_by", "engine-reaper");
                m.put("reaper_quarantined_at", stamp);
            }
            metas.add(m);
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            if (registerOrigin) PgContainerHelper.insertCollection(ctx, tenant, origin);
            PgContainerHelper.insertCollection(ctx, tenant, q);
            PgContainerHelper.insertChunks(ctx, tenant, q, hexes, texts, vecs, metas);
        }
        return hexes;
    }

    @Test
    void aPassExpiresTheChunksItMovedOnceTheyAreOlderThan14Days_andKeepsTheRest_andTheExpiryIsAudited()
            throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        List<String> old = quarantined(t, origin, "old", 3, 15, true);
        List<String> recent = quarantined(t, origin, "recent", 2, 5, false);

        ChunkReaper.TenantResult result = reaper(t).runOnce(null).tenant(t);

        assertThat(result.expiry(quarantineOf(origin)).expired()).isEqualTo(3);
        for (String h : old) assertThat(inCollection(t, quarantineOf(origin), h)).as("15 days old: expired").isFalse();
        for (String h : recent) assertThat(inCollection(t, quarantineOf(origin), h)).as("5 days old: kept").isTrue();
        assertThat(auditRows(t, "reaper_expire_quarantine")).as("the expiry audits its own delete")
            .singleElement().satisfies(a -> {
                assertThat(a.actor()).isEqualTo(ChunkReaper.ACTOR);
                assertThat(a.collection()).isEqualTo(quarantineOf(origin));
                assertThat(a.chashCount()).isEqualTo(3);
            });
    }

    /** The retention boundary, pinned from both sides (code review I2: a 7 day retention left every test green). */
    @Test
    void theRetentionBoundary_13DaysIsKept_15DaysIsExpired_andTheBoundarySecondItselfIsExpired() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        String cutoff = CLOCK.instant().minus(Duration.ofDays(14)).toString();   // whole second: CLOCK is
        List<String> thirteen = quarantined(t, origin, "d13", 1, 13, true);
        List<String> fifteen = quarantined(t, origin, "d15", 1, 15, false);
        List<String> exactly = quarantinedAt(t, origin, "exact", 1, cutoff, false, true);
        List<String> oneSecondShort = quarantinedAt(t, origin, "short", 1,
            CLOCK.instant().minus(Duration.ofDays(14)).plusSeconds(1).toString(), false, true);

        ChunkReaper.TenantResult result = reaper(t).runOnce(null).tenant(t);

        assertThat(inCollection(t, q, thirteen.get(0))).as("13 days: kept").isTrue();
        assertThat(inCollection(t, q, fifteen.get(0))).as("15 days: expired").isFalse();
        assertThat(inCollection(t, q, exactly.get(0))).as("stamped exactly 14 days ago: expired (<=)").isFalse();
        assertThat(inCollection(t, q, oneSecondShort.get(0))).as("one second short of 14 days: kept").isTrue();
        assertThat(result.expiry(q).expired()).isEqualTo(2);
    }

    @Test
    void aChunkTheReaperMovedItself_isKeptAt13Days_andExpiredAtTheRetention_endToEndThroughTheRealMove()
            throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String q = quarantineOf(c);
        String h = orphan(t, c, "moved");
        assertThat(reaper(t).runOnce(Duration.ZERO).tenant(t).collection(c).moved()).isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            var m = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "quarantined_by"),
                        DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "reaper_quarantined_at"),
                        DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "quarantined_at"))
                .from(CHUNKS).where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(q))
                    .and(CHUNKS.CHASH.eq(Chash.fromHex(h).toBytes()))).fetchOne();
            assertThat(m.value1()).as("the move tags what it moves").isEqualTo("engine-reaper");
            assertThat(m.value2()).isEqualTo(m.value3()).isEqualTo(CLOCK.instant().toString());
        }

        ChunkReaper at13 = reaperAt(Instant.parse("2026-10-14T12:00:00Z"), Settings.defaults(), t);
        ChunkReaper at14Short = reaperAt(Instant.parse("2026-10-15T11:59:59Z"), Settings.defaults(), t);
        ChunkReaper at14 = reaperAt(Instant.parse("2026-10-15T12:00:00Z"), Settings.defaults(), t);

        assertThat(at13.runOnce(Duration.ZERO).tenant(t).expiry(q).expired()).isZero();
        assertThat(inCollection(t, q, h)).as("13 days after the move").isTrue();
        assertThat(at14Short.runOnce(Duration.ZERO).tenant(t).expiry(q).expired()).isZero();
        assertThat(inCollection(t, q, h)).as("one second short of 14 days").isTrue();
        assertThat(at14.runOnce(Duration.ZERO).tenant(t).expiry(q).expired()).isEqualTo(1);
        assertThat(inCollection(t, q, h)).as("14 days after the move").isFalse();
    }

    private ChunkReaper reaperAt(Instant now, Settings s, String... tenants) {
        return new ChunkReaper(store, vectors, repo, gate, () -> List.of(tenants), s,
            Clock.fixed(now, ZoneOffset.UTC));
    }

    @Test
    void quarantineAClientFilled_isNeverExpiredByTheEngine_evenAt30Days() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        // nx index repo / nx t3 gc moved these: quarantined_at and origin_collection, and no engine tag. The
        // client expires them on its own run with NX_GC_QUARANTINE_DAYS; the engine must not.
        List<String> clientMoved = quarantined(t, origin, "client", 3, 30, true, false);
        ChunkReaper r = reaper(t);

        ChunkReaper.TenantResult result = r.runOnce(null).tenant(t);

        assertThat(result.expiry(q).expired()).isZero();
        assertThat(result.expiry(q).refusal()).isNull();
        assertThat(result.expiry(q).protectedCount()).isZero();
        for (String h : clientMoved) assertThat(inCollection(t, q, h)).as("30 days old, client-moved: kept").isTrue();
        assertThat(auditRows(t, "reaper_expire_quarantine")).isEmpty();
        assertThat(r.refusedTotal()).isZero();
    }

    @Test
    void aTagLeftOverFromTheReaper_onAChunkAClientMovedAgain_doesNotMakeItTheEnginesToExpire() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        // The reaper moved it 40 days ago, a restore stripped quarantined_at and origin_collection but not the
        // tag, and a client's quarantine then moved it again 30 days ago: a NEW quarantined_at, with the old
        // quarantined_by and reaper_quarantined_at still on the row.
        String reaperStamp = CLOCK.instant().minus(Duration.ofDays(40)).toString();
        String clientStamp = CLOCK.instant().minus(Duration.ofDays(30)).toString();
        String chash = Chash.ofText(q + "/stale").toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, t, origin);
            PgContainerHelper.insertCollection(ctx, t, q);
            PgContainerHelper.insertChunks(ctx, t, q, List.of(chash), List.of("stale tag"),
                List.of(new float[384]), List.of(Map.of("quarantined_at", clientStamp,
                    "origin_collection", origin, "quarantined_by", "engine-reaper",
                    "reaper_quarantined_at", reaperStamp)));
        }
        List<String> hex = List.of(chash);

        ChunkReaper.TenantResult result = reaper(t).runOnce(null).tenant(t);

        assertThat(result.expiry(q).expired()).isZero();
        assertThat(inCollection(t, q, hex.get(0))).as("the stamps differ: not the engine's any more").isTrue();
    }

    @Test
    void theRetentionIsASetting() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        List<String> fifteen = quarantined(t, origin, "d15", 1, 15, true);
        List<String> thirtyOne = quarantined(t, origin, "d31", 1, 31, false);
        Settings sixty = new Settings(true, Duration.ofHours(1), 300, 0.25, 100, Duration.ofMinutes(10),
            Duration.ofSeconds(60), Duration.ofDays(30), java.util.Set.of());

        reaper(sixty, t).runOnce(null);

        assertThat(inCollection(t, q, fifteen.get(0))).as("inside a 30 day window").isTrue();
        assertThat(inCollection(t, q, thirtyOne.get(0))).as("outside it").isFalse();
    }

    /**
     * Sam, 2026-10-01: engine expiry has no fraction floor. 150 of 200 tagged chunks past the cutoff is what the
     * old floor refused every hour forever; it must expire, audited, with no refusal anywhere.
     */
    @Test
    void aMassExpiryOfTaggedChunksIsNotRefused_thereIsNoExpiryFloor() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        List<String> old = quarantined(t, origin, "old", 150, 15, true);
        List<String> recent = quarantined(t, origin, "recent", 50, 1, false);
        ChunkReaper r = reaper(t);

        ChunkReaper.TenantResult result = r.runOnce(null).tenant(t);

        assertThat(result.expiry(q).expired()).isEqualTo(150);
        assertThat(result.expiry(q).refusal()).isNull();
        assertThat(countIn(t, q)).as("the 50 recent chunks stay").isEqualTo(50);
        for (String h : old) assertThat(inCollection(t, q, h)).isFalse();
        for (String h : recent) assertThat(inCollection(t, q, h)).isTrue();
        assertThat(refusedRows(t)).isEmpty();
        assertThat(r.refusedTotal()).isZero();
        assertThat(auditRows(t, "reaper_expire_quarantine")).singleElement().satisfies(a ->
            assertThat(a.chashCount()).isEqualTo(150));
    }

    /**
     * The critique's S1 / code M1 test: a drain carried ALL THE WAY THROUGH to expiry. A collection that is 70%
     * garbage is drained through the named move-floor exemption in three passes; fifteen days later the same
     * engine expires every moved chunk in one pass, with nothing refused, and the origin keeps what it owns.
     */
    @Test
    void aDrainCarriesAllTheWayThroughToExpiry_nothingWedgesAndEveryMovedChunkIsAudited() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String q = quarantineOf(c);
        bulkFast(t, c, 1000, 300, i -> Map.of());   // 700 reapable of 1000, drained through the exemption
        ChunkReaper drain = reaper(exempting(c), t);
        assertThat(drain.runOnce(Duration.ZERO).tenant(t).collection(c).moved()).isEqualTo(300);
        assertThat(drain.runOnce(Duration.ZERO).tenant(t).collection(c).moved()).isEqualTo(300);
        assertThat(drain.runOnce(Duration.ZERO).tenant(t).collection(c).moved()).isEqualTo(100);
        assertThat(countIn(t, q)).as("all 700 are in quarantine, tagged by the move").isEqualTo(700);
        Instant fifteenDaysLater = CLOCK.instant().plus(Duration.ofDays(15));
        ChunkReaper later = reaperAt(fifteenDaysLater, exempting(c), t);

        ChunkReaper.TenantResult result = later.runOnce(Duration.ZERO).tenant(t);

        assertThat(result.expiry(q).expired()).as("one pass takes all 700: no floor, no wedge").isEqualTo(700);
        assertThat(result.expiry(q).refusal()).isNull();
        assertThat(countIn(t, q)).isZero();
        assertThat(countIn(t, c)).as("what the origin owns is untouched").isEqualTo(300);
        assertThat(later.refusedTotal()).isZero();
        assertThat(refusedRows(t)).isEmpty();
        assertThat(auditRows(t, "reaper_expire_quarantine")).singleElement().satisfies(a -> {
            assertThat(a.actor()).isEqualTo(ChunkReaper.ACTOR);
            assertThat(a.chashCount()).isEqualTo(700);
        });
    }

    /**
     * Critique S6: the engine reads a chunk's origin from the chunk's own {@code origin_collection} tag. A sibling
     * whose name is not {@code quarantine-<origin>} (the client builds names from the origin's catalog row, which
     * agrees with the name only for a conformant one) is still expired against the right origin's manifest.
     */
    @Test
    void theExpiryReadsTheOriginFromTheChunkTag_notFromTheSiblingsName() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String sibling = "quarantine-" + col("knowledge");   // NOT quarantine-<origin>
        String stamp = CLOCK.instant().minus(Duration.ofDays(15)).toString();
        String hex = Chash.ofText(sibling + "/odd").toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, t, origin);
            PgContainerHelper.insertCollection(ctx, t, sibling);
            PgContainerHelper.insertChunks(ctx, t, sibling, List.of(hex), List.of("odd"), List.of(new float[384]),
                List.of(Map.of("quarantined_at", stamp, "origin_collection", origin,
                    "quarantined_by", "engine-reaper", "reaper_quarantined_at", stamp)));
        }

        ChunkReaper.TenantResult result = reaper(t).runOnce(null).tenant(t);

        assertThat(result.expiry(sibling).expired()).isEqualTo(1);
        assertThat(inCollection(t, sibling, hex)).isFalse();
    }

    /** Code L7: one expiry call is bounded; the rest wait for the next pass. */
    @Test
    void oneExpiryCallDeletesAtMostItsRowLimit_theRestWaitForTheNextCall() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        quarantined(t, origin, "old", 5, 15, true);
        String cutoff = CLOCK.instant().minus(Duration.ofDays(14)).toString();

        assertThat(store.expire(t, q, origin, cutoff, 2, 25_000, 2_000).expired()).isEqualTo(2);
        assertThat(store.expire(t, q, origin, cutoff, 2, 25_000, 2_000).expired()).isEqualTo(2);
        assertThat(store.expire(t, q, origin, cutoff, 2, 25_000, 2_000).expired()).isEqualTo(1);
        assertThat(store.expire(t, q, origin, cutoff, 2, 25_000, 2_000).expired()).isZero();
        assertThat(countIn(t, q)).isZero();
    }

    // ── the expiry DELETE re-checks every condition against the row's NEW version (code M3) ──

    /**
     * Run {@code store.expire} while a writer holds an uncommitted UPDATE on the one tagged, aged chunk (so the
     * expiry's DELETE blocks on the row lock), then commit the writer: READ COMMITTED re-evaluates the DELETE's
     * WHERE against the row's new version. Asserts the DELETE really was blocked (non-vacuity) and returns the
     * outcome.
     */
    private ReaperRepository.Expiry expireWhileAWriterHolds(
            String t, String origin, String hex,
            java.util.function.Consumer<Map<String, Object>> mutateMetadata) throws Exception {
        String cutoff = CLOCK.instant().minus(Duration.ofDays(14)).toString();
        return whileAWriterHolds(t, origin, hex, mutateMetadata,
            () -> store.expire(t, quarantineOf(origin), origin, cutoff, 100, 25_000, 5_000));
    }

    /** {@link #expireWhileAWriterHolds} for any call that deletes from the sibling. */
    private <T> T whileAWriterHolds(String t, String origin, String hex,
                                    java.util.function.Consumer<Map<String, Object>> mutateMetadata,
                                    java.util.concurrent.Callable<T> call) throws Exception {
        String q = quarantineOf(origin);
        java.util.concurrent.ExecutorService pool = java.util.concurrent.Executors.newSingleThreadExecutor();
        try (Connection writer = svcDs.getConnection()) {
            writer.setAutoCommit(false);
            PgContainerHelper.setTenant(writer, TenantScope.DEFAULT_TENANT_GUC, t, true);
            DSLContext w = DSL.using(writer, SQLDialect.POSTGRES);
            var rowIs = CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(q))
                .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()));
            org.jooq.JSONB current = w.select(CHUNKS.METADATA).from(CHUNKS).where(rowIs).fetchOne(CHUNKS.METADATA);
            Map<String, Object> metadata = JSON.readValue(current.data(),
                new com.fasterxml.jackson.core.type.TypeReference<LinkedHashMap<String, Object>>() {});
            mutateMetadata.accept(metadata);
            w.update(CHUNKS).set(CHUNKS.METADATA, org.jooq.JSONB.jsonb(JSON.writeValueAsString(metadata)))
                .where(rowIs).execute();
            java.util.concurrent.Future<T> pending = pool.submit(call);
            Thread.sleep(800);
            assertThat(pending.isDone()).as("the DELETE is parked on the writer's row lock, not finished").isFalse();
            writer.commit();
            return pending.get(20, java.util.concurrent.TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void expiryRace_controlAWriterThatChangesNothingTheDeleteChecks_theChunkIsStillDeleted() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String hex = quarantined(t, origin, "x", 1, 15, true).get(0);

        var out = expireWhileAWriterHolds(t, origin, hex, m -> m.put("note", "x"));

        assertThat(out.expired()).as("the blocked DELETE proceeds after the commit").isEqualTo(1);
        assertThat(inCollection(t, quarantineOf(origin), hex)).isFalse();
    }

    @Test
    void expiryRace_aWriterThatRemovesTheTag_theChunkSurvives() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String hex = quarantined(t, origin, "x", 1, 15, true).get(0);

        var out = expireWhileAWriterHolds(t, origin, hex, m -> m.remove("quarantined_by"));

        assertThat(out.expired()).isZero();
        assertThat(inCollection(t, quarantineOf(origin), hex)).as("no longer the engine's").isTrue();
    }

    @Test
    void expiryRace_aWriterThatMakesTheStampsDisagree_theChunkSurvives() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String hex = quarantined(t, origin, "x", 1, 15, true).get(0);

        var out = expireWhileAWriterHolds(t, origin, hex, m -> m.put("reaper_quarantined_at", "2026-09-30T00:00:00Z"));

        assertThat(out.expired()).isZero();
        assertThat(inCollection(t, quarantineOf(origin), hex)).isTrue();
    }

    @Test
    void expiryRace_aWriterThatReMovesTheChunkAfterTheCutoff_theChunkSurvives() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String hex = quarantined(t, origin, "x", 1, 15, true).get(0);
        String fresh = CLOCK.instant().toString();   // both stamps move together, as a reaper re-move writes them

        var out = expireWhileAWriterHolds(t, origin, hex, m -> {
            m.put("quarantined_at", fresh);
            m.put("reaper_quarantined_at", fresh);
        });

        assertThat(out.expired()).isZero();
        assertThat(inCollection(t, quarantineOf(origin), hex)).as("inside the retention window again").isTrue();
    }

    @Test
    void expiryRace_aWriterThatChangesTheOrigin_theChunkSurvives() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String hex = quarantined(t, origin, "x", 1, 15, true).get(0);

        String elsewhere = col("knowledge");
        var out = expireWhileAWriterHolds(t, origin, hex, m -> m.put("origin_collection", elsewhere));

        assertThat(out.expired()).isZero();
        assertThat(inCollection(t, quarantineOf(origin), hex)).isTrue();
    }

    // ── the symmetric split: each side expires only what it moved (Sam, 2026-10-01) ──────

    @Test
    void eachSideExpiresOnlyWhatItMoved_theClientForceFlagCannotReachTheEnginesRows() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        List<String> engineMoved = quarantined(t, origin, "engine", 3, 40, true, true);
        List<String> clientMoved = quarantined(t, origin, "client", 3, 40, false, false);
        String clientCutoff = CLOCK.instant().minus(Duration.ofDays(30)).toString();

        // The client's expiry, force and all: takes only the untagged rows.
        var client = vectors.expireQuarantine(t, q, origin, clientCutoff, 0.25, 100, true);

        assertThat(client.expired()).isEqualTo(3);
        for (String h : clientMoved) assertThat(inCollection(t, q, h)).as("client-moved: the client's to expire").isFalse();
        for (String h : engineMoved) assertThat(inCollection(t, q, h)).as("engine-moved: skipped by the client").isTrue();

        // The engine's expiry: takes the tagged rows, and finds nothing else to take.
        ChunkReaper.TenantResult result = reaper(t).runOnce(null).tenant(t);

        assertThat(result.expiry(q).expired()).isEqualTo(3);
        for (String h : engineMoved) assertThat(inCollection(t, q, h)).isFalse();
        assertThat(countIn(t, q)).isZero();
    }

    /**
     * The client's DELETE re-checks ownership too: a chunk the reaper (re-)moves between the client's read and its
     * delete has fresh tags and is the engine's by the time the client's DELETE gets the row lock.
     */
    @Test
    void theClientsDeleteRechecksOwnership_aChunkTheReaperTagsWhileItWaitsSurvives() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        String hex = quarantined(t, origin, "client", 1, 40, true, false).get(0);   // untagged: the client's to expire
        String cutoff = CLOCK.instant().minus(Duration.ofDays(30)).toString();

        var out = whileAWriterHolds(t, origin, hex, m -> {
            m.put("quarantined_by", "engine-reaper");
            m.put("reaper_quarantined_at", m.get("quarantined_at"));
        }, () -> vectors.expireQuarantine(t, q, origin, cutoff, 0.99, 100, false));

        assertThat(out.expired()).as("it was the client's when the client read it, the engine's by the delete").isZero();
        assertThat(inCollection(t, q, hex)).isTrue();
    }

    @Test
    void theClientsExpiryTreatsAStaleTagAsItsOwn_andItsFloorCountsOnlyItsOwnRows() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        // 9 engine-owned rows would hide a client mass-expiry under an all-rows denominator: 1 of 10 is 0.1, under
        // the floor; 1 of 1 client rows is 1.0, over it.
        quarantined(t, origin, "engine", 9, 40, true, true);
        List<String> clientOne = quarantined(t, origin, "client", 1, 40, false, false);
        String cutoff = CLOCK.instant().minus(Duration.ofDays(30)).toString();

        var refused = vectors.expireQuarantine(t, q, origin, cutoff, 0.5, 1, false);

        assertThat(refused.expired()).as("the floor judged the client's own 1 of 1").isZero();
        assertThat(refused.refused()).isEqualTo(1);
        assertThat(inCollection(t, q, clientOne.get(0))).isTrue();

        // A stale tag (the stamps disagree: a client moved it again after the engine did) is the client's.
        String stale = Chash.ofText(q + "/stale").toHex();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), t, q, List.of(stale), List.of("stale"),
                List.of(new float[384]), List.of(Map.of("quarantined_at", CLOCK.instant().minus(Duration.ofDays(35)).toString(),
                    "origin_collection", origin, "quarantined_by", "engine-reaper",
                    "reaper_quarantined_at", CLOCK.instant().minus(Duration.ofDays(50)).toString())));
        }

        var forced = vectors.expireQuarantine(t, q, origin, cutoff, 0.5, 1, true);

        assertThat(forced.expired()).as("the untagged row and the stale-tagged row").isEqualTo(2);
        assertThat(inCollection(t, q, stale)).isFalse();
        assertThat(countIn(t, q)).as("the 9 engine-owned rows are all that is left").isEqualTo(9);
    }

    /** SIG-3: a past-cutoff chunk a manifest row still names is benign, and must not read as a refusal. */
    @Test
    void anAgedChunkAManifestRowStillNames_isNeverExpired_andIsLabelledProtectedNotRefused() throws Throwable {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        String named = orphan(t, origin, "named");
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.ownChunks(ctx, t, origin, named);
            PgContainerHelper.insertCollection(ctx, t, q);
            PgContainerHelper.insertChunks(ctx, t, q, List.of(named), List.of("named text"),
                List.of(new float[384]), List.of(Map.of("quarantined_at", "2026-09-01T00:00:00Z",
                    "origin_collection", origin, "quarantined_by", "engine-reaper",
                    "reaper_quarantined_at", "2026-09-01T00:00:00Z")));
        }
        ChunkReaper r = reaper(t);

        ChunkReaper.TenantResult[] result = new ChunkReaper.TenantResult[1];
        List<String> logs = captureLogs(() -> result[0] = r.runOnce(null).tenant(t));

        assertThat(inCollection(t, q, named)).as("a chunk a live manifest row names is not hard-deleted").isTrue();
        assertThat(result[0].expiry(q).expired()).isZero();
        assertThat(result[0].expiry(q).protectedCount()).as("labelled on its own").isEqualTo(1);
        assertThat(result[0].expiry(q).refusal()).as("not a refusal").isNull();
        assertThat(result[0].expiryProtected()).isEqualTo(1);
        assertThat(result[0].refused()).isZero();
        assertThat(r.refusedTotal()).as("not counted in refused_total").isZero();
        assertThat(refusedRows(t)).as("no audit row").isEmpty();
        assertThat(logs).as("and no WARN every hour").noneMatch(l -> l.startsWith("WARN") && l.contains("reaper_expire_refused"));
        assertThat(logs).anyMatch(l -> l.contains("event=reaper_pass") && l.contains("expiry_protected=1"));
    }

    /** Code I2: the guard, not the dimension lookup, is what leaves an unregistered origin alone. */
    @Test
    void aSiblingWhoseOriginIsNotRegisteredIsLeftAlone_byTheReapersOwnGuard() throws Throwable {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        // quarantined(..., registerOrigin=false) never registers the origin here. The chunks are tagged and aged:
        // the expiry function itself would delete them (it needs no registered origin), so ONLY the guard saves them.
        List<String> old = quarantined(t, origin, "old", 3, 40, false);

        ChunkReaper.TenantResult[] result = new ChunkReaper.TenantResult[1];
        List<String> logs = captureLogs(() -> result[0] = reaper(t).runOnce(null).tenant(t));

        assertThat(result[0].expiry(quarantineOf(origin)).expired()).isZero();
        assertThat(inCollection(t, quarantineOf(origin), old.get(0))).isTrue();
        assertThat(countIn(t, quarantineOf(origin))).isEqualTo(3);
        assertThat(logs).anyMatch(l -> l.contains("event=reaper_expire_skipped") && l.contains("origin_not_registered"));
    }

    /** Code I2: removing the wall-clock check from the expiry loop left every test green. */
    @Test
    void theWallClockBudgetCutsTheExpiryLoop_theNextSiblingIsNotExpiredAndTheRunReportsTheCut() throws Exception {
        String t = newTenant();
        openGate(t);
        int n = seq.incrementAndGet();
        String originA = colNamed("knowledge", n, "a");
        String originB = colNamed("knowledge", n, "b");
        List<String> a = quarantined(t, originA, "a", 2, 15, true);
        List<String> b = quarantined(t, originB, "b", 2, 15, true);
        // Calls: (1) the run's deadline, (2) the tenant boundary, (3) before sibling A, (4) before sibling B.
        // The clock is 0 for the first three and past the 10 s budget from the fourth on.
        java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
        java.util.function.LongSupplier nanos = () ->
            calls.incrementAndGet() <= 3 ? 0L : Duration.ofSeconds(20).toNanos();

        ChunkReaper.Census real = (tenant, collection, limit, timeout) ->
            vectors.manifestLessCensusBounded(tenant, collection, limit, 0, timeout);

        RunResult run = reaper(budgetOf(Duration.ofSeconds(10)), real, nanos, t).runOnce(null);

        assertThat(run.wallClockCut()).isTrue();
        assertThat(run.tenant(t).expiries()).as("only the first sibling was reached").hasSize(1);
        assertThat(run.tenant(t).expiry(quarantineOf(originA)).expired()).isEqualTo(2);
        assertThat(inCollection(t, quarantineOf(originA), a.get(0))).isFalse();
        assertThat(inCollection(t, quarantineOf(originB), b.get(0))).as("cut before it").isTrue();
        assertThat(run.tenant(t).collections()).as("and no collection was visited").isEmpty();
    }

    /** Code I2 / suggestion: a lock wait that times out during expiry is a skip, not an error. */
    @Test
    void aLockTimeoutDuringExpiryIsASkip_notAnError_andNotARefusal() throws Exception {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        List<String> old = quarantined(t, origin, "old", 2, 15, true);
        ChunkReaper r = reaper(t);
        ChunkReaper.TenantResult result;
        try (Connection writer = svcDs.getConnection()) {
            writer.setAutoCommit(false);
            PgContainerHelper.setTenant(writer, TenantScope.DEFAULT_TENANT_GUC, t, true);
            // A client holding an uncommitted write on one of the expiring rows for longer than the 2 s lock bound.
            DSL.using(writer, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now())
               .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(q))
                      .and(CHUNKS.CHASH.eq(Chash.fromHex(old.get(0)).toBytes()))).execute();

            result = r.runOnce(null).tenant(t);
            writer.rollback();
        }

        assertThat(result.expiry(q).refusal()).isEqualTo(Refusal.LOCK_TIMEOUT);
        assertThat(result.expiry(q).error()).as("not an error").isNull();
        assertThat(result.errors()).isZero();
        assertThat(result.refused()).as("not a refusal").isZero();
        assertThat(result.skipped()).isEqualTo(1);
        assertThat(r.lockTimeoutTotal()).isEqualTo(1);
        assertThat(r.refusedTotal()).isZero();
        assertThat(inCollection(t, q, old.get(0))).as("nothing was deleted").isTrue();
        assertThat(r.runOnce(null).tenant(t).expiry(q).expired()).as("the next pass takes both").isEqualTo(2);
    }

    // ── the floor exemption: one named collection, move floor only ───────────

    private static Settings exempting(String... collections) {
        return new Settings(true, Duration.ofHours(1), 300, 0.25, 100, Duration.ofMinutes(10), Duration.ofSeconds(60),
            Duration.ofDays(14), java.util.Set.of(collections));
    }

    @Test
    void aNamedCollectionIsExemptFromTheMoveFloor_anotherIsStillRefused_andTheExemptionIsLoggedAndAudited()
            throws Throwable {
        String t = newTenant();
        openGate(t);
        String exempt = col("knowledge");
        String other = col("knowledge");
        bulkFast(t, exempt, 300, 200, i -> Map.of());   // 100 of 300: over the floor
        bulkFast(t, other, 300, 200, i -> Map.of());

        RunResult[] run = new RunResult[1];
        List<String> logs = captureLogs(() -> run[0] = reaper(exempting(exempt), t).runOnce(Duration.ZERO));

        assertThat(run[0].tenant(t).collection(exempt).refusal()).isNull();
        assertThat(run[0].tenant(t).collection(exempt).moved()).isEqualTo(100);
        assertThat(run[0].tenant(t).collection(other).refusal()).as("the floor still guards everything else")
            .isEqualTo(Refusal.FLOOR_EXCEEDED);
        assertThat(countIn(t, other)).isEqualTo(300);
        assertThat(logs).anyMatch(l -> l.startsWith("WARN") && l.contains("event=reaper_floor_exempt")
            && l.contains("collection=" + exempt) && l.contains("would_have_refused=true"));
        assertThat(auditRows(t)).singleElement().satisfies(a -> {
            assertThat(a.collection()).isEqualTo(exempt);
            assertThat(readJson(a.details()).get("floor_fraction").asDouble()).as("auditable").isEqualTo(1.0);
        });
    }

    @Test
    void anExemptCollectionWithMoreThanOnePassOfGarbageDrainsInCeilROver300Passes() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        bulkFast(t, c, 1000, 300, i -> Map.of());   // 700 reapable of 1000: 0.7, over the floor, over one batch
        ChunkReaper refusing = reaper(t);
        assertThat(refusing.runOnce(Duration.ZERO).tenant(t).collection(c).refusal()).isEqualTo(Refusal.FLOOR_EXCEEDED);
        ChunkReaper r = reaper(exempting(c), t);

        assertThat(r.runOnce(Duration.ZERO).tenant(t).collection(c).moved()).isEqualTo(300);
        assertThat(r.runOnce(Duration.ZERO).tenant(t).collection(c).moved()).isEqualTo(300);
        assertThat(r.runOnce(Duration.ZERO).tenant(t).collection(c).moved()).as("ceil(700 / 300) = 3 passes").isEqualTo(100);
        assertThat(countIn(t, c)).isEqualTo(300);
        assertThat(countIn(t, quarantineOf(c))).isEqualTo(700);
    }

    /** Code M2: collection names are per-tenant, so a bare name waives the floor everywhere; tenant/collection does not. */
    @Test
    void aTenantScopedExemptionWaivesTheFloorForThatTenantOnly_aBareNameWaivesItForEveryTenant() throws Throwable {
        String a = newTenant();
        String b = newTenant();
        openGate(a);
        openGate(b);
        String c = col("knowledge");   // the SAME collection name in both tenants
        bulkFast(a, c, 300, 200, i -> Map.of());
        bulkFast(b, c, 300, 200, i -> Map.of());

        RunResult scoped = reaper(exempting(a + "/" + c), a, b).runOnce(Duration.ZERO);

        assertThat(scoped.tenant(a).collection(c).moved()).as("tenant a is named").isEqualTo(100);
        assertThat(scoped.tenant(b).collection(c).refusal()).as("tenant b has the same name and is NOT exempt")
            .isEqualTo(Refusal.FLOOR_EXCEEDED);
        assertThat(countIn(b, c)).isEqualTo(300);

        RunResult bare = reaper(exempting(c), a, b).runOnce(Duration.ZERO);

        assertThat(bare.tenant(b).collection(c).moved()).as("a bare name waives it in every tenant").isEqualTo(100);
    }

    // ── a statement that hits its bound is a classified refusal, not a stack trace every hour (code review S8) ──

    private static org.jooq.exception.DataAccessException statementTimeout() {
        return new org.jooq.exception.DataAccessException("canceling statement due to statement timeout",
            new java.sql.SQLException("canceling statement due to statement timeout", "57014"));
    }

    /** The real repository with the named statements replaced by one that raises a 57014 while {@code failing} says so. */
    private ReaperRepository timingOutRepo(java.util.function.BooleanSupplier failing, String... statements) {
        java.util.Set<String> which = java.util.Set.of(statements);
        return new ReaperRepository(tenantScope) {
            @Override
            public Pass probe(String tenant, String collection, Duration grace, int statementTimeoutMs) {
                if (which.contains("probe") && failing.getAsBoolean()) throw statementTimeout();
                return super.probe(tenant, collection, grace, statementTimeoutMs);
            }

            @Override
            public Pass move(String tenant, String collection, String quarantineCollection, String quarantinedAt,
                             int rowLimit, Duration grace, double floorFraction, int floorMinChunks,
                             int statementTimeoutMs, int lockTimeoutMs) {
                if (which.contains("move") && failing.getAsBoolean()) throw statementTimeout();
                return super.move(tenant, collection, quarantineCollection, quarantinedAt, rowLimit, grace,
                    floorFraction, floorMinChunks, statementTimeoutMs, lockTimeoutMs);
            }

            @Override
            public Expiry expire(String tenant, String quarantineCollection, String originCollection, String cutoff,
                                 int rowLimit, int statementTimeoutMs, int lockTimeoutMs) {
                if (which.contains("expire") && failing.getAsBoolean()) throw statementTimeout();
                return super.expire(tenant, quarantineCollection, originCollection, cutoff, rowLimit,
                    statementTimeoutMs, lockTimeoutMs);
            }
        };
    }

    private ChunkReaper reaperOver(ReaperRepository repository, String... tenants) {
        return new ChunkReaper(repository, vectors, repo, gate, () -> List.of(tenants), Settings.defaults(), CLOCK);
    }

    private void assertTimeoutSequence(List<Refusal> seen) {
        // Passes 1-3 time out (the third starts a 2 pass rest), 4-5 rest, 6 times out (4 pass rest).
        assertThat(seen).containsExactly(
            Refusal.STATEMENT_TIMED_OUT, Refusal.STATEMENT_TIMED_OUT, Refusal.STATEMENT_TIMED_OUT,
            Refusal.STATEMENT_BACKOFF, Refusal.STATEMENT_BACKOFF, Refusal.STATEMENT_TIMED_OUT);
    }

    @Test
    void aDryRunThatHitsItsBound_isARefusalWithTheCensusBackoff_notAnErrorEveryHour() throws Throwable {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        ChunkReaper r = reaperOver(timingOutRepo(() -> true, "probe"), t);

        List<Refusal> seen = new ArrayList<>();
        List<String> logs = captureLogs(() -> {
            for (int i = 1; i <= 6; i++) seen.add(r.runOnce(Duration.ZERO).tenant(t).collection(c).refusal());
        });

        assertTimeoutSequence(seen);
        assertThat(r.statementTimedOutTotal()).isEqualTo(4);
        assertThat(r.statementBackoffTotal()).isEqualTo(2);
        assertThat(r.refusedTotal()).as("a rest is a skip, not a refusal").isEqualTo(4);
        assertThat(refusedRows(t)).as("one durable audit row for the state").singleElement().satisfies(a ->
            assertThat(a.details()).contains("STATEMENT_TIMED_OUT").contains("probe"));
        assertThat(logs).as("no collection_failed stack trace").noneMatch(l -> l.contains("event=reaper_collection_failed"));
        assertThat(inCollection(t, c, h)).isTrue();
    }

    @Test
    void aMoveThatHitsItsBound_isClassifiedTheSameWay() throws Throwable {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        ChunkReaper r = reaperOver(timingOutRepo(() -> true, "move"), t);

        List<Refusal> seen = new ArrayList<>();
        List<String> logs = captureLogs(() -> {
            for (int i = 1; i <= 6; i++) seen.add(r.runOnce(Duration.ZERO).tenant(t).collection(c).refusal());
        });

        assertTimeoutSequence(seen);
        assertThat(refusedRows(t)).singleElement().satisfies(a -> assertThat(a.details()).contains("move"));
        assertThat(logs).noneMatch(l -> l.contains("event=reaper_collection_failed"));
        assertThat(inCollection(t, c, h)).as("nothing moved").isTrue();
    }

    @Test
    void anExpiryThatHitsItsBound_isAClassifiedRefusalUnderTheSiblingsName_andRests() throws Throwable {
        String t = newTenant();
        openGate(t);
        String origin = col("knowledge");
        String q = quarantineOf(origin);
        List<String> old = quarantined(t, origin, "old", 2, 15, true);
        ChunkReaper r = reaperOver(timingOutRepo(() -> true, "expire"), t);

        List<Refusal> seen = new ArrayList<>();
        List<String> logs = captureLogs(() -> {
            for (int i = 1; i <= 6; i++) seen.add(r.runOnce(null).tenant(t).expiry(q).refusal());
        });

        assertTimeoutSequence(seen);
        assertThat(r.statementTimedOutTotal()).isEqualTo(4);
        assertThat(r.statementBackoffTotal()).isEqualTo(2);
        assertThat(refusedRows(t)).singleElement().satisfies(a -> {
            assertThat(a.collection()).as("filed under the quarantine- name").isEqualTo(q);
            assertThat(a.details()).contains("STATEMENT_TIMED_OUT");
        });
        assertThat(logs).noneMatch(l -> l.contains("event=reaper_expire_failed"));
        assertThat(countIn(t, q)).as("nothing deleted").isEqualTo(2);
        assertThat(inCollection(t, q, old.get(0))).isTrue();
    }

    @Test
    void aStatementThatCompletesEndsItsTimeoutStreak() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        bulkFast(t, c, 3, 2, i -> Map.of());   // one orphan, and two owned chunks that keep the collection listed after it moves
        java.util.concurrent.atomic.AtomicInteger n = new java.util.concurrent.atomic.AtomicInteger();
        // Timeouts on passes 1, 2, 4 and 5; pass 3 completes. Without the reset the 4th would be the third in a row.
        ChunkReaper r = reaperOver(timingOutRepo(() -> n.incrementAndGet() != 3, "probe"), t);

        List<Refusal> seen = new ArrayList<>();
        for (int i = 1; i <= 5; i++) seen.add(r.runOnce(Duration.ZERO).tenant(t).collection(c).refusal());

        assertThat(seen).as("no rest was ever started").doesNotContain(Refusal.STATEMENT_BACKOFF);
        assertThat(seen.get(0)).isEqualTo(Refusal.STATEMENT_TIMED_OUT);
        assertThat(seen.get(3)).isEqualTo(Refusal.STATEMENT_TIMED_OUT);
        assertThat(r.statementTimedOutTotal()).isEqualTo(4);
    }

    // ── census timeouts back off; a pathological collection cannot starve the rest ────────

    private static ChunkReaper.Census timingOut(java.util.List<String> calls) {
        return (tenant, collection, limit, timeout) -> {
            calls.add(collection);
            throw new org.jooq.exception.DataAccessException("canceling statement due to statement timeout",
                new java.sql.SQLException("canceling statement due to statement timeout", "57014"));
        };
    }

    @Test
    void afterThreeConsecutiveCensusTimeouts_theCensusRests2ToTheKPasses_keepingOneDurableAuditRow() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        String h = orphan(t, c, "x");
        List<String> censusCalls = new ArrayList<>();
        ChunkReaper r = reaper(Settings.defaults(), timingOut(censusCalls), System::nanoTime, t);

        List<Refusal> seen = new ArrayList<>();
        for (int i = 1; i <= 11; i++) seen.add(r.runOnce(Duration.ZERO).tenant(t).collection(c).refusal());

        // Passes 1-3 time out (the third starts a 2 pass rest), 4-5 rest, 6 times out (4 pass rest), 7-10 rest,
        // 11 times out (8 pass rest).
        assertThat(seen).containsExactly(
            Refusal.CENSUS_TIMED_OUT, Refusal.CENSUS_TIMED_OUT, Refusal.CENSUS_TIMED_OUT,
            Refusal.CENSUS_BACKOFF, Refusal.CENSUS_BACKOFF,
            Refusal.CENSUS_TIMED_OUT,
            Refusal.CENSUS_BACKOFF, Refusal.CENSUS_BACKOFF, Refusal.CENSUS_BACKOFF, Refusal.CENSUS_BACKOFF,
            Refusal.CENSUS_TIMED_OUT);
        assertThat(censusCalls).as("the census ran on passes 1,2,3,6,11 only").hasSize(5);
        assertThat(r.censusTimedOutTotal()).isEqualTo(5);
        assertThat(r.censusBackoffTotal()).isEqualTo(6);
        assertThat(r.refusedTotal()).as("a rest is a skip, not a refusal").isEqualTo(5);
        assertThat(refusedRows(t)).as("the single durable audit row").singleElement().satisfies(a ->
            assertThat(a.details()).contains("CENSUS_TIMED_OUT"));
        assertThat(inCollection(t, c, h)).isTrue();
    }

    @Test
    void theCensusBackoffIsCappedAt24Hours() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        orphan(t, c, "x");
        List<String> censusCalls = new ArrayList<>();
        // A one day interval caps the rest at ONE pass (24 h / 24 h), where 2^1 would be two.
        Settings daily = new Settings(true, Duration.ofDays(1), 300, 0.25, 100, Duration.ofMinutes(10),
            Duration.ofSeconds(60));
        ChunkReaper r = reaper(daily, timingOut(censusCalls), System::nanoTime, t);

        List<Refusal> seen = new ArrayList<>();
        for (int i = 1; i <= 5; i++) seen.add(r.runOnce(Duration.ZERO).tenant(t).collection(c).refusal());

        assertThat(seen).containsExactly(Refusal.CENSUS_TIMED_OUT, Refusal.CENSUS_TIMED_OUT, Refusal.CENSUS_TIMED_OUT,
            Refusal.CENSUS_BACKOFF, Refusal.CENSUS_TIMED_OUT);
    }

    @Test
    void aCensusThatCompletesEndsTheStreak() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("knowledge");
        orphan(t, c, "x");
        java.util.concurrent.atomic.AtomicInteger n = new java.util.concurrent.atomic.AtomicInteger();
        List<String> calls = new ArrayList<>();
        ChunkReaper.Census flaky = (tenant, collection, limit, timeout) -> {
            calls.add(collection);
            int k = n.incrementAndGet();
            if (k == 3) return censusOf(1, Map.of("legacy-unmanifested", 1L));   // completes, with a verdict
            throw new org.jooq.exception.DataAccessException("canceling statement due to statement timeout",
                new java.sql.SQLException("canceling statement due to statement timeout", "57014"));
        };
        ChunkReaper r = reaper(Settings.defaults(), flaky, System::nanoTime, t);

        for (int i = 1; i <= 5; i++) r.runOnce(Duration.ZERO);

        // 1 timeout, 2 timeouts, 3 completes (streak over), 4 times out (streak 1: no rest), 5 times out (2).
        assertThat(calls).as("with the streak reset the census still runs on passes 4 and 5").hasSize(5);
        assertThat(r.censusBackoffTotal()).isZero();
    }

    @Test
    void aWallClockCutResumesWhereItStopped_aTimingOutCollectionCannotStarveTheRest() throws Exception {
        String t = newTenant();
        openGate(t);
        int n = seq.incrementAndGet();
        String a = colNamed("knowledge", n, "a");
        String b = colNamed("knowledge", n, "b");
        String c = colNamed("knowledge", n, "c");
        orphan(t, a, "x");
        String hb = orphan(t, b, "x");
        String hc = orphan(t, c, "x");
        java.util.concurrent.atomic.AtomicLong fakeNanos = new java.util.concurrent.atomic.AtomicLong();
        // Every census spends 20 s of the 10 s budget; the first collection's census also times out, every pass.
        ChunkReaper.Census slow = (tenant, collection, limit, timeout) -> {
            fakeNanos.addAndGet(Duration.ofSeconds(20).toNanos());
            if (collection.equals(a)) {
                throw new org.jooq.exception.DataAccessException("canceling statement due to statement timeout",
                    new java.sql.SQLException("canceling statement due to statement timeout", "57014"));
            }
            return vectors.manifestLessCensusBounded(tenant, collection, limit, 0, timeout);
        };
        ChunkReaper r = reaper(budgetOf(Duration.ofSeconds(10)), slow, fakeNanos::get, t);

        RunResult first = r.runOnce(Duration.ZERO);
        RunResult second = r.runOnce(Duration.ZERO);
        RunResult third = r.runOnce(Duration.ZERO);

        assertThat(first.tenant(t).collection(a).refusal()).isEqualTo(Refusal.CENSUS_TIMED_OUT);
        assertThat(first.tenant(t).collection(b)).as("cut before it").isNull();
        assertThat(second.tenant(t).collection(a)).as("the next pass resumes at b, not at a").isNull();
        assertThat(second.tenant(t).collection(b).moved()).isEqualTo(1);
        assertThat(third.tenant(t).collection(c).moved()).isEqualTo(1);
        assertThat(inCollection(t, b, hb)).isFalse();
        assertThat(inCollection(t, c, hc)).isFalse();
    }

    @Test
    void aWallClockCutResumesAtTheTenantItStoppedAt() throws Exception {
        String t1 = newTenant();
        String t2 = newTenant();
        openGate(t1);
        openGate(t2);
        String c1 = col("knowledge");
        String c2 = col("knowledge");
        orphan(t1, c1, "x");
        String h2 = orphan(t2, c2, "x");
        java.util.concurrent.atomic.AtomicLong fakeNanos = new java.util.concurrent.atomic.AtomicLong();
        ChunkReaper.Census slow = (tenant, collection, limit, timeout) -> {
            fakeNanos.addAndGet(Duration.ofSeconds(20).toNanos());
            return censusOf(1, Map.of("legacy-unmanifested", 1L));   // refuses: nothing moves, the tenant stays busy
        };
        ChunkReaper r = reaper(budgetOf(Duration.ofSeconds(10)), slow, fakeNanos::get, t1, t2);

        RunResult first = r.runOnce(Duration.ZERO);
        RunResult second = r.runOnce(Duration.ZERO);

        assertThat(first.tenant(t1)).isNotNull();
        assertThat(first.tenant(t2)).as("never reached on the first pass").isNull();
        assertThat(second.tenant(t2)).as("the second pass starts where the first stopped").isNotNull();
        assertThat(second.tenant(t1)).isNull();
        assertThat(inCollection(t2, c2, h2)).isTrue();
    }

    // ── a multi-batch re-index through the REAL combined writer ──────────────

    private void registerDocsDocument(String tenant, String docId, String collection) {
        repo.upsertDocument(tenant, Map.of(
            "tumbler", docId, "title", "reaper-" + docId, "content_type", "docs", "corpus", "docs",
            "physical_collection", collection, "chunk_count", 0));
    }

    private static String textHash(String text) {
        return Chash.ofText(text).toHex();
    }

    private static Map<String, Object> wireChunk(String text) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("chash", textHash(text));
        m.put("text", text);
        m.put("metadata", Map.of());
        return m;
    }

    private static Map<String, Object> wireRow(String text, int position) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("position", position);
        m.put("chash", textHash(text));
        m.put("chunk_index", position);
        return m;
    }

    /** A chunk written long ago by a run that crashed: it exists, with a vector, and nothing owns it. */
    private String agedOwnerless(String tenant, String collection, String text) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            PgContainerHelper.insertChunks(ctx, tenant, collection, List.of(textHash(text)), List.of(text),
                List.of(new float[384]), List.of(Map.of()));
        }
        makeOld(tenant, collection);
        return textHash(text);
    }

    @Test
    void anAgedOwnerlessChunkReAddedByALaterBatch_survivesAPassBetweenBatches_andTheRunCompletes() throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("docs");
        String docId = "rp.doc." + seq.incrementAndGet();
        registerDocsDocument(t, docId, c);
        String x = agedOwnerless(t, c, "crashed-run tail");   // old, ownerless: reapable the moment a pass looks

        // Batch 1 through the real combined writer: replaces the manifest, sweep off.
        svc.writeManyCombined(t, c, List.of(wireChunk("a1"), wireChunk("a2")),
            List.of(Map.of("doc_id", docId, "rows", List.of(wireRow("a1", 0), wireRow("a2", 1)))),
            null, false, false);

        // The reaper passes between batch 1 and batch 2 and takes the aged ownerless chunk: it is garbage at
        // that instant, by the predicate. The run has not finished, and batch 2 is about to want it back.
        CollectionResult between = reaper(t).runOnce(null).tenant(t).collection(c);
        assertThat(between.error()).isNull();
        assertThat(between.moved()).as("aged and ownerless: the pass takes it").isEqualTo(1);
        assertThat(inCollection(t, c, x)).isFalse();

        // Batch 2 re-adds it: absent now, so the writer re-embeds and re-inserts it. The run must COMPLETE.
        var append = svc.appendCombined(t, c, docId, List.of(wireRow("crashed-run tail", 2)),
            List.of(wireChunk("crashed-run tail")), false, null);

        assertThat(append.response().get("ok")).isEqualTo(true);
        assertThat(inCollection(t, c, x)).as("the run completed and the chunk it re-added is live").isTrue();
        assertThat(manifestHas(t, docId, x)).isTrue();
        assertThat(reaper(t).runOnce(null).tenant(t).collection(c).moved()).as("and nothing further to take").isZero();
        assertThat(inCollection(t, c, x)).isTrue();
    }

    @Test
    void anAgedOwnerlessChunkReAddedByALaterBatch_isNotTakenByAPassAfterTheExistencePartitionRefreshedIt()
            throws Exception {
        String t = newTenant();
        openGate(t);
        String c = col("docs");
        String docId = "rp.doc." + seq.incrementAndGet();
        registerDocsDocument(t, docId, c);
        String x = agedOwnerless(t, c, "crashed-run tail");
        svc.writeManyCombined(t, c, List.of(wireChunk("a1")),
            List.of(Map.of("doc_id", docId, "rows", List.of(wireRow("a1", 0)))), null, false, false);

        // A pass in the window AFTER batch 2's existence partition committed (the chunk exists, so the partition
        // refreshed its metadata and with it last_written_at) and BEFORE its insert transaction.
        List<Long> movedInWindow = new ArrayList<>();
        svc.setAfterNeedEmbedResolvedHookForTests(() -> {
            CollectionResult cr = reaper(t).runOnce(null).tenant(t).collection(c);
            movedInWindow.add(cr == null ? -1L : cr.moved());
        });
        try {
            svc.appendCombined(t, c, docId, List.of(wireRow("crashed-run tail", 1)),
                List.of(wireChunk("crashed-run tail")), false, null);
        } finally {
            svc.setAfterNeedEmbedResolvedHookForTests(null);
        }

        assertThat(movedInWindow).as("the hook fired, so the window was exercised").hasSize(1);
        assertThat(movedInWindow.get(0)).as("the refreshed chunk is inside its grace: nothing to take").isZero();
        assertThat(inCollection(t, c, x)).isTrue();
        assertThat(inCollection(t, quarantineOf(c), x)).isFalse();
        assertThat(manifestHas(t, docId, x)).as("the run completed").isTrue();
    }

    private boolean manifestHas(String tenant, String docId, String chashHex) {
        for (var r : repo.getManifest(tenant, docId)) {
            if (chashHex.equals(String.valueOf(r.get("chash")))) return true;
        }
        return false;
    }

    // ── helpers for the journey ──────────────────────────────────────────────

    private static String h(String collection, String name) {
        return Chash.ofText(collection + "/" + name).toHex();
    }

    private Set<String> chunkSet(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            Set<String> out = new LinkedHashSet<>();
            DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.CHASH).from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)))
               .fetch().forEach(r -> out.add(HexFormat.of().formatHex(r.value1())));
            return out;
        }
    }

    /** What time does to chunks written long ago. Only the multi-batch journey needs it: it starts from an old run. */
    private void makeOld(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            OffsetDateTime then = OffsetDateTime.now().minusDays(40);
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then)
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))).execute();
        }
    }

    /**
     * The RDR-223 client protocol through the real manifest writers: batch 1 replaces the manifest with the sweep
     * off, later batches append, and the last one carries the chashes batch 1 dropped as the sweep set.
     */
    @SuppressWarnings("unchecked")
    private void writeDocument(String tenant, String collection, String docId, String[][] batches,
                               java.util.function.IntConsumer afterRequest) throws Exception {
        List<String> dropped = List.of();
        int position = 0;
        for (int k = 1; k <= batches.length; k++) {
            List<Map<String, Object>> rows = new ArrayList<>();
            for (String name : batches[k - 1]) {
                orphan(tenant, collection, name);
                rows.add(Map.of("position", position, "chash", h(collection, name), "chunk_index", position));
                position++;
            }
            if (k == 1) {
                Map<String, Object> r = repo.writeManifestMany(tenant,
                    List.of(Map.of("doc_id", docId, "rows", rows)), collection);
                dropped = ((Map<String, List<String>>) r.get("dropped_chashes")).get(docId);
            } else {
                repo.appendManifestChunks(tenant, docId, collection, rows, null, null,
                    k == batches.length && !dropped.isEmpty() ? dropped : null);
            }
            if (afterRequest != null) afterRequest.accept(k);
        }
    }
}
