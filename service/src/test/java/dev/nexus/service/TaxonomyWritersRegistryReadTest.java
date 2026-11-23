// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, row 14): the taxonomy writers keyed by a TOPIC (or by a label or a label prefix) read the registry rows they
 * write under {@code FOR KEY SHARE} before they write, and write by the id the rows carry, never by a name column.
 *
 * <p><b>Rename-gap tests</b> (a misdirect: a write keyed by a label reached rows of another collection, or re-stamped a row with another
 * collection's id). The one-row rename (P3.7) is not on this base, so the registry row takes its new label in place and the content's name
 * columns stay at the old one, as in {@code RenameGapSeededChildRowsTest}; the old label is then taken by a NEW row. Each test files rows under
 * the renamed collection (before the rename) and under the new holder of the old label (after), runs the write, and reads that the renamed
 * collection's rows were written by their id and the new holder's rows were neither touched nor reached.
 *
 * <p><b>Verb-race tests</b> (the write meets a retiring verb): a hand session holds a registry row {@code FOR UPDATE}, which is all a verb's
 * first step is. The write must WAIT for the row (it shows as a waiting lock in {@code pg_locks}, and has written nothing), and land when the
 * verb lets go. The row held is the SOURCE collection of an assignment the write deletes or moves, not the topic's own collection: the
 * registry rows a topic-keyed write did not read before this change.
 *
 * <p>Nothing here is engine-run by its author. The pattern is {@code RenameGapSeededChildRowsTest} (the fixture, the rename) and
 * {@code MultiCollectionWritersLockOrderTest} (the verb).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyWritersRegistryReadTest {

    CollectionRouteFixture fx;
    ExecutorService pool;
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
        pool = Executors.newFixedThreadPool(2);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (fx != null) fx.close();
    }

    // ---- seeds and reads ------------------------------------------------------------------------------------------------

    private String registered(String stem) {
        String l = label("tpr-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    /** The one-row rename: the registry row takes its new label in place; the content's name columns stay as they are. */
    private String oneRowRename(String from) {
        long id = fx.idOf(from);
        String to = label("tpr-renamed-" + (n++));
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute());
        assertThat(fx.idOf(to)).as("setup: the renamed row is the same row").isEqualTo(id);
        return to;
    }

    /** {@code from} renamed in place and its old label taken by a new row; returns the new row's id. */
    private long renamedAndRetaken(String from) {
        oneRowRename(from);
        fx.register(from);
        return fx.idOf(from);
    }

    private long topic(String collection, String topicLabel) {
        Resp r = fx.post("/v1/taxonomy/topics/insert", "{\"collection\":" + json(collection) + ",\"label\":" + json(topicLabel) + ",\"doc_count\":0}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
        return r.json().path("id").asLong(-1);
    }

    private String doc(String stem) {
        return Chash.ofText("tpr-" + stem + "-" + (n++)).toHex();
    }

    /** A stored chunk under {@code collection}: an assignment names its source collection and its chunk (topic_assignments_chunk_fk). */
    private void chunk(String collection, String hex) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("seed"), List.of(new float[384]),
                List.of(java.util.Map.<String, Object>of()));
            return null;
        });
    }

    /** A projection assignment of {@code doc} (a chunk of {@code source}, which it stores first) onto {@code topic}. */
    private void projection(long topic, String doc, double similarity, String source) {
        chunk(source, doc);
        Resp r = fx.post("/v1/taxonomy/assignments/assign", "{\"doc_id\":\"" + doc + "\",\"topic_id\":" + topic
            + ",\"assigned_by\":\"projection\",\"similarity\":" + similarity + ",\"source_collection\":" + json(source) + "}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    /** An assignment of a chunk already stored (two topics may hold one document). */
    private void projectionOfStored(long topic, String doc, double similarity, String source) {
        Resp r = fx.post("/v1/taxonomy/assignments/assign", "{\"doc_id\":\"" + doc + "\",\"topic_id\":" + topic
            + ",\"assigned_by\":\"projection\",\"similarity\":" + similarity + ",\"source_collection\":" + json(source) + "}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private record TopicRow(String label, Long collectionId, String reviewStatus) {
    }

    private TopicRow topicRow(long id) {
        return fx.asSuperuser(ctx -> ctx.select(TOPICS.LABEL, TOPICS.COLLECTION_ID, TOPICS.REVIEW_STATUS).from(TOPICS)
            .where(TOPICS.ID.eq(id)).fetchOptional().map(r -> new TopicRow(r.value1(), r.value2(), r.value3())).orElse(null));
    }

    private int count(org.jooq.Table<?> table, org.jooq.Condition where) {
        return fx.asSuperuser(ctx -> ctx.fetchCount(table, where));
    }

    private int assignmentCount(String doc, long topic) {
        return fx.asSuperuser(ctx -> ctx.fetchCount(TOPIC_ASSIGNMENTS,
            TOPIC_ASSIGNMENTS.DOC_ID.eq(Chash.fromHex(doc).toBytes()).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic))));
    }

    private Long sourceId(String doc, long topic) {
        return fx.asSuperuser(ctx -> ctx.select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID).from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.DOC_ID.eq(Chash.fromHex(doc).toBytes()).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic)))
            .fetchOne(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID));
    }

    // ---- the verb ----------------------------------------------------------------------------------------------------

    private int waitingRows() {
        return fx.asSuperuser(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid"))));
    }

    private void awaitWaiting(Future<?> racer, String what) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waitingRows() < 1) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": no lock request ever showed waiting in pg_locks (racer done: " + racer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    private void lockRow(Connection c, String name) {
        DSL.using(c, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).forUpdate().fetch();
    }

    /**
     * Sends {@code path} while a hand session holds {@code heldCollection}'s registry row {@code FOR UPDATE}: waits until the request shows as a
     * waiting lock, runs {@code whileWaiting} (what must not have happened yet), lets the verb go, and returns the answer.
     */
    private Resp whileAVerbHolds(String heldCollection, String path, String body, Runnable whileWaiting) throws Exception {
        Future<Resp> writer;
        try (Connection verb = fx.pg.createConnection("")) {
            verb.setAutoCommit(false);
            lockRow(verb, heldCollection);
            writer = pool.submit(() -> fx.post(path, body));
            try {
                awaitWaiting(writer, path + " waits for the registry row the verb holds");
                assertThat(writer.isDone()).as("the write has not finished while the verb holds the row").isFalse();
                whileWaiting.run();
            } finally {
                verb.rollback();
            }
        }
        return writer.get(120, TimeUnit.SECONDS);
    }

    // ==== misdirect rows: rename-gap tests ===============================================================================

    /** One topic of a renamed collection (id x, name column still the old label) and one of the new holder of that label (id y). */
    private record Gap(String old, long x, long y, long renamedTopic, long holderTopic) {
    }

    private Gap gapWithATopicEach(String stem) {
        String b = registered(stem);
        long x = fx.idOf(b);
        long renamedTopic = topic(b, "renamed-topic");
        long y = renamedAndRetaken(b);
        long holderTopic = topic(b, "holder-topic");
        assertThat(topicRow(renamedTopic).collectionId()).as("setup: the renamed collection's topic carries its id").isEqualTo(x);
        assertThat(topicRow(holderTopic).collectionId()).as("setup: the new holder's topic carries the holder's id").isEqualTo(y);
        return new Gap(b, x, y, renamedTopic, holderTopic);
    }

    @Timeout(180)
    @Test
    void updateTopicLabel_ofATopicOfARenamedCollection_keepsItsId_andLeavesTheNewHoldersTopicAlone() {
        Gap g = gapWithATopicEach("ul");

        Resp r = fx.post("/v1/taxonomy/topics/update_label", "{\"topic_id\":" + g.renamedTopic + ",\"label\":\"relabelled\"}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(topicRow(g.renamedTopic).label()).as("the topic took its new label").isEqualTo("relabelled");
        assertThat(topicRow(g.renamedTopic).collectionId()).as("and kept the id of its collection (not the id its stale name now resolves to)").isEqualTo(g.x);
        assertThat(topicRow(g.holderTopic)).as("the new holder's topic is untouched").isEqualTo(new TopicRow("holder-topic", g.y, "pending"));
    }

    @Timeout(180)
    @Test
    void renameTopic_ofATopicOfARenamedCollection_keepsItsId_andLeavesTheNewHoldersTopicAlone() {
        Gap g = gapWithATopicEach("rn");

        Resp r = fx.post("/v1/taxonomy/topics/rename", "{\"topic_id\":" + g.renamedTopic + ",\"label\":\"renamed-by-hand\"}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(topicRow(g.renamedTopic)).as("renamed, accepted, and still its own collection's")
            .isEqualTo(new TopicRow("renamed-by-hand", g.x, "accepted"));
        assertThat(topicRow(g.holderTopic)).as("the new holder's topic is untouched").isEqualTo(new TopicRow("holder-topic", g.y, "pending"));
    }

    @Timeout(180)
    @Test
    void markTopicReviewed_ofATopicOfARenamedCollection_keepsItsId_andLeavesTheNewHoldersTopicAlone() {
        Gap g = gapWithATopicEach("mr");

        Resp r = fx.post("/v1/taxonomy/topics/mark_reviewed", "{\"topic_id\":" + g.renamedTopic + ",\"status\":\"deleted\"}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(topicRow(g.renamedTopic)).as("reviewed, and still its own collection's").isEqualTo(new TopicRow("renamed-topic", g.x, "deleted"));
        assertThat(topicRow(g.holderTopic)).as("the new holder's topic is untouched").isEqualTo(new TopicRow("holder-topic", g.y, "pending"));
    }

    /**
     * Found in passing: a topic whose name column bears a label NO row holds any more (the collection was renamed, nothing took the old label).
     * Re-deriving the id from that name gave NULL, which topics_collection_id_check refuses (23514); the topic keeps the id it carries.
     */
    private long topicWhoseLabelLostItsRow(String stem) {
        String b = registered(stem);
        long x = fx.idOf(b);
        long t = topic(b, "orphaned-label-topic");
        oneRowRename(b);
        assertThat(fx.exists(b)).as("setup: no row holds the old label").isFalse();
        assertThat(topicRow(t).collectionId()).as("setup").isEqualTo(x);
        return t;
    }

    @Timeout(180)
    @Test
    void updateTopicLabel_ofATopicWhoseNameLostItsRegistryRow_keepsItsId() {
        long t = topicWhoseLabelLostItsRow("ul-lost");
        long x = topicRow(t).collectionId();

        Resp r = fx.post("/v1/taxonomy/topics/update_label", "{\"topic_id\":" + t + ",\"label\":\"relabelled\"}");

        assertThat(r.status()).as("not a 23514: %s", r.body()).isEqualTo(200);
        assertThat(topicRow(t)).isEqualTo(new TopicRow("relabelled", x, "pending"));
    }

    @Timeout(180)
    @Test
    void renameTopic_ofATopicWhoseNameLostItsRegistryRow_keepsItsId() {
        long t = topicWhoseLabelLostItsRow("rn-lost");
        long x = topicRow(t).collectionId();

        Resp r = fx.post("/v1/taxonomy/topics/rename", "{\"topic_id\":" + t + ",\"label\":\"renamed-by-hand\"}");

        assertThat(r.status()).as("not a 23514: %s", r.body()).isEqualTo(200);
        assertThat(topicRow(t)).isEqualTo(new TopicRow("renamed-by-hand", x, "accepted"));
    }

    @Timeout(180)
    @Test
    void markTopicReviewed_ofATopicWhoseNameLostItsRegistryRow_keepsItsId() {
        long t = topicWhoseLabelLostItsRow("mr-lost");
        long x = topicRow(t).collectionId();

        Resp r = fx.post("/v1/taxonomy/topics/mark_reviewed", "{\"topic_id\":" + t + ",\"status\":\"deleted\"}");

        assertThat(r.status()).as("not a 23514: %s", r.body()).isEqualTo(200);
        assertThat(topicRow(t)).isEqualTo(new TopicRow("orphaned-label-topic", x, "deleted"));
    }

    /**
     * purge_doc names the project by LABEL: the collection that label resolves to now. After a rename and a re-registration that is the new
     * holder, whose topics are the ones of its id; a topic of the renamed collection still bears the label in its name column and is not the
     * project's. (hex title: the assignments delete, then the empty-topics sweep.)
     */
    @Timeout(180)
    @Test
    void purgeDoc_byALabelTheRenamedCollectionNoLongerBears_reachesOnlyTheNewHoldersTopicsAndAssignments() {
        String b = registered("pd");
        long renamedTopic = topic(b, "renamed-topic");
        String renamedDoc = doc("pd-renamed");
        projection(renamedTopic, renamedDoc, 0.9, b);
        long y = renamedAndRetaken(b);
        long holderTopic = topic(b, "holder-topic");
        String holderDoc = doc("pd-holder");
        projection(holderTopic, holderDoc, 0.9, b);
        assertThat(y).as("setup: a new holder").isNotEqualTo(topicRow(renamedTopic).collectionId());

        Resp ofTheRenamed = fx.post("/v1/taxonomy/assignments/purge_doc", "{\"project\":" + json(b) + ",\"title\":\"" + renamedDoc + "\"}");

        assertThat(ofTheRenamed.status()).as(ofTheRenamed.body()).isEqualTo(200);
        assertThat(ofTheRenamed.json().path("removed").asInt(-1)).as("the renamed collection's assignment is not the project's").isZero();
        assertThat(assignmentCount(renamedDoc, renamedTopic)).as("it is still there").isEqualTo(1);
        assertThat(topicRow(renamedTopic)).as("and its topic").isNotNull();

        Resp ofTheHolder = fx.post("/v1/taxonomy/assignments/purge_doc", "{\"project\":" + json(b) + ",\"title\":\"" + holderDoc + "\"}");

        assertThat(ofTheHolder.status()).as(ofTheHolder.body()).isEqualTo(200);
        assertThat(ofTheHolder.json().path("removed").asInt(-1)).as("the new holder's assignment is").isEqualTo(1);
        assertThat(topicRow(holderTopic)).as("and its topic, left empty, was swept").isNull();
        assertThat(assignmentCount(renamedDoc, renamedTopic)).as("the renamed collection's rows survived both").isEqualTo(1);
        assertThat(topicRow(renamedTopic)).isNotNull();
    }

    /** purge_doc with a title that is no chash still sweeps the project's empty topics: the project's by id, not by a stale name. */
    @Timeout(180)
    @Test
    void purgeDoc_withAnUnshapedTitle_sweepsOnlyTheNewHoldersEmptyTopics() {
        String b = registered("pd-sweep");
        long renamedEmpty = topic(b, "renamed-empty");
        long y = renamedAndRetaken(b);
        long holderEmpty = topic(b, "holder-empty");
        assertThat(topicRow(holderEmpty).collectionId()).as("setup").isEqualTo(y);

        Resp r = fx.post("/v1/taxonomy/assignments/purge_doc", "{\"project\":" + json(b) + ",\"title\":\"not-a-chash\"}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(topicRow(holderEmpty)).as("the new holder's empty topic was swept").isNull();
        assertThat(topicRow(renamedEmpty)).as("the renamed collection's empty topic was not the project's").isNotNull();
    }

    /**
     * prune_projection with match "exact": the rows of the collection the label resolves to now, by its id. The renamed collection's
     * projection still bears the label in its source_collection column and is not the label's.
     */
    @Timeout(180)
    @Test
    void pruneProjectionExact_byALabelTheRenamedCollectionNoLongerBears_prunesOnlyTheNewHoldersRows() {
        String b = registered("pe");
        long renamedTopic = topic(b, "renamed-topic");
        String renamedDoc = doc("pe-renamed");
        projection(renamedTopic, renamedDoc, 0.1, b);
        long y = renamedAndRetaken(b);
        long holderTopic = topic(b, "holder-topic");
        String holderDoc = doc("pe-holder");
        projection(holderTopic, holderDoc, 0.1, b);
        assertThat(sourceId(holderDoc, holderTopic)).as("setup: the holder's projection carries the holder's id").isEqualTo(y);

        Resp r = fx.post("/v1/taxonomy/assignments/prune_projection",
            "{\"source_collection_prefix\":" + json(b) + ",\"min_similarity\":0.5,\"match\":\"exact\"}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("removed").asInt(-1)).as("one row: the holder's").isEqualTo(1);
        assertThat(assignmentCount(holderDoc, holderTopic)).as("the holder's weak projection was pruned").isZero();
        assertThat(assignmentCount(renamedDoc, renamedTopic)).as("the renamed collection's was not").isEqualTo(1);
    }

    /** The prefix mode: the rows of every collection whose label starts with the prefix NOW, by id. */
    @Timeout(180)
    @Test
    void pruneProjectionPrefix_byAPrefixTheRenamedCollectionNoLongerBears_prunesOnlyTheRowsOfCollectionsThatDo() {
        String b = registered("pp");
        long renamedTopic = topic(b, "renamed-topic");
        String renamedDoc = doc("pp-renamed");
        projection(renamedTopic, renamedDoc, 0.1, b);
        long y = renamedAndRetaken(b);
        long holderTopic = topic(b, "holder-topic");
        String holderDoc = doc("pp-holder");
        projection(holderTopic, holderDoc, 0.1, b);
        assertThat(sourceId(holderDoc, holderTopic)).as("setup").isEqualTo(y);

        Resp r = fx.post("/v1/taxonomy/assignments/prune_projection", "{\"source_collection_prefix\":" + json(b) + ",\"min_similarity\":0.5}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("removed").asInt(-1)).as("one row: the holder's").isEqualTo(1);
        assertThat(assignmentCount(holderDoc, holderTopic)).as("the holder's weak projection was pruned").isZero();
        assertThat(assignmentCount(renamedDoc, renamedTopic)).as("the renamed collection's, filed under a label that prefix no longer names, was not").isEqualTo(1);
    }

    /**
     * mergeTopics, the conflict arm: both topics hold a document; the winner's source collection is written with its id. The id was re-resolved
     * from the winning row's NAME, which after the rename is the new holder's. Two documents cover the two winners (the incoming row, the
     * existing row), and a third has no conflict (it moves, keeping its id).
     */
    @Timeout(180)
    @Test
    void mergeTopics_whoseAssignmentsAreOfARenamedCollection_keepEachRowsSourceId_inBothConflictArms() {
        String b = registered("mg");
        long x = fx.idOf(b);
        long source = topic(b, "merge-source");
        long target = topic(b, "merge-target");
        String incomingWins = doc("mg-in");
        String existingWins = doc("mg-ex");
        String moved = doc("mg-mv");
        projection(source, incomingWins, 0.9, b);
        projectionOfStored(target, incomingWins, 0.2, b);
        projection(source, existingWins, 0.1, b);
        projectionOfStored(target, existingWins, 0.8, b);
        projection(source, moved, 0.5, b);
        long y = renamedAndRetaken(b);
        String c = fx.labelOf(x);   // x's current label
        assertThat(y).as("setup: a new holder of the old label").isNotEqualTo(x);

        Resp r = fx.post("/v1/taxonomy/topics/merge", "{\"source_id\":" + source + ",\"target_id\":" + target + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("collection").asText()).as("answered by the collection's CURRENT label").isEqualTo(c);
        assertThat(sourceId(incomingWins, target)).as("the incoming row won: it keeps its source id").isEqualTo(x);
        assertThat(sourceId(existingWins, target)).as("the existing row won: it keeps its source id").isEqualTo(x);
        assertThat(sourceId(moved, target)).as("a row with no conflict moved, keeping its source id").isEqualTo(x);
        assertThat(topicRow(source)).as("the source topic is gone").isNull();
    }

    // ==== race rows: verb-race tests =====================================================================================

    /** A topic of {@code collection} that holds one projection of a chunk of {@code other}. Returns the topic. */
    private long topicProjectedFrom(String collection, String topicLabel, String other, String stem) {
        long t = topic(collection, topicLabel);
        projection(t, doc(stem), 0.7, other);
        return t;
    }

    @Timeout(180)
    @Test
    void deleteTopic_meetingAVerbHoldingTheSourceCollectionOfItsAssignments_waitsForIt_thenDeletes() throws Exception {
        String home = registered("dt-home");
        String other = registered("dt-other");
        long t = topicProjectedFrom(home, "doomed", other, "dt");
        String current = oneRowRename(home);   // the topic's name column keeps the old label

        Resp r = whileAVerbHolds(other, "/v1/taxonomy/topics/delete", "{\"topic_id\":" + t + "}",
            () -> assertThat(topicRow(t)).as("the delete has written nothing while it waits").isNotNull());

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("collection").asText()).as("answered by the collection's CURRENT label").isEqualTo(current);
        assertThat(topicRow(t)).as("deleted once the verb let go").isNull();
    }

    @Timeout(180)
    @Test
    void mergeTopics_meetingAVerbHoldingTheSourceCollectionOfAMovedAssignment_waitsForIt_thenMerges() throws Exception {
        String home = registered("mg-home");
        String other = registered("mg-other");
        long source = topic(home, "merge-source");
        long target = topic(home, "merge-target");
        String d = doc("mg-race");
        projection(source, d, 0.7, other);

        Resp r = whileAVerbHolds(other, "/v1/taxonomy/topics/merge", "{\"source_id\":" + source + ",\"target_id\":" + target + "}",
            () -> assertThat(assignmentCount(d, source)).as("the merge has moved nothing while it waits").isEqualTo(1));

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(assignmentCount(d, target)).as("the assignment moved to the target once the verb let go").isEqualTo(1);
        assertThat(sourceId(d, target)).as("with its own source id").isEqualTo(fx.idOf(other));
    }

    @Timeout(180)
    @Test
    void persistSplit_meetingAVerbHoldingTheSourceCollectionOfTheParentsAssignments_waitsForIt_thenSplits() throws Exception {
        String home = registered("ps-home");
        String other = registered("ps-other");
        long parent = topicProjectedFrom(home, "parent", other, "ps");
        String spec = "{\"label\":\"child\",\"doc_count\":0,\"created_at\":\"2026-10-22T00:00:00Z\",\"terms_json\":\"[]\",\"doc_ids\":[]}";

        Resp r = whileAVerbHolds(other, "/v1/taxonomy/topics/persist_split",
            "{\"topic_id\":" + parent + ",\"collection_name\":" + json(home) + ",\"child_specs\":[" + spec + "]}",
            () -> assertThat(count(TOPICS, TOPICS.PARENT_ID.eq(parent))).as("no child yet").isZero());

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("child_ids")).as("the child was written once the verb let go").hasSize(1);
        assertThat(count(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TOPIC_ID.eq(parent))).as("the parent's assignments were replaced").isZero();
    }

    /** A split names a collection and a topic: the topic must be that collection's, or nothing is deleted or written. */
    @Timeout(180)
    @Test
    void persistSplit_ofATopicOfAnotherCollection_isRefused_andChangesNothing() {
        String home = registered("ps-own");
        String foreign = registered("ps-foreign");
        long foreignParent = topicProjectedFrom(foreign, "foreign-parent", foreign, "ps-foreign-doc");
        String spec = "{\"label\":\"child\",\"doc_count\":0,\"created_at\":\"2026-10-22T00:00:00Z\",\"terms_json\":\"[]\",\"doc_ids\":[]}";

        Resp r = fx.post("/v1/taxonomy/topics/persist_split",
            "{\"topic_id\":" + foreignParent + ",\"collection_name\":" + json(home) + ",\"child_specs\":[" + spec + "]}");

        assertThat(r.status()).as("a 400, the shared catch arm of an IllegalArgumentException: %s", r.body()).isEqualTo(400);
        assertThat(count(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TOPIC_ID.eq(foreignParent)))
            .as("the other collection's topic kept its assignments").isEqualTo(1);
        assertThat(count(TOPICS, TOPICS.PARENT_ID.eq(foreignParent))).as("and gained no child").isZero();
    }

    @Timeout(180)
    @Test
    void purgeCollection_meetingAVerbHoldingTheSourceCollectionOfADoomedTopicsAssignments_waitsForIt_thenPurges() throws Exception {
        String home = registered("pc-home");
        String other = registered("pc-other");
        long t = topicProjectedFrom(home, "doomed", other, "pc");

        Resp r = whileAVerbHolds(other, "/v1/taxonomy/purge_collection", "{\"collection\":" + json(home) + "}",
            () -> assertThat(topicRow(t)).as("the purge has deleted nothing while it waits").isNotNull());

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("topics").asInt(-1)).isEqualTo(1);
        assertThat(r.json().path("assignments").asInt(-1)).as("the projection from the other collection went with its topic").isEqualTo(1);
        assertThat(topicRow(t)).as("purged once the verb let go").isNull();
    }
}
