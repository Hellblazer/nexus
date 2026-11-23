// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.concurrent.atomic.AtomicLong;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), the census's three stale-name matches in the re-home cascade, from gate 3. The chunk update of
 * a re-home carries the manifest, the topic assignments and {@code chunk_orphaned_at} through the chunk foreign key without
 * writing their id columns; the re-home gives the carried rows the target's id afterwards, and its model guard reads the
 * registry rows. After a one-row rename (simulated as the superuser does it, the registry row takes its label in place and the
 * content keeps the old name) a name match finds none of them. Each is matched by id from gate 3: the carried rows are those of
 * the source's id whose chunk now carries the target's id, so the rows of a chunk a collision left behind keep the source's id.
 * Before gate 3 the name forms are unchanged, which {@code CollectionRenameIdTest} (a re-home with no latch) pins.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRehomeCascadeByIdTest {

    private static final String REHOME = "/v1/catalog/collections/rehome";

    CollectionRouteFixture fx;
    int n;
    final AtomicLong topicIds = new AtomicLong(910_000);

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        fx.gate3.set(true);
        fx.asSuperuser(ctx -> {
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (!key.getFields().contains(CATALOG_COLLECTIONS.NAME)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
            ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(CATALOG_COLLECTIONS.getPrimaryKey().getName()).cascade().execute();
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private static byte[] bytes(String hex) {
        return HexFormat.of().parseHex(hex);
    }

    private static String hex(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    private String registered(String stem) {
        String l = label("cas-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    /**
     * Collection {@code coll} (registered) with a document, chunk {@code p1} owned by it through a manifest row and carrying a
     * topic assignment, and chunk {@code p2} owned by nothing, with its {@code chunk_orphaned_at} row.
     */
    private void seed(String coll, String p) {
        fx.asSuperuser(ctx -> {
            Long id = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            String model = PgContainerHelper.collectionModel(ctx, fx.tenant, coll);
            String doc = p + "-doc";
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
                .values(fx.tenant, doc, "Doc " + p, coll, id).execute();
            PgContainerHelper.insertChunk384(ctx, fx.tenant, coll, bytes(hex(p + "1")), VectorSeeds.vec(384, 0.1f));
            PgContainerHelper.insertChunk384(ctx, fx.tenant, coll, bytes(hex(p + "2")), VectorSeeds.vec(384, 0.1f));
            CollectionIdSeeds.manifestRows(ctx, fx.tenant, doc, coll, 0, hex(p + "1"));
            ctx.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION,
                    CHUNK_ORPHANED_AT.COLLECTION_ID, CHUNK_ORPHANED_AT.CHASH, CHUNK_ORPHANED_AT.EMBEDDING_MODEL,
                    CHUNK_ORPHANED_AT.ORPHANED_AT)
                .values(fx.tenant, coll, id, bytes(hex(p + "2")), model, OffsetDateTime.now()).onConflictDoNothing().execute();
            long topicId = topicIds.incrementAndGet();
            ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.COLLECTION_ID,
                    TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                .values(topicId, fx.tenant, "topic-" + p, coll, id, 0, OffsetDateTime.now(), "pending").execute();
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
                .values(fx.tenant, bytes(hex(p + "1")), topicId, "projection", coll, id, OffsetDateTime.now(), model).execute();
            return null;
        });
    }

    private String oneRowRename(String from, String stem) {
        long id = fx.idOf(from);
        String to = label("cas-" + stem + "-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        return to;
    }

    private Resp rehome(String source, String target) {
        return fx.post(REHOME, "{\"source\":" + json(source) + ",\"target\":" + json(target) + "}");
    }

    /** Typed ints (a typed local keeps AssertJ's assertThat from being ambiguous over a generic lambda result). */
    private int manifestRows(Condition where) {
        int c = fx.asSuperuser(ctx -> ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, where));
        return c;
    }

    private int assignments(Condition where) {
        int c = fx.asSuperuser(ctx -> ctx.fetchCount(TOPIC_ASSIGNMENTS, where));
        return c;
    }

    private int orphans(Condition where) {
        int c = fx.asSuperuser((DSLContext ctx) -> ctx.fetchCount(CHUNK_ORPHANED_AT, where));
        return c;
    }

    @Test
    void afterAOneRowRename_theRowsTheChunkCascadeCarriedTakeTheTargetsId() {
        String a = registered("a");
        String t = registered("t");
        seed(a, "ca");
        long aId = fx.idOf(a);
        long tId = fx.idOf(t);
        String a2 = oneRowRename(a, "a2");
        assertThat(manifestRows(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(a))).as("setup: the manifest still names the old label").isEqualTo(1);

        Resp r = rehome(a2, t);

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("moved_chunks").asInt(-1)).as(r.body()).isEqualTo(2);
        assertThat(manifestRows(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(tId))).as("the manifest row follows its chunk").isEqualTo(1);
        assertThat(manifestRows(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(aId))).as("and none keeps the source's id").isZero();
        assertThat(assignments(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.eq(tId))).as("the topic assignment too").isEqualTo(1);
        assertThat(assignments(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.eq(aId))).isZero();
        assertThat(orphans(CHUNK_ORPHANED_AT.COLLECTION_ID.eq(tId))).as("and the orphaned-at record").isEqualTo(1);
        assertThat(orphans(CHUNK_ORPHANED_AT.COLLECTION_ID.eq(aId))).isZero();
    }

    @Test
    void theRowsOfAChunkACollisionLeftBehindKeepTheSourcesId() {
        String a = registered("lb-a");
        String t = registered("lb-t");
        seed(a, "lb");
        long aId = fx.idOf(a);
        long tId = fx.idOf(t);
        // the target already holds a chunk with the source's first chash, so that chunk cannot move
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunk384(ctx, fx.tenant, t, bytes(hex("lb1")), VectorSeeds.vec(384, 0.1f));
            return null;
        });
        String a2 = oneRowRename(a, "lb-a2");

        Resp r = rehome(a2, t);

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("left_behind_by_table").path("chunks").asInt(-1)).as(r.body()).isEqualTo(1);
        assertThat(manifestRows(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(aId))).as("the manifest row of the left-behind chunk stays").isEqualTo(1);
        assertThat(manifestRows(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(tId))).isZero();
        assertThat(assignments(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.eq(aId))).as("so does its topic assignment").isEqualTo(1);
        assertThat(orphans(CHUNK_ORPHANED_AT.COLLECTION_ID.eq(tId).and(CHUNK_ORPHANED_AT.CHASH.eq(bytes(hex("lb2")))))).as("the moved chunk's orphaned-at record follows it")
            .isEqualTo(1);
    }

    @Test
    void afterAOneRowRename_theModelGuardReadsTheSourcesChunksAndRegistryRowByTheirId() {
        String a = registered("m-a");
        seed(a, "cm");
        String other = label("cas-m-t-" + (n++)).replace("minilm-l6-v2-384", "bge-base-en-v15-768");
        fx.asSuperuser(ctx -> {
            dev.nexus.service.PgContainerHelper.insertCollection(ctx, fx.tenant, other, "bge-base-en-v15-768");
            return null;
        });
        String a2 = oneRowRename(a, "m-a2");

        Resp r = rehome(a2, other);

        assertThat(r.status()).as("the chunks still named a, found by a2's id: %s", r.body()).isEqualTo(409);
        assertThat(r.reason()).as(r.body()).isEqualTo("collection_model_mismatch");
    }
}
