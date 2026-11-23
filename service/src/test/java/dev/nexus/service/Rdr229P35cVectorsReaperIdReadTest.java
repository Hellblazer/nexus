// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.Chash;
import dev.nexus.service.vectors.ChunkConstraintAdmin;
import dev.nexus.service.vectors.ChunkKeyPlan;
import dev.nexus.service.vectors.ChunkKeySweep;
import dev.nexus.service.vectors.ChunkMetadataKeys;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import java.util.function.Function;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), item 6: {@link ReaperRepository}, {@link ChunkKeySweep} and {@link ChunkConstraintAdmin}
 * read the collection id once gate 3 holds, and keep the name path before it.
 *
 * <p>Diverging names. The registry row of a collection is renamed in place the way P3.7's one-row rename does it (the name keys
 * are dropped first, {@link ByIdRestoreSupport#dropRegistryNameKeys}), and the chunk rows keep the old name beside the id. A
 * reader that selects by id finds the rows by the new label and not by the old one; a reader before gate 3 finds them by the old
 * name, as before. A label the registry does not hold selects nothing, and a row with no id is no collection: neither falls back
 * to a name. Gate 3 is read on every call, so one instance flips between the two with no restart.
 */
class Rdr229P35cVectorsReaperIdReadTest extends AtomicWriteTestBase {

    private static final Gate3 LATCHED = () -> true;
    private static final int DIM = 768;
    private static final String MODEL = "bge-base-en-v15-768";
    private static final Duration BUDGET = Duration.ofSeconds(30);
    private static final List<String> ROW_KEYS =
        List.of("origin_collection", "quarantined_at", "quarantined_by", "reaper_quarantined_at");
    private static final String QUARANTINED_AT = "2026-09-01T00:00:00Z";

    @BeforeAll
    void dropTheNameKeys() throws Exception {
        su(ctx -> assertThat(ByIdRestoreSupport.dropRegistryNameKeys(ctx))
            .as("non-vacuity: the registry's name keys exist and are dropped").isNotEmpty());
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            PgContainerHelper.dropCatalogChunksFk(c);
        }
    }

    // ---- fixtures -----------------------------------------------------------------------------------------------------

    private String tenant(String tag) {
        return "p35cvr-" + tag + "-" + seq.incrementAndGet();
    }

    private static String docs(String tag) {
        return "docs__p35cvr-" + tag + "__" + MODEL + "__v1";
    }

    private static String knowledge(String tag) {
        return "knowledge__p35cvr-" + tag + "__" + MODEL + "__v1";
    }

    private static List<String> hashes(String tenant, String collection, int n) {
        List<String> out = new ArrayList<>();
        for (int i = 1; i <= n; i++) {
            out.add(Chash.ofText(tenant + "/" + collection + "/" + i).toHex());
        }
        return out;
    }

    /** Retired keys beside the two the CHECK pins, so a sweep has something to strip. */
    private static Map<String, Object> legacy(String hex) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("chunk_text_hash", hex);
        m.put("embedding_model", MODEL);
        m.put("title", "a title");
        m.put("chunk_index", 3);
        m.put("source_path", "/x/y");
        return m;
    }

    private void su(Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    /** Registers {@code collection} (a no-op when it is) and writes {@code n} chunks to it, each with {@code meta} of its hash. */
    private List<String> seed(DSLContext ctx, String tenant, String collection, int n, Function<String, Map<String, Object>> meta) {
        PgContainerHelper.insertCollection(ctx, tenant, collection);
        List<String> hex = hashes(tenant, collection, n);
        List<float[]> vectors = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (String h : hex) {
            float[] v = new float[DIM];
            java.util.Arrays.fill(v, 0.1f);
            vectors.add(v);
            texts.add("text " + h.substring(0, 8));
            metas.add(meta.apply(h));
        }
        PgContainerHelper.insertChunks(ctx, tenant, collection, hex, texts, vectors, metas);
        return hex;
    }

    /** The registry row renamed in place, the id kept, the chunk rows' own name left stale. */
    private static void rename(DSLContext ctx, String tenant, String from, String to, int chunkRows) {
        Long id = dev.nexus.service.db.CollectionIds.require(ctx, tenant, from);
        int n = ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute();
        assertThat(n).as("the registry row is renamed in place").isEqualTo(1);
        assertThat(dev.nexus.service.db.CollectionIds.require(ctx, tenant, to)).as("a rename keeps the id").isEqualTo(id);
        assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(from)).and(CHUNKS.COLLECTION_ID.eq(id))))
            .as("precondition: the chunks still carry the old name beside the id").isEqualTo(chunkRows);
        assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(to))))
            .as("precondition: no chunk carries the new name").isZero();
    }

    private record Renamed(String tenant, String oldName, String newName, List<String> chashes) {}

    private Renamed renamed(String tag, int n, Function<String, Map<String, Object>> meta) throws Exception {
        String tenant = tenant(tag), old = docs(tag), live = docs(tag + "-renamed");
        List<String> hex = new ArrayList<>();
        su(ctx -> {
            hex.addAll(seed(ctx, tenant, old, n, meta));
            rename(ctx, tenant, old, live, n);
        });
        return new Renamed(tenant, old, live, hex);
    }

    private ReaperRepository reaper(Gate3 gate) {
        return new ReaperRepository(tenantScope, gate);
    }

    private ChunkKeySweep sweep(Gate3 gate) {
        return new ChunkKeySweep(tenantScope, (t, c, b) -> 0L, gate);
    }

    private static ChunkKeyPlan plan() {
        List<String> allowed = new ArrayList<>(ChunkMetadataKeys.CHUNK);
        allowed.addAll(ROW_KEYS);
        return ChunkKeyPlan.of(allowed, List.of(ChunkKeyPlan.TAXONOMY_PREFIX, "quarantine-"),
            List.of("quarantined_by", "quarantined_at"));
    }

    /** Sets the id of the tenant's chunk rows of {@code collection} NULL: a stamp that has not reached them. The CHECKs that refuse it are dropped. */
    private void unstamp(String tenant, String collection) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            PgContainerHelper.dropConstraint(c, CHUNKS, "chunks_collection_id_check");
            PgContainerHelper.dropConstraint(c, CHUNKS, "chunks_collection_id_not_null");
            DSL.using(c, SQLDialect.POSTGRES).update(CHUNKS).setNull(CHUNKS.COLLECTION_ID)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection)).execute();
        }
    }

    private long retiredRows(String tenant) throws Exception {
        long[] n = new long[1];
        su(ctx -> n[0] = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant)
            .and(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "title").isNotNull())));
        return n[0];
    }

    // ---- ReaperRepository ---------------------------------------------------------------------------------------------

    @Test
    void collectionsWithChunks_namesTheRegistryLabelFromGate3_andTheChunkNameBefore() throws Exception {
        Renamed r = renamed("lst", 2, h -> Map.of("title", "t"));

        assertThat(reaper(LATCHED).collectionsWithChunks(r.tenant, Duration.ofSeconds(20)))
            .as("from gate 3 the label the registry holds now").containsExactly(r.newName);
        assertThat(reaper(Gate3.never()).collectionsWithChunks(r.tenant, Duration.ofSeconds(20)))
            .as("before gate 3 the chunk row's own name").containsExactly(r.oldName);
        assertThat(new ReaperRepository(tenantScope).collectionsWithChunks(r.tenant, Duration.ofSeconds(20)))
            .as("the one-argument constructor is the name path").containsExactly(r.oldName);
    }

    @Test
    void collectionsWithChunks_aRowWithNoIdIsNoCollection() throws Exception {
        String tenant = tenant("nul"), live = docs("nul-live"), unstamped = docs("nul-unstamped");
        su(ctx -> {
            seed(ctx, tenant, live, 1, h -> Map.of("title", "t"));
            seed(ctx, tenant, unstamped, 1, h -> Map.of("title", "t"));
        });
        unstamp(tenant, unstamped);

        assertThat(reaper(Gate3.never()).collectionsWithChunks(tenant, Duration.ofSeconds(20)))
            .as("before gate 3 the name lists it").containsExactlyInAnyOrder(live, unstamped);
        assertThat(reaper(LATCHED).collectionsWithChunks(tenant, Duration.ofSeconds(20)))
            .as("from gate 3 a NULL id is no collection, and the name is not the fallback").containsExactly(live);
    }

    @Test
    void describe_findsTheRowsByTheNewLabelFromGate3_andByTheOldNameBefore() throws Exception {
        Renamed r = renamed("dsc", 3, h -> Map.of("title", "t"));

        assertThat(reaper(LATCHED).describe(r.tenant, r.newName, r.chashes)).hasSize(3);
        assertThat(reaper(LATCHED).describe(r.tenant, r.oldName, r.chashes))
            .as("the old name is no label of the registry: nothing, never a fall back to the row's name").isEmpty();
        assertThat(reaper(LATCHED).describe(r.tenant, "", r.chashes)).as("an empty label is no collection").isEmpty();
        assertThat(reaper(Gate3.never()).describe(r.tenant, r.oldName, r.chashes)).hasSize(3);
        assertThat(reaper(Gate3.never()).describe(r.tenant, r.newName, r.chashes)).isEmpty();
    }

    @Test
    void describe_readsGate3OnEveryCall_andTheLatchOnlyGoesForward() throws Exception {
        Renamed r = renamed("flip", 2, h -> Map.of("title", "t"));
        AtomicBoolean latched = new AtomicBoolean(false);
        ReaperRepository repo = reaper(latched::get);

        assertThat(repo.describe(r.tenant, r.oldName, r.chashes)).hasSize(2);
        latched.set(true);
        assertThat(repo.describe(r.tenant, r.oldName, r.chashes)).as("latched: by id, so the old name finds nothing").isEmpty();
        assertThat(repo.describe(r.tenant, r.newName, r.chashes)).hasSize(2);
    }

    @Test
    void taggedOrigins_findsTheRenamedQuarantineByItsLabelFromGate3() throws Exception {
        String tenant = tenant("tag"), live = docs("tag"), q = "quarantine-" + live, q2 = "quarantine-" + docs("tag-renamed");
        Function<String, Map<String, Object>> engineOwned = h -> Map.of("origin_collection", "an-origin-label",
            "quarantined_by", "engine-reaper", "quarantined_at", QUARANTINED_AT, "reaper_quarantined_at", QUARANTINED_AT);
        su(ctx -> {
            seed(ctx, tenant, q, 2, engineOwned);
            rename(ctx, tenant, q, q2, 2);
        });

        assertThat(reaper(LATCHED).taggedOrigins(tenant, q2, 20_000)).containsExactly("an-origin-label");
        assertThat(reaper(LATCHED).taggedOrigins(tenant, q, 20_000)).as("the old name selects nothing").isEmpty();
        assertThat(reaper(LATCHED).taggedOrigins(tenant, "", 20_000)).isEmpty();
        assertThat(reaper(Gate3.never()).taggedOrigins(tenant, q, 20_000)).containsExactly("an-origin-label");
        assertThat(reaper(Gate3.never()).taggedOrigins(tenant, q2, 20_000)).isEmpty();
    }

    @Test
    void clientMovedOrigins_findsTheRenamedQuarantineByItsLabelFromGate3() throws Exception {
        String tenant = tenant("cli"), origin = knowledge("cli-origin"), q = "quarantine-" + knowledge("cli"),
            q2 = "quarantine-" + knowledge("cli-renamed");
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            seed(ctx, tenant, q, 2, h -> Map.of("origin_collection", origin));
            rename(ctx, tenant, q, q2, 2);
        });

        assertThat(reaper(LATCHED).clientMovedOrigins(tenant, q2, "knowledge", 20_000)).containsExactly(origin);
        assertThat(reaper(LATCHED).clientMovedOrigins(tenant, q, "knowledge", 20_000))
            .as("the old name selects nothing").isEmpty();
        assertThat(reaper(LATCHED).clientMovedOrigins(tenant, "", "knowledge", 20_000)).isEmpty();
        assertThat(reaper(Gate3.never()).clientMovedOrigins(tenant, q2, "knowledge", 20_000))
            .as("before gate 3 the chunk's name is the old one and the registry has no row by it").isEmpty();
    }

    @Test
    void taggedAndClientMovedOrigins_anUnrenamedCollectionReadsTheSameWhicheverGate() throws Exception {
        String tenant = tenant("same"), origin = knowledge("same-origin"), q = "quarantine-" + knowledge("same"),
            qe = "quarantine-" + knowledge("same-engine");
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            seed(ctx, tenant, q, 2, h -> Map.of("origin_collection", origin));
            seed(ctx, tenant, qe, 2, h -> Map.of("origin_collection", origin, "quarantined_by", "engine-reaper",
                "quarantined_at", QUARANTINED_AT, "reaper_quarantined_at", QUARANTINED_AT));
        });

        for (Gate3 gate : List.of(Gate3.never(), LATCHED)) {
            assertThat(reaper(gate).clientMovedOrigins(tenant, q, "knowledge", 20_000)).containsExactly(origin);
            assertThat(reaper(gate).taggedOrigins(tenant, qe, 20_000)).containsExactly(origin);
            assertThat(reaper(gate).taggedOrigins(tenant, q, 20_000)).as("a client's rows are not the engine's").isEmpty();
        }
    }

    /**
     * RDR-229 P3.5c (linda 2026-11-23, ruling b): from gate 3 a quarantine chunk that carries the origin LABEL tag and no origin id is no sibling
     * of the label's current holder (a label names whichever row holds it now): it is skipped, counted and WARNed. A quarantine collection
     * whose chunks carry the id is found, and before the latch the label tag finds the first one as it always did.
     */
    @Test
    void siblings_fromGate3_aQuarantineChunkWithOnlyTheLabelTagIsNoSiblingOfTheLabelsHolder_andTheSkipIsWarned() throws Exception {
        String tenant = tenant("sib"), origin = knowledge("sib-origin"), qLabelOnly = "quarantine-" + knowledge("sib-label"),
            qWithId = "quarantine-" + knowledge("sib-id");
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, origin);
            long originId = dev.nexus.service.db.CollectionIds.require(ctx, tenant, origin);
            seed(ctx, tenant, qLabelOnly, 2, h -> Map.of("origin_collection", origin));
            seed(ctx, tenant, qWithId, 2, h -> Map.of("origin_collection", origin, "origin_collection_id", originId));
        });
        var unlatched = new dev.nexus.service.vectors.PgVectorRepository(tenantScope, embedder, embedder, Gate3.never());
        var latchedRepo = new dev.nexus.service.vectors.PgVectorRepository(tenantScope, embedder, embedder, LATCHED);
        assertThat(unlatched.resolveQuarantineSiblings(tenant, origin)).as("before gate 3 the label tag finds both")
            .contains(qLabelOnly, qWithId);

        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(dev.nexus.service.db.QuarantineOrigin.class);
        var appender = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        appender.start();
        logger.addAppender(appender);
        long before = dev.nexus.service.db.QuarantineOrigin.NO_ORIGIN_ID_SKIPPED.get();
        List<String> siblings;
        try {
            siblings = latchedRepo.resolveQuarantineSiblings(tenant, origin);
        } finally {
            logger.detachAppender(appender);
        }

        assertThat(siblings).as("from gate 3 the id-tagged quarantine collection is a sibling and the label-only one is not")
            .contains(qWithId).doesNotContain(qLabelOnly);
        assertThat(dev.nexus.service.db.QuarantineOrigin.NO_ORIGIN_ID_SKIPPED.get()).as("the skip is counted").isGreaterThan(before);
        assertThat(appender.list).as("and a WARN says so").anyMatch(e -> e.getLevel() == ch.qos.logback.classic.Level.WARN
            && e.getFormattedMessage().contains("carry no origin_collection_id"));
    }

    @Test
    void lifecycleStates_namesTheRegistryLabel_whichGate() throws Exception {
        Renamed r = renamed("lcs", 1, h -> Map.of("title", "t"));

        for (Gate3 gate : List.of(Gate3.never(), LATCHED)) {
            assertThat(reaper(gate).lifecycleStates(r.tenant)).containsKey(r.newName).doesNotContainKey(r.oldName);
        }
    }

    // ---- collectionStats (linda (iii)): the by-id read equals the by-name read on a half-stamped collection ---------------

    @Test
    void collectionStats_onAHalfStampedCollection_readsByIdWhatTheNameFormReads_unlatchedAndLatched() throws Exception {
        String tenant = tenant("hst"), c = docs("hst"), other = docs("hst-other");
        List<String> hex = new ArrayList<>();
        su(ctx -> {
            hex.addAll(seed(ctx, tenant, c, 4, h -> Map.of("title", "t")));
            seed(ctx, tenant, other, 2, h -> Map.of("title", "t"));
        });
        try (Connection conn = pg.createConnection("")) {
            conn.setAutoCommit(true);
            PgContainerHelper.dropConstraint(conn, CHUNKS, "chunks_collection_id_check");
            PgContainerHelper.dropConstraint(conn, CHUNKS, "chunks_collection_id_not_null");
            int n = DSL.using(conn, SQLDialect.POSTGRES).update(CHUNKS).setNull(CHUNKS.COLLECTION_ID)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(c))
                .and(CHUNKS.CHASH.in(hex.subList(0, 2).stream().map(h -> java.util.HexFormat.of().parseHex(h)).toList())).execute();
            assertThat(n).as("fixture: half of the collection's chunks have no id yet").isEqualTo(2);
        }

        var unlatched = new dev.nexus.service.vectors.PgVectorRepository(tenantScope, embedder, embedder, Gate3.never())
            .collectionStats(tenant);
        var latchedRows = new dev.nexus.service.vectors.PgVectorRepository(tenantScope, embedder, embedder, LATCHED)
            .collectionStats(tenant);

        assertThat(unlatched).as("non-vacuity: the half-stamped collection is listed with every one of its chunks")
            .anySatisfy(r -> {
                assertThat(r.get("name")).isEqualTo(c);
                assertThat(((Number) r.get("stored_count")).longValue()).isEqualTo(4L);
            });
        assertThat(latchedRows).as("by id (latched) exactly what the name form (unlatched) answers: the view gives an unstamped registered chunk "
            + "its registry id").isEqualTo(unlatched);
    }

    // ---- ChunkKeySweep ------------------------------------------------------------------------------------------------

    @Test
    void sweepDryRun_listsTheRegistryLabelFromGate3_andTheChunkNameBefore() throws Exception {
        Renamed r = renamed("dry", 3, Rdr229P35cVectorsReaperIdReadTest::legacy);

        var latched = sweep(LATCHED).dryRun(r.tenant, plan(), Map.of(), 100, false);
        assertThat(latched.collections()).extracting(ChunkKeySweep.CollectionCount::collection).containsExactly(r.newName);
        assertThat(latched.collections().get(0).retiredRows()).isEqualTo(3);
        var before = sweep(Gate3.never()).dryRun(r.tenant, plan(), Map.of(), 100, false);
        assertThat(before.collections()).extracting(ChunkKeySweep.CollectionCount::collection).containsExactly(r.oldName);
        assertThat(before.collections().get(0).retiredRows()).isEqualTo(3);
    }

    @Test
    void sweepApply_keysetAcrossBatchesOnTheIdPath_carriesTheLabel_andSweepsEveryRowOnce() throws Exception {
        String tenant = tenant("ks"), a = docs("ks-a"), b = docs("ks-b"), a2 = docs("ks-a-renamed");
        su(ctx -> {
            seed(ctx, tenant, a, 3, Rdr229P35cVectorsReaperIdReadTest::legacy);
            seed(ctx, tenant, b, 3, Rdr229P35cVectorsReaperIdReadTest::legacy);
            rename(ctx, tenant, a, a2, 3);
        });
        assertThat(retiredRows(tenant)).isEqualTo(6);

        ChunkKeySweep sw = sweep(LATCHED);
        ChunkKeySweep.Resume resume = null;
        long total = 0;
        boolean done = false;
        int calls = 0;
        String firstCursor = null;
        while (!done && calls++ < 12) {
            var applied = sw.apply(tenant, plan(), Map.of(), null, 2, 1, BUDGET, resume);
            total += applied.rowsSwept();
            done = applied.done();
            resume = applied.resume();
            if (firstCursor == null && resume != null && resume.cursor() != null) {
                firstCursor = resume.cursor().collection();
            }
        }
        assertThat(done).as("the run ended").isTrue();
        assertThat(total).as("each row swept once across the batches").isEqualTo(6);
        assertThat(firstCursor).as("the cursor names the collection by the registry's label, not the stale row name").isEqualTo(a2);
        assertThat(retiredRows(tenant)).as("nothing left to sweep").isZero();
    }

    /** The cursor carries the collection's id (nexus-p2m1x.25 review): a rename between batches leaves its label resolving to nothing. */
    @Test
    void sweepApply_aRenameBetweenBatchesDoesNotEndTheSweepEarly() throws Exception {
        String tenant = tenant("ksren"), a = docs("ksren-a"), a2 = docs("ksren-a-renamed");
        su(ctx -> seed(ctx, tenant, a, 4, Rdr229P35cVectorsReaperIdReadTest::legacy));
        ChunkKeySweep sw = sweep(LATCHED);

        var first = sw.apply(tenant, plan(), Map.of(), null, 2, 1, BUDGET, null);
        assertThat(first.rowsSwept()).isEqualTo(2);
        assertThat(first.done()).isFalse();
        assertThat(first.resume().cursor().collectionId()).as("the cursor carries the id").isNotNull();
        su(ctx -> rename(ctx, tenant, a, a2, 4));

        var second = sw.apply(tenant, plan(), Map.of(), null, 2, 1, BUDGET, first.resume());

        assertThat(second.rowsSwept()).as("the sweep went on past the cursor, the label having moved").isEqualTo(2);
        assertThat(retiredRows(tenant)).isZero();
    }

    @Test
    void sweepApply_theNamePathStillSweepsBeforeGate3() throws Exception {
        Renamed r = renamed("nam", 3, Rdr229P35cVectorsReaperIdReadTest::legacy);

        var applied = sweep(Gate3.never()).apply(r.tenant, plan(), Map.of(), null, 100, 5, BUDGET, null);

        assertThat(applied.rowsSwept()).isEqualTo(3);
        assertThat(applied.done()).isTrue();
        assertThat(retiredRows(r.tenant)).isZero();
    }

    @Test
    void sweepApply_onlyOneLabelRestrictsTheIdPathToThatCollection() throws Exception {
        Renamed r = renamed("only", 3, Rdr229P35cVectorsReaperIdReadTest::legacy);

        var oldLabel = sweep(LATCHED).apply(r.tenant, plan(), Map.of(), r.oldName, 100, 5, BUDGET, null);
        assertThat(oldLabel.rowsSwept()).as("the old name is no label: nothing is swept, no fall back to the row's name").isZero();
        assertThat(retiredRows(r.tenant)).isEqualTo(3);

        var newLabel = sweep(LATCHED).apply(r.tenant, plan(), Map.of(), r.newName, 100, 5, BUDGET, null);
        assertThat(newLabel.rowsSwept()).isEqualTo(3);
        assertThat(retiredRows(r.tenant)).isZero();
    }

    @Test
    void sweep_aRowWithNoIdIsNoCollection_untilTheNamePathStampsIt() throws Exception {
        String tenant = tenant("snul"), unstamped = docs("snul");
        su(ctx -> seed(ctx, tenant, unstamped, 3, Rdr229P35cVectorsReaperIdReadTest::legacy));
        unstamp(tenant, unstamped);

        assertThat(sweep(LATCHED).dryRun(tenant, plan(), Map.of(), 100, false).collections())
            .as("from gate 3 a row with no id is not listed").isEmpty();
        var skipped = sweep(LATCHED).apply(tenant, plan(), Map.of(), null, 100, 5, BUDGET, null);
        assertThat(skipped.rowsSwept()).isZero();
        assertThat(retiredRows(tenant)).as("and not swept: no fall back to the name").isEqualTo(3);

        var before = sweep(Gate3.never()).apply(tenant, plan(), Map.of(), null, 100, 5, BUDGET, null);
        assertThat(before.rowsSwept()).as("the name path takes it, as before gate 3").isEqualTo(3);
        assertThat(retiredRows(tenant)).isZero();
    }

    @Test
    void sweepBypass_isTheRegistryLabelsPrefixFromGate3_andTheChunkNamesBefore() throws Exception {
        // A live-named collection whose registry label has since been renamed to a quarantine- name: the bypass follows the label.
        String tenant = tenant("byp"), old = docs("byp"), bypassLabel = "quarantine-" + docs("byp-renamed");
        su(ctx -> {
            seed(ctx, tenant, old, 3, Rdr229P35cVectorsReaperIdReadTest::legacy);
            rename(ctx, tenant, old, bypassLabel, 3);
        });

        assertThat(sweep(LATCHED).dryRun(tenant, plan(), Map.of(), 100, false).collections())
            .as("from gate 3 the label is a bypass collection: the allow-list leaves it").isEmpty();
        assertThat(sweep(Gate3.never()).dryRun(tenant, plan(), Map.of(), 100, false).collections())
            .extracting(ChunkKeySweep.CollectionCount::collection).containsExactly(old);
    }

    @Test
    void preconditions_theManifestJoinMatchesOnTheIdFromGate3_andOnTheNameBefore() throws Exception {
        String tenant = tenant("pre"), col = docs("pre");
        su(ctx -> {
            List<String> hex = seed(ctx, tenant, col, 1, h -> {
                Map<String, Object> m = new LinkedHashMap<>(legacy(h));
                m.put("ttl_days", "30");
                return m;
            });
            PgContainerHelper.ownChunks(ctx, tenant, col, hex.toArray(String[]::new));
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.METADATA, JSONB.jsonb("{\"ttl_days\": 30}"))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).execute();
        });
        for (Gate3 gate : List.of(Gate3.never(), LATCHED)) {
            assertThat(sweep(gate).preconditions(tenant, plan()).get("keyless_ttl_rows"))
                .as("non-vacuity: the owner records the TTL, so the chunk's TTL has a home on either path").isZero();
        }

        // The manifest row's name now differs from the chunk's, its id does not.
        // (vectors-079 stamps a manifest row's name from its chunk until P4.1, so the diverged name, the state after P4.1, is written with triggers off,
        // on this session only)
        su(ctx -> {
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, "another-name")
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)).execute();
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
        });

        assertThat(sweep(Gate3.never()).preconditions(tenant, plan()).get("keyless_ttl_rows"))
            .as("by name the owner is not found").isEqualTo(1L);
        assertThat(sweep(LATCHED).preconditions(tenant, plan()).get("keyless_ttl_rows"))
            .as("by id it is").isZero();
    }

    @Test
    void preconditions_aDocumentWithNoPhysicalCollectionIsHomeless_whichGate() throws Exception {
        String tenant = tenant("home");
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, docs("home"));
            ctx.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, "1.1.1")
                .set(CATALOG_DOCUMENTS.TITLE, "homeless").set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "")
                .execute();
        });

        for (Gate3 gate : List.of(Gate3.never(), LATCHED)) {
            assertThat(sweep(gate).preconditions(tenant, plan()).get("homeless_documents")).isEqualTo(1L);
        }
    }

    // ---- ChunkConstraintAdmin -----------------------------------------------------------------------------------------

    @Test
    void constraintProbe_bypassFollowsTheRegistryLabelFromGate3_andTheChunkNameBefore() throws Exception {
        String tenant = tenant("prb"), kept = docs("prb-kept"), old = docs("prb-old"), bypassLabel = "quarantine-" + docs("prb-renamed");
        su(ctx -> {
            seed(ctx, tenant, kept, 2, Rdr229P35cVectorsReaperIdReadTest::legacy);
            seed(ctx, tenant, old, 3, Rdr229P35cVectorsReaperIdReadTest::legacy);
            rename(ctx, tenant, old, bypassLabel, 3);
        });
        var conn = new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());

        long latched = retiredOf(new ChunkConstraintAdmin(conn, LATCHED).probe(plan()), tenant);
        long before = retiredOf(new ChunkConstraintAdmin(conn, Gate3.never()).probe(plan()), tenant);
        long legacy = retiredOf(new ChunkConstraintAdmin(conn).probe(plan()), tenant);

        assertThat(latched).as("from gate 3 the renamed collection's label is a bypass prefix: only the kept collection counts").isEqualTo(2);
        assertThat(before).as("before gate 3 the chunk's own name is not a bypass prefix: both count").isEqualTo(5);
        assertThat(legacy).as("the one-argument constructor is the name path").isEqualTo(5);
    }

    private static long retiredOf(List<ChunkConstraintAdmin.TenantProbe> probes, String tenant) {
        return probes.stream().filter(p -> p.tenant().equals(tenant)).findFirst().orElseThrow().retiredRows();
    }
}
