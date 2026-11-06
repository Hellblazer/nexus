// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.ChunkReaper.RunResult;
import dev.nexus.service.ChunkReaper.Settings;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashHex;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.QuarantineRestoreOutcome;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
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
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_IS_REAPABLE;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.QUARANTINE_RESTORE_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-192 Step 9 Day-2 (bead nexus-2x9xa, Sam's ruling 2026-10-01): the quarantine RESTORE verb's engine half,
 * {@code nexus.quarantine_restore_chunks} (vectors-025) through {@link PgVectorRepository#quarantineRestore}. Drives
 * the real reaper against a real PostgreSQL substrate to put chunks in quarantine, then restores them, never a mock.
 *
 * <p>The chunks are seeded manifest-less, which is the case that has no other way back: {@code gc_restore_rereferenced}
 * needs a manifest row in the origin, and a chunk the reaper moved wrongly (a manifest defect, the R8 shape) has none.
 *
 * <p>Each test works in its own tenant.
 */
class QuarantineRestoreIntegrationTest extends AtomicWriteTestBase {

    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-10-01T12:00:00Z"), ZoneOffset.UTC);
    private static final String ACTOR = "test-operator";

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
        return "qr" + seq.incrementAndGet();
    }

    private void openGate(String tenant) {
        ladder.record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
    }

    private String col(String prefix) {
        return prefix + "__qr" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private static String quarantineOf(String collection) {
        return "quarantine-" + collection;
    }

    private ChunkReaper reaper(String... tenants) {
        return new ChunkReaper(store, vectors, repo, gate, () -> List.of(tenants), Settings.defaults(), CLOCK);
    }

    private String orphan(String tenant, String collection, String seed, Map<String, Object> metadata) throws Exception {
        String hex = Chash.ofText(collection + "/" + seed).toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            PgContainerHelper.insertChunks(ctx, tenant, collection, List.of(hex), List.of(seed + " text"),
                embedder.embed(List.of(seed + " text")), List.of(metadata));
        }
        return hex;
    }

    private String orphan(String tenant, String collection, String seed) throws Exception {
        return orphan(tenant, collection, seed, Map.of("title", seed));
    }

    /** Seeds {@code seeds} as manifest-less chunks and lets the real reaper move them into quarantine. */
    private List<String> quarantined(String tenant, String collection, String... seeds) throws Exception {
        openGate(tenant);
        List<String> hexes = new java.util.ArrayList<>();
        for (String s : seeds) hexes.add(orphan(tenant, collection, s));
        RunResult run = reaper(tenant).runOnce(Duration.ZERO);
        assertThat(run.tenant(tenant).collection(collection).moved()).as("fixture: the reaper moved them")
            .isEqualTo(seeds.length);
        for (String h : hexes) {
            assertThat(inCollection(tenant, quarantineOf(collection), h)).isTrue();
            assertThat(inCollection(tenant, collection, h)).isFalse();
        }
        return hexes;
    }

    /** Like {@link #quarantined}, with a metadata map per seed (in the map's iteration order). */
    private List<String> quarantinedWith(String tenant, String collection,
                                         Map<String, Map<String, Object>> metaBySeed) throws Exception {
        openGate(tenant);
        List<String> hexes = new java.util.ArrayList<>();
        for (var e : metaBySeed.entrySet()) hexes.add(orphan(tenant, collection, e.getKey(), e.getValue()));
        RunResult run = reaper(tenant).runOnce(Duration.ZERO);
        assertThat(run.tenant(tenant).collection(collection).moved()).as("fixture: the reaper moved them")
            .isEqualTo(metaBySeed.size());
        for (String h : hexes) assertThat(inCollection(tenant, quarantineOf(collection), h)).isTrue();
        return hexes;
    }

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** A live catalog document registered under {@code collection}. */
    private void liveDoc(String tenant, String collection, String tumbler, String title, Integer chunkCount,
                         String filePath, Map<String, Object> metadata) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES)
                .insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.CHUNK_COUNT,
                    CATALOG_DOCUMENTS.FILE_PATH, CATALOG_DOCUMENTS.METADATA)
                .values(tenant, tumbler, title, collection, chunkCount, filePath,
                    JSONB.jsonb(MAPPER.writeValueAsString(metadata)))
                .execute();
        }
    }

    private void tombstone(String tenant, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute();
        }
    }

    private void setIndexState(String tenant, String tumbler, String state) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.INDEX_STATE, state)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute();
        }
    }

    /** A manifest row of {@code doc} naming a chunk that lives in ANOTHER collection (a rename-copy leftover). */
    private void manifestUnderAnotherCollection(String tenant, String doc, String otherCollection, int position,
                                                String seed) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, otherCollection);
        }
        manifested(tenant, otherCollection, doc, position, seed);
    }

    /** A chunk of {@code collection} that the document's manifest already names at {@code position}. */
    private String manifested(String tenant, String collection, String doc, int position, String seed) throws Exception {
        String hex = Chash.ofText(collection + "/" + seed).toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertChunks(ctx, tenant, collection, List.of(hex), List.of(seed + " text"),
                embedder.embed(List.of(seed + " text")), List.of(Map.<String, Object>of("title", seed)));
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                    CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
               .values(tenant, doc, position, Chash.fromHex(hex).toBytes(), position, collection,
                    PgContainerHelper.collectionModel(ctx, tenant, collection)).execute();
        }
        return hex;
    }

    private record ManifestRow(int position, String chash, String collection) {}

    private List<ManifestRow> manifest(String tenant, String doc) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(CATALOG_DOCUMENT_CHUNKS.POSITION, ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH),
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                .from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc)))
                .orderBy(CATALOG_DOCUMENT_CHUNKS.POSITION)
                .fetch(r -> new ManifestRow(r.value1(), r.value2(), r.value3()));
        }
    }

    /** The manifest row's {@code [line_start, line_end, char_start, char_end]} at {@code position} of {@code doc}. */
    private List<Integer> manifestSpan(String tenant, String doc, int position) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(CATALOG_DOCUMENT_CHUNKS.LINE_START, CATALOG_DOCUMENT_CHUNKS.LINE_END,
                        CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END)
                .from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))
                    .and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(position)))
                .fetchOne(r -> List.of(r.value1(), r.value2(), r.value3(), r.value4()));
        }
    }

    /** What the engine's read paths show: live(c) hides a chunk no live manifest row names. */
    private boolean visibleToGet(String tenant, String collection, String hex) {
        var r = vectors.get(tenant, collection, List.of(hex), 10, 0, false);
        return ((List<?>) r.get("ids")).contains(hex);
    }

    private boolean visibleToSearch(String tenant, String collection, String text, String hex) {
        return vectors.search(tenant, text, List.of(collection), 50, null).stream()
            .anyMatch(r -> hex.equals(r.get("id")));
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> metadataOf(ChunkState state) throws Exception {
        return MAPPER.readValue(state.metadata(), Map.class);
    }

    private static String verdictOf(QuarantineRestoreOutcome out, String hex) {
        return out.rows().stream().filter(r -> r.chash().equals(hex)).findFirst().orElseThrow().reattach();
    }

    private static String reasonOf(QuarantineRestoreOutcome out, String hex) {
        return out.rows().stream().filter(r -> r.chash().equals(hex)).findFirst().orElseThrow().reason();
    }

    private void setContentHash(String tenant, String tumbler, String hash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, hash)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute();
        }
    }

    /** Makes the quarantined copies look long-lived: written 90 days ago, moved 10 days ago. */
    private void ageQuarantined(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS)
                .set(CHUNKS.CREATED_AT, OffsetDateTime.now().minusDays(90))
                .set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minusDays(10))
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(quarantineOf(collection)))).execute();
        }
    }

    private boolean inCollection(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))));
        }
    }

    private record ChunkState(String text, String metadata, OffsetDateTime createdAt, OffsetDateTime lastWrittenAt) {}

    private ChunkState chunk(String tenant, String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(CHUNKS.CHUNK_TEXT, DSL.field(DSL.name("chunks", "metadata"), String.class),
                        CHUNKS.CREATED_AT, CHUNKS.LAST_WRITTEN_AT)
                .from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes())))
                .fetchOne(r -> new ChunkState(r.value1(), r.value2(), r.value3(), r.value4()));
        }
    }

    private record AuditRow(long id, String operation, String actor, String collection, int chashCount,
                            String chashes, String details) {}

    private List<AuditRow> audit(String tenant, String operation) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).selectFrom(GC_AUDIT)
                .where(GC_AUDIT.TENANT_ID.eq(tenant).and(GC_AUDIT.OPERATION.eq(operation))).orderBy(GC_AUDIT.ID)
                .fetch(r -> new AuditRow(r.getId(), r.getOperation(), r.getActor(), r.getCollection(),
                    r.getChashCount(), r.getChashes().data(), r.getDetails() == null ? "" : r.getDetails().data()));
        }
    }

    // ── the round trip, and the grace ────────────────────────────────────────

    @Test
    void aWronglyMovedManifestLessChunkComesBack_andTheNextPassDoesNotMoveItAgain() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "wrongly-moved").get(0);
        ageQuarantined(t, c);

        QuarantineRestoreOutcome out =
            vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        assertThat(out.restored()).containsExactly(h);
        assertThat(inCollection(t, c, h)).as("back in the origin").isTrue();
        assertThat(inCollection(t, quarantineOf(c), h)).as("and gone from quarantine").isFalse();
        // The next hourly pass, with the production grace (30 days), leaves it alone. It is manifest-less with no
        // orphaning record, so only the grace stands between it and the reaper.
        RunResult next = reaper(t).runOnce(null);
        assertThat(next.tenant(t).collection(c).moved()).as("not immediately reapable").isZero();
        assertThat(inCollection(t, c, h)).isTrue();
    }

    @Test
    void aRestoredChunkIsReapableAgainOnlyAfterTheGraceWindow() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "grace").get(0);
        ageQuarantined(t, c);
        vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        // A day-long grace: the chunk was restored seconds ago, so it is inside the window.
        assertThat(reaper(t).runOnce(Duration.ofDays(1)).tenant(t).collection(c).moved()).isZero();

        // Age it past the window and the same pass takes it again: the restore bought a fresh grace, not immunity.
        ReapableFixtures.agePastGrace(pg, t, c);
        assertThat(reaper(t).runOnce(null).tenant(t).collection(c).moved()).isEqualTo(1);
        assertThat(inCollection(t, quarantineOf(c), h)).as("quarantined once more, reversibly").isTrue();
    }

    @Test
    void theRestoredRowKeepsItsContentAndCreatedAt_dropsTheQuarantineStamp_andTakesAFreshWriteTime() throws Exception {
        String t = newTenant();
        String c = col("docs");
        String h = quarantined(t, c, "stamps").get(0);
        // Make the chunk look old: written, quarantined and left there for a long time. Truncated to microseconds,
        // the precision timestamptz stores: Linux's clock carries nanoseconds, so an untruncated value comes back
        // rounded and the exact created_at comparison below fails on every Linux run (never on macOS, whose clock
        // ticks in microseconds).
        OffsetDateTime old = OffsetDateTime.now().minusDays(90).truncatedTo(ChronoUnit.MICROS);
        OffsetDateTime movedAt = OffsetDateTime.now().minusDays(10);
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS)
                .set(CHUNKS.CREATED_AT, old).set(CHUNKS.LAST_WRITTEN_AT, movedAt)
                .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(quarantineOf(c)))).execute();
        }
        ChunkState before = chunk(t, quarantineOf(c), h);
        assertThat(before.metadata()).contains("quarantined_at").contains("origin_collection");

        vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        ChunkState after = chunk(t, c, h);
        assertThat(after.text()).isEqualTo("stamps text");
        // Keys, not substrings: "reaper_quarantined_at" contains "quarantined_at", so a substring check goes red
        // the moment the reaper's own tag is present and green when it is stripped by accident.
        assertThat(metadataOf(after)).as("nexus-l003p: the CHUNK keys only; the title stays behind with the copy")
            .containsOnlyKeys("chunk_text_hash", "embedding_model")
            .containsEntry("chunk_text_hash", h)
            .doesNotContainKeys("title", "quarantined_at", "origin_collection", "quarantined_by", "reaper_quarantined_at");
        assertThat(after.createdAt().toInstant()).as("created_at is carried through").isEqualTo(old.toInstant());
        // Neither the quarantine row's last_written_at (10 days ago: copying it would shorten the grace by the time
        // spent in quarantine) nor anything derived from created_at (90 days ago: reapable at the very next pass).
        assertThat(after.lastWrittenAt()).as("last_written_at restarts the grace: now, not copied, not derived")
            .isAfter(OffsetDateTime.now().minusMinutes(5));
    }

    // ── collisions, missing chashes, idempotence ─────────────────────────────

    @Test
    void aChunkAlreadyInTheOriginIsSkippedNeverOverwritten_andItsQuarantineCopyStays() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "collide").get(0);
        // A newer write of the same chash landed in the origin meanwhile, with different text.
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertChunks(ctx, t, c, List.of(h), List.of("newer live text"),
                List.of(new float[384]), List.of(Map.<String, Object>of("title", "live")));
        }

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        assertThat(out.restored()).isEmpty();
        assertThat(out.present()).containsExactly(h);
        assertThat(chunk(t, c, h).text()).as("the live row is untouched").isEqualTo("newer live text");
        assertThat(inCollection(t, quarantineOf(c), h)).as("the quarantine copy is left for expiry").isTrue();
        assertThat(out.auditId()).as("nothing moved, so no audit row").isNull();
        assertThat(audit(t, "quarantine_restore")).isEmpty();
    }

    @Test
    void aChashNotInTheQuarantineSiblingIsReportedMissing_andTheRestWithinTheCallStillRestore() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "have-a", "have-b");
        String nowhere = Chash.ofText("never-existed").toHex();
        // Quarantined for ANOTHER origin: its sibling name is not this origin's, so it must read missing too.
        String other = col("knowledge");
        String foreign = quarantined(t, other, "foreign").get(0);

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c),
            List.of(hs.get(0), nowhere, foreign, hs.get(1)), ACTOR, false);

        assertThat(out.restored()).containsExactlyInAnyOrder(hs.get(0), hs.get(1));
        assertThat(out.missing()).containsExactlyInAnyOrder(nowhere, foreign);
        assertThat(inCollection(t, quarantineOf(other), foreign)).as("the other origin's quarantine is untouched").isTrue();
        assertThat(out.rows()).extracting(QuarantineRestoreOutcome.Row::chash)
            .as("one row per requested chash, in request order")
            .containsExactly(hs.get(0), nowhere, foreign, hs.get(1));
    }

    @Test
    void aSecondRestoreOfTheSameChashesIsAnIdempotentReportOfAlreadyPresent() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "twice").get(0);
        vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        QuarantineRestoreOutcome again = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        assertThat(again.restored()).isEmpty();
        assertThat(again.present()).containsExactly(h);
        assertThat(again.missing()).isEmpty();
        assertThat(audit(t, "quarantine_restore")).as("one audit row, for the call that moved something").hasSize(1);
    }

    @Test
    void aRepeatedChashInOneRequestIsRestoredOnce() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "dup").get(0);

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h, h), ACTOR, false);

        assertThat(out.restored()).containsExactly(h);
        assertThat(out.rows()).hasSize(1);
    }

    // ── dry run ──────────────────────────────────────────────────────────────

    @Test
    void aDryRunReportsWhatWouldMove_movesNothing_andWritesNoAuditRow() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "dry-a", "dry-b");
        String nowhere = Chash.ofText("dry-missing").toHex();

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c),
            List.of(hs.get(0), hs.get(1), nowhere), ACTOR, true);

        assertThat(out.dryRun()).isTrue();
        assertThat(out.wouldRestore()).containsExactlyInAnyOrder(hs.get(0), hs.get(1));
        assertThat(out.restored()).isEmpty();
        assertThat(out.missing()).containsExactly(nowhere);
        assertThat(inCollection(t, quarantineOf(c), hs.get(0))).isTrue();
        assertThat(inCollection(t, c, hs.get(0))).isFalse();
        assertThat(audit(t, "quarantine_restore")).isEmpty();
    }

    // ── what the report tells the operator ──────────────────────────────────

    @Test
    void aRestoredChunkReportsThatItHasNoManifestRowAndTheDateTheReaperMayTakeItAgain() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "reapable-again").get(0);

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        var row = out.rows().get(0);
        assertThat(row.outcome()).isEqualTo("restored");
        assertThat(row.noManifest()).as("a restored chunk has no manifest row: the manifest's FK needs the origin row").isTrue();
        OffsetDateTime written = chunk(t, c, h).lastWrittenAt();
        assertThat(Instant.parse(row.reapableAfter()))
            .as("restore time plus the grace").isBetween(written.plusDays(30).minusHours(1).toInstant(),
                                                         written.plusDays(30).plusHours(1).toInstant());
        assertThat(audit(t, "quarantine_restore")).singleElement()
            .satisfies(a -> assertThat(a.details()).contains("reapable_again_after").contains("\"no_manifest\": 1"));
        // A chash that was not restored carries neither.
        QuarantineRestoreOutcome again = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
        assertThat(again.rows().get(0).noManifest()).isNull();
        assertThat(again.rows().get(0).reapableAfter()).isNull();
    }

    @Test
    void theGraceTheReportReadsIsTheGraceThePredicateUses() throws Exception {
        // vectors-025 restates chunk_is_reapable's default grace (30 days) for the report. This pins the two together:
        // a chunk last written 30 days and a minute ago is reapable under the default, one written 30 days less a
        // minute ago is not. If the predicate's default moves, this fails and points at vectors-025's v_default_grace.
        String t = newTenant();
        String c = col("knowledge");
        String h = orphan(t, c, "grace-pin");
        Instant now = Instant.now();
        assertThat(reapableUnderDefaultGrace(t, c, h, now.minus(Duration.ofDays(30)).minus(Duration.ofMinutes(1)))).isTrue();
        assertThat(reapableUnderDefaultGrace(t, c, h, now.minus(Duration.ofDays(30)).plus(Duration.ofMinutes(1)))).isFalse();
    }

    private boolean reapableUnderDefaultGrace(String tenant, String collection, String hex, Instant lastWrittenAt)
            throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectFrom(CHUNK_IS_REAPABLE.call(
                DSL.val(tenant), DSL.val(collection), DSL.val(Chash.fromHex(hex).toBytes()),
                DSL.val(lastWrittenAt.atOffset(ZoneOffset.UTC)),
                DSL.castNull(SQLDataType.INTERVAL))));
        }
    }

    @Test
    void aChunkWhoseOriginRowHoldsAnotherEmbeddingWidthIsReportedAsADimConflictAndLeftAlone() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "dim-clash").get(0);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // RDR-225: a collection holds ONE embedding model, so a row of another width cannot sit beside the
            // quarantine copy under the same registration. The clash is now what it can only be: the origin
            // collection is registered under a 768-wide model while its quarantine sibling (registered by the
            // reaper under the origin's original minilm-384) still holds the 384-wide copy. Re-register the
            // origin (it holds no chunk at this point: the reaper moved them) and seed its 768-wide row.
            PgContainerHelper.insertCollection(ctx, t, c, "bge-base-en-v15-768");
            PgContainerHelper.insertChunks(ctx, t, c, List.of(h), List.of("origin 768 text"),
                List.of(new float[768]), List.of(Map.<String, Object>of()));
        }

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        assertThat(out.dimConflict()).containsExactly(h);
        assertThat(out.restored()).isEmpty();
        assertThat(chunk(t, c, h).text()).as("never overwritten").isEqualTo("origin 768 text");
        assertThat(inCollection(t, quarantineOf(c), h)).as("the quarantine copy stays").isTrue();
    }

    // ── audit row ────────────────────────────────────────────────────────────

    @Test
    void everyRestoreWritesOneAuditRowNamingTheActorTheChashesAndThePlaceTheyWent() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "aud-a", "aud-b");
        String nowhere = Chash.ofText("aud-missing").toHex();

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c),
            List.of(hs.get(0), hs.get(1), nowhere), ACTOR, false);

        assertThat(audit(t, "quarantine_restore")).singleElement().satisfies(a -> {
            assertThat(a.id()).isEqualTo(out.auditId());
            assertThat(a.actor()).isEqualTo(ACTOR);
            assertThat(a.collection()).as("keyed by the origin, like the reaper's own row").isEqualTo(c);
            assertThat(a.chashCount()).isEqualTo(2);
            assertThat(a.chashes()).contains(hs.get(0)).contains(hs.get(1)).doesNotContain(nowhere);
            assertThat(a.details()).contains(quarantineOf(c)).contains(nowhere);
        });
    }

    // ── the audit id as the source ───────────────────────────────────────────

    @Test
    void anAuditIdNamesTheChashesTheReaperMoved_andTheRestoreRecordsWhereItCameFrom() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "src-a", "src-b", "src-c");
        long reaperAuditId = audit(t, "reaper_quarantine").get(0).id();

        QuarantineRestoreOutcome out =
            vectors.quarantineRestoreFromAudit(t, c, quarantineOf(c), reaperAuditId, 0, 1000, ACTOR, false);

        assertThat(out.restored()).containsExactlyInAnyOrderElementsOf(hs);
        assertThat(out.source()).isNotNull();
        assertThat(out.source().auditId()).isEqualTo(reaperAuditId);
        assertThat(out.source().operation()).isEqualTo("reaper_quarantine");
        assertThat(out.source().chashCount()).isEqualTo(3);
        assertThat(out.source().nextOffset()).isNull();
        assertThat(audit(t, "quarantine_restore")).singleElement()
            .satisfies(a -> assertThat(a.details()).contains("\"source_audit_id\": " + reaperAuditId));
    }

    @Test
    void anAuditIdIsPagedByOffsetAndLimit() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "pg-a", "pg-b", "pg-c");
        long id = audit(t, "reaper_quarantine").get(0).id();

        QuarantineRestoreOutcome first = vectors.quarantineRestoreFromAudit(t, c, quarantineOf(c), id, 0, 2, ACTOR, false);
        assertThat(first.restored()).hasSize(2);
        assertThat(first.source().nextOffset()).isEqualTo(2);

        QuarantineRestoreOutcome second = vectors.quarantineRestoreFromAudit(t, c, quarantineOf(c), id, 2, 2, ACTOR, false);
        assertThat(second.restored()).hasSize(1);
        assertThat(second.source().nextOffset()).isNull();
        assertThat(java.util.stream.Stream.concat(first.restored().stream(), second.restored().stream()))
            .containsExactlyInAnyOrderElementsOf(hs);
    }

    @Test
    void anAuditIdThatIsNotAQuarantineMove_orNamesAnotherCollection_orAnotherTenant_isRefused() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "scope").get(0);
        long reaperId = audit(t, "reaper_quarantine").get(0).id();
        vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
        long restoreId = audit(t, "quarantine_restore").get(0).id();

        // A restore row lists chashes that moved the other way: not a source.
        assertThatThrownBy(() -> vectors.quarantineRestoreFromAudit(t, c, quarantineOf(c), restoreId, 0, 10, ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("quarantine_restore");
        // Another collection's row.
        String otherCol = col("knowledge");
        orphan(t, otherCol, "x");
        assertThatThrownBy(() -> vectors.quarantineRestoreFromAudit(t, otherCol, quarantineOf(otherCol), reaperId, 0, 10, ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(c);
        // No such row.
        assertThatThrownBy(() -> vectors.quarantineRestoreFromAudit(t, c, quarantineOf(c), 987_654_321L, 0, 10, ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("987654321");
        // Another tenant's row: RLS hides it, so it reads as no such row.
        String t2 = newTenant();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), t2, c);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), t2, quarantineOf(c));
        }
        assertThatThrownBy(() -> vectors.quarantineRestoreFromAudit(t2, c, quarantineOf(c), reaperId, 0, 10, ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining(Long.toString(reaperId));
    }

    // ── selecting from the quarantine sibling itself ─────────────────────────

    private void stamp(String tenant, String collection, String hex, String quarantinedAt) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String json = ctx.select(DSL.field(DSL.name("chunks", "metadata"), String.class)).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(quarantineOf(collection)))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))).fetchOne().value1();
            @SuppressWarnings("unchecked")
            Map<String, Object> meta = new com.fasterxml.jackson.databind.ObjectMapper().readValue(json, Map.class);
            meta.put("quarantined_at", quarantinedAt);
            ctx.update(CHUNKS).set(CHUNKS.METADATA,
                    org.jooq.JSONB.jsonb(new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(meta)))
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(quarantineOf(collection)))
                      .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))).execute();
        }
    }

    @Test
    void chunksAreSelectedFromTheSiblingByTheirQuarantinedAtWindow() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "w-a", "w-b", "w-c", "w-d");
        stamp(t, c, hs.get(0), "2026-09-01T00:00:00Z");
        stamp(t, c, hs.get(1), "2026-09-02T00:00:00Z");
        stamp(t, c, hs.get(2), "2026-09-15T00:00:00Z");
        stamp(t, c, hs.get(3), "2026-09-29T12:00:00.123456Z");   // the engine's fractional form

        // before 09-10: the two September chunks
        var early = vectors.quarantineRestoreSelected(t, c, quarantineOf(c), null, Instant.parse("2026-09-10T00:00:00Z"),
            null, 100, ACTOR, true);
        assertThat(early.wouldRestore()).containsExactlyInAnyOrder(hs.get(0), hs.get(1));
        assertThat(early.dryRun()).isTrue();
        assertThat(inCollection(t, quarantineOf(c), hs.get(0))).as("a dry run moves nothing").isTrue();

        // since 09-10 before 09-20: exactly the mid one
        var mid = vectors.quarantineRestoreSelected(t, c, quarantineOf(c), Instant.parse("2026-09-10T00:00:00Z"),
            Instant.parse("2026-09-20T00:00:00Z"), null, 100, ACTOR, false);
        assertThat(mid.restored()).containsExactly(hs.get(2));

        // since 09-29T12:00:00Z: the fractional-second stamp is inside the window
        var late = vectors.quarantineRestoreSelected(t, c, quarantineOf(c), Instant.parse("2026-09-29T12:00:00Z"), null,
            null, 100, ACTOR, false);
        assertThat(late.restored()).containsExactly(hs.get(3));
        assertThat(inCollection(t, quarantineOf(c), hs.get(0))).as("outside both windows: untouched").isTrue();
        assertThat(audit(t, "quarantine_restore")).hasSize(2);
    }

    @Test
    void aSelectionIsPagedByChashAndNamesTheNextCursor() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "pg-1", "pg-2", "pg-3");
        Instant since = Instant.parse("2026-01-01T00:00:00Z");

        var first = vectors.quarantineRestoreSelected(t, c, quarantineOf(c), since, null, null, 2, ACTOR, false);
        assertThat(first.restored()).hasSize(2);
        assertThat(first.nextAfter()).as("a full page names where the next one starts").isNotNull();

        var second = vectors.quarantineRestoreSelected(t, c, quarantineOf(c), since, null, first.nextAfter(), 2, ACTOR, false);
        assertThat(second.restored()).hasSize(1);
        assertThat(second.nextAfter()).as("a short page is the last").isNull();
        assertThat(java.util.stream.Stream.concat(first.restored().stream(), second.restored().stream()))
            .containsExactlyInAnyOrderElementsOf(hs);
    }

    @Test
    void aSelectionNeedsAWindow_andOnlyTakesChunksQuarantinedFromThisOrigin() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "scoped").get(0);

        assertThatThrownBy(() -> vectors.quarantineRestoreSelected(t, c, quarantineOf(c), null, null, null, 10, ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("quarantined_since");
        assertThatThrownBy(() -> vectors.quarantineRestoreSelected(t, c, quarantineOf(c),
                Instant.parse("2026-02-01T00:00:00Z"), Instant.parse("2026-01-01T00:00:00Z"), null, 10, ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class);

        // The same sibling name, asked for as another origin: the chunk's origin_collection stamp excludes it.
        String other = col("knowledge");
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), t, other);
        }
        var none = vectors.quarantineRestoreSelected(t, other, quarantineOf(c), Instant.parse("2026-01-01T00:00:00Z"),
            null, null, 10, ACTOR, false);
        assertThat(none.rows()).isEmpty();
        assertThat(inCollection(t, quarantineOf(c), h)).isTrue();
    }

    @Test
    void aGcQuarantineOrphansAuditRowIsOnlyASample_soItIsRefusedAndTheSelectionIsTheWayBack() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String a = orphan(t, c, "gq-a");
        String b = orphan(t, c, "gq-b");
        ReapableFixtures.agePastGrace(pg, t, c);
        // The route nx index repo and nx t3 gc quarantine through: it audits a SAMPLE (here 1 of the 2 it moved).
        var moved = vectors.quarantineOrphans(t, c, quarantineOf(c), "2026-10-01T12:00:00Z", 1);
        assertThat(moved.moved()).isEqualTo(2);
        long auditId = audit(t, "gc_quarantine_orphans").get(0).id();

        assertThatThrownBy(() -> vectors.quarantineRestoreFromAudit(t, c, quarantineOf(c), auditId, 0, 10, ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("sample").hasMessageContaining("quarantined_since");
        assertThat(inCollection(t, quarantineOf(c), a)).as("nothing moved").isTrue();

        var out = vectors.quarantineRestoreSelected(t, c, quarantineOf(c), Instant.parse("2026-10-01T00:00:00Z"), null,
            null, 100, ACTOR, false);
        assertThat(out.restored()).containsExactlyInAnyOrder(a, b);
    }

    // ── tenant scope ─────────────────────────────────────────────────────────

    @Test
    void aTenantCannotRestoreAnotherTenantsQuarantinedChunk() throws Exception {
        String a = newTenant();
        String b = newTenant();
        String c = col("knowledge");
        String h = quarantined(a, c, "tenant-a-only").get(0);
        // Tenant B has the same two collections, registered and empty.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), b, c);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), b, quarantineOf(c));
        }

        QuarantineRestoreOutcome out = vectors.quarantineRestore(b, c, quarantineOf(c), List.of(h), ACTOR, false);

        assertThat(out.missing()).containsExactly(h);
        assertThat(inCollection(a, quarantineOf(c), h)).as("tenant A's quarantine is untouched").isTrue();
        assertThat(inCollection(b, c, h)).isFalse();
        assertThat(audit(a, "quarantine_restore")).isEmpty();
        assertThat(audit(b, "quarantine_restore")).isEmpty();
    }

    // ── the exclusive sweep gate ─────────────────────────────────────────────

    @Test
    void theMoveTakesTheExclusiveSweepGate_soAManifestWriterHoldingItSharedBlocksIt() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "gated").get(0);

        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock_shared",
                SQLDataType.OTHER, DSL.function("hashtext", SQLDataType.INTEGER,
                    DSL.val("sweepgate:" + t + "/" + c)))).execute();
            assertThatThrownBy(() -> vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false))
                .as("typed and retryable, not an opaque database error")
                .isInstanceOf(PgVectorRepository.QuarantineRestoreBusyException.class)
                .hasStackTraceContaining("sweep gate");
            holder.rollback();
        }
        assertThat(inCollection(t, quarantineOf(c), h)).as("a refused restore moved nothing").isTrue();
        // And once the gate is free it goes through.
        assertThat(vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false).restored()).containsExactly(h);
    }

    // ── refusals before any move ─────────────────────────────────────────────

    @Test
    void theArgumentsAreValidatedBeforeAnythingMoves() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "args").get(0);
        String q = quarantineOf(c);

        assertThatThrownBy(() -> vectors.quarantineRestore(t, q, q, List.of(h), ACTOR, false))
            .as("the origin may not itself be a quarantine collection")
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("quarantine");
        assertThatThrownBy(() -> vectors.quarantineRestore(t, c, c, List.of(h), ACTOR, false))
            .as("the sibling must be a quarantine collection")
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> vectors.quarantineRestore(t, c, "quarantine-" + col("knowledge"), List.of(h), ACTOR, false))
            .as("an unregistered sibling")
            .isInstanceOf(RuntimeException.class);
        assertThatThrownBy(() -> vectors.quarantineRestore(t, c, q, List.of(), ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> vectors.quarantineRestore(t, c, q, List.of("not-a-chash"), ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> vectors.quarantineRestore(t, c, q,
                java.util.Collections.nCopies(PgVectorRepository.MAX_QUARANTINE_RESTORE_CHASHES + 1, h), ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("at most");
        assertThat(inCollection(t, q, h)).isTrue();
    }

    // ── reattach: the chunk is visible again (nexus-wbfpw.49, Sam's decision 2026-10-01) ──────────

    @Test
    void aRestoredChunkWhoseDocumentIsLiveIsAttachedAndComesBackToSearchAndGet() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.1.1";
        String h = quarantinedWith(t, c, Map.of("legacy", Map.<String, Object>of(
            "catalog_doc_id", doc, "title", "Legacy Note", "chunk_index", 0))).get(0);
        liveDoc(t, c, doc, "Legacy Note", 1, "legacy.md", Map.of());

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        var row = out.rows().get(0);
        assertThat(row.outcome()).isEqualTo("restored");
        assertThat(row.reattach()).isEqualTo("attach");
        assertThat(row.attached()).isTrue();
        assertThat(row.owner()).isEqualTo(doc);
        assertThat(row.ownerTitle()).isEqualTo("Legacy Note");
        assertThat(row.position()).isZero();
        assertThat(row.noManifest()).as("it has a manifest row now").isFalse();
        assertThat(row.reapableAfter()).as("an owned chunk has no date to be reaped").isNull();
        assertThat(manifest(t, doc)).containsExactly(new ManifestRow(0, h, c));
        // The point of the whole step: the chunk comes back to the read paths, not just to the table.
        assertThat(visibleToGet(t, c, h)).as("returned by get").isTrue();
        assertThat(visibleToSearch(t, c, "legacy text", h)).as("returned by search").isTrue();
        assertThat(audit(t, "quarantine_restore")).singleElement().satisfies(a -> {
            assertThat(a.chashes()).contains(h);
            assertThat(a.details()).contains("\"attached\": 1").contains("\"reattach\": true");
        });
        // An owned chunk is not the reaper's, however old: the restore did not just buy a grace.
        ReapableFixtures.agePastGrace(pg, t, c);
        assertThat(reaper(t).runOnce(null).tenant(t).collection(c).moved()).isZero();
        assertThat(inCollection(t, c, h)).isTrue();
    }

    @Test
    void withoutReattachTheBytesComeBackHiddenWithTheOwnerLinkDropped_andARerunCannotAttachThePresentChunk()
            throws Exception {
        // nexus-l003p (RDR-236 Phase 4): the restored row carries the CHUNK keys only, so the forward key the plan read
        // (catalog_doc_id, chunk_index) is gone with the quarantine copy. A bytes-only restore says so on the verdict
        // row, and a rerun finds no owner. Restore with reattach (the default) is the way to attach a forward-keyed chunk.
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.2.1";
        String h = quarantinedWith(t, c, Map.of("legacy", Map.<String, Object>of(
            "catalog_doc_id", doc, "title", "Legacy Note", "chunk_index", 0))).get(0);
        liveDoc(t, c, doc, "Legacy Note", 1, "legacy.md", Map.of());

        QuarantineRestoreOutcome dryReattachOff =
            vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, true, false);
        QuarantineRestoreOutcome dryReattachOn =
            vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, true, true);
        assertThat(reasonOf(dryReattachOff, h)).as("the dry run predicts the warning").isEqualTo("owner_link_dropped");
        assertThat(reasonOf(dryReattachOn, h)).as("a run with reattach on drops nothing").isNull();

        QuarantineRestoreOutcome bytesOnly =
            vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false, false);

        var row = bytesOnly.rows().get(0);
        assertThat(row.outcome()).isEqualTo("restored");
        assertThat(row.attached()).isFalse();
        assertThat(row.reattach()).as("what reattach WOULD have done is still reported").isEqualTo("attach");
        assertThat(row.reason()).as("and the verdict row warns that the owner link does not survive")
            .isEqualTo("owner_link_dropped");
        assertThat(row.owner()).as("the owner the link named").isEqualTo(doc);
        assertThat(row.position()).as("and the position").isZero();
        assertThat(row.noManifest()).isTrue();
        assertThat(row.reapableAfter()).isNotNull();
        assertThat(manifest(t, doc)).isEmpty();
        assertThat(visibleToGet(t, c, h)).as("hidden: restored, but no live owner row names it").isFalse();
        assertThat(visibleToSearch(t, c, "legacy text", h)).isFalse();
        assertThat(metadataOf(chunk(t, c, h))).as("the restored row holds the CHUNK keys and nothing else")
            .containsOnlyKeys("chunk_text_hash", "embedding_model");
        assertThat(audit(t, "quarantine_restore")).singleElement().satisfies(a -> {
            assertThat(a.details()).contains("\"reattach\": false").contains("\"attached\": 0");
            // The audit row records what the strip cost: the dropped link, with the owner and position it named.
            assertThat(a.details()).contains("\"owner_link_dropped\": 1");
            assertThat(a.details()).contains("\"owner_link_dropped_chashes\": [{\"chash\": \"" + h + "\", \"owner\": \""
                + doc + "\", \"position\": 0}]");
        });

        // The rerun with reattach on: the chunk is present and its row names no document, so there is no owner.
        QuarantineRestoreOutcome again = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
        assertThat(again.present()).containsExactly(h);
        assertThat(again.attached()).isEmpty();
        assertThat(verdictOf(again, h)).isEqualTo("no_live_owner");
        assertThat(reasonOf(again, h)).as("a chunk with no owner carries no warning").isNull();
        assertThat(manifest(t, doc)).isEmpty();
        assertThat(visibleToGet(t, c, h)).isFalse();
        assertThat(audit(t, "quarantine_restore")).as("the rerun moved and attached nothing: no second row").hasSize(1);
    }

    @Test
    void aSingleCallRestoreWithReattachStillAttachesAForwardKeyedChunk_whileTheRestoredRowHoldsChunkKeysOnly()
            throws Exception {
        // The plan is judged from the quarantine copy before the move, so the stripped row still attaches in the call.
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.2.2";
        Map<String, Object> keyed = new java.util.LinkedHashMap<>();
        keyed.put("catalog_doc_id", doc);
        keyed.put("chunk_index", 1);
        keyed.put("title", "Forward");
        keyed.put("line_start", 10);
        keyed.put("line_end", 20);
        keyed.put("chunk_start_char", 100);
        keyed.put("chunk_end_char", 400);
        // A stale chunk_text_hash: the CHUNK key is derived from the chash column, never copied.
        keyed.put("chunk_text_hash", "0".repeat(64));
        String h = quarantinedWith(t, c, Map.of("forward", keyed)).get(0);
        liveDoc(t, c, doc, "Forward", 2, "forward.md", Map.of());

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        assertThat(out.restored()).containsExactly(h);
        assertThat(out.attached()).containsExactly(h);
        assertThat(reasonOf(out, h)).as("an attach has no reason").isNull();
        assertThat(manifest(t, doc)).containsExactly(new ManifestRow(1, h, c));
        assertThat(manifestSpan(t, doc, 1)).as("the span keys of the quarantine copy reach the manifest row")
            .containsExactly(10, 20, 100, 400);
        assertThat(metadataOf(chunk(t, c, h))).containsOnlyKeys("chunk_text_hash", "embedding_model")
            .containsEntry("chunk_text_hash", h);
        assertThat(visibleToGet(t, c, h)).isTrue();
    }

    @Test
    void aNoteFoundByItsOwnDocIdAttachesOnARerunAfterABytesOnlyRestore_withNoWarning() throws Exception {
        // The reverse path reads the catalog (the note's metadata.doc_id equals the chash), not the chunk row, so the
        // strip costs it nothing: no warning on the bytes-only restore, and the rerun attaches.
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantinedWith(t, c, Map.of("note", Map.<String, Object>of("title", "A note"))).get(0);
        liveDoc(t, c, "1.2.3", "A note", 1, "", Map.of("doc_id", h));

        QuarantineRestoreOutcome bytesOnly =
            vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false, false);
        assertThat(verdictOf(bytesOnly, h)).isEqualTo("attach");
        assertThat(reasonOf(bytesOnly, h)).as("nothing is lost: the note is found by the chash").isNull();
        assertThat(bytesOnly.attached()).isEmpty();

        QuarantineRestoreOutcome again = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
        assertThat(again.present()).containsExactly(h);
        assertThat(again.attached()).containsExactly(h);
        assertThat(manifest(t, "1.2.3")).containsExactly(new ManifestRow(0, h, c));
        assertThat(visibleToGet(t, c, h)).isTrue();
    }

    @Test
    void aBytesOnlyRestoreOfAChunkTheForwardKeyCannotPlaceCarriesNoWarning() throws Exception {
        // The warning is for a link that would have attached. A chunk the plan refuses (a document mid index run) or
        // that names no live document loses nothing by the strip, so its reason stays the plan's own.
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("indexing", Map.of("catalog_doc_id", "1.2.4", "chunk_index", 0));
        meta.put("ownerless", Map.of("title", "no owner named"));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.2.4", "Mid run", 1, "m.md", Map.of());
        setIndexState(t, "1.2.4", "indexing");

        QuarantineRestoreOutcome out =
            vectors.quarantineRestore(t, c, quarantineOf(c), hs, ACTOR, false, false);

        assertThat(verdictOf(out, hs.get(0))).isEqualTo("superseded");
        assertThat(reasonOf(out, hs.get(0))).isEqualTo("indexing");
        assertThat(out.kept()).as("a mid-run owner keeps the chunk in quarantine, its link intact").containsExactly(hs.get(0));
        assertThat(verdictOf(out, hs.get(1))).isEqualTo("no_live_owner");
        assertThat(reasonOf(out, hs.get(1))).isNull();
    }

    @Test
    void aPresentKeyedChunkIsNotTouchedByTheRestore_soItsOwnKeysStillReattachItOnTheNextCall() throws Exception {
        // present = the origin already has the row (a client write that still carries its document keys, written
        // before the Phase 4 sweep): the restore never rewrites it, so the plan still reads its keys, with no warning.
        String t = newTenant();
        String c = col("knowledge");
        quarantined(t, c, "something-else");
        String h = orphan(t, c, "live-keyed", Map.<String, Object>of(
            "catalog_doc_id", "1.2.5", "chunk_index", 0, "title", "p"));
        liveDoc(t, c, "1.2.5", "P", 1, "p.md", Map.of());

        QuarantineRestoreOutcome bytesOnly =
            vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false, false);
        assertThat(bytesOnly.present()).containsExactly(h);
        assertThat(reasonOf(bytesOnly, h)).as("nothing is stripped, so nothing is dropped").isNull();
        assertThat(metadataOf(chunk(t, c, h))).containsEntry("title", "p").containsEntry("catalog_doc_id", "1.2.5");

        QuarantineRestoreOutcome again = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
        assertThat(again.attached()).containsExactly(h);
        assertThat(manifest(t, "1.2.5")).containsExactly(new ManifestRow(0, h, c));
    }

    @Test
    void aPositionTheDocumentsManifestHoldsAnotherChunkAtIsSuperseded_andTheManifestIsLeftExactlyAsItWas()
            throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.3.1";
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("old-0", Map.of("catalog_doc_id", doc, "chunk_index", 0));     // position 0 is taken: re-indexed
        meta.put("old-5", Map.of("catalog_doc_id", doc, "chunk_index", 5));     // past the registered 3: a longer old version
        meta.put("dup-a", Map.of("catalog_doc_id", doc, "chunk_index", 1));     // two chunks claim position 1
        meta.put("dup-b", Map.of("catalog_doc_id", doc, "chunk_index", 1));
        meta.put("fresh", Map.of("catalog_doc_id", doc, "chunk_index", 2));     // a free position inside the count: attaches
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, doc, "Re-indexed", 3, "doc.md", Map.of());
        String current0 = manifested(t, c, doc, 0, "current-0");

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), hs, ACTOR, false);

        assertThat(out.restored()).as("the bytes come back, except the two claimants of one position")
            .containsExactlyInAnyOrder(hs.get(0), hs.get(1), hs.get(4));
        assertThat(out.kept()).as("a rival is not moved: it stays in quarantine with its keys")
            .containsExactlyInAnyOrder(hs.get(2), hs.get(3));
        assertThat(inCollection(t, c, hs.get(2))).isFalse();
        assertThat(inCollection(t, quarantineOf(c), hs.get(2))).isTrue();
        assertThat(inCollection(t, quarantineOf(c), hs.get(3))).isTrue();
        assertThat(metadataOf(chunk(t, quarantineOf(c), hs.get(2)))).containsEntry("catalog_doc_id", doc)
            .containsEntry("chunk_index", 1);
        assertThat(verdictOf(out, hs.get(0))).as("position 0 holds another chunk").isEqualTo("superseded");
        assertThat(reasonOf(out, hs.get(0))).isEqualTo("position_taken");
        assertThat(verdictOf(out, hs.get(1))).as("past the document's registered chunk_count").isEqualTo("superseded");
        assertThat(reasonOf(out, hs.get(1))).isEqualTo("past_end");
        assertThat(verdictOf(out, hs.get(2))).as("two claimants of one position: neither is written").isEqualTo("superseded");
        assertThat(verdictOf(out, hs.get(3))).isEqualTo("superseded");
        assertThat(reasonOf(out, hs.get(2))).isEqualTo("rival");
        assertThat(reasonOf(out, hs.get(3))).isEqualTo("rival");
        assertThat(reasonOf(out, hs.get(4))).as("an attach has no reason").isNull();
        assertThat(verdictOf(out, hs.get(4))).as("the control: a free position inside the count attaches").isEqualTo("attach");
        assertThat(out.attached()).containsExactly(hs.get(4));
        assertThat(manifest(t, doc)).as("the existing row is untouched and only the free position was added")
            .containsExactly(new ManifestRow(0, current0, c), new ManifestRow(2, hs.get(4), c));
        for (int i = 0; i < 4; i++) {
            assertThat(visibleToGet(t, c, hs.get(i))).as("superseded stays hidden: " + i).isFalse();
        }
        assertThat(visibleToGet(t, c, hs.get(4))).isTrue();
        assertThat(visibleToGet(t, c, current0)).as("the re-indexed content is exactly as visible as before").isTrue();
        assertThat(out.rows().get(0).noManifest()).isTrue();
        assertThat(audit(t, "quarantine_restore")).singleElement().satisfies(a ->
            assertThat(a.details()).contains("\"superseded\": 4").contains("\"attached\": 1")
                .contains("\"kept\": 2").contains("\"restored\": 3"));
    }

    @Test
    void aChunkWithNoLiveOwnerIsRestoredAsBytesOnly_hidden_andTheManifestIsNeverWritten() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String other = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("nobody", Map.of("title", "orphan"));
        meta.put("tomb", Map.of("catalog_doc_id", "1.4.1", "chunk_index", 0));
        meta.put("elsewhere", Map.of("catalog_doc_id", "1.4.2", "chunk_index", 0));
        meta.put("multi", Map.of("catalog_doc_id", "1.4.3"));                   // multi-chunk document, no chunk_index
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.4.1", "Deleted", 1, "x.md", Map.of());
        tombstone(t, "1.4.1");
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), t, other);
        }
        liveDoc(t, other, "1.4.2", "Lives Elsewhere", 1, "y.md", Map.of());
        liveDoc(t, c, "1.4.3", "Multi", 4, "z.md", Map.of());

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), hs, ACTOR, false);

        assertThat(out.restored()).containsExactlyInAnyOrderElementsOf(hs);
        assertThat(verdictOf(out, hs.get(0))).isEqualTo("no_live_owner");
        assertThat(verdictOf(out, hs.get(1))).as("a tombstoned owner is not live").isEqualTo("no_live_owner");
        assertThat(verdictOf(out, hs.get(2))).as("a live owner registered under another collection").isEqualTo("no_live_owner");
        assertThat(verdictOf(out, hs.get(3))).as("a live owner, but nothing says where the chunk goes").isEqualTo("no_position");
        assertThat(out.attached()).isEmpty();
        for (String doc : List.of("1.4.1", "1.4.2", "1.4.3")) {
            assertThat(manifest(t, doc)).as("no manifest row is invented for " + doc).isEmpty();
        }
        for (String h : hs) {
            assertThat(inCollection(t, c, h)).as("the bytes are back").isTrue();
            assertThat(visibleToGet(t, c, h)).as("and they stay hidden from get").isFalse();
        }
        assertThat(out.rows().get(0).chunkTitle()).as("the chunk's own title, for the re-put recipe").isEqualTo("orphan");
        assertThat(out.rows().get(2).owner()).as("the report still names the owner it resolved").isEqualTo("1.4.2");
        assertThat(out.rows().get(0).noManifest()).isTrue();
        assertThat(out.rows().get(0).reapableAfter()).isNotNull();
    }

    @Test
    void aNoteTheCensusFindsByItsOwnDocIdIsAttachedAtPositionZero_andAnAmbiguousNoteIsNot() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantinedWith(t, c, new java.util.LinkedHashMap<>(Map.of(
            "note-one", Map.<String, Object>of("title", "Note One"))));
        String lone = hs.get(0);
        String twin = quarantinedWith(t, c, Map.of("note-two", Map.<String, Object>of("title", "Note Two"))).get(0);
        // The reverse path: no forward key on the chunk; a live note-shaped (empty file_path) document of this
        // collection carries the chunk's chash as its own metadata.doc_id.
        liveDoc(t, c, "1.5.1", "Note One", 1, "", Map.of("doc_id", lone));
        liveDoc(t, c, "1.5.2", "Note Two A", 1, "", Map.of("doc_id", twin));
        liveDoc(t, c, "1.5.3", "Note Two B", 1, "", Map.of("doc_id", twin));

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(lone, twin), ACTOR, false);

        assertThat(verdictOf(out, lone)).isEqualTo("attach");
        assertThat(manifest(t, "1.5.1")).containsExactly(new ManifestRow(0, lone, c));
        assertThat(visibleToGet(t, c, lone)).isTrue();
        assertThat(verdictOf(out, twin)).as("two live notes claim it: the restore does not pick a winner").isEqualTo("no_live_owner");
        assertThat(manifest(t, "1.5.2")).isEmpty();
        assertThat(manifest(t, "1.5.3")).isEmpty();
        assertThat(visibleToGet(t, c, twin)).isFalse();
    }

    @Test
    void aDocumentThatIsMidIndexRun_orHoldsManifestRowsUnderAnotherCollection_orAReverseNoteWithRows_isNotExtended()
            throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("indexing", Map.of("catalog_doc_id", "1.9.1", "chunk_index", 0));
        meta.put("elsewhere-rows", Map.of("catalog_doc_id", "1.9.2", "chunk_index", 1));
        meta.put("control", Map.of("catalog_doc_id", "1.9.3", "chunk_index", 0));
        meta.put("note-with-rows", Map.of("title", "A note that already has a manifest"));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.9.1", "Mid run", 2, "a.md", Map.of());
        setIndexState(t, "1.9.1", "indexing");
        liveDoc(t, c, "1.9.2", "Copied", 3, "b.md", Map.of());
        manifestUnderAnotherCollection(t, "1.9.2", col("knowledge"), 0, "copy-0");
        liveDoc(t, c, "1.9.3", "Control", 2, "c.md", Map.of());
        liveDoc(t, c, "1.9.4", "Note", 1, "", Map.of("doc_id", hs.get(3)));
        manifested(t, c, "1.9.4", 3, "note-row");        // the reverse path wants a note with NO manifest rows at all

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), hs, ACTOR, false);

        assertThat(verdictOf(out, hs.get(0))).as("an index run is rewriting this manifest").isEqualTo("superseded");
        assertThat(reasonOf(out, hs.get(0))).isEqualTo("indexing");
        assertThat(verdictOf(out, hs.get(1))).as("its manifest lives under another collection").isEqualTo("superseded");
        assertThat(reasonOf(out, hs.get(1))).isEqualTo("other_collection");
        assertThat(reasonOf(out, hs.get(3))).isEqualTo("has_rows");
        assertThat(verdictOf(out, hs.get(2))).as("the control: the same shape without either problem attaches")
            .isEqualTo("attach");
        assertThat(verdictOf(out, hs.get(3))).as("a note that already has a manifest row is not the census's candidate")
            .isEqualTo("superseded");
        assertThat(out.attached()).containsExactly(hs.get(2));
        assertThat(manifest(t, "1.9.1")).isEmpty();
        assertThat(manifest(t, "1.9.4")).extracting(ManifestRow::position).containsExactly(3);
    }

    @Test
    void theSqlBackstopsRefuseANonLiveOriginAndAMalformedChashWhenCalledDirectly() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "backstop").get(0);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "disputed")
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t).and(CATALOG_COLLECTIONS.NAME.eq(c))).execute();
            assertThatThrownBy(() -> ctx.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(
                    t, c, quarantineOf(c), new String[] {h}, ACTOR, (Long) null, false, true)).fetch())
                .hasStackTraceContaining("not a registered live collection");
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "live")
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t).and(CATALOG_COLLECTIONS.NAME.eq(c))).execute();
            assertThatThrownBy(() -> ctx.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(
                    t, c, quarantineOf(c), new String[] {"NOT-HEX"}, ACTOR, (Long) null, false, true)).fetch())
                .hasStackTraceContaining("64 lowercase hex");
        }
        assertThat(inCollection(t, quarantineOf(c), h)).as("both refusals moved nothing").isTrue();
    }

    @Test
    void aDryRunSaysWhatReattachWouldDo_andWritesNothingAnywhere() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.6.1";
        List<String> hs = quarantinedWith(t, c, new java.util.LinkedHashMap<>(Map.of(
            "would-attach", Map.<String, Object>of("catalog_doc_id", doc, "chunk_index", 1))));
        String blocked = quarantinedWith(t, c, Map.of("blocked", Map.<String, Object>of(
            "catalog_doc_id", doc, "chunk_index", 0))).get(0);
        liveDoc(t, c, doc, "Dry", 2, "dry.md", Map.of());
        manifested(t, c, doc, 0, "current");

        QuarantineRestoreOutcome out =
            vectors.quarantineRestore(t, c, quarantineOf(c), List.of(hs.get(0), blocked), ACTOR, true);

        assertThat(out.wouldRestore()).containsExactlyInAnyOrder(hs.get(0), blocked);
        assertThat(verdictOf(out, hs.get(0))).isEqualTo("attach");
        assertThat(verdictOf(out, blocked)).isEqualTo("superseded");
        assertThat(out.attached()).isEmpty();
        assertThat(manifest(t, doc)).as("no manifest row written by a dry run").hasSize(1);
        assertThat(inCollection(t, quarantineOf(c), hs.get(0))).isTrue();
        assertThat(audit(t, "quarantine_restore")).isEmpty();
    }

    @Test
    void aDocumentOfAnotherTenantIsNeverAnOwner_andNeverGainsAManifestRow() throws Exception {
        String a = newTenant();
        String b = newTenant();
        String c = col("knowledge");
        String doc = "1.7.1";
        String h = quarantinedWith(a, c, Map.of("x-tenant", Map.<String, Object>of(
            "catalog_doc_id", doc, "chunk_index", 0))).get(0);
        // Tenant B has the same collection and a LIVE document with the very tumbler the chunk names.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), b, c);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), b, quarantineOf(c));
        }
        liveDoc(b, c, doc, "B's document", 1, "b.md", Map.of());

        QuarantineRestoreOutcome asA = vectors.quarantineRestore(a, c, quarantineOf(c), List.of(h), ACTOR, false);
        assertThat(asA.restored()).containsExactly(h);
        assertThat(verdictOf(asA, h)).as("A has no such document; B's does not count").isEqualTo("no_live_owner");
        assertThat(manifest(b, doc)).as("B's manifest is untouched").isEmpty();
        assertThat(manifest(a, doc)).isEmpty();

        // And B cannot restore A's chunk at all.
        String h2 = quarantinedWith(a, col("knowledge"), Map.of("only-a", Map.<String, Object>of("title", "x"))).get(0);
        QuarantineRestoreOutcome asB = vectors.quarantineRestore(b, c, quarantineOf(c), List.of(h2), ACTOR, false);
        assertThat(asB.missing()).containsExactly(h2);
        assertThat(manifest(b, doc)).isEmpty();
    }

    @Test
    void thePlanNamesTheTenantItself_soAnotherTenantsDocumentIsNotAnOwnerEvenWithRowLevelSecurityOff() throws Exception {
        // Every other tenant-isolation test here runs as nexus_svc, where FORCE RLS filters a foreign row before the
        // statement's own tenant_id predicate could be the thing that excludes it. This calls the function as the
        // superuser, which bypasses RLS, so only the predicates stand between tenant A's call and tenant B's row.
        String a = newTenant();
        String b = newTenant();
        String c = col("knowledge");
        String doc = "1.10.1";
        String h = quarantinedWith(a, c, Map.of("x", Map.<String, Object>of("catalog_doc_id", doc, "chunk_index", 0))).get(0);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), b, c);
        }
        liveDoc(b, c, doc, "B's document", 1, "b.md", Map.of());

        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES).selectFrom(QUARANTINE_RESTORE_CHUNKS.call(
                a, c, quarantineOf(c), new String[] {h}, ACTOR, (Long) null, false, true)).fetch();
            assertThat(rows.get(0).get(QUARANTINE_RESTORE_CHUNKS.R_OUTCOME)).isEqualTo("restored");
            assertThat(rows.get(0).get(QUARANTINE_RESTORE_CHUNKS.R_REATTACH))
                .as("tenant B's live document is not tenant A's owner").isEqualTo("no_live_owner");
        }
        assertThat(manifest(b, doc)).isEmpty();
        assertThat(manifest(a, doc)).isEmpty();
    }

    @Test
    void aHeldIndexRunLockOfTheOwningDocumentRollsTheWholeCallBack_withNothingMovedOrAttached() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.8.1";
        String h = quarantinedWith(t, c, Map.of("locked", Map.<String, Object>of(
            "catalog_doc_id", doc, "chunk_index", 0))).get(0);
        liveDoc(t, c, doc, "Locked", 1, "l.md", Map.of());

        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock",
                SQLDataType.OTHER, DSL.function("hashtext", SQLDataType.INTEGER,
                    DSL.val("indexrun:" + t + ":" + doc)))).execute();
            assertThatThrownBy(() -> vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false))
                .isInstanceOf(PgVectorRepository.QuarantineRestoreBusyException.class);
            holder.rollback();
        }
        assertThat(inCollection(t, quarantineOf(c), h)).as("rolled back whole: still in quarantine").isTrue();
        assertThat(inCollection(t, c, h)).isFalse();
        assertThat(manifest(t, doc)).isEmpty();
        assertThat(audit(t, "quarantine_restore")).isEmpty();
        assertThat(vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false).attached())
            .as("and the same call goes through once the lock is free").containsExactly(h);
    }

    // ── review round 2 ───────────────────────────────────────────────────────

    @Test
    void theReapersOwnTagsAreStrippedToo_andAChunkRestoredThenQuarantinedAgainRoundTripsClean() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "tagged").get(0);
        // The reaper's move also writes quarantined_by and reaper_quarantined_at (the nexus-2x9xa round-3 branch).
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Map<String, Object> meta = metadataOf(chunk(t, quarantineOf(c), h));
            meta.put("quarantined_by", "engine-reaper");
            meta.put("reaper_quarantined_at", "2026-09-01T00:00:00Z");
            ctx.update(CHUNKS).set(CHUNKS.METADATA, JSONB.jsonb(MAPPER.writeValueAsString(meta)))
               .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(quarantineOf(c)))
                      .and(CHUNKS.CHASH.eq(Chash.fromHex(h).toBytes()))).execute();
        }

        vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
        assertThat(metadataOf(chunk(t, c, h))).as("nexus-l003p: the CHUNK keys only, so the title does not survive")
            .containsOnlyKeys("chunk_text_hash", "embedding_model")
            .doesNotContainKeys("title", "quarantined_at", "origin_collection", "quarantined_by", "reaper_quarantined_at");

        // Restore, let it age past the grace, and the reaper takes it again; it must restore again, clean.
        ReapableFixtures.agePastGrace(pg, t, c);
        assertThat(reaper(t).runOnce(null).tenant(t).collection(c).moved()).isEqualTo(1);
        assertThat(inCollection(t, quarantineOf(c), h)).as("quarantined once more").isTrue();
        QuarantineRestoreOutcome second = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
        assertThat(second.restored()).containsExactly(h);
        assertThat(metadataOf(chunk(t, c, h))).containsOnlyKeys("chunk_text_hash", "embedding_model")
            .doesNotContainKeys("title", "quarantined_at", "origin_collection", "quarantined_by", "reaper_quarantined_at");
    }

    @Test
    void twoOriginsChunksInOneSiblingAreNeverRestoredIntoTheWrongOrigin() throws Exception {
        String t = newTenant();
        String a = col("knowledge");
        String b = col("knowledge");
        String sibling = quarantineOf(a);
        String forA = Chash.ofText("for-a").toHex();
        String forB = Chash.ofText("for-b").toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, t, a);
            PgContainerHelper.insertCollection(ctx, t, b);
            PgContainerHelper.insertCollection(ctx, t, sibling);
            // One sibling, two origins: what a catalog row that disagrees with its name produces.
            PgContainerHelper.insertChunks(ctx, t, sibling, List.of(forA), List.of("for a text"),
                embedder.embed(List.of("for a text")), List.of(Map.<String, Object>of("origin_collection", a)));
            PgContainerHelper.insertChunks(ctx, t, sibling, List.of(forB), List.of("for b text"),
                embedder.embed(List.of("for b text")), List.of(Map.<String, Object>of("origin_collection", b)));
        }

        // The dry run and the real run agree: B's chunk is not A's to take.
        QuarantineRestoreOutcome dry = vectors.quarantineRestore(t, a, sibling, List.of(forA, forB), ACTOR, true);
        assertThat(dry.wouldRestore()).containsExactly(forA);
        assertThat(dry.missing()).containsExactly(forB);
        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, a, sibling, List.of(forA, forB), ACTOR, false);

        assertThat(out.restored()).containsExactly(forA);
        assertThat(out.missing()).as("another origin's chunk reads missing").containsExactly(forB);
        assertThat(inCollection(t, a, forB)).as("never restored into A").isFalse();
        assertThat(inCollection(t, sibling, forB)).as("still where it was").isTrue();
        // ...and it is B's to take, through the same sibling.
        assertThat(vectors.quarantineRestore(t, b, sibling, List.of(forB), ACTOR, false).restored()).containsExactly(forB);
        assertThat(inCollection(t, b, forB)).isTrue();
    }

    @Test
    void anAuditRowNamesTheSiblingItMovedInto_andACallNamingAnotherSiblingIsRefused() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        quarantined(t, c, "named");
        long auditId = audit(t, "reaper_quarantine").get(0).id();
        String wrong = quarantineOf(col("knowledge"));
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), t, wrong);
        }

        assertThatThrownBy(() -> vectors.quarantineRestoreFromAudit(t, c, wrong, auditId, 0, 10, ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("moved its chunks into " + quarantineOf(c)).hasMessageContaining(wrong);
    }

    @Test
    void aRestoreIntoAnOriginThatIsNotLiveIsRefusedBeforeAnythingMoves() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "dormant").get(0);
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "dormant")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t).and(CATALOG_COLLECTIONS.NAME.eq(c))).execute();
        }

        assertThatThrownBy(() -> vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("not a live collection");
        assertThat(inCollection(t, quarantineOf(c), h)).isTrue();
    }

    @Test
    void theReapableAgainDateIsUtcWhateverTheSessionTimeZone() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "tz").get(0);

        // Called directly over a session whose TimeZone is nine hours ahead: the engine pins UTC, psql does not.
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).select(DSL.function("set_config", String.class,
                DSL.val("TimeZone"), DSL.val("Asia/Tokyo"), DSL.val(false))).fetch();
            DSL.using(su, SQLDialect.POSTGRES).selectFrom(QUARANTINE_RESTORE_CHUNKS.call(
                t, c, quarantineOf(c), new String[] {h}, ACTOR, (Long) null, false, false)).fetch();
        }

        var details = MAPPER.readTree(audit(t, "quarantine_restore").get(0).details());
        Instant said = Instant.parse(details.get("reapable_again_after").asText());
        Instant written = chunk(t, c, h).lastWrittenAt().toInstant();
        assertThat(said).as("labelled Z, so it must BE UTC").isBetween(
            written.plus(Duration.ofDays(30)).minus(Duration.ofMinutes(5)),
            written.plus(Duration.ofDays(30)).plus(Duration.ofMinutes(5)));
    }

    // ── round 3 (nexus-wbfpw.49): complete documents, version ambiguity, reasons, partial attach ─────────

    @Test
    void anEmptiedCompleteDocumentIsNotAReattachTarget_butAnUnstampedLegacyOneOfTheSameShapeIs() throws Exception {
        // The reviewer's probe: chunk_count 0 reads as "unknown" to the position checks, but a document stamped
        // complete with no manifest rows was emptied on purpose, and its manifest is authoritative: a chunk of it
        // that no row names is stale by definition.
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("stale", Map.of("catalog_doc_id", "1.20.1", "chunk_index", 3));
        meta.put("legacy", Map.of("catalog_doc_id", "1.20.2", "chunk_index", 3));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.20.1", "Emptied", 0, "e.md", Map.of());
        setIndexState(t, "1.20.1", "complete");
        liveDoc(t, c, "1.20.2", "Legacy, unstamped", 0, "l.md", Map.of());

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), hs, ACTOR, false);

        assertThat(verdictOf(out, hs.get(0))).isEqualTo("superseded");
        assertThat(reasonOf(out, hs.get(0))).isEqualTo("complete");
        assertThat(manifest(t, "1.20.1")).as("a stale chunk is not written into an emptied document").isEmpty();
        assertThat(visibleToGet(t, c, hs.get(0))).isFalse();
        assertThat(verdictOf(out, hs.get(1))).as("the control: an unstamped legacy document (the R8 case) attaches")
            .isEqualTo("attach");
        assertThat(manifest(t, "1.20.2")).containsExactly(new ManifestRow(3, hs.get(1), c));
    }

    @Test
    void aChunkCutFromAnotherVersionOfTheFileIsSupersededByItsContentHash() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("other-version", Map.of("catalog_doc_id", "1.21.1", "chunk_index", 0, "content_hash", "bbb"));
        meta.put("same-version", Map.of("catalog_doc_id", "1.21.1", "chunk_index", 1, "content_hash", "aaa"));
        meta.put("no-hash", Map.of("catalog_doc_id", "1.21.1", "chunk_index", 2));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.21.1", "Versioned", 3, "v.md", Map.of());
        setContentHash(t, "1.21.1", "aaa");

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), hs, ACTOR, false);

        assertThat(verdictOf(out, hs.get(0))).isEqualTo("superseded");
        assertThat(reasonOf(out, hs.get(0))).isEqualTo("version");
        assertThat(verdictOf(out, hs.get(1))).as("the same content hash attaches").isEqualTo("attach");
        assertThat(verdictOf(out, hs.get(2))).as("a chunk that records no hash is not refused for it").isEqualTo("attach");
        assertThat(manifest(t, "1.21.1")).extracting(ManifestRow::position).containsExactly(1, 2);
    }

    @Test
    void twoVersionsOfOnePositionStayInQuarantineAndAreRefusedInEverySeparateCall_inEitherOrder() throws Exception {
        // nexus-l003p: a rival is NOT moved, so it keeps naming its position and the other version keeps seeing it.
        // The page-split case: each call names only one of the two, so a per-call check alone sees no claim at all.
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("v1", Map.of("catalog_doc_id", "1.22.1", "chunk_index", 0, "title", "v1"));
        meta.put("v2", Map.of("catalog_doc_id", "1.22.1", "chunk_index", 0, "title", "v2"));
        meta.put("alone", Map.of("catalog_doc_id", "1.22.1", "chunk_index", 1));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.22.1", "Two versions", 2, "p.md", Map.of());

        for (int order : new int[] {0, 1, 0, 1}) {
            String h = hs.get(order);
            QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
            assertThat(out.rows().get(0).outcome()).as("kept, not restored: " + h).isEqualTo("kept");
            assertThat(verdictOf(out, h)).isEqualTo("superseded");
            assertThat(reasonOf(out, h)).isEqualTo("rival");
            assertThat(out.restored()).isEmpty();
            assertThat(out.attached()).isEmpty();
            assertThat(out.auditId()).as("a call that moved and attached nothing writes no audit row").isNull();
            assertThat(inCollection(t, quarantineOf(c), h)).as("still in quarantine").isTrue();
            assertThat(inCollection(t, c, h)).as("not in the collection").isFalse();
            assertThat(metadataOf(chunk(t, quarantineOf(c), h))).as("with the keys it came with")
                .containsEntry("catalog_doc_id", "1.22.1").containsEntry("chunk_index", 0);
        }
        assertThat(manifest(t, "1.22.1")).isEmpty();
        assertThat(audit(t, "quarantine_restore")).isEmpty();

        QuarantineRestoreOutcome control = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(hs.get(2)), ACTOR, false);
        assertThat(verdictOf(control, hs.get(2))).as("the control: a position with one chunk attaches").isEqualTo("attach");
        assertThat(control.restored()).containsExactly(hs.get(2));
        assertThat(audit(t, "quarantine_restore")).singleElement().satisfies(a ->
            assertThat(a.chashes()).as("the audit row names what moved, not the kept versions")
                .contains(hs.get(2)).doesNotContain(hs.get(0)).doesNotContain(hs.get(1)));
    }

    @Test
    void aRivalIsKeptInQuarantineWhetherOrNotReattachIsOn_andTheDryRunSaysSo() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("r1", Map.of("catalog_doc_id", "1.22.2", "chunk_index", 0));
        meta.put("r2", Map.of("catalog_doc_id", "1.22.2", "chunk_index", 0));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.22.2", "Doc", 1, "d.md", Map.of());

        for (boolean reattach : new boolean[] {true, false}) {
            QuarantineRestoreOutcome dry = vectors.quarantineRestore(t, c, quarantineOf(c), hs, ACTOR, true, reattach);
            QuarantineRestoreOutcome real = vectors.quarantineRestore(t, c, quarantineOf(c), hs, ACTOR, false, reattach);
            for (String h : hs) {
                assertThat(dry.rows().stream().filter(r -> r.chash().equals(h)).findFirst().orElseThrow().outcome())
                    .as("dry run, reattach=" + reattach).isEqualTo("kept");
                assertThat(real.rows().stream().filter(r -> r.chash().equals(h)).findFirst().orElseThrow().outcome())
                    .as("real run, reattach=" + reattach).isEqualTo("kept");
                assertThat(reasonOf(real, h)).isEqualTo(reasonOf(dry, h)).isEqualTo("rival");
                assertThat(inCollection(t, quarantineOf(c), h)).isTrue();
            }
            assertThat(dry.wouldRestore()).as("a kept chunk is not one the dry run would restore").isEmpty();
        }
        assertThat(audit(t, "quarantine_restore")).isEmpty();
    }

    @Test
    void twoVersionsOfOnePositionOnDifferentPagesOfOneSelectionAreBothKept() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("pv1", Map.of("catalog_doc_id", "1.23.1", "chunk_index", 0));
        meta.put("pv2", Map.of("catalog_doc_id", "1.23.1", "chunk_index", 0));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.23.1", "Paged", 1, "pg.md", Map.of());
        Instant since = Instant.parse("2026-01-01T00:00:00Z");

        var page1 = vectors.quarantineRestoreSelected(t, c, quarantineOf(c), since, null, null, 1, ACTOR, false);
        assertThat(page1.nextAfter()).as("a full page of one: the other chunk is on the next page").isNotNull();
        var page2 = vectors.quarantineRestoreSelected(t, c, quarantineOf(c), since, null, page1.nextAfter(), 1, ACTOR, false);

        for (var page : List.of(page1, page2)) {
            assertThat(page.rows()).singleElement().satisfies(r -> {
                assertThat(r.outcome()).isEqualTo("kept");
                assertThat(r.reattach()).isEqualTo("superseded");
                assertThat(r.reason()).isEqualTo("rival");
            });
        }
        assertThat(manifest(t, "1.23.1")).as("neither version is attached alone").isEmpty();
        for (String h : hs) {
            assertThat(inCollection(t, quarantineOf(c), h)).isTrue();
            assertThat(inCollection(t, c, h)).isFalse();
        }
    }

    @Test
    void aStoredOriginChunkNamingTheSamePositionIsARivalEvenWhenTheCallDoesNotNameIt() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantinedWith(t, c, Map.of("wrongly-moved", Map.<String, Object>of(
            "catalog_doc_id", "1.23.2", "chunk_index", 0))).get(0);
        liveDoc(t, c, "1.23.2", "Has a sibling", 1, "s.md", Map.of());
        orphan(t, c, "lingering-version", Map.<String, Object>of("catalog_doc_id", "1.23.2", "chunk_index", 0));

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);

        assertThat(verdictOf(out, h)).isEqualTo("superseded");
        assertThat(reasonOf(out, h)).isEqualTo("rival");
        assertThat(out.kept()).containsExactly(h);
        assertThat(inCollection(t, quarantineOf(c), h)).as("the quarantined version stays where it is").isTrue();
        assertThat(manifest(t, "1.23.2")).isEmpty();
    }

    @Test
    void aPartialAttachSaysHowManyOfTheDocumentsChunksNowHaveARow() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("p0", Map.of("catalog_doc_id", "1.24.1", "chunk_index", 0));
        meta.put("p1", Map.of("catalog_doc_id", "1.24.1", "chunk_index", 1));
        meta.put("p2", Map.of("catalog_doc_id", "1.24.1", "chunk_index", 2));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.24.1", "Three parts", 3, "three.md", Map.of());

        var dry = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(hs.get(0), hs.get(1)), ACTOR, true);
        assertThat(dry.rows()).allSatisfy(r -> {
            assertThat(r.ownerRows()).as("before anything is written").isZero();
            assertThat(r.ownerChunks()).isEqualTo(3);
        });

        var two = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(hs.get(0), hs.get(1)), ACTOR, false);
        assertThat(two.attached()).containsExactlyInAnyOrder(hs.get(0), hs.get(1));
        assertThat(two.rows()).allSatisfy(r -> {
            assertThat(r.ownerRows()).as("2 of 3 now have a row").isEqualTo(2);
            assertThat(r.ownerChunks()).isEqualTo(3);
            assertThat(r.owner()).isEqualTo("1.24.1");
        });

        var last = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(hs.get(2)), ACTOR, false);
        assertThat(last.rows().get(0).ownerRows()).isEqualTo(3);
        assertThat(last.rows().get(0).ownerChunks()).isEqualTo(3);
    }

    @Test
    void theOwnersStateIsJudgedAgainUnderTheIndexRunLock_soADocumentDeletedWhileTheRestoreWaitedIsNotAttachedTo()
            throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.26.1";
        String h = quarantinedWith(t, c, Map.of("waits", Map.<String, Object>of(
            "catalog_doc_id", doc, "chunk_index", 0))).get(0);
        liveDoc(t, c, doc, "Deleted mid-call", 1, "w.md", Map.of());

        var pool = java.util.concurrent.Executors.newSingleThreadExecutor();
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock",
                SQLDataType.OTHER, DSL.function("hashtext", SQLDataType.INTEGER,
                    DSL.val("indexrun:" + t + ":" + doc)))).execute();
            // The restore plans (the document is live: attach), then waits for this lock.
            var call = pool.submit(() -> vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false));
            awaitAdvisoryLockWaiter();
            DSL.using(holder, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(doc))).execute();
            holder.commit();

            QuarantineRestoreOutcome out = call.get(15, java.util.concurrent.TimeUnit.SECONDS);
            assertThat(verdictOf(out, h)).as("judged again under the lock: the owner is gone").isEqualTo("no_live_owner");
            assertThat(out.attached()).isEmpty();
        } finally {
            pool.shutdownNow();
        }
        assertThat(manifest(t, doc)).as("no row was written into a deleted document").isEmpty();
        assertThat(inCollection(t, c, h)).as("the bytes still came back").isTrue();
    }

    @Test
    void aWriterHoldingTheDocumentLockAndWantingTheQuarantineRowCannotDeadlockTheRestore_itIsTheTypedBusyWithNothingMoved()
            throws Exception {
        // nexus-l003p: the document locks are now taken BEFORE the move, so the restore waits for the lock holding no
        // row. It used to hold the quarantine row it had deleted while it waited, and a session that held the lock
        // and then wanted that row deadlocked with it (40P01); that class is gone. The other session's delete goes
        // straight through, and the restore trips its 2 s lock bound: the typed retryable busy, nothing moved.
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.27.1";
        String h = quarantinedWith(t, c, Map.of("deadlocks", Map.<String, Object>of(
            "catalog_doc_id", doc, "chunk_index", 0))).get(0);
        liveDoc(t, c, doc, "Deadlock", 1, "d.md", Map.of());

        var pool = java.util.concurrent.Executors.newFixedThreadPool(2);
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSLContext hctx = DSL.using(holder, SQLDialect.POSTGRES);
            hctx.select(DSL.function("pg_advisory_xact_lock", SQLDataType.OTHER,
                DSL.function("hashtext", SQLDataType.INTEGER, DSL.val("indexrun:" + t + ":" + doc)))).execute();
            var call = pool.submit(() -> vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false));
            awaitAdvisoryLockWaiter();
            // The detector aborts whichever waiter's 1 s deadlock timer fires first. Let the restore be the clear
            // first waiter (0.3 s ahead, far outside scheduler jitter) before the other session starts waiting.
            Thread.sleep(300);
            var rowWanted = pool.submit(() -> hctx.deleteFrom(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(quarantineOf(c)))
                       .and(CHUNKS.CHASH.eq(Chash.fromHex(h).toBytes()))).execute());

            rowWanted.get(5, java.util.concurrent.TimeUnit.SECONDS);   // the other session's delete is not blocked
            assertThatThrownBy(() -> call.get(15, java.util.concurrent.TimeUnit.SECONDS))
                .hasCauseInstanceOf(PgVectorRepository.QuarantineRestoreBusyException.class)
                .hasStackTraceContaining("lock timeout")
                .satisfies(e -> assertThat(e.getCause().getMessage()).contains("nothing was moved"));
            holder.rollback();
        } finally {
            pool.shutdownNow();
        }
        assertThat(inCollection(t, quarantineOf(c), h)).as("nothing moved: still in quarantine").isTrue();
        assertThat(inCollection(t, c, h)).isFalse();
        assertThat(manifest(t, doc)).isEmpty();
    }

    @Test
    void aPositionTheManifestAlreadyNamesIsPositionTaken_notAnAmbiguity_evenWhenThatChunkCarriesTheKeyToo()
            throws Exception {
        // The current chunk names the same document and position as the stale one, so a rival scan alone would call
        // this ambiguous; but a manifest row already holds the position, which is the stronger, plainer fact.
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.28.1";
        String stale = quarantinedWith(t, c, Map.of("stale", Map.<String, Object>of(
            "catalog_doc_id", doc, "chunk_index", 0))).get(0);
        liveDoc(t, c, doc, "Re-indexed", 1, "r.md", Map.of());
        String current = orphan(t, c, "current-keyed", Map.<String, Object>of("catalog_doc_id", doc, "chunk_index", 0));
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                    CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                .values(t, doc, 0, Chash.fromHex(current).toBytes(), 0, c,
                    PgContainerHelper.collectionModel(ctx, t, c)).execute();
        }

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(stale), ACTOR, false);

        assertThat(verdictOf(out, stale)).isEqualTo("superseded");
        assertThat(reasonOf(out, stale)).as("the document has moved on: not 'compare the two versions'")
            .isEqualTo("position_taken");
        assertThat(manifest(t, doc)).containsExactly(new ManifestRow(0, current, c));
    }

    // ── round 4 (nexus-wbfpw.49): the reverse path's rival, a failed index run, every named document locked ────

    @Test
    void aSingleChunkNoteIsNotAttachedWhenAnotherStoredChunkNamesItAtPositionZero_whereverThatChunkSits()
            throws Exception {
        // Sam's ruling: never attach when another stored chunk, in the origin or in the quarantine sibling, names
        // the same document and position. The reverse path (a legacy note found by its own metadata.doc_id) is
        // judged by it exactly as the forward path is.
        String t = newTenant();
        String c = col("knowledge");
        // One reaper pass moves all five (the fifth is the keyed rival that will sit in the sibling); the keyed
        // rivals in the ORIGIN are stored afterwards, or the same pass would have moved them too.
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("note-lonely", Map.of("title", "lonely"));
        meta.put("note-rival-at-zero", Map.of("title", "at zero"));
        meta.put("note-rival-no-index", Map.of("title", "no index"));
        meta.put("note-rival-in-quarantine", Map.of("title", "in quarantine"));
        meta.put("keyed-in-quarantine", Map.of("catalog_doc_id", "1.29.4", "chunk_index", 0));
        List<String> hs = quarantinedWith(t, c, meta);
        String lonely = hs.get(0), atZero = hs.get(1), noIndex = hs.get(2), inSibling = hs.get(3);
        liveDoc(t, c, "1.29.1", "Lonely note", 1, "", Map.of("doc_id", lonely));
        liveDoc(t, c, "1.29.2", "Note with a keyed rival at 0", 1, "", Map.of("doc_id", atZero));
        liveDoc(t, c, "1.29.3", "Note with a keyed rival, no position", 1, "", Map.of("doc_id", noIndex));
        liveDoc(t, c, "1.29.4", "Note with a rival in quarantine", 1, "", Map.of("doc_id", inSibling));
        orphan(t, c, "keyed-at-zero", Map.<String, Object>of("catalog_doc_id", "1.29.2", "chunk_index", 0));
        orphan(t, c, "keyed-no-index", Map.<String, Object>of("doc_id", "1.29.3"));

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c),
            List.of(lonely, atZero, noIndex, inSibling), ACTOR, false);

        assertThat(verdictOf(out, lonely)).as("the control: a note nothing else names attaches").isEqualTo("attach");
        for (String h : List.of(atZero, noIndex, inSibling)) {
            assertThat(verdictOf(out, h)).isEqualTo("superseded");
            assertThat(reasonOf(out, h)).isEqualTo("rival");
            assertThat(visibleToGet(t, c, h)).isFalse();
            assertThat(out.kept()).contains(h);
            assertThat(inCollection(t, quarantineOf(c), h)).as("kept in quarantine: " + h).isTrue();
            assertThat(inCollection(t, c, h)).as("not restored: " + h).isFalse();
        }
        assertThat(out.attached()).containsExactly(lonely);
        assertThat(manifest(t, "1.29.1")).containsExactly(new ManifestRow(0, lonely, c));
        assertThat(manifest(t, "1.29.2")).isEmpty();
        assertThat(manifest(t, "1.29.3")).isEmpty();
        assertThat(manifest(t, "1.29.4")).isEmpty();
    }

    @Test
    void aDocumentWhoseLastIndexRunFailedIsNotAReattachTarget_butAnUnstampedOneIs() throws Exception {
        // failIndexRun stamps index_state 'failed' and leaves whatever manifest the run had written: partial, of
        // unknown shape. The restore refuses rather than guesses.
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("failed-run", Map.of("catalog_doc_id", "1.30.1", "chunk_index", 1));
        meta.put("control", Map.of("catalog_doc_id", "1.30.2", "chunk_index", 1));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.30.1", "Failed run", 3, "f.md", Map.of());
        setIndexState(t, "1.30.1", "failed");
        liveDoc(t, c, "1.30.2", "Unstamped", 3, "u.md", Map.of());

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), hs, ACTOR, false);

        assertThat(verdictOf(out, hs.get(0))).isEqualTo("superseded");
        assertThat(reasonOf(out, hs.get(0))).isEqualTo("failed");
        assertThat(manifest(t, "1.30.1")).as("a partial manifest is not extended").isEmpty();
        assertThat(visibleToGet(t, c, hs.get(0))).isFalse();
        assertThat(verdictOf(out, hs.get(1))).as("the control: the same shape, unstamped, attaches").isEqualTo("attach");
        assertThat(manifest(t, "1.30.2")).containsExactly(new ManifestRow(1, hs.get(1), c));
    }

    @Test
    void anOwnerMidIndexRunOrWhoseRunFailedKeepsTheChunkInQuarantine_andARerunAfterItEndsAttachesIt() throws Exception {
        // nexus-l003p (decision of linda, review round 1): indexing and failed are states that end, and the remedy the
        // operator is given ("run the verb again once the run has finished") needs the forward key the move deletes. So
        // the chunk is not moved: it keeps its keys, and a bytes-only restore (reattach off) does not lose the link either.
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("mid-run", Map.of("catalog_doc_id", "1.32.1", "chunk_index", 0));
        meta.put("failed-run", Map.of("catalog_doc_id", "1.32.2", "chunk_index", 0));
        meta.put("complete", Map.of("catalog_doc_id", "1.32.3", "chunk_index", 0));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.32.1", "Mid run", 1, "a.md", Map.of());
        setIndexState(t, "1.32.1", "indexing");
        liveDoc(t, c, "1.32.2", "Failed run", 1, "b.md", Map.of());
        setIndexState(t, "1.32.2", "failed");
        liveDoc(t, c, "1.32.3", "Complete", 1, "c.md", Map.of());
        setIndexState(t, "1.32.3", "complete");

        for (boolean reattach : new boolean[] {false, true}) {
            QuarantineRestoreOutcome dry = vectors.quarantineRestore(t, c, quarantineOf(c), hs.subList(0, 2), ACTOR, true, reattach);
            QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, quarantineOf(c), hs.subList(0, 2), ACTOR, false, reattach);
            for (int i = 0; i < 2; i++) {
                String h = hs.get(i);
                assertThat(out.kept()).as("reattach=" + reattach).contains(h);
                assertThat(dry.kept()).as("the dry run predicts it, reattach=" + reattach).contains(h);
                assertThat(reasonOf(out, h)).isEqualTo(i == 0 ? "indexing" : "failed");
                assertThat(inCollection(t, c, h)).as("not moved").isFalse();
                assertThat(metadataOf(chunk(t, quarantineOf(c), h))).as("keys kept")
                    .containsEntry("catalog_doc_id", i == 0 ? "1.32.1" : "1.32.2");
            }
        }
        QuarantineRestoreOutcome complete = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(hs.get(2)), ACTOR, false);
        assertThat(complete.restored()).as("a verdict on the document's current text is not a state that ends: bytes only")
            .containsExactly(hs.get(2));
        assertThat(reasonOf(complete, hs.get(2))).isEqualTo("complete");

        // The runs end without a stamp (a legacy document): the same command, run again, attaches both.
        setIndexState(t, "1.32.1", null);
        setIndexState(t, "1.32.2", null);
        QuarantineRestoreOutcome rerun = vectors.quarantineRestore(t, c, quarantineOf(c), hs.subList(0, 2), ACTOR, false);
        assertThat(rerun.restored()).containsExactlyInAnyOrder(hs.get(0), hs.get(1));
        assertThat(rerun.attached()).containsExactlyInAnyOrder(hs.get(0), hs.get(1));
        assertThat(manifest(t, "1.32.1")).containsExactly(new ManifestRow(0, hs.get(0), c));
        assertThat(manifest(t, "1.32.2")).containsExactly(new ManifestRow(0, hs.get(1), c));
        assertThat(visibleToGet(t, c, hs.get(0))).isTrue();
    }

    @Test
    void everyDocumentThePlanNamesIsLockedWhateverItsVerdict_soOneThatBecomesAttachableWhileTheRestoreWaitsIsJudgedUnderTheLock()
            throws Exception {
        // The first plan reads 'indexing' (not an attach), so a lock set taken from attach verdicts alone would be
        // empty and the second plan would run at once, still reading 'indexing'. With every named document locked
        // the restore waits for the holder, which clears the run state and commits; the second plan then attaches.
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.31.1";
        String h = quarantinedWith(t, c, Map.of("was-mid-run", Map.<String, Object>of(
            "catalog_doc_id", doc, "chunk_index", 0))).get(0);
        liveDoc(t, c, doc, "Run finished meanwhile", 1, "m.md", Map.of());
        setIndexState(t, doc, "indexing");

        var pool = java.util.concurrent.Executors.newSingleThreadExecutor();
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSLContext hctx = DSL.using(holder, SQLDialect.POSTGRES);
            hctx.select(DSL.function("pg_advisory_xact_lock", SQLDataType.OTHER,
                DSL.function("hashtext", SQLDataType.INTEGER, DSL.val("indexrun:" + t + ":" + doc)))).execute();
            var call = pool.submit(() -> vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false));
            awaitAdvisoryLockWaiter();
            hctx.update(CATALOG_DOCUMENTS).setNull(CATALOG_DOCUMENTS.INDEX_STATE)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(t).and(CATALOG_DOCUMENTS.TUMBLER.eq(doc))).execute();
            holder.commit();

            QuarantineRestoreOutcome out = call.get(15, java.util.concurrent.TimeUnit.SECONDS);
            assertThat(verdictOf(out, h)).as("judged again under the lock: the run is over").isEqualTo("attach");
            assertThat(out.attached()).containsExactly(h);
        } finally {
            pool.shutdownNow();
        }
        assertThat(manifest(t, doc)).containsExactly(new ManifestRow(0, h, c));
        assertThat(visibleToGet(t, c, h)).isTrue();
    }

    // ── the engine names the sibling itself (nexus-wbfpw.55, RDR-192 Phase 3 gate I-1) ──────────────────────

    /** catalog-044 rewrote {@code owner_id} on repo collections after the fact: the row now disagrees with the name. */
    private void setOwner(String tenant, String collection, String owner) throws Exception {
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.OWNER_ID, owner)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(collection))).execute();
            assertThat(n).as("fixture: the origin has a catalog row to rewrite").isEqualTo(1);
        }
    }

    /** What the Python client derived for the origin: {@code quarantine-<content_type>__<row owner>__<model>__<v>}. */
    private static String rowDerivedSibling(String collection, String owner) {
        String[] seg = collection.split("__");
        return "quarantine-" + seg[0] + "__" + owner + "__" + seg[2] + "__" + seg[3];
    }

    @Test
    void aReaperMovedChunkIsRestoredWhenTheOriginsCatalogRowDisagreesWithItsName() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "catalog-044").get(0);
        setOwner(t, c, "curator-9");
        String rowSibling = rowDerivedSibling(c, "curator-9");
        assertThat(rowSibling).as("fixture: the row-derived sibling is not where the reaper put it")
            .isNotEqualTo(quarantineOf(c));

        // The engine finds the sibling from the origin's name and the chunks' own origin tag, so a caller that
        // does not name one (or names the row-derived one) reaches the chunk.
        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, null, List.of(h), ACTOR, false);

        assertThat(out.restored()).containsExactly(h);
        assertThat(inCollection(t, c, h)).as("back in the origin").isTrue();
        assertThat(inCollection(t, quarantineOf(c), h)).as("and gone from where the reaper put it").isFalse();
    }

    @Test
    void aSlugCollectionAndItsConformantTwinRestoreOnlyTheirOwnChunks() throws Exception {
        String t = newTenant();
        String slug = col("knowledge");
        String twin = col("knowledge");
        // The reaper names each sibling by the collection's own name, so each chunk sits in ITS OWN sibling here;
        // this test does not put two origins' chunks in one sibling (aSharedSiblingIsFoundByTheOriginTagNotByItsName
        // does). What it pins: after catalog-044 both rows read the same owner, so a client deriving the sibling
        // from the row would name ONE sibling for the two and reach neither chunk; the engine reaches only the
        // chunks of the origin it was asked about and never restores the twin's into the slug.
        String hSlug = quarantined(t, slug, "slug").get(0);
        String hTwin = quarantined(t, twin, "twin").get(0);
        setOwner(t, slug, "curator-9");
        setOwner(t, twin, "curator-9");

        QuarantineRestoreOutcome dry = vectors.quarantineRestore(t, slug, null, List.of(hSlug, hTwin), ACTOR, true);
        assertThat(dry.wouldRestore()).containsExactly(hSlug);
        assertThat(dry.missing()).as("the twin's chunk is not the slug's to take").containsExactly(hTwin);

        assertThat(vectors.quarantineRestore(t, slug, null, List.of(hSlug, hTwin), ACTOR, false).restored())
            .containsExactly(hSlug);
        assertThat(inCollection(t, twin, hTwin)).as("never restored into the other origin").isFalse();
        assertThat(inCollection(t, quarantineOf(twin), hTwin)).as("still in the twin's own sibling").isTrue();
        assertThat(vectors.quarantineRestore(t, twin, null, List.of(hTwin), ACTOR, false).restored())
            .containsExactly(hTwin);
    }

    @Test
    void aSharedSiblingIsFoundByTheOriginTagNotByItsName() throws Exception {
        String t = newTenant();
        String a = col("knowledge");
        String b = col("knowledge");
        String shared = "quarantine-knowledge__curator-9__minilm-l6-v2-384__v1";   // what the client's row rule gave both
        String forA = Chash.ofText("shared-for-a").toHex();
        String forB = Chash.ofText("shared-for-b").toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, t, a);
            PgContainerHelper.insertCollection(ctx, t, b);
            PgContainerHelper.insertCollection(ctx, t, shared);
            PgContainerHelper.insertChunks(ctx, t, shared, List.of(forA), List.of("a text"),
                embedder.embed(List.of("a text")), List.of(Map.<String, Object>of("origin_collection", a)));
            PgContainerHelper.insertChunks(ctx, t, shared, List.of(forB), List.of("b text"),
                embedder.embed(List.of("b text")), List.of(Map.<String, Object>of("origin_collection", b)));
        }

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, a, null, List.of(forA, forB), ACTOR, false);

        assertThat(out.restored()).containsExactly(forA);
        assertThat(out.missing()).containsExactly(forB);
        assertThat(inCollection(t, shared, forB)).as("B's chunk stays where it is").isTrue();
    }

    @Test
    void anOriginWithNoQuarantineSiblingAtAllReadsEveryChashMissing_notAnError() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = orphan(t, c, "never-moved");

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, null, List.of(h), ACTOR, false);

        assertThat(out.missing()).containsExactly(h);
        assertThat(inCollection(t, c, h)).as("untouched").isTrue();
    }

    @Test
    void aWindowAndAnAuditIdAreResolvedByTheEngineToo() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "win-a", "win-b");
        long id = audit(t, "reaper_quarantine").get(0).id();
        setOwner(t, c, "curator-9");

        QuarantineRestoreOutcome fromAudit = vectors.quarantineRestoreFromAudit(t, c, null, id, 0, 1, ACTOR, false);
        assertThat(fromAudit.restored()).hasSize(1);
        assertThat(fromAudit.source().nextOffset()).isEqualTo(1);

        QuarantineRestoreOutcome fromWindow = vectors.quarantineRestoreSelected(
            t, c, null, Instant.parse("2000-01-01T00:00:00Z"), null, null, 1000, ACTOR, false);
        assertThat(fromWindow.restored()).hasSize(1);
        assertThat(fromAudit.restored().get(0)).isNotEqualTo(fromWindow.restored().get(0));
        assertThat(hs).allSatisfy(h -> assertThat(inCollection(t, c, h)).isTrue());
    }

    @Test
    void chunksInTheReapersSiblingAndInAClientNamedSiblingAreBothReached_oneAuditRowEach() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String byReaper = quarantined(t, c, "reaper-side").get(0);
        // nx index repo moved a chunk of the same origin before catalog-044, into the name its row gave then.
        String clientSibling = rowDerivedSibling(c, "old-owner");
        String byClient = Chash.ofText("client-side").toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, t, clientSibling);
            PgContainerHelper.insertChunks(ctx, t, clientSibling, List.of(byClient), List.of("client text"),
                embedder.embed(List.of("client text")), List.of(Map.<String, Object>of(
                    "origin_collection", c, "quarantined_at", "2026-09-01T00:00:00Z")));
        }
        String nowhere = Chash.ofText("nowhere").toHex();
        setOwner(t, c, "curator-9");

        assertThat(vectors.resolveQuarantineSiblings(t, c)).as("the reaper's name first, then the tagged one")
            .containsExactly(quarantineOf(c), clientSibling);
        QuarantineRestoreOutcome dry =
            vectors.quarantineRestore(t, c, null, List.of(byClient, nowhere, byReaper), ACTOR, true);
        assertThat(dry.wouldRestore()).containsExactlyInAnyOrder(byClient, byReaper);
        assertThat(dry.missing()).containsExactly(nowhere);
        assertThat(dry.rows()).extracting(QuarantineRestoreOutcome.Row::chash)
            .as("one row per chash, in request order").containsExactly(byClient, nowhere, byReaper);
        assertThat(dry.auditIds()).as("a dry run writes no audit row").isEmpty();

        QuarantineRestoreOutcome out =
            vectors.quarantineRestore(t, c, null, List.of(byClient, nowhere, byReaper), ACTOR, false);

        assertThat(out.restored()).containsExactlyInAnyOrder(byClient, byReaper);
        assertThat(out.missing()).containsExactly(nowhere);
        assertThat(out.quarantineCollections()).containsExactly(quarantineOf(c), clientSibling);
        assertThat(out.auditIds()).as("one quarantine_restore row per sibling that moved something").hasSize(2);
        assertThat(out.auditId()).isEqualTo(out.auditIds().get(0));
        assertThat(audit(t, "quarantine_restore")).extracting(AuditRow::collection).containsExactly(c, c);
        assertThat(inCollection(t, c, byClient)).isTrue();
        assertThat(inCollection(t, clientSibling, byClient)).isFalse();
        assertThat(inCollection(t, c, byReaper)).isTrue();
    }

    @Test
    void aWindowSelectsAChashOnceEvenWhenTwoSiblingsHoldIt() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantined(t, c, "both").get(0);
        String clientSibling = rowDerivedSibling(c, "old-owner");
        ChunkState reaperCopy = chunk(t, quarantineOf(c), h);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, t, clientSibling);
            PgContainerHelper.insertChunks(ctx, t, clientSibling, List.of(h), List.of(reaperCopy.text()),
                embedder.embed(List.of(reaperCopy.text())), List.of(Map.<String, Object>of(
                    "origin_collection", c, "quarantined_at", "2026-09-01T00:00:00Z")));
        }

        QuarantineRestoreOutcome out = vectors.quarantineRestoreSelected(
            t, c, null, Instant.parse("2000-01-01T00:00:00Z"), null, null, 1000, ACTOR, false);

        assertThat(out.rows()).as("one row for the chash, not one per sibling").hasSize(1);
        assertThat(out.restored()).containsExactly(h);
        assertThat(inCollection(t, clientSibling, h)).as("the second copy is left for expiry, as a present chash's is")
            .isTrue();
    }

    /** A chunk a client move left in {@code sibling}, tagged for {@code origin}, as the real tag shape carries it. */
    private String clientSiblingChunk(String tenant, String origin, String sibling, String seed,
                                      Map<String, Object> extraMeta) throws Exception {
        String hex = Chash.ofText(origin + "/client/" + seed).toHex();
        var meta = new java.util.LinkedHashMap<String, Object>(extraMeta);
        meta.put("title", seed);
        meta.put("origin_collection", origin);
        meta.put("quarantined_at", "2026-09-01T00:00:00Z");
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, sibling);
            PgContainerHelper.insertChunks(ctx, tenant, sibling, List.of(hex), List.of(seed + " text"),
                embedder.embed(List.of(seed + " text")), List.of(meta));
        }
        return hex;
    }

    // ── a busy trip after an earlier sibling committed (nexus-wbfpw.55 round 2) ───────────────────────────────

    @Test
    void aBusyTripOnALaterSiblingSaysWhatTheEarlierSiblingAlreadyCommitted_notNothingMoved() throws Exception {
        // Each sibling's restore is its own transaction. Sibling one commits (bytes moved, a manifest row, an audit
        // row); sibling two then trips the index-run lock of its chunk's document. The old answer, "nothing was
        // moved, attached or audited", was true of that one statement and false of the call.
        String t = newTenant();
        String c = col("knowledge");
        String first = quarantinedWith(t, c, Map.of("first-sibling", Map.<String, Object>of(
            "catalog_doc_id", "1.40.1", "chunk_index", 0))).get(0);
        String clientSibling = rowDerivedSibling(c, "old-owner");
        String second = clientSiblingChunk(t, c, clientSibling, "second-sibling",
            Map.of("catalog_doc_id", "1.40.2", "chunk_index", 0));
        liveDoc(t, c, "1.40.1", "Free document", 1, "f.md", Map.of());
        liveDoc(t, c, "1.40.2", "Locked document", 1, "l.md", Map.of());
        setOwner(t, c, "curator-9");
        assertThat(vectors.resolveQuarantineSiblings(t, c)).containsExactly(quarantineOf(c), clientSibling);

        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock", SQLDataType.OTHER,
                DSL.function("hashtext", SQLDataType.INTEGER, DSL.val("indexrun:" + t + ":1.40.2")))).execute();

            assertThatThrownBy(() -> vectors.quarantineRestore(t, c, null, List.of(first, second), ACTOR, false))
                .isInstanceOfSatisfying(PgVectorRepository.QuarantineRestoreBusyException.class, busy -> {
                    assertThat(busy.somethingMoved()).as("the first sibling had committed").isTrue();
                    assertThat(busy.auditIds()).as("and wrote its audit row").hasSize(1);
                    assertThat(busy.getMessage()).doesNotContain("nothing was moved");
                    assertThat(busy.getMessage()).contains("already");
                });
            holder.rollback();
        }

        assertThat(inCollection(t, c, first)).as("the first sibling's chunk is home").isTrue();
        assertThat(manifest(t, "1.40.1")).as("and attached").hasSize(1);
        assertThat(audit(t, "quarantine_restore")).as("its audit row exists").hasSize(1);
        assertThat(inCollection(t, clientSibling, second)).as("the tripped sibling's chunk did not move").isTrue();
        // Sent again, the call finishes: the done chash reads present, the other is restored.
        QuarantineRestoreOutcome again = vectors.quarantineRestore(t, c, null, List.of(first, second), ACTOR, false);
        assertThat(again.present()).containsExactly(first);
        assertThat(again.restored()).containsExactly(second);
    }

    @Test
    void aBusyTripOnTheFirstSiblingStillSaysNothingMoved() throws Exception {
        // The control: nothing had committed, so "nothing moved" is true and somethingMoved() is false.
        String t = newTenant();
        String c = col("knowledge");
        String h = quarantinedWith(t, c, Map.of("only", Map.<String, Object>of(
            "catalog_doc_id", "1.41.1", "chunk_index", 0))).get(0);
        liveDoc(t, c, "1.41.1", "Locked", 1, "l.md", Map.of());
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock", SQLDataType.OTHER,
                DSL.function("hashtext", SQLDataType.INTEGER, DSL.val("indexrun:" + t + ":1.41.1")))).execute();
            assertThatThrownBy(() -> vectors.quarantineRestore(t, c, null, List.of(h), ACTOR, false))
                .isInstanceOfSatisfying(PgVectorRepository.QuarantineRestoreBusyException.class, busy -> {
                    assertThat(busy.somethingMoved()).isFalse();
                    assertThat(busy.auditIds()).isEmpty();
                    assertThat(busy.getMessage()).contains("nothing was moved");
                });
            holder.rollback();
        }
    }

    // ── the rival check spans every sibling of the origin (nexus-wbfpw.55 round 2) ───────────────────────────

    /** Two versions of one position of one document, one in the reaper's sibling and one in a client-named one. */
    private record Rivals(String t, String c, String clientSibling, String inReaperSibling, String inClientSibling,
                          String control) {}

    private Rivals rivalsAcrossSiblings() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String x = quarantinedWith(t, c, Map.of("version-in-reaper-sibling", Map.<String, Object>of(
            "catalog_doc_id", "1.42.1", "chunk_index", 0, "title", "x"))).get(0);
        String clientSibling = rowDerivedSibling(c, "old-owner");
        String y = clientSiblingChunk(t, c, clientSibling, "version-in-client-sibling",
            Map.of("catalog_doc_id", "1.42.1", "chunk_index", 0));
        String z = clientSiblingChunk(t, c, clientSibling, "position-one-alone",
            Map.of("catalog_doc_id", "1.42.1", "chunk_index", 1));
        liveDoc(t, c, "1.42.1", "Two versions of position zero", 2, "p.md", Map.of());
        setOwner(t, c, "curator-9");
        assertThat(vectors.resolveQuarantineSiblings(t, c)).containsExactly(quarantineOf(c), clientSibling);
        return new Rivals(t, c, clientSibling, x, y, z);
    }

    @Test
    void twoVersionsOfOnePositionInDifferentSiblingsAreBothKept_inOneCall_andTheDryRunEqualsTheRealRun()
            throws Exception {
        // nexus-l003p: a rival is not moved, so each sibling's plan still sees the other version in quarantine and no
        // cross-sibling pre-pass is needed; the dry run (which moves nothing) and the real run agree row for row.
        Rivals r = rivalsAcrossSiblings();
        List<String> all = List.of(r.inReaperSibling(), r.inClientSibling(), r.control());

        QuarantineRestoreOutcome dry = vectors.quarantineRestore(r.t(), r.c(), null, all, ACTOR, true);
        assertThat(reasonOf(dry, r.inReaperSibling())).isEqualTo("rival");
        assertThat(reasonOf(dry, r.inClientSibling())).isEqualTo("rival");
        assertThat(verdictOf(dry, r.control())).isEqualTo("attach");

        QuarantineRestoreOutcome out = vectors.quarantineRestore(r.t(), r.c(), null, all, ACTOR, false);

        assertThat(verdictOf(out, r.inReaperSibling())).isEqualTo("superseded");
        assertThat(reasonOf(out, r.inReaperSibling())).isEqualTo("rival");
        assertThat(verdictOf(out, r.inClientSibling())).isEqualTo("superseded");
        assertThat(reasonOf(out, r.inClientSibling())).isEqualTo("rival");
        assertThat(verdictOf(out, r.control())).as("a position with one chunk attaches").isEqualTo("attach");
        assertThat(out.kept()).containsExactlyInAnyOrder(r.inReaperSibling(), r.inClientSibling());
        assertThat(manifest(r.t(), "1.42.1")).extracting(ManifestRow::position).containsExactly(1);
        // The dry run is the real run's prediction: the same outcome, verdict and reason for every chash.
        for (String h : all) {
            var dryRow = dry.rows().stream().filter(x -> x.chash().equals(h)).findFirst().orElseThrow();
            var realRow = out.rows().stream().filter(x -> x.chash().equals(h)).findFirst().orElseThrow();
            assertThat(realRow.outcome()).as("dry run == real run, outcome of " + h)
                .isEqualTo(dryRow.outcome().replace("would_restore", "restored"));
            assertThat(verdictOf(out, h)).as("dry run == real run, verdict of " + h).isEqualTo(verdictOf(dry, h));
            assertThat(reasonOf(out, h)).as("dry run == real run, reason of " + h).isEqualTo(reasonOf(dry, h));
        }
        assertThat(inCollection(r.t(), r.c(), r.inReaperSibling())).as("neither version was restored").isFalse();
        assertThat(inCollection(r.t(), r.c(), r.inClientSibling())).isFalse();
        assertThat(inCollection(r.t(), quarantineOf(r.c()), r.inReaperSibling())).as("each stays in its sibling").isTrue();
        assertThat(inCollection(r.t(), r.clientSibling(), r.inClientSibling())).isTrue();
    }

    @Test
    void theFunctionKeepsItsEightArgumentSignature_noRivalsPrePassParameter() throws Exception {
        // vectors-053 replaces the vectors-030-1 function in place. A ninth p_rivals parameter (the cross-sibling
        // pre-pass this round removed) would leave a second overload beside it.
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc"));
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
        var nspname = DSL.field(DSL.name("pg_catalog", "pg_namespace", "nspname"), String.class);
        var proname = DSL.field(DSL.name("pg_catalog", "pg_proc", "proname"), String.class);
        var defaults = DSL.field(DSL.name("pg_catalog", "pg_proc", "pronargdefaults"), Integer.class);
        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES).select(defaults).from(proc).join(ns)
                .on(DSL.field(DSL.name("pg_catalog", "pg_proc", "pronamespace"), Object.class)
                    .eq(DSL.field(DSL.name("pg_catalog", "pg_namespace", "oid"), Object.class)))
                .where(nspname.eq("nexus").and(proname.eq("quarantine_restore_chunks"))).fetch();
            assertThat(rows).as("exactly one overload").hasSize(1);
            assertThat(rows.get(0).get(defaults)).as("and it takes no defaulted parameter, p_rivals' shape").isZero();
        }
    }

    @Test
    void twoVersionsOfOnePositionInDifferentSiblings_inSeparateCalls_areBothKeptWhicheverIsRestoredFirst()
            throws Exception {
        for (boolean reaperFirst : new boolean[] {true, false}) {
            Rivals r = rivalsAcrossSiblings();
            String first = reaperFirst ? r.inReaperSibling() : r.inClientSibling();
            String second = reaperFirst ? r.inClientSibling() : r.inReaperSibling();

            QuarantineRestoreOutcome one = vectors.quarantineRestore(r.t(), r.c(), null, List.of(first), ACTOR, false);
            QuarantineRestoreOutcome two = vectors.quarantineRestore(r.t(), r.c(), null, List.of(second), ACTOR, false);

            assertThat(reasonOf(one, first)).as("the other version sits in the other sibling").isEqualTo("rival");
            assertThat(reasonOf(two, second)).as("and the first stayed in quarantine, so it is still a rival")
                .isEqualTo("rival");
            assertThat(one.kept()).containsExactly(first);
            assertThat(two.kept()).containsExactly(second);
            assertThat(manifest(r.t(), "1.42.1")).as("neither version attached alone").isEmpty();
            assertThat(inCollection(r.t(), r.c(), first)).isFalse();
            assertThat(inCollection(r.t(), r.c(), second)).isFalse();
        }
    }

    @Test
    void aBusyRetryAfterAnEarlierSiblingCommitted_keepsTheRivalPairKept() throws Exception {
        // nexus-l003p: sibling one commits (its free chunk attaches; its rival version is kept, not stripped), sibling two
        // then trips the index-run lock of its free chunk's document. The retry finishes the call: the rival pair is
        // kept again, so the busy trip cannot turn the refusal into "the second version attaches alone".
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("rival-in-reaper-sibling", Map.of("catalog_doc_id", "1.45.1", "chunk_index", 0));
        meta.put("free-in-reaper-sibling", Map.of("catalog_doc_id", "1.45.2", "chunk_index", 0));
        List<String> reaper = quarantinedWith(t, c, meta);
        String clientSibling = rowDerivedSibling(c, "old-owner");
        String rivalB = clientSiblingChunk(t, c, clientSibling, "rival-in-client-sibling",
            Map.of("catalog_doc_id", "1.45.1", "chunk_index", 0));
        String lockedFree = clientSiblingChunk(t, c, clientSibling, "free-in-client-sibling",
            Map.of("catalog_doc_id", "1.45.3", "chunk_index", 0));
        liveDoc(t, c, "1.45.1", "Contested", 1, "c.md", Map.of());
        liveDoc(t, c, "1.45.2", "Free", 1, "f.md", Map.of());
        liveDoc(t, c, "1.45.3", "Locked", 1, "l.md", Map.of());
        setOwner(t, c, "curator-9");
        String rivalA = reaper.get(0), freeA = reaper.get(1);
        List<String> all = List.of(rivalA, freeA, rivalB, lockedFree);

        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock", SQLDataType.OTHER,
                DSL.function("hashtext", SQLDataType.INTEGER, DSL.val("indexrun:" + t + ":1.45.3")))).execute();
            assertThatThrownBy(() -> vectors.quarantineRestore(t, c, null, all, ACTOR, false))
                .isInstanceOfSatisfying(PgVectorRepository.QuarantineRestoreBusyException.class, busy ->
                    assertThat(busy.somethingMoved()).as("sibling one had committed").isTrue());
            holder.rollback();
        }
        assertThat(inCollection(t, quarantineOf(c), rivalA)).as("sibling one kept its rival, keys and all").isTrue();
        assertThat(inCollection(t, c, freeA)).as("and restored the free chunk").isTrue();

        QuarantineRestoreOutcome again = vectors.quarantineRestore(t, c, null, all, ACTOR, false);

        assertThat(again.kept()).as("the pair is refused again on the retry").containsExactlyInAnyOrder(rivalA, rivalB);
        assertThat(reasonOf(again, rivalA)).isEqualTo("rival");
        assertThat(reasonOf(again, rivalB)).isEqualTo("rival");
        assertThat(again.present()).containsExactly(freeA);
        assertThat(again.restored()).containsExactly(lockedFree);
        assertThat(manifest(t, "1.45.1")).as("neither contested version attached alone").isEmpty();
        assertThat(inCollection(t, quarantineOf(c), rivalA)).isTrue();
        assertThat(inCollection(t, clientSibling, rivalB)).isTrue();
    }

    @Test
    void aQuarantinedChunkOfAnotherOriginDoesNotMakeARival() throws Exception {
        // The control for the wider scan: a chunk in a sibling that names ANOTHER origin is not this origin's
        // version of the document, so it must not refuse an attach.
        String t = newTenant();
        String c = col("knowledge");
        String other = col("knowledge");
        String mine = quarantinedWith(t, c, Map.of("mine", Map.<String, Object>of(
            "catalog_doc_id", "1.43.1", "chunk_index", 0))).get(0);
        String clientSibling = rowDerivedSibling(c, "shared");
        clientSiblingChunk(t, other, clientSibling, "theirs", Map.of("catalog_doc_id", "1.43.1", "chunk_index", 0));
        liveDoc(t, c, "1.43.1", "Mine", 1, "m.md", Map.of());

        QuarantineRestoreOutcome out = vectors.quarantineRestore(t, c, null, List.of(mine), ACTOR, false);

        assertThat(verdictOf(out, mine)).isEqualTo("attach");
    }

    // ── the keyed form: one transaction, one marker (nexus-k9kr9, RDR-222) ──────────────────────────────────────

    /** Counts marker calls and keeps what the last one saw; {@code inside} runs in the marker, in the call's transaction. */
    private static final class Mark {
        final AtomicInteger calls = new AtomicInteger();
        final AtomicReference<QuarantineRestoreOutcome> seen = new AtomicReference<>();
        final java.util.function.Consumer<DSLContext> inside;

        Mark() { this(ctx -> { }); }

        Mark(java.util.function.Consumer<DSLContext> inside) { this.inside = inside; }

        PgVectorRepository.LedgerMarker<QuarantineRestoreOutcome> marker() {
            return (ctx, outcome) -> {
                calls.incrementAndGet();
                seen.set(outcome);
                inside.accept(ctx);
            };
        }
    }

    private static PgVectorRepository.KeyedRestore keyed(Mark mark) {
        return PgVectorRepository.KeyedRestore.begin(mark.marker(), 25_000);
    }

    /** Two siblings, one chunk in each: {@code first} in the reaper's, {@code second} in a client-named one. */
    private record TwoSiblings(String t, String c, String clientSibling, String first, String second) {}

    private TwoSiblings twoSiblings(int docBase) throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String first = quarantinedWith(t, c, Map.of("first-sibling", Map.<String, Object>of(
            "catalog_doc_id", "1." + docBase + ".1", "chunk_index", 0))).get(0);
        String clientSibling = rowDerivedSibling(c, "old-owner");
        String second = clientSiblingChunk(t, c, clientSibling, "second-sibling",
            Map.of("catalog_doc_id", "1." + docBase + ".2", "chunk_index", 0));
        liveDoc(t, c, "1." + docBase + ".1", "Free document", 1, "f.md", Map.of());
        liveDoc(t, c, "1." + docBase + ".2", "Locked document", 1, "l.md", Map.of());
        setOwner(t, c, "curator-9");
        assertThat(vectors.resolveQuarantineSiblings(t, c)).containsExactly(quarantineOf(c), clientSibling);
        return new TwoSiblings(t, c, clientSibling, first, second);
    }

    @Test
    void aKeyedRestoreRunsEverySiblingInOneTransaction_theMarkerRunsOnceInsideIt() throws Exception {
        var f = twoSiblings(50);
        // What a second connection sees while the marker runs: the call has not committed, so neither sibling's move
        // is visible to it. Per-sibling transactions would show the first one's chunk in the origin by then.
        var seenByOthers = new AtomicReference<Boolean>();
        var seenInside = new AtomicReference<Integer>();
        Mark mark = new Mark(ctx -> {
            try {
                seenByOthers.set(inCollection(f.t(), f.c(), f.first()) || inCollection(f.t(), f.c(), f.second()));
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            seenInside.set(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(f.t()).and(CHUNKS.COLLECTION.eq(f.c()))));
        });

        QuarantineRestoreOutcome out = vectors.quarantineRestoreKeyed(f.t(), f.c(), List.of(f.first(), f.second()),
            ACTOR, true, keyed(mark));

        assertThat(mark.calls).as("one marker call for the whole call").hasValue(1);
        assertThat(mark.seen.get()).as("it saw the final outcome").isSameAs(out);
        assertThat(seenInside.get()).as("inside the transaction both moves are there").isEqualTo(2);
        assertThat(seenByOthers.get()).as("and none of it is committed yet").isFalse();
        assertThat(out.restored()).containsExactlyInAnyOrder(f.first(), f.second());
        assertThat(out.auditIds()).as("one audit row per sibling, as unkeyed").hasSize(2);
        assertThat(out.quarantineCollections()).containsExactly(quarantineOf(f.c()), f.clientSibling());
        assertThat(inCollection(f.t(), f.c(), f.first())).isTrue();
        assertThat(inCollection(f.t(), f.c(), f.second())).isTrue();
        assertThat(audit(f.t(), "quarantine_restore")).hasSize(2);
    }

    @Test
    void aBusyTripOnTheSecondSiblingOfAKeyedRestoreRollsTheFirstBack_nothingMoved_noMarker() throws Exception {
        var f = twoSiblings(51);
        Mark mark = new Mark();
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock", SQLDataType.OTHER,
                DSL.function("hashtext", SQLDataType.INTEGER, DSL.val("indexrun:" + f.t() + ":1.51.2")))).execute();

            assertThatThrownBy(() -> vectors.quarantineRestoreKeyed(f.t(), f.c(), List.of(f.first(), f.second()),
                    ACTOR, true, keyed(mark)))
                .isInstanceOfSatisfying(PgVectorRepository.QuarantineRestoreBusyException.class, busy -> {
                    assertThat(busy.somethingMoved()).as("sibling one rolled back with the rest").isFalse();
                    assertThat(busy.auditIds()).isEmpty();
                    assertThat(busy.movedChashes()).isEmpty();
                    assertThat(busy.getMessage()).contains("nothing was moved");
                });
            holder.rollback();
        }

        assertThat(mark.calls).as("no marker: nothing committed").hasValue(0);
        assertThat(inCollection(f.t(), quarantineOf(f.c()), f.first())).as("sibling one's chunk never left").isTrue();
        assertThat(inCollection(f.t(), f.c(), f.first())).isFalse();
        assertThat(manifest(f.t(), "1.51.1")).as("and was not attached").isEmpty();
        assertThat(audit(f.t(), "quarantine_restore")).as("no audit row survived").isEmpty();
        assertThat(inCollection(f.t(), f.clientSibling(), f.second())).isTrue();

        // The same keyed call, sent again with the lock gone, finishes the whole thing.
        Mark again = new Mark();
        QuarantineRestoreOutcome out = vectors.quarantineRestoreKeyed(f.t(), f.c(), List.of(f.first(), f.second()),
            ACTOR, true, keyed(again));
        assertThat(out.restored()).containsExactlyInAnyOrder(f.first(), f.second());
        assertThat(again.calls).hasValue(1);
    }

    @Test
    void aKeyedRestoreWhoseMarkerFailsMovesNothing_theLostLeaseCase() throws Exception {
        var f = twoSiblings(52);
        var lost = new Mark(ctx -> { throw new IllegalStateException("lease lost"); });

        assertThatThrownBy(() -> vectors.quarantineRestoreKeyed(f.t(), f.c(), List.of(f.first(), f.second()), ACTOR,
                true, keyed(lost)))
            .isInstanceOf(IllegalStateException.class).hasMessage("lease lost");

        assertThat(lost.calls).hasValue(1);
        assertThat(inCollection(f.t(), quarantineOf(f.c()), f.first())).as("both siblings rolled back").isTrue();
        assertThat(inCollection(f.t(), f.clientSibling(), f.second())).isTrue();
        assertThat(manifest(f.t(), "1.52.1")).isEmpty();
        assertThat(audit(f.t(), "quarantine_restore")).isEmpty();
    }

    @Test
    void aKeyedRestoreSpendsOneBudgetAcrossItsSiblings_aSpentOneIsBusyWithNothingMoved() throws Exception {
        var f = twoSiblings(53);
        Mark mark = new Mark();
        // 400 ms for the whole call; the hook uses it up between sibling one and sibling two.
        vectors.setKeyedRestoreSiblingHookForTests((ctx, index) -> {
            if (index == 2) {
                try { Thread.sleep(600); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            }
        });
        try {
            assertThatThrownBy(() -> vectors.quarantineRestoreKeyed(f.t(), f.c(), List.of(f.first(), f.second()),
                    ACTOR, true, PgVectorRepository.KeyedRestore.begin(mark.marker(), 400)))
                .isInstanceOfSatisfying(PgVectorRepository.QuarantineRestoreBusyException.class, busy -> {
                    assertThat(busy.getMessage()).contains("time budget");
                    assertThat(busy.somethingMoved()).isFalse();
                    assertThat(busy.auditIds()).isEmpty();
                });
        } finally {
            vectors.setKeyedRestoreSiblingHookForTests(null);
        }

        assertThat(mark.calls).hasValue(0);
        assertThat(inCollection(f.t(), quarantineOf(f.c()), f.first())).as("sibling one rolled back").isTrue();
        assertThat(audit(f.t(), "quarantine_restore")).isEmpty();
    }

    @Test
    void eachSiblingOfAKeyedRestoreRunsUnderWhatIsLeftOfOneStatementBudget_lockTimeoutStaysTwoSeconds()
            throws Exception {
        var f = twoSiblings(54);
        var afterSiblingOne = new AtomicReference<Integer>();
        var inMarker = new AtomicReference<Integer>();
        var lockInMarker = new AtomicReference<Integer>();
        Mark mark = new Mark(ctx -> {
            inMarker.set(settingMs(ctx, "statement_timeout"));
            lockInMarker.set(settingMs(ctx, "lock_timeout"));
        });
        vectors.setKeyedRestoreSiblingHookForTests((ctx, index) -> {
            if (index == 2) {
                afterSiblingOne.set(settingMs(ctx, "statement_timeout"));   // what sibling one ran under
                try { Thread.sleep(500); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            }
        });
        try {
            vectors.quarantineRestoreKeyed(f.t(), f.c(), List.of(f.first(), f.second()), ACTOR, true,
                PgVectorRepository.KeyedRestore.begin(mark.marker(), 20_000));
        } finally {
            vectors.setKeyedRestoreSiblingHookForTests(null);
        }

        assertThat(afterSiblingOne.get()).as("sibling one ran under about the whole budget")
            .isBetween(15_000, 20_000);
        assertThat(inMarker.get()).as("sibling two ran under the budget less the 500 ms the call had already spent")
            .isLessThanOrEqualTo(afterSiblingOne.get() - 450).isGreaterThan(10_000);
        assertThat(lockInMarker.get()).as("the row-lock bound stays 2 s").isEqualTo(2_000);
    }

    private static int settingMs(DSLContext ctx, String name) {
        var setting = DSL.field(DSL.name("pg_catalog", "pg_settings", "setting"), String.class);
        var settingName = DSL.field(DSL.name("pg_catalog", "pg_settings", "name"), String.class);
        return Integer.parseInt(ctx.select(setting).from(DSL.table(DSL.name("pg_catalog", "pg_settings")))
            .where(settingName.eq(name)).fetchOne(setting));
    }

    @Test
    void aKeyedWindowHandsTheMarkerTheCursorOfItsPage() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "kw-1", "kw-2", "kw-3");
        Mark mark = new Mark();

        var page = vectors.quarantineRestoreSelectedKeyed(t, c, Instant.parse("2026-01-01T00:00:00Z"), null, null, 2,
            ACTOR, true, keyed(mark));

        assertThat(page.restored()).hasSize(2);
        assertThat(page.nextAfter()).isNotNull();
        assertThat(mark.seen.get().nextAfter()).as("the stored outcome names the cursor the caller gets")
            .isEqualTo(page.nextAfter());
        assertThat(mark.calls).hasValue(1);
        var last = vectors.quarantineRestoreSelectedKeyed(t, c, Instant.parse("2026-01-01T00:00:00Z"), null,
            page.nextAfter(), 2, ACTOR, true, keyed(new Mark()));
        assertThat(last.restored()).hasSize(1);
        assertThat(last.nextAfter()).isNull();
        assertThat(java.util.stream.Stream.concat(page.restored().stream(), last.restored().stream()))
            .containsExactlyInAnyOrderElementsOf(hs);
    }

    @Test
    void aKeyedAuditRestoreHandsTheMarkerItsSource() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "ka-1", "ka-2", "ka-3");
        long id = audit(t, "reaper_quarantine").get(0).id();
        Mark mark = new Mark();

        var page = vectors.quarantineRestoreFromAuditKeyed(t, c, id, 0, 2, ACTOR, true, keyed(mark));

        assertThat(page.restored()).hasSize(2);
        assertThat(mark.seen.get().source().auditId()).isEqualTo(id);
        assertThat(mark.seen.get().source().nextOffset()).isEqualTo(2);
        assertThat(hs).containsAll(page.restored());
    }

    @Test
    void aKeyedCallWithNothingToRestoreStillSettlesItsKey() throws Exception {
        // An empty selection, an audit slice past the end and an origin with no sibling all return before any sibling
        // runs. The marker is what commits the ledger row, so each must still reach it, once, inside a transaction.
        String t = newTenant();
        String c = col("knowledge");
        List<String> hs = quarantined(t, c, "kn-1");
        long id = audit(t, "reaper_quarantine").get(0).id();

        Mark emptyWindow = new Mark();
        var w = vectors.quarantineRestoreSelectedKeyed(t, c, Instant.parse("2999-01-01T00:00:00Z"), null, null, 10,
            ACTOR, true, keyed(emptyWindow));
        assertThat(w.rows()).isEmpty();
        assertThat(emptyWindow.calls).hasValue(1);

        Mark pastEnd = new Mark();
        var a = vectors.quarantineRestoreFromAuditKeyed(t, c, id, 50, 10, ACTOR, true, keyed(pastEnd));
        assertThat(a.rows()).isEmpty();
        assertThat(a.source()).isNotNull();
        assertThat(pastEnd.calls).hasValue(1);

        String lonely = col("knowledge");
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), t, lonely);
        }
        Mark noSibling = new Mark();
        var n = vectors.quarantineRestoreKeyed(t, lonely, List.of(Chash.ofText("nowhere").toHex()), ACTOR, true,
            keyed(noSibling));
        assertThat(n.missing()).hasSize(1);
        assertThat(noSibling.calls).hasValue(1);
        assertThat(inCollection(t, quarantineOf(c), hs.get(0))).as("none of the three moved anything").isTrue();
    }

    /** Blocks until a backend is waiting on an advisory lock inside a quarantine_restore_chunks call. */
    private void awaitAdvisoryLockWaiter() throws Exception {
        var activity = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));
        var waitType = DSL.field(DSL.name("pg_catalog", "pg_stat_activity", "wait_event_type"), String.class);
        var waitEvent = DSL.field(DSL.name("pg_catalog", "pg_stat_activity", "wait_event"), String.class);
        var query = DSL.field(DSL.name("pg_catalog", "pg_stat_activity", "query"), String.class);
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            while (System.nanoTime() < deadline) {
                int waiting = ctx.fetchCount(activity,
                    waitType.eq("Lock").and(waitEvent.eq("advisory")).and(query.like("%quarantine_restore_chunks%")));
                if (waiting > 0) return;
                Thread.sleep(20);
            }
        }
        throw new AssertionError("the restore never reached its advisory lock wait");
    }
}
