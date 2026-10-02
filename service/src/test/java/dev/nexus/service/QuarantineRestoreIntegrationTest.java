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
import java.util.List;
import java.util.Map;

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
                    CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(tenant, doc, position, Chash.fromHex(hex).toBytes(), position, collection).execute();
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
        // Make the chunk look old: written, quarantined and left there for a long time.
        OffsetDateTime old = OffsetDateTime.now().minusDays(90);
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
        assertThat(metadataOf(after)).containsEntry("title", "stamps")
            .doesNotContainKeys("quarantined_at", "origin_collection", "quarantined_by", "reaper_quarantined_at");
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
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), t, c, List.of(h), List.of("origin 768 text"),
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
    void withoutReattachTheBytesComeBackHidden_andARerunAttachesThePresentChunk() throws Exception {
        String t = newTenant();
        String c = col("knowledge");
        String doc = "1.2.1";
        String h = quarantinedWith(t, c, Map.of("legacy", Map.<String, Object>of(
            "catalog_doc_id", doc, "title", "Legacy Note", "chunk_index", 0))).get(0);
        liveDoc(t, c, doc, "Legacy Note", 1, "legacy.md", Map.of());

        QuarantineRestoreOutcome bytesOnly =
            vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false, false);

        var row = bytesOnly.rows().get(0);
        assertThat(row.outcome()).isEqualTo("restored");
        assertThat(row.attached()).isFalse();
        assertThat(row.reattach()).as("what reattach WOULD have done is still reported").isEqualTo("attach");
        assertThat(row.noManifest()).isTrue();
        assertThat(row.reapableAfter()).isNotNull();
        assertThat(manifest(t, doc)).isEmpty();
        assertThat(visibleToGet(t, c, h)).as("hidden: restored, but no live owner row names it").isFalse();
        assertThat(visibleToSearch(t, c, "legacy text", h)).isFalse();
        assertThat(audit(t, "quarantine_restore")).singleElement()
            .satisfies(a -> assertThat(a.details()).contains("\"reattach\": false").contains("\"attached\": 0"));

        // Bytes-only is not a dead end: the chunk is present, and the same verb with reattach on finishes the job.
        QuarantineRestoreOutcome again = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
        assertThat(again.present()).containsExactly(h);
        assertThat(again.attached()).containsExactly(h);
        assertThat(manifest(t, doc)).containsExactly(new ManifestRow(0, h, c));
        assertThat(visibleToGet(t, c, h)).isTrue();
        assertThat(audit(t, "quarantine_restore")).as("the attach is audited too").hasSize(2);
        assertThat(audit(t, "quarantine_restore").get(1).chashes()).contains(h);
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

        assertThat(out.restored()).as("the bytes always come back").containsExactlyInAnyOrderElementsOf(hs);
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
            assertThat(a.details()).contains("\"superseded\": 4").contains("\"attached\": 1"));
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
        assertThat(metadataOf(chunk(t, c, h))).containsEntry("title", "tagged")
            .doesNotContainKeys("quarantined_at", "origin_collection", "quarantined_by", "reaper_quarantined_at");

        // Restore, let it age past the grace, and the reaper takes it again; it must restore again, clean.
        ReapableFixtures.agePastGrace(pg, t, c);
        assertThat(reaper(t).runOnce(null).tenant(t).collection(c).moved()).isEqualTo(1);
        assertThat(inCollection(t, quarantineOf(c), h)).as("quarantined once more").isTrue();
        QuarantineRestoreOutcome second = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(h), ACTOR, false);
        assertThat(second.restored()).containsExactly(h);
        assertThat(metadataOf(chunk(t, c, h))).containsEntry("title", "tagged")
            .doesNotContainKeys("quarantined_at", "origin_collection", "quarantined_by", "reaper_quarantined_at");
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
    void twoVersionsOfOnePositionAreRefusedWhenTheyLandInSeparateCalls() throws Exception {
        // The page-split case: each call names only one of the two, so a per-call check sees no claim at all.
        String t = newTenant();
        String c = col("knowledge");
        Map<String, Map<String, Object>> meta = new java.util.LinkedHashMap<>();
        meta.put("v1", Map.of("catalog_doc_id", "1.22.1", "chunk_index", 0, "title", "v1"));
        meta.put("v2", Map.of("catalog_doc_id", "1.22.1", "chunk_index", 0, "title", "v2"));
        meta.put("alone", Map.of("catalog_doc_id", "1.22.1", "chunk_index", 1));
        List<String> hs = quarantinedWith(t, c, meta);
        liveDoc(t, c, "1.22.1", "Two versions", 2, "p.md", Map.of());

        QuarantineRestoreOutcome first = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(hs.get(0)), ACTOR, false);
        assertThat(verdictOf(first, hs.get(0))).as("v2 still sits in quarantine, naming the same position")
            .isEqualTo("superseded");
        assertThat(reasonOf(first, hs.get(0))).isEqualTo("rival");
        QuarantineRestoreOutcome second = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(hs.get(1)), ACTOR, false);
        assertThat(verdictOf(second, hs.get(1))).as("v1 is in the collection now, naming the same position")
            .isEqualTo("superseded");
        assertThat(reasonOf(second, hs.get(1))).isEqualTo("rival");
        assertThat(manifest(t, "1.22.1")).as("neither version was chosen for the operator").isEmpty();
        assertThat(visibleToGet(t, c, hs.get(0))).isFalse();
        assertThat(visibleToGet(t, c, hs.get(1))).isFalse();

        QuarantineRestoreOutcome control = vectors.quarantineRestore(t, c, quarantineOf(c), List.of(hs.get(2)), ACTOR, false);
        assertThat(verdictOf(control, hs.get(2))).as("the control: a position with one chunk attaches").isEqualTo("attach");
    }

    @Test
    void twoVersionsOfOnePositionAreRefusedWhenTheyLandOnDifferentPagesOfOneSelection() throws Exception {
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

        assertThat(page1.rows()).singleElement().satisfies(r -> {
            assertThat(r.reattach()).isEqualTo("superseded");
            assertThat(r.reason()).isEqualTo("rival");
        });
        assertThat(page2.rows()).singleElement().satisfies(r -> {
            assertThat(r.reattach()).isEqualTo("superseded");
            assertThat(r.reason()).isEqualTo("rival");
        });
        assertThat(manifest(t, "1.23.1")).isEmpty();
        for (String h : hs) assertThat(inCollection(t, c, h)).isTrue();
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
    void aDeadlockWithAnotherWriterOfTheSameRowsIsTheTypedRetryableBusy_notAnOpaqueError() throws Exception {
        // The restore holds the quarantine row it deleted and waits for the document's index-run lock; the other
        // session holds that lock and then wants the same row. The restore waited first, so the deadlock detector
        // picks it as the victim (40P01).
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

            assertThatThrownBy(() -> call.get(15, java.util.concurrent.TimeUnit.SECONDS))
                .hasCauseInstanceOf(PgVectorRepository.QuarantineRestoreBusyException.class)
                .hasStackTraceContaining("deadlock");
            rowWanted.get(15, java.util.concurrent.TimeUnit.SECONDS);   // the survivor's delete goes through
            holder.rollback();
        } finally {
            pool.shutdownNow();
        }
        assertThat(inCollection(t, quarantineOf(c), h)).as("the victim rolled back whole: still in quarantine").isTrue();
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
            DSL.using(su, SQLDialect.POSTGRES)
                .insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                .values(t, doc, 0, Chash.fromHex(current).toBytes(), 0, c).execute();
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
    void twoVersionsOfOnePositionInDifferentSiblingsAreRefusedForBoth_inOneCall() throws Exception {
        Rivals r = rivalsAcrossSiblings();

        QuarantineRestoreOutcome dry = vectors.quarantineRestore(r.t(), r.c(), null,
            List.of(r.inReaperSibling(), r.inClientSibling(), r.control()), ACTOR, true);
        assertThat(reasonOf(dry, r.inReaperSibling())).as("the dry run predicts what the real call does")
            .isEqualTo("rival");
        assertThat(reasonOf(dry, r.inClientSibling())).isEqualTo("rival");
        assertThat(verdictOf(dry, r.control())).isEqualTo("attach");

        QuarantineRestoreOutcome out = vectors.quarantineRestore(r.t(), r.c(), null,
            List.of(r.inReaperSibling(), r.inClientSibling(), r.control()), ACTOR, false);

        assertThat(verdictOf(out, r.inReaperSibling())).isEqualTo("superseded");
        assertThat(reasonOf(out, r.inReaperSibling())).isEqualTo("rival");
        assertThat(verdictOf(out, r.inClientSibling())).isEqualTo("superseded");
        assertThat(reasonOf(out, r.inClientSibling())).isEqualTo("rival");
        assertThat(verdictOf(out, r.control())).as("a position with one chunk attaches").isEqualTo("attach");
        assertThat(manifest(r.t(), "1.42.1")).extracting(ManifestRow::position).containsExactly(1);
    }

    @Test
    void twoVersionsOfOnePositionInDifferentSiblingsAreRefusedForBoth_reaperSiblingFirstInSeparateCalls()
            throws Exception {
        Rivals r = rivalsAcrossSiblings();

        QuarantineRestoreOutcome one = vectors.quarantineRestore(r.t(), r.c(), null, List.of(r.inReaperSibling()),
            ACTOR, false);
        assertThat(reasonOf(one, r.inReaperSibling())).as("the other version sits in the client sibling")
            .isEqualTo("rival");
        QuarantineRestoreOutcome two = vectors.quarantineRestore(r.t(), r.c(), null, List.of(r.inClientSibling()),
            ACTOR, false);
        assertThat(reasonOf(two, r.inClientSibling())).as("and now the first version is in the origin")
            .isEqualTo("rival");
        assertThat(manifest(r.t(), "1.42.1")).as("neither version was chosen for the operator").isEmpty();
    }

    @Test
    void twoVersionsOfOnePositionInDifferentSiblingsAreRefusedForBoth_clientSiblingFirstInSeparateCalls()
            throws Exception {
        Rivals r = rivalsAcrossSiblings();

        QuarantineRestoreOutcome one = vectors.quarantineRestore(r.t(), r.c(), null, List.of(r.inClientSibling()),
            ACTOR, false);
        assertThat(reasonOf(one, r.inClientSibling())).as("the other version sits in the reaper's sibling")
            .isEqualTo("rival");
        QuarantineRestoreOutcome two = vectors.quarantineRestore(r.t(), r.c(), null, List.of(r.inReaperSibling()),
            ACTOR, false);
        assertThat(reasonOf(two, r.inReaperSibling())).isEqualTo("rival");
        assertThat(manifest(r.t(), "1.42.1")).isEmpty();
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
