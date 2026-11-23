// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's ruling R-B: a re-home stays keyed by id (RDR-229 section "what this replaces"). Before
 * gate 3 it is keyed by the name columns, as it always was; from gate 3 the source and target labels resolve to their registry
 * rows and every reader and writer of the re-home (the chunk read, the collision predicate, the mover, the status counts) is
 * scoped by {@code collection_id}.
 *
 * <p>The one-row rename (P3.7) is not on this base, so the superuser does what it does, as {@code CollectionDeleteScopeByIdTest}
 * does: every inbound foreign key on a name-bearing registry key and the registry's primary key go as the superuser on this
 * class's own throwaway database, the registry row takes its new label in place, and the content's name columns stay at the old
 * one.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRehomeByIdTest {

    private static final String REHOME = "/v1/catalog/collections/rehome";
    private static final String STATUS = "/v1/catalog/collections/rehome/status";

    CollectionRouteFixture fx;
    int n;

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

    private String registered(String stem) {
        String l = label("rhid-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    /** One chunk, one topic and, when asked, a taxonomy_meta row, filed under {@code collection} by the engine's own writers. */
    private void seedContent(String collection, boolean withMeta) {
        String hex = Chash.ofText(collection + "/chunk").toHex();
        fx.asSuperuser(ctx -> {
            dev.nexus.service.PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("rehome text"),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
        assertThat(fx.post("/v1/taxonomy/topics/insert",
            "{\"collection\":" + json(collection) + ",\"label\":" + json("topic-" + collection) + ",\"doc_count\":1}").status())
            .isEqualTo(200);
        if (withMeta) {
            assertThat(fx.post("/v1/taxonomy/meta/record",
                "{\"collection\":" + json(collection) + ",\"doc_count\":3}").status()).isEqualTo(200);
        }
    }

    /** The chunk rows matching {@code where}, as a plain int (a typed local, so AssertJ's assertThat is not ambiguous). */
    private int chunkCount(org.jooq.Condition where) {
        int count = fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, where));
        return count;
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

    /** The one-row rename: the registry row takes its new label in place; the content's name columns stay as they are. */
    private String oneRowRename(String from, String stem) {
        long id = fx.idOf(from);
        String to = label("rhid-" + stem + "-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        assertThat(fx.idOf(to)).as("setup: the renamed row is the same row").isEqualTo(id);
        assertThat(fx.exists(from)).as("setup: the old label has no registry row").isFalse();
        return to;
    }

    private String body(String source, String target) {
        return "{\"source\":" + json(source) + ",\"target\":" + json(target) + "}";
    }

    // ---- (i) --------------------------------------------------------------------------------------------------------

    @Test
    void afterAOneRowRename_aRehomeNamingTheNewLabelMovesTheRowsThatStillCarryTheOldName() {
        String a = registered("i-a");
        String t = registered("i-t");
        seedContent(a, true);
        long aId = fx.idOf(a);
        long tId = fx.idOf(t);
        assertThat(heldById(aId)).as("setup").isEqualTo(new Held(1, 1, 1));
        String a2 = oneRowRename(a, "i-a2");
        assertThat(chunkCount(CHUNKS.COLLECTION.eq(a)))
            .as("setup: the content still carries the old name").isEqualTo(1);
        assertThat(chunkCount(CHUNKS.COLLECTION.eq(a2)))
            .as("setup: nothing carries the new label").isZero();

        Resp before = fx.post(STATUS, body(a2, t));
        assertThat(before.status()).as(before.body()).isEqualTo(200);
        assertThat(before.json().path("remaining_chunks").asInt(-1)).as("status by id: %s", before.body()).isEqualTo(1);
        assertThat(before.json().path("remaining_rows").asInt(-1)).as(before.body()).isEqualTo(3);
        assertThat(before.json().path("done").asBoolean()).as(before.body()).isFalse();
        Resp byOld = fx.post(STATUS, body(a, t));
        assertThat(byOld.json().path("remaining_rows").asInt(-1))
            .as("the old label has no registry row, so the status of an unknown collection: %s", byOld.body()).isZero();

        Resp r = fx.post(REHOME, body(a2, t));

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("moved_chunks").asInt(-1)).as("the rows still named a are found by a2's id: %s", r.body()).isEqualTo(1);
        assertThat(r.json().path("moved_by_table").path("topics").asInt(-1)).as(r.body()).isEqualTo(1);
        assertThat(r.json().path("moved_by_table").path("taxonomy_meta").asInt(-1)).as(r.body()).isEqualTo(1);
        assertThat(r.json().path("remaining_rows").asInt(-1)).as(r.body()).isZero();
        assertThat(r.json().path("done").asBoolean()).as(r.body()).isTrue();
        assertThat(heldById(aId)).as("nothing is left under the source's id").isEqualTo(new Held(0, 0, 0));
        assertThat(heldById(tId)).as("it all carries the target's id").isEqualTo(new Held(1, 1, 1));
        assertThat(chunkCount(CHUNKS.COLLECTION_ID.eq(tId).and(CHUNKS.COLLECTION.eq(t))))
            .as("and the target's label in the name column, as a re-home always wrote it").isEqualTo(1);
        Resp after = fx.post(STATUS, body(a2, t));
        assertThat(after.json().path("remaining_rows").asInt(-1)).as(after.body()).isZero();
        assertThat(after.json().path("done").asBoolean()).as(after.body()).isTrue();
    }

    @Test
    void afterAOneRowRename_theOldLabelIsAnUnknownCollectionToARehome() {
        String a = registered("i-old-a");
        String t = registered("i-old-t");
        seedContent(a, false);
        long aId = fx.idOf(a);
        oneRowRename(a, "i-old-a2");

        Resp r = fx.post(REHOME, body(a, t));

        assertThat(r.status()).as("the 409 an unregistered source gets today: %s", r.body()).isEqualTo(409);
        assertThat(r.body()).contains("source collection");
        assertThat(heldById(aId)).as("nothing moved").isEqualTo(new Held(1, 1, 0));
    }

    // ---- (ii) -------------------------------------------------------------------------------------------------------

    @Test
    void ontoARenamedTarget_theTargetKeepsOneId_andTheMovedRowsCarryIt() {
        String s = registered("ii-s");
        String t = registered("ii-t");
        seedContent(s, false);
        seedContent(t, false);
        long sId = fx.idOf(s);
        long tId = fx.idOf(t);
        String t2 = oneRowRename(t, "ii-t2");

        Resp r = fx.post(REHOME, body(s, t2));

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("moved_chunks").asInt(-1)).as(r.body()).isEqualTo(1);
        assertThat(r.json().path("done").asBoolean()).as(r.body()).isTrue();
        assertThat(fx.idOf(t2)).as("the target's id is unchanged").isEqualTo(tId);
        assertThat(heldById(sId)).isEqualTo(new Held(0, 0, 0));
        assertThat(heldById(tId)).as("its own chunk and topic and the moved ones, all under its one id").isEqualTo(new Held(2, 2, 0));
        assertThat(chunkCount(CHUNKS.COLLECTION_ID.eq(tId).and(CHUNKS.COLLECTION.eq(t2))))
            .as("the moved chunk names the target's current label").isEqualTo(1);
    }

    @Test
    void aRowThatWouldCollideWithTheRenamedTargetsOwnRowIsLeftBehind_byTheTargetsId() {
        String s = registered("ii-c-s");
        String t = registered("ii-c-t");
        seedContent(s, true);
        seedContent(t, true);
        long sId = fx.idOf(s);
        long tId = fx.idOf(t);
        String t2 = oneRowRename(t, "ii-c-t2");

        Resp status = fx.post(STATUS, body(s, t2));
        assertThat(status.json().path("remaining_rows").asInt(-1)).as(status.body()).isEqualTo(3);
        assertThat(status.json().path("remaining_movable_rows").asInt(-1))
            .as("the source's taxonomy_meta row would take the key the target's own holds, so it is stuck: %s", status.body())
            .isEqualTo(2);

        Resp r = fx.post(REHOME, body(s, t2));

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("left_behind_by_table").path("taxonomy_meta").asInt(-1)).as(r.body()).isEqualTo(1);
        assertThat(r.json().path("moved_by_table").path("taxonomy_meta").asInt(-1)).as(r.body()).isZero();
        assertThat(r.json().path("remaining_rows").asInt(-1)).as(r.body()).isEqualTo(1);
        assertThat(r.json().path("done").asBoolean()).as("the parked row is finished business: %s", r.body()).isTrue();
        assertThat(heldById(sId)).as("only the stuck meta row stays at the source").isEqualTo(new Held(0, 0, 1));
        assertThat(heldById(tId)).isEqualTo(new Held(2, 2, 1));
    }

    // ---- (iii) ------------------------------------------------------------------------------------------------------

    @Test
    void beforeGate3_aRehomeIsKeyedByTheNameColumnsAsItAlwaysWas() {
        fx.gate3.set(false);
        try {
            String a = registered("iii-a");
            String t = registered("iii-t");
            seedContent(a, true);
            long aId = fx.idOf(a);
            long tId = fx.idOf(t);

            // a one-row rename leaves the content under the old name; the name-keyed re-home naming the new label finds no row
            String a2 = oneRowRename(a, "iii-a2");
            Resp byNew = fx.post(REHOME, body(a2, t));
            assertThat(byNew.status()).as(byNew.body()).isEqualTo(200);
            assertThat(byNew.json().path("moved_chunks").asInt(-1))
                .as("before gate 3 the name columns key it, and none carries a2: %s", byNew.body()).isZero();
            assertThat(heldById(aId)).as("nothing moved").isEqualTo(new Held(1, 1, 1));

            // and an unrenamed source moves by name, rows carrying the target's name and id
            String b = registered("iii-b");
            String t2 = registered("iii-t2");
            seedContent(b, false);
            long bId = fx.idOf(b);
            long t2Id = fx.idOf(t2);
            Resp byName = fx.post(REHOME, body(b, t2));
            assertThat(byName.status()).as(byName.body()).isEqualTo(200);
            assertThat(byName.json().path("moved_chunks").asInt(-1)).as(byName.body()).isEqualTo(1);
            assertThat(byName.json().path("done").asBoolean()).as(byName.body()).isTrue();
            assertThat(heldById(bId)).isEqualTo(new Held(0, 0, 0));
            assertThat(heldById(t2Id)).isEqualTo(new Held(1, 1, 0));
            assertThat(heldById(tId)).as("the other target is untouched").isEqualTo(new Held(0, 0, 0));
        } finally {
            fx.gate3.set(true);
        }
    }
}
