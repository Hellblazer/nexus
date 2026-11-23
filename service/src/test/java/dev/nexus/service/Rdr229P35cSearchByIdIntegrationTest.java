// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.vectors.CollectionSelector;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Collectors;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), item 6 "Readers during the stamp": once gate 3 holds the engine's searches call the
 * {@code <name>_by_id} twins with the collection's registry id, so a collection whose registry row was renamed (P3.7's
 * one-row rename: the registry's name changes, the id and every row's own name column stay) is found by its new label and
 * answered under it. Before gate 3 every search calls the name function, which finds nothing under the new label.
 *
 * <p>Fixture: one collection written through the combined write (chunks, one document, manifest rows), then its registry row
 * renamed with the registry's name keys dropped, the move P3.7 makes. Each route is run with gate 3 latched and with it
 * never holding, and the latched run flips back.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cSearchByIdIntegrationTest extends AtomicWriteTestBase {

    private static final String TOPIC_LABEL = "p35c-topic";

    /** A renamed collection: its chunk and manifest rows still carry {@code oldName}; the registry row says {@code newName}. */
    private record Renamed(String oldName, String newName, Set<String> chashes, String docId) {}

    private PgVectorRepository repository(Gate3 gate3) {
        return new PgVectorRepository(tenantScope, embedder, embedder, gate3);
    }

    private Renamed renamedCollection(String tag) throws Exception {
        String oldName = "code__p35c" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(oldName);
        String docId = freshDoc("p35c", oldName);
        List<String> chashes = List.of(ch("p35c " + tag + " one"), ch("p35c " + tag + " two"), ch("p35c " + tag + " three"));
        svc.writeManyCombined(TENANT, oldName,
            List.of(chunk(chashes.get(0), "alpha one"), chunk(chashes.get(1), "alpha two"), chunk(chashes.get(2), "alpha three")),
            List.of(doc(docId, List.of(row(0, chashes.get(0)), row(1, chashes.get(1)), row(2, chashes.get(2))))),
            null, false, true);
        seedTopic(oldName, chashes);
        String newName = oldName.replace("__v1", "__v2");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ByIdRestoreSupport.dropRegistryNameKeys(ctx);
            int n = ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, newName)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(oldName)).execute();
            assertThat(n).as("fixture: the one registry row renamed").isEqualTo(1);
        }
        CollectionRegistry.evict(TENANT, oldName);
        CollectionRegistry.evict(TENANT, newName);
        return new Renamed(oldName, newName, Set.copyOf(chashes), docId);
    }

    private void seedTopic(String collection, List<String> chashes) {
        tenantScope.withTenant(TENANT, ctx -> {
            long collectionId = CollectionIds.require(ctx, TENANT, collection);
            long topicId = ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.CREATED_AT,
                    TOPICS.COLLECTION_ID)
                .values(TENANT, TOPIC_LABEL, collection, OffsetDateTime.parse("2026-01-01T00:00:00+00:00"), collectionId)
                .returning(TOPICS.ID).fetchOne().getId();
            for (String chash : chashes) {
                ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                        TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID)
                    .values(TENANT, HexFormat.of().parseHex(chash), topicId, collection,
                        OffsetDateTime.parse("2026-01-01T00:00:00+00:00"), "minilm-l6-v2-384", collectionId)
                    .execute();
            }
            return null;
        });
    }

    private static Set<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> String.valueOf(r.get("id"))).collect(Collectors.toSet());
    }

    private static Set<String> labels(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> String.valueOf(r.get("collection"))).collect(Collectors.toSet());
    }

    // ---- plain /search ------------------------------------------------------------------------------------------

    @Test
    void plainSearchFindsTheRenamedCollectionUnderItsNewLabelFromGate3() throws Exception {
        Renamed c = renamedCollection("plain");

        var rows = repository(() -> true).search(TENANT, "alpha", List.of(c.newName()), 10, null);

        assertThat(ids(rows)).isEqualTo(c.chashes());
        assertThat(labels(rows)).as("the rows answer the registry's label, not the stale name on the chunk rows")
            .containsExactly(c.newName());
    }

    @Test
    void plainSearchBeforeGate3CallsTheNameFunctionAndFindsNothingUnderTheNewLabel() throws Exception {
        Renamed c = renamedCollection("plainname");

        assertThat(repository(Gate3.never()).search(TENANT, "alpha", List.of(c.newName()), 10, null)).isEmpty();
    }

    /** The twins fail open on an unstamped row, so the gate decides on every call: true reads by id, false goes back to the name. */
    @Test
    void theGateIsReadOnEveryCallAndTheNameFunctionRunsWheneverItIsFalse() throws Exception {
        Renamed c = renamedCollection("flip");
        var gate = new AtomicBoolean(false);
        var repo = repository(gate::get);

        assertThat(repo.search(TENANT, "alpha", List.of(c.newName()), 10, null)).isEmpty();
        gate.set(true);
        assertThat(ids(repo.search(TENANT, "alpha", List.of(c.newName()), 10, null))).isEqualTo(c.chashes());
        gate.set(false);
        assertThat(repo.search(TENANT, "alpha", List.of(c.newName()), 10, null))
            .as("gate 3 stopped holding: the next call is the name function again").isEmpty();
    }

    @Test
    void anUnregisteredOrEmptyLabelIsNoCollectionAndNeverFallsBackToAName() throws Exception {
        Renamed c = renamedCollection("nolabel");

        // the registry no longer holds the old label, though the chunk rows still carry it: from gate 3 it selects nothing
        var sel = CollectionSelector.of(() -> true, tenantScope, TENANT, List.of("", c.oldName(), "no-such-collection"));
        assertThat(sel.byId()).isTrue();
        assertThat(sel.selectsNothing()).isTrue();
        assertThat(sel.idOf(c.oldName())).isNull();
        // the label that is registered resolves, beside the ones that are not
        var some = CollectionSelector.of(() -> true, tenantScope, TENANT, List.of("", c.oldName(), c.newName()));
        assertThat(some.ids()).hasSize(1);
        // before gate 3 the selector is the name path, as it always was
        assertThat(CollectionSelector.of(Gate3.never(), tenantScope, TENANT, List.of(c.oldName())).byId()).isFalse();
        // and a read of the old label through the repository finds nothing by id: the empty id selects no row
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CHUNKS,
                CollectionSelector.collectionIs(() -> true, TENANT, dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION,
                    dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID, c.oldName())
                    .and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(TENANT)))).isZero();
            assertThat(ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CHUNKS,
                CollectionSelector.collectionIs(() -> true, TENANT, dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION,
                    dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID, "")
                    .and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(TENANT)))).isZero();
        }
    }

    // ---- the manifest-less census (the one published SQL statement) ------------------------------------------------------

    @Test
    void theManifestLessCensusCountsTheRenamedCollectionsChunksFromGate3() throws Exception {
        Renamed c = renamedCollection("census");

        assertThat(repository(() -> true).manifestLessCensus(TENANT, c.newName(), 10, 0).scopeChunkTotal()).isEqualTo(3L);
        assertThat(repository(Gate3.never()).manifestLessCensus(TENANT, c.newName(), 10, 0).scopeChunkTotal())
            .as("the name statement finds no chunk row named the new label").isZero();
    }

    // ---- the centroid readers -------------------------------------------------------------------------------------------

    @Test
    void theCentroidReadersFindTheRenamedCollectionByIdAndAnswerItsLabelFromGate3() throws Exception {
        String oldName = "code__p35ccen" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(oldName);
        String other = "code__p35ccen" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(other);
        var never = new TaxonomyCentroidRepository(tenantScope, Gate3.never());
        var latched = new TaxonomyCentroidRepository(tenantScope, () -> true);
        never.upsertCentroids(TENANT, List.of(
            new TaxonomyCentroidRepository.CentroidRecord(oldName, 11L, new float[384], "t11", 2),
            new TaxonomyCentroidRepository.CentroidRecord(oldName, 12L, new float[384], "t12", 3),
            new TaxonomyCentroidRepository.CentroidRecord(other, 21L, new float[384], "t21", 1)));
        String newName = oldName.replace("__v1", "__v2");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ByIdRestoreSupport.dropRegistryNameKeys(ctx);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, newName)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(oldName)).execute();
        }
        CollectionRegistry.evict(TENANT, oldName);
        CollectionRegistry.evict(TENANT, newName);

        assertThat(latched.count(TENANT, newName)).isEqualTo(2);
        assertThat(never.count(TENANT, newName)).as("the name path finds no centroid named the new label").isZero();
        var mine = latched.getByCollection(TENANT, newName);
        assertThat(mine).extracting(TaxonomyCentroidRepository.CentroidRecord::topicId).containsExactly(11L, 12L);
        assertThat(mine).extracting(TaxonomyCentroidRepository.CentroidRecord::collection).containsOnly(newName);
        assertThat(latched.getForeignCentroids(TENANT, newName)).extracting(TaxonomyCentroidRepository.CentroidRecord::collection)
            .doesNotContain(newName).contains(other);
        assertThat(latched.getByCollection(TENANT, "")).as("an empty label is no collection").isEmpty();
        assertThat(latched.deleteByIds(TENANT, newName, List.of(11L))).isEqualTo(1);
        assertThat(latched.purgeByCollection(TENANT, newName)).isEqualTo(1);
    }

    // ---- per-collection fan-out ---------------------------------------------------------------------------------

    @Test
    void perCollectionSearchCallsTheTwinWithOneIdPerArm() throws Exception {
        Renamed c = renamedCollection("fan");

        var result = repository(() -> true).searchPerCollection(TENANT, "alpha", List.of(c.newName()), 10, 10, null, null,
            false);

        assertThat(ids(result.rows())).isEqualTo(c.chashes());
        assertThat(labels(result.rows())).containsExactly(c.newName());
        assertThat(result.perCollection()).extracting(PgVectorRepository.PerCollectionStat::collection)
            .containsExactly(c.newName());
    }

    // ---- metadata-scoped ----------------------------------------------------------------------------------------

    @Test
    void metadataScopedSearchFindsTheRenamedCollectionFromGate3() throws Exception {
        Renamed c = renamedCollection("meta");

        var rows = repository(() -> true).searchMetadataScoped(TENANT, "alpha", List.of(c.newName()), null, null, null, null,
            10);

        assertThat(rows).isNotEmpty();
        assertThat(labels(rows)).containsExactly(c.newName());
        assertThat(repository(Gate3.never()).searchMetadataScoped(TENANT, "alpha", List.of(c.newName()), null, null, null,
            null, 10)).isEmpty();
    }

    // ---- topic-scoped -------------------------------------------------------------------------------------------

    @Test
    void topicScopedSearchFindsTheRenamedCollectionFromGate3() throws Exception {
        Renamed c = renamedCollection("topic");

        var rows = repository(() -> true).searchTopicScoped(TENANT, "alpha", TOPIC_LABEL, c.newName(), 10);

        assertThat(ids(rows)).isEqualTo(c.chashes());
        assertThat(labels(rows)).containsExactly(c.newName());
    }
}
