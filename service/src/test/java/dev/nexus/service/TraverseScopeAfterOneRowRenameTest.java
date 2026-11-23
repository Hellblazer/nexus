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

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's follow-up to the census: the traverse route's {@code scope_filter.collections} stays a list
 * of labels on the wire, but what it matches must not be a stored name from gate 3. After a one-row rename a document keeps its old
 * {@code physical_collection}, so a filter that names the new label and is compared with that column misses the renamed
 * collection's documents. From gate 3 the engine resolves each label to its registry id and matches the document's
 * {@code physical_collection_id}; before it, the name match is unchanged.
 *
 * <p>The one-row rename is simulated as {@code CollectionDeleteScopeByIdTest} does it (P3.7's move is not on this base): the registry
 * row takes the new label, the documents keep the old one.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TraverseScopeAfterOneRowRenameTest {

    CollectionRouteFixture fx;

    @BeforeAll
    void start() throws Exception {
        fx = new CollectionRouteFixture();
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
    void stop() {
        if (fx != null) fx.close();
    }

    private String registerDoc(String ownerPrefix, String title, String collection) {
        Resp r = fx.post("/v1/catalog/doc/register", "{\"owner_prefix\":\"" + ownerPrefix + "\",\"title\":" + json(title)
            + ",\"content_type\":\"knowledge\",\"source_uri\":\"file:///" + ownerPrefix + "/" + title + ".md\","
            + "\"physical_collection\":" + json(collection) + "}");
        assertThat(r.status()).as("setup: register %s: %s", title, r.body()).isEqualTo(200);
        return r.json().path("tumbler").asText();
    }

    private List<String> traversed(String seed, String... labels) {
        List<String> quoted = new ArrayList<>();
        for (String l : labels) {
            quoted.add(json(l));
        }
        Resp r = fx.post("/v1/catalog/traverse", "{\"seeds\":[" + json(seed) + "],\"link_types\":[\"cites\"],\"direction\":\"out\","
            + "\"depth\":1,\"scope_filter\":{\"collections\":[" + String.join(",", quoted) + "]}}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        List<String> nodes = new ArrayList<>();
        for (JsonNode n : r.json().path("nodes")) {
            nodes.add(n.path("tumbler").asText());
        }
        return nodes;
    }

    @Test
    void aScopeNamingTheNewLabelFindsTheRenamedCollectionsDocumentsFromGate3() {
        String home = label("trav-home");
        String x = label("trav-x");
        fx.register(home, x);
        String seed = registerDoc("travr.1", "seed", home);
        String doc = registerDoc("travr.1", "in-x", x);
        Resp link = fx.post("/v1/catalog/link",
            "{\"from_tumbler\":" + json(seed) + ",\"to_tumbler\":" + json(doc) + ",\"link_type\":\"cites\",\"created_by\":\"test\"}");
        assertThat(link.status()).as(link.body()).isEqualTo(200);
        long id = fx.idOf(x);

        fx.gate3.set(true);
        assertThat(traversed(seed, x)).as("setup: before the rename the label and the id agree").containsExactlyInAnyOrder(seed, doc);

        // the one-row rename: the registry row takes the new label, the document keeps x
        String y = label("trav-y");
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, y)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        // P3.7 evicts the old label from the registry cache with its rename; the simulation does it by hand
        dev.nexus.service.db.CollectionRegistry.shared().evictLabel(fx.tenant, x);

        assertThat(traversed(seed, y)).as("gate 3: the new label finds the renamed collection's document by its id")
            .containsExactlyInAnyOrder(seed, doc);
        assertThat(traversed(seed, x)).as("gate 3: the old label names no registry row, so it admits no collection")
            .containsExactly(seed);

        fx.gate3.set(false);
        assertThat(traversed(seed, x)).as("gate 3 off: the name match, as before").containsExactlyInAnyOrder(seed, doc);
        assertThat(traversed(seed, y)).as("gate 3 off: the stored name is still x").containsExactly(seed);
    }
}
