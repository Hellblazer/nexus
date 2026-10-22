// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ReembedSourceReader.PlanCounts;
import dev.nexus.service.vectors.VoyageEmbedder;
import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_LINKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 6 Step 3 (bead nexus-cnxjk): {@code ReembedSourceReader#planCounts}, the counts the {@code /plan}
 * route states before a job runs, against a real Postgres. Each count is the number of rows the cutover would move,
 * skip or destroy, so each is seeded with rows the count must include AND rows it must leave out.
 *
 * <p>Run: {@code -Dtest=ReembedPlanCountsTest}.
 */
class ReembedPlanCountsTest extends ReembedFixture {

    private static long tokens(String text) {
        return VoyageEmbedder.estimateTokens(text);
    }

    @Test
    void anEmptySourcePlansZeroOfEverything() {
        Fx fx = pair("empty");
        assertThat(reader.planCounts(fx.tenant(), fx.src()))
            .isEqualTo(new PlanCounts(0, 0, 0, 0, 0, 0, 0, 0, 0, 0));
    }

    @Test
    void documentsChunksOwnerlessAndTheTokenEstimateAreCountedFromLiveOwnership() {
        Fx fx = pair("docs");
        List<String> live = seedDocs(fx, 2, 3);                       // 2 live documents, 6 chunks
        List<String> uni = List.of("héllo wörld ünïcode ✓");  // multi-byte: bytes, not chars
        String uniDoc = seedDoc(fx, "uni", uni);                      // 1 live document, 1 chunk
        String tomb = seedDoc(fx, "tomb", List.of("tombstoned one " + fx.tag(), "tombstoned two " + fx.tag()));
        assertThat(catalog.deleteDocument(fx.tenant(), tomb)).isEqualTo(1);
        String orphanText = "orphan chunk no manifest " + fx.tag();
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant(), fx.src(), List.of(ch(orphanText)), List.of(orphanText),
                List.of(new float[768]), List.of(Map.of()));
            return null;
        });

        PlanCounts n = reader.planCounts(fx.tenant(), fx.src());

        assertThat(n.documents()).as("live documents").isEqualTo(3);
        assertThat(n.tombstonedDocuments()).isEqualTo(1);
        assertThat(n.chunks()).as("chunks a live document owns").isEqualTo(7);
        assertThat(n.ownerlessChunks()).as("the tombstoned document's two and the orphan").isEqualTo(3);
        long expected = 0;
        for (String id : live) {
            for (int k = 0; k < 3; k++) {
                expected += tokens("doc " + fx.tag() + " d" + Integer.parseInt(id.substring(id.length() - 3))
                    + " chunk " + k + " lorem ipsum");
            }
        }
        expected += tokens(uni.get(0));
        assertThat(uni.get(0).getBytes(StandardCharsets.UTF_8).length).isGreaterThan(uni.get(0).length());
        assertThat(n.tokenEstimate()).as("the runner's own estimate, over live-owned chunks only").isEqualTo(expected);
        assertThat(uniDoc).isNotBlank();
    }

    @Test
    void aChunkTwoDocumentsShareIsCountedOnce() {
        Fx fx = pair("shared");
        String text = "shared chunk text " + fx.tag();
        seedDoc(fx, "a", List.of(text));
        seedDoc(fx, "b", List.of(text));

        PlanCounts n = reader.planCounts(fx.tenant(), fx.src());

        assertThat(n.documents()).isEqualTo(2);
        assertThat(n.chunks()).isEqualTo(1);
        assertThat(n.tokenEstimate()).isEqualTo(tokens(text));
    }

    @Test
    void quarantineRowsCountOnlyTheSourcesOwn() {
        Fx fx = pair("quar");
        String sibling = "quarantine-" + fx.src();
        String otherOrigin = "quarantine-knowledge__someone-else__" + BGE + "__v1";
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, fx.tenant(), sibling);
            PgContainerHelper.insertCollection(ctx, fx.tenant(), otherOrigin);
            for (int i = 0; i < 2; i++) {
                String text = "quarantined " + i + " " + fx.tag();
                PgContainerHelper.insertChunks(ctx, fx.tenant(), sibling, List.of(ch(text)), List.of(text),
                    List.of(new float[768]), List.of(Map.of("origin_collection", fx.src())));
            }
            String foreign = "quarantined for another origin " + fx.tag();
            PgContainerHelper.insertChunks(ctx, fx.tenant(), otherOrigin, List.of(ch(foreign)), List.of(foreign),
                List.of(new float[768]), List.of(Map.of("origin_collection", "knowledge__someone-else__" + BGE + "__v1")));
            return null;
        });

        assertThat(reader.planCounts(fx.tenant(), fx.src()).quarantineRows()).isEqualTo(2);
    }

    @Test
    void crossCollectionTopicAssignmentsAndLinksAreTheOnesTheTopicDeleteCascadeWouldTake() {
        Fx fx = pair("topics");
        List<String> ids = seedDocs(fx, 1, 2);
        assertThat(ids).hasSize(1);
        String ownText = "doc " + fx.tag() + " d0 chunk 0 lorem ipsum";
        String otherText = "chunk of another collection " + fx.tag();
        long topicA = 9_000_000L + Math.abs((long) (fx.tenant() + "a").hashCode());
        long topicB = topicA + 1;
        long topicElsewhere = topicA + 2;
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant(), fx.tgt(), List.of(ch(otherText)), List.of(otherText),
                List.of(new float[384]), List.of(Map.of()));
            for (var spec : List.of(new Object[] {topicA, fx.src()}, new Object[] {topicB, fx.src()},
                                    new Object[] {topicElsewhere, fx.tgt()})) {
                ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                        TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                    .values((Long) spec[0], fx.tenant(), "t" + spec[0], (String) spec[1], 0, OffsetDateTime.now(),
                        "pending")
                    .execute();
            }
            // an assignment of ANOTHER collection to the source's topic A: counted
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
                .values(fx.tenant(), Chash.fromHex(ch(otherText)).toBytes(), topicA, "projection", fx.tgt(),
                    OffsetDateTime.now(), MINI)
                .execute();
            // the source's OWN assignment to its own topic: rebuilt with the topics, not a cross-collection loss
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
                .values(fx.tenant(), Chash.fromHex(ch(ownText)).toBytes(), topicA, "projection", fx.src(),
                    OffsetDateTime.now(), BGE)
                .execute();
            String ownText2 = "doc " + fx.tag() + " d0 chunk 1 lorem ipsum";
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
                .values(fx.tenant(), Chash.fromHex(ch(ownText2)).toBytes(), topicA, "projection", fx.src(),
                    OffsetDateTime.now(), BGE)
                .execute();
            // links: A to B (both the source's), and the elsewhere topic to B: both touch the source's topics.
            // A link between two topics of another collection is not the source's.
            ctx.insertInto(TOPIC_LINKS, TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                .values(fx.tenant(), topicA, topicB).execute();
            ctx.insertInto(TOPIC_LINKS, TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                .values(fx.tenant(), topicElsewhere, topicB).execute();
            return null;
        });

        PlanCounts n = reader.planCounts(fx.tenant(), fx.src());

        assertThat(n.foreignTopicAssignments()).isEqualTo(1);
        assertThat(n.topicLinks()).isEqualTo(2);
    }

    @Test
    void satelliteRowsCountWhenTheirDocumentIsNotOneTheJobMoves() {
        Fx fx = pair("sat");
        String live = seedDoc(fx, "live", List.of("live doc chunk " + fx.tag()));
        String tomb = seedDoc(fx, "tomb", List.of("tombstoned doc chunk " + fx.tag()));
        assertThat(catalog.deleteDocument(fx.tenant(), tomb)).isEqualTo(1);
        su(ctx -> {
            OffsetDateTime now = OffsetDateTime.now();
            // rows of the live document: moved with it, not counted
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(fx.tenant(), fx.src(), "/p/live.md", now, "v1", "docling", live, "file:///p/live.md").execute();
            ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(fx.tenant(), live, fx.src(), "file:///hl-live", "hi", now).execute();
            // rows of the tombstoned document: the job never moves it, so these stay naming the source
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(fx.tenant(), fx.src(), "/p/tomb.md", now, "v1", "docling", tomb, "file:///p/tomb.md").execute();
            ctx.insertInto(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID,
                    ASPECT_EXTRACTION_QUEUE.COLLECTION, ASPECT_EXTRACTION_QUEUE.SOURCE_PATH,
                    ASPECT_EXTRACTION_QUEUE.STATUS, ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
                .values(fx.tenant(), fx.src(), "/p/tomb-q.md", "pending", now, tomb).execute();
            ctx.insertInto(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID,
                    DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD,
                    DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(fx.tenant(), tomb, fx.src(), "file:///hl-tomb", "hi", now).execute();
            return null;
        });

        assertThat(reader.planCounts(fx.tenant(), fx.src()).unmatchedSatelliteRows())
            .as("aspect + queue + highlight rows of the tombstoned document").isEqualTo(3);
    }

    @Test
    void aLiveDocumentHomedHereWhoseChunksAreElsewhereIsNotAMoveCandidate() {
        Fx fx = pair("elsewhere");
        String docId = "rr." + fx.tag() + ".elsewhere";
        catalog.upsertDocument(fx.tenant(), Map.of("tumbler", docId, "title", "t", "content_type", CONTENT_TYPE,
            "corpus", CONTENT_TYPE, "physical_collection", fx.src(), "chunk_count", 0));
        String text = "lives in the target " + fx.tag();
        var written = moveSvc.writeManyCombined(fx.tenant(), fx.tgt(),
            List.of(Map.of("chash", ch(text), "text", text, "metadata", Map.of("k", 0))),
            List.of(Map.of("doc_id", docId, "rows", List.of(Map.of("position", 0, "chash", ch(text), "chunk_index", 0)))),
            null, false, false);
        assertThat((List<?>) written.response().get("failed_doc_ids")).isEmpty();

        PlanCounts n = reader.planCounts(fx.tenant(), fx.src());

        assertThat(n.documents()).as("its manifest names only the target").isZero();
        assertThat(n.chunks()).isZero();
        assertThat(n.liveHomedElsewhere()).as("the cutover's delete removes its catalog row: residue").isEqualTo(1);
        assertThat(n.liveHomedElsewhere()).as("the plan agrees with the cutover's own residue count")
            .isEqualTo(reader.residue(fx.tenant(), fx.src()).liveHomedElsewhere());
    }

    @Test
    void liveHomedElsewhereLeavesOutCandidatesTombstonesAndOtherCollectionsDocuments() {
        Fx fx = pair("homed");
        seedDoc(fx, "mover", List.of("a candidate " + fx.tag()));                       // names the source: a candidate
        String bare = "rr." + fx.tag() + ".bare";                                       // homed here, no manifest: a candidate
        catalog.upsertDocument(fx.tenant(), Map.of("tumbler", bare, "title", "t", "content_type", CONTENT_TYPE,
            "corpus", CONTENT_TYPE, "physical_collection", fx.src(), "chunk_count", 0));
        String away = "rr." + fx.tag() + ".away";                                       // homed in the target: not ours
        catalog.upsertDocument(fx.tenant(), Map.of("tumbler", away, "title", "t", "content_type", CONTENT_TYPE,
            "corpus", CONTENT_TYPE, "physical_collection", fx.tgt(), "chunk_count", 0));
        String strayText = "stray " + fx.tag();
        String stray = "rr." + fx.tag() + ".stray";                                     // homed here, manifest elsewhere
        catalog.upsertDocument(fx.tenant(), Map.of("tumbler", stray, "title", "t", "content_type", CONTENT_TYPE,
            "corpus", CONTENT_TYPE, "physical_collection", fx.src(), "chunk_count", 0));
        writeElsewhere(fx, stray, strayText);
        String tomb = "rr." + fx.tag() + ".tomb";                                       // tombstoned stray: counted as a tombstone
        catalog.upsertDocument(fx.tenant(), Map.of("tumbler", tomb, "title", "t", "content_type", CONTENT_TYPE,
            "corpus", CONTENT_TYPE, "physical_collection", fx.src(), "chunk_count", 0));
        writeElsewhere(fx, tomb, "tombstoned stray " + fx.tag());
        assertThat(catalog.deleteDocument(fx.tenant(), tomb)).isEqualTo(1);

        PlanCounts n = reader.planCounts(fx.tenant(), fx.src());

        assertThat(n.documents()).isEqualTo(2);
        assertThat(n.tombstonedDocuments()).isEqualTo(1);
        assertThat(n.liveHomedElsewhere()).isEqualTo(1);
    }

    private void writeElsewhere(Fx fx, String docId, String text) {
        var written = moveSvc.writeManyCombined(fx.tenant(), fx.tgt(),
            List.of(Map.of("chash", ch(text), "text", text, "metadata", Map.of("k", 0))),
            List.of(Map.of("doc_id", docId, "rows", List.of(Map.of("position", 0, "chash", ch(text), "chunk_index", 0)))),
            null, false, false);
        assertThat((List<?>) written.response().get("failed_doc_ids")).isEmpty();
    }

    /**
     * Every count the plan states, seeded with at least one row, in {@code fx}'s tenant. The seeded sizes grow with
     * {@code scale}, so two tenants seeded at different scales cannot produce equal counts by accident.
     */
    private void seedEveryCount(Fx fx, int scale) {
        seedDocs(fx, scale, 2);                                                  // live candidates, 2 chunks each
        String tomb = seedDoc(fx, "tomb", List.of("tombstoned " + fx.tag()));
        assertThat(catalog.deleteDocument(fx.tenant(), tomb)).isEqualTo(1);
        String orphan = "ownerless " + fx.tag();
        String stray = "rr." + fx.tag() + ".stray";
        catalog.upsertDocument(fx.tenant(), Map.of("tumbler", stray, "title", "t", "content_type", CONTENT_TYPE,
            "corpus", CONTENT_TYPE, "physical_collection", fx.src(), "chunk_count", 0));
        writeElsewhere(fx, stray, "stray " + fx.tag());
        String sibling = "quarantine-" + fx.src();
        long topicA = 9_100_000L + Math.abs((long) (fx.tenant() + "ta").hashCode());
        long topicB = topicA + 1;
        long topicElsewhere = topicA + 2;
        String foreignText = "foreign " + fx.tag();
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant(), fx.src(), List.of(ch(orphan)), List.of(orphan),
                List.of(new float[768]), List.of(Map.of()));
            PgContainerHelper.insertCollection(ctx, fx.tenant(), sibling);
            for (int i = 0; i < scale; i++) {
                String q = "quarantined " + i + " " + fx.tag();
                PgContainerHelper.insertChunks(ctx, fx.tenant(), sibling, List.of(ch(q)), List.of(q),
                    List.of(new float[768]), List.of(Map.of("origin_collection", fx.src())));
            }
            PgContainerHelper.insertChunks(ctx, fx.tenant(), fx.tgt(), List.of(ch(foreignText)), List.of(foreignText),
                List.of(new float[384]), List.of(Map.of()));
            for (var spec : List.of(new Object[] {topicA, fx.src()}, new Object[] {topicB, fx.src()},
                                    new Object[] {topicElsewhere, fx.tgt()})) {
                ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                        TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                    .values((Long) spec[0], fx.tenant(), "t" + spec[0], (String) spec[1], 0, OffsetDateTime.now(),
                        "pending")
                    .execute();
            }
            ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
                .values(fx.tenant(), Chash.fromHex(ch(foreignText)).toBytes(), topicA, "projection", fx.tgt(),
                    OffsetDateTime.now(), MINI)
                .execute();
            ctx.insertInto(TOPIC_LINKS, TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                .values(fx.tenant(), topicA, topicB).execute();
            ctx.insertInto(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
                    DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                    DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(fx.tenant(), fx.src(), "/p/tomb.md", OffsetDateTime.now(), "v1", "docling", tomb,
                    "file:///p/tomb.md").execute();
            return null;
        });
    }

    @Test
    void anotherTenantsSameNamedSourceChangesNoCount() {
        Fx fx = pair("xtenant");
        seedEveryCount(fx, 2);
        PlanCounts before = reader.planCounts(fx.tenant(), fx.src());
        assertThat(before.documents()).isEqualTo(2);
        assertThat(before.tombstonedDocuments()).isEqualTo(1);
        assertThat(before.liveHomedElsewhere()).isEqualTo(1);
        assertThat(before.ownerlessChunks()).isPositive();
        assertThat(before.quarantineRows()).isEqualTo(2);
        assertThat(before.foreignTopicAssignments()).isEqualTo(1);
        assertThat(before.topicLinks()).isEqualTo(1);
        assertThat(before.unmatchedSatelliteRows()).isEqualTo(1);
        assertThat(before.tokenEstimate()).isPositive();

        // A second tenant with a source and a target of the very same names, document ids and chunk texts,
        // holding more of everything.
        Fx other = new Fx(fx.tenant() + "-other", fx.tag(), fx.src(), fx.tgt());
        register(other.tenant(), other.src());
        register(other.tenant(), other.tgt());
        setProfile(other.tenant(), MINI, 384);
        seedEveryCount(other, 5);
        PlanCounts theirs = reader.planCounts(other.tenant(), other.src());
        assertThat(theirs.documents()).as("the other tenant's rows are really there").isEqualTo(5);

        assertThat(reader.planCounts(fx.tenant(), fx.src())).as("the first tenant's counts, unchanged")
            .isEqualTo(before);
        assertThat(theirs).isNotEqualTo(before);
        assertThat(reader.residue(fx.tenant(), fx.src()).liveHomedElsewhere()).isEqualTo(before.liveHomedElsewhere());

        // Row-level security already hides the other tenant from the service role, so the check above cannot see a
        // dropped tenant_id predicate. Read through the container's superuser (RLS does not apply to it): then the
        // predicates are the only thing keeping the tenants apart.
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        try (var suDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
            var unguarded = new dev.nexus.service.db.ReembedSourceReader(new dev.nexus.service.db.TenantScope(suDs));
            assertThat(unguarded.planCounts(other.tenant(), other.src())).as("superuser read, other tenant")
                .isEqualTo(theirs);
            assertThat(unguarded.planCounts(fx.tenant(), fx.src())).as("superuser read, first tenant: predicates only")
                .isEqualTo(before);
        }
    }
}
