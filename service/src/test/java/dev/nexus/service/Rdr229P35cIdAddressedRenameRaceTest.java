// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionRegistry;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.function.LongConsumer;
import java.util.function.LongFunction;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), census invariants I7 and I8, in the rename gap P3.5a's round 2 defined (linda's ruling): the route
 * boundary has resolved the id X to the label B, and just before the write's own by-id registry read X's row is renamed in place to C
 * (it keeps its id, nothing else is written: P3.7's rename) and a NEW row Y takes the label B. The write then goes ahead, by its id, under
 * the row's CURRENT label:
 * <ul>
 *   <li>I7: it reaches X through {@code CollectionIds}, never resolves the label itself, so the new holder Y of B keeps every row it had
 *       and gets none;</li>
 *   <li>I8: every row it files for X carries C, the locked row's current label, never B, the label the request carried from the boundary.</li>
 * </ul>
 * The rename is {@link CollectionRouteFixture#renameInPlaceBeforeTheWritesRead} (P3.5a's hook: the rename runs on ANOTHER thread, since the
 * hook runs on the request's thread, where the carried ids are set, and a setup that read the registry there would read the requested id and
 * pass by accident). Each test asserts the hook ran and that rows landed (or, for a delete, that X's rows are gone), so none passes vacuously.
 *
 * <p>The sites here are the ones P3.5a's own table-driven test ({@code CollectionRoutesByIdTest}) cannot land rows for on its fixture, or lands
 * none for: the manifest write and append (a row with no chunk, so the fallback name is the label), assign_from_chashes (both its transactions),
 * the meta record, and the reads-then-writes of the aspect, highlight and taxonomy deletes. The three foreign keys that tie a child's NAME to
 * another table's name are dropped first, as the name-key drops do ({@code Rdr229P37PrerequisitesTest}): with them in place a write under
 * the new label is refused (23503) until the name keys go (P3.7 for the registry's, P4.1 for the rest), which is pinned there and is not what this class measures.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cIdAddressedRenameRaceTest {

    private static final String PATH = "p35c-i7.md";
    private static final String DOC = "1.1.7";
    private static final String EMBEDDING_384 = "[" + String.join(",", java.util.Collections.nCopies(384, "0.1")) + "]";

    CollectionRouteFixture fx;
    CollectionRouteFixture fxLocal;   // the engine that serves a re-embed (reembed start); built like HeavyRoutesRenameGapTest's
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        fx.asSuperuser(ctx -> {
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(ctx);   // the registry's name-key drop: a one-row rename cascades nowhere
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(DSL.name("fk_catalog_chunks_chunk")).execute();
            ctx.alterTable(TOPIC_ASSIGNMENTS).dropConstraintIfExists(DSL.name("topic_assignments_chunk_fk")).execute();
            ctx.alterTable(DOCUMENT_HIGHLIGHTS).dropConstraintIfExists(DSL.name("fk_doc_highlights_catalog_doc")).execute();
            // the name uniques the delete and purge tests would trip: X's stale rows (named B) beside the new holder's (named B). The id
            // arbiters (built when the gate latches) remain; the name keys go with P3.7 / P4.1
            ctx.alterTable(DOCUMENT_ASPECTS).dropConstraintIfExists(DSL.name("document_aspects_tenant_col_path_uq")).execute();
            ctx.alterTable(TAXONOMY_META).dropConstraintIfExists(DSL.name("taxonomy_meta_pk")).execute();
            return null;
        });
        fx.gate3.set(true);   // latches the gate, and builds the id arbiters the latched upserts infer
        fxLocal = new CollectionRouteFixture(true);
        fxLocal.buildIdTwins();
        fxLocal.asSuperuser(ctx -> {
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (!key.getFields().contains(CATALOG_COLLECTIONS.NAME)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
            ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(CATALOG_COLLECTIONS.getPrimaryKey().getName()).cascade().execute();
            // the registry keeps its live arbiter for the upsert that registers the re-embed's target (see HeavyRoutesRenameGapTest.prepared)
            ctx.createUniqueIndex("i7_catalog_collections_tenant_name").on(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID,
                CATALOG_COLLECTIONS.NAME).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(DSL.name("fk_catalog_chunks_chunk")).execute();
            ctx.alterTable(TOPIC_ASSIGNMENTS).dropConstraintIfExists(DSL.name("topic_assignments_chunk_fk")).execute();
            ctx.deleteFrom(EMBEDDING_PROFILE).where(EMBEDDING_PROFILE.TENANT_ID.eq(fxLocal.tenant)).and(EMBEDDING_PROFILE.CONTENT_TYPE.eq("docs")).execute();
            ctx.insertInto(EMBEDDING_PROFILE, EMBEDDING_PROFILE.TENANT_ID, EMBEDDING_PROFILE.CONTENT_TYPE, EMBEDDING_PROFILE.EMBEDDING_MODEL,
                EMBEDDING_PROFILE.DIMENSION).values(fxLocal.tenant, "docs", "bge-base-en-v15-768", 768).execute();
            return null;
        });
        fxLocal.service.setReembedServedModelsForTests(() -> List.of("bge-base-en-v15-768", "minilm-l6-v2-384"));
        fxLocal.gate3.set(true);
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
        if (fxLocal != null) fxLocal.close();
    }

    private String registered(String stem) {
        String l = label("i7-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    /** What one run of a request in the gap left behind. */
    private record Gap(Resp resp, long x, String c, long y) { }

    /**
     * Runs {@code request} (given the id X of {@code b}) with, just before the write's own by-id read: {@code b} renamed to C in place and a
     * new row Y registered under {@code b} ({@code seedNewHolder} runs for it, by its id). Asserts the hook ran and that X now bears C.
     */
    private Gap inTheGap(String b, LongConsumer seedNewHolder, LongFunction<Resp> request) {
        long x = fx.idOf(b);
        // warm the process-wide cache for the label: a write that went back to the label would hit the cache and the new row alike
        CollectionRegistry.lookup(new dev.nexus.service.db.TenantScope(fx.svcDs), fx.tenant, b);
        try (CollectionRouteFixture.RenameGap gap = fx.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), "i8-" + (n++),
                gap0 -> gap0.newHolders().forEach(id -> seedNewHolder.accept(id)))) {
            Resp r = request.apply(x);
            assertThat(gap.ran()).as("setup: the rename ran before the write's own by-id read (%s)", r.body()).isTrue();
            String c = gap.currentLabelOf().get(x);
            long y = gap.newHolders().iterator().next();
            assertThat(fx.labelOf(x)).as("X is the renamed row's id").isEqualTo(c);
            assertThat(fx.idOf(b)).as("and the label B is the new row's").isEqualTo(y).isNotEqualTo(x);
            return new Gap(r, x, c, y);
        }
    }

    private void assertOk(Gap g) {
        assertThat(g.resp().status()).as("the write goes ahead by its id, under the row's current label: %s", g.resp().body()).isEqualTo(200);
    }

    // ---- what the rows say ---------------------------------------------------------------------------------------------------------

    private List<String> names(org.jooq.Field<String> name, org.jooq.Field<Long> id, org.jooq.Table<?> table, org.jooq.Field<String> tenant, long x) {
        return fx.asSuperuser(ctx -> ctx.select(name).from(table).where(tenant.eq(fx.tenant).and(id.eq(x))).fetch(name));
    }

    /** Asserts rows of X landed, every one under C, and that none of the new holder's id is among them. */
    private void assertStored(Gap g, List<String> storedUnderX, int rowsOfTheNewHolder) {
        assertThat(storedUnderX).as("non-vacuity: rows were filed for id %d", g.x()).isNotEmpty();
        assertThat(storedUnderX).as("I8: they carry the locked row's current label %s, never the boundary's", g.c()).allMatch(g.c()::equals);
        assertThat(rowsOfTheNewHolder).as("I7: the new holder of the old label got none").isZero();
    }

    private int topicsOf(long id) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.TENANT_ID.eq(fx.tenant).and(TOPICS.COLLECTION_ID.eq(id))));
        return v;
    }

    private int metasOf(long id) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(TAXONOMY_META, TAXONOMY_META.TENANT_ID.eq(fx.tenant).and(TAXONOMY_META.COLLECTION_ID.eq(id))));
        return v;
    }

    private int aspectsOf(long id) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID.eq(fx.tenant).and(DOCUMENT_ASPECTS.COLLECTION_ID.eq(id))));
        return v;
    }

    private int highlightsOf(long id) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(DOCUMENT_HIGHLIGHTS,
            DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(fx.tenant).and(DOCUMENT_HIGHLIGHTS.COLLECTION_ID.eq(id))));
        return v;
    }

    private int manifestRowsOf(long id) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS,
            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(fx.tenant).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(id))));
        return v;
    }

    // ---- seeds ---------------------------------------------------------------------------------------------------------------------

    private long seedTopic(long id, String label) {
        Resp r = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + id + ",\"label\":" + json(label) + ",\"doc_count\":1}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
        return r.json().path("id").asLong();
    }

    private void seedCentroid(long id, long topicId) {
        Resp r = fx.post("/v1/taxonomy/centroids/upsert", "{\"records\":[{\"collection_id\":" + id + ",\"topic_id\":" + topicId
            + ",\"label\":\"i8c\",\"doc_count\":1,\"embedding\":" + EMBEDDING_384 + "}]}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private String seedChunk(long id, String text) {
        String hex = Chash.ofText(text).toHex();
        Resp r = fx.post("/v1/vectors/upsert-chunks", "{\"collection_id\":" + id + ",\"ids\":[\"" + hex + "\"],\"documents\":[" + json(text) + "]}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
        return hex;
    }

    private void seedMeta(long id) {
        Resp r = fx.post("/v1/taxonomy/meta/record", "{\"collection_id\":" + id + ",\"doc_count\":3}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private void seedAspect(long id) {
        String c = fx.labelOf(id);
        String docId = "own-" + c;
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, docId, c);
            return null;
        });
        Resp r = fx.post("/v1/aspects/upsert", "{\"collection_id\":" + id + ",\"source_path\":" + json(PATH)
            + ",\"doc_id\":" + json(docId) + ",\"confidence\":0.9,\"extracted_at\":\"2026-01-01T00:00:00Z\","
            + "\"model_version\":\"v1\",\"extractor_name\":\"ex\",\"salient_sentences\":\"[\\\"held\\\"]\"}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private void seedDocument() {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertCatalogDocument(ctx, fx.tenant, DOC);
            return null;
        });
    }

    // ---- the manifest write and append (a row whose chunk is not found names the collection by the locked row's current label) -------

    private static final String MANIFEST_ROW = "{\"position\":0,\"chash\":\"" + Chash.ofText("p35c-i8-manifest").toHex() + "\"}";

    @Test
    void manifestWrite_underAnIdRenamedInTheGap_filesItsRowsUnderTheCurrentLabel() {
        String b = registered("mw");
        seedDocument();
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/catalog/manifest/write", "{\"collection_id\":" + x + ",\"doc_id\":\"" + DOC + "\",\"rows\":[" + MANIFEST_ROW + "]}"));
        assertOk(g);
        assertStored(g, names(CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CATALOG_DOCUMENT_CHUNKS,
            CATALOG_DOCUMENT_CHUNKS.TENANT_ID, g.x()), manifestRowsOf(g.y()));
    }

    @Test
    void manifestAppend_underAnIdRenamedInTheGap_filesItsRowsUnderTheCurrentLabel() {
        String b = registered("ma");
        seedDocument();
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/catalog/manifest/append", "{\"collection_id\":" + x + ",\"doc_id\":\"" + DOC + "\",\"rows\":[" + MANIFEST_ROW + "]}"));
        assertOk(g);
        assertStored(g, names(CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CATALOG_DOCUMENT_CHUNKS,
            CATALOG_DOCUMENT_CHUNKS.TENANT_ID, g.x()), manifestRowsOf(g.y()));
    }

    // ---- the taxonomy writes -----------------------------------------------------------------------------------------------------------

    @Test
    void topicInsert_underAnIdRenamedInTheGap_filesTheTopicUnderTheCurrentLabel() {
        String b = registered("ti");
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + x + ",\"label\":\"p35c-i7\",\"doc_count\":1}"));
        assertOk(g);
        assertStored(g, names(TOPICS.COLLECTION, TOPICS.COLLECTION_ID, TOPICS, TOPICS.TENANT_ID, g.x()), topicsOf(g.y()));
    }

    @Test
    void metaRecord_underAnIdRenamedInTheGap_recordsUnderTheCurrentLabel() {
        String b = registered("mr");
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/taxonomy/meta/record", "{\"collection_id\":" + x + ",\"doc_count\":9}"));
        assertOk(g);
        assertStored(g, names(TAXONOMY_META.COLLECTION, TAXONOMY_META.COLLECTION_ID, TAXONOMY_META, TAXONOMY_META.TENANT_ID, g.x()), metasOf(g.y()));
    }

    /** X has a chunk and a centroid under B (all seeded before the rename); the own pass assigns the chunk to X's topic. */
    @Test
    void assignFromChashes_ownPass_underAnIdRenamedInTheGap_filesItsAssignmentsUnderTheCurrentLabel() {
        String b = registered("af");
        long x = fx.idOf(b);
        String hex = seedChunk(x, "p35c-i8 own pass chunk");
        seedCentroid(x, seedTopic(x, "i8-own"));
        Gap g = inTheGap(b, y -> { },
            id -> fx.post("/v1/taxonomy/assignments/assign_from_chashes",
                "{\"collection_id\":" + id + ",\"chashes\":[\"" + hex + "\"],\"cross_collection\":false}"));
        assertOk(g);
        assertThat(g.resp().json().path("assigned").asInt()).as("the chunk is found by its id and assigned: %s", g.resp().body()).isEqualTo(1);
        assertStored(g, names(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, TOPIC_ASSIGNMENTS,
            TOPIC_ASSIGNMENTS.TENANT_ID, g.x()), assignmentsOf(g.y()));
    }

    /**
     * The cross pass is its own transaction and the row it reads carries the label too: the request still says B to the end, and a cross pass
     * that handed B to the function would file X's chunk through the NEW holder of B. Z is a second collection with a centroid (the tenant holds
     * other tests' centroids too, so the nearest foreign one is whichever ties first); the rows the cross pass files are X's rows whose topic is
     * not X's own.
     */
    @Test
    void assignFromChashes_crossPass_underAnIdRenamedInTheGap_filesItsAssignmentsUnderTheCurrentLabel() {
        String b = registered("ax");
        String z = registered("az");
        long x = fx.idOf(b);
        long zId = fx.idOf(z);
        String hex = seedChunk(x, "p35c-i8 cross pass chunk");
        long xTopic = seedTopic(x, "i8-x");
        seedCentroid(x, xTopic);
        long zTopic = seedTopic(zId, "i8-z");
        seedCentroid(zId, zTopic);
        Gap g = inTheGap(b, y -> { },
            id -> fx.post("/v1/taxonomy/assignments/assign_from_chashes",
                "{\"collection_id\":" + id + ",\"chashes\":[\"" + hex + "\"],\"cross_collection\":true}"));
        assertOk(g);
        assertThat(g.resp().json().path("cross_assigned").asInt()).as("the cross pass assigned the chunk: %s", g.resp().body()).isEqualTo(1);
        List<String> crossRows = fx.asSuperuser(ctx -> ctx.select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(fx.tenant).and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.eq(g.x())).and(TOPIC_ASSIGNMENTS.TOPIC_ID.ne(xTopic)))
            .fetch(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION));
        assertThat(crossRows).as("non-vacuity: the cross pass filed a row for X under a foreign topic").isNotEmpty().allMatch(g.c()::equals);
        assertThat(assignmentsOf(g.y())).as("I7: the new holder of the old label got none").isZero();
    }

    private int assignmentsOf(long id) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(TOPIC_ASSIGNMENTS,
            TOPIC_ASSIGNMENTS.TENANT_ID.eq(fx.tenant).and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.eq(id))));
        return v;
    }

    @Test
    void purgeCollection_underAnIdRenamedInTheGap_purgesItsOwnRowsAndLeavesTheNewHolders() {
        String b = registered("pc");
        long x0 = fx.idOf(b);
        seedTopic(x0, "doomed");
        seedMeta(x0);
        Gap g = inTheGap(b, y -> {
            seedTopic(y, "held");
            seedMeta(y);
        }, x -> fx.post("/v1/taxonomy/purge_collection", "{\"collection_id\":" + x + "}"));
        assertOk(g);
        assertThat(topicsOf(g.x())).as("non-vacuity: X's own topic is purged").isZero();
        assertThat(metasOf(g.x())).as("and its meta row").isZero();
        assertThat(topicsOf(g.y())).as("I7: the new holder's topic survives the purge").isEqualTo(1);
        assertThat(metasOf(g.y())).as("and its meta row").isEqualTo(1);
    }

    // ---- the aspect and highlight writes ------------------------------------------------------------------------------------------------

    @Test
    void aspectsUpsert_underAnIdRenamedInTheGap_filesTheRowUnderTheCurrentLabel() {
        String b = registered("au");
        String docId = "own-" + b;
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, docId, b);
            return null;
        });
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/aspects/upsert", "{\"collection_id\":" + x + ",\"source_path\":" + json(PATH) + ",\"doc_id\":" + json(docId)
                + ",\"confidence\":0.9,\"extracted_at\":\"2026-01-01T00:00:00Z\",\"model_version\":\"v1\",\"extractor_name\":\"ex\","
                + "\"salient_sentences\":\"[\\\"x\\\"]\"}"));
        assertOk(g);
        assertStored(g, names(DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.COLLECTION_ID, DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, g.x()),
            aspectsOf(g.y()));
    }

    @Test
    void aspectsDelete_underAnIdRenamedInTheGap_deletesItsOwnRowAndLeavesTheNewHolders() {
        String b = registered("ad");
        seedAspect(fx.idOf(b));
        Gap g = inTheGap(b, this::seedAspect,
            x -> fx.post("/v1/aspects/delete", "{\"collection_id\":" + x + ",\"source_path\":" + json(PATH) + "}"));
        assertOk(g);
        assertThat(aspectsOf(g.x())).as("non-vacuity: X's own aspect row is deleted").isZero();
        assertThat(aspectsOf(g.y())).as("I7: the new holder's aspect row survives").isEqualTo(1);
    }

    @Test
    void aspectsSetSalientByKey_underAnIdRenamedInTheGap_changesItsOwnRowAndLeavesTheNewHolders() {
        String b = registered("as");
        seedAspect(fx.idOf(b));
        Gap g = inTheGap(b, this::seedAspect,
            x -> fx.post("/v1/aspects/salient_sentences/set_by_key",
                "{\"collection_id\":" + x + ",\"source_path\":" + json(PATH) + ",\"sentences\":[\"changed\"]}"));
        assertOk(g);
        assertThat(salientOf(g.x())).as("non-vacuity: X's own sentences changed").contains("changed");
        assertThat(salientOf(g.y())).as("I7: the new holder's sentences are untouched").contains("held").doesNotContain("changed");
    }

    private String salientOf(long id) {
        return fx.asSuperuser(ctx -> ctx.select(DOCUMENT_ASPECTS.SALIENT_SENTENCES).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(fx.tenant).and(DOCUMENT_ASPECTS.COLLECTION_ID.eq(id))).fetchOne(x -> String.valueOf(x.value1())));
    }

    @Test
    void highlightsUpsert_underAnIdRenamedInTheGap_filesTheRowUnderTheCurrentLabel() {
        String b = registered("hu");
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/aspects/highlights/upsert", "{\"collection_id\":" + x + ",\"doc_id\":\"p35c-i7-doc\","
                + "\"ingested_at\":\"2026-01-01T00:00:00Z\",\"highlights_md\":\"p35c\"}"));
        assertOk(g);
        assertStored(g, names(DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.COLLECTION_ID, DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID,
            g.x()), highlightsOf(g.y()));
    }

    // ---- the routes folio's review found uncovered: chunk upsert, centroids, reembed start, pipeline create, remap, chash, document move ----------

    /** The rows of {@code table} that name collection {@code id}, with {@code cols}; the tenant of the fixture. */
    private List<org.jooq.Record> rowsOf(CollectionRouteFixture f, String table, List<String> cols, String idCol, long id) {
        return f.asSuperuser(ctx -> ctx.select(cols.stream().map(c -> DSL.field(DSL.name(c))).toList())
            .from(DSL.table(DSL.name("nexus", table)))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(f.tenant)).and(DSL.field(DSL.name(idCol), Long.class).eq(id)).fetch());
    }

    private void assertRowsUnder(Gap g, List<org.jooq.Record> x, String nameCol, List<org.jooq.Record> y) {
        assertThat(x).as("non-vacuity: rows were filed for id %d", g.x()).isNotEmpty();
        assertThat(x.stream().map(r -> String.valueOf(r.get(DSL.field(DSL.name(nameCol))))).toList())
            .as("I8: they carry the locked row's current label %s, never the boundary's", g.c()).allMatch(g.c()::equals);
        assertThat(y).as("I7: the new holder of the old label got none").isEmpty();
    }

    @Test
    void chunkUpsert_underAnIdRenamedInTheGap_filesTheChunkUnderTheCurrentLabelAndNotInTheNewHolder() {
        String b = registered("cu");
        String hex = Chash.ofText("p35c-i7 upsert " + b).toHex();
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/vectors/upsert-chunks", "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\"],\"documents\":[\"i7 upsert text\"]}"));
        assertOk(g);
        assertRowsUnder(g, rowsOf(fx, "chunks", List.of("collection"), "collection_id", g.x()), "collection",
            rowsOf(fx, "chunks", List.of("collection"), "collection_id", g.y()));
    }

    @Test
    void centroidUpsert_underAnIdRenamedInTheGap_filesTheCentroidUnderTheCurrentLabelAndNotInTheNewHolder() {
        String b = registered("ce");
        long topic = seedTopic(fx.idOf(b), "i7-centroid");
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/taxonomy/centroids/upsert", "{\"records\":[{\"collection_id\":" + x + ",\"topic_id\":" + topic
                + ",\"label\":\"i7c\",\"doc_count\":1,\"embedding\":" + EMBEDDING_384 + "}]}"));
        assertOk(g);
        assertRowsUnder(g, rowsOf(fx, "taxonomy_centroids", List.of("collection"), "collection_id", g.x()), "collection",
            rowsOf(fx, "taxonomy_centroids", List.of("collection"), "collection_id", g.y()));
    }

    @Test
    void pipelineCreate_underAnIdRenamedInTheGap_filesTheRunUnderTheCurrentLabelAndNotInTheNewHolder() {
        String b = registered("pl");
        String hash = "i7-" + b;
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/pipeline/create", "{\"collection_id\":" + x + ",\"content_hash\":" + json(hash) + ",\"pdf_path\":\"/i7.pdf\"}"));
        assertOk(g);
        assertRowsUnder(g, rowsOf(fx, "pdf_pipeline", List.of("collection"), "collection_id", g.x()), "collection",
            rowsOf(fx, "pdf_pipeline", List.of("collection"), "collection_id", g.y()));
    }

    @Test
    void remapRecord_underAnIdRenamedInTheGap_filesTheFactUnderTheCurrentLabelAndNotInTheNewHolder() {
        String b = registered("rm");
        String target = registered("rmt");
        long targetId = fx.idOf(target);
        String chash = Chash.ofText("i7-remap-" + b).toHex();
        Gap g = inTheGap(b, y -> { },
            x -> fx.post("/v1/remap/record_batch", "{\"source_collection_id\":" + x + ",\"entries\":[{\"old_id\":\"i7-old\",\"new_chash\":\"" + chash
                + "\",\"target_collection_id\":" + targetId + ",\"provenance\":\"i7\"}]}"));
        assertOk(g);
        assertRowsUnder(g, rowsOf(fx, "chash_remap", List.of("source_collection"), "source_collection_id", g.x()), "source_collection",
            rowsOf(fx, "chash_remap", List.of("source_collection"), "source_collection_id", g.y()));
    }

    /** The re-embed start is on the engine that serves one: the same rename in the gap, on the local fixture. */
    @Test
    void reembedStart_underAnIdRenamedInTheGap_filesTheJobUnderTheCurrentLabelAndNotInTheNewHolder() {
        String b = label("i7-re-" + (n++));
        fxLocal.register(b);
        long x = fxLocal.idOf(b);
        String target = "docs__i7-re-" + (n++) + "__bge-base-en-v15-768__v1";
        CollectionRegistry.lookup(new dev.nexus.service.db.TenantScope(fxLocal.svcDs), fxLocal.tenant, b);
        try (CollectionRouteFixture.RenameGap gap = fxLocal.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), "i8-" + (n++), null)) {
            Resp r = fxLocal.post("/v1/vectors/reembed/jobs", "{\"source_collection_id\":" + x + ",\"target_collection\":" + json(target) + "}");
            assertThat(gap.ran()).as("setup: the rename ran before the write's own by-id read (%s)", r.body()).isTrue();
            assertThat(r.status()).as("the job is filed: %s", r.body()).isEqualTo(201);
            String c = gap.currentLabelOf().get(x);
            long y = gap.newHolders().iterator().next();
            assertThat(fxLocal.idOf(b)).as("the old label is the new row's").isEqualTo(y).isNotEqualTo(x);
            var forX = rowsOf(fxLocal, "reembed_jobs", List.of("source_collection"), "source_collection_id", x);
            assertThat(forX).as("non-vacuity: the job was filed for id %d", x).isNotEmpty();
            assertThat(forX.stream().map(row -> String.valueOf(row.get(0))).toList()).as("I8: under the current label %s", c).allMatch(c::equals);
            assertThat(rowsOf(fxLocal, "reembed_jobs", List.of("source_collection"), "source_collection_id", y)).as("I7: none for the new holder").isEmpty();
        }
    }

    /** A read by id: the counts are X's rows whichever collection bears the old label now. */
    @Test
    void chashCount_byAnIdWhoseLabelAnotherCollectionTookAfterTheRename_countsTheRenamedRowsAndNotTheNewHoldersOfTheOldLabel() {
        String b = registered("ch");
        long x = fx.idOf(b);
        seedChunk(x, "p35c-i7 chash one");
        seedChunk(x, "p35c-i7 chash two");
        String c = label("i7-ch-renamed-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, c)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(x)).execute());
        fx.register(b);   // a NEW row takes the old label
        long y = fx.idOf(b);
        seedChunk(y, "p35c-i7 chash held 1");
        seedChunk(y, "p35c-i7 chash held 2");
        seedChunk(y, "p35c-i7 chash held 3");
        Resp r = fx.get("/v1/chash/count_for_collection?collection_id=" + x);
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("count").asInt()).as("the renamed row's two chunks, not the new holder's three: %s", r.body()).isEqualTo(2);
    }

    /** S and T are documents of X; T holds the new URI and has a highlights row, which the move re-keys to S's collection: X, under its current label. */
    @Test
    void documentMove_underAnIdRenamedInTheGap_rekeysTheHighlightsToTheRenamedRowAndNotTheNewHolder() {
        String b = registered("dm");
        String s = "1.1.101", t = "1.1.102";
        fx.asSuperuser(ctx -> {
            ctx.insertInto(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS.TENANT_ID, fx.tenant)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS.TUMBLER_PREFIX, "1.1")
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS.NAME, "i7-owner")
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS.OWNER_TYPE, "curator").onConflictDoNothing().execute();
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, s, b);
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, t, b);
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///old/i7.pdf").set(CATALOG_DOCUMENTS.FILE_PATH, "/old/i7.pdf")
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(s)).execute();
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///new/i7.pdf").set(CATALOG_DOCUMENTS.FILE_PATH, "/new/i7.pdf")
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(t)).execute();
            return null;
        });
        Resp seeded = fx.post("/v1/aspects/highlights/upsert", "{\"collection_id\":" + fx.idOf(b) + ",\"doc_id\":" + json(t)
            + ",\"source_uri\":\"file:///new/i7.pdf\",\"ingested_at\":\"2026-01-01T00:00:00Z\",\"highlights_md\":\"i7 marks\"}");
        assertThat(seeded.status()).as("setup: %s", seeded.body()).isEqualTo(200);
        // the move names a tumbler, not a collection id, so the gap hook (armed by a request that carries one) cannot fire: the rename and the
        // new holder of the old label are in place before the request, which is the same state the gap leaves
        long x = fx.idOf(b);
        String c = label("i7-dm-renamed-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, c)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(x)).execute());
        fx.register(b);
        long y = fx.idOf(b);
        assertThat(y).as("setup: the old label is a new row's").isNotEqualTo(x);
        Resp r = fx.post("/v1/catalog/documents/move",
            "{\"tumbler\":" + json(s) + ",\"new_source_uri\":\"file:///new/i7.pdf\",\"new_file_path\":\"/new/i7.pdf\",\"dry_run\":false}");
        assertThat(r.status()).as("the move goes ahead by id: %s", r.body()).isEqualTo(200);
        var moved = rowsOf(fx, "document_highlights", List.of("collection", "doc_id"), "collection_id", x);
        assertThat(moved).as("non-vacuity: the highlights now belong to S, in X").extracting(row -> String.valueOf(row.get("doc_id"))).contains(s);
        assertThat(moved.stream().map(row -> String.valueOf(row.get("collection"))).toList()).as("I8: under the current label %s", c).allMatch(c::equals);
        assertThat(rowsOf(fx, "document_highlights", List.of("collection"), "collection_id", y)).as("I7: the new holder got none").isEmpty();
    }
}
