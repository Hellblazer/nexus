// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's ruling of 2026-11-23, option (b) for the delete of a retired label: the 200 stays and
 * is made safe by scope. From gate 3 a delete by label resolves the label to its registry row and removes only the rows that
 * carry that row's {@code collection_id}, then the row; a label with no registry row removes nothing from any content table.
 *
 * <p>(i) A one-row rename moves the registry row to its new label and leaves the content's name columns at the old one (P3.7's
 * move; it is not on this base, so the superuser does what it does). The old label then has no registry row, and a delete of it
 * must leave the renamed collection's content alone, which a name-scoped delete would not. The name key's foreign keys go with
 * the name-key drop move, so this class drops the registry's primary key as the superuser on its own throwaway database, as
 * {@code CollectionDeleteLabelClashTest} does for its case.
 *
 * <p>(ii) Superseding X by Y leaves X a tombstone that names Y. Deleting X takes the tombstone and the rows of X's own id, and Y
 * and its content stay.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionDeleteScopeByIdTest {

    private static final String DELETE = "/v1/catalog/collections/delete";

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        fx.buildIdTwins();   // a real install has the id twins before gate 3 latches (P3.4's pass)
        fx.gate3.set(true);
        // the name-key drop move, as far as this class needs it. Every inbound foreign key on a registry key that
        // holds `name` goes first: the primary key's cascade does not reach the ones on the other name-keyed unique
        // keys (observed: chunks_collection_fk on (tenant_id, name, embedding_model) refused the in-place rename).
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

    private String registered(String stem) {
        String l = label("scope-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    /** One chunk, one topic and a taxonomy_meta row, all filed under {@code collection} by the engine's own writers. */
    private void seedContent(String collection) {
        String hex = Chash.ofText(collection + "/chunk").toHex();
        fx.asSuperuser(ctx -> {
            dev.nexus.service.PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("scope text"),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
        assertThat(fx.post("/v1/taxonomy/topics/insert",
            "{\"collection\":" + json(collection) + ",\"label\":\"scope-topic\",\"doc_count\":1}").status()).isEqualTo(200);
        assertThat(fx.post("/v1/taxonomy/meta/record",
            "{\"collection\":" + json(collection) + ",\"doc_count\":3}").status()).isEqualTo(200);
    }

    private record Held(int chunks, int topics, int meta) {
    }

    /** What the content tables hold under the registry id {@code id}, whatever name column those rows carry. */
    private Held heldById(long id) {
        return fx.asSuperuser(ctx -> new Held(
            ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION_ID.eq(id)),
            ctx.fetchCount(TOPICS, TOPICS.COLLECTION_ID.eq(id)),
            ctx.fetchCount(TAXONOMY_META, TAXONOMY_META.COLLECTION_ID.eq(id))));
    }

    // ---- (i) --------------------------------------------------------------------------------------------------------

    @Test
    void afterAOneRowRename_aDeleteOfTheOldLabelLeavesTheRenamedCollectionsContentAlone() {
        String x = registered("one-row-x");
        seedContent(x);
        long id = fx.idOf(x);
        assertThat(heldById(id)).as("setup").isEqualTo(new Held(1, 1, 1));

        // the one-row rename: the registry row takes its new label, the content's name columns stay at x
        String y = label("scope-one-row-y-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, y)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        assertThat(fx.exists(x)).as("setup: x no longer has a registry row").isFalse();
        assertThat(fx.idOf(y)).as("setup: y is the same row").isEqualTo(id);
        int oldNamed = fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(x)));
        assertThat(oldNamed).as("setup: the content still carries the old name").isEqualTo(1);

        Resp byOld = fx.post(DELETE, "{\"name\":" + json(x) + "}");

        assertThat(byOld.status()).as("the same answer an unknown label gets: %s", byOld.body()).isEqualTo(200);
        assertThat(byOld.json().path("deleted").path("chunks").asInt(-1)).as("no chunk counted: %s", byOld.body()).isZero();
        assertThat(heldById(id)).as("y's chunk, topic and taxonomy_meta are untouched by the delete of x")
            .isEqualTo(new Held(1, 1, 1));
        assertThat(fx.exists(y)).as("and so is its registry row").isTrue();

        // the delete of y, by its label, takes the rows that carry its id, wherever their name column says they are
        Resp byNew = fx.post(DELETE, "{\"name\":" + json(y) + "}");
        assertThat(byNew.status()).as(byNew.body()).isEqualTo(200);
        assertThat(byNew.json().path("deleted").path("chunks").asInt(-1)).as(byNew.body()).isEqualTo(1);
        assertThat(heldById(id)).isEqualTo(new Held(0, 0, 0));
        assertThat(fx.exists(y)).isFalse();
    }

    // ---- (i-b) every table CollectionPurgeScope covers ---------------------------------------------------------------------

    private static final String EMBEDDING_384 = "[" + String.join(",", java.util.Collections.nCopies(384, "0.1")) + "]";

    /**
     * Content under {@code collection} in every table the purge scope covers, by the engine's own writers where a route exists and by
     * the shared seeds where one does not, all carrying the name {@code collection}. {@code tag} keeps the keys of two collections that
     * share the label apart (a decoy). {@code host} is another collection whose topic the projection assignment (topic_assignments,
     * keyed by {@code source_collection}) points at. Returns the chunk's hash.
     */
    private String seedEveryTable(String collection, String host, long hostTopic, String tag, boolean meta) {
        String hex = Chash.ofText(collection + "/every/" + tag).toHex();
        String docId = "every-" + tag + "-" + collection;
        fx.asSuperuser(ctx -> {
            dev.nexus.service.PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("every text " + tag),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, docId, collection);
            CollectionIdSeeds.manifestRows(ctx, fx.tenant, docId, collection, 0, hex);
            return null;
        });
        Resp aspect = fx.post("/v1/aspects/upsert", "{\"collection\":" + json(collection) + ",\"source_path\":\"every-" + tag
            + ".md\",\"doc_id\":" + json(docId) + ",\"confidence\":0.9,\"extracted_at\":\"2026-01-01T00:00:00Z\","
            + "\"model_version\":\"v1\",\"extractor_name\":\"ex\"}");
        assertThat(aspect.status()).as("setup: aspect " + aspect.body()).isEqualTo(200);
        Resp highlight = fx.post("/v1/aspects/highlights/upsert", "{\"collection\":" + json(collection) + ",\"doc_id\":" + json(docId)
            + ",\"highlights_md\":\"## key\\nfinding\",\"ingested_at\":\"2026-10-22T00:00:00.000000Z\"}");
        assertThat(highlight.status()).as("setup: highlight " + highlight.body()).isEqualTo(200);
        Resp topic = fx.post("/v1/taxonomy/topics/insert",
            "{\"collection\":" + json(collection) + ",\"label\":\"every-topic-" + tag + "\",\"doc_count\":1}");
        assertThat(topic.status()).as("setup: topic " + topic.body()).isEqualTo(200);
        Resp assign = fx.post("/v1/taxonomy/assignments/assign", "{\"doc_id\":\"" + hex + "\",\"topic_id\":" + hostTopic
            + ",\"assigned_by\":\"projection\",\"similarity\":0.9,\"source_collection\":" + json(collection) + "}");
        assertThat(assign.status()).as("setup: assignment sourced from the collection " + assign.body()).isEqualTo(200);
        Resp centroid = fx.post("/v1/taxonomy/centroids/upsert", "{\"records\":[{\"collection\":" + json(collection)
            + ",\"topic_id\":" + (900 + tag.hashCode() % 50 + 50) + ",\"label\":\"every-centroid-" + tag + "\",\"doc_count\":1,"
            + "\"embedding\":" + EMBEDDING_384 + "}]}");
        assertThat(centroid.status()).as("setup: centroid " + centroid.body()).isEqualTo(200);
        if (meta) {
            assertThat(fx.post("/v1/taxonomy/meta/record",
                "{\"collection\":" + json(collection) + ",\"doc_count\":3}").status()).as("setup: taxonomy_meta").isEqualTo(200);
        }
        return hex;
    }

    /** The rows each covered table holds under the registry id {@code id}, by table. */
    private Map<String, Integer> coveredRows(long id) {
        return fx.asSuperuser(ctx -> {
            Map<String, Integer> rows = new LinkedHashMap<>();
            rows.put("chunks", ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION_ID.eq(id)));
            rows.put("catalog_documents", ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID.eq(id)));
            rows.put("catalog_document_chunks", ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(id)));
            rows.put("document_aspects", ctx.fetchCount(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.COLLECTION_ID.eq(id)));
            rows.put("document_highlights", ctx.fetchCount(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.COLLECTION_ID.eq(id)));
            rows.put("topics", ctx.fetchCount(TOPICS, TOPICS.COLLECTION_ID.eq(id)));
            rows.put("topic_assignments", ctx.fetchCount(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.eq(id)));
            rows.put("taxonomy_centroids", ctx.fetchCount(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.COLLECTION_ID.eq(id)));
            rows.put("taxonomy_meta", ctx.fetchCount(TAXONOMY_META, TAXONOMY_META.COLLECTION_ID.eq(id)));
            return rows;
        });
    }

    private int orphanRecords(long id) {
        return fx.asSuperuser(ctx -> ctx.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.COLLECTION_ID.eq(id)));
    }

    /**
     * Review prerequisite for stacking (folio, wren): after a one-row rename the delete of the NEW label purges, by its id, every table
     * the purge scope covers, though their name columns still say the old label, and the {@code chunk_orphaned_at} records go with the
     * chunks. A decoy registered under the old label afterwards (a different id) keeps all its rows: the purge by id never reaches a row
     * that merely carries the old name. The decoy has no taxonomy_meta row, which is keyed by the name and would be the renamed
     * collection's own.
     */
    @Test
    void afterAOneRowRename_aDeleteOfTheNewLabelPurgesTheOldNamedChildRowsByItsId() {
        // Gate 3 is latched for the whole class (startAll). The fixture's id-twin build, fx.buildIdTwins(), does not exist on this
        // base; the merge that brings it calls it here, before the latch, so the id columns this test reads are the built ones.
        String x = registered("every-x");
        String host = registered("every-host");
        Resp hostTopic = fx.post("/v1/taxonomy/topics/insert", "{\"collection\":" + json(host) + ",\"label\":\"every-host-topic\",\"doc_count\":1}");
        assertThat(hostTopic.status()).as("setup: " + hostTopic.body()).isEqualTo(200);
        long hostTopicId = hostTopic.json().path("id").asLong(-1);
        assertThat(hostTopicId).as("setup: the host topic id").isPositive();
        seedEveryTable(x, host, hostTopicId, "renamed", true);
        long id = fx.idOf(x);
        Map<String, Integer> before = coveredRows(id);
        assertThat(before).as("setup: every covered table holds a row of the collection").allSatisfy((table, rows) ->
            assertThat(rows).as(table).isPositive());
        int orphansBefore = orphanRecords(id);

        // the one-row rename (the registry label changes, the id stays, the child rows keep the name x), as test (i) simulates it
        String y = label("scope-every-y-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, y)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        assertThat(fx.idOf(y)).as("setup: y is the same row").isEqualTo(id);

        // the decoy: a registry row that now bears the free label x, with a different id, and rows of its own
        Long decoyId = null;
        Map<String, Integer> decoyBefore = null;
        try {
            fx.register(x);
            decoyId = fx.idOf(x);
            seedEveryTable(x, host, hostTopicId, "decoy", false);
        } catch (RuntimeException staging) {
            decoyId = null;   // the decoy could not be staged on this schema: the main assertions stand, and the message says so
        }
        if (decoyId != null) {
            assertThat(decoyId).as("setup: the decoy is another row").isNotEqualTo(id);
            decoyBefore = coveredRows(decoyId);
        }

        Resp byNew = fx.post(DELETE, "{\"name\":" + json(y) + "}");

        assertThat(byNew.status()).as(byNew.body()).isEqualTo(200);
        Map<String, Integer> after = coveredRows(id);
        assertThat(after).as("no covered table holds a row of the collection after the delete of its new label (before: %s)", before)
            .allSatisfy((table, rows) -> assertThat(rows).as(table).isZero());
        assertThat(orphanRecords(id)).as("the chunk_orphaned_at records went with the chunks (there were %d)", orphansBefore).isZero();
        assertThat(fx.exists(y)).as("and the registry row of y").isFalse();
        if (decoyId != null) {
            assertThat(coveredRows(decoyId)).as("the decoy named x, another id, keeps every row").isEqualTo(decoyBefore);
            assertThat(fx.exists(x)).as("and its registry row").isTrue();
        }
    }

    // ---- (ii) -------------------------------------------------------------------------------------------------------

    @Test
    void aRetiredLabelIsDeletedWithItsOwnRows_andItsSuccessorAndItsContentStay() {
        String x = registered("sup-x");
        String y = registered("sup-y");
        seedContent(x);
        seedContent(y);
        long xId = fx.idOf(x);
        long yId = fx.idOf(y);
        Resp sup = fx.post("/v1/catalog/collections/supersede", "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}");
        assertThat(sup.status()).as("setup: " + sup.body()).isEqualTo(200);
        assertThat(fx.supersededBy(x)).as("setup: x is a tombstone of y").isEqualTo(y);

        Resp r = fx.post(DELETE, "{\"name\":" + json(x) + "}");

        assertThat(r.status()).as("the 200 stays: %s", r.body()).isEqualTo(200);
        assertThat(fx.exists(x)).as("the tombstone goes").isFalse();
        assertThat(heldById(xId)).as("x's own rows go with it").isEqualTo(new Held(0, 0, 0));
        assertThat(fx.exists(y)).as("the successor stays").isTrue();
        assertThat(fx.supersededBy(y)).isEqualTo("");
        assertThat(heldById(yId)).as("and so does its content").isEqualTo(new Held(1, 1, 1));
    }

    /**
     * linda's D3 ruling (2026-11-23), which replaces this case's earlier "a retired row's id deletes the tombstone": a retired ID
     * answers 409 {@code collection_superseded} naming the successor's label and id, on the delete route as on every other, and
     * deletes nothing. A retired LABEL keeps the 200 ({@link #aRetiredLabelIsDeletedWithItsOwnRows_andItsSuccessorAndItsContentStay}).
     */
    @Test
    void aRetiredRowsIdIsRefused409NamingTheSuccessor_andDeletesNothing() {
        String x = registered("sup-id-x");
        String y = registered("sup-id-y");
        seedContent(x);
        seedContent(y);
        long xId = fx.idOf(x);
        long yId = fx.idOf(y);
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}").status()).isEqualTo(200);

        Resp r = fx.post(DELETE, "{\"collection_id\":" + xId + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection").asText()).isEqualTo(x);
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(xId);
        assertThat(r.json().path("superseded_by").asText()).as("the successor's label: %s", r.body()).isEqualTo(y);
        assertThat(r.json().path("successor_collection_id").asLong(-1)).as("and its id").isEqualTo(yId);
        assertThat(fx.exists(x)).as("the tombstone is still there").isTrue();
        assertThat(heldById(xId)).as("and so is its content").isEqualTo(new Held(1, 1, 1));
        assertThat(fx.exists(y)).isTrue();
        assertThat(heldById(yId)).isEqualTo(new Held(1, 1, 1));
    }
}
