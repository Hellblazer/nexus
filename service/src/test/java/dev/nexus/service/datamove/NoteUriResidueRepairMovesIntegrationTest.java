// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.DrivingTableIsolationCheck;
import dev.nexus.service.db.NoteUri;
import dev.nexus.service.db.TenantScope;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * RDR-229 P2.10 (nexus-p2m1x.15): the three RDR-246 data moves that catalog-049-2 registers, run by the engine's worker
 * over the SHIPPED registry row (the template, verification, {@code on_complete} and sizing the changeset's INSERT wrote:
 * the registry is emptied before each scenario, so the test re-inserts the row it read after the migration). The rule is
 * RDR-229 item 12: a {@code chroma://S/rest} value whose segment {@code S} has no {@code rdr__}, {@code docs__} or
 * {@code code__} prefix, on a row filed under {@code S} whose registry row has content type code, docs or rdr, becomes
 * {@code chroma://<content_type>__S/rest}; a row whose new value a live document holds is skipped, and so are its
 * satellites; the verification is the rows that still match the rule with a free new value.
 */
class NoteUriResidueRepairMovesIntegrationTest extends DataMoveIntegrationBase {

    static final String DOCS_MOVE = "rdr229-residue-catalog-documents";
    static final String ASPECTS_MOVE = "rdr229-residue-document-aspects";
    static final String HIGHLIGHTS_MOVE = "rdr229-residue-document-highlights";
    static final String CHANGESET = "catalog-049-2";

    private static final String TENANT_A = "residue-a";
    private static final String TENANT_B = "residue-b";
    private static final OffsetDateTime NOW = OffsetDateTime.parse("2026-11-20T00:00:00Z");

    private static final org.jooq.Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> LABEL_F = DSL.field(DSL.name("label"), String.class);

    /** What the migration registered, read before the first scenario empties the registry. */
    private static List<String> registeredAtMigration;
    private static Record shipped;

    @BeforeAll
    void readWhatTheMigrationRegistered() {
        // P2.10's own labels: D2's repair-catalog-collection-model-version (vectors-064) is registered by the same walk
        registeredAtMigration = ctx().select(LABEL_F).from(MOVES).where(LABEL_F.like("rdr229-residue-%"))
            .orderBy(LABEL_F).fetch(LABEL_F);
        shipped = ctx().select().from(MOVES).where(LABEL_F.eq(DOCS_MOVE)).fetchOne();
    }

    /** The base's reset emptied the registry; the move under test goes back in exactly as the changeset registered it. */
    @BeforeEach
    void reRegisterTheShippedMove() {
        // dml_utils keeps one active run per label and resumes it, so an earlier scenario's finished runs would make this
        // one's move run nothing: the labels are fixed (the shipped row's), so the runs are archived instead
        for (String label : List.of(DOCS_MOVE, ASPECTS_MOVE, HIGHLIGHTS_MOVE)) {
            for (String run : DataMoveRuns.activeRunLabels(ctx(), label)) {
                DataMoveRuns.archive(ctx(), run);
            }
        }
        assertThat(shipped).as("the migration registered " + DOCS_MOVE).isNotNull();
        ctx().insertInto(MOVES)
            .columns(LABEL_F, DSL.field(DSL.name("template"), String.class),
                DSL.field(DSL.name("driving_schema"), String.class), DSL.field(DSL.name("driving_table"), String.class),
                DSL.field(DSL.name("chunk_size"), Integer.class), DSL.field(DSL.name("sleep_ms"), Integer.class),
                DSL.field(DSL.name("statement_timeout_ms"), Integer.class),
                DSL.field(DSL.name("verification"), String.class), DSL.field(DSL.name("on_complete"), String.class),
                DSL.field(DSL.name("changeset_id"), String.class))
            .values(shipped.get("label", String.class), shipped.get("template", String.class),
                shipped.get("driving_schema", String.class), shipped.get("driving_table", String.class),
                shipped.get("chunk_size", Integer.class), shipped.get("sleep_ms", Integer.class),
                shipped.get("statement_timeout_ms", Integer.class), shipped.get("verification", String.class),
                shipped.get("on_complete", String.class), shipped.get("changeset_id", String.class))
            .execute();
        for (String t : List.of("catalog_documents", "document_aspects", "document_highlights")) {
            ctx().deleteFrom(DSL.table(DSL.name("nexus", t))).where(DSL.field(DSL.name("tenant_id"), String.class)
                .in(TENANT_A, TENANT_B)).execute();
        }
        ctx().deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.in(TENANT_A, TENANT_B)).execute();
    }

    // ── what the changeset registered ────────────────────────────────────────────────────────────────────

    @Test
    void theMigrationRegistersTheDocumentsMoveAlone_withItsSatellitesLeftToOnComplete() {
        assertThat(registeredAtMigration).as("registered by the migration: the first move only, never a boot rewrite")
            .containsExactly(DOCS_MOVE);
        assertThat(shipped.get("driving_table", String.class)).isEqualTo("catalog_documents");
        assertThat(shipped.get("changeset_id", String.class)).isEqualTo(CHANGESET);
        assertThat(shipped.get("supersedes", String.class)).isNull();
        assertThat(List.of(shipped.get("chunk_size", Integer.class), shipped.get("sleep_ms", Integer.class),
            shipped.get("statement_timeout_ms", Integer.class)))
            .as("the sizing measured on the production PITR fork 2026-11-20: chunk_size, sleep_ms, statement_timeout_ms")
            .containsExactly(1000, 100, 30_000);
        assertThat(shipped.get("on_complete", String.class)).contains(ASPECTS_MOVE).contains(HIGHLIGHTS_MOVE)
            .contains("'" + CHANGESET + "'");
        assertThat(shipped.get("completed_at")).as("open: the worker has not run it").isNull();
    }

    // ── the fixture ──────────────────────────────────────────────────────────────────────────────────────

    private void collection(String tenant, String name, String contentType) {
        PgContainerHelper.insertCollection(ctx(), tenant, name);
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.CONTENT_TYPE, contentType)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
    }

    private void doc(String tenant, String tumbler, String collection, String uri, boolean tombstoned) {
        ctx().insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, tumbler).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, uri)
            .set(CATALOG_DOCUMENTS.DELETED_AT, tombstoned ? NOW : null)
            .execute();
    }

    /** One aspect and one highlight row for the document, each carrying {@code uri} and filed under {@code collection}. */
    private void satellites(String tenant, String tumbler, String collection, String uri) {
        ctx().insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, tenant).set(DOCUMENT_ASPECTS.COLLECTION, collection)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, "p/" + tumbler).set(DOCUMENT_ASPECTS.EXTRACTED_AT, NOW)
            .set(DOCUMENT_ASPECTS.MODEL_VERSION, "test").set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test")
            .set(DOCUMENT_ASPECTS.DOC_ID, tumbler).set(DOCUMENT_ASPECTS.SOURCE_URI, uri).execute();
        ctx().insertInto(DOCUMENT_HIGHLIGHTS)
            .set(DOCUMENT_HIGHLIGHTS.TENANT_ID, tenant).set(DOCUMENT_HIGHLIGHTS.DOC_ID, tumbler)
            .set(DOCUMENT_HIGHLIGHTS.COLLECTION, collection).set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, uri)
            .set(DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, "- h").set(DOCUMENT_HIGHLIGHTS.INGESTED_AT, NOW).execute();
    }

    /** Every document of the fixture, with its satellites, for one tenant; returns nothing: the scenarios read by tumbler. */
    private void plant(String tenant) {
        collection(tenant, "plain-docs", "docs");
        collection(tenant, "plain-code", "code");
        collection(tenant, "plain-rdr", "rdr");
        collection(tenant, "plain-know", "knowledge");
        collection(tenant, "docs__prefixed__minilm-l6-v2-384__v1", "docs");

        doc(tenant, "d1", "plain-docs", "chroma://plain-docs/a.md", false);
        satellites(tenant, "d1", "plain-docs", "chroma://plain-docs/a.md");
        doc(tenant, "d2", "plain-code", "chroma://plain-code/x/y.py", false);
        satellites(tenant, "d2", "plain-code", "chroma://plain-code/x/y.py");
        doc(tenant, "d3", "plain-rdr", "chroma://plain-rdr/rdr-1.md", false);
        satellites(tenant, "d3", "plain-rdr", "chroma://plain-rdr/rdr-1.md");
        // a knowledge collection's note on a prefix-free label: left alone
        doc(tenant, "k1", "plain-know", "chroma://plain-know/a-note", false);
        satellites(tenant, "k1", "plain-know", "chroma://plain-know/a-note");
        // held: a live document already holds the value d5 would get
        doc(tenant, "d4", "plain-docs", "chroma://docs__plain-docs/held.md", false);
        doc(tenant, "d5", "plain-docs", "chroma://plain-docs/held.md", false);
        satellites(tenant, "d5", "plain-docs", "chroma://plain-docs/held.md");
        // a tombstoned row the rule covers; and a tombstoned holder does not hold a value
        doc(tenant, "d6", "plain-docs", "chroma://plain-docs/dead.md", true);
        doc(tenant, "d7", "plain-docs", "chroma://plain-docs/free.md", false);
        doc(tenant, "d7h", "plain-docs", "chroma://docs__plain-docs/free.md", true);
        // left alone: the segment is not the row's collection; a note; a file identity; an already prefixed segment
        doc(tenant, "o1", "plain-docs", "chroma://elsewhere/z.md", false);
        doc(tenant, "o2", "plain-docs", "nx-note://plain-docs/n", false);
        doc(tenant, "o3", "plain-docs", "file:///repo/a.md", false);
        doc(tenant, "o4", "docs__prefixed__minilm-l6-v2-384__v1", "chroma://docs__prefixed__minilm-l6-v2-384__v1/p.md", false);
        satellites(tenant, "o4", "docs__prefixed__minilm-l6-v2-384__v1", "chroma://docs__prefixed__minilm-l6-v2-384__v1/p.md");
        // a document filed under a collection that is not the segment's, and a collection with no registry row of a file type
        doc(tenant, "o5", "plain-code", "chroma://plain-docs/moved.md", false);
    }

    private String uri(String tenant, String tumbler) {
        return ctx().select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
            .fetchOne(CATALOG_DOCUMENTS.SOURCE_URI);
    }

    private String aspectUri(String tenant, String tumbler) {
        return ctx().select(DOCUMENT_ASPECTS.SOURCE_URI).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant)).and(DOCUMENT_ASPECTS.DOC_ID.eq(tumbler))
            .fetchOne(DOCUMENT_ASPECTS.SOURCE_URI);
    }

    private String highlightUri(String tenant, String tumbler) {
        return ctx().select(DOCUMENT_HIGHLIGHTS.SOURCE_URI).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(tumbler))
            .fetchOne(DOCUMENT_HIGHLIGHTS.SOURCE_URI);
    }

    /**
     * The rows a verification text returns table-wide, as the worker's table-wide run reads them: the admin role (the
     * migrating role, bound by FORCE row-level security) with NO nexus.tenant set. Reading through the superuser would
     * hide a table the admin role cannot see.
     */
    private long verifyRows(String verification) {
        try (var c = adminPool.getConnection()) {
            return MoveSql.countRows(c, verification, null, 1000);
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private DataMoveWorker finishingWorker(List<FailureHandler.Failure> seen) {
        return worker(installed(), null, f -> {
            seen.add(f);
            return FailureHandler.Disposition.blocked(f.detail());
        }, new VerifyingFinisher());
    }

    /** Works the registry until no move is open (the documents move completes first; its on_complete adds the other two). */
    private void workUntilIdle(DataMoveWorker w) {
        for (int i = 0; i < 6; i++) {
            if (w.runCycle() == 0) return;
        }
        throw new AssertionError("the registry never went idle");
    }

    // ── the repair ───────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aRenamedDocsCollectionsResidueGoesToADocsSegment_aKnowledgeNoteStaysPut_aHeldValueIsSkippedWithItsSatellites() {
        plant(TENANT_A);
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();
        var w = finishingWorker(seen);

        workUntilIdle(w);

        assertThat(seen).as("no failure, and no blocked verification: the held row is not counted").isEmpty();
        // repaired: the segment gains the content type's prefix
        assertThat(uri(TENANT_A, "d1")).isEqualTo("chroma://docs__plain-docs/a.md");
        assertThat(uri(TENANT_A, "d2")).isEqualTo("chroma://code__plain-code/x/y.py");
        assertThat(uri(TENANT_A, "d3")).isEqualTo("chroma://rdr__plain-rdr/rdr-1.md");
        assertThat(uri(TENANT_A, "d6")).as("a tombstone is repaired too").isEqualTo("chroma://docs__plain-docs/dead.md");
        assertThat(uri(TENANT_A, "d7")).as("a tombstoned holder holds nothing").isEqualTo("chroma://docs__plain-docs/free.md");
        // left alone
        assertThat(uri(TENANT_A, "k1")).as("a knowledge collection's note on a prefix-free label").isEqualTo("chroma://plain-know/a-note");
        assertThat(uri(TENANT_A, "d4")).isEqualTo("chroma://docs__plain-docs/held.md");
        assertThat(uri(TENANT_A, "d5")).as("the new value is held by a live document: skipped")
            .isEqualTo("chroma://plain-docs/held.md");
        assertThat(uri(TENANT_A, "o1")).as("the segment is not the row's collection").isEqualTo("chroma://elsewhere/z.md");
        assertThat(uri(TENANT_A, "o2")).isEqualTo("nx-note://plain-docs/n");
        assertThat(uri(TENANT_A, "o3")).isEqualTo("file:///repo/a.md");
        assertThat(uri(TENANT_A, "o4")).isEqualTo("chroma://docs__prefixed__minilm-l6-v2-384__v1/p.md");
        assertThat(uri(TENANT_A, "o5")).isEqualTo("chroma://plain-docs/moved.md");
        // satellites follow their document: repaired where it was, skipped where it was skipped or not a file type
        assertThat(aspectUri(TENANT_A, "d1")).isEqualTo("chroma://docs__plain-docs/a.md");
        assertThat(highlightUri(TENANT_A, "d1")).isEqualTo("chroma://docs__plain-docs/a.md");
        assertThat(aspectUri(TENANT_A, "d2")).isEqualTo("chroma://code__plain-code/x/y.py");
        assertThat(highlightUri(TENANT_A, "d3")).isEqualTo("chroma://rdr__plain-rdr/rdr-1.md");
        assertThat(aspectUri(TENANT_A, "d5")).as("its document was skipped for a held value").isEqualTo("chroma://plain-docs/held.md");
        assertThat(highlightUri(TENANT_A, "d5")).isEqualTo("chroma://plain-docs/held.md");
        assertThat(aspectUri(TENANT_A, "k1")).isEqualTo("chroma://plain-know/a-note");
        assertThat(highlightUri(TENANT_A, "k1")).isEqualTo("chroma://plain-know/a-note");
        assertThat(aspectUri(TENANT_A, "o4")).isEqualTo("chroma://docs__prefixed__minilm-l6-v2-384__v1/p.md");
        // all three moves completed; the satellites carry the parent's changeset_id
        for (String m : List.of(DOCS_MOVE, ASPECTS_MOVE, HIGHLIGHTS_MOVE)) {
            assertThat(completedAt(m)).as(m).isNotNull();
            assertThat(ctx().select(DSL.field(DSL.name("changeset_id"), String.class)).from(MOVES).where(LABEL_F.eq(m))
                .fetchOne(0, String.class)).as(m + " changeset_id").isEqualTo(CHANGESET);
        }
        assertThat(w.runCycle()).as("nothing is open: the held row does not keep a move open").isZero();
    }

    /**
     * An install WITH residue (linda's case a): the moves change rows, the file-routed segment is restored, so
     * {@code NoteUri.isNote} classes each repaired value as residue again, and the engine's stale-note repair, which skips
     * what {@code isNote} rejects, finds nothing to rewrite, merge or refuse.
     */
    @Test
    void anInstallWithResidue_theSegmentIsRestored_isNoteClassesItAsResidue_andTheStaleNoteRepairSkipsIt() {
        plant(TENANT_A);
        // the rows the rule covers, before: prefix-free residue, a note by the classifier
        for (String t : List.of("d1", "d2", "d3")) {
            assertThat(NoteUri.isNote(uri(TENANT_A, t))).as(t + " before").isTrue();
        }

        workUntilIdle(finishingWorker(new CopyOnWriteArrayList<>()));

        for (String t : List.of("d1", "d2", "d3", "d6", "d7")) {
            String after = uri(TENANT_A, t);
            assertThat(after).as(t).startsWith("chroma://");
            assertThat(NoteUri.isNote(after)).as(t + " after: file-routed residue, not a note").isFalse();
        }
        for (String after : List.of(aspectUri(TENANT_A, "d1"), highlightUri(TENANT_A, "d1"), aspectUri(TENANT_A, "d2"))) {
            assertThat(NoteUri.isNote(after)).as("satellite " + after).isFalse();
        }
        var repo = new CatalogRepository(new TenantScope(adminPool));
        var census = repo.repairStaleNoteUris(TENANT_A, false, 1000, 100);
        List<String> listed = new ArrayList<>();
        for (Object bucket : ((Map<?, ?>) census.get("sample")).values()) {
            for (Object row : (List<?>) bucket) {
                listed.add((String) ((Map<?, ?>) row).get("tumbler"));
            }
        }
        // the census does see the planted stale notes (o1 names another collection, o5 sits in one it does not name),
        // so it is not vacuous, and it lists none of the rows the moves repaired
        assertThat(listed).as("non-vacuity: the census lists the genuinely stale notes").contains("o1", "o5");
        assertThat(listed).as("the stale-note repair skips every repaired residue row")
            .doesNotContain("d1", "d2", "d3", "d6", "d7");
    }

    /**
     * An install with NO {@code chroma://} residue (linda's case b, the cloud's expected path): all three moves complete,
     * change no row, and their verification is empty. Pinned, not assumed.
     */
    @Test
    void anInstallWithNoChromaResidue_allThreeMovesCompleteChangingNothing_withAnEmptyVerification() {
        collection(TENANT_A, "plain-docs", "docs");
        collection(TENANT_A, "docs__prefixed__minilm-l6-v2-384__v1", "docs");
        collection(TENANT_A, "plain-know", "knowledge");
        doc(TENANT_A, "f1", "plain-docs", "file:///repo/a.md", false);
        satellites(TENANT_A, "f1", "plain-docs", "file:///repo/a.md");
        doc(TENANT_A, "n1", "plain-know", "nx-note://plain-know/a-note", false);
        satellites(TENANT_A, "n1", "plain-know", "nx-note://plain-know/a-note");
        doc(TENANT_A, "p1", "docs__prefixed__minilm-l6-v2-384__v1",
            "chroma://docs__prefixed__minilm-l6-v2-384__v1/p.md", false);
        satellites(TENANT_A, "p1", "docs__prefixed__minilm-l6-v2-384__v1",
            "chroma://docs__prefixed__minilm-l6-v2-384__v1/p.md");
        var before = snapshot(TENANT_A);
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();

        workUntilIdle(finishingWorker(seen));

        assertThat(seen).as("no failure, no blocked verification").isEmpty();
        for (String m : List.of(DOCS_MOVE, ASPECTS_MOVE, HIGHLIGHTS_MOVE)) {
            assertThat(completedAt(m)).as(m + " completed").isNotNull();
            assertThat(ctx().select(DSL.field(DSL.name("failed_at"), OffsetDateTime.class)).from(MOVES)
                .where(LABEL_F.eq(m)).fetchOne(0)).as(m + " not failed").isNull();
            String verification = ctx().select(DSL.field(DSL.name("verification"), String.class)).from(MOVES)
                .where(LABEL_F.eq(m)).fetchOne(0, String.class);
            assertThat(verifyRows(verification)).as(m + " verification").isZero();
        }
        assertThat(snapshot(TENANT_A)).as("zero rows changed, in any of the three tables").isEqualTo(before);
    }

    /**
     * The worker's table-wide verification runs as the admin role with no nexus.tenant, bound by FORCE row-level security:
     * it must SEE planted residue, for every move. It joins nexus.catalog_collections, so that table needs the same
     * owner-read policy as the three driving tables (vectors-065's, RDR-229 D2); without it the join is empty and the backstop
     * reports a table with residue clean.
     */
    @Test
    void theShippedVerificationsRunAsTheAdminRoleWithNoTenant_seePlantedResidue() {
        plant(TENANT_A);
        String docs = ctx().select(DSL.field(DSL.name("verification"), String.class)).from(MOVES)
            .where(LABEL_F.eq(DOCS_MOVE)).fetchOne(0, String.class);
        assertThat(verifyRows(docs)).as("documents: d1, d2, d3, d6, d7 are repairable").isEqualTo(5);

        workUntilIdle(finishingWorker(new ArrayList<>()));
        assertThat(verifyRows(docs)).as("after the repair").isZero();
        // a satellite left in the old shape beside its repaired document, for each satellite move
        ctx().update(DOCUMENT_ASPECTS).set(DOCUMENT_ASPECTS.SOURCE_URI, "chroma://plain-docs/a.md")
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT_A)).and(DOCUMENT_ASPECTS.DOC_ID.eq("d1")).execute();
        ctx().update(DOCUMENT_HIGHLIGHTS).set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, "chroma://plain-docs/a.md")
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT_A)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq("d1")).execute();
        for (String satellite : List.of(ASPECTS_MOVE, HIGHLIGHTS_MOVE)) {
            String verification = ctx().select(DSL.field(DSL.name("verification"), String.class)).from(MOVES)
                .where(LABEL_F.eq(satellite)).fetchOne(0, String.class);
            assertThat(verifyRows(verification)).as(satellite).isEqualTo(1);
        }
    }

    /**
     * catalog_collections carries an owner-read policy for the verifications, so the boot isolation check asks about it
     * too while a residue move is registered: it is listed beside the driving tables, and the policy engine's own question
     * (does a permissive policy other than tenant_isolation apply to the connected role?) finds the policy there.
     */
    @Test
    void theIsolationCheckAsksAboutTheRegistryTheVerificationsReadAsWellAsTheDrivingTables() throws Exception {
        var tables = DrivingTableIsolationCheck.registeredDrivingTables(ctx());
        assertThat(tables).extracting(DrivingTableIsolationCheck.Table::name)
            .containsExactlyInAnyOrder("catalog_documents", "catalog_collections");
        try (var c = adminPool.getConnection()) {
            var found = DrivingTableIsolationCheck.violations(DSL.using(c, org.jooq.SQLDialect.POSTGRES), tables);
            assertThat(found).as("non-vacuity: the admin role is the one the owner-read policies are for")
                .extracting(v -> v.table().name() + "." + v.policy())
                .contains("catalog_documents.catalog_documents_owner_read", "catalog_collections.catalog_collections_owner_read");
        }
    }

    /**
     * on_complete INSERTs the satellites with no ON CONFLICT, on purpose: a label already in the registry (hand-registered,
     * or another definition under the name) would otherwise be silently kept while the parent reports done, and a move
     * template that is not the shipped one would run unseen. The INSERT fails, on_complete and completed_at roll back
     * together, and the parent reports a loud ON_COMPLETE failure with the other row untouched.
     */
    @Test
    void aSatelliteLabelAlreadyRegistered_failsTheParentLoudly_andIsLeftUntouched() {
        plant(TENANT_A);
        DataMoveTestFixtures.register(ctx(), ASPECTS_MOVE, "tenant_plain", 50, 0);
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();
        var w = finishingWorker(seen);

        assertThat(w.runCycle()).isGreaterThanOrEqualTo(1);

        assertThat(seen).extracting(FailureHandler.Failure::stage).contains(FailureHandler.Stage.ON_COMPLETE);
        assertThat(completedAt(DOCS_MOVE)).as("on_complete and completed_at roll back together").isNull();
        assertThat(ctx().select(DSL.field(DSL.name("driving_table"), String.class)).from(MOVES)
            .where(LABEL_F.eq(ASPECTS_MOVE)).fetchOne(0, String.class)).as("the other row is untouched").isEqualTo("tenant_plain");
    }

    /** Every source_uri of the tenant's three tables, as one comparable text. */
    private String snapshot(String tenant) {
        return ctx().select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).orderBy(CATALOG_DOCUMENTS.TUMBLER).fetch().formatCSV()
            + ctx().select(DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI).from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(tenant)).orderBy(DOCUMENT_ASPECTS.DOC_ID).fetch().formatCSV()
            + ctx().select(DOCUMENT_HIGHLIGHTS.DOC_ID, DOCUMENT_HIGHLIGHTS.SOURCE_URI).from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant)).orderBy(DOCUMENT_HIGHLIGHTS.DOC_ID).fetch().formatCSV();
    }

    @Test
    void theRepairRunsPerTenant_andTheSecondTenantsRowsAreRepairedToo() {
        plant(TENANT_A);
        plant(TENANT_B);

        workUntilIdle(finishingWorker(new CopyOnWriteArrayList<>()));

        for (String tenant : List.of(TENANT_A, TENANT_B)) {
            assertThat(uri(tenant, "d1")).isEqualTo("chroma://docs__plain-docs/a.md");
            assertThat(aspectUri(tenant, "d1")).isEqualTo("chroma://docs__plain-docs/a.md");
            assertThat(uri(tenant, "d5")).isEqualTo("chroma://plain-docs/held.md");
        }
    }

    @Test
    void theVerificationOfEachMove_isTheRowsThatStillMatchTheRuleWithAFreeNewValue() {
        plant(TENANT_A);
        var w = finishingWorker(new ArrayList<>());
        workUntilIdle(w);
        // each shipped verification, run by hand over the finished state, finds nothing
        for (String m : List.of(DOCS_MOVE, ASPECTS_MOVE, HIGHLIGHTS_MOVE)) {
            String verification = ctx().select(DSL.field(DSL.name("verification"), String.class)).from(MOVES)
                .where(LABEL_F.eq(m)).fetchOne(0, String.class);
            assertThat(verification).as(m).doesNotContain("?").contains("$1");
            assertThat(verifyRows(verification)).as(m + " verification rows").isZero();
        }
        // a row put back in the old shape with a free new value is found by the documents verification
        ctx().update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "chroma://plain-docs/again.md")
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT_A)).and(CATALOG_DOCUMENTS.TUMBLER.eq("d1")).execute();
        String docsVerification = ctx().select(DSL.field(DSL.name("verification"), String.class)).from(MOVES)
            .where(LABEL_F.eq(DOCS_MOVE)).fetchOne(0, String.class);
        assertThat(verifyRows(docsVerification)).isEqualTo(1);
        // d5 is in the old shape too, and its new value is held by d4: it is not counted, so the count is d1 alone
        }

    @Test
    void theShippedMoveIsRegisteredWithItsSizing_andTheSatellitesAreRegisteredWithTheSame() {
        plant(TENANT_A);
        workUntilIdle(finishingWorker(new ArrayList<>()));
        Map<String, Record> rows = new java.util.TreeMap<>();
        ctx().select().from(MOVES).fetch().forEach(r -> rows.put(r.get("label", String.class), r));
        assertThat(rows.keySet()).containsExactly(DOCS_MOVE, ASPECTS_MOVE, HIGHLIGHTS_MOVE);
        for (Record r : rows.values()) {
            assertThat(r.get("supersedes", String.class)).isNull();
            assertThat(r.get("changeset_id", String.class)).isEqualTo(CHANGESET);
        }
        for (String satellite : List.of(ASPECTS_MOVE, HIGHLIGHTS_MOVE)) {
            Record r = rows.get(satellite);
            assertThat(List.of(r.get("chunk_size", Integer.class), r.get("sleep_ms", Integer.class),
                r.get("statement_timeout_ms", Integer.class)))
                .as(satellite + " sizing (the PITR-fork measurement)").containsExactly(1000, 50, 30_000);
        }
        assertThat(rows.get(ASPECTS_MOVE).get("on_complete", String.class)).isNull();
        assertThat(rows.get(HIGHLIGHTS_MOVE).get("on_complete", String.class)).isNull();
        assertThat(rows.get(DOCS_MOVE).get("driving_table", String.class)).isEqualTo("catalog_documents");
        assertThat(rows.get(ASPECTS_MOVE).get("driving_table", String.class)).isEqualTo("document_aspects");
        assertThat(rows.get(HIGHLIGHTS_MOVE).get("driving_table", String.class)).isEqualTo("document_highlights");
    }
}
