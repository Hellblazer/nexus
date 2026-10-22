// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-i15oc: the taxonomy writers refuse a collection name whose catalog row carries
 * {@code superseded_by} with the same typed 409 {@code collection_superseded} that nexus-mp8ys gave the
 * chunk, manifest and centroid writers. Plain {@code requireRegistered} accepted the tombstone and
 * stranded topics, assignments and meta rows under a name nothing routes to.
 *
 * <p>Each test warms the registry with a LIVE write first (the control for that route), retires the name
 * through {@code CatalogRepository#supersedeCollection} (which must evict the cached row), then repeats
 * the write and demands the refusal body. {@code assertRefusal} checks the exact 409 body, so a writer
 * reverted to the plain check fails whether it stores the row (200) or trips an unrelated constraint
 * (a different 409 body or a 5xx).
 *
 * <p>The import verbs and the rename's re-home stay plain on purpose (fidelity import and rename ordering);
 * {@link #importMeta_ontoATombstone_isStillAccepted} pins that, and {@link #unassigned_readOfARetiredName_isStillServed}
 * pins the read side.
 */
class TaxonomySupersededWriteTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String MODEL_SEG = "__minilm-l6-v2-384__";

    private TaxonomyHandler handler;

    @BeforeAll
    void wireHandler() {
        handler = new TaxonomyHandler(new TaxonomyRepository(tenantScope), new TaxonomyCentroidRepository(tenantScope));
    }

    private record Resp(int status, String body) {
        JsonNode json() throws Exception {
            return JSON.readTree(body);
        }
    }

    private Resp send(String method, String path, Map<String, Object> body) throws Exception {
        CapturingExchange ex = new CapturingExchange(method, URI.create(path), JSON.writeValueAsString(body));
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Resp(ex.status, ex.bodyString());
    }

    private Resp post(String op, Map<String, Object> body) throws Exception {
        return send("POST", "/v1/taxonomy" + op, body);
    }

    /** A live collection with one topic already written through the route (warms the registry cache). */
    private record Live(String collection, long topicId) {}

    private Live liveWithTopic(String tag) throws Exception {
        String coll = fixture(tag).collection();
        assertThat(coll).contains(MODEL_SEG);
        Resp r = post("/topics/insert", Map.of("label", "warm-" + coll, "collection", coll, "doc_count", 0));
        assertThat(r.status()).as("the live insert that warms the cache: %s", r.body()).isEqualTo(200);
        return new Live(coll, r.json().get("id").asLong());
    }

    private String retire(Live l) {
        String successor = l.collection().replace("__v1", "__v2");
        assertThat(repo.supersedeCollection(TENANT, l.collection(), successor, "")).isEqualTo(1);
        return successor;
    }

    private void assertRefusal(Resp r, String collection, String successor) throws Exception {
        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(409);
        JsonNode j = r.json();
        assertThat(j.get("error").asText()).isEqualTo("collection_superseded");
        assertThat(j.get("reason").asText()).isEqualTo("collection_superseded");
        assertThat(j.get("collection").asText()).isEqualTo(collection);
        assertThat(j.get("superseded_by").asText()).isEqualTo(successor);
        assertThat(j.get("message").asText()).doesNotContain("is not registered").doesNotContain("register it first");
    }

    private long topicCount(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return DSL.using(su, SQLDialect.POSTGRES)
                .selectCount().from(TOPICS)
                .where(TOPICS.TENANT_ID.eq(TENANT)).and(TOPICS.COLLECTION.eq(collection))
                .fetchOne(0, Long.class);
        }
    }

    private static Map<String, Object> spec(String label) {
        Map<String, Object> s = new LinkedHashMap<>();
        s.put("label", label);
        s.put("doc_count", 0);
        s.put("created_at", "2026-10-22T00:00:00Z");
        s.put("terms_json", "[]");
        s.put("doc_ids", List.of());
        return s;
    }

    // ── refusals ────────────────────────────────────────────────────────────────

    @Test
    void insertTopic_intoARetiredName_is409NamingTheSuccessor_andStoresNothing() throws Exception {
        Live l = liveWithTopic("tx-ins");
        String successor = retire(l);

        Resp r = post("/topics/insert", Map.of("label", "after-retire", "collection", l.collection(), "doc_count", 0));

        assertRefusal(r, l.collection(), successor);
        assertThat(topicCount(l.collection())).as("only the pre-retire topic").isEqualTo(1);
    }

    @Test
    void assign_toARetiredSourceCollection_is409NamingTheSuccessor() throws Exception {
        Live l = liveWithTopic("tx-asg");
        String successor = retire(l);

        Resp r = post("/assignments/assign", Map.of("doc_id", ch("a-" + l.collection()), "topic_id", l.topicId(),
            "assigned_by", "centroid", "source_collection", l.collection()));

        assertRefusal(r, l.collection(), successor);
    }

    @Test
    void assignMany_toARetiredSourceCollection_is409NamingTheSuccessor() throws Exception {
        Live l = liveWithTopic("tx-asm");
        String successor = retire(l);

        Resp r = post("/assignments/assign_many", Map.of("assignments", List.of(Map.of(
            "doc_id", ch("m-" + l.collection()), "topic_id", l.topicId(), "assigned_by", "centroid",
            "source_collection", l.collection()))));

        assertRefusal(r, l.collection(), successor);
    }

    @Test
    void assignFromChashes_intoARetiredCollection_is409NamingTheSuccessor() throws Exception {
        Live l = liveWithTopic("tx-afc");
        String successor = retire(l);

        Resp r = post("/assignments/assign_from_chashes", Map.of("collection", l.collection(),
            "chashes", List.of(ch("c-" + l.collection())), "cross_collection", false));

        assertRefusal(r, l.collection(), successor);
    }

    @Test
    void recordDiscoverCount_forARetiredName_is409NamingTheSuccessor() throws Exception {
        Live l = liveWithTopic("tx-meta");
        String successor = retire(l);

        Resp r = post("/meta/record", Map.of("collection", l.collection(), "doc_count", 7));

        assertRefusal(r, l.collection(), successor);
    }

    @Test
    void persistSplit_inARetiredName_is409_andLeavesTheParentTopic() throws Exception {
        Live l = liveWithTopic("tx-spl");
        String successor = retire(l);

        Resp r = post("/topics/persist_split", Map.of("topic_id", l.topicId(), "collection_name", l.collection(),
            "child_specs", List.of(spec("child"))));

        assertRefusal(r, l.collection(), successor);
        assertThat(topicCount(l.collection())).as("the split rolled back whole").isEqualTo(1);
    }

    @Test
    void persistRebuild_inARetiredName_is409_andDoesNotClearTheOldTopics() throws Exception {
        Live l = liveWithTopic("tx-reb");
        String successor = retire(l);

        // Empty specs: the REPLACE would delete the old topics without writing a new one.
        Resp r = post("/topics/persist_rebuild", Map.of("collection", l.collection(), "specs", List.of()));

        assertRefusal(r, l.collection(), successor);
        assertThat(topicCount(l.collection())).as("the refused rebuild deleted nothing").isEqualTo(1);
    }

    @Test
    void persistDiscovered_inARetiredName_is409NamingTheSuccessor() throws Exception {
        String coll = fixture("tx-dis").collection();
        // No warm topic here: persist_discovered skips a collection that already has topics.
        Live l = new Live(coll, -1);
        String successor = retire(l);

        Resp r = post("/topics/persist_discovered", Map.of("collection", coll, "specs", List.of(spec("found"))));

        assertRefusal(r, coll, successor);
        assertThat(topicCount(coll)).isZero();
    }

    // ── controls and the sites that stay plain ──────────────────────────────────

    @Test
    void control_aLiveCollectionAcceptsTheSameWrites() throws Exception {
        Live l = liveWithTopic("tx-live");

        assertThat(post("/topics/insert", Map.of("label", "second", "collection", l.collection(), "doc_count", 0))
            .status()).isEqualTo(200);
        Resp meta = post("/meta/record", Map.of("collection", l.collection(), "doc_count", 7));
        assertThat(meta.status()).as(meta.body()).isEqualTo(200);
        Resp rebuild = post("/topics/persist_rebuild", Map.of("collection", l.collection(), "specs", List.of(spec("r"))));
        assertThat(rebuild.status()).as(rebuild.body()).isEqualTo(200);
        assertThat(topicCount(l.collection())).as("rebuild replaced the two topics with one").isEqualTo(1);
    }

    @Test
    void importMeta_ontoATombstone_isStillAccepted() throws Exception {
        // The /import/* verbs are fidelity imports, as the manifest UPSERT_IMPORT is: they carry tombstoned
        // collections as exported. Stays on the plain registration check (nexus-i15oc).
        Live l = liveWithTopic("tx-imp");
        retire(l);

        Resp r = post("/import/meta", Map.of("collection", l.collection(), "last_discover_doc_count", 3));

        assertThat(r.status()).as(r.body()).isEqualTo(200);
    }

    @Test
    void unassigned_readOfARetiredName_isStillServed() throws Exception {
        Live l = liveWithTopic("tx-rd");
        retire(l);

        Resp r = post("/assignments/unassigned", Map.of("collection", l.collection(), "limit", 10));

        assertThat(r.status()).as(r.body()).isEqualTo(200);
    }
}
