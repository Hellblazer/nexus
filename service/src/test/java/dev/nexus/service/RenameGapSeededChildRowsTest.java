// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, review of de481230b6, item 5): the rename-gap with the collection's OWN child rows already stored. The
 * route-table gap test files nothing before the rename, so it cannot see a writer that stamps the current label but finds the collection's
 * existing child rows by NAME: after a one-row rename those rows still bear the old label, and a match by the current label misses them
 * (a REPLACE leaves them behind, a "skip if topics exist" guard runs again, a centroid upsert meets the leaf's id unique index).
 *
 * <p>The one-row rename (P3.7) is not on this base, so the superuser does what it does, as {@code CollectionRehomeByIdTest} does: every inbound
 * foreign key on a name-bearing registry key goes on this class's own throwaway database, the registry row takes its new label in place and the
 * content's name columns stay at the old one. Each test stores rows under the old label, renames, writes by id, and reads the rows by id.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RenameGapSeededChildRowsTest {

    private static final String EMBEDDING = "[" + String.join(",", java.util.Collections.nCopies(384, "0.1")) + "]";

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        fx.buildIdTwins();   // a real install has the id twins before the rename moves land
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
        String l = label("rgs-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    /** The one-row rename: the registry row takes its new label in place; the content's name columns stay as they are. */
    private String oneRowRename(String from) {
        long id = fx.idOf(from);
        String to = label("rgs-renamed-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        assertThat(fx.idOf(to)).as("setup: the renamed row is the same row").isEqualTo(id);
        return to;
    }

    private long seedTopic(String collection, String topicLabel) {
        Resp r = fx.post("/v1/taxonomy/topics/insert", "{\"collection\":" + json(collection) + ",\"label\":" + json(topicLabel) + ",\"doc_count\":1}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
        return r.json().path("id").asLong(-1);
    }

    private List<String> topicLabelsOf(long id) {
        return fx.asSuperuser(ctx -> ctx.select(TOPICS.LABEL).from(TOPICS).where(TOPICS.COLLECTION_ID.eq(id)).orderBy(TOPICS.LABEL).fetch(TOPICS.LABEL));
    }

    private List<String> topicNamesOf(long id) {
        return fx.asSuperuser(ctx -> ctx.selectDistinct(TOPICS.COLLECTION).from(TOPICS).where(TOPICS.COLLECTION_ID.eq(id)).fetch(TOPICS.COLLECTION));
    }

    @Test
    void persistRebuild_byAnIdWhoseRowWasRenamed_replacesTheCollectionsOwnTopics_notLeavingTheOldOnesBehind() {
        String b = registered("rebuild");
        long x = fx.idOf(b);
        seedTopic(b, "seed-topic");
        assertThat(topicLabelsOf(x)).as("setup: one stored topic").containsExactly("seed-topic");
        String c = oneRowRename(b);

        Resp r = fx.post("/v1/taxonomy/topics/persist_rebuild",
            "{\"collection_id\":" + x + ",\"specs\":[{\"label\":\"fresh-topic\",\"doc_ids\":[]}]}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(topicLabelsOf(x)).as("the REPLACE removed the collection's own old topic (found by its id) and filed the new one")
            .containsExactly("fresh-topic");
        assertThat(topicNamesOf(x)).as("under the row's current label").containsExactly(c);
    }

    @Test
    void persistDiscovered_byAnIdWhoseRowWasRenamed_seesItsOwnExistingTopics_andSkips() {
        String b = registered("discovered");
        long x = fx.idOf(b);
        seedTopic(b, "seed-topic");
        oneRowRename(b);

        Resp r = fx.post("/v1/taxonomy/topics/persist_discovered",
            "{\"collection_id\":" + x + ",\"specs\":[{\"label\":\"discovered-topic\",\"doc_ids\":[]}]}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("topic_ids")).as("the existing-topics guard saw the collection's own topic by its id: nothing discovered").isEmpty();
        assertThat(topicLabelsOf(x)).containsExactly("seed-topic");
    }

    @Test
    void centroidUpsert_byAnIdWhoseRowWasRenamed_replacesTheCollectionsOwnCentroid_underTheCurrentLabel() {
        String b = registered("centroid");
        long x = fx.idOf(b);
        long topic = seedTopic(b, "seed-topic");
        String body = "\"records\":[{\"collection\":%s,\"topic_id\":" + topic + ",\"label\":\"c\",\"doc_count\":%d,\"embedding\":" + EMBEDDING + "}]";
        Resp seeded = fx.post("/v1/taxonomy/centroids/upsert", "{" + String.format(body, json(b), 1) + "}");
        assertThat(seeded.status()).as("setup: " + seeded.body()).isEqualTo(200);
        String c = oneRowRename(b);

        Resp r = fx.post("/v1/taxonomy/centroids/upsert",
            "{" + String.format(body.replace("\"collection\":%s", "\"collection_id\":" + x), 7) + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        var t = TAXONOMY_CENTROIDS;
        List<org.jooq.Record2<String, Integer>> rows = fx.asSuperuser(ctx -> ctx.select(t.COLLECTION, t.DOC_COUNT).from(t)
            .where(t.COLLECTION_ID.eq(x).and(t.TOPIC_ID.eq(topic))).fetch());
        assertThat(rows).as("one centroid of the topic, not the old one beside a new one").hasSize(1);
        assertThat(rows.get(0).value1()).as("under the row's current label").isEqualTo(c);
        assertThat(rows.get(0).value2()).as("with the new count").isEqualTo(7);
    }
}
