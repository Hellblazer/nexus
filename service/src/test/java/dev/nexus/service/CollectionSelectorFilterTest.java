// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.CollectionRouteFixture.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): what the route boundary ({@code CollectionSelectorFilter}) does beyond the per-route checks of
 * {@link CollectionRoutesByIdTest}: a client cannot hand the delete route an id the boundary did not resolve, an id in a query
 * string keeps the other parameters, and a rename by id answers like a rename by label.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionSelectorFilterTest {

    private static final String DELETE = "/v1/catalog/collections/delete";

    CollectionRouteFixture fx;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    /**
     * The delete route reads the id the boundary resolved from a private body field. A client that sends that field itself, plain
     * or with an escaped key (which a byte search would miss), gets it removed: the request is a delete by label, of the label it
     * names, and never of another row.
     */
    @Test
    void aClientSuppliedResolvedIdOnTheDeleteRouteIsIgnored() {
        for (String key : new String[] {"resolved_id", "resolved\\u005fid"}) {
            String target = label("sel-target-" + key.length());
            String other = label("sel-other-" + key.length());
            fx.register(target, other);
            long targetId = fx.idOf(target);
            long otherId = fx.idOf(other);

            Resp r = fx.post(DELETE, "{\"name\":" + json(target) + ",\"" + key + "\":" + otherId + "}");

            assertThat(r.status()).as("key %s: %s", key, r.body()).isEqualTo(200);
            assertThat(r.json().path("collection_id").asLong(-1)).as("the row it deleted is the label's").isEqualTo(targetId);
            assertThat(fx.exists(target)).isFalse();
            assertThat(fx.exists(other)).as("the other collection is untouched").isTrue();
        }
    }

    /** An id in a query string is replaced by the label and the other parameters stay, in the handler's own reading of them. */
    @Test
    void anIdInAQueryStringKeepsTheOtherParameters() {
        String x = label("sel-query");
        fx.register(x);
        long id = fx.idOf(x);
        assertThat(fx.post("/v1/taxonomy/topics/insert",
            "{\"collection\":" + json(x) + ",\"label\":\"p35a-topic\",\"doc_count\":1}").status()).isEqualTo(200);

        Resp byId = fx.get("/v1/taxonomy/topics/resolve?label=p35a-topic&collection_id=" + id);
        Resp byLabel = fx.get("/v1/taxonomy/topics/resolve?label=p35a-topic&collection="
            + java.net.URLEncoder.encode(x, java.nio.charset.StandardCharsets.UTF_8));

        assertThat(byId.status()).as(byId.body()).isEqualTo(200);
        assertThat(byId.body()).as("the same topic by id as by label").isEqualTo(byLabel.body());
    }

    /** A request that names only a label is untouched: the same body reaches the handler and the same answer comes back. */
    @Test
    void aRequestWithOnlyALabelIsAnsweredAsBefore() {
        String x = label("sel-label-only");
        fx.register(x);
        long id = fx.idOf(x);
        Resp r = fx.get("/v1/catalog/collections/get?name=" + java.net.URLEncoder.encode(x, java.nio.charset.StandardCharsets.UTF_8));
        assertThat(r.status()).isEqualTo(200);
        JsonNode j = r.json();
        assertThat(j.path("name").asText()).isEqualTo(x);
        assertThat(j.path("collection_id").asLong()).isEqualTo(id);
    }

    /** A rename by id answers with the renamed row, as a rename by label does. */
    @Test
    void aRenameByIdAnswersWithTheRenamedRow() {
        String from = label("sel-ren-from");
        String to = label("sel-ren-to");
        fx.register(from);
        long oldId = fx.idOf(from);
        Resp r = fx.post("/v1/catalog/collections/rename", "{\"collection_id\":" + oldId + ",\"new_name\":" + json(to) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("name").asText()).isEqualTo(to);
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(fx.idOf(to)).isNotEqualTo(oldId);
        assertThat(r.json().path("ids_stable").asBoolean(true)).isFalse();
    }
}
