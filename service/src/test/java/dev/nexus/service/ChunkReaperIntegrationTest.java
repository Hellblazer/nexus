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
        return new Settings(true, Duration.ofHours(1), batch, fraction, minChunks, Duration.ofMinutes(10));
    }

    private ChunkReaper reaper(Settings s, String... tenants) {
        return new ChunkReaper(store, vectors, gate, () -> List.of(tenants), s, CLOCK);
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

    private List<AuditRow> auditRows(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).selectFrom(GC_AUDIT)
                .where(GC_AUDIT.TENANT_ID.eq(tenant)).orderBy(GC_AUDIT.ID)
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
