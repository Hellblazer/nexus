// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RetiredAspectQueueTable;
import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import java.util.Set;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): the boot-time completion check, {@link CompletionCheck}, against a real PostgreSQL walked by
 * the product changelog. It reads the registry ({@code nexus.data_moves}), the catalog and the CHECK constraints, and reports
 * three gates; it writes nothing (a case runs it in a READ ONLY transaction).
 *
 * <p>The rules under test: complete means {@code completed_at}, or the superseding move's; a label with no row is incomplete,
 * except the one allowlisted queue label (residual R3); a failed move whose driving table is gone is complete; a stamp label is
 * complete only while its table's CHECKs are present and validated, so an orphan that keeps a CHECK NOT VALID holds the label
 * back and is counted; every gate refuses while {@code catalog_collections_superseded_by_id_check} is missing (linda's ruling
 * 2026-11-21, item c); gate 3 is defined on the registry and VALID id indexes (S-2). The read set is small in most cases so a
 * case validates only the tables it names; the last case runs P3.1's whole set.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CompletionCheckIntegrationTest {

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final String TENANT = "p31-cc";
    private static final String COLLECTION = "code__p31-cc__voyage-code-3__v1";
    private static final String QUEUE = "aspect_extraction_queue";
    private static final String QUEUE_LABEL = StampLabels.labelFor(QUEUE);
    private static final String FK_QUEUE_LABEL = FkTwins.PREFIX + QUEUE;
    private static final String DOCS_LABEL = StampLabels.labelFor("catalog_documents");
    private static final String DOCS_CHECK = "catalog_documents_physical_collection_id_check";

    private PostgreSQLContainer<?> pg;
    private Connection su;
    private DSLContext ctx;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
    }

    @AfterAll
    void stop() throws Exception {
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void emptyRegistry() {
        ctx.deleteFrom(MOVES).execute();
    }

    // ---- fixtures ----------------------------------------------------------------------------------------------------

    private static Field<String> text(String name) {
        return DSL.field(DSL.name(name), String.class);
    }

    private static Field<OffsetDateTime> stamp(String name) {
        return DSL.field(DSL.name(name), SQLDataType.TIMESTAMPWITHTIMEZONE);
    }

    /** A registered move as a changeset writes it: open until a test completes or fails it. */
    private void register(String label, String drivingTable, String supersedes) {
        ctx.insertInto(MOVES)
            .set(text("label"), label).set(text("template"), "t")
            .set(text("driving_schema"), "nexus").set(text("driving_table"), drivingTable)
            .set(DSL.field(DSL.name("chunk_size"), Integer.class), 100)
            .set(DSL.field(DSL.name("sleep_ms"), Integer.class), 0)
            .set(DSL.field(DSL.name("statement_timeout_ms"), Integer.class), 60_000)
            .set(text("verification"), "v").set(text("changeset_id"), "test")
            .set(text("supersedes"), supersedes)
            .execute();
    }

    private void complete(String label) {
        ctx.update(MOVES).set(stamp("completed_at"), OffsetDateTime.now()).where(text("label").eq(label)).execute();
    }

    private void fail(String label) {
        ctx.update(MOVES).set(stamp("failed_at"), OffsetDateTime.now()).set(text("failure"), "42P01")
            .where(text("label").eq(label)).execute();
    }

    private CompletionCheck.Result run(CompletionCheck.ReadSet readSet) {
        return CompletionCheck.evaluate(ctx, readSet, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
    }

    private static CompletionCheck.ReadSet gate1(String... labels) {
        return new CompletionCheck.ReadSet(List.of(labels), List.of());
    }

    private CompletionCheck.LabelState state(CompletionCheck.Result r, String label) {
        return r.labels().stream().filter(l -> l.label().equals(label)).findFirst().orElseThrow();
    }

    /** The twin's changeset replayed: rollback drops it, forward adds it back NOT VALID. The changeset's own text, so no DDL string lives here. */
    private void dropTwin(String table) {
        replayTwin(table, true);
    }

    private void addTwin(String table) {
        replayTwin(table, false);
    }

    private void replayTwin(String table, boolean rollback) {
        ctx.connection(c -> {
            try {
                String id = FkTwinSpecs.ddlChangeset(FkTwinSpecs.indexOf(table));
                if (rollback) {
                    PgContainerHelper.replayChangesetRollback(c, FkTwinSpecs.CHANGELOG, id);
                } else {
                    PgContainerHelper.replayChangeset(c, FkTwinSpecs.CHANGELOG, id);
                }
            } catch (java.sql.SQLException | RuntimeException e) {
                throw e;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
    }

    private void validate(String table, String constraint) throws Exception {
        PgContainerHelper.validateConstraint(su, DSL.table(DSL.name("nexus", table)), constraint);
    }

    // ---- the read set ------------------------------------------------------------------------------------------------

    @Test
    void theStampLabelsAreOnePerTableThatCarriesACheck_andHaveNoSlash() {
        List<String> labels = StampLabels.all();
        assertThat(labels).as("non-vacuity: the 16 tables of p31-checks.tsv that carry a CHECK").hasSize(16)
            .doesNotHaveDuplicates().contains(QUEUE_LABEL, DOCS_LABEL, StampLabels.labelFor("chunks"));
        assertThat(labels).allSatisfy(l -> {
            assertThat(l).doesNotContain("/");   // data_moves_label_check
            assertThat(StampLabels.tableOf(l)).isPresent();
        });
        assertThat(StampLabels.tableOf("rdr229-stamp-relevance_log")).as("a history table has no stamp").isEmpty();
        assertThat(StampLabels.tableOf("something-else")).isEmpty();
        assertThat(CompletionCheck.ReadSet.p31().gate1Labels()).as("the read set is the stamp labels").isEqualTo(labels);
        assertThat(CompletionCheck.ReadSet.p31().gate2Labels()).as("gate 2 adds nothing in P3.1's read set").isEmpty();
    }

    // ---- an empty registry (R3 a) ------------------------------------------------------------------------------------

    @Test
    void anEmptyRegistry_everyLabelIsIncompleteButTheQueueOne_noGateHolds() {
        CompletionCheck.Result r = run(CompletionCheck.ReadSet.p31());
        assertThat(r.labels()).hasSize(16);
        assertThat(r.incomplete()).as("every label but the allowlisted queue one has no row").hasSize(15)
            .allSatisfy(l -> assertThat(l.why()).isEqualTo("no row in nexus.data_moves"));
        assertThat(state(r, QUEUE_LABEL).status()).as("the queue table is absent on a fresh install: nothing to stamp (R3 a)")
            .isEqualTo(CompletionCheck.Status.NOT_APPLICABLE);
        assertThat(r.gate1()).isFalse();
        assertThat(r.gate2()).isFalse();
        assertThat(r.gate3()).isFalse();
        assertThat(r.supersededByCheckPresent()).as("the walk adds the CHECK: no tombstone waits").isTrue();
    }

    @Test
    void aLabelWithNoRowStaysIncomplete_theQueueExceptionIsAnAllowlistOfOneLabel() {
        // an extension label that is not on the allowlist is incomplete when it has no row.
        CompletionCheck.Result r = run(gate1("rdr229-p33c-swap-validation", "rdr229-stamp-nonexistent"));
        assertThat(r.incomplete()).extracting(CompletionCheck.LabelState::label)
            .containsExactly("rdr229-p33c-swap-validation", "rdr229-stamp-nonexistent");
        assertThat(r.gate1()).isFalse();
        assertThat(StampLabels.NOT_APPLICABLE_WHEN_ABSENT).as("the allowlist is exactly the queue's two labels: its stamp and its P3.3a twin validation")
            .containsOnlyKeys(QUEUE_LABEL, FK_QUEUE_LABEL);
    }

    @Test
    void theQueueLabelAloneHoldsGate1OnAFreshInstall_noQueueMoveIsRegistered() {
        CompletionCheck.Result r = run(gate1(QUEUE_LABEL));
        assertThat(ctx.fetchCount(MOVES)).as("no queue move is registered: nothing registers one when the table is absent").isZero();
        assertThat(r.gate1()).isTrue();
        assertThat(r.gate2()).isTrue();
        assertThat(r.gate3()).as("P3.4 builds no id index in this release").isFalse();
        assertThat(r.gate3Why()).contains("P3.4");
    }

    // ---- complete and superseded -------------------------------------------------------------------------------------

    @Test
    void aSupersededMoveIsCompleteWhenTheMoveThatSupersedesItIs() {
        register("ext-old", "chunks", null);
        fail("ext-old");
        register("ext-new", "chunks", "ext-old");
        assertThat(state(run(gate1("ext-old")), "ext-old").status()).as("the superseder is open").isEqualTo(CompletionCheck.Status.INCOMPLETE);
        complete("ext-new");
        assertThat(state(run(gate1("ext-old")), "ext-old").status()).as("the superseder is complete").isEqualTo(CompletionCheck.Status.COMPLETE);
        assertThat(state(run(gate1("ext-new")), "ext-new").status()).isEqualTo(CompletionCheck.Status.COMPLETE);
    }

    @Test
    void aFailedMoveIsIncompleteWhileItsDrivingTableExists_andCompleteOnceTheTableIsGone() {
        register("ext-failed", "chunks", null);
        fail("ext-failed");
        assertThat(state(run(gate1("ext-failed")), "ext-failed").status()).isEqualTo(CompletionCheck.Status.INCOMPLETE);
        // R3 c: failed with 42P01 because the driving table is gone.
        register("ext-gone", "table_that_was_dropped", null);
        fail("ext-gone");
        assertThat(state(run(gate1("ext-gone")), "ext-gone").status()).isEqualTo(CompletionCheck.Status.COMPLETE);
    }

    @Test
    void theQueueMoveFailedOnADroppedTableCountsComplete_r3c() {
        register(QUEUE_LABEL, QUEUE, null);
        fail(QUEUE_LABEL);
        assertThat(state(run(gate1(QUEUE_LABEL)), QUEUE_LABEL).status()).isEqualTo(CompletionCheck.Status.COMPLETE);
    }

    // ---- the queue table kept (R3 b) and a missing column (item 4) ------------------------------------------------------

    @Test
    void aKeptQueueTable_itsMoveIsCountedAndCompletesOnceItsCheckIsValidated_r3b() throws Exception {
        RetiredAspectQueueTable.restore(ctx);
        try {
            PgContainerHelper.replayChangeset(su, P31CheckReplay.CHANGELOG, "vectors-068-18");
            CompletionCheck.Result noRow = run(gate1(QUEUE_LABEL));
            assertThat(state(noRow, QUEUE_LABEL).status()).as("the table and its id column exist: no row means not yet registered")
                .isEqualTo(CompletionCheck.Status.INCOMPLETE);
            assertThat(noRow.gate1()).isFalse();

            register(QUEUE_LABEL, QUEUE, null);
            complete(QUEUE_LABEL);
            CompletionCheck.Result notValid = run(gate1(QUEUE_LABEL));
            assertThat(state(notValid, QUEUE_LABEL).why()).contains("aspect_extraction_queue_collection_id_check is NOT VALID");
            validate(QUEUE, "aspect_extraction_queue_collection_id_check");
            CompletionCheck.Result done = run(gate1(QUEUE_LABEL));
            assertThat(state(done, QUEUE_LABEL).status()).isEqualTo(CompletionCheck.Status.COMPLETE);
            assertThat(done.gate1()).isTrue();
        } finally {
            RetiredAspectQueueTable.drop(ctx);
        }
    }

    @Test
    void aKeptQueueTableWithoutTheIdColumn_isNotApplicable_neverAFailedStamp_item4() throws Exception {
        // vectors-068-18 is skipped, unrecorded, where another role owns the kept table: the table is back, the column is not.
        RetiredAspectQueueTable.restore(ctx);
        try {
            assertThat(CompletionCheck.columnExists(ctx, QUEUE, "collection_id")).as("the precondition of the case").isFalse();
            assertThat(state(run(gate1(QUEUE_LABEL)), QUEUE_LABEL).status()).as("no row, no column: nothing to stamp")
                .isEqualTo(CompletionCheck.Status.NOT_APPLICABLE);
            register(QUEUE_LABEL, QUEUE, null);
            fail(QUEUE_LABEL);   // a stamp that ran into the missing column would fail like this
            CompletionCheck.LabelState failed = state(run(gate1(QUEUE_LABEL)), QUEUE_LABEL);
            assertThat(failed.status()).isEqualTo(CompletionCheck.Status.NOT_APPLICABLE);
            assertThat(failed.why()).contains("has no collection_id column");
            assertThat(run(gate1(QUEUE_LABEL)).gate1()).isTrue();
        } finally {
            RetiredAspectQueueTable.drop(ctx);
        }
    }

    // ---- a stamp is complete only with its CHECKs validated; the orphan path ----------------------------------------------

    @Test
    void anOrphanKeepsTheCheckNotValid_theLabelWaitsAndTheRowsAreCounted_thenItValidatesOnceTheyAreGone() throws Exception {
        PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
        Table<?> docs = DSL.table(DSL.name("nexus", "catalog_documents"));
        Field<String> tumbler = text("tumbler");
        // Rows written before the CHECK existed: the orphan names no registry row, the unstamped one names a registered
        // collection the stamp has not reached, and '' is "unset" (NULLIF rule), never an orphan.
        P31CheckReplay.lift(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        try {
            insertDoc(docs, "1.7.1", "ghost-collection-p31-cc");
            insertDoc(docs, "1.7.2", COLLECTION);
            insertDoc(docs, "1.7.3", "");
        } finally {
            P31CheckReplay.restore(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        }
        try {
            register(DOCS_LABEL, "catalog_documents", null);
            complete(DOCS_LABEL);

            CompletionCheck.Result waiting = run(gate1(DOCS_LABEL));
            assertThat(waiting.gate1()).isFalse();
            CompletionCheck.LabelState label = state(waiting, DOCS_LABEL);
            assertThat(label.status()).as("the move is done, the CHECK is NOT VALID").isEqualTo(CompletionCheck.Status.INCOMPLETE);
            assertThat(label.why()).as("the why carries no count: it goes out on the unauthenticated /v1/status").isEqualTo("CHECK " + DOCS_CHECK + " is NOT VALID");
            assertThat(waiting.violators()).singleElement().satisfies(v -> {
                assertThat(v.table()).isEqualTo("catalog_documents");
                assertThat(v.constraint()).isEqualTo(DOCS_CHECK);
                assertThat(v.orphans()).as("one orphan; the '' row is not one").isEqualTo(1);
                assertThat(v.unstamped()).isEqualTo(1);
                assertThat(v.counted()).isTrue();
            });
            assertThat(waiting.checks()).anySatisfy(c -> {
                assertThat(c.constraint()).isEqualTo(DOCS_CHECK);
                assertThat(c.present()).isTrue();
                assertThat(c.validated()).isFalse();
            });

            // The check READS: the same evaluation, counting included, in a READ ONLY transaction (a write raises 25006).
            try (Connection ro = pg.createConnection("")) {
                ro.setReadOnly(true);
                ro.setAutoCommit(false);
                CompletionCheck.Result readOnly = CompletionCheck.evaluate(DSL.using(ro, SQLDialect.POSTGRES), gate1(DOCS_LABEL),
                    CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
                ro.rollback();
                assertThat(readOnly.violators()).singleElement().satisfies(v -> assertThat(v.orphans()).isEqualTo(1));
            }

            // The rows go away: the CHECK is still NOT VALID until someone validates it, which is the VALIDATE that waited.
            ctx.deleteFrom(docs).where(tumbler.in("1.7.1", "1.7.2")).execute();
            CompletionCheck.Result drained = run(gate1(DOCS_LABEL));
            assertThat(state(drained, DOCS_LABEL).why()).isEqualTo("CHECK " + DOCS_CHECK + " is NOT VALID");
            validate("catalog_documents", DOCS_CHECK);   // passes with the '' row still there
            assertThat(state(run(gate1(DOCS_LABEL)), DOCS_LABEL).status()).isEqualTo(CompletionCheck.Status.COMPLETE);
            assertThat(run(gate1(DOCS_LABEL)).gate1()).isTrue();
        } finally {
            ctx.deleteFrom(docs).where(text("tenant_id").eq(TENANT)).execute();
        }
    }

    private void insertDoc(Table<?> docs, String tumbler, String physicalCollection) {
        ctx.insertInto(docs).set(text("tenant_id"), TENANT).set(text("tumbler"), tumbler).set(text("title"), "t")
            .set(text("physical_collection"), physicalCollection).execute();
    }

    @Test
    void aStampLabelWhoseCheckIsMissingIsIncomplete_evenWithTheMoveDone() throws Exception {
        PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
        String label = StampLabels.labelFor("document_aspects");
        register(label, "document_aspects", null);
        complete(label);
        P31CheckReplay.lift(ctx, P31CheckReplay.DOCUMENT_ASPECTS);
        try {
            CompletionCheck.LabelState s = state(run(gate1(label)), label);
            assertThat(s.status()).isEqualTo(CompletionCheck.Status.INCOMPLETE);
            assertThat(s.why()).isEqualTo("CHECK document_aspects_collection_id_check is missing");
        } finally {
            P31CheckReplay.restore(ctx, P31CheckReplay.DOCUMENT_ASPECTS);
        }
        // restored NOT VALID: still incomplete, now for that reason
        assertThat(state(run(gate1(label)), label).why()).contains("is NOT VALID");
    }

    // ---- item c: every gate refuses while the superseded_by CHECK is missing -------------------------------------------

    @Test
    void everyGateRefusesWhileTheSupersededByCheckIsMissing_andTheWaitingRowsAreCounted() throws Exception {
        PgContainerHelper.insertCollection(ctx, TENANT, "tomb-p31-cc");
        P31CheckReplay.lift(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
        Field<String> name = text("name");
        Table<?> registry = DSL.table(DSL.name("nexus", "catalog_collections"));
        try {
            // a tombstone with a blank model_version and no id: what vectors-068-19's precondition waits on
            ctx.update(registry).set(text("superseded_by"), "successor-gone").set(text("model_version"), "")
                .where(name.eq("tomb-p31-cc")).execute();
            CompletionCheck.Result r = CompletionCheck.evaluate(ctx, new CompletionCheck.ReadSet(List.of(), List.of()),
                everyIndexValid(), CompletionCheck.DIRECT);
            assertThat(r.supersededByCheckPresent()).isFalse();
            assertThat(r.supersededByWaiting()).isEqualTo(1);
            assertThat(r.gate1()).as("nothing to wait on but the CHECK").isFalse();
            assertThat(r.gate2()).isFalse();
            assertThat(r.gate3()).as("not even with every id index valid").isFalse();
            assertThat(r.checks()).anySatisfy(c -> {
                assertThat(c.constraint()).isEqualTo(CompletionCheck.SUPERSEDED_BY_CHECK);
                assertThat(c.present()).isFalse();
            });
            // the stamp fills the id (R7: a tombstone whose successor is gone takes its own); the CHECK lands on the next boot
            ctx.update(registry).set(DSL.field(DSL.name("superseded_by_id"), Long.class), DSL.field(DSL.name("collection_id"), Long.class))
                .where(name.eq("tomb-p31-cc")).execute();
            assertThat(CompletionCheck.evaluate(ctx, new CompletionCheck.ReadSet(List.of(), List.of()), everyIndexValid(),
                CompletionCheck.DIRECT).supersededByWaiting()).as("the waiting count follows the data").isZero();
        } finally {
            P31CheckReplay.restore(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
            ctx.deleteFrom(registry).where(name.eq("tomb-p31-cc")).execute();
        }
        CompletionCheck.Result back = CompletionCheck.evaluate(ctx, new CompletionCheck.ReadSet(List.of(), List.of()),
            everyIndexValid(), CompletionCheck.DIRECT);
        assertThat(back.supersededByCheckPresent()).isTrue();
        assertThat(back.gate1()).isTrue();
        assertThat(back.gate2()).isTrue();
        assertThat(back.gate3()).isTrue();
    }

    // ---- gate 3 (S-2): defined on the registry and VALID id indexes -------------------------------------------------------

    private static CompletionCheck.Gate3Source everyIndexValid() {
        return c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("a", "b"), Set.of("a", "b"), Set.of()));
    }

    private CompletionCheck.Result withGate3(CompletionCheck.Gate3Source source) {
        return CompletionCheck.evaluate(ctx, new CompletionCheck.ReadSet(List.of(), List.of()), source, CompletionCheck.DIRECT);
    }

    @Test
    void gate3RefusesWhileAnyPlannedCollectionHasNoValidIdIndex_missingOrInvalid() {
        // a collection with no id index at all, one whose id index is INVALID, and one that is fine
        CompletionCheck.Result r = withGate3(c -> Optional.of(new CompletionCheck.IdIndexCoverage(
            Set.of("fine", "missing", "invalid"), Set.of("fine"), Set.of("invalid"))));
        assertThat(r.gate2()).isTrue();
        assertThat(r.gate3()).isFalse();
        assertThat(r.gate3Uncovered()).containsExactly("invalid", "missing");
        assertThat(r.gate3Why()).contains("collections that hold a VALID name-predicate index or an id-predicate index have no VALID id-predicate index").doesNotMatch("(?s).*\\d.*");

        // the vacuous pass the planner's drop-before-build would give: a collection whose NAME index is gone and whose id
        // index is not built is still planned, and still uncovered
        assertThat(withGate3(c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("c"), Set.of(), Set.of()))).gate3()).isFalse();
    }

    private void emptyCollectionRegistry() {
        ctx.deleteFrom(DSL.table(DSL.name("nexus", "catalog_collections"))).where(text("tenant_id").eq(TENANT)).execute();
    }

    @Test
    void gate3HoldsWhenEveryPlannedCollectionIsCovered_orNoneIsPlannedAndTheRegistryIsEmpty_andNeverWithoutAReader() {
        emptyCollectionRegistry();
        assertThat(withGate3(everyIndexValid()).gate3()).isTrue();
        assertThat(withGate3(c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of(), Set.of(), Set.of()))).gate3())
            .as("nothing to index, and no collection registered: covered").isTrue();
        // an id index no planner wants does not matter; a planned one must be there
        assertThat(withGate3(c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("a"), Set.of("a", "stale"), Set.of()))).gate3()).isTrue();
        assertThat(withGate3(CompletionCheck.Gate3Source.NOT_INSTALLED).gate3()).as("no id-index reader in this release").isFalse();
    }

    /** Review F5: an empty planned set is a pass only when there is nothing to plan (the registry is empty too). */
    @Test
    void gate3DoesNotPassVacuously_whenNothingIsPlannedButTheRegistryHoldsLiveCollections() {
        PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
        try {
            CompletionCheck.Result r = withGate3(c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of(), Set.of(), Set.of())));
            assertThat(r.gate2()).as("non-vacuity: gate 3 was asked").isTrue();
            assertThat(r.gate3()).isFalse();
            assertThat(r.gate3Why()).contains("no collection requires an id-predicate index").contains("live collections are registered").doesNotMatch("(?s).*\\d+ live.*");
            // a planned set that is covered still holds: the registry only vetoes the EMPTY plan
            assertThat(withGate3(c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of(COLLECTION), Set.of(COLLECTION), Set.of()))).gate3())
                .isTrue();
        } finally {
            emptyCollectionRegistry();
        }
    }

    @Test
    void gate3IsNotAskedWhileGate2DoesNotHold() {
        CompletionCheck.Result r = CompletionCheck.evaluate(ctx, gate1("ext-open-label"),
            c -> { throw new AssertionError("gate 3 must not be read before gate 2 holds"); }, CompletionCheck.DIRECT);
        assertThat(r.gate2()).isFalse();
        assertThat(r.gate3()).isFalse();
        assertThat(r.gate3Why()).isEqualTo("gate 2 does not hold");
    }

    /** RDR-229 P3.4 (linda 2026-11-22): gate 3 implies gate 2 structurally; a swap or validation label that is open keeps it closed. */
    @Test
    void gate3IsFalseWhileOneGate2LabelIsIncomplete_whateverEveryIdIndexSays_andOpensWhenItCompletes() {
        register("ext-g3-gate1", "chunks", null);
        complete("ext-g3-gate1");
        register("ext-g3-swap", "chunks", null);
        complete("ext-g3-swap");
        register("ext-g3-validation", "chunks", null);   // the one open label: a child validation move
        CompletionCheck.ReadSet rs = new CompletionCheck.ReadSet(List.of("ext-g3-gate1"), List.of("ext-g3-swap", "ext-g3-validation"));
        boolean[] asked = {false};
        CompletionCheck.Gate3Source valid = c -> {
            asked[0] = true;
            return Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("a", "b"), Set.of("a", "b"), Set.of()));
        };

        CompletionCheck.Result open = CompletionCheck.evaluate(ctx, rs, valid, CompletionCheck.DIRECT);

        assertThat(open.gate1()).as("non-vacuity: gate 1 holds").isTrue();
        assertThat(open.gate2()).isFalse();
        assertThat(open.gate3()).as("every id index valid, one gate 2 label open").isFalse();
        assertThat(asked[0]).as("the PCI coverage is not even read").isFalse();
        complete("ext-g3-validation");
        CompletionCheck.Result done = CompletionCheck.evaluate(ctx, rs, valid, CompletionCheck.DIRECT);
        assertThat(done.gate2()).isTrue();
        assertThat(done.gate3()).isTrue();
    }

    /** The seam for IdUniqueIndexPass.allValid: each extra condition must hold, and one that throws holds nothing. */
    @Test
    void gate3ExtraConditionsAreRead_afterGate2AndTheCoverage_andEveryOneMustHold() {
        CompletionCheck.ReadSet rs = new CompletionCheck.ReadSet(List.of(), List.of());
        java.util.function.Predicate<org.jooq.DSLContext> yes = c -> true;
        java.util.function.Predicate<org.jooq.DSLContext> no = c -> false;
        java.util.function.Predicate<org.jooq.DSLContext> boom = c -> { throw new IllegalStateException("catalog unreadable"); };

        assertThat(CompletionCheck.evaluate(ctx, rs, everyIndexValid(), CompletionCheck.DIRECT, List.of(yes, yes)).gate3()).isTrue();
        assertThat(CompletionCheck.evaluate(ctx, rs, everyIndexValid(), CompletionCheck.DIRECT, List.of()).gate3()).isTrue();
        var refused = CompletionCheck.evaluate(ctx, rs, everyIndexValid(), CompletionCheck.DIRECT, List.of(yes, no));
        assertThat(refused.gate3()).isFalse();
        assertThat(refused.gate3Why()).contains("further gate 3 condition");
        assertThat(CompletionCheck.evaluate(ctx, rs, everyIndexValid(), CompletionCheck.DIRECT, List.of(boom)).gate3()).as("unknown is not holds").isFalse();
        // not read while the coverage is incomplete
        boolean[] read = {false};
        CompletionCheck.evaluate(ctx, rs, c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("x"), Set.of(), Set.of())),
            CompletionCheck.DIRECT, List.of(c -> { read[0] = true; return true; }));
        assertThat(read[0]).isFalse();
    }

    /** linda 2026-11-22: with per-collection indexing off the PCI clause is not applicable; gate 3 rests on the rest, the extras included. */
    @Test
    void withPciOffTheClauseIsNotApplicable_gate3ReachesHolding_andTheExtrasStillApply() {
        PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
        try {
            CompletionCheck.ReadSet rs = new CompletionCheck.ReadSet(List.of(), List.of());
            CompletionCheck.Result r = CompletionCheck.evaluate(ctx, rs, c -> Optional.of(CompletionCheck.IdIndexCoverage.pciOff()),
                CompletionCheck.DIRECT, List.of());
            assertThat(r.gate2()).as("non-vacuity: gate 2 holds and a live collection is registered").isTrue();
            assertThat(r.gate3()).as("a registered live collection and no PCI: not the empty-plan veto").isTrue();
            assertThat(r.gate3PciNotApplicable()).isTrue();
            assertThat(CompletionBootWork.summary(r)).contains("per-collection indexing is off");
            CompletionCheck.Result refused = CompletionCheck.evaluate(ctx, rs,
                c -> Optional.of(CompletionCheck.IdIndexCoverage.pciOff()), CompletionCheck.DIRECT, List.of(c -> false));
            assertThat(refused.gate3()).as("the id unique indexes are still required").isFalse();
            assertThat(withGate3(everyIndexValid()).gate3PciNotApplicable()).isFalse();
        } finally {
            emptyCollectionRegistry();
        }
    }

    /** The empty-plan veto stays for an unverified empty plan; a source that verified nothing qualifies lifts it. */
    @Test
    void anEmptyPlanOverARegisteredCollection_failsUnlessTheSourceVerifiedThereIsNothingToIndex() {
        PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
        try {
            assertThat(withGate3(c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of(), Set.of(), Set.of()))).gate3()).isFalse();
            assertThat(withGate3(c -> Optional.of(CompletionCheck.IdIndexCoverage.verifiedEmpty())).gate3()).isTrue();
            assertThat(withGate3(c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("unbuilt"), Set.of(), Set.of()))).gate3())
                .as("a qualifying collection with no index").isFalse();
        } finally {
            emptyCollectionRegistry();
        }
    }

    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));

    private void clearLatch() {
        ctx.deleteFrom(LATCH).execute();
    }

    /** linda 2026-11-22: gate 3 latches (INSERT ... ON CONFLICT DO NOTHING); the latch is read back whatever the live gate 3 says. */
    @Test
    void gate3IsLatchedTheFirstTimeItHolds_theFirstWritersRowStands_andTheLatchOutlivesTheLiveGate() {
        clearLatch();
        CompletionCheck.ReadSet rs = new CompletionCheck.ReadSet(List.of(), List.of());
        try {
            var quiet = CompletionCheck.evaluate(ctx, rs, everyIndexValid(), CompletionCheck.DIRECT, List.of(), CompletionCheck.LatchWriter.NONE);
            assertThat(quiet.gate3()).as("non-vacuity: gate 3 holds").isTrue();
            assertThat(quiet.gate3Latched()).as("a reader that writes nothing leaves no latch").isFalse();

            var held = CompletionCheck.evaluate(ctx, rs, everyIndexValid(), CompletionCheck.DIRECT, List.of(),
                CompletionCheck.latchOnCtx("engine-1"));
            assertThat(held.gate3Latched()).isTrue();
            assertThat(ctx.fetchCount(LATCH)).isEqualTo(1);

            CompletionCheck.evaluate(ctx, rs, everyIndexValid(), CompletionCheck.DIRECT, List.of(), CompletionCheck.latchOnCtx("engine-2"));
            assertThat(ctx.select(DSL.field(DSL.name("engine_version"), String.class)).from(LATCH).fetchOne(0, String.class))
                .as("ON CONFLICT DO NOTHING: the first writer's row stands").isEqualTo("engine-1");

            var live = CompletionCheck.evaluate(ctx, rs, c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("a"), Set.of(), Set.of("a"))),
                CompletionCheck.DIRECT, List.of(), CompletionCheck.LatchWriter.NONE);
            assertThat(live.gate3()).as("the live gate 3 is false (an INVALID id index)").isFalse();
            assertThat(live.idIndexInvalid()).isTrue();
            assertThat(live.gate3Latched()).as("the latch is not").isTrue();

            clearLatch();   // a latch row from before a restore
            assertThat(CompletionCheck.evaluate(ctx, rs, c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("a"), Set.of(), Set.of())),
                CompletionCheck.DIRECT, List.of(), CompletionCheck.LatchWriter.NONE).gate3Latched()).isFalse();

            // A writer that fails leaves it unlatched and the result intact: the next read tries again.
            var failed = CompletionCheck.evaluate(ctx, rs, everyIndexValid(), CompletionCheck.DIRECT, List.of(), (c, gate) -> {
                throw new IllegalStateException("read only");
            });
            assertThat(failed.gate3()).isTrue();
            assertThat(failed.gate3Latched()).isFalse();
        } finally {
            clearLatch();
        }
    }

    @Test
    void gate2NeedsItsOwnLabelsOnTopOfGate1() {
        register("ext-gate1", "chunks", null);
        complete("ext-gate1");
        CompletionCheck.ReadSet rs = new CompletionCheck.ReadSet(List.of("ext-gate1"), List.of("ext-swap"));
        CompletionCheck.Result open = CompletionCheck.evaluate(ctx, rs, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
        assertThat(open.gate1()).isTrue();
        assertThat(open.gate2()).as("the swap label has no row").isFalse();
        register("ext-swap", "chunks", null);
        complete("ext-swap");
        CompletionCheck.Result done = CompletionCheck.evaluate(ctx, rs, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
        assertThat(done.gate1()).isTrue();
        assertThat(done.gate2()).isTrue();
    }

    // ---- P3.3a's read set: the stamp labels and the seven id-twin validation labels (nexus-p2m1x.19) ---------------------

    @Test
    void p33asReadSetIsTheStampLabelsAndTheSevenTwinValidationLabels_allInGate1_andP31sReadSetIsUnchanged() {
        List<String> twinLabels = FkTwins.labels();
        assertThat(twinLabels).as("non-vacuity: seven twins").hasSize(7).doesNotHaveDuplicates()
            .allSatisfy(l -> assertThat(l).startsWith("rdr229-fkvalidate-").doesNotContain("/")).contains(FK_QUEUE_LABEL);
        assertThat(CompletionCheck.ReadSet.p33a().gate1Labels()).hasSize(23)
            .containsExactlyElementsOf(java.util.stream.Stream.concat(StampLabels.all().stream(), twinLabels.stream()).toList());
        assertThat(CompletionCheck.ReadSet.p33a().gate2Labels()).as("gate 2 adds nothing before P3.3c's read set").isEmpty();
        assertThat(CompletionCheck.ReadSet.p31().gate1Labels()).as("P3.1's read set is what it was").isEqualTo(StampLabels.all());
    }

    @Test
    void aTwinLabelIsCompleteOnlyWhileItsMoveIsCompleteAndItsTwinIsPresentAndValidated() throws Exception {
        FkTwins.Row topics = FkTwinSpecs.twin("topics");
        dropTwin("topics");
        addTwin("topics");   // NOT VALID, whatever an earlier case validated
        register(topics.label(), "topics", null);
        CompletionCheck.LabelState open = state(run(gate1(topics.label())), topics.label());
        assertThat(open.status()).as("a registered, open move").isEqualTo(CompletionCheck.Status.INCOMPLETE);
        assertThat(open.why()).isEqualTo("open");
        assertThat(open.table()).as("the label names its table").isEqualTo("topics");

        complete(topics.label());
        CompletionCheck.LabelState notValid = state(run(gate1(topics.label())), topics.label());
        assertThat(notValid.status()).as("the move is done and the twin is still NOT VALID").isEqualTo(CompletionCheck.Status.INCOMPLETE);
        assertThat(notValid.why()).contains(topics.twin()).contains("NOT VALID");

        validate("topics", topics.twin());
        assertThat(state(run(gate1(topics.label())), topics.label()).status()).as("done and validated").isEqualTo(CompletionCheck.Status.COMPLETE);
        assertThat(run(gate1(topics.label())).gate1()).isTrue();

        dropTwin("topics");
        try {
            CompletionCheck.LabelState gone = state(run(gate1(topics.label())), topics.label());
            assertThat(gone.status()).as("the twin is gone while its table is there").isEqualTo(CompletionCheck.Status.INCOMPLETE);
            assertThat(gone.why()).contains(topics.twin()).contains("missing");
        } finally {
            addTwin("topics");
        }
    }

    @Test
    void theQueuesTwinLabelAloneHoldsGate1OnAFreshInstall_noQueueMoveIsRegistered() {
        CompletionCheck.Result r = run(gate1(FK_QUEUE_LABEL));
        assertThat(ctx.fetchCount(MOVES)).as("no move is registered: nothing registers one when the table is absent").isZero();
        assertThat(r.labels().get(0).status()).isEqualTo(CompletionCheck.Status.NOT_APPLICABLE);
        assertThat(r.gate1()).isTrue();
    }

    // ---- P3.1's whole read set (last: it validates every CHECK of the walk) ----------------------------------------------

    @Test
    @Order(99)
    void p31sWholeReadSet_gate1And2HoldOnceEveryMoveIsCompleteAndEveryCheckValidated_gate3WaitsOnP34() throws Exception {
        int registered = 0;
        for (var e : P31Checks.checksByTable().entrySet()) {
            String table = e.getKey();
            if (table.equals(QUEUE)) {
                continue;   // absent on this install
            }
            register(StampLabels.labelFor(table), table, null);
            complete(StampLabels.labelFor(table));
            registered++;
            for (P31Checks.Row row : e.getValue()) {
                validate(table, row.constraint());
            }
        }
        assertThat(registered).as("non-vacuity: 15 moves (every table but the absent queue)").isEqualTo(15);
        CompletionCheck.Result r = run(CompletionCheck.ReadSet.p31());
        assertThat(r.incomplete()).isEmpty();
        assertThat(r.labelsSatisfied()).isEqualTo(16);
        assertThat(r.checks()).as("every listed CHECK of a table that exists was read").hasSize(19).allSatisfy(c -> {
            assertThat(c.present()).isTrue();
            assertThat(c.validated()).isTrue();
        });
        assertThat(r.violators()).isEmpty();
        assertThat(r.gate1()).isTrue();
        assertThat(r.gate2()).isTrue();
        assertThat(r.gate3()).isFalse();
        assertThat(r.gate3Why()).contains("P3.4");
    }

    @Test
    void p33asWholeReadSet_gate1And2HoldOnceEveryMoveIsCompleteEveryCheckAndEveryTwinValidated() throws Exception {
        int registered = 0;
        for (var e : P31Checks.checksByTable().entrySet()) {
            String table = e.getKey();
            if (table.equals(QUEUE)) {
                continue;   // absent on this install
            }
            register(StampLabels.labelFor(table), table, null);
            complete(StampLabels.labelFor(table));
            registered++;
            for (P31Checks.Row row : e.getValue()) {
                validate(table, row.constraint());
            }
        }
        CompletionCheck.Result before = run(CompletionCheck.ReadSet.p33a());
        assertThat(before.incomplete()).as("the stamp labels are done, the six twin labels have no row").hasSize(6);
        assertThat(before.gate1()).isFalse();
        for (FkTwins.Row r : FkTwinSpecs.alwaysThere()) {
            register(r.label(), r.table(), null);
            complete(r.label());
            registered++;
            validate(r.table(), r.twin());
        }
        assertThat(registered).as("non-vacuity: 15 stamp moves and 6 twin moves, every table but the absent queue").isEqualTo(21);
        CompletionCheck.Result r = run(CompletionCheck.ReadSet.p33a());
        assertThat(r.incomplete()).isEmpty();
        assertThat(r.labelsSatisfied()).as("23 labels, the two queue ones not applicable").isEqualTo(23);
        assertThat(r.labels().stream().filter(l -> l.status() == CompletionCheck.Status.NOT_APPLICABLE).map(CompletionCheck.LabelState::label))
            .containsExactlyInAnyOrder(QUEUE_LABEL, FK_QUEUE_LABEL);
        assertThat(r.gate1()).isTrue();
        assertThat(r.gate2()).isTrue();
        assertThat(r.gate3()).isFalse();
    }

    // ---- P3.3c's read set: the swap and validation labels in gate 2 (nexus-p2m1x.21) --------------------------------------

    private static final String CHUNKS_SWAP = LeafIndexPass.swapLabel("chunks");

    @Test
    void p33csReadSetKeepsP33asGate1_andReadsTheTwoSwapsAndTheThreeValidationsInGate2() {
        assertThat(CompletionCheck.ReadSet.p33c().gate1Labels()).isEqualTo(CompletionCheck.ReadSet.p33a().gate1Labels());
        assertThat(CompletionCheck.ReadSet.p33c().gate2Labels()).hasSize(5).doesNotHaveDuplicates()
            .allSatisfy(l -> assertThat(l).doesNotContain("/"))
            .contains(CHUNKS_SWAP, LeafIndexPass.swapLabel("taxonomy_centroids"))
            .containsAll(Rdr229SwapMove.CHILD_TABLES.stream().map(Rdr229SwapMove::validationLabel).toList());
    }

    /** Adds the id keys by the route that recurses (the same end state as the pass), registers and completes the swaps, runs their on_complete. */
    private void swap() throws Exception {
        for (var t : List.of(List.of("chunks", "chash"), List.of("taxonomy_centroids", "topic_id"))) {
            ctx.alterTable(DSL.table(DSL.name("nexus", t.get(0))))
                .add(DSL.constraint(DSL.name("cc_test_" + t.get(0) + "_id_uq")).unique(DSL.name("tenant_id"), DSL.name("collection_id"),
                    DSL.name(t.get(1)), DSL.name("embedding_model")))
                .execute();
            register(LeafIndexPass.swapLabel(t.get(0)), t.get(0), null);
        }
        for (String table : List.of("chunks", "taxonomy_centroids")) {
            MoveSql.runOnComplete(su, Rdr229SwapMove.spec(table).onComplete());
            complete(LeafIndexPass.swapLabel(table));
        }
        for (String child : Rdr229SwapMove.CHILD_TABLES) {
            complete(Rdr229SwapMove.validationLabel(child));
            validate(child, Rdr229SwapMove.childKeyConstraint(child));
        }
    }

    private void unswap() {
        for (String child : Rdr229SwapMove.CHILD_TABLES) {
            ctx.alterTable(DSL.table(DSL.name("nexus", child))).dropConstraintIfExists(DSL.name(Rdr229SwapMove.childKeyConstraint(child))).execute();
        }
        ctx.alterTable(DSL.table(DSL.name("nexus", "chunks"))).dropConstraintIfExists(DSL.name(Rdr229SwapMove.COMPOSITE_CONSTRAINT)).execute();
        for (String table : List.of("chunks", "taxonomy_centroids")) {
            ctx.alterTable(DSL.table(DSL.name("nexus", table))).dropConstraintIfExists(DSL.name("cc_test_" + table + "_id_uq")).execute();
        }
    }

    @Test
    void gate2HoldsOnceBothSwapsAndTheThreeValidationsAreCompleteWithTheirConstraintsValidated_andFallsWhenAValidatedConstraintIsDropped()
            throws Exception {
        try {
            swap();
            var readSet = new CompletionCheck.ReadSet(List.of(), CompletionCheck.ReadSet.p33c().gate2Labels());
            CompletionCheck.Result held = run(readSet);
            assertThat(held.incomplete()).as("every label of gate 2 is complete").isEmpty();
            assertThat(held.labelsSatisfied()).as("non-vacuity: five labels read").isEqualTo(5);
            assertThat(held.gate2()).isTrue();

            String child = "chunk_orphaned_at";
            ctx.alterTable(DSL.table(DSL.name("nexus", child))).dropConstraint(DSL.name(Rdr229SwapMove.childKeyConstraint(child))).execute();
            CompletionCheck.Result child1 = run(readSet);
            assertThat(child1.gate2()).as("a validation move is done but its validated id key is gone").isFalse();
            assertThat(state(child1, Rdr229SwapMove.validationLabel(child)).status()).isEqualTo(CompletionCheck.Status.INCOMPLETE);
            assertThat(state(child1, Rdr229SwapMove.validationLabel(child)).why()).contains(Rdr229SwapMove.childKeyConstraint(child))
                .contains("missing");

            ctx.alterTable(DSL.table(DSL.name("nexus", "chunks"))).dropConstraint(DSL.name(Rdr229SwapMove.COMPOSITE_CONSTRAINT)).execute();
            CompletionCheck.Result composite = run(readSet);
            assertThat(state(composite, CHUNKS_SWAP).status()).as("the chunks swap stands on the composite on the id").isEqualTo(CompletionCheck.Status.INCOMPLETE);
            assertThat(state(composite, CHUNKS_SWAP).why()).contains(Rdr229SwapMove.COMPOSITE_CONSTRAINT).contains("missing");
            assertThat(state(composite, LeafIndexPass.swapLabel("taxonomy_centroids")).status())
                .as("the centroids swap stands on its move alone").isEqualTo(CompletionCheck.Status.COMPLETE);
        } finally {
            unswap();
        }
    }
}
