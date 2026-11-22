// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.CollectionRouteFixture.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.CollectionRouteFixture.TENANT;
import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a write half (nexus-p2m1x.23), delete contract point 5: "a label held by more than one live row -> refusal
 * naming the ids (cannot happen before P4.1; bob tests it on a stub)."
 *
 * <p>Before P4.1 the registry's {@code (tenant_id, name)} primary key keeps a label to one row, so the case is made here on
 * this class's own throwaway database, where the primary key and the live-label partial index are dropped as the superuser
 * and a second live row is inserted under the same label with a different embedding model (so the model-keyed UNIQUE still
 * holds). It proves the engine's route refuses an ambiguous label by naming every id, and that the id path still reaches
 * exactly one row: the by-id delete is how an operator clears such a clash, which is what point 5 is for. It is not a claim
 * that a clash occurs before P4.1.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionDeleteLabelClashTest {

    private static final String DELETE = "/v1/catalog/collections/delete";

    /** The typed refusal's {@code reason} and the field that lists the ids (point 5: "naming the ids"). */
    private static final String AMBIGUOUS_REASON = "collection_label_ambiguous";
    private static final String IDS = "collection_ids";

    CollectionRouteFixture fx;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private long rowsLabelled(String label) {
        return fx.asSuperuser(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS,
            CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(label))));
    }

    private List<Long> idsLabelled(String label) {
        return fx.asSuperuser(ctx -> new ArrayList<>(ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID).fetch(CATALOG_COLLECTIONS.COLLECTION_ID)));
    }

    @Test
    void point5_aLabelHeldByMoreThanOneLiveRowIsRefusedNamingTheIds_andTheIdPathReachesOneRow() {
        String clash = label("clash-x");
        fx.register(clash);
        fx.asSuperuser(ctx -> {
            ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(CATALOG_COLLECTIONS.getPrimaryKey().getName()).cascade().execute();
            // Schema-qualified: the superuser connection's search_path does not include nexus, so an unqualified
            // DROP INDEX IF EXISTS finds nothing, succeeds silently, and the live-label index keeps refusing the twin.
            // (jOOQ DSL, not a string statement: the raw-SQL gate counts those.)
            ctx.dropIndexIfExists(org.jooq.impl.DSL.name("nexus", "idx_catalog_collections_live_label")).execute();
            Object stillThere = ctx.select(org.jooq.impl.DSL.function("to_regclass", Object.class,
                    org.jooq.impl.DSL.inline("nexus.idx_catalog_collections_live_label"))).fetchOne(0);
            org.assertj.core.api.Assertions.assertThat(stillThere)
                .as("setup: the live-label index is dropped before the twin insert (rendered: %s)",
                    ctx.renderInlined(ctx.dropIndexIfExists(org.jooq.impl.DSL.name("nexus", "idx_catalog_collections_live_label"))))
                .isNull();
            ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                    CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                    CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                .values(TENANT, clash, "docs", "clash-x-twin", "bge-base-en-v15-768", "live").execute();
            return null;
        });
        List<Long> ids = idsLabelled(clash);
        assertThat(ids).as("setup: two live rows hold one label").hasSize(2);
        assertThat(ids.get(0)).isNotEqualTo(ids.get(1));

        Resp r = fx.post(DELETE, "{\"name\":" + json(clash) + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).as("typed: %s", r.body()).isEqualTo(AMBIGUOUS_REASON);
        JsonNode named = r.json().path(IDS);
        assertThat(named.isArray()).as("the ids are listed: %s", r.body()).isTrue();
        List<Long> got = new ArrayList<>();
        named.forEach(j -> got.add(j.asLong()));
        assertThat(got).as("every live row's id is named: %s", r.body()).containsExactlyInAnyOrderElementsOf(ids);
        assertThat(rowsLabelled(clash)).as("a refused delete removes neither row").isEqualTo(2);

        // The id path is the way out: it names one row and deletes only it.
        Resp byId = fx.post(DELETE, "{\"collection_id\":" + ids.get(0) + "}");
        assertThat(byId.status()).as(byId.body()).isEqualTo(200);
        assertThat(byId.json().path("collection_id").asLong(-1)).isEqualTo(ids.get(0));
        assertThat(idsLabelled(clash)).as("only that row went").containsExactly(ids.get(1));
    }
}
