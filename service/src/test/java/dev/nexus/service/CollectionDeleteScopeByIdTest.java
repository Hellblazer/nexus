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
        fx.gate3.set(true);
        // the name-key drop move, as far as this class needs it: the name keys' foreign keys depend on the primary key
        fx.asSuperuser(ctx -> {
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
        assertThat(fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(x))))
            .as("setup: the content still carries the old name").isEqualTo(1);

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

    @Test
    void aRetiredRowsIdDeletesItToo() {
        String x = registered("sup-id-x");
        String y = registered("sup-id-y");
        seedContent(y);
        long xId = fx.idOf(x);
        long yId = fx.idOf(y);
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}").status()).isEqualTo(200);

        Resp r = fx.post(DELETE, "{\"collection_id\":" + xId + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(xId);
        assertThat(fx.exists(x)).isFalse();
        assertThat(fx.exists(y)).isTrue();
        assertThat(heldById(yId)).isEqualTo(new Held(1, 1, 1));
    }
}
