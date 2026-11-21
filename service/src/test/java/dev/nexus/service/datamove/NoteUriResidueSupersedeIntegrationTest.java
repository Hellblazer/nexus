// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgContainerHelper;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * RDR-229 P3.1 fix round B (nexus-p2m1x.17, review gate B2 and F1): P2.10's three residue moves (catalog-049-2) {@code UPDATE}
 * {@code source_uri} on rows whose collection name is set and whose collection id is still NULL, and P3.1's NOT VALID CHECKs
 * ({@code (NULLIF(name, '') IS NULL) = (id IS NULL)}) refuse every such row version with 23514, which the worker classes
 * permanent. A move that is still open when P3.1 lands therefore fails, and the documents move never reaches the
 * {@code on_complete} that registers its satellites. The bead's step "supersedes any P2.10 repair move not yet complete" and
 * the P2.10 critique's S2 comment ask for state-conditional changesets, one per registry state; this class pins them.
 *
 * <p>The base walks the real changelog, so P3.1's CHECKs exist. Each scenario stages the registry (the base empties it), lifts the three
 * CHECKs to write rows as an upgrade left them (name set, id NULL, {@link P31CheckReplay}), puts the CHECKs back NOT VALID, deletes
 * the {@code DATABASECHANGELOG} rows of the supersede changesets and boots again: Liquibase then applies exactly those changesets
 * against the staged registry, which is the tail of the P3.1 walk. No stamp move is registered, so any id a row carries afterwards
 * came from a replacement move's own UPDATE.
 *
 * <p>Not covered here, by design: NoteUriResidueRepairMovesIntegrationTest keeps the rows P3.1 stamps, which hide this state.
 */
class NoteUriResidueSupersedeIntegrationTest extends DataMoveIntegrationBase {

    static final String DOCS_OLD = "rdr229-residue-catalog-documents";
    static final String ASPECTS_OLD = "rdr229-residue-document-aspects";
    static final String HIGHLIGHTS_OLD = "rdr229-residue-document-highlights";
    static final String DOCS_NEW = "rdr229-p31-residue-catalog-documents";
    static final String ASPECTS_NEW = "rdr229-p31-residue-document-aspects";
    static final String HIGHLIGHTS_NEW = "rdr229-p31-residue-document-highlights";
    /** The changesets that supersede, one per move, in the file's order. */
    static final List<String> SUPERSEDE_CHANGESETS = List.of("catalog-052-1", "catalog-052-2", "catalog-052-3");

    private static final String TENANT = "residue-p31";
    private static final OffsetDateTime NOW = OffsetDateTime.parse("2026-11-20T00:00:00Z");

    private static final org.jooq.Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> LABEL_F = DSL.field(DSL.name("label"), String.class);
    private static final Field<String> SUPERSEDES_F = DSL.field(DSL.name("supersedes"), String.class);
    private static final Field<String> CHANGESET_F = DSL.field(DSL.name("changeset_id"), String.class);
    private static final Field<OffsetDateTime> FAILED_AT_F = DSL.field(DSL.name("failed_at"), OffsetDateTime.class);

    /** The registry as the fresh walk of the whole changelog left it, before the first scenario emptied it. */
    private static Map<String, String> supersedesAtMigration;
    private static Record shippedDocs;

    @BeforeAll
    void readWhatTheWalkRegistered() {
        supersedesAtMigration = new TreeMap<>();
        for (Record r : ctx().select(LABEL_F, SUPERSEDES_F).from(MOVES).where(LABEL_F.like("rdr229-%residue-%")).fetch()) {
            supersedesAtMigration.put(r.get(LABEL_F), r.get(SUPERSEDES_F));
        }
        shippedDocs = ctx().select().from(MOVES).where(LABEL_F.eq(DOCS_OLD)).fetchOne();
    }

    // ── a fresh install ──────────────────────────────────────────────────────────────────────────────────

    /**
     * On a walk that registers P2.10's documents move and reaches P3.1 in the same boot, the move is open: it is superseded at
     * once, and the replacement is the only open residue move. The satellites are not registered by the walk (the replacement's
     * on_complete does that).
     */
    @Test
    void aFreshWalkSupersedesTheOpenDocumentsMoveAtOnce_andRegistersTheReplacementAlone() {
        assertThat(supersedesAtMigration).as("the walk registered the old documents move and its replacement, no satellite")
            .containsOnlyKeys(DOCS_OLD, DOCS_NEW);
        assertThat(supersedesAtMigration.get(DOCS_NEW)).isEqualTo(DOCS_OLD);
        assertThat(supersedesAtMigration.get(DOCS_OLD)).isNull();
    }

    // ── the premise: what the shipped moves do to a legacy row ───────────────────────────────────────────

    /**
     * Non-vacuity of everything below: the SHIPPED documents move, run over a row with a name and no id under P3.1's CHECK, fails
     * with 23514. This is the failure the replacement exists to avoid.
     */
    @Test
    void thePremise_theShippedDocumentsMoveFailsWith23514OnALegacyRow() {
        registerOld();
        legacyRows(false);
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();

        finishingWorker(seen).runCycle();

        assertThat(seen).as("the worker reports the failure").isNotEmpty();
        List<String> states = seen.stream().map(f -> f.raised() == null ? "none" : f.raised().getSQLState()).toList();
        assertThat(states).as("the call raised check_violation").contains("23514");
        assertThat(completedAt(DOCS_OLD)).isNull();
    }

    // ── state A: the documents move is open ──────────────────────────────────────────────────────────────

    @Test
    void documentsMoveOpen_theReplacementRepairsTheUriAndSetsTheId_andRegistersTheReplacementSatellites() {
        registerOld();
        legacyRows(false);

        bootAgain();

        assertThat(supersededBy(DOCS_OLD)).as("the open documents move is superseded").isEqualTo(DOCS_NEW);
        assertThat(rowExists(ASPECTS_OLD)).as("the old satellites were never registered, so nothing to supersede").isFalse();
        assertThat(rowExists(ASPECTS_NEW)).as("the replacement's on_complete registers them, not the walk").isFalse();
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();

        workUntilIdle(finishingWorker(seen));

        assertThat(seen).as("no failure: the id is set in the same UPDATE as source_uri").isEmpty();
        assertThat(completedAt(DOCS_OLD)).as("the superseded move never runs").isNull();
        assertThat(DataMoveRuns.activeRunLabels(ctx(), DOCS_OLD)).as("and starts no run").isEmpty();
        for (String m : List.of(DOCS_NEW, ASPECTS_NEW, HIGHLIGHTS_NEW)) {
            assertThat(completedAt(m)).as(m + " completed").isNotNull();
        }
        assertThat(supersededBy(ASPECTS_NEW)).isNull();
        assertThat(supersededBy(HIGHLIGHTS_NEW)).isNull();
        // the repair, and the id, on the document and on both satellites
        assertThat(uri("d1")).isEqualTo("chroma://docs__plain-docs/a.md");
        assertThat(documentId("d1")).as("the replacement set the id with source_uri").isEqualTo(id("plain-docs"));
        assertThat(uri("d2")).isEqualTo("chroma://code__plain-code/x/y.py");
        assertThat(documentId("d2")).isEqualTo(id("plain-code"));
        assertThat(aspectUri("d1")).isEqualTo("chroma://docs__plain-docs/a.md");
        assertThat(aspectId("d1")).isEqualTo(id("plain-docs"));
        assertThat(highlightUri("d1")).isEqualTo("chroma://docs__plain-docs/a.md");
        assertThat(highlightId("d1")).isEqualTo(id("plain-docs"));
        // a row the rule leaves alone stays as it was, id included: the stamp is not this move's work
        assertThat(uri("k1")).isEqualTo("chroma://plain-know/a-note");
        assertThat(documentId("k1")).isNull();
        assertThat(uri("d5")).as("a held value is still skipped").isEqualTo("chroma://plain-docs/held.md");
        assertThat(documentId("d5")).isNull();
    }

    @Test
    void theReplacementsCarryTheShippedSizing_aLabelWithoutASlash_andTemplatesThatSetTheId() {
        registerOld();
        legacyRows(false);
        bootAgain();
        workUntilIdle(finishingWorker(new CopyOnWriteArrayList<>()));

        Map<String, Record> rows = new TreeMap<>();
        ctx().select().from(MOVES).fetch().forEach(r -> rows.put(r.get("label", String.class), r));
        for (String label : List.of(DOCS_NEW, ASPECTS_NEW, HIGHLIGHTS_NEW)) {
            Record r = rows.get(label);
            assertThat(r).as(label).isNotNull();
            assertThat(label).doesNotContain("/");
            assertThat(r.get("changeset_id", String.class)).as(label + " is registered by the changeset whose text holds its INSERT")
                .isEqualTo(SUPERSEDE_CHANGESETS.get(0));
            assertThat(r.get("statement_timeout_ms", Integer.class)).isEqualTo(30_000);
            assertThat(r.get("verification", String.class)).doesNotContain("?").contains("$1");
        }
        assertThat(List.of(rows.get(DOCS_NEW).get("chunk_size", Integer.class), rows.get(DOCS_NEW).get("sleep_ms", Integer.class)))
            .containsExactly(1000, 100);
        for (String satellite : List.of(ASPECTS_NEW, HIGHLIGHTS_NEW)) {
            assertThat(List.of(rows.get(satellite).get("chunk_size", Integer.class), rows.get(satellite).get("sleep_ms", Integer.class)))
                .as(satellite).containsExactly(1000, 50);
            assertThat(rows.get(satellite).get("on_complete", String.class)).isNull();
        }
        assertThat(rows.get(DOCS_NEW).get("template", String.class)).contains("physical_collection_id")
            .contains("COALESCE(t.physical_collection_id, c.collection_id)");
        assertThat(rows.get(ASPECTS_NEW).get("template", String.class)).contains("COALESCE(t.collection_id, c.collection_id)");
        assertThat(rows.get(HIGHLIGHTS_NEW).get("template", String.class)).contains("COALESCE(t.collection_id, c.collection_id)");
        assertThat(rows.get(DOCS_NEW).get("on_complete", String.class)).contains(ASPECTS_NEW).contains(HIGHLIGHTS_NEW)
            .contains("'" + SUPERSEDE_CHANGESETS.get(0) + "'");
    }

    // ── state A, failed: a move that failed permanently is replaced too ──────────────────────────────────

    @Test
    void documentsMoveFailed_itIsSupersededToo() {
        registerOld();
        ctx().update(MOVES).set(FAILED_AT_F, NOW).set(DSL.field(DSL.name("failure"), String.class), "check_violation")
            .where(LABEL_F.eq(DOCS_OLD)).execute();
        legacyRows(false);

        bootAgain();

        assertThat(supersededBy(DOCS_OLD)).isEqualTo(DOCS_NEW);
        workUntilIdle(finishingWorker(new CopyOnWriteArrayList<>()));
        assertThat(uri("d1")).isEqualTo("chroma://docs__plain-docs/a.md");
        assertThat(documentId("d1")).isEqualTo(id("plain-docs"));
    }

    // ── state B: the documents move is done, the satellites are open ─────────────────────────────────────

    @Test
    void documentsMoveDone_theOpenSatellitesAreSuperseded_andTheDocumentsMoveIsLeftAlone() {
        completeOldDocumentsMoveOnEmptyTables();
        assertThat(rowExists(ASPECTS_OLD)).as("staging: the finished documents move registered its satellites").isTrue();
        assertThat(completedAt(ASPECTS_OLD)).as("staging: and they are open").isNull();
        legacyRows(true);

        bootAgain();

        assertThat(supersededBy(DOCS_OLD)).as("a completed move is left alone").isNull();
        assertThat(rowExists(DOCS_NEW)).as("no replacement documents move").isFalse();
        assertThat(supersededBy(ASPECTS_OLD)).isEqualTo(ASPECTS_NEW);
        assertThat(supersededBy(HIGHLIGHTS_OLD)).isEqualTo(HIGHLIGHTS_NEW);
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();

        workUntilIdle(finishingWorker(seen));

        assertThat(seen).isEmpty();
        assertThat(aspectUri("d1")).isEqualTo("chroma://docs__plain-docs/a.md");
        assertThat(aspectId("d1")).as("the replacement set the id with source_uri").isEqualTo(id("plain-docs"));
        assertThat(highlightUri("d1")).isEqualTo("chroma://docs__plain-docs/a.md");
        assertThat(highlightId("d1")).isEqualTo(id("plain-docs"));
        assertThat(aspectUri("k1")).isEqualTo("chroma://plain-know/a-note");
        assertThat(aspectId("k1")).isNull();
        for (String m : List.of(ASPECTS_NEW, HIGHLIGHTS_NEW)) {
            assertThat(completedAt(m)).as(m).isNotNull();
        }
    }

    @Test
    void documentsMoveDone_andOneSatelliteDone_onlyTheOpenSatelliteIsSuperseded() {
        completeOldDocumentsMoveOnEmptyTables();
        // the highlights satellite finishes before P3.1 lands (its table holds nothing yet)
        ctx().update(MOVES).set(DSL.field(DSL.name("completed_at"), OffsetDateTime.class), NOW)
            .where(LABEL_F.eq(HIGHLIGHTS_OLD)).execute();
        legacyRows(true);

        bootAgain();

        assertThat(supersededBy(ASPECTS_OLD)).isEqualTo(ASPECTS_NEW);
        assertThat(supersededBy(HIGHLIGHTS_OLD)).as("complete: left alone").isNull();
        assertThat(rowExists(HIGHLIGHTS_NEW)).isFalse();
    }

    // ── state C: all done ────────────────────────────────────────────────────────────────────────────────

    @Test
    void allThreeDone_nothingIsRegisteredOrSuperseded() {
        completeOldDocumentsMoveOnEmptyTables();
        var w = finishingWorker(new CopyOnWriteArrayList<>());
        workUntilIdle(w);   // the satellites finish on the empty tables
        for (String m : List.of(DOCS_OLD, ASPECTS_OLD, HIGHLIGHTS_OLD)) {
            assertThat(completedAt(m)).as("staging: " + m).isNotNull();
        }
        legacyRows(true);
        var before = registry();

        bootAgain();

        assertThat(registry()).as("the registry is exactly as P2.10 left it").isEqualTo(before);
        assertThat(ctx().fetchCount(MOVES, SUPERSEDES_F.isNotNull())).as("nothing is superseded").isZero();
        assertThat(ctx().fetchCount(MOVES, LABEL_F.like("rdr229-p31-residue-%"))).as("nothing is registered").isZero();
    }

    // ── staging ──────────────────────────────────────────────────────────────────────────────────────────

    /** The shipped documents move, as catalog-049-2 registered it, put back (the base emptied the registry). */
    private void registerOld() {
        assertThat(shippedDocs).as("the walk registered " + DOCS_OLD).isNotNull();
        for (String old : List.of(DOCS_OLD, ASPECTS_OLD, HIGHLIGHTS_OLD, DOCS_NEW, ASPECTS_NEW, HIGHLIGHTS_NEW)) {
            for (String run : DataMoveRuns.activeRunLabels(ctx(), old)) {
                DataMoveRuns.archive(ctx(), run);   // dml_utils keeps one active run per label and resumes it
            }
        }
        ctx().insertInto(MOVES)
            .columns(LABEL_F, DSL.field(DSL.name("template"), String.class),
                DSL.field(DSL.name("driving_schema"), String.class), DSL.field(DSL.name("driving_table"), String.class),
                DSL.field(DSL.name("chunk_size"), Integer.class), DSL.field(DSL.name("sleep_ms"), Integer.class),
                DSL.field(DSL.name("statement_timeout_ms"), Integer.class),
                DSL.field(DSL.name("verification"), String.class), DSL.field(DSL.name("on_complete"), String.class),
                CHANGESET_F)
            .values(shippedDocs.get("label", String.class), shippedDocs.get("template", String.class),
                shippedDocs.get("driving_schema", String.class), shippedDocs.get("driving_table", String.class),
                shippedDocs.get("chunk_size", Integer.class), shippedDocs.get("sleep_ms", Integer.class),
                shippedDocs.get("statement_timeout_ms", Integer.class), shippedDocs.get("verification", String.class),
                shippedDocs.get("on_complete", String.class), shippedDocs.get("changeset_id", String.class))
            .execute();
    }

    /** Staging for the later states: P2.10's own moves run to the end over empty tables, so the registry holds what 0.1.200 left. */
    private void completeOldDocumentsMoveOnEmptyTables() {
        registerOld();
        clearRows();
        var w = finishingWorker(new CopyOnWriteArrayList<>());
        assertThat(w.runCycle()).isGreaterThanOrEqualTo(1);
        assertThat(completedAt(DOCS_OLD)).as("staging: the documents move finished").isNotNull();
    }

    /**
     * A boot after the registry is staged: the supersede changesets run again (their DATABASECHANGELOG rows are deleted), which is what
     * the tail of the P3.1 walk does to an install whose P2.10 moves are in the staged state.
     */
    private void bootAgain() {
        ctx().deleteFrom(DSL.table(DSL.name("public", "databasechangelog")))
            .where(DSL.field(DSL.name("id"), String.class).in(SUPERSEDE_CHANGESETS)).execute();
        installed();
    }

    private void clearRows() {
        for (String t : List.of("catalog_documents", "document_aspects", "document_highlights")) {
            ctx().deleteFrom(DSL.table(DSL.name("nexus", t))).where(DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT)).execute();
        }
        ctx().deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).execute();
    }

    private void collection(String name, String contentType) {
        PgContainerHelper.insertCollection(ctx(), TENANT, name);
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.CONTENT_TYPE, contentType)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
    }

    private void doc(String tumbler, String collection, String uri) {
        ctx().insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, tumbler).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, uri).execute();   // no id: a row as an upgrade leaves it
    }

    private void satellites(String tumbler, String collection, String uri) {
        ctx().insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, TENANT).set(DOCUMENT_ASPECTS.COLLECTION, collection)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, "p/" + tumbler).set(DOCUMENT_ASPECTS.EXTRACTED_AT, NOW)
            .set(DOCUMENT_ASPECTS.MODEL_VERSION, "test").set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test")
            .set(DOCUMENT_ASPECTS.DOC_ID, tumbler).set(DOCUMENT_ASPECTS.SOURCE_URI, uri).execute();
        ctx().insertInto(DOCUMENT_HIGHLIGHTS)
            .set(DOCUMENT_HIGHLIGHTS.TENANT_ID, TENANT).set(DOCUMENT_HIGHLIGHTS.DOC_ID, tumbler)
            .set(DOCUMENT_HIGHLIGHTS.COLLECTION, collection).set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, uri)
            .set(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, "- h").set(DOCUMENT_HIGHLIGHTS.INGESTED_AT, NOW).execute();
    }

    /**
     * Legacy rows: a name and no id, in the three tables. {@code documentsRepaired} stages the install whose documents move finished
     * before P3.1 (the documents carry the repaired segment and the satellites do not, yet).
     */
    private void legacyRows(boolean documentsRepaired) {
        clearRows();
        P31CheckReplay.lift(ctx(), P31CheckReplay.CATALOG_DOCUMENTS);
        P31CheckReplay.lift(ctx(), P31CheckReplay.DOCUMENT_ASPECTS);
        P31CheckReplay.lift(ctx(), P31CheckReplay.DOCUMENT_HIGHLIGHTS);
        try {
            collection("plain-docs", "docs");
            collection("plain-code", "code");
            collection("plain-know", "knowledge");
            String docsSegment = documentsRepaired ? "docs__plain-docs" : "plain-docs";
            String codeSegment = documentsRepaired ? "code__plain-code" : "plain-code";
            doc("d1", "plain-docs", "chroma://" + docsSegment + "/a.md");
            satellites("d1", "plain-docs", "chroma://plain-docs/a.md");
            doc("d2", "plain-code", "chroma://" + codeSegment + "/x/y.py");
            satellites("d2", "plain-code", "chroma://plain-code/x/y.py");
            doc("k1", "plain-know", "chroma://plain-know/a-note");
            satellites("k1", "plain-know", "chroma://plain-know/a-note");
            // held: a live document already holds the value d5 would get
            doc("d4", "plain-docs", "chroma://docs__plain-docs/held.md");
            doc("d5", "plain-docs", "chroma://plain-docs/held.md");
        } finally {
            P31CheckReplay.restore(ctx(), P31CheckReplay.CATALOG_DOCUMENTS);
            P31CheckReplay.restore(ctx(), P31CheckReplay.DOCUMENT_ASPECTS);
            P31CheckReplay.restore(ctx(), P31CheckReplay.DOCUMENT_HIGHLIGHTS);
        }
    }

    // ── reads ────────────────────────────────────────────────────────────────────────────────────────────

    private DataMoveWorker finishingWorker(List<FailureHandler.Failure> seen) {
        return worker(installed(), null, f -> {
            seen.add(f);
            return FailureHandler.Disposition.blocked(f.detail());
        }, new VerifyingFinisher());
    }

    private void workUntilIdle(DataMoveWorker w) {
        for (int i = 0; i < 8; i++) {
            if (w.runCycle() == 0) return;
        }
        throw new AssertionError("the registry never went idle");
    }

    private boolean rowExists(String label) {
        return ctx().fetchExists(MOVES, LABEL_F.eq(label));
    }

    /** The label of the move that supersedes {@code label}, or null. */
    private String supersededBy(String label) {
        return ctx().select(LABEL_F).from(MOVES).where(SUPERSEDES_F.eq(label)).fetchOne(LABEL_F);
    }

    /** Every move's identity columns, as one comparable text (the staged registry before and after a boot). */
    private String registry() {
        return ctx().select(LABEL_F, SUPERSEDES_F, DSL.field(DSL.name("completed_at"), OffsetDateTime.class), CHANGESET_F)
            .from(MOVES).orderBy(LABEL_F).fetch().formatCSV();
    }

    private Long id(String collection) {
        return ctx().select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
    }

    private String uri(String tumbler) {
        return ctx().select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
            .fetchOne(CATALOG_DOCUMENTS.SOURCE_URI);
    }

    private Long documentId(String tumbler) {
        return ctx().select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
            .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
    }

    private String aspectUri(String tumbler) {
        return ctx().select(DOCUMENT_ASPECTS.SOURCE_URI).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT)).and(DOCUMENT_ASPECTS.DOC_ID.eq(tumbler))
            .fetchOne(DOCUMENT_ASPECTS.SOURCE_URI);
    }

    private Long aspectId(String tumbler) {
        return ctx().select(DOCUMENT_ASPECTS.COLLECTION_ID).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT)).and(DOCUMENT_ASPECTS.DOC_ID.eq(tumbler))
            .fetchOne(DOCUMENT_ASPECTS.COLLECTION_ID);
    }

    private String highlightUri(String tumbler) {
        return ctx().select(DOCUMENT_HIGHLIGHTS.SOURCE_URI).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(tumbler))
            .fetchOne(DOCUMENT_HIGHLIGHTS.SOURCE_URI);
    }

    private Long highlightId(String tumbler) {
        return ctx().select(DOCUMENT_HIGHLIGHTS.COLLECTION_ID).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(tumbler))
            .fetchOne(DOCUMENT_HIGHLIGHTS.COLLECTION_ID);
    }
}
