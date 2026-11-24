// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.vectors.CollectionSelector;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TopicRoute;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.stream.Collectors;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), item 6 "Readers during the stamp": once gate 3 holds the engine's readers select by the
 * collection's id, so a collection whose registry row was renamed (P3.7's one-row rename: the registry's name changes, the id and
 * every row's own name column stay) is found by its new label and answered under it. Before gate 3 every reader is the name
 * path, which finds nothing under the new label.
 *
 * <p>Fixture: a collection written through the combined write (chunks, a document, manifest rows) with a topic, an aspect, a link
 * and a seed document, then its registry row renamed with the registry's name keys dropped, the move P3.7 makes. Every route and
 * reader below runs with the latched gate (a fixed true: tests pass what the latch would) and with {@link Gate3#never()}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cSearchByIdIntegrationTest extends AtomicWriteTestBase {

    /**
     * The fixture latches gate 3 by hand on the fresh walk, which skips vectors-084 (CONTINUE-gated); in production gate 3 waits for
     * those three changesets, and annQuery calls their by-id overload with no fallback. So the by-id overloads are applied here, for the
     * routed case; idempotent under {@code -Dnexus.test.formA.applied=true}, where the walk already applied them.
     */
    @Override
    @org.junit.jupiter.api.BeforeAll
    protected void startAll() throws Exception {
        super.startAll();
        try (Connection su = pg.createConnection("")) {
            assertThat(Rdr229CompleteStamps.applyAnnQueryById(su)).as("the by-id overloads of vectors-084").hasSize(3);
        }
    }

    private static final String TOPIC_LABEL = "p35c-topic";
    private static final Gate3 LATCHED = () -> true;

    /** A renamed collection: its chunk and manifest rows still carry {@code oldName}; the registry row says {@code newName}. */
    private record Renamed(String oldName, String newName, Set<String> chashes, String docId, String seedDoc, long topicId) {
        String title() {
            return "atomic-write-" + docId;
        }
    }

    private PgVectorRepository repository(Gate3 gate3) {
        return new PgVectorRepository(tenantScope, embedder, embedder, gate3);
    }

    private Renamed renamedCollection(String tag) throws Exception {
        return renamedCollection(tag, "code");
    }

    private Renamed renamedCollection(String tag, String contentType) throws Exception {
        String oldName = contentType + "__p35c" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(oldName);
        Renamed fixture = seededCollection(oldName, tag);
        rename(oldName, fixture.newName());
        return fixture;
    }

    /** A collection seeded under {@code oldName}, not yet renamed. */
    private Renamed seededCollection(String oldName, String tag) {
        String docId = freshDoc("p35c", oldName);
        String seedDoc = freshDoc("p35cseed", oldName);
        List<String> chashes = List.of(ch("p35c " + tag + " one"), ch("p35c " + tag + " two"), ch("p35c " + tag + " three"));
        svc.writeManyCombined(TENANT, oldName,
            List.of(chunk(chashes.get(0), "alpha one"), chunk(chashes.get(1), "alpha two"), chunk(chashes.get(2), "alpha three")),
            List.of(doc(docId, List.of(row(0, chashes.get(0)), row(1, chashes.get(1)), row(2, chashes.get(2))))),
            null, false, true);
        long topicId = seedTopic(oldName, chashes);
        seedAspectAndLink(oldName, docId, seedDoc);
        return new Renamed(oldName, oldName.replace("__v1", "__v2"), Set.copyOf(chashes), docId, seedDoc, topicId);
    }

    private void rename(String oldName, String newName) throws Exception {
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
    }

    private long seedTopic(String collection, List<String> chashes) {
        return tenantScope.withTenant(TENANT, ctx -> {
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
            return topicId;
        });
    }

    private void seedAspectAndLink(String collection, String docId, String seedDoc) {
        new AspectRepository(tenantScope).upsertAspect(TENANT, Map.of(
            "collection", collection, "source_path", docId + ".md", "extracted_at", "2026-01-01T00:00:00+00:00",
            "model_version", "v1", "extractor_name", "test", "source_uri", "file:///" + docId + ".md",
            "confidence", 0.9, "doc_id", docId, "problem_formulation", "alpha probe " + docId));
        tenantScope.withTenant(TENANT, ctx -> ctx.insertInto(CATALOG_LINKS, CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER,
                CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY)
            .values(TENANT, seedDoc, docId, "cites", "test").onConflictDoNothing().execute());
    }

    private void markSensitive(String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, true)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId)).execute();
        }
    }

    private static Set<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> String.valueOf(r.get("id"))).collect(Collectors.toSet());
    }

    private static Set<String> labels(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> String.valueOf(r.get("collection"))).collect(Collectors.toSet());
    }

    @SuppressWarnings("unchecked")
    private static Set<String> idsOf(Map<String, Object> envelope) {
        return Set.copyOf((List<String>) envelope.get("ids"));
    }

    // ---- plain /search ------------------------------------------------------------------------------------------------

    @Test
    void plainSearchFindsTheRenamedCollectionUnderItsNewLabelFromGate3() throws Exception {
        Renamed c = renamedCollection("plain");

        var rows = repository(LATCHED).search(TENANT, "alpha", List.of(c.newName()), 10, null);

        assertThat(ids(rows)).isEqualTo(c.chashes());
        assertThat(labels(rows)).as("the rows answer the registry's label, not the stale name on the chunk rows")
            .containsExactly(c.newName());
    }

    @Test
    void plainSearchBeforeGate3CallsTheNameFunctionAndFindsNothingUnderTheNewLabel() throws Exception {
        Renamed c = renamedCollection("plainname");

        assertThat(repository(Gate3.never()).search(TENANT, "alpha", List.of(c.newName()), 10, null)).isEmpty();
    }

    /** One call with two renamed collections: the id array carries both, and each row answers its own label. */
    @Test
    void aMultiCollectionSearchTakesTheIdArrayAndKeepsEachRowsOwnLabel() throws Exception {
        Renamed a = renamedCollection("multia");
        Renamed b = renamedCollection("multib");

        var rows = repository(LATCHED).search(TENANT, "alpha", List.of(a.newName(), b.newName()), 20, null);

        Set<String> expected = new java.util.HashSet<>(a.chashes());
        expected.addAll(b.chashes());
        assertThat(ids(rows)).isEqualTo(expected);
        assertThat(labels(rows)).containsExactlyInAnyOrder(a.newName(), b.newName());
        assertThat(rows.stream().filter(r -> a.newName().equals(r.get("collection"))).map(r -> String.valueOf(r.get("id")))
            .collect(Collectors.toSet())).isEqualTo(a.chashes());
    }

    /** A -> B, then a new collection takes the freed label A: searching A returns only the new A's rows. */
    @Test
    void aNewHolderOfTheOldLabelIsTheOnlyCollectionTheOldLabelFinds() throws Exception {
        Renamed old = renamedCollection("holder");
        registerCollection(old.oldName());
        List<String> fresh = List.of(ch("p35c holder fresh one"), ch("p35c holder fresh two"));
        String freshDoc = freshDoc("p35cfresh", old.oldName());
        svc.writeManyCombined(TENANT, old.oldName(),
            List.of(chunk(fresh.get(0), "alpha fresh one"), chunk(fresh.get(1), "alpha fresh two")),
            List.of(doc(freshDoc, List.of(row(0, fresh.get(0)), row(1, fresh.get(1))))), null, false, true);

        var asA = repository(LATCHED).search(TENANT, "alpha", List.of(old.oldName()), 20, null);
        var asB = repository(LATCHED).search(TENANT, "alpha", List.of(old.newName()), 20, null);

        assertThat(ids(asA)).as("the label A is the new collection's now").isEqualTo(Set.copyOf(fresh));
        assertThat(labels(asA)).containsExactly(old.oldName());
        assertThat(ids(asB)).as("the renamed collection answers under B only").isEqualTo(old.chashes());
        assertThat(repository(LATCHED).count(TENANT, old.oldName())).isEqualTo(2);
        assertThat(repository(LATCHED).count(TENANT, old.newName())).isEqualTo(3);
    }

    // ---- sensitive exclusion by the twins ---------------------------------------------------------------------------------

    @Test
    void aSensitiveChunkIsFilteredOnThePlainHybridAndGetPathsByIdToo() throws Exception {
        Renamed c = renamedCollection("sens");
        markSensitive(c.docId());
        var repo = repository(LATCHED);

        assertThat(repo.searchWithTokens(TENANT, "alpha", List.of(c.newName()), 10, null, false, null, true).value())
            .as("plain, excluded").isEmpty();
        assertThat(repo.hybridSearchWithTokens(TENANT, "alpha", List.of(c.newName()), 10, null, false, null, true).value())
            .as("hybrid, excluded").isEmpty();
        assertThat(idsOf(repo.get(TENANT, c.newName(), List.copyOf(c.chashes()), 10, 0, false, true)))
            .as("get, excluded").isEmpty();
        var kept = repo.searchWithTokens(TENANT, "alpha", List.of(c.newName()), 10, null, false, null, false).value();
        assertThat(ids(kept)).as("not excluded: the rows come back").isEqualTo(c.chashes());
        assertThat(kept).extracting(r -> r.get("sensitive")).containsOnly(true);
    }

    // ---- hybrid, text-gated and the probe ---------------------------------------------------------------------------------

    @Test
    void hybridSearchFindsTheRenamedCollectionOnTheSelectiveAndTheDenseBranch() throws Exception {
        Renamed c = renamedCollection("hybrid");
        var latched = repository(LATCHED);

        var selective = latched.hybridSearch(TENANT, "alpha", List.of(c.newName()), 10, null, 1000);
        var dense = latched.hybridSearch(TENANT, "alpha", List.of(c.newName()), 10, null, 1);

        assertThat(ids(selective)).as("probe, then text_gated_search_by_chash").isEqualTo(c.chashes());
        assertThat(labels(selective)).containsExactly(c.newName());
        assertThat(ids(dense)).as("probe, then text_gated_search_hnsw_first").isEqualTo(c.chashes());
        assertThat(repository(Gate3.never()).hybridSearch(TENANT, "alpha", List.of(c.newName()), 10, null, 1000)).isEmpty();
    }

    @Test
    void aChashAllowListIsRankedByIdOnThePlainAndTheHybridPath() throws Exception {
        Renamed c = renamedCollection("allow");
        byte[][] allowed = {HexFormat.of().parseHex(c.chashes().iterator().next())};
        var latched = repository(LATCHED);

        var plain = latched.searchWithTokens(TENANT, "alpha", List.of(c.newName()), 10, null, false, allowed).value();
        var hybrid = latched.hybridSearchWithTokens(TENANT, "alpha", List.of(c.newName()), 10, null, false, 1000, allowed).value();

        assertThat(ids(plain)).as("text_gated_search_by_chash_by_id").containsExactly(c.chashes().iterator().next());
        assertThat(ids(hybrid)).as("text_gated_search_allowed_by_id").containsExactly(c.chashes().iterator().next());
    }

    @Test
    void aManifestWherePushdownRunsThroughTheManifestTwin() throws Exception {
        Renamed c = renamedCollection("mfwhere");
        Map<String, Object> where = Map.of("title", c.title());

        var rows = repository(LATCHED).search(TENANT, "alpha", List.of(c.newName()), 10, where);

        assertThat(ids(rows)).as("manifest_search_by_id").isEqualTo(c.chashes());
        assertThat(labels(rows)).containsExactly(c.newName());
        assertThat(repository(LATCHED).search(TENANT, "alpha", List.of(c.newName()), 10, Map.of("title", "no such title")))
            .isEmpty();
    }

    // ---- per-collection fan-out -----------------------------------------------------------------------------------------

    @Test
    void perCollectionSearchCallsTheTwinWithOneIdPerArmOverTwoCollections() throws Exception {
        Renamed a = renamedCollection("fana");
        Renamed b = renamedCollection("fanb");

        var result = repository(LATCHED).searchPerCollection(TENANT, "alpha", List.of(a.newName(), b.newName()), 10, 10, null,
            null, false);

        Set<String> expected = new java.util.HashSet<>(a.chashes());
        expected.addAll(b.chashes());
        assertThat(ids(result.rows())).isEqualTo(expected);
        assertThat(result.perCollection()).extracting(PgVectorRepository.PerCollectionStat::collection)
            .containsExactly(a.newName(), b.newName());
        assertThat(result.perCollection()).extracting(PgVectorRepository.PerCollectionStat::rawCount).containsExactly(3, 3);
        assertThat(labels(result.rows())).containsExactlyInAnyOrder(a.newName(), b.newName());
    }

    // ---- metadata-, aspect-, graph-hop- and topic-scoped ------------------------------------------------------------------

    @Test
    void metadataScopedSearchFindsTheRenamedCollectionFromGate3() throws Exception {
        Renamed c = renamedCollection("meta");

        var rows = repository(LATCHED).searchMetadataScoped(TENANT, "alpha", List.of(c.newName()), null, null, null, null, 10);

        assertThat(rows).isNotEmpty();
        assertThat(labels(rows)).containsExactly(c.newName());
        assertThat(repository(Gate3.never()).searchMetadataScoped(TENANT, "alpha", List.of(c.newName()), null, null, null,
            null, 10)).isEmpty();
    }

    @Test
    void aspectScopedSearchFindsTheRenamedCollectionFromGate3() throws Exception {
        Renamed c = renamedCollection("aspect");

        var rows = repository(LATCHED).searchAspectScopedWithTokens(TENANT, "alpha", List.of(c.newName()), null, null, null,
            null, 10).value();

        assertThat(rows).isNotEmpty();
        assertThat(ids(rows)).contains(c.docId());
        assertThat(labels(rows)).containsExactly(c.newName());
        assertThat(repository(Gate3.never()).searchAspectScopedWithTokens(TENANT, "alpha", List.of(c.newName()), null, null,
            null, null, 10).value()).isEmpty();
    }

    @Test
    void graphHopSearchFindsTheRenamedCollectionFromGate3() throws Exception {
        Renamed c = renamedCollection("hop");

        var rows = repository(LATCHED).searchGraphHopWithTokens(TENANT, "alpha", List.of(c.seedDoc()), List.of(c.newName()),
            "cites", 1, "out", null, 10).value();

        assertThat(ids(rows)).contains(c.docId());
        assertThat(labels(rows)).containsExactly(c.newName());
        assertThat(repository(Gate3.never()).searchGraphHopWithTokens(TENANT, "alpha", List.of(c.seedDoc()),
            List.of(c.newName()), "cites", 1, "out", null, 10).value()).isEmpty();
    }

    @Test
    void topicScopedSearchFindsTheRenamedCollectionFromGate3() throws Exception {
        Renamed c = renamedCollection("topic");

        var rows = repository(LATCHED).searchTopicScoped(TENANT, "alpha", TOPIC_LABEL, c.newName(), 10);

        assertThat(ids(rows)).isEqualTo(c.chashes());
        assertThat(labels(rows)).containsExactly(c.newName());
    }

    /** The routed arm: the centroid ranks the topic, and the topic-set twin serves the rows under the registry label. */
    @Test
    void aRoutedFanOutArmSearchesTheTopicSetByIdAndAttachesItsMetadata() throws Exception {
        Renamed c = renamedCollection("route", "knowledge");   // a code collection is never routed
        // the centroid sits where the query does, so its topic is the nearest; written before the latch under the new label
        float[] q = embedder.embed(List.of("alpha")).get(0);
        new TaxonomyCentroidRepository(tenantScope, Gate3.never())   // seeding: the id unique key is P3.3c's, not built on this fixture
            .upsertCentroids(TENANT, List.of(new TaxonomyCentroidRepository.CentroidRecord(c.newName(), c.topicId(), q,
                TOPIC_LABEL, 3)));
        TopicRoute route = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(c.newName()),
            (t, col, ms) -> Optional.of(new dev.nexus.service.db.TaxonomyCoverage.Counts(true, 3, 0)), false);

        var result = repository(LATCHED).searchPerCollection(TENANT, "alpha", List.of(c.newName()), 10, 10, null, null, false,
            false, false, new PgVectorRepository.FanoutSettings(1, 30_000, 30_000), null, false, route);

        assertThat(result.perCollection().get(0).routeStatus()).as("the arm routed").isEqualTo("routed");
        assertThat(ids(result.rows())).isEqualTo(c.chashes());
        assertThat(labels(result.rows())).containsExactly(c.newName());
    }

    // ---- the get family, count, delete, update, list --------------------------------------------------------------------

    @Test
    void getGetWhereGetAllMetadataListAndCountSelectTheRenamedCollectionByIdFromGate3() throws Exception {
        Renamed c = renamedCollection("gets");
        var latched = repository(LATCHED);
        var never = repository(Gate3.never());
        List<String> ids = List.copyOf(c.chashes());

        assertThat(idsOf(latched.get(TENANT, c.newName(), ids, 10, 0, false))).isEqualTo(c.chashes());
        assertThat(idsOf(never.get(TENANT, c.newName(), ids, 10, 0, false))).isEmpty();
        assertThat(idsOf(latched.getWhere(TENANT, c.newName(), null, 10, 0))).isEqualTo(c.chashes());
        assertThat(idsOf(latched.getWhere(TENANT, c.newName(), Map.of("title", c.title()), 10, 0)))
            .as("a manifest label key is answered per owner, handed the label").isEqualTo(c.chashes());
        assertThat(idsOf(latched.getAllMetadata(TENANT, c.newName(), null, false))).isEqualTo(c.chashes());
        assertThat(idsOf(latched.getAllMetadata(TENANT, c.newName(), Map.of("title", c.title()), false)))
            .isEqualTo(c.chashes());
        assertThat(idsOf(latched.list(TENANT, c.newName(), 10, 0))).isEqualTo(c.chashes());
        assertThat(latched.count(TENANT, c.newName())).isEqualTo(3);
        assertThat(never.count(TENANT, c.newName())).isZero();
        assertThat(idsOf(latched.presentRows(TENANT, c.newName(), ids, false))).isEqualTo(c.chashes());
        assertThat(idsOf(latched.getEmbeddings(TENANT, c.newName(), ids))).isEqualTo(c.chashes());
    }

    @Test
    void updateMetadataAndDeleteSelectTheRenamedCollectionByIdFromGate3() throws Exception {
        Renamed c = renamedCollection("writes");
        String loose = ch("p35c writes loose");
        // a chunk no manifest row protects, written before the rename under the old name
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, c.newName(), List.of(loose),
                List.of("alpha loose"), List.of(embedder.embed(List.of("alpha loose")).get(0)), List.of(Map.of()));
        }
        String firstChash = c.chashes().iterator().next();
        var latched = repository(LATCHED);

        assertThat(latched.updateMetadata(TENANT, c.newName(), List.of(firstChash), List.of(Map.of("p35c", "stamped"))))
            .isEqualTo(1);
        assertThat(repository(Gate3.never()).updateMetadata(TENANT, c.newName(), List.of(firstChash),
            List.of(Map.of("p35c", "never")))).as("the name path finds no chunk named the new label").isZero();
        @SuppressWarnings("unchecked")
        var metas = (List<Map<String, Object>>) latched.get(TENANT, c.newName(), List.of(firstChash), 10, 0, false)
            .get("metadatas");
        assertThat(metas.get(0)).containsEntry("p35c", "stamped");

        assertThat(latched.delete(TENANT, c.newName(), List.of(loose))).as("the unprotected chunk is deleted by id").isEqualTo(1);
        assertThat(latched.delete(TENANT, c.newName(), List.of(firstChash))).as("a manifest-owned chunk stays").isZero();
    }

    @Test
    void aCombinedWriteOfIdenticalTextRefreshesTheMetadataByIdFromGate3() throws Exception {
        Renamed c = renamedCollection("combined");
        String chash = c.chashes().iterator().next();
        String text = chunkText(c.oldName(), chash);
        var service = new CombinedWriteService(tenantScope, repo, new EmbedderRouter(embedder, "document"), LATCHED);

        service.writeManyCombined(TENANT, c.newName(),
            List.of(Map.of("chash", chash, "text", text, "metadata", Map.of("chunk_text_hash", ch("p35c refreshed")))),
            List.of(doc(c.docId(), List.of(row(0, chash)))), null, false, false);   // forceReEmbed off: the metadata-only branch

        try (Connection su = pg.createConnection("")) {
            var rows = DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.COLLECTION, CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash))).fetch();
            assertThat(rows).as("one chunk row: the metadata-only path found it by id, no second row was inserted").hasSize(1);
            assertThat(rows.get(0).value2().data()).contains(ch("p35c refreshed"));
        }
    }

    // ---- ownership, stats, routing, siblings ------------------------------------------------------------------------------

    @Test
    void liveOwnedChashesMatchesTheManifestByIdFromGate3() throws Exception {
        Renamed c = renamedCollection("owned");
        List<String> hexes = List.copyOf(c.chashes());

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgVectorRepository.liveOwnedChashes(dsl, TENANT, c.newName(), hexes, LATCHED))
                .containsExactlyInAnyOrderElementsOf(hexes);
            assertThat(PgVectorRepository.liveOwnedChashes(dsl, TENANT, c.newName(), hexes, Gate3.never())).isEmpty();
        }
    }

    @Test
    void statsAndRoutingListTheRenamedCollectionUnderItsLabelFromGate3() throws Exception {
        Renamed c = renamedCollection("stats");

        for (Gate3 gate : List.of(LATCHED)) {
            var stats = repository(gate).collectionStats(TENANT, null);
            var mine = stats.stream().filter(r -> c.newName().equals(r.get("name"))).toList();
            assertThat(mine).hasSize(1);
            assertThat(((Number) mine.get(0).get("count")).intValue()).isEqualTo(3);
            assertThat(repository(gate).collectionRouting(TENANT, null).stream().map(r -> r.get("name")))
                .contains(c.newName());
            assertThat(repository(gate).listCollections(TENANT).stream().map(r -> r.get("name"))).contains(c.newName());
        }
    }

    /** linda Q4: the wire stays byte-identical. A retired tombstone with no successor reports the stored text, never itself. */
    @Test
    void aTombstoneWithNoSuccessorReportsTheStoredSupersededByOnTheNameAndTheIdPath() throws Exception {
        String name = "code__p35ctomb" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(name);
        seededCollection(name, "tomb");
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "gone-successor")
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.COLLECTION_ID)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
        }
        CollectionRegistry.evict(TENANT, name);

        var statsName = repository(Gate3.never()).collectionStats(TENANT, null).stream()
            .filter(r -> name.equals(r.get("name"))).toList();
        var statsId = repository(LATCHED).collectionStats(TENANT, null).stream()
            .filter(r -> name.equals(r.get("name"))).toList();
        var routingName = repository(Gate3.never()).collectionRouting(TENANT, null).stream()
            .filter(r -> name.equals(r.get("name"))).toList();
        var routingId = repository(LATCHED).collectionRouting(TENANT, null).stream()
            .filter(r -> name.equals(r.get("name"))).toList();

        assertThat(statsName).hasSize(1);
        assertThat(statsName.get(0)).containsEntry("superseded_by", "gone-successor");
        assertThat(statsId).as("stats: byte-identical to the name path").isEqualTo(statsName);
        assertThat(routingName.get(0)).containsEntry("superseded_by", "gone-successor");
        assertThat(routingId).as("routing: byte-identical to the name path").isEqualTo(routingName);
    }

    @Test
    void quarantineSiblingsAreFoundByIdFromGate3() throws Exception {
        String origin = "code__p35csib" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        String sibling = dev.nexus.service.db.QuarantineOrigin.PREFIX + origin;
        registerCollection(sibling);
        String renamedSibling = dev.nexus.service.db.QuarantineOrigin.PREFIX + origin + "-moved";
        // a client-moved sibling: its own name is not the reaper's, a chunk tagged with the origin is the only link
        String moved = "code__p35csibmoved" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(moved);
        registerCollection(origin);   // the origin is registered: from gate 3 the tag names it by its id (a label-only tag is no sibling, ruling b)
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "quarantine")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.in(sibling, moved)).execute();
            PgContainerHelper.insertChunks(ctx, TENANT, moved, List.of(ch("p35c sibling chunk")), List.of("alpha sib"),
                List.of(embedder.embed(List.of("alpha sib")).get(0)),
                List.of(Map.of("origin_collection", origin, "origin_collection_id",
                    ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(origin)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID))));
            ByIdRestoreSupport.dropRegistryNameKeys(ctx);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, renamedSibling + "x")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(moved)).execute();
        }

        assertThat(repository(LATCHED).resolveQuarantineSiblings(TENANT, origin))
            .as("the renamed sibling is found through its chunk's id, under its registry label")
            .contains(sibling, renamedSibling + "x");
        assertThat(repository(Gate3.never()).resolveQuarantineSiblings(TENANT, origin))
            .as("the name path finds only the reaper's own sibling").containsExactly(sibling);
    }

    // ---- NULL id is no collection, through readers --------------------------------------------------------------------------

    @Test
    void aChunkRowWithNoIdIsNoCollectionToAReaderAndNeverFallsBackToItsName() throws Exception {
        String name = "code__p35cnul" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(name);
        String chash = ch("p35c nul chunk");
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertChunks(ctx, TENANT, name, List.of(chash), List.of("alpha nul"),
                List.of(embedder.embed(List.of("alpha nul")).get(0)), List.of(Map.of()));
            PgContainerHelper.dropConstraint(su, CHUNKS, "chunks_collection_id_check");
            PgContainerHelper.dropConstraint(su, CHUNKS, "chunks_collection_id_not_null");
            ctx.update(CHUNKS).set(CHUNKS.COLLECTION_ID, (Long) null).where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION.eq(name)).execute();
        }

        assertThat(repository(Gate3.never()).count(TENANT, name)).as("the name path still sees the row").isEqualTo(1);
        assertThat(repository(LATCHED).count(TENANT, name)).as("a NULL id is no collection").isZero();
        assertThat(idsOf(repository(LATCHED).list(TENANT, name, 10, 0))).isEmpty();
        assertThat(repository(LATCHED).delete(TENANT, name, List.of(chash))).isZero();
        assertThat(CollectionSelector.of(LATCHED, tenantScope, TENANT, List.of("", "no-such-collection")).selectsNothing())
            .isTrue();
    }

    // ---- linda R2: a NULL-id audit row is compared by its stored name, live holder required ------------------------------------

    private long auditRow(String collection, Long collectionId, String chash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).insertInto(GC_AUDIT)
                .set(GC_AUDIT.TENANT_ID, TENANT).set(GC_AUDIT.OPERATION, "reaper_quarantine")
                .set(GC_AUDIT.COLLECTION, collection).set(GC_AUDIT.COLLECTION_ID, collectionId)
                .set(GC_AUDIT.ACTOR, "engine").set(GC_AUDIT.DRY_RUN, false).set(GC_AUDIT.CHASH_COUNT, 1)
                .set(GC_AUDIT.CHASHES, JSONB.jsonb("[\"" + chash + "\"]"))
                .returningResult(GC_AUDIT.ID).fetchOne().value1();
        }
    }

    @Test
    void aNullIdAuditRowWhoseLabelIsLiveRestoresFromGate3() throws Exception {
        String live = "code__p35caud" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(live);
        long audit = auditRow(live, null, ch("p35c audit live"));

        var outcome = repository(LATCHED).quarantineRestoreFromAudit(TENANT, live, null, audit, 0, 10, "tester", true);

        assertThat(outcome.dryRun()).isTrue();
    }

    @Test
    void aNullIdAuditRowWhoseLabelWasRenamedOrDeletedIsRefusedNamingTheLabel() throws Exception {
        Renamed c = renamedCollection("audren");
        long renamedAway = auditRow(c.oldName(), null, ch("p35c audit renamed"));
        String deleted = "code__p35cauddel" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        long gone = auditRow(deleted, null, ch("p35c audit deleted"));

        assertThat(org.assertj.core.api.Assertions.catchThrowable(() ->
                repository(LATCHED).quarantineRestoreFromAudit(TENANT, c.oldName(), null, renamedAway, 0, 10, "tester", true)))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("recorded before collection ids, under label " + c.oldName()
                + "; no live collection holds " + c.oldName());
        assertThat(org.assertj.core.api.Assertions.catchThrowable(() ->
                repository(LATCHED).quarantineRestoreFromAudit(TENANT, deleted, null, gone, 0, 10, "tester", true)))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("recorded before collection ids, under label " + deleted + "; no live collection holds " + deleted);
    }

    /** A NULL-id audit row whose label only a tombstone holds: no live holder, so the restore refuses (the ordered resolver would pick the tombstone). */
    @Test
    void aNullIdAuditRowWhoseLabelOnlyATombstoneHoldsIsRefused() throws Exception {
        String label = "code__p35caudtomb" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(label);
        String successor = label.replace("__v1", "__succ");
        registerCollection(successor);
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            long succId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(successor))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, succId)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(label)).execute();
        }
        long audit = auditRow(label, null, ch("p35c audit tombstone"));

        assertThat(org.assertj.core.api.Assertions.catchThrowable(() ->
                repository(LATCHED).quarantineRestoreFromAudit(TENANT, label, null, audit, 0, 10, "tester", true)))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("recorded before collection ids, under label " + label + "; no live collection holds " + label);
    }

    @Test
    void anAuditRowWithAnIdIsMatchedByItAfterARename() throws Exception {
        Renamed c = renamedCollection("audid");
        long id = tenantScope.withTenant(TENANT, ctx -> CollectionIds.require(ctx, TENANT, c.newName()));
        long audit = auditRow(c.oldName(), id, ch("p35c audit id"));

        assertThat(repository(LATCHED).quarantineRestoreFromAudit(TENANT, c.newName(), null, audit, 0, 10, "tester", true)
            .dryRun()).as("the id says it is this collection, whatever the stored name").isTrue();
    }

    // ---- the live row wins a shared label ---------------------------------------------------------------------------------

    /**
     * A label resolves live first, then to the newest tombstone (linda Q3). Two rows sharing one label text need P4.1's dropped name
     * key; until then the registry's primary key refuses it, so each label here belongs to one row and the ordering is pinned on the
     * predicate it sorts by: the live row sorts first.
     */
    @Test
    void aLabelResolvesLiveFirstThenToTheNewestTombstone() throws Exception {
        String label = "code__p35clive" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        String tomb = label.replace("__v1", "__old");
        registerCollection(label);
        registerCollection(tomb);
        long liveId;
        long tombId;
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            liveId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(label))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            tombId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(tomb))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, label)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, liveId)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(tombId)).execute();
            var order = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.in(label, tomb))
                .orderBy(CollectionSelector.liveFirst(), CATALOG_COLLECTIONS.COLLECTION_ID.desc())
                .fetch(CATALOG_COLLECTIONS.COLLECTION_ID);
            assertThat(order).as("the row holding its label sorts first").containsExactly(liveId, tombId);
        }

        var sel = CollectionSelector.of(LATCHED, tenantScope, TENANT, List.of(label, tomb));

        assertThat(sel.idOf(label)).as("the live row's id").isEqualTo(liveId);
        assertThat(sel.idOf(tomb)).as("a tombstone-only label resolves to that tombstone").isEqualTo(tombId);
    }

    /** linda Q3: a metadata UPDATE on a chunk of a superseded collection succeeds from gate 3: it keeps the row's own id, no 23514. */
    @Test
    void updateMetadataAndBatchUpdateOnASupersededCollectionsChunkKeepTheRowsOwnId() throws Exception {
        Renamed c = renamedCollection("super");
        long id = tenantScope.withTenant(TENANT, ctx -> CollectionIds.require(ctx, TENANT, c.newName()));
        long other;
        String successor = "code__p35csucc" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(successor);
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            other = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(successor))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, other)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)).execute();
        }
        CollectionRegistry.evict(TENANT, c.newName());
        String chash = c.chashes().iterator().next();

        assertThat(repository(LATCHED).updateMetadata(TENANT, c.newName(), List.of(chash),
            List.of(Map.of("chunk_text_hash", ch("p35c superseded"))))).as("the tombstone's chunk is updated, no 23514").isEqualTo(1);
        var zero = tenantScope.withTenant(TENANT, ctx -> PgVectorRepository.batchUpdateMetadata(ctx,
            dev.nexus.service.vectors.DimTables.CHUNKS.get(384), TENANT, c.newName(), List.of(chash),
            List.of(Map.of("chunk_text_hash", ch("p35c superseded again"))), List.of(0), null, LATCHED));
        assertThat(zero).as("batchUpdateMetadata reports no row missing").isEmpty();
    }

    // ---- the manifest-less census (the one published SQL statement) -------------------------------------------------------

    @Test
    void theManifestLessCensusCountsTheRenamedCollectionsChunksFromGate3() throws Exception {
        Renamed c = renamedCollection("census");

        assertThat(repository(LATCHED).manifestLessCensus(TENANT, c.newName(), 10, 0).scopeChunkTotal()).isEqualTo(3L);
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
        var latched = new TaxonomyCentroidRepository(tenantScope, LATCHED);
        never.upsertCentroids(TENANT, List.of(
            new TaxonomyCentroidRepository.CentroidRecord(oldName, 11L, new float[384], "t11", 2),
            new TaxonomyCentroidRepository.CentroidRecord(oldName, 12L, new float[384], "t12", 3),
            new TaxonomyCentroidRepository.CentroidRecord(other, 21L, new float[384], "t21", 1)));
        String newName = oldName.replace("__v1", "__v2");
        rename(oldName, newName);

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

    @SuppressWarnings("unused")
    private static List<String> unused() {
        return new ArrayList<>();
    }
}
